"""Executor paper path on real XAUUSD@ ticks: persisted per-leg watermark (restart replays exactly the missed ticks,
EXE-02), bounded indexed reads only (EXE-01), legacy migration, account exposure incl. pending orders (RISK-01),
outcome settlement query (PERF-02)."""
import sqlite3
from pathlib import Path

import pytest

from tradingsystem.ai.store import DecisionRecord
from tradingsystem.core.settings import PathsCfg, RiskCfg, load_settings
from tradingsystem.core.timeutil import iso, now_ms
from tradingsystem.execution.backends.paper import PaperBackend, Tick
from tradingsystem.execution.executor import Executor
from tradingsystem.execution.risk_gate import ExecContext, evaluate
from tradingsystem.ingest.mt5.servertime import ServerTimeModel
from tradingsystem.storage.reader import InstrumentReader
from tradingsystem.storage.sqlite_store import SQLiteHotStore
from tradingsystem.storage.tablespec import spec_for

XAU = "mt5:XAUUSD@"


@pytest.fixture
def xticks(real_xau_ticks):
    t = real_xau_ticks
    return [Tick(int(ms), float(b), float(a), int(k)) for k, ms, b, a in
            zip(t["key"].to_pylist(), t["time_msc"].to_pylist(), t["bid"].to_pylist(), t["ask"].to_pylist())]


@pytest.fixture
def settings(tmp_path):
    s = load_settings(env_path=Path("nope.env"), extra_env={"EXECUTION_MODE": "paper", "EXECUTION_TRIGGER": "manual"})
    return s.model_copy(update={"paths": PathsCfg(data_dir=str(tmp_path / "data"), logs_dir=str(tmp_path / "logs"))})


def write_ticks(s, ticks):
    """Store real ticks in the instrument's hot DB exactly as the MT5 ingester lays them out."""
    from tradingsystem.core.instruments import InstrumentRegistry
    inst = InstrumentRegistry.from_settings(s).get(XAU)
    spec, model = spec_for(inst, "ticks"), ServerTimeModel()
    with SQLiteHotStore(inst.hot_db_path(s.paths.data())) as st:
        st.ensure_tables([spec])
        st.upsert(spec, [(t.key, t.time_msc, model.utc_to_server(t.time_msc), t.bid, t.ask, None, None, 134) for t in ticks])


def rec(side, order_type, sl, tps, valid_until_ms, entry=None, mgmt=None):
    return {"decision": side, "order_type": order_type, "stop_loss": sl, "valid_until": iso(valid_until_ms),
            "take_profits": [{"price": p, "close_fraction": f} for p, f in tps], "management": mgmt or [],
            "entry": {"price": entry}}


def place(pb, did, r, quote, lots=0.01, entry=None):
    return pb.place(decision_id=did, pair="XAUUSD", instrument=XAU, rec=r, lots=lots, entry=entry or quote.ask,
                    contract_size=100, volume_step=0.01, volume_min=0.01, quote=quote)


def exit_in_second_half(ticks, half):
    """A market trade whose TP is first reached only after ``half`` (SL never): exercises the downtime replay."""
    t0 = ticks[0]
    hi1, hi2 = max(t.bid for t in ticks[1:half]), max(t.bid for t in ticks[half:])
    if hi2 > hi1:
        return rec("BUY", "MARKET", round(min(t.bid for t in ticks) - 5, 2), [(round(hi1 + 0.01, 2), 1.0)], t0.time_msc + 86_400_000)
    lo1, lo2 = min(t.ask for t in ticks[1:half]), min(t.ask for t in ticks[half:])
    assert lo2 < lo1, "fixture has neither a new high nor a new low after the split"
    return rec("SELL", "MARKET", round(max(t.ask for t in ticks) + 5, 2), [(round(lo1 - 0.01, 2), 1.0)], t0.time_msc + 86_400_000)


def test_restart_replays_ticks_missed_while_down(settings, xticks, tmp_path):
    half = 1500
    r = exit_in_second_half(xticks, half)
    write_ticks(settings, xticks[:half])
    ex = Executor(settings)
    assert place(ex.paper, "d1", r, xticks[0], entry=xticks[0].ask if r["decision"] == "BUY" else xticks[0].bid)["ok"]
    ex.advance_paper()
    [leg] = ex.paper.decision_legs("d1")
    assert leg["status"] == "open" and leg["eval_key"] == xticks[half - 1].key      # watermark persisted
    ex.paper.close()
    write_ticks(settings, xticks[half:])                                             # ticks stored while we were down
    ex2 = Executor(settings)                                                         # restart: nothing in memory
    ex2.advance_paper()
    [after] = ex2.paper.decision_legs("d1")
    ref = PaperBackend(tmp_path / "ref.db", 100)                                     # one uninterrupted pass
    place(ref, "d1", r, xticks[0], entry=xticks[0].ask if r["decision"] == "BUY" else xticks[0].bid)
    ref.process(XAU, xticks[1:])
    [want] = ref.decision_legs("d1")
    assert want["status"] == "closed" and want["close_ms"] > xticks[half - 1].time_msc
    for k in ("status", "close_reason", "close_price", "close_ms", "pnl_usd"):
        assert after[k] == want[k], k
    ex2.advance_paper()                                                              # idempotent: nothing re-applied
    assert ex2.paper.account()["balance"] == pytest.approx(100 + want["pnl_usd"], abs=0.01)


def test_reads_are_bounded_and_skipped_without_legs(settings, xticks, monkeypatch):
    write_ticks(settings, xticks)
    calls = []
    orig = InstrumentReader.read_range

    def spy(self, spec, start_ms=None, end_ms=None, columns=None):
        calls.append((start_ms, end_ms))
        return orig(self, spec, start_ms, end_ms, columns)

    monkeypatch.setattr(InstrumentReader, "read_range", spy)
    ex = Executor(settings)
    ex.advance_paper()
    assert calls == []                                               # no pending/open leg → no tick read at all
    place(ex.paper, "d1", rec("BUY", "MARKET", 1.0, [(99_999.0, 1.0)], xticks[0].time_msc + 86_400_000), xticks[0])
    ex.advance_paper()
    ex.advance_paper()
    assert calls and all(s is not None and e is not None and 0 < e - s <= 3_600_000 for s, e in calls)
    assert calls[-1][0] >= xticks[-1].time_msc                        # steady state starts at the last evaluated tick
    q = ex.latest_quote(XAU, None)
    assert q.key == xticks[-1].key and ex.latest_quote(XAU) is None  # fixture ticks are days old → not "fresh"


def test_replayed_ticks_are_never_applied_twice(tmp_path, xticks):
    """Ticks a leg already saw are never applied again (fills, TP/SL hits). The declared management rules are carried
    but executed by execution.management.PositionManager (Phase 3), not by the paper backend's tick loop."""
    pb = PaperBackend(tmp_path / "app.db", 10_000)
    t0 = xticks[0]
    r = rec("SELL", "MARKET", round(t0.ask + 30, 2), [(round(t0.bid - 0.3, 2), 0.5), (round(t0.bid - 40, 2), 0.5)],
            t0.time_msc + 86_400_000, mgmt=[{"action": "move_sl_to_breakeven", "trigger": "tp_hit", "value": 1}])
    place(pb, "d1", r, t0, lots=0.02, entry=t0.bid)
    pb.process(XAU, xticks[1:2000])
    before = pb.decision_legs("d1")
    assert pb.process(XAU, xticks[1:2000]) == []                     # full overlap → no event, no state change
    assert pb.decision_legs("d1") == before
    ref = PaperBackend(tmp_path / "ref.db", 10_000)
    place(ref, "d1", r, t0, lots=0.02, entry=t0.bid)
    ref.process(XAU, xticks[1:])
    pb.process(XAU, xticks[1000:])                                   # overlapping catch-up == single pass
    assert pb.decision_legs("d1") == ref.decision_legs("d1")


def test_legacy_legs_get_a_watermark(tmp_path, xticks):
    db = tmp_path / "app.db"
    con = sqlite3.connect(db)
    con.execute("""CREATE TABLE paper_legs (id TEXT PRIMARY KEY, decision_id TEXT NOT NULL, pair TEXT NOT NULL,
        instrument TEXT NOT NULL, side TEXT NOT NULL, order_type TEXT NOT NULL, order_price REAL, volume REAL NOT NULL,
        contract_size REAL NOT NULL, sl REAL NOT NULL, tp REAL, tp_index INTEGER, status TEXT NOT NULL,
        created_ms INTEGER NOT NULL, expires_ms INTEGER, fill_price REAL, fill_ms INTEGER, close_price REAL,
        close_ms INTEGER, close_reason TEXT, pnl_usd REAL, management TEXT)""")
    t0, t5 = xticks[0], xticks[5]
    con.execute("INSERT INTO paper_legs VALUES ('d:1','d','XAUUSD',?,'BUY','MARKET',NULL,0.01,100,1,2,1,'closed',?,NULL,"
                "?,?,2,?,'tp',1,'[]')", (XAU, t0.time_msc, t0.ask, t0.time_msc, t5.time_msc))
    con.execute("INSERT INTO paper_legs VALUES ('d:2','d','XAUUSD',?,'BUY','MARKET',NULL,0.01,100,1,99999,2,'open',?,NULL,"
                "?,?,NULL,NULL,NULL,NULL,'[]')", (XAU, t0.time_msc, t0.ask, t0.time_msc))
    con.commit()
    con.close()
    pb = PaperBackend(db, 100)
    legs = {l["id"]: l for l in pb.decision_legs("d")}
    assert legs["d:2"]["eval_key"] == t5.time_msc * 1000 + 999      # after the sibling's TP (breakeven moment)
    assert pb.watermarks() == {XAU: t5.time_msc * 1000 + 999}
    place(pb, "n1", rec("BUY", "MARKET", 1.0, [(99_999.0, 1.0)], t0.time_msc + 86_400_000), xticks[10])
    assert pb.decision_legs("n1")[0]["eval_key"] == xticks[10].key


RISK = RiskCfg()


def gate_ctx(acct, bid, ask):
    return ExecContext(now_ms=now_ms(), bid=bid, ask=ask, quote_age_s=0.5, market_open=True, atr=7.0,
                       stops_level_price=0.25, contract_size=100, volume_min=0.01, volume_step=0.01, volume_max=20,
                       equity=acct["equity"], open_positions=acct["open_positions"],
                       open_risk_pct_by_pair=acct["open_risk_pct_by_pair"],
                       realized_pnl_today_usd=acct["realized_today_usd"], unrealized_pnl_usd=acct["unrealized_usd"])


@pytest.mark.parametrize("n_open,n_pending,max_pos,ok", [
    (2, 2, 3, False),      # 2 positions + 2 pending orders = 4 decisions ≥ 3
    (2, 0, 3, True),
    (1, 1, 3, True),
    (0, 3, 3, False),      # pending orders alone take the slots
])
def test_pending_orders_and_repeat_positions_count(tmp_path, xticks, n_open, n_pending, max_pos, ok):
    pb = PaperBackend(tmp_path / "app.db", 1_000_000)
    t0 = xticks[0]
    far = now_ms() + 86_400_000
    for i in range(n_open):
        place(pb, f"o{i}", rec("BUY", "MARKET", round(t0.bid - 20, 2), [(round(t0.ask + 60, 2), 1.0)], far), t0)
    for i in range(n_pending):
        p = round(t0.ask - 5, 2)
        place(pb, f"p{i}", rec("BUY", "BUY_LIMIT", round(p - 20, 2), [(round(p + 60, 2), 1.0)], far, entry=p), t0, entry=p)
    acct = pb.account({XAU: t0})
    assert acct["open_positions"] == n_open + n_pending and acct["open_orders"] == n_pending
    per_leg = 0.01 * 100 / acct["equity"] * 100                          # $ risk of one 0.01-lot leg, % of equity
    assert acct.get("open_risk_pct_by_pair", {}).get("XAUUSD", 0) == pytest.approx(
        n_open * (t0.ask - round(t0.bid - 20, 2)) * per_leg + n_pending * 20 * per_leg, abs=1e-3)
    r = {"pair": "XAUUSD", "timestamp": iso(now_ms() - 10_000), "valid_until": iso(far), "decision": "BUY",
         "order_type": "MARKET", "entry": {"price": None}, "stop_loss": round(t0.bid - 20, 2),
         "take_profits": [{"price": round(t0.ask + 60, 2), "close_fraction": 1.0}], "confidence": 70,
         "risk_management": {"risk_percent_suggested": 0.5}}
    g = evaluate(r, "XAUUSD", gate_ctx(acct, t0.bid, t0.ask), RISK.model_copy(update={"max_open_positions": max_pos}), [])
    assert ("max_open_positions" not in [n for n, k, _ in g.checks if not k]) == ok


def test_single_pair_exposure_is_capped(tmp_path, xticks):
    """A pair outside every correlated group is its own group: stacked positions on it hit max_correlated_risk_pct."""
    pb = PaperBackend(tmp_path / "app.db", 10_000)
    t0 = xticks[0]
    far = now_ms() + 86_400_000
    for i in range(2):
        place(pb, f"o{i}", rec("BUY", "MARKET", round(t0.ask - 12, 2), [(round(t0.ask + 30, 2), 1.0)], far), t0, lots=0.04)
    acct = pb.account({XAU: t0})
    assert acct["open_risk_pct_by_pair"]["XAUUSD"] == pytest.approx(0.96, abs=0.01)
    r = {"pair": "XAUUSD", "timestamp": iso(now_ms() - 10_000), "valid_until": iso(far), "decision": "BUY",
         "order_type": "MARKET", "entry": {"price": None}, "stop_loss": round(t0.ask - 12, 2),
         "take_profits": [{"price": round(t0.ask + 30, 2), "close_fraction": 1.0}], "confidence": 70,
         "risk_management": {"risk_percent_suggested": 0.5}}
    g = evaluate(r, "XAUUSD", gate_ctx(acct, t0.bid, t0.ask), RISK, [["BTCUSDT", "ETHUSDT"]])
    assert "correlated_exposure" in [n for n, k, _ in g.checks if not k]


def test_settlement_only_touches_finished_unsettled_decisions(settings, xticks):
    write_ticks(settings, xticks)
    ex = Executor(settings)
    t0 = xticks[0]
    for did in ("done", "running"):
        ex.store.save(DecisionRecord(pair="XAUUSD", mode="test", trigger="test", status="valid", id=did, ts=t0.time_msc,
                                     recommendation={"decision": "BUY"}))
    lo, hi = min(t.bid for t in xticks[1:]), max(t.bid for t in xticks[1:])
    place(ex.paper, "done", rec("BUY", "MARKET", round(lo - 1, 2), [(round((t0.ask + hi) / 2, 2), 1.0)], now_ms() + 86_400_000), t0)
    place(ex.paper, "running", rec("BUY", "MARKET", round(lo - 1, 2), [(round(hi + 50, 2), 1.0)], now_ms() + 86_400_000), t0)
    assert ex.paper.unsettled_decisions() == []
    ex.advance_paper()                                               # "done" hits its TP → settled once
    assert ex.paper.unsettled_decisions() == []
    con = sqlite3.connect(ex.app_db)
    rows = dict(con.execute("SELECT id, outcome FROM ai_decisions").fetchall())
    pct = con.execute("SELECT outcome_pnl_pct, outcome_pnl_usd FROM ai_decisions WHERE id='done'").fetchone()
    con.close()
    assert rows == {"done": "closed_profit", "running": None}
    assert pct[0] == pytest.approx(pct[1] / ex.paper.start_equity * 100, abs=1e-3)
