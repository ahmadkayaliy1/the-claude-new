"""Decision metrics (Phase 4 §3.8 component 1) on the real BTCUSDT 1m fixture stored in a tmp data root.

Every expected number below is read off the fixture rows (bar index i = minutes after 2025-11-09T10:40Z) and written
out by hand in the comments: entries, stops and targets are placed around real prices, so what the walk must find is
checkable against the printed bars."""
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace as NS

import numpy as np
import pytest

from tradingsystem.ai.store import DecisionRecord, DecisionStore
from tradingsystem.core.instruments import InstrumentRegistry
from tradingsystem.core.settings import PathsCfg, load_settings
from tradingsystem.core.timeframes import Timeframe
from tradingsystem.core.timeutil import iso, now_ms
from tradingsystem.execution import metrics as mx
from tradingsystem.execution.backends.mt5_backend import MT5Backend
from tradingsystem.execution.management import ActionLog
from tradingsystem.execution.metrics import (MetricsJob, decision_exit, leg_exit, levels_of, mt5_outcome_detail,
                                             paper_outcome_detail, spread_at_gate, virtual_trade)
from tradingsystem.storage.reader import InstrumentReader
from tradingsystem.storage.sqlite_store import SQLiteHotStore
from tradingsystem.storage.tablespec import spec_for

PAIR = "BTCUSDT"
MIN = 60_000
T0 = 1762684800000                         # open time of fixture bar 0 (2025-11-09T10:40Z)
FAR = 1_900_000_000_000                    # a "now" long after every window (all bars closed, every wait over)


def ot(i: int) -> int:
    return T0 + i * MIN


@pytest.fixture
def env(tmp_path, real_candles_1m):
    s = load_settings(env_path=Path("nope.env"), extra_env={"EXECUTION_MODE": "paper"})
    s = s.model_copy(update={"paths": PathsCfg(data_dir=str(tmp_path / "data"), logs_dir=str(tmp_path / "logs"))})
    reg = InstrumentRegistry.from_settings(s)
    inst = reg.primary(PAIR)
    spec = spec_for(inst, "candles", Timeframe.M1)
    with SQLiteHotStore(inst.hot_db_path(s.paths.data())) as st:      # the real bars, laid out as the ingester does
        st.ensure_tables([spec])
        st.upsert(spec, list(zip(*(real_candles_1m[c].to_pylist() for c in spec.column_names))))
    readers: dict[str, InstrumentReader] = {}

    def reader(key):
        if key not in readers:
            readers[key] = InstrumentReader(reg.get(key), s.paths.data())
        return readers[key]

    app_db = tmp_path / "app.db"
    store = DecisionStore(app_db, "cfg")
    actions = ActionLog(app_db)
    job = MetricsJob(s, store, reg, reader, actions=actions)
    yield NS(s=s, reg=reg, inst=inst, store=store, reader=reader, actions=actions, job=job, app_db=app_db,
             t=real_candles_1m)
    store.close()
    actions.close()
    for r in readers.values():
        r.close()


def trade(side, order_type, entry, sl, tps, cycle, valid_min=60):
    return {"decision": side, "order_type": order_type, "entry": entry, "stop_loss": sl,
            "take_profits": [{"price": p, "close_fraction": round(1 / len(tps), 4)} for p in tps],
            "timestamp": iso(cycle), "valid_until": iso(cycle + valid_min * MIN)}


def no_trade(cycle):
    return {"decision": "NO_TRADE", "timestamp": iso(cycle), "valid_until": iso(cycle + 60 * MIN)}


def save(e, rec, *, record_ts, state=None, detail=None, virtual=None, payload_hash=None, status="valid"):
    rid = e.store.save(DecisionRecord(PAIR, "agent_per_pair", "t", status, recommendation=rec,
                                      payload_hash=payload_hash, ts=record_ts))
    if state:
        e.store.set_execution_state(rid, state, detail)
    if virtual:
        con = sqlite3.connect(e.app_db)
        con.execute("UPDATE ai_decisions SET virtual_outcome=?, virtual_r=? WHERE id=?", (*virtual, rid))
        con.commit()
        con.close()
    return rid


# --------------------------------------------------------------------------- virtual trades (not executed)
def test_gate_rejected_buy_scored_on_the_virtual_trade(env):
    """BUY MARKET at bar 550's open 104695.85, SL −150, targets +150/+300/+450 (104845.85 / 104995.85 / 105145.85).
    Bar 560 high 104946.81 → TP1 (tp1_first, 10 min); bar 565 high 105061.08 → TP2; bar 570 low 104450.00 ≤ SL
    104545.85 → stopped, TP3 never. MFE = (105061.08 − 104695.85)/150 = 2.435 R; MAE = (104695.85 − 104450)/150 =
    1.639 R (the stop bar's low counts, its high does not)."""
    e = 104695.85
    rec = trade("BUY", "MARKET", {"price": e}, e - 150, [e + 150, e + 300, e + 450], ot(550))
    gate = {"gate": [{"check": "confidence", "ok": False, "detail": "50 (≥55)"},
                     {"check": "spread_vs_sl", "ok": True, "detail": "spread 11.20 ≤ 10% of SL distance 150.00"}],
            "reason": "confidence: 50 (≥55)"}
    rid = save(env, rec, record_ts=ot(551), state="rejected", detail=gate, virtual=("tp1_first", 1.0))
    assert env.job.run(FAR) == 1
    m = env.store.metrics_of(rid)
    assert (m["mfe_r"], m["mae_r"]) == (2.435, 1.639)
    assert (m["tp1_hit"], m["tp2_hit"], m["tp3_hit"]) == (1, 1, 0)
    assert m["minutes_to_resolve"] == 10 and m["exit_reason"] == "tp"
    assert m["rejected_but_virtual_win"] == 1 and m["spread_at_gate"] == pytest.approx(11.2)
    assert m["slippage"] is None and m["commission"] is None and m["no_trade_counterfactual_atr"] is None
    d = m["detail"]
    assert d["basis"] == "virtual" and d["walk_outcome"] == "tp1_first" and d["spread_source"] == "gate_text"
    assert (d["fill_ms"], d["resolved_ms"], d["end_ms"]) == (ot(550), ot(560), ot(570))
    assert env.job.run(FAR) == 0                                     # computed once


def test_sell_limit_zone_uses_the_worst_edge_for_r(env):
    """SELL_LIMIT at 104750.38 inside the zone [104730.38, 104790.38] (the fill trigger is the price, as the virtual
    outcome); the R reference is the zone's bottom 104730.38 (worst SELL fill, as rr_computed): R = 104950.38 −
    104730.38 = 220. Fill on bar 794 (high 104752.65), TP1 104600.38 on bar 801 (low 104458.88, 24 min after the
    cycle at bar 777), TP2 104450.38 on bar 802 (low 104324.03) → the trade ends there. MFE = (104730.38 −
    104324.03)/220 = 1.847; MAE = (104762.30 − 104730.38)/220 = 0.145 (bar 795's high)."""
    p = 104750.38
    rec = trade("SELL", "SELL_LIMIT", {"price": p, "range_min": p - 20, "range_max": p + 40}, p + 200,
                [p - 150, p - 300], ot(777))
    rid = save(env, rec, record_ts=ot(778), state="expired", detail={"reason": "expired"}, virtual=("tp1_first", 0.75))
    env.job.run(FAR)
    m = env.store.metrics_of(rid)
    assert (m["mfe_r"], m["mae_r"]) == (1.847, 0.145)
    assert (m["tp1_hit"], m["tp2_hit"], m["tp3_hit"]) == (1, 1, None)       # two targets: tp3 does not exist
    assert m["minutes_to_resolve"] == 24 and m["exit_reason"] == "tp"
    assert m["rejected_but_virtual_win"] == 0                               # expired, not refused by the gate
    assert m["detail"]["entry"] == pytest.approx(104730.38) and m["detail"]["fill_ms"] == ot(794)
    assert m["detail"]["end_ms"] == ot(802)


def test_pending_order_never_triggered(env):
    """BUY_LIMIT 2000 below bar 1000's open: never touched; not_triggered at valid_until (30 min)."""
    p = 106011.54 - 2000
    rec = trade("BUY", "BUY_LIMIT", {"price": p}, p - 200, [p + 300], ot(1000), valid_min=30)
    rid = save(env, rec, record_ts=ot(1001), virtual=("not_triggered", 0.0))
    env.job.run(FAR)
    m = env.store.metrics_of(rid)
    assert m["exit_reason"] == "not_triggered" and m["minutes_to_resolve"] == 30
    assert m["mfe_r"] is None and m["mae_r"] is None and m["tp1_hit"] is None


def test_virtual_trade_stopped_on_its_fill_bar_keeps_both_excursions(env):
    """BUY MARKET at bar 570's open 104647.30, SL 104547.30 (R 100): bar 570's low 104450.00 stops it on the fill bar.
    Its high does not count (the order inside the bar is unknown) → the fill price is the favourable extreme: MFE 0,
    MAE (104647.30 − 104450)/100 = 1.973 — stored, not NULL. A BUY_LIMIT 104600 in the zone [104580, 104620] (R =
    104620 − 104500 = 120) filled and stopped on the same bar: MFE (104600 − 104620)/120 = −0.167, MAE 1.417."""
    e = 104647.30
    rec = trade("BUY", "MARKET", {"price": e}, e - 100, [e + 150], ot(570))
    rid = save(env, rec, record_ts=ot(571), state="rejected", detail={"reason": "x"}, virtual=("sl_first", -1.0))
    assert env.job.run(FAR) == 1
    m = env.store.metrics_of(rid)
    assert (m["mfe_r"], m["mae_r"]) == (0.0, 1.973)
    assert (m["tp1_hit"], m["exit_reason"], m["minutes_to_resolve"]) == (0, "sl", 0)
    lv = levels_of(trade("BUY", "BUY_LIMIT", {"price": 104600.0, "range_min": 104580.0, "range_max": 104620.0},
                         104500.0, [104800.0], ot(570)))
    c = env.reader(env.inst.key).read_range(spec_for(env.inst, "candles", Timeframe.M1), ot(570), ot(600),
                                            ["open_time", "high", "low"])
    vt = virtual_trade(lv, ot(570), c["open_time"], c["high"], c["low"])
    assert (vt.outcome, vt.fill_ms, vt.end_ms, vt.best, vt.worst) == ("sl_first", ot(570), ot(570), 104600.0, 104450.0)
    assert mx.excursions_r(lv, vt.best, vt.worst) == (-0.167, 1.417)


def test_walk_agrees_with_evaluate_virtual(env, monkeypatch):
    """The metrics walk decides exactly what executor.evaluate_virtual decided, on the same bars."""
    from tradingsystem.execution.executor import evaluate_virtual
    t = env.t
    o, rd = t["open"].to_numpy(), env.reader(env.inst.key)
    c = rd.read_range(spec_for(env.inst, "candles", Timeframe.M1), T0, ot(3000), ["open_time", "high", "low"])
    checked = 0
    for i0 in range(0, 2400, 60):
        for side, sign in (("BUY", 1), ("SELL", -1)):
            e = float(o[i0])
            rec = trade(side, "MARKET", {"price": e}, e - sign * 150, [e + sign * 150, e + sign * 300], ot(i0))
            vo, _ = evaluate_virtual(rec, rd, env.inst)
            vt = virtual_trade(levels_of(rec), ot(i0), c["open_time"], c["high"], c["low"])
            assert vt.outcome == vo, (i0, side)
            checked += vo is not None
    assert checked > 50


# --------------------------------------------------------------------------- executed trades
def test_executed_paper_trade_through_the_settlement(env):
    """BUY_STOP 104651.18 (cycle bar 782), SL 104451.18 (R 200), TP1 104751.18, TP2 105051.18. Paper legs: both filled
    at 104654.18 (stop slippage +3.00) during bar 792; TP1 leg closed 'tp' in bar 794, TP2 leg closed 'sl' in bar 802
    (last close → exit 'sl', 20 min after the cycle). Window bars 792…802: best high 104762.30 → MFE 0.556 R, low
    104324.03 → MAE 1.636 R; TP1 reached, TP2 not."""
    from tradingsystem.execution.executor import Executor
    ex = Executor(env.s)
    p = 104651.18
    rec = trade("BUY", "BUY_STOP", {"price": p}, p - 200, [p + 100, p + 400], ot(782))
    rid = ex.store.save(DecisionRecord(PAIR, "agent_per_pair", "t", "valid", recommendation=rec, ts=ot(783)))
    ex.store.set_execution_state(rid, "executed", {"mode": "paper", "spread_at_gate": 12.5,
                                                   "backend": {"ok": True, "legs": [f"{rid}:1", f"{rid}:2"]}})
    fill, c1, c2 = ot(792) + 30_000, ot(794) + 40_000, ot(802) + 10_000
    rows = [(f"{rid}:1", rid, PAIR, "mt5:BTCUSD@", "BUY", "BUY_STOP", p, 0.01, 1.0, p - 200, p + 100, 1, "closed",
             ot(783), ot(842), p + 3, fill, p + 100, c1, "tp", 0.97, "[]", 0),
            (f"{rid}:2", rid, PAIR, "mt5:BTCUSD@", "BUY", "BUY_STOP", p, 0.01, 1.0, p - 200, p + 400, 2, "closed",
             ot(783), ot(842), p + 3, fill, p - 200, c2, "sl", -2.03, "[]", 0)]
    con = sqlite3.connect(ex.app_db)
    con.executemany(f"INSERT INTO paper_legs VALUES ({','.join('?' * 23)})", rows)
    con.commit()
    con.close()
    ex._settle_paper_outcomes()
    od = json.loads(sqlite3.connect(ex.app_db).execute("SELECT outcome_detail FROM ai_decisions WHERE id=?",
                                                        (rid,)).fetchone()[0])
    assert od["venue"] == "paper" and (od["open_ms"], od["close_ms"]) == (fill, c2) and od["commission"] == 0.0
    assert [(lg["reason"], lg["close_ms"]) for lg in od["legs"]] == [("tp", c1), ("sl", c2)]
    assert ex.metrics.run() == 1
    m = ex.store.metrics_of(rid)
    assert (m["mfe_r"], m["mae_r"], m["tp1_hit"], m["tp2_hit"]) == (0.556, 1.636, 1, 0)
    assert m["exit_reason"] == "sl" and m["minutes_to_resolve"] == 20
    assert m["slippage"] == pytest.approx(3.0) and m["detail"]["slippage_adverse"] == pytest.approx(3.0)
    assert (m["commission"], m["swap"], m["spread_at_gate"]) == (0.0, 0.0, 12.5)
    assert m["detail"]["basis"] == "broker" and m["detail"]["bars"] == 11
    ex.paper.close()
    ex.store.close()


def test_executed_mt5_trade_model_close_costs_and_slippage(env):
    """SELL MARKET at bar 777's open 104690.38, SL 104890.38 (R 200), TP1 104540.38, TP2 104390.38. MT5 legs:
    '5001' closed by its TP in bar 782; '5002' closed 'other' in bar 790 — our model's applied close in
    position_actions → model_close (the last leg to close). Window 777…790: lowest low 104533.24 → MFE 0.786 R, highest
    high 104696.01 → MAE 0.028 R; TP1 reached (bar 782 low 104536.85), TP2 not. Costs: commission −0.10 + fee −0.02,
    swap −0.01; slippage mean(−1.5, −0.5) = −1.0 (fill below the request = worse for a SELL → adverse +1.0)."""
    e = 104690.38
    rec = trade("SELL", "MARKET", {"price": e}, e + 200, [e - 150, e - 300], ot(777))
    placed = [{"comment": "a", "slippage": -1.5}, {"comment": "b", "slippage": -0.5}, {"comment": "c", "note": "x"}]
    rid = save(env, rec, record_ts=ot(778), state="executed",
               detail={"mode": "demo", "backend": {"ok": True, "placed": placed}, "spread_at_gate": 18.0})
    r = {"filled": True, "pnl_usd": 1.23, "commission": -0.10, "swap": -0.01, "fee": -0.02, "open_ms": ot(777) + 2_000,
         "close_ms": ot(790), "exits": [
             {"leg": "5001", "comment": "a", "tp_index": 1, "filled": True, "reason": "tp", "open_ms": ot(777) + 2_000,
              "close_ms": ot(782) + 5_000},
             {"leg": "5002", "comment": "b", "tp_index": 2, "filled": True, "reason": "other",
              "open_ms": ot(777) + 2_000, "close_ms": ot(790)}]}
    env.store.set_outcome(rid, "closed_profit", 1.23, 1.2, 150.0, detail=mt5_outcome_detail(r))
    env.actions.record("model", "src-decision", 0, rid, "5002", "close", {"action": "close"}, "applied", {})
    env.job.run(FAR)
    m = env.store.metrics_of(rid)
    assert (m["mfe_r"], m["mae_r"], m["tp1_hit"], m["tp2_hit"]) == (0.786, 0.028, 1, 0)
    assert m["exit_reason"] == "model_close" and m["minutes_to_resolve"] == 13
    assert m["commission"] == pytest.approx(-0.12) and m["swap"] == pytest.approx(-0.01)
    assert m["slippage"] == pytest.approx(-1.0) and m["detail"]["slippage_adverse"] == pytest.approx(1.0)
    assert m["spread_at_gate"] == 18.0 and m["rejected_but_virtual_win"] == 0
    assert [lg["exit"] for lg in m["detail"]["legs"]] == ["tp", "model_close"]


def test_executed_rows_wait_for_the_broker_and_are_recomputed_on_a_newer_settlement(env):
    e = 104695.85
    rec = trade("BUY", "MARKET", {"price": e}, e - 150, [e + 150], ot(550))
    rid = save(env, rec, record_ts=ot(551), state="executed", detail={"mode": "demo"}, virtual=("tp1_first", 1.0))
    assert env.job.run(FAR) == 0                                     # a virtual outcome alone is not the trade's end
    env.store.set_outcome(rid, "closed_profit", 1.5, 1.5, 150.0)     # pre-Phase-4 style: no venue split
    assert env.job.run(FAR) == 1
    m = env.store.metrics_of(rid)
    assert m["exit_reason"] is None and m["commission"] is None and m["detail"]["no_outcome_detail"]
    assert m["detail"]["window_end"].startswith("settlement")
    con = sqlite3.connect(env.app_db)                                # the settlement is rewritten later
    con.execute("UPDATE ai_decisions SET outcome_ts=? WHERE id=?", (m["computed_ms"] + 1, rid))
    con.commit()
    con.close()
    split = {"venue": "mt5", "commission": -0.05, "swap": 0.0, "fee": 0.0, "open_ms": ot(550) + 1_000,
             "close_ms": ot(560) + 30_000, "legs": [{"leg": "7", "filled": True, "reason": "tp",
                                                     "close_ms": ot(560) + 30_000}]}
    con = sqlite3.connect(env.app_db)
    con.execute("UPDATE ai_decisions SET outcome_detail=? WHERE id=?", (json.dumps(split), rid))
    con.commit()
    con.close()
    assert env.job.run(FAR) == 1
    m2 = env.store.metrics_of(rid)
    assert m2["exit_reason"] == "tp" and m2["minutes_to_resolve"] == 10 and m2["commission"] == pytest.approx(-0.05)
    assert env.job.run(FAR) == 0


def test_not_filled_trade(env):
    p = 106011.54 - 2000
    rec = trade("BUY", "BUY_LIMIT", {"price": p}, p - 200, [p + 300], ot(1000), valid_min=30)
    rid = save(env, rec, record_ts=ot(1001), state="executed", detail={"mode": "paper", "backend": {"ok": True}})
    legs = [{"id": f"{rid}:1", "status": "expired", "close_reason": "expired before fill", "order_type": "BUY_LIMIT",
             "order_price": p, "fill_price": None, "fill_ms": None, "close_ms": ot(1030), "tp_index": 1}]
    env.store.set_outcome(rid, "not_filled", 0.0, 0.0, 0.0, detail=paper_outcome_detail(legs))
    env.job.run(FAR)
    m = env.store.metrics_of(rid)
    assert m["exit_reason"] == "not_triggered" and m["minutes_to_resolve"] == 30
    assert m["mfe_r"] is None and m["tp1_hit"] is None and m["slippage"] is None


def _settle_mt5(e, rid, open_ms, close_ms, reason):
    r = {"filled": True, "commission": 0.0, "swap": 0.0, "fee": 0.0, "open_ms": open_ms, "close_ms": close_ms,
         "exits": [{"leg": "6001", "tp_index": 1, "filled": True, "reason": reason, "open_ms": open_ms,
                    "close_ms": close_ms}]}
    e.store.set_outcome(rid, "closed_profit", 1.0, 1.0, 150.0, detail=mt5_outcome_detail(r))


def test_executed_trade_waits_for_the_bar_it_closed_in(env):
    """SELL MARKET at bar 777's open 104690.38, SL +200, one TP 104540.38 — reached in bar 782 (low 104536.85), closed
    by the broker 5 s into it. The housekeeping pass right after the settlement (close + 50 s, bar 782 still forming)
    writes nothing: scored then, the TP bar would be missing for good (a written row is final). Once bar 782 has
    closed: window 777…782 (6 bars), TP1 hit, MFE (104690.38 − 104536.85)/200 = 0.768, MAE (104696.01 − 104690.38)/200
    = 0.028 (bar 781's high)."""
    e = 104690.38
    rid = save(env, trade("SELL", "MARKET", {"price": e}, e + 200, [e - 150], ot(777)), record_ts=ot(778),
               state="executed", detail={"mode": "demo", "backend": {"ok": True}})
    close = ot(782) + 5_000
    _settle_mt5(env, rid, ot(777) + 2_000, close, "tp")
    assert env.job.run(close + 50_000) == 0 and env.store.metrics_of(rid) is None
    assert env.job.run(close + 50_000 + mx.NOT_READY_RETRY_MS) == 1
    m = env.store.metrics_of(rid)
    assert (m["tp1_hit"], m["mfe_r"], m["mae_r"], m["exit_reason"]) == (1, 0.768, 0.028, "tp")
    assert m["detail"]["bars"] == 6 and "partial" not in m["detail"]


def test_executed_trade_close_bar_not_stored_waits_then_scores_partial(env):
    """A trade closed in bar 3001, past the fixture's last stored bar (2999): bar 3001 has closed but is not stored
    (ingestion behind) → waits; still missing MISSING_BARS_GRACE_MS after the close → scored on bars 2990…2999, flagged."""
    e = 104956.77                                                    # bar 2990's open
    rid = save(env, trade("BUY", "MARKET", {"price": e}, e - 200, [e + 300], ot(2990)), record_ts=ot(2991),
               state="executed", detail={"mode": "demo", "backend": {"ok": True}})
    close = ot(3001) + 5_000
    _settle_mt5(env, rid, ot(2990) + 1_000, close, "sl")
    assert env.job.run(close + 2 * MIN) == 0
    assert env.job.run(close + mx.MISSING_BARS_GRACE_MS) == 1
    m = env.store.metrics_of(rid)
    assert m["detail"]["partial"].startswith("bars missing") and m["detail"]["bars"] == 10
    assert m["mfe_r"] == 0.381 and m["tp1_hit"] == 0                # bar 2993's high 105032.91


# --------------------------------------------------------------------------- NO_TRADE counterfactual
def test_no_trade_counterfactual_in_decision_atr(env):
    """Cycle at bar 1010 (03:30 UTC, a 15m close): the next four 15m bars are 1m bars 1010…1069. Price at the cycle =
    close of bar 1009, 105953.97; highest high 106300.01 (+346.04), lowest low 105876.00 (−77.97). ATR14 the model was
    shown: 120 → 346.04/120 = 2.884 (up), 0.650 (down)."""
    env.store.save_payload("ph-1", PAIR, {"timeframes": {"15m": {"indicators": {"atr14": 120.0}}}})
    rid = save(env, no_trade(ot(1010)), record_ts=ot(1011), payload_hash="ph-1")
    assert env.job.run(ot(1010) + 60 * MIN) == 0                     # the 4th bar has not closed yet
    assert env.job.run(ot(1011) + 61 * MIN) == 1
    m = env.store.metrics_of(rid)
    assert m["no_trade_counterfactual_atr"] == 2.884
    d = m["detail"]
    assert d["ref_price"] == pytest.approx(105953.97) and d["atr_source"] == "payload" and d["bars"] == 60
    assert (d["up_atr"], d["down_atr"]) == (2.884, 0.65) and d["window"] == [ot(1010), ot(1070)]
    assert m["exit_reason"] is None and m["mfe_r"] is None and m["rejected_but_virtual_win"] is None


def test_no_trade_mid_minute_cycle_and_frame_atr(env, real_candles_1m):
    """As-of 42 s into bar 1010: the window starts at bar 1011 (reference = close of bar 1010, 105991.98) and still
    ends at the 4th 15m close (bar 1070): up 308.03. No stored payload → ATR14 of the real 15m frame as of the cycle."""
    from tradingsystem.analysis import indicators as ind
    t = real_candles_1m
    spec15 = spec_for(env.inst, "candles", Timeframe.M15)
    col = {k: t[k].to_numpy() for k in spec15.column_names}
    first = 5                                                        # bar 5 = 10:45, the first whole 15m bar
    n = (1010 - first) // 15                                         # 15m bars closed at 03:30 (bar 1010)
    how = {"open_time": lambda x: int(x[0]), "open": lambda x: float(x[0]), "high": lambda x: float(x.max()),
           "low": lambda x: float(x.min()), "close": lambda x: float(x[-1])}
    agg = [tuple((how.get(k) or (lambda x: x.sum().item()))(col[k][first + 15 * j:first + 15 * (j + 1)])
                 for k in spec15.column_names) for j in range(n)]
    with SQLiteHotStore(env.inst.hot_db_path(env.s.paths.data())) as st:      # 15m bars built from the real 1m bars
        st.ensure_tables([spec15])
        st.upsert(spec15, agg)
    i_h, i_l, i_c = (spec15.column_names.index(k) for k in ("high", "low", "close"))
    atr = ind.atr(*(np.array([r[i] for r in agg]) for i in (i_h, i_l, i_c)))[-1]
    rid = save(env, no_trade(ot(1010) + 42_421), record_ts=ot(1012))
    env.job.run(FAR)
    m = env.store.metrics_of(rid)
    d = m["detail"]
    assert d["atr_source"] == "frame" and d["atr"] == pytest.approx(atr)
    assert d["ref_price"] == pytest.approx(105991.98) and d["window"] == [ot(1011), ot(1070)] and d["bars"] == 59
    assert m["no_trade_counterfactual_atr"] == round(308.03 / atr, 3)


def test_missing_bars_wait_then_score_what_exists(env):
    """Cycle at bar 2980: its window runs past the fixture's last bar (2999) — it waits, and after the grace period is
    scored on the 20 bars there are (flagged partial)."""
    env.store.save_payload("ph-2", PAIR, {"timeframes": {"15m": {"indicators": {"atr14": 100.0}}}})
    rid = save(env, no_trade(ot(2980)), record_ts=ot(2981), payload_hash="ph-2")
    end = ot(2975) + 60 * MIN                                        # floor(bar 2980, 15m) + 4 bars
    assert env.job.run(end + 5 * MIN) == 0
    env.job._later.clear()
    assert env.job.run(end + 7 * 3_600_000) == 1
    d = env.store.metrics_of(rid)["detail"]
    assert d["bars"] == 20 and d["partial"].startswith("bars missing")


def _drop_bars(e, first: int, last: int) -> None:
    """Bars first…last (fixture indexes) removed from the hot store — not ingested yet, or never there."""
    con = sqlite3.connect(e.inst.hot_db_path(e.s.paths.data()))
    con.execute(f"DELETE FROM {spec_for(e.inst, 'candles', Timeframe.M1).name} WHERE open_time BETWEEN ? AND ?",
                (ot(first), ot(last)))
    con.commit()
    con.close()


def _restore_bars(e) -> None:
    spec = spec_for(e.inst, "candles", Timeframe.M1)
    with SQLiteHotStore(e.inst.hot_db_path(e.s.paths.data())) as st:
        st.upsert(spec, list(zip(*(e.t[c].to_pylist() for c in spec.column_names))))


def test_no_trade_waits_for_the_window_last_bar(env):
    """The window of the cycle at bar 1010 ends with bar 1069; bars 1065…1069 not ingested yet 2 min after the end →
    waits (the old 10-min tolerance scored 55 bars for good); ingested → the full 60-bar window (2.884 ATR)."""
    env.store.save_payload("ph-7", PAIR, {"timeframes": {"15m": {"indicators": {"atr14": 120.0}}}})
    rid = save(env, no_trade(ot(1010)), record_ts=ot(1011), payload_hash="ph-7")
    _drop_bars(env, 1065, 1069)
    assert env.job.run(ot(1072)) == 0
    _restore_bars(env)
    assert env.job.run(ot(1072) + mx.NOT_READY_RETRY_MS) == 1
    m = env.store.metrics_of(rid)
    assert m["detail"]["bars"] == 60 and m["no_trade_counterfactual_atr"] == 2.884 and "partial" not in m["detail"]


def test_no_trade_short_gap_at_the_window_end_is_accepted_after_the_tolerance(env):
    """Bars 1065…1069 never arrive (a quiet market, a session break): scored COVER_TOL_MS after the window's end on the
    55 bars there are, as a complete window — no 6-hour wait for bars that do not exist."""
    env.store.save_payload("ph-8", PAIR, {"timeframes": {"15m": {"indicators": {"atr14": 120.0}}}})
    rid = save(env, no_trade(ot(1010)), record_ts=ot(1011), payload_hash="ph-8")
    _drop_bars(env, 1065, 1069)
    assert env.job.run(ot(1072)) == 0
    assert env.job.run(ot(1072) + mx.NOT_READY_RETRY_MS) == 0                  # 7 min after the end: still waiting
    assert env.job.run(ot(1072) + 2 * mx.NOT_READY_RETRY_MS) == 1              # 12 min ≥ COVER_TOL_MS
    d = env.store.metrics_of(rid)["detail"]
    assert d["bars"] == 55 and "partial" not in d


def test_forming_bar_is_never_scored(env):
    """Bar 560 (TP1) still forming → not measurable yet; once bar 570 (the stop) has closed the row is written."""
    e = 104695.85
    rec = trade("BUY", "MARKET", {"price": e}, e - 150, [e + 150, e + 300, e + 450], ot(550))
    rid = save(env, rec, record_ts=ot(551), state="rejected", detail={"reason": "x"}, virtual=("tp1_first", 1.0))
    assert env.job.run(ot(560) + 30_000) == 0
    assert env.job.run(ot(570) + 30_000) == 0                        # the stop bar is still forming
    assert env.job.run(ot(571)) == 0                                 # not measurable → asked again after a while
    assert env.job.run(ot(570) + 30_000 + mx.NOT_READY_RETRY_MS) == 1
    assert env.store.metrics_of(rid)["detail"]["end_ms"] == ot(570)


# --------------------------------------------------------------------------- the pass
def test_pass_is_bounded_oldest_first_and_isolates_failures(env, monkeypatch):
    env.store.save_payload("ph-3", PAIR, {"timeframes": {"15m": {"indicators": {"atr14": 100.0}}}})
    ids = [save(env, no_trade(ot(20 + 15 * k)), record_ts=ot(21 + 15 * k), payload_hash="ph-3") for k in range(5)]
    job = MetricsJob(env.s, env.store, env.reg, env.reader, max_per_pass=2)
    orig = job.compute

    def flaky(row, now):
        if row["id"] == ids[0]:
            raise RuntimeError("boom")
        return orig(row, now)

    monkeypatch.setattr(job, "compute", flaky)
    assert job.run(FAR) == 1                                         # ids[0] fails, ids[1] is written
    assert env.store.metrics_of(ids[1]) and not env.store.metrics_of(ids[2])
    assert job.run(FAR) == 2                                         # ids[0] waits (backoff); 2 and 3 next
    assert job.run(FAR) == 1 and all(env.store.metrics_of(i) for i in ids[1:])
    assert env.store.metrics_of(ids[0]) is None
    assert job.run(FAR + mx.ERROR_RETRY_MS) == 0                     # retried after the backoff, fails again
    monkeypatch.setattr(job, "compute", orig)
    assert job.run(FAR + mx.ERROR_RETRY_MAX_MS) == 1                 # recovered


def test_reads_are_bounded(env, monkeypatch):
    calls = []
    orig = InstrumentReader.read_range

    def spy(self, spec, start_ms=None, end_ms=None, columns=None):
        calls.append((start_ms, end_ms))
        return orig(self, spec, start_ms, end_ms, columns)

    monkeypatch.setattr(InstrumentReader, "read_range", spy)
    e = 104695.85
    save(env, trade("BUY", "MARKET", {"price": e}, e - 150, [e + 150], ot(550)), record_ts=ot(551),
         virtual=("tp1_first", 1.0))
    env.store.save_payload("ph-4", PAIR, {"timeframes": {"15m": {"indicators": {"atr14": 100.0}}}})
    save(env, no_trade(ot(1010)), record_ts=ot(1011), payload_hash="ph-4")
    assert env.job.run(FAR) == 2
    assert calls and all(s is not None and e_ is not None and 0 < e_ - s <= 26 * 3_600_000 for s, e_ in calls)


def test_pending_selection(env):
    e = 104695.85
    rec = trade("BUY", "MARKET", {"price": e}, e - 150, [e + 150], ot(550))
    save(env, rec, record_ts=ot(551))                                            # no virtual outcome yet
    save(env, rec, record_ts=ot(552), state="executed", detail={"mode": "paper"}, virtual=("tp1_first", 1.0))
    save(env, rec, record_ts=ot(553), status="invalid")
    save(env, no_trade(ot(1010)), record_ts=ot(1011), status="skipped")
    nt = save(env, no_trade(ot(1010)), record_ts=ot(1011))
    v = save(env, rec, record_ts=ot(554), virtual=("tp1_first", 1.0))
    got = [r["id"] for r in env.store.pending_metrics(10, no_trade_before_ms=ot(1011))]
    assert got == [v, nt]
    assert [r["id"] for r in env.store.pending_metrics(10, no_trade_before_ms=ot(1010))] == [v]
    assert [r["id"] for r in env.store.pending_metrics(10, no_trade_before_ms=ot(1011), exclude=[v])] == [nt]
    assert env.store.pending_metrics(10, no_trade_before_ms=FAR, pairs=["ETHUSDT"]) == []


# --------------------------------------------------------------------------- pieces
def test_leg_and_decision_exit_reasons():
    assert [leg_exit(r) for r in ("tp", "sl", "rule", "model", "cancelled (rule)", "cancelled (model)",
                                  "expired before fill", "cancelled", "open", "other")] == \
        ["tp", "sl", "rule_close", "model_close", "rule_close", "model_close", "not_triggered", "not_triggered",
         "open", None]
    assert leg_exit("other", "rule") == "rule_close" and leg_exit("cancelled", "model") == "model_close"
    legs = [{"filled": True, "close_ms": 5, "exit": "tp"}, {"filled": True, "close_ms": 9, "exit": "sl"}]
    assert decision_exit(legs) == "sl"
    assert decision_exit([{"filled": True, "close_ms": 9, "exit": "tp"}, {"filled": True, "close_ms": 9,
                                                                           "exit": "sl"}]) == "sl"   # tie → adverse
    assert decision_exit([{"filled": False, "exit": "not_triggered"}, {"filled": False, "exit": "rule_close"}]) == \
        "rule_close"
    assert decision_exit([{"filled": False, "exit": "not_triggered"}]) == "not_triggered"
    assert set(mx.VIRTUAL_EXIT.values()) <= set(mx.EXIT_REASONS)


def test_spread_at_gate_sources():
    assert spread_at_gate({"spread_at_gate": 0.3, "gate": [{"check": "spread_vs_sl", "detail": "spread 9.99 ≤ …"}]}) \
        == (0.3, "gate")
    assert spread_at_gate({"gate": [{"check": "spread_vs_sl", "ok": True,
                                     "detail": "spread 0.30 ≤ 10% of SL distance 5.00"}]}) == (0.3, "gate_text")
    assert spread_at_gate({"gate": [{"check": "confidence", "detail": "spread 1"}]}) == (None, None)
    assert spread_at_gate(None) == (None, None) and spread_at_gate({"reason": "no quote"}) == (None, None)


def test_old_set_outcome_signature_still_works(env):
    rid = save(env, {"decision": "BUY"}, record_ts=ot(1))
    env.store.set_outcome(rid, "closed_loss", -1.0, -1.0, -10.0)
    r = sqlite3.connect(env.app_db).execute("SELECT outcome, outcome_detail FROM ai_decisions WHERE id=?",
                                            (rid,)).fetchone()
    assert r == ("closed_loss", None)


def test_malformed_recommendation_is_recorded_not_retried(env):
    rid = save(env, {"decision": "BUY", "order_type": "MARKET"}, record_ts=ot(1), state="rejected",
               detail={"gate": [{"check": "x"}]}, virtual=("sl_first", -1.0))
    assert env.job.run(FAR) == 1
    m = env.store.metrics_of(rid)
    assert m["exit_reason"] == "sl" and "error" in m["detail"] and m["mfe_r"] is None
    assert env.job.run(FAR) == 0


# --------------------------------------------------------------------------- MT5 settlement split
IN, OUT, BUY, SELL = 0, 1, 0, 1
DID = "d" * 32
TAG = "ts:" + "d" * 20 + ":"


def _mt5(hist, deals_by_pos, shift_ms=3 * 3_600_000):
    b = object.__new__(MT5Backend)
    b.magic, b.adopt_magic, b.own_pairs, b.family, b.pair_by_symbol = 7, None, set(), {7}, {}
    b.t = NS(healthy=lambda: True)
    b.model = NS(server_to_utc=lambda ms, prefer="earlier": ms - shift_ms)      # server time = UTC+3
    b.mt5 = NS(orders_get=lambda: (), positions_get=lambda: (), history_orders_get=lambda a, z: hist,
               history_deals_get=lambda position: deals_by_pos.get(position), last_error=lambda: (1, "Success"),
               DEAL_ENTRY_IN=IN, DEAL_ENTRY_OUT=OUT, DEAL_ENTRY_OUT_BY=3, DEAL_TYPE_BUY=BUY, DEAL_REASON_SL=4,
               DEAL_REASON_TP=5, ORDER_STATE_EXPIRED=6, ORDER_STATE_CANCELED=2)
    return b


def _deal(entry, typ, price, vol, *, profit=0.0, commission=0.0, swap=0.0, fee=0.0, reason=0, t=0):
    return NS(entry=entry, type=typ, price=price, volume=vol, profit=profit, commission=commission, swap=swap, fee=fee,
              reason=reason, time_msc=t)


def test_decision_result_returns_the_cost_split_close_time_and_leg_reasons():
    srv = 1_790_000_000_000
    hist = [NS(comment=f"{TAG}1", magic=7, position_id=11, ticket=11),
            NS(comment=f"{TAG}2", magic=7, position_id=12, ticket=12),
            NS(comment=f"{TAG}3", magic=7, position_id=0, ticket=13, state=6, time_done_msc=srv + 9_000)]
    deals = {11: [_deal(IN, SELL, 84000, 0.01, commission=-0.05, t=srv),
                  _deal(OUT, BUY, 83628, 0.01, profit=3.72, reason=5, t=srv + 60_000)],
             12: [_deal(IN, SELL, 84000, 0.01, commission=-0.05, fee=-0.01, t=srv),
                  _deal(OUT, BUY, 84215, 0.01, profit=-2.15, swap=-0.01, reason=3, t=srv + 120_000)]}
    r = _mt5(hist, deals).decision_result(DID, 0)
    assert r["pnl_usd"] == pytest.approx(3.72 - 2.15 - 0.10 - 0.01 - 0.01)                    # unchanged meaning
    assert (r["commission"], r["swap"], r["fee"]) == (pytest.approx(-0.10), pytest.approx(-0.01), pytest.approx(-0.01))
    utc = srv - 3 * 3_600_000
    assert r["open_ms"] == utc and r["close_ms"] == utc + 120_000
    assert r["reasons"] == {"11": "tp", "12": "other", "13": "expired"}
    assert [(x["leg"], x["tp_index"], x["filled"]) for x in r["exits"]] == [("11", 1, True), ("12", 2, True),
                                                                            ("13", 3, False)]
    od = mt5_outcome_detail(r)
    assert od["venue"] == "mt5" and od["close_ms"] == r["close_ms"] and len(od["legs"]) == 3


def test_mt5_settlement_persists_the_split(env):
    """Executor._settle_mt5_outcomes writes the broker's split into outcome_detail (the only place it exists)."""
    from tradingsystem.execution.executor import Executor
    from tradingsystem.ingest.common.appdb import AppDB
    ex = object.__new__(Executor)
    ex.s, ex.app_db, ex.store, ex.mode, ex._warned = env.s, env.app_db, env.store, "demo", set()
    ex.appdb = AppDB(env.app_db)
    rid = env.store.save(DecisionRecord(PAIR, "agent_per_pair", "t", "valid", recommendation={"decision": "SELL"}))
    env.store.set_execution_state(rid, "executed", {"mode": "demo", "equity_at_entry": 100.0,
                                                    "backend": {"ok": True, "placed": [{"comment": "c1"}]}})
    res = {"filled": True, "pnl_usd": 2.5, "move": 30.0, "volume": 0.01, "legs": 1, "commission": -0.04, "swap": 0.0,
           "fee": 0.0, "open_ms": 1, "close_ms": 2, "reasons": {"9": "tp"},
           "exits": [{"leg": "9", "filled": True, "reason": "tp", "close_ms": 2}]}
    ex.mt5 = NS(decision_result=lambda did, since_s, legs=None: res)
    ex._settle_mt5_outcomes()
    row = sqlite3.connect(env.app_db).execute("SELECT outcome, outcome_detail FROM ai_decisions WHERE id=?",
                                              (rid,)).fetchone()
    od = json.loads(row[1])
    assert row[0] == "closed_profit" and od["commission"] == -0.04 and od["close_ms"] == 2
    assert od["legs"][0]["reason"] == "tp"
    ex.appdb.close()


def test_executor_housekeeping_runs_the_metrics(env):
    from tradingsystem.execution.executor import Executor
    ex = Executor(env.s)
    ex.store.save_payload("ph-5", PAIR, {"timeframes": {"15m": {"indicators": {"atr14": 100.0}}}})
    rid = ex.store.save(DecisionRecord(PAIR, "agent_per_pair", "t", "valid", recommendation=no_trade(ot(1010)),
                                       payload_hash="ph-5", ts=ot(1011)))
    assert isinstance(ex.metrics, MetricsJob)
    ex.step(False)
    assert ex.store.metrics_of(rid) is None                        # only in housekeeping
    ex.step(True)
    assert ex.store.metrics_of(rid)["no_trade_counterfactual_atr"] is not None
    ex.paper.close()
    ex.store.close()


def test_executor_step_survives_a_failing_metrics_pass(env, monkeypatch):
    from tradingsystem.execution.executor import Executor
    ex = Executor(env.s)
    monkeypatch.setattr(ex.metrics, "run", lambda now_ms=None: (_ for _ in ()).throw(RuntimeError("x")))
    assert ex.step(True)["mode"] == "paper"
    ex.paper.close()
    ex.store.close()


def test_now_ms_is_wall_clock_for_computed_ms(env):
    env.store.save_payload("ph-6", PAIR, {"timeframes": {"15m": {"indicators": {"atr14": 100.0}}}})
    rid = save(env, no_trade(ot(1010)), record_ts=ot(1011), payload_hash="ph-6")
    before = now_ms()
    env.job.run(ot(1011) + 61 * MIN)
    assert env.store.metrics_of(rid)["computed_ms"] >= before
