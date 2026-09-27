"""The model's actions on its live trades (D-043: Claude leads, code protects) — the action gate and the executor's
end-to-end handling on the paper backend with real XAUUSD@ ticks (shifted to now so the quotes are fresh)."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from tradingsystem.ai.store import DecisionRecord
from tradingsystem.core.settings import PathsCfg, PositionActionsCfg, load_settings
from tradingsystem.core.timeutil import iso, now_ms
from tradingsystem.execution import executor as ex_mod
from tradingsystem.execution.action_gate import ActionContext, evaluate
from tradingsystem.execution.backends.paper import Tick
from tradingsystem.execution.executor import Executor
from tradingsystem.execution.management import Leg, Venue

from .test_executor_paper import XAU, place, write_ticks

NOW = 1_790_500_000_000


def leg(key="t1", side="BUY", kind="position", volume=0.01, fill=100.0, sl=95.0, tp=110.0, tp_index=1, pair="XAUUSD"):
    return Leg(key=key, decision_id="d" * 32, pair=pair, symbol="XAUUSD@", side=side, kind=kind, volume=volume,
               fill=fill if kind == "position" else None, order_price=fill if kind == "order" else None, sl=sl, tp=tp,
               tp_index=tp_index, opened_ms=NOW - 600_000)


def venue(bid=104.0, ask=104.2, stops=0.25, age=1.0):
    return Venue(bid=bid, ask=ask, spread=round(ask - bid, 8), stops_level=stops, freeze_level=0.0, digits=2,
                 point=0.01, tick_size=0.01, volume_min=0.01, volume_step=0.01, quote_age_s=age)


def ctx(**kw):
    base = dict(now_ms=NOW, pair="XAUUSD", rec_ts_ms=NOW - 60_000, max_age_s=300, market_open=True,
                cfg=PositionActionsCfg(), applied_today=0, last_sl_change_ms=lambda k: None)
    base.update(kw)
    return ActionContext(**base)


def act(action, kind="position", **kw):
    return {"target": {"decision": "dddddddd", "kind": kind}, "action": action, "reason": "test", **kw}


def status(plans):
    return [(p.op, p.status) for p in plans]


# ------------------------------------------------------------------ gate: stops only tighten
def test_tighter_stop_applies_and_a_wider_one_is_refused():
    v = lambda lg: venue()                                    # noqa: E731
    assert status(evaluate(act("modify_sl", value=100.5), [leg()], v, ctx())) == [("set_sl", "apply")]
    p = evaluate(act("modify_sl", value=94.0), [leg()], v, ctx())[0]
    assert p.status == "rejected" and "tighter_only" in p.reason
    sell = leg(side="SELL", fill=100.0, sl=105.0, tp=90.0)
    assert evaluate(act("modify_sl", value=104.9), [sell], lambda lg: venue(bid=99.0, ask=99.2), ctx())[0].status == "apply"
    assert evaluate(act("modify_sl", value=106.0), [sell], lambda lg: venue(bid=99.0, ask=99.2), ctx())[0].status == "rejected"


def test_stop_beyond_the_price_is_refused_and_a_too_close_one_waits():
    v = lambda lg: venue(bid=104.0, ask=104.2, stops=0.25)    # noqa: E731
    wrong = evaluate(act("modify_sl", value=104.5), [leg()], v, ctx())[0]           # above the bid: would close now
    assert wrong.status == "rejected" and "protective_side" in wrong.reason
    close = evaluate(act("modify_sl", value=103.7), [leg()], v, ctx())[0]           # 0.3 < stops 0.25 + spread 0.2
    assert close.status == "deferred" and "stops_level" in close.reason


def test_one_stop_change_per_position_per_spacing_window():
    p = evaluate(act("modify_sl", value=100.5), [leg()], lambda lg: venue(),
                 ctx(last_sl_change_ms=lambda k: NOW - 5 * 60_000))[0]
    assert p.status == "rejected" and "sl_change_spacing" in p.reason


def test_action_wide_checks():
    v = lambda lg: venue()                                    # noqa: E731
    for c, name in ((ctx(rec_ts_ms=NOW - 400_000), "not_expired"), (ctx(market_open=False), "market_open"),
                    (ctx(applied_today=12), "daily_limit")):
        [p] = evaluate(act("modify_sl", value=100.5), [leg()], v, c)
        assert p.status == "rejected" and p.leg is None and name in p.reason
    [p] = evaluate(act("modify_sl", value=100.5), [leg(pair="BTCUSDT")], v, ctx())
    assert p.status == "rejected" and "own_target" in p.reason
    [p] = evaluate(act("modify_sl", value=100.5), [leg()], lambda lg: venue(age=45.0), ctx())
    assert p.status == "rejected" and "quote_fresh" in p.reason


# ------------------------------------------------------------------ gate: take-profit, close, cancel
def test_take_profit_side_and_leg_choice():
    v = lambda lg: venue()                                    # noqa: E731
    assert evaluate(act("modify_tp", value=108.0), [leg()], v, ctx())[0].status == "apply"
    assert evaluate(act("modify_tp", value=104.3), [leg()], v, ctx())[0].status == "rejected"   # inside stops level
    two = [leg("t1", tp=106.0, tp_index=1), leg("t2", tp=110.0, tp_index=2)]
    [p] = evaluate(act("modify_tp", value=112.0), two, v, ctx())
    assert p.status == "rejected" and "say which one" in p.reason
    [p] = evaluate(act("modify_tp", value=112.0, leg=2), two, v, ctx())
    assert p.status == "apply" and p.leg.key == "t2"


def test_close_fractions_respect_the_minimum_lot():
    v = lambda lg: venue()                                    # noqa: E731
    assert [(p.leg.key, p.volume) for p in evaluate(act("close", fraction=1.0), [leg()], v, ctx())] == [("t1", 0.01)]
    assert [(p.leg.key, p.volume) for p in evaluate(act("close", fraction=0.5), [leg()], v, ctx())] == [("t1", 0.01)]
    [p] = evaluate(act("close", fraction=0.3), [leg()], v, ctx())
    assert p.status == "rejected" and "cannot split" in p.reason
    two = [leg("t2", volume=0.02, tp_index=2), leg("t1", volume=0.02, tp_index=1)]
    assert [(p.leg.key, p.volume) for p in evaluate(act("close", fraction=0.5), two, v, ctx())] == [("t1", 0.02)]
    assert [(p.leg.key, p.volume) for p in evaluate(act("close", fraction=0.25), two, v, ctx())] == [("t1", 0.01)]


def test_cancel_applies_to_pending_orders_only():
    v = lambda lg: venue()                                    # noqa: E731
    order = leg(kind="order", fill=98.0)
    assert status(evaluate(act("cancel_order", kind="order"), [order, leg("t2")], v, ctx())) == [("cancel", "apply")]
    [p] = evaluate(act("cancel_order", kind="order"), [leg()], v, ctx())
    assert p.status == "rejected" and "own_target" in p.reason


# ------------------------------------------------------------------ executor end to end (paper, real ticks)
@pytest.fixture
def live(tmp_path, real_xau_ticks, monkeypatch):
    """A paper executor whose XAUUSD@ ticks end one second ago, with the market reported open."""
    t = real_xau_ticks
    shift = now_ms() - 1_000 - int(t["time_msc"].to_pylist()[-1])
    ticks = [Tick(int(ms) + shift, float(b), float(a), int(k) + shift * 1000) for k, ms, b, a in
             zip(t["key"].to_pylist(), t["time_msc"].to_pylist(), t["bid"].to_pylist(), t["ask"].to_pylist())]
    s = load_settings(env_path=Path("nope.env"), extra_env={"EXECUTION_MODE": "paper", "EXECUTION_TRIGGER": "manual",
                                                           "TS_INSTANCE": ""})
    s = s.model_copy(update={"paths": PathsCfg(data_dir=str(tmp_path / "data"), logs_dir=str(tmp_path / "logs"))})
    write_ticks(s, ticks)

    class Open:
        def is_open(self, ms):
            return True
    monkeypatch.setattr(ex_mod, "calendar_for", lambda *a, **k: Open())
    ex = Executor(s)
    yield ex, ticks
    ex.paper.close()


def trade_decision(ex, ticks, did="a" * 32):
    """An executed BUY with one open paper leg; returns its id."""
    q = ticks[-1]
    r = {"decision": "BUY", "order_type": "MARKET", "stop_loss": round(q.bid - 20, 2),
         "valid_until": iso(now_ms() + 3_600_000), "take_profits": [{"price": round(q.ask + 30, 2), "close_fraction": 1.0}],
         "management": [], "entry": {"price": None}}
    assert place(ex.paper, did, r, q)["ok"]
    rec = DecisionRecord("XAUUSD", "agent_per_pair", "t", "valid", recommendation={**r, "pair": "XAUUSD"}, id=did,
                         ts=now_ms() - 120_000)
    ex.store.save(rec)
    ex.store.set_execution_state(did, "executed", {"mode": "paper", "executed_levels": {"stop_loss": r["stop_loss"]}})
    return did, r


def model_decision(ex, actions, did="b" * 32):
    rec = {"pair": "XAUUSD", "decision": "NO_TRADE", "timestamp": iso(now_ms() - 30_000), "position_actions": actions,
           "price_reference": XAU}
    r = DecisionRecord("XAUUSD", "agent_per_pair", "t", "valid", recommendation=rec, id=did)
    r.actions_state = "pending"
    ex.store.save(r)
    return did


def rows(ex):
    con = sqlite3.connect(ex.app_db)
    try:
        return con.execute("SELECT source, action, leg, status, detail FROM position_actions ORDER BY id").fetchall()
    finally:
        con.close()


def events(ex, kind):
    con = sqlite3.connect(ex.app_db)
    try:
        return [json.loads(r[0]) for r in con.execute("SELECT detail FROM ingestion_events WHERE event=?", (kind,))]
    finally:
        con.close()


def test_executor_applies_a_tighter_stop_once(live):
    ex, ticks = live
    did, r = trade_decision(ex, ticks)
    new_sl = round(r["stop_loss"] + 5, 2)
    src = model_decision(ex, [{"target": {"decision": did[:8], "kind": "position"}, "action": "modify_sl",
                               "value": new_sl, "reason": "tighten behind the new higher low"}])
    ex.process_actions()
    [leg_row] = ex.paper.decision_legs(did)
    assert leg_row["sl"] == pytest.approx(new_sl)
    assert [(s, a, st) for s, a, _, st, _ in rows(ex)] == [("model", "modify_sl", "applied")]
    assert events(ex, "action_applied") and events(ex, "action_applied")[0]["pair"] == "XAUUSD"
    assert ex.store.pending_actions("XAUUSD", 0) == []                        # actions_state → done
    ex.store.set_actions_state(src, "pending")                                # replayed: never applied twice
    ex.process_actions()
    assert len(rows(ex)) == 1


def test_executor_refuses_a_wider_stop_and_an_unknown_target(live):
    ex, ticks = live
    did, r = trade_decision(ex, ticks)
    model_decision(ex, [{"target": {"decision": did[:8], "kind": "position"}, "action": "modify_sl",
                         "value": round(r["stop_loss"] - 5, 2), "reason": "x"},
                        {"target": {"decision": "cccccccc", "kind": "position"}, "action": "close", "fraction": 1.0,
                         "reason": "x"}])
    ex.process_actions()
    assert [(a, st) for _, a, _, st, _ in rows(ex)] == [("modify_sl", "rejected"), ("close", "rejected")]
    assert ex.paper.decision_legs(did)[0]["sl"] == pytest.approx(r["stop_loss"])
    assert len(events(ex, "action_rejected")) == 2


def test_executor_closes_on_the_models_request(live):
    ex, ticks = live
    did, _ = trade_decision(ex, ticks)
    model_decision(ex, [{"target": {"decision": did[:8], "kind": "position"}, "action": "close", "fraction": 1.0,
                         "reason": "target liquidity reached, momentum fading"}])
    ex.process_actions()
    [leg_row] = ex.paper.decision_legs(did)
    assert leg_row["status"] == "closed" and leg_row["close_reason"] == "model"
    assert rows(ex)[0][3] == "applied"


def test_disabled_actions_do_nothing(live):
    ex, ticks = live
    did, r = trade_decision(ex, ticks)
    model_decision(ex, [{"target": {"decision": did[:8], "kind": "position"}, "action": "close", "fraction": 1.0,
                         "reason": "x"}])
    ex.s = ex.s.model_copy(update={"execution": ex.s.execution.model_copy(update={
        "position_actions": ex.s.execution.position_actions.model_copy(update={"enabled": False})})})
    ex.process_actions()
    assert rows(ex) == [] and ex.paper.decision_legs(did)[0]["status"] == "open"


def test_management_time_stop_runs_through_the_executor(live):
    ex, ticks = live
    did, r = trade_decision(ex, ticks)
    con = sqlite3.connect(ex.app_db)
    con.execute("UPDATE paper_legs SET fill_ms=? WHERE decision_id=?", (now_ms() - 20 * 60_000, did))
    con.commit()
    con.close()
    detail = {"mode": "paper", "executed_levels": {"stop_loss": r["stop_loss"]},
              "executed_management": [{"action": "close_all", "trigger": "minutes_elapsed", "value": 10, "params": {}}]}
    ex.store.set_execution_state(did, "executed", detail)
    ex.manage_positions()
    [leg_row] = ex.paper.decision_legs(did)
    assert leg_row["status"] == "closed" and leg_row["close_reason"] == "rule"
    assert any(src == "rule" and st == "applied" for src, _, _, st, _ in rows(ex))


def test_an_ambiguous_short_id_is_rejected_not_guessed(live):
    ex, ticks = live
    one, _ = trade_decision(ex, ticks, did="a" * 32)
    two, _ = trade_decision(ex, ticks, did="a" * 8 + "d" * 24)
    model_decision(ex, [{"target": {"decision": "a" * 8, "kind": "position"}, "action": "close", "fraction": 1.0,
                         "reason": "x"}])
    ex.process_actions()
    assert [(a, st) for _, a, _, st, _ in rows(ex)] == [("close", "rejected")]
    assert "ambiguous" in events(ex, "action_rejected")[0]["text"]
    assert all(leg["status"] == "open" for d in (one, two) for leg in ex.paper.decision_legs(d))


def test_a_too_close_stop_waits_and_keeps_the_decision_pending(live):
    ex, ticks = live
    did, r = trade_decision(ex, ticks)
    q = ticks[-1]
    src = model_decision(ex, [{"target": {"decision": did[:8], "kind": "position"}, "action": "modify_sl",
                               "value": round(q.bid - 0.01, 2), "reason": "lock in"}])
    ex.process_actions()
    assert [(a, st) for _, a, _, st, _ in rows(ex)] == [("modify_sl", "deferred")]
    assert [d["id"] for d in ex.store.pending_actions("XAUUSD", 0)] == [src]      # retried on the next loop
    assert ex.paper.decision_legs(did)[0]["sl"] == pytest.approx(r["stop_loss"])
    assert not events(ex, "action_applied") and not events(ex, "action_rejected")


# ------------------------------------------------------------------ review fixes (Phase 3 adversarial review)
def test_a_deferred_stop_keeps_waiting_when_crossed_aged_or_the_market_closes():
    held = ctx(deferred_legs=frozenset({"t1"}), rec_ts_ms=NOW - 7_200_000)          # 2 h old: waits anyway
    crossed = evaluate(act("modify_sl", value=104.1), [leg()], lambda lg: venue(bid=103.9, ask=104.1), held)
    assert status(crossed) == [("set_sl", "deferred")]
    fresh = evaluate(act("modify_sl", value=104.1), [leg()], lambda lg: venue(bid=103.9, ask=104.1), ctx())
    assert status(fresh) == [("set_sl", "rejected")]                                # a new wrong-side stop: refused
    closed = evaluate(act("modify_sl", value=103.0), [leg()], lambda lg: venue(),
                      ctx(deferred_legs=frozenset({"t1"}), market_open=False))
    assert status(closed) == [("set_sl", "deferred")]
    fits = evaluate(act("modify_sl", value=103.0), [leg()], lambda lg: venue(), held)
    assert status(fits) == [("set_sl", "apply")]                                    # it fits now: applied
    looser = evaluate(act("modify_sl", value=103.0), [leg(sl=103.5)], lambda lg: venue(), held)
    assert status(looser) == [("set_sl", "rejected")]                               # already tighter: final


def test_single_leg_management_turns_nearer_targets_into_prices():
    from tradingsystem.execution.executor import single_leg_management
    tps = [{"price": 101.0, "close_fraction": 0.3}, {"price": 104.0, "close_fraction": 0.7}]
    rules = [{"action": "move_sl_to_breakeven", "trigger": "tp_hit", "value": 1, "params": {}},
             {"action": "trail_atr", "trigger": "tp_hit", "value": 2, "params": {"atr_mult": 1.5}},
             {"action": "close_all", "trigger": "minutes_elapsed", "value": 240, "params": {}}]
    out, notes = single_leg_management(rules, tps, 1)
    assert out[0] == {"action": "move_sl_to_breakeven", "trigger": "price_reached", "value": 101.0, "params": {}}
    assert out[1]["trigger"] == "minutes_elapsed" and len(out) == 2 and len(notes) == 2 and "dropped" in notes[1]


def test_a_single_leg_is_numbered_by_its_target_and_its_plan_rewritten(live):
    ex, ticks = live
    q = ticks[-1]
    r = {"decision": "BUY", "order_type": "MARKET", "stop_loss": round(q.bid - 20, 2),
         "valid_until": iso(now_ms() + 3_600_000),
         "take_profits": [{"price": round(q.ask + 10, 2), "close_fraction": 0.3},
                          {"price": round(q.ask + 30, 2), "close_fraction": 0.7}],
         "management": [{"action": "move_sl_to_breakeven", "trigger": "tp_hit", "value": 1, "params": {}}],
         "entry": {"price": None}}
    res = ex.paper.place(decision_id="e" * 32, pair="XAUUSD", instrument=XAU, rec=r, lots=0.01, entry=q.ask,
                         contract_size=100, volume_step=0.01, volume_min=0.01, quote=q)
    assert res["legs"] == ["e" * 32 + ":2"] and "single leg at TP2" in res["note"]
    [lg] = ex.paper.decision_legs("e" * 32)
    assert lg["tp_index"] == 2 and lg["tp"] == r["take_profits"][1]["price"]


def test_an_unknown_close_outcome_is_re_read_and_never_sent_twice(live, monkeypatch):
    from tradingsystem.execution.management import ActionResult
    ex, ticks = live
    did, _ = trade_decision(ex, ticks)
    real_close, sent = ex.model_legs.close, []

    def close_no_confirmation(lg, volume, reason=None):
        sent.append(volume)
        real_close(lg, volume, reason=reason)                         # it happens at the venue …
        return ActionResult(False, "unknown", "10012 TIMEOUT: outcome unknown", 10012)   # … unconfirmed
    monkeypatch.setattr(ex.model_legs, "close", close_no_confirmation)
    src = model_decision(ex, [{"target": {"decision": did[:8], "kind": "position"}, "action": "close",
                               "fraction": 1.0, "reason": "done"}])
    ex.process_actions()
    assert [(a, st) for _, a, _, st, _ in rows(ex)] == [("close", "pending")]
    ex.process_actions()                                              # re-read: the leg is closed → applied
    assert [(a, st) for _, a, _, st, _ in rows(ex)] == [("close", "applied")] and len(sent) == 1
    assert ex.store.pending_actions("XAUUSD", 0) == [] and events(ex, "action_applied")
    assert "reconciled" in json.loads(rows(ex)[0][4])


def test_an_infrastructure_error_is_reported_once_and_never_wakes_the_model(live, monkeypatch):
    ex, ticks = live
    did, _ = trade_decision(ex, ticks)
    model_decision(ex, [{"target": {"decision": did[:8], "kind": "position"}, "action": "close", "fraction": 1.0,
                         "reason": "x"}])

    def down(_):
        raise RuntimeError("MT5 could not list positions")
    monkeypatch.setattr(ex.model_legs, "legs_of", down)
    for _ in range(5):
        ex.process_actions()
    assert events(ex, "action_rejected") == [] and len(events(ex, "action_error")) == 1
    assert len(ex.store.pending_actions("XAUUSD", 0)) == 1                           # still to do


def test_the_decision_frame_waits_for_the_bar_just_closed(live, monkeypatch):
    ex, _ = live
    bar = ex.decision_bar_open_ms("XAUUSD")

    class Fr:
        def __init__(self, last):
            import numpy as np
            self.open_time = np.array([last - 900_000, last], dtype=np.int64)
            self.close = np.array([99.0, 105.0])
            self.high = self.low = self.close

        def __len__(self):
            return 2
    frames = {"last": bar - 900_000}                                  # the store still ends one bar earlier
    monkeypatch.setattr(ex_mod, "load_frame", lambda *a, **k: Fr(frames["last"]))
    assert ex._decision_frame("XAUUSD") is None and ex.decision_bar_close("XAUUSD") is None
    frames["last"] = bar                                              # the closing bar is stored now
    assert ex.decision_bar_close("XAUUSD") == pytest.approx(105.0)


def test_the_live_basis_needs_fresh_quotes_and_a_basis_in_line_with_its_history():
    from types import SimpleNamespace as NS
    ex = object.__new__(Executor)

    class Reg:
        def primary(self, pair):
            return NS(key="binance_spot:BTCUSDT")

        def with_role(self, pair, role):
            return [NS(key="mt5:BTCUSD@")]
    quotes = {"binance_spot:BTCUSDT": Tick(0, 100.0, 100.2, 0), "mt5:BTCUSD@": Tick(0, 150.0, 150.2, 0)}
    ex.reg, ex._basis, ex._warned = Reg(), {}, set()
    ex.s = NS(execution=NS(max_basis_deviation_pct=0.5))
    ex.latest_quote = lambda key, max_age_ms=None: quotes.get(key)
    ex.basis_history = lambda pair: [50.0] * 60
    assert ex._live_basis("BTCUSDT") == pytest.approx(50.0)
    ex._basis = {}
    ex.basis_history = lambda pair: [10.0] * 60                    # the analysis feed stalled: 40 off its median
    assert ex._live_basis("BTCUSDT") is None
    ex._basis, quotes["binance_spot:BTCUSDT"] = {}, None          # no fresh analysis quote
    assert ex._live_basis("BTCUSDT") is None


def test_a_newer_stop_supersedes_an_older_waiting_one(live):
    ex, ticks = live
    did, r = trade_decision(ex, ticks)
    q = ticks[-1]
    old = model_decision(ex, [{"target": {"decision": did[:8], "kind": "position"}, "action": "modify_sl",
                               "value": round(q.bid - 0.01, 2), "reason": "lock in"}], did="b" * 32)
    ex.process_actions()
    assert [(a, st) for _, a, _, st, _ in rows(ex)] == [("modify_sl", "deferred")]
    newer = model_decision(ex, [{"target": {"decision": did[:8], "kind": "position"}, "action": "modify_sl",
                                 "value": round(r["stop_loss"] + 5, 2), "reason": "give it room"}], did="c" * 32)
    con = sqlite3.connect(ex.app_db)                                  # the newer decision is newer
    con.execute("UPDATE ai_decisions SET ts=ts+1000 WHERE id=?", (newer,))
    con.commit()
    con.close()
    ex.process_actions()
    got = {(json.loads(req or "{}").get("reason"), st) for src, a, _, st, req in
           sqlite3.connect(ex.app_db).execute("SELECT source, action, leg, status, requested FROM position_actions")}
    assert ("give it room", "applied") in got and ("lock in", "skipped") in got
    assert ex.paper.decision_legs(did)[0]["sl"] == pytest.approx(round(r["stop_loss"] + 5, 2))


def test_a_model_close_of_half_a_split_trade_is_carried_out_once(live, monkeypatch):
    from tradingsystem.execution.management import ActionResult
    ex, ticks = live
    q = ticks[-1]
    did = "f" * 32
    r = {"decision": "BUY", "order_type": "MARKET", "stop_loss": round(q.bid - 20, 2),
         "valid_until": iso(now_ms() + 3_600_000),
         "take_profits": [{"price": round(q.ask + 10, 2), "close_fraction": 0.5},
                          {"price": round(q.ask + 30, 2), "close_fraction": 0.5}],
         "management": [], "entry": {"price": None}}
    assert ex.paper.place(decision_id=did, pair="XAUUSD", instrument=XAU, rec=r, lots=0.02, entry=q.ask,
                          contract_size=100, volume_step=0.01, volume_min=0.01, quote=q)["ok"]
    ex.store.save(DecisionRecord("XAUUSD", "agent_per_pair", "t", "valid", recommendation={**r, "pair": "XAUUSD"},
                                 id=did, ts=now_ms() - 120_000))
    ex.store.set_execution_state(did, "executed", {"mode": "paper"})
    real, sent = ex.model_legs.close, []

    def unconfirmed(lg, volume, reason=None):
        sent.append((lg.key, volume))
        real(lg, volume, reason=reason)
        return ActionResult(False, "unknown", "10012 TIMEOUT: outcome unknown", 10012)
    monkeypatch.setattr(ex.model_legs, "close", unconfirmed)
    model_decision(ex, [{"target": {"decision": did[:8], "kind": "position"}, "action": "close", "fraction": 0.5,
                         "reason": "take half"}])
    for _ in range(3):
        ex.process_actions()
    assert sent == [(did + ":1", 0.01)]                               # half = the TP1 leg, once
    legs = {lg["id"]: lg["status"] for lg in ex.paper.decision_legs(did)}
    assert legs == {did + ":1": "closed", did + ":2": "open"}


def test_close_and_cancel_need_no_basis_priced_actions_wait_for_it(live, monkeypatch):
    from types import SimpleNamespace as NS
    ex, ticks = live
    did, r = trade_decision(ex, ticks)
    real_primary = ex.reg.primary
    monkeypatch.setattr(ex.reg, "primary", lambda pair: NS(key="binance_usdm:XAUUSDT"))   # analysis ≠ execution
    monkeypatch.setattr(ex, "_live_basis", lambda pair: None)                           # … and no basis now
    model_decision(ex, [{"target": {"decision": did[:8], "kind": "position"}, "action": "modify_sl",
                         "value": round(r["stop_loss"] + 5, 2), "reason": "tighten"},
                        {"target": {"decision": did[:8], "kind": "position"}, "action": "close", "fraction": 1.0,
                         "reason": "out"}])
    ex.process_actions()
    assert [(a, st) for _, a, _, st, _ in rows(ex)] == [("close", "applied")]
    assert len(ex.store.pending_actions("XAUUSD", 0)) == 1                              # the stop move waits
    monkeypatch.setattr(ex.reg, "primary", real_primary)


def test_a_leg_the_venue_deferred_is_retried_while_the_other_is_done(live, monkeypatch):
    """A stop move on two legs: leg 1 applied, leg 2 deferred by the venue (freeze level) — leg 2 is checked again
    and applied later; the action is not closed with one leg left behind (final check, defect 1)."""
    from tradingsystem.execution.management import ActionResult
    ex, ticks = live
    q = ticks[-1]
    did = "e" * 32
    r = {"decision": "BUY", "order_type": "MARKET", "stop_loss": round(q.bid - 20, 2),
         "valid_until": iso(now_ms() + 3_600_000),
         "take_profits": [{"price": round(q.ask + 10, 2), "close_fraction": 0.5},
                          {"price": round(q.ask + 30, 2), "close_fraction": 0.5}],
         "management": [], "entry": {"price": None}}
    assert ex.paper.place(decision_id=did, pair="XAUUSD", instrument=XAU, rec=r, lots=0.02, entry=q.ask,
                          contract_size=100, volume_step=0.01, volume_min=0.01, quote=q)["ok"]
    ex.store.save(DecisionRecord("XAUUSD", "agent_per_pair", "t", "valid", recommendation={**r, "pair": "XAUUSD"},
                                 id=did, ts=now_ms() - 120_000))
    ex.store.set_execution_state(did, "executed", {"mode": "paper"})
    real, frozen = ex.model_legs.set_sl, {"on": True}

    def set_sl(lg, sl):
        if lg.key.endswith(":2") and frozen["on"]:
            return ActionResult(False, "deferred", "deferred: within the freeze level")
        return real(lg, sl)
    monkeypatch.setattr(ex.model_legs, "set_sl", set_sl)
    new_sl = round(r["stop_loss"] + 5, 2)
    src = model_decision(ex, [{"target": {"decision": did[:8], "kind": "position"}, "action": "modify_sl",
                               "value": new_sl, "reason": "tighten"}])
    ex.process_actions()
    ex.process_actions()
    assert [lg["sl"] for lg in ex.paper.decision_legs(did)] == [new_sl, r["stop_loss"]]
    assert [d["id"] for d in ex.store.pending_actions("XAUUSD", 0)] == [src]            # still waiting for :2
    frozen["on"] = False
    ex.process_actions()
    assert [lg["sl"] for lg in ex.paper.decision_legs(did)] == [new_sl, new_sl]
    assert ex.store.pending_actions("XAUUSD", 0) == []


def test_a_superseded_stop_ends_quietly(live):
    ex, ticks = live
    did, r = trade_decision(ex, ticks)
    q = ticks[-1]
    old = model_decision(ex, [{"target": {"decision": did[:8], "kind": "position"}, "action": "modify_sl",
                               "value": round(q.bid - 0.01, 2), "reason": "lock in"}], did="b" * 32)
    ex.process_actions()
    newer = model_decision(ex, [{"target": {"decision": did[:8], "kind": "position"}, "action": "modify_sl",
                                 "value": round(r["stop_loss"] + 5, 2), "reason": "give it room"}], did="c" * 32)
    con = sqlite3.connect(ex.app_db)
    con.execute("UPDATE ai_decisions SET ts=ts+1000 WHERE id=?", (newer,))
    con.execute("UPDATE ai_decisions SET recommendation=json_set(recommendation, '$.timestamp', ?) WHERE id=?",
                (iso(now_ms() - 900_000), old))                          # the old decision is 15 min old by now
    con.commit()
    con.close()
    for _ in range(3):
        ex.process_actions()
    assert events(ex, "action_rejected") == []                               # no false "refused" wakes the model
    assert ex.store.pending_actions("XAUUSD", 0) == []
