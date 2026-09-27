"""Position management (P9.6, Phase 3): pure rule evaluation, the never-loosen property, the action log (idempotent,
restart-safe), paper end-to-end on the real XAUUSD@ tick fixture, and the hardened MT5 calls on the MetaTrader5 call
contract (SimpleNamespace fakes shaped like the terminal's named tuples)."""
import json
import random
import sqlite3
from pathlib import Path
from types import SimpleNamespace as NS

import numpy as np
import pytest

from tradingsystem.analysis import indicators as ind
from tradingsystem.analysis.structure import pivots
from tradingsystem.core.settings import ManagementCfg, PathsCfg, load_settings
from tradingsystem.core.timeutil import iso
from tradingsystem.execution.backends import mt5_backend as mt5mod
from tradingsystem.execution.backends.mt5_backend import MT5Backend
from tradingsystem.execution.backends.paper import PaperBackend, Tick
from tradingsystem.execution.management import (STEP, ActionLog, ActionResult, Leg, MT5Legs, PaperLegs,
                                                PositionManager, RuleContext, Venue, breakeven_level,
                                                evaluate_rules, partial_volume, rules_of, to_tick)
from tradingsystem.ingest.mt5.servertime import ServerTimeModel

XAU = "mt5:XAUUSD@"
MIN = 60_000


# --------------------------------------------------------------------------- helpers
def venue(bid, ask, stops=0.25, freeze=0.0, tick=0.01, vmin=0.01, step=0.01, age=0.5):
    return Venue(bid=bid, ask=ask, spread=round(ask - bid, 10), stops_level=stops, freeze_level=freeze, digits=2,
                 point=tick, tick_size=tick, volume_min=vmin, volume_step=step, quote_age_s=age)


def pos(key="d:2", side="BUY", fill=100.0, sl=98.0, vol=0.02, k=2, opened=0, tp=None):
    return Leg(key=key, decision_id="d", pair="XAUUSD", symbol=XAU, side=side, kind="position", volume=vol,
               fill=fill, sl=sl, tp=tp, tp_index=k, opened_ms=opened)


def closed_tp(k=1, side="BUY"):
    return Leg(key=f"d:{k}", decision_id="d", pair="XAUUSD", symbol=XAU, side=side, kind="closed", volume=0.01,
               fill=100.0, tp_index=k, closed_reason="tp")


def ctx(now=10 * MIN, bar=5 * MIN, atr=1.0, lo=None, hi=None, sl0=98.0, cfg=None, states=None, close=None, tf=MIN):
    states = states or {}
    return RuleContext(now_ms=now, decision_bar_open_ms=bar, atr=atr, swing_low=lo, swing_high=hi, original_sl=sl0,
                       cfg=cfg or ManagementCfg(), rule_state=lambda d, i, k: states.get((i, k)),
                       decision_bar_close=close, decision_tf_ms=tf)


def rule(action, trigger, value, **params):
    return {"action": action, "trigger": trigger, "value": value, "params": params}


def plans(rules, legs, v, c):
    return evaluate_rules(rules, legs, lambda lg: v, c)


# --------------------------------------------------------------------------- pure rules
def test_breakeven_buffer_math_both_sides():
    v = venue(103.0, 103.2, stops=0.5)                       # spread 0.2 → buffer max(0.2, 0.5 + 0.2) = 0.7
    assert breakeven_level(pos(), v, 1.0) == 100.7
    assert breakeven_level(pos(), v, 5.0) == 101.0            # spread × 5 = 1.0 wins
    assert breakeven_level(pos(side="SELL", fill=100.0, sl=102.0), venue(96.8, 97.0, stops=0.5), 1.0) == 99.3
    # a buffer that is not on the tick grid lands on the safe side (never below break-even for a BUY)
    assert breakeven_level(pos(fill=100.004), venue(103.0, 103.2, stops=0.5), 1.0) == 100.71
    [p] = plans([rule("move_sl_to_breakeven", "tp_hit", 1)], [closed_tp(), pos()], v, ctx())
    assert (p.action, p.status, p.value) == ("set_sl", "apply", 100.7) and "TP1 hit" in p.reason


def test_breakeven_deferred_when_too_close_and_skipped_when_not_tighter():
    r = [rule("move_sl_to_breakeven", "tp_hit", 1)]
    [p] = plans(r, [closed_tp(), pos()], venue(101.2, 101.4, stops=0.5), ctx())   # BE 100.7, bid 0.5 away < 0.7
    assert p.status == "deferred" and p.value == 100.7
    [p] = plans(r, [closed_tp(), pos(sl=100.9)], venue(103.0, 103.2, stops=0.5), ctx())
    assert p.status == "skipped" and "not tighter" in p.reason
    assert plans(r, [pos()], venue(103.0, 103.2), ctx()) == []                      # TP1 not hit → nothing
    other = Leg("d:1", "d", "XAUUSD", XAU, "BUY", "closed", 0.01, fill=100.0, tp_index=1, closed_reason="sl")
    assert plans(r, [other, pos()], venue(103.0, 103.2), ctx()) == []               # closed by SL is not tp_hit


def test_price_reached_direction_from_the_entry_and_the_side():
    up, down = rule("close_all", "price_reached", 102.0), rule("close_all", "price_reached", 99.0)
    assert plans([up], [pos()], venue(101.9, 102.1), ctx()) == []                    # BUY exits on the bid
    assert plans([up], [pos()], venue(102.0, 102.2), ctx())[0].action == "close"
    assert plans([down], [pos()], venue(99.1, 99.3), ctx()) == []
    assert plans([down], [pos()], venue(98.9, 99.1), ctx())[0].volume == 0.02
    s = pos(side="SELL", fill=100.0, sl=103.0)
    assert plans([rule("close_all", "price_reached", 98.0)], [s], venue(97.8, 98.1), ctx()) == []   # SELL: the ask
    assert plans([rule("close_all", "price_reached", 98.0)], [s], venue(97.8, 98.0), ctx())[0].action == "close"


def test_r_multiple_uses_the_original_stop():
    lg = pos(sl=99.5)                                         # stop already tightened; the risk was fill − 98 = 2
    v = venue(102.0, 102.2, stops=0.1)
    assert plans([rule("move_sl_to_breakeven", "r_multiple", 1.5)], [lg], v, ctx(sl0=98.0)) == []   # 1.0 R only
    [p] = plans([rule("move_sl_to_breakeven", "r_multiple", 1.0)], [lg], v, ctx(sl0=98.0))
    assert p.status == "apply" and "1.00R" in p.reason
    assert plans([rule("move_sl_to_breakeven", "r_multiple", 1.0)], [lg], v, ctx(sl0=None)) == []
    s = pos(side="SELL", fill=100.0, sl=102.0)
    assert plans([rule("close_all", "r_multiple", 2.0)], [s], venue(95.8, 96.0), ctx(sl0=102.0))[0].action == "close"


def test_minutes_elapsed_counts_from_the_fill_and_ignores_pending_orders():
    r = [rule("close_all", "minutes_elapsed", 30)]
    lg = pos(opened=0)
    order = Leg("d:3", "d", "XAUUSD", XAU, "BUY", "order", 0.01, order_price=99.0, sl=97.0, tp_index=3, opened_ms=0)
    assert plans(r, [lg, order], venue(100, 100.2), ctx(now=29 * MIN)) == []
    ps = plans(r, [lg, order], venue(100, 100.2), ctx(now=30 * MIN))
    assert [(p.leg_key, p.action) for p in ps] == [("d:2", "close"), ("d:3", "cancel")]   # time stop: close + cancel
    assert plans(r, [order], venue(100, 100.2), ctx(now=99 * MIN)) == []               # never filled → no fill time


def test_candle_close_only_after_the_fill_and_beyond_the_level():
    r = [rule("close_all", "candle_close", 99.0)]
    lg = pos(opened=10 * MIN)
    assert plans(r, [lg], venue(99.5, 99.7), ctx(bar=12 * MIN, close=99.2)) == []      # not beyond
    assert plans(r, [lg], venue(99.5, 99.7), ctx(bar=12 * MIN, close=98.9))[0].action == "close"
    assert plans(r, [lg], venue(99.5, 99.7), ctx(bar=8 * MIN, close=98.9, tf=MIN)) == []   # closed before the fill
    assert plans(r, [lg], venue(99.5, 99.7), ctx(bar=12 * MIN, close=None)) == []


@pytest.mark.parametrize("volume,fraction,want", [
    (0.05, 0.5, 0.02),        # 0.025 → rounded DOWN to the step
    (0.03, 0.34, 0.01),
    (0.01, 0.5, 0.01),        # the minimum lot cannot be split: ≥ 50 % closes the whole leg
    (0.01, 0.3, None),        # < 50 % of the minimum lot: nothing
    (0.02, 0.3, None),        # 0.006 → 0 lots: below the minimum
    (0.02, 0.6, 0.01),        # 0.012 → 0.01, leaves 0.01
    (0.015, 0.9, 0.015),      # 0.0135 → 0.01 would leave 0.005 < min → ≥ 50 % → all
])
def test_partial_close_rounding(volume, fraction, want):
    got, note = partial_volume(volume, fraction, 0.01, 0.01)
    assert got == (pytest.approx(want) if want is not None else None)
    if want is None:
        assert "cannot split the minimum lot" in note


def test_partial_close_plan_skips_with_reason():
    [p] = plans([rule("partial_close", "tp_hit", 1, fraction=0.3)], [closed_tp(), pos(vol=0.01)],
                venue(103, 103.2), ctx())
    assert p.status == "skipped" and "cannot split the minimum lot" in p.reason
    [p] = plans([rule("partial_close", "tp_hit", 1, fraction=0.5)], [closed_tp(), pos(vol=0.05)],
                venue(103, 103.2), ctx())
    assert (p.action, p.status, p.volume) == ("close", "apply", 0.02)


def test_trail_atr_once_per_bar_tighten_only_and_minimum_move():
    r = [rule("trail_atr", "tp_hit", 1, atr_mult=1.5)]
    legs = [closed_tp(), pos(sl=100.0)]
    v = venue(104.0, 104.2)
    [p] = plans(r, legs, v, ctx(atr=2.0, bar=5 * MIN))
    assert (p.status, p.value, p.trailing, p.bar_ms) == ("apply", 101.0, True, 5 * MIN)
    armed = {(0, "d:2"): {"status": "armed", "last_bar_ms": 5 * MIN}}
    assert plans(r, legs, v, ctx(atr=2.0, bar=5 * MIN, states=armed)) == []            # same bar: nothing
    [p] = plans(r, legs, v, ctx(atr=2.0, bar=6 * MIN, states=armed))                  # next bar: armed, no trigger
    assert p.status == "apply" and p.reason.startswith("armed")
    [p] = plans(r, [closed_tp(), pos(sl=101.5)], v, ctx(atr=2.0, bar=6 * MIN, states=armed))
    assert p.status == "bar_done" and "not tighter" in p.reason                      # never loosens
    [p] = plans(r, [closed_tp(), pos(sl=100.97)], v, ctx(atr=2.0, bar=6 * MIN, states=armed))
    assert p.status == "bar_done" and "ticks" in p.reason                            # 3 ticks < 5
    [p] = plans(r, legs, v, ctx(atr=float("nan"), bar=6 * MIN, states=armed))
    assert p.status == "noop"


def test_trail_structure_uses_the_swing_and_defers_when_too_close():
    r = [rule("trail_structure", "r_multiple", 1.0)]
    v = venue(104.0, 104.2, stops=0.3)                        # pad = 0.3 + 0.2
    [p] = plans(r, [pos(sl=98.0)], v, ctx(lo=102.0))
    assert (p.status, p.value) == ("apply", 101.5)
    armed = {(0, "d:2"): {"status": "armed", "last_bar_ms": 0}}
    [p] = plans(r, [pos(sl=98.0)], venue(101.8, 102.0, stops=0.3), ctx(lo=102.0, states=armed))   # 0.3 < 0.5 away
    assert (p.status, p.value) == ("deferred", 101.5)
    [p] = plans(r, [pos(sl=98.0)], v, ctx(lo=None))
    assert p.status == "bar_done"
    s = pos(side="SELL", fill=110.0, sl=112.0)
    [p] = plans(r, [s], venue(107.0, 107.2, stops=0.3), ctx(hi=108.0, sl0=112.0))
    assert (p.status, p.value) == ("apply", 108.5)


def test_stops_planned_in_one_pass_never_loosen_each_other():
    """Breakeven (100.7) then an ATR trail that computes 100.2: the trail must not undo the breakeven."""
    r = [rule("move_sl_to_breakeven", "tp_hit", 1), rule("trail_atr", "tp_hit", 1, atr_mult=1.0)]
    ps = plans(r, [closed_tp(), pos(sl=98.0)], venue(103.4, 103.6, stops=0.5), ctx(atr=3.2))
    assert [(p.rule, p.status, p.value) for p in ps] == [("move_sl_to_breakeven", "apply", 100.7),
                                                         ("trail_atr", "noop", 100.2)]
    ps = plans(r[::-1], [closed_tp(), pos(sl=98.0)], venue(103.4, 103.6, stops=0.5), ctx(atr=3.2))
    assert [(p.status, p.value) for p in ps] == [("apply", 100.2), ("apply", 100.7)]    # both tighten, in order


def test_deferred_and_pending_states_replan_without_the_trigger():
    r = [rule("move_sl_to_breakeven", "price_reached", 103.0)]
    st = {(0, "d:2"): {"status": "deferred"}}
    [p] = plans(r, [pos()], venue(102.0, 102.2, stops=0.1), ctx(states=st))            # price fell back: retried
    assert p.status == "apply" and p.reason.startswith("retry")
    assert plans(r, [pos()], venue(102.0, 102.2), ctx(states={(0, "d:2"): {"status": "done"}})) == []
    failed = {(0, "d:2"): {"status": "failed", "applied_ms": 10 * MIN - 30_000}}
    assert plans(r, [pos()], venue(103.1, 103.3), ctx(states=failed)) == []            # retry after 60 s only
    assert plans(r, [pos()], venue(103.1, 103.3), ctx(now=11 * MIN, states=failed))[0].status == "apply"
    dry = {(0, "d:2"): {"status": "dry_run"}}
    assert plans(r, [pos()], venue(103.1, 103.3), ctx(states=dry, cfg=ManagementCfg(dry_run=True))) == []
    assert plans(r, [pos()], venue(103.1, 103.3), ctx(states=dry))[0].status == "apply"   # dry run switched off


def test_unknown_rules_are_ignored_and_orders_only_get_close_all():
    order = Leg("d:1", "d", "XAUUSD", XAU, "BUY", "order", 0.01, order_price=99.0, sl=97.0, opened_ms=0)
    r = [{"action": "hedge", "trigger": "tp_hit", "value": 1}, rule("move_sl_to_breakeven", "price_reached", 98.0)]
    assert plans(r, [order], venue(98.0, 98.2), ctx()) == []
    assert plans([rule("close_all", "price_reached", 98.0)], [order], venue(97.9, 98.0), ctx())[0].action == "cancel"


def test_rules_of_prefers_executed_management_and_shifts_legacy_rules_by_the_placement_basis():
    rec = {"management": [rule("close_all", "price_reached", 100.0), rule("move_sl_to_breakeven", "tp_hit", 1)]}
    assert rules_of({"recommendation": rec, "execution_detail": {"executed_management": [rule("close_all", "tp_hit", 2)]}}) \
        == [rule("close_all", "tp_hit", 2)]
    got = rules_of({"recommendation": json.dumps(rec), "execution_detail": json.dumps({"translation": {"basis": -7.5}})})
    assert got[0]["value"] == 92.5 and got[1]["value"] == 1 and rec["management"][0]["value"] == 100.0


# --------------------------------------------------------------------------- the never-loosen property
def _random_rule(rng):
    act = rng.choice(["move_sl_to_breakeven", "partial_close", "trail_atr", "trail_structure", "close_all"])
    trig = rng.choice(["tp_hit", "price_reached", "r_multiple", "minutes_elapsed", "candle_close"])
    val = {"tp_hit": rng.randint(1, 3), "price_reached": round(rng.uniform(90, 110), 2),
           "r_multiple": round(rng.uniform(0.2, 3), 2), "minutes_elapsed": rng.randint(5, 60),
           "candle_close": round(rng.uniform(90, 110), 2)}[trig]
    params = {"partial_close": {"fraction": round(rng.uniform(0.05, 0.95), 2)},
              "trail_atr": {"atr_mult": round(rng.uniform(0.5, 5), 2)}}.get(act, {})
    return {"action": act, "trigger": trig, "value": val, "params": params}


def test_property_no_planned_stop_ever_loosens():
    """200 random rule sets on random price paths (seeded): every stop the plans apply is strictly tighter than the
    leg's stop at that moment, on the protective side of the price and at least stops level + spread away."""
    rng = random.Random(20260927)
    applied = 0
    for case in range(200):
        side = rng.choice(["BUY", "SELL"])
        buy = side == "BUY"
        fill = 100.0
        sl0 = round(fill - rng.uniform(0.5, 5) if buy else fill + rng.uniform(0.5, 5), 2)
        n = rng.randint(1, 3)
        tps = sorted((round(fill + rng.uniform(0.3, 8), 2) if buy else round(fill - rng.uniform(0.3, 8), 2)
                      for _ in range(n)), reverse=not buy)
        legs = [Leg(f"c{case}:{k + 1}", "d", "XAUUSD", XAU, side, "position", rng.choice([0.01, 0.02, 0.05]),
                    fill=fill, sl=sl0, tp=tps[k], tp_index=k + 1, opened_ms=0) for k in range(n)]
        rules = [_random_rule(rng) for _ in range(rng.randint(1, 6))]
        states: dict = {}
        px, bar, stops = fill, 0, rng.choice([0.0, 0.1, 0.3])
        for step in range(60):
            px = round(px + rng.gauss(0, 0.6), 2)
            spread = rng.choice([0.1, 0.2, 0.4])
            v = venue(px, round(px + spread, 2), stops=stops)
            bar = bar + MIN if step % 3 == 0 else bar
            for lg in legs:                                   # the venue closes legs at their stop / target
                if lg.kind != "position":
                    continue
                x = v.bid if buy else v.ask
                if (x <= lg.sl) if buy else (x >= lg.sl):
                    lg.kind, lg.closed_reason = "closed", "sl"
                elif lg.tp is not None and ((x >= lg.tp) if buy else (x <= lg.tp)):
                    lg.kind, lg.closed_reason = "closed", "tp"
            c = ctx(now=step * MIN, bar=bar, atr=rng.uniform(0.05, 3), lo=round(px - rng.uniform(0, 4), 2),
                    hi=round(px + rng.uniform(0, 4), 2), sl0=sl0, states=states, close=round(px + rng.gauss(0, 1), 2))
            by_key = {lg.key: lg for lg in legs}
            for p in evaluate_rules(rules, legs, lambda lg: v, c):
                lg, key = by_key[p.leg_key], (p.rule_idx, p.leg_key)
                if p.action == "set_sl" and p.status == "apply":
                    assert p.value is not None
                    assert (p.value > lg.sl) if buy else (p.value < lg.sl), (case, step, p)
                    dist = (v.bid - p.value) if buy else (p.value - v.ask)
                    assert dist >= v.stops_level + v.spread - 1e-9, (case, step, p)
                    lg.sl = p.value
                    applied += 1
                elif p.action == "close" and p.status == "apply":
                    lg.volume = round(lg.volume - p.volume, 8)
                    if lg.volume <= 1e-9:
                        lg.kind, lg.closed_reason = "closed", "rule"
                if p.status in ("apply", "skipped", "bar_done"):
                    states[key] = ({"status": "armed", "last_bar_ms": p.bar_ms} if p.trailing else {"status": "done"})
                elif p.status == "deferred":
                    states[key] = {"status": "deferred"}
                elif p.status == "noop" and p.trailing:
                    states.setdefault(key, {"status": "armed", "last_bar_ms": None})
    assert applied > 100                                      # the property was exercised, not vacuous


# --------------------------------------------------------------------------- the manager on an in-memory venue
class SimLegs:
    """A venue in memory (what the broker would hold): enforces nothing, records every call, so tests can check what
    the manager sends."""

    def __init__(self, legs, bid, ask, stops=0.25):
        self.legs = {lg.key: lg for lg in legs}
        self.bid, self.ask, self.stops = bid, ask, stops
        self.calls: list[tuple] = []
        self.fail_sl = None
        self.raise_on: str | None = None

    def legs_of(self, decision_id):
        if self.raise_on == "legs_of":
            raise RuntimeError("terminal down")
        return [Leg(**vars(lg)) for lg in self.legs.values() if lg.decision_id == decision_id]

    def venue(self, leg):
        return venue(self.bid, self.ask, stops=self.stops)

    def set_sl(self, leg, sl):
        self.calls.append(("set_sl", leg.key, sl))
        if self.raise_on == "set_sl":
            raise RuntimeError("connection reset")
        if self.fail_sl:
            return ActionResult(False, "failed", self.fail_sl, 10031)
        self.legs[leg.key].sl = sl
        return ActionResult(True, "applied", "done", 10009)

    def set_tp(self, leg, tp):
        raise AssertionError("rules never move a take-profit")

    def close(self, leg, volume):
        self.calls.append(("close", leg.key, volume))
        lg = self.legs[leg.key]
        lg.volume = round(lg.volume - volume, 8)
        if lg.volume <= 1e-9:
            lg.kind, lg.closed_reason = "closed", "other"
        return ActionResult(True, "applied", "closed", 10009)

    def cancel(self, leg):
        self.calls.append(("cancel", leg.key, None))
        self.legs[leg.key].kind, self.legs[leg.key].closed_reason = "closed", "cancelled"
        return ActionResult(True, "applied", "cancelled", 10009)


class FixedMarket:
    def __init__(self, bar=5 * MIN, atr=1.0, lo=None, hi=None, close=None):
        self.bar, self._atr, self.lo, self.hi, self.close = bar, atr, lo, hi, close

    def decision_bar_open_ms(self, pair):
        return self.bar

    def atr(self, pair):
        return self._atr

    def swing(self, pair, side):
        return self.lo if side == "low" else self.hi

    def decision_bar_close(self, pair):
        return self.close


@pytest.fixture
def settings(tmp_path):
    s = load_settings(env_path=Path("nope.env"), extra_env={"EXECUTION_MODE": "paper", "EXECUTION_TRIGGER": "manual"})
    return s.model_copy(update={"paths": PathsCfg(data_dir=str(tmp_path / "data"), logs_dir=str(tmp_path / "logs"))})


def with_mgmt(s, **kw):
    ex = s.execution.model_copy(update={"management": s.execution.management.model_copy(update=kw)})
    return s.model_copy(update={"execution": ex})


def decision(rules, side="BUY", sl0=98.0, did="d"):
    return {"id": did, "pair": "XAUUSD", "recommendation": {"decision": side, "management": rules},
            "execution_detail": {"executed_levels": {"stop_loss": sl0}, "executed_management": rules}}


def rows(db):
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute("SELECT * FROM position_actions ORDER BY id")]
    finally:
        con.close()


def test_manager_applies_once_records_and_survives_a_restart(tmp_path, settings):
    db = tmp_path / "app.db"
    sim = SimLegs([closed_tp(), pos()], 103.0, 103.2, stops=0.5)
    events = []
    rules = [rule("move_sl_to_breakeven", "tp_hit", 1)]
    pm = PositionManager(settings, ActionLog(db), sim, FixedMarket(), lambda k, p: events.append((k, p)))
    pm.manage([decision(rules)], 10 * MIN)
    pm.manage([decision(rules)], 10 * MIN + 1000)
    assert sim.calls == [("set_sl", "d:2", 100.7)]
    [r] = rows(db)
    assert (r["source"], r["source_decision"], r["seq"], r["target_decision"], r["leg"], r["action"], r["status"],
            r["pair"]) == ("rule", "d", 0, "d", "d:2", "set_sl", "applied", "XAUUSD")
    assert json.loads(r["requested"])["rule"] == "move_sl_to_breakeven"
    assert [k for k, _ in events] == ["mgmt_applied"]
    pm2 = PositionManager(settings, ActionLog(db), sim, FixedMarket(), lambda k, p: events.append((k, p)))
    sim.legs["d:2"].sl = 98.0                                # even if the broker lost it, the rule never re-fires
    pm2.manage([decision(rules)], 20 * MIN)
    assert len(sim.calls) == 1 and len(rows(db)) == 1


def test_manager_dry_run_records_and_touches_nothing_then_applies_when_switched_off(tmp_path, settings):
    db = tmp_path / "app.db"
    sim = SimLegs([closed_tp(), pos()], 103.0, 103.2, stops=0.5)
    rules = [rule("move_sl_to_breakeven", "tp_hit", 1)]
    PositionManager(with_mgmt(settings, dry_run=True), ActionLog(db), sim, FixedMarket(), lambda *a: None).manage(
        [decision(rules)], 10 * MIN)
    [r] = rows(db)
    assert r["status"] == "skipped" and json.loads(r["detail"])["dry_run"] is True and sim.calls == []
    PositionManager(settings, ActionLog(db), sim, FixedMarket(), lambda *a: None).manage([decision(rules)], 11 * MIN)
    [r] = rows(db)
    assert r["status"] == "applied" and sim.calls == [("set_sl", "d:2", 100.7)]


def test_manager_defers_then_applies_when_the_price_allows(tmp_path, settings):
    db = tmp_path / "app.db"
    sim = SimLegs([closed_tp(), pos()], 101.2, 101.4, stops=0.5)
    pm = PositionManager(settings, ActionLog(db), sim, FixedMarket(), lambda *a: None)
    rules = [rule("move_sl_to_breakeven", "tp_hit", 1)]
    pm.manage([decision(rules)], 10 * MIN)
    pm.manage([decision(rules)], 10 * MIN + 1000)
    assert sim.calls == [] and [r["status"] for r in rows(db)] == ["deferred"]
    sim.bid, sim.ask = 102.0, 102.2
    pm.manage([decision(rules)], 10 * MIN + 2000)
    assert sim.calls == [("set_sl", "d:2", 100.7)] and [r["status"] for r in rows(db)] == ["applied"]


def test_manager_deferred_move_is_closed_out_when_the_leg_closes(tmp_path, settings):
    db = tmp_path / "app.db"
    sim = SimLegs([closed_tp(), pos()], 101.2, 101.4, stops=0.5)
    pm = PositionManager(settings, ActionLog(db), sim, FixedMarket(), lambda *a: None)
    rules = [rule("move_sl_to_breakeven", "tp_hit", 1)]
    pm.manage([decision(rules)], 10 * MIN)
    sim.legs["d:2"].kind, sim.legs["d:2"].closed_reason = "closed", "sl"
    pm.manage([decision(rules)], 11 * MIN)
    [r] = rows(db)
    assert r["status"] == "skipped" and "before the action completed" in json.loads(r["detail"])["reason"]
    assert json.loads(r["requested"])["rule"] == "move_sl_to_breakeven"          # the original request is kept


def test_manager_failed_action_is_retried_after_a_minute_then_given_up(tmp_path, settings):
    db = tmp_path / "app.db"
    sim = SimLegs([closed_tp(), pos()], 103.0, 103.2, stops=0.5)
    sim.fail_sl = "10031 CONNECTION"
    events = []
    pm = PositionManager(settings, ActionLog(db), sim, FixedMarket(), lambda k, p: events.append(k))
    rules = [rule("move_sl_to_breakeven", "tp_hit", 1)]
    for t in (0, 1_000, 61_000, 62_000, 122_000, 200_000, 400_000):
        pm.manage([decision(rules)], 10 * MIN + t)
    assert len(sim.calls) == 3 and events.count("mgmt_error") == 1
    [r] = rows(db)
    assert r["status"] == "failed" and json.loads(r["detail"])["attempt"] == 3


def test_manager_backend_exception_leaves_pending_and_reconciles_without_resending(tmp_path, settings):
    """Sent, then the call raised (unknown outcome): the next loop sees the stop already there → applied, not resent."""
    db = tmp_path / "app.db"
    sim = SimLegs([closed_tp(), pos()], 103.0, 103.2, stops=0.5)
    sim.raise_on = "set_sl"
    events = []
    pm = PositionManager(settings, ActionLog(db), sim, FixedMarket(), lambda k, p: events.append(k))
    rules = [rule("move_sl_to_breakeven", "tp_hit", 1)]
    pm.manage([decision(rules)], 10 * MIN)
    assert [r["status"] for r in rows(db)] == ["pending"] and events == ["mgmt_error"]
    sim.raise_on = None
    sim.legs["d:2"].sl = 100.7                               # the broker did apply it
    pm.manage([decision(rules)], 10 * MIN + 1000)
    [r] = rows(db)
    assert r["status"] == "applied" and "reconciled" in json.loads(r["detail"]) and len(sim.calls) == 1


def test_manager_pending_close_is_not_sent_twice_after_a_crash(tmp_path, settings):
    db = tmp_path / "app.db"
    sim = SimLegs([pos(vol=0.05)], 103.0, 103.2)
    rules = [rule("partial_close", "price_reached", 102.0, fraction=0.5)]
    log_ = ActionLog(db)
    # a previous run wrote the pending row + state, sent the close (the venue shows 0.03 left) and crashed
    log_.record("rule", "d", 0, "d", "d:2", "close", {}, "pending", {}, pair="XAUUSD", now_ms=MIN)
    log_.set_rule_state("d", 0, "d:2", "pending", detail={"seq": 0, "op": "close", "pending": {
        "op": "close", "value": None, "volume": 0.02, "leg_volume_before": 0.05}})
    sim.legs["d:2"].volume = 0.03
    PositionManager(settings, ActionLog(db), sim, FixedMarket(), lambda *a: None).manage([decision(rules)], 2 * MIN)
    assert sim.calls == [] and [r["status"] for r in rows(db)] == ["applied"]


def test_one_failing_decision_never_stops_the_others(tmp_path, settings):
    class Two(SimLegs):
        def legs_of(self, decision_id):
            if decision_id == "bad":
                raise RuntimeError("MT5 could not list positions")
            return super().legs_of(decision_id)

    sim = Two([closed_tp(), pos()], 103.0, 103.2, stops=0.5)
    events = []
    pm = PositionManager(settings, ActionLog(tmp_path / "app.db"), sim, FixedMarket(),
                         lambda k, p: events.append((k, p.get("decision"))))
    rules = [rule("move_sl_to_breakeven", "tp_hit", 1)]
    pm.manage([decision(rules, did="bad"), decision(rules)], 10 * MIN)
    assert ("mgmt_error", "bad") in events and ("mgmt_applied", "d") in events and len(sim.calls) == 1


def test_a_missing_market_fact_never_blocks_protective_rules(tmp_path, settings):
    class Broken(FixedMarket):
        def atr(self, pair):
            raise IndexError("no closed bars yet")

        def decision_bar_open_ms(self, pair):
            raise RuntimeError("frame not ready")

    sim = SimLegs([closed_tp(), pos()], 103.0, 103.2, stops=0.5)
    events = []
    pm = PositionManager(settings, ActionLog(tmp_path / "app.db"), sim, Broken(), lambda k, p: events.append(k))
    d = decision([rule("trail_atr", "tp_hit", 1, atr_mult=1.0), rule("move_sl_to_breakeven", "tp_hit", 1)])
    pm.manage([d], 10 * MIN)
    assert sim.calls == [("set_sl", "d:2", 100.7)] and "mgmt_error" not in events


def test_manager_disabled_still_reports_fills_and_closes(tmp_path, settings):
    order = Leg("d:1", "d", "XAUUSD", XAU, "BUY", "order", 0.01, order_price=99.0, sl=97.0, opened_ms=0)
    sim = SimLegs([order], 103.0, 103.2)
    events = []
    pm = PositionManager(with_mgmt(settings, enabled=False), ActionLog(tmp_path / "app.db"), sim, FixedMarket(),
                         lambda k, p: events.append((k, p["text"])))
    d = decision([rule("close_all", "price_reached", 102.0)])
    pm.manage([d], MIN)
    sim.legs["d:1"].kind, sim.legs["d:1"].fill = "position", 99.0
    pm.manage([d], 2 * MIN)
    assert events == [("mgmt_filled", "XAUUSD filled 0.01 @ 99.0")] and sim.calls == []


def test_trailing_rows_get_one_seq_per_step(tmp_path, settings):
    db = tmp_path / "app.db"
    sim = SimLegs([closed_tp(), pos(sl=98.0)], 104.0, 104.2)
    mk = FixedMarket(bar=5 * MIN, atr=2.0)
    pm = PositionManager(settings, ActionLog(db), sim, mk, lambda *a: None)
    d = decision([rule("trail_atr", "tp_hit", 1, atr_mult=1.5)])
    pm.manage([d], 10 * MIN)                                  # 104 − 3 = 101.0
    pm.manage([d], 10 * MIN + 1000)                           # same bar: nothing
    sim.bid, sim.ask, mk.bar = 105.0, 105.2, 6 * MIN
    pm.manage([d], 11 * MIN)                                  # 102.0
    sim.bid, sim.ask, mk.bar = 104.5, 104.7, 7 * MIN
    pm.manage([d], 12 * MIN)                                  # 101.5 would loosen: nothing
    assert sim.calls == [("set_sl", "d:2", 101.0), ("set_sl", "d:2", 102.0)]
    assert [(r["seq"], r["status"]) for r in rows(db)] == [(0, "applied"), (STEP, "applied")]


# --------------------------------------------------------------------------- ActionLog
def test_action_log_idempotency_and_counters(tmp_path):
    lg = ActionLog(tmp_path / "app.db")
    day = 20_000 * 86_400_000
    assert lg.record("model", "m1", 0, "d1", "7", "set_sl", {"value": 1}, "pending", {}, pair="BTCUSDT", now_ms=day + 5)
    assert lg.record("model", "m1", 0, "d1", "7", "set_sl", {"value": 1}, "applied", {}, pair="BTCUSDT", now_ms=day + 6)
    assert not lg.record("model", "m1", 0, "d1", "7", "set_sl", {"value": 1}, "pending", {}, pair="BTCUSDT",
                         now_ms=day + 7)                      # final: never again
    assert lg.record("rule", "d1", 1, "d1", "7", "close", {}, "deferred", {}, pair="BTCUSDT", now_ms=day - 10)
    assert lg.record("rule", "d1", 1, "d1", "7", "close", {}, "applied", {}, pair="BTCUSDT", now_ms=day + 9)
    assert lg.applied_today("BTCUSDT", now_ms=day + 100) == 2
    assert lg.applied_today("BTCUSDT", "model", now_ms=day + 100) == 1
    assert lg.applied_today("BTCUSDT", now_ms=day + 86_400_000) == 0
    assert lg.last_sl_change_ms("7") == day + 6 and lg.last_sl_change_ms("8") is None
    assert lg.closed_by("7") == "rule"
    lg.set_rule_state("d1", 2, "7", "armed", last_bar_ms=5, detail={"steps": 1})
    lg.close()
    again = ActionLog(tmp_path / "app.db")                   # reopened: same state
    assert again.rule_state("d1", 2, "7") == {"rule_idx": 2, "leg": "7", "status": "armed", "last_bar_ms": 5,
                                              "applied_ms": None, "detail": {"steps": 1}}
    assert again.rule_state("d1", 3, "7") is None


def test_action_log_pair_defaults_to_the_decision(tmp_path):
    con = sqlite3.connect(tmp_path / "app.db")
    con.execute("CREATE TABLE ai_decisions (id TEXT PRIMARY KEY, pair TEXT)")
    con.execute("INSERT INTO ai_decisions VALUES ('d1', 'XAUUSD')")
    con.commit()
    con.close()
    lg = ActionLog(tmp_path / "app.db")
    lg.record("model", "m1", 0, "d1", "7", "close", {}, "applied", {}, now_ms=1)
    assert rows(tmp_path / "app.db")[0]["pair"] == "XAUUSD"


def test_leg_transitions_emit_fill_and_close_once_across_restarts(tmp_path):
    db = tmp_path / "app.db"
    order = Leg("5", "d", "XAUUSD", "XAUUSD", "BUY", "order", 0.01, order_price=99.0, sl=97.0, tp_index=1)
    assert ActionLog(db).leg_transitions([order]) == []                           # first sight: silent
    filled = Leg("5", "d", "XAUUSD", "XAUUSD", "BUY", "position", 0.01, fill=99.0, sl=97.0, tp_index=1)
    assert [e for e, _ in ActionLog(db).leg_transitions([filled])] == ["filled"]
    assert ActionLog(db).leg_transitions([filled]) == []
    moved = Leg(**{**vars(filled), "sl": 98.0})
    assert ActionLog(db).leg_transitions([moved]) == []                           # a stop move is not an event
    gone = Leg(**{**vars(filled), "kind": "closed", "closed_reason": "tp"})
    assert [e for e, _ in ActionLog(db).leg_transitions([gone])] == ["closed"]
    assert ActionLog(db).leg_transitions([gone]) == []
    exp = Leg("6", "d", "XAUUSD", "XAUUSD", "BUY", "order", 0.01, order_price=99.0, sl=97.0)
    log_ = ActionLog(db)
    log_.leg_transitions([exp])
    assert log_.leg_transitions([Leg(**{**vars(exp), "kind": "closed", "closed_reason": "expired"})]) == []
    quick = Leg("7", "d", "XAUUSD", "XAUUSD", "BUY", "order", 0.01, order_price=99.0, sl=97.0)
    log_.leg_transitions([quick])                             # filled and stopped out between two loops
    assert [e for e, _ in log_.leg_transitions([Leg(**{**vars(quick), "kind": "closed", "closed_reason": "sl"})])] \
        == ["filled", "closed"]


# --------------------------------------------------------------------------- paper, end to end on real XAU ticks
@pytest.fixture
def xticks(real_xau_ticks):
    t = real_xau_ticks
    return [Tick(int(ms), float(b), float(a), int(k)) for k, ms, b, a in
            zip(t["key"].to_pylist(), t["time_msc"].to_pylist(), t["bid"].to_pylist(), t["ask"].to_pylist())]


class TickMarket:
    """Decision-TF facts from the real ticks seen so far: 1-minute bid bars (the test's decision timeframe), ATR14 and
    the last confirmed swing (``analysis.structure.pivots``) — the same indicator code the snapshot uses."""

    def __init__(self):
        self.bars: dict[int, list[float]] = {}
        self.now = 0

    def feed(self, ticks):
        for t in ticks:
            b = self.bars.setdefault(t.time_msc // MIN * MIN, [t.bid, t.bid, t.bid, t.bid])
            b[1], b[2], b[3] = max(b[1], t.bid), min(b[2], t.bid), t.bid
            self.now = t.time_msc

    def _closed(self):
        return [k for k in sorted(self.bars) if k + MIN <= self.now]

    def decision_bar_open_ms(self, pair):
        ks = self._closed()
        return ks[-1] if ks else None

    def atr(self, pair):
        ks = self._closed()
        if not ks:
            return float("nan")
        a = ind.atr(*(np.array([self.bars[k][i] for k in ks]) for i in (1, 2, 3)))
        return float(a[-1]) if len(a) and not np.isnan(a[-1]) else float("nan")

    def swing(self, pair, side):
        ks = self._closed()
        pv = [p for p in pivots(np.array([self.bars[k][1] for k in ks]), np.array([self.bars[k][2] for k in ks]),
                                2, 2) if p.kind == side]
        return pv[-1].price if pv else None


def run_paper(tmp_path, settings, xticks, rules, *, lots=0.02, sl_off=3.72, tp1=4346.5, tp2=4330.0, every_ms=2_000):
    """Place a 2-leg SELL at the first tick and replay the fixture: ticks → paper fills/exits → manager, as the
    executor loop does. Returns (paper, rows, events, fill)."""
    pb = PaperBackend(tmp_path / "app.db", 10_000)
    t0 = xticks[0]
    sl = round(t0.bid + sl_off, 2)
    rec = {"decision": "SELL", "order_type": "MARKET", "stop_loss": sl, "valid_until": iso(t0.time_msc + 3_600_000),
           "take_profits": [{"price": tp1, "close_fraction": 0.5}, {"price": tp2, "close_fraction": 0.5}],
           "management": rules, "entry": {"price": None}}
    assert pb.place(decision_id="d1", pair="XAUUSD", instrument=XAU, rec=rec, lots=lots, entry=t0.bid,
                    contract_size=100, volume_step=0.01, volume_min=0.01, quote=t0)["ok"]
    last = {"q": t0}
    legs = PaperLegs(pb, lambda k: last["q"], lambda k: {"stops_level": 0.25, "tick_size": 0.01, "digits": 2,
                                                         "volume_min": 0.01, "volume_step": 0.01},
                     now=lambda: last["q"].time_msc)
    market, events = TickMarket(), []
    market.feed([t0])
    pm = PositionManager(settings, ActionLog(tmp_path / "app.db"), legs, market, lambda k, p: events.append((k, p)))
    d = {"id": "d1", "pair": "XAUUSD", "recommendation": rec,
         "execution_detail": {"executed_levels": {"stop_loss": sl}, "executed_management": rules}}
    batch, start = [], t0.time_msc
    for t in xticks[1:]:
        batch.append(t)
        if t.time_msc - start >= every_ms:
            pb.process(XAU, batch)
            market.feed(batch)
            last["q"], start, batch = t, t.time_msc, []
            pm.manage([d], t.time_msc)
    return pb, rows(tmp_path / "app.db"), events, t0.bid


def test_paper_two_leg_sell_breakeven_trail_and_time_stop_on_real_ticks(tmp_path, settings, xticks):
    rules = [rule("move_sl_to_breakeven", "tp_hit", 1), rule("trail_atr", "tp_hit", 1, atr_mult=2.0),
             rule("close_all", "minutes_elapsed", 22)]
    pb, got, events, fill = run_paper(tmp_path, settings, xticks, rules)
    legs = {lg["id"]: lg for lg in pb.decision_legs("d1")}
    assert legs["d1:1"]["close_reason"] == "tp"
    by = {(r["seq"], r["status"]) for r in got}
    be = next(r for r in got if r["seq"] == 0)
    assert be["status"] == "applied" and be["leg"] == "d1:2" and be["action"] == "set_sl"
    q = max((t for t in xticks if t.time_msc <= be["ts"]), key=lambda t: t.time_msc)
    sp = q.ask - q.bid
    assert json.loads(be["requested"])["value"] == to_tick(fill - max(sp, 0.25 + sp), 0.01, "down", 2)
    trails = [r for r in got if r["seq"] % STEP == 1 and r["status"] == "applied"]
    assert trails, got
    values = [json.loads(be["requested"])["value"]] + [json.loads(r["requested"])["value"] for r in trails]
    assert all(b < a for a, b in zip(values, values[1:]))    # SELL: every applied stop strictly lower
    bars = [r["ts"] // MIN for r in trails]
    assert len(bars) == len(set(bars))                        # at most one trail step per decision-TF bar
    stop = next(r for r in got if r["seq"] == 2)
    assert (stop["status"], stop["action"], stop["leg"]) == ("applied", "close", "d1:2")
    assert stop["ts"] - xticks[0].time_msc >= 22 * MIN
    assert legs["d1:2"]["status"] == "closed" and legs["d1:2"]["close_reason"] == "rule"
    assert legs["d1:2"]["sl"] == values[-1]                   # the paper leg carried the last trailed stop
    assert all(s in ("applied", "skipped") for _, s in by)   # nothing left pending / deferred
    kinds = [k for k, _ in events]
    assert "mgmt_position_closed" in kinds and kinds.count("mgmt_applied") == len(trails) + 2
    tp_event = next(p for k, p in events if k == "mgmt_position_closed" and p["leg"] == "d1:1")
    assert tp_event["reason"] == "tp" and tp_event["text"] == "XAUUSD TP1 hit"
    closed2 = next(p for k, p in events if k == "mgmt_position_closed" and p["leg"] == "d1:2")
    assert closed2["reason"] == "rule"
    acct = pb.account()
    assert acct["balance"] == pytest.approx(10_000 + sum(lg["pnl_usd"] for lg in legs.values()), abs=1e-6)


def test_paper_partial_close_books_the_closed_part(tmp_path, settings, xticks):
    rules = [rule("partial_close", "price_reached", 4347.0, fraction=0.5)]
    pb, got, _, fill = run_paper(tmp_path, settings, xticks, rules, lots=0.06)
    [r1, r2] = [r for r in got if r["status"] == "applied"]
    assert {r1["leg"], r2["leg"]} == {"d1:1", "d1:2"} and json.loads(r1["requested"])["volume"] == 0.01
    legs = {lg["id"]: lg for lg in pb.decision_legs("d1")}
    part = legs["d1:2:p1"]
    assert (part["status"], part["close_reason"], part["volume"]) == ("closed", "rule", 0.01)
    assert part["pnl_usd"] == pytest.approx((fill - part["close_price"]) * 0.01 * 100, abs=1e-6)
    assert legs["d1:2"]["volume"] == pytest.approx(0.02)


def test_paper_modify_leg_refuses_widening_and_wrong_side(tmp_path, xticks):
    pb = PaperBackend(tmp_path / "app.db", 10_000)
    t0 = xticks[0]
    rec = {"decision": "BUY", "order_type": "MARKET", "stop_loss": round(t0.bid - 3, 2),
           "valid_until": iso(t0.time_msc + 3_600_000), "take_profits": [{"price": round(t0.ask + 3, 2),
                                                                          "close_fraction": 1.0}]}
    pb.place(decision_id="b1", pair="XAUUSD", instrument=XAU, rec=rec, lots=0.01, entry=t0.ask, contract_size=100,
             volume_step=0.01, volume_min=0.01, quote=t0)
    assert pb.modify_leg("b1:1", sl=rec["stop_loss"] - 1)["status"] == "rejected"
    assert pb.modify_leg("b1:1", sl=0)["status"] == "rejected"
    assert pb.modify_leg("b1:1", sl=t0.bid + 0.1, quote=t0)["status"] == "deferred"
    assert pb.modify_leg("b1:1", tp=t0.bid - 0.1, quote=t0)["status"] == "rejected"
    assert pb.modify_leg("b1:1", sl=t0.bid - 1, tp=t0.ask + 5, quote=t0)["ok"]
    [lg] = pb.decision_legs("b1")
    assert (lg["sl"], lg["tp"]) == (t0.bid - 1, t0.ask + 5)
    assert PaperLegs(pb, lambda k: t0).cancel(PaperLegs(pb, lambda k: t0).legs_of("b1")[0]).status == "failed"


# --------------------------------------------------------------------------- MT5 on the call contract
BUY, SELL = 0, 1


class FakeMT5:
    """The MetaTrader5 calls the management code makes; ``sends`` answers order_send in order."""
    TRADE_ACTION_SLTP, TRADE_ACTION_DEAL, TRADE_ACTION_REMOVE = 6, 1, 8
    POSITION_TYPE_BUY, POSITION_TYPE_SELL = 0, 1
    ORDER_TYPE_BUY, ORDER_TYPE_SELL, ORDER_TYPE_BUY_LIMIT, ORDER_TYPE_SELL_LIMIT = 0, 1, 2, 3
    ORDER_TYPE_BUY_STOP, ORDER_TYPE_SELL_STOP = 4, 5
    ORDER_FILLING_FOK, ORDER_FILLING_IOC, ORDER_FILLING_RETURN = 0, 1, 2
    DEAL_ENTRY_IN, DEAL_ENTRY_OUT, DEAL_ENTRY_OUT_BY = 0, 1, 3
    DEAL_TYPE_BUY, DEAL_TYPE_SELL = 0, 1
    DEAL_REASON_SL, DEAL_REASON_TP, DEAL_REASON_EXPERT = 4, 5, 3
    ORDER_STATE_CANCELED, ORDER_STATE_EXPIRED = 2, 6

    def __init__(self, positions=(), orders=(), hist=(), deals=None, bid=4348.2, ask=4348.5, stops=25, freeze=0,
                 sends=()):
        self.positions, self.orders, self.hist, self.deals = list(positions), list(orders), list(hist), deals or {}
        self.tick = NS(bid=bid, ask=ask, time_msc=1_790_000_000_000)
        self.info = NS(digits=2, point=0.01, trade_tick_size=0.01, trade_stops_level=stops, trade_freeze_level=freeze,
                       volume_min=0.01, volume_step=0.01, volume_max=100.0, trade_contract_size=100.0, filling_mode=1)
        self.sends = list(sends)
        self.sent: list[dict] = []

    def positions_get(self, ticket=None):
        return tuple(p for p in self.positions if ticket is None or p.ticket == ticket)

    def orders_get(self, ticket=None):
        return tuple(o for o in self.orders if ticket is None or o.ticket == ticket)

    def history_orders_get(self, a, b):
        return tuple(self.hist)

    def history_deals_get(self, position=None):
        return tuple(self.deals.get(position, ()))

    def symbol_info(self, s):
        return self.info

    def symbol_info_tick(self, s):
        return self.tick

    def last_error(self):
        return (1, "Success")

    def order_send(self, req):
        self.sent.append(dict(req))
        r = self.sends.pop(0) if self.sends else NS(retcode=10009, price=req.get("price"), order=1, deal=1, volume=1)
        return r(req) if callable(r) else r


def mt5(fake, magic=7):
    b = object.__new__(MT5Backend)
    b.mt5, b.magic, b.adopt_magic, b.own_pairs, b.family = fake, magic, None, {"XAUUSD"}, {magic}
    b.pair_by_symbol, b._warned, b.model = {"XAUUSD": "XAUUSD"}, set(), ServerTimeModel()
    b.t = NS(healthy=lambda: True)
    return b


TAG = "ts:" + "d" * 20 + ":"


def position(ticket=11, typ=BUY, sl=4340.0, tp=4360.0, vol=0.05, k=2, magic=7):
    return NS(ticket=ticket, identifier=ticket, magic=magic, symbol="XAUUSD", comment=f"{TAG}{k}", type=typ,
              volume=vol, price_open=4345.0, sl=sl, tp=tp, time_msc=1_790_000_000_000, profit=0.0, swap=0.0)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(mt5mod, "RETRY_SLEEP_S", 0.0)


def test_mt5_modify_sl_refuses_widening_and_removal_without_sending():
    f = FakeMT5(positions=[position()])
    r = mt5(f).modify_sl(11, "XAUUSD", 4339.0)
    assert (r["ok"], r["status"]) == (False, "rejected") and "widen" in r["meaning"] and f.sent == []
    s = FakeMT5(positions=[position(typ=SELL, sl=4355.0, tp=4330.0)])
    assert mt5(s).modify_sl(11, "XAUUSD", 4356.0)["status"] == "rejected" and s.sent == []
    assert mt5(f).modify_sl(11, "XAUUSD", 0.0)["status"] == "rejected"


def test_mt5_modify_sl_defers_inside_stops_level_or_freeze_level():
    f = FakeMT5(positions=[position()], bid=4348.2, ask=4348.5, stops=25)     # need 0.25 + 0.30 = 0.55
    r = mt5(f).modify_sl(11, "XAUUSD", 4347.7)                                # 0.50 from the bid
    assert r["status"] == "deferred" and "stops level" in r["meaning"] and f.sent == []
    assert mt5(f).modify_sl(11, "XAUUSD", 4347.65)["status"] == "applied"     # 0.55: fits
    frz = FakeMT5(positions=[position(sl=4340.0, tp=4348.4)], stops=0, freeze=50)   # freeze 0.5
    r = mt5(frz).modify_sl(11, "XAUUSD", 4345.0)             # fits, but the current TP is 0.2 from the bid: frozen
    assert r["status"] == "deferred" and "freeze" in r["meaning"] and frz.sent == []
    frz.positions = [position(sl=4340.0, tp=4360.0)]
    assert mt5(frz).modify_sl(11, "XAUUSD", 4347.8)["status"] == "deferred"      # 0.4 ≤ freeze 0.5
    assert mt5(frz).modify_sl(11, "XAUUSD", 4345.0)["status"] == "applied"


def test_mt5_modify_sl_sends_rounded_keeps_tp_and_counts_no_changes_as_ok():
    f = FakeMT5(positions=[position()], sends=[NS(retcode=10025)])
    r = mt5(f).modify_sl(11, "XAUUSD", 4345.004)
    assert r["ok"] and r["status"] == "applied" and r["retcode"] == 10025
    [req] = f.sent
    assert req == {"action": 6, "position": 11, "symbol": "XAUUSD", "sl": 4345.0, "tp": 4360.0, "magic": 7}
    same = FakeMT5(positions=[position(sl=4345.0)])
    assert mt5(same).modify_sl(11, "XAUUSD", 4345.0)["retcode"] == 10025 and same.sent == []


def test_mt5_modify_sl_retries_retryable_codes_only():
    f = FakeMT5(positions=[position()], sends=[NS(retcode=10004), NS(retcode=10031), NS(retcode=10009)])
    r = mt5(f).modify_sl(11, "XAUUSD", 4345.0)
    assert r["ok"] and [a["retcode"] for a in r["attempts"]] == [10004, 10031, 10009]
    assert r["attempts"][0]["meaning"].startswith("10004 REQUOTE")
    g = FakeMT5(positions=[position()], sends=[NS(retcode=10016)])
    r = mt5(g).modify_sl(11, "XAUUSD", 4345.0)
    assert (r["ok"], r["status"], len(g.sent)) == (False, "failed", 1) and "INVALID_STOPS" in r["meaning"]
    h = FakeMT5(positions=[position()], sends=[NS(retcode=10004)] * 5)
    assert len(mt5(h).modify_sl(11, "XAUUSD", 4345.0)["attempts"]) == 3             # 1 + 2 retries


def test_mt5_modify_tp_stays_on_the_profit_side():
    f = FakeMT5(positions=[position()], bid=4348.2, ask=4348.5, stops=25)
    assert mt5(f).modify_tp(11, "XAUUSD", 4348.3)["status"] == "rejected"          # 0.1 above the bid < 0.25
    assert mt5(f).modify_tp(11, "XAUUSD", 4350.0)["ok"]
    assert f.sent[-1]["sl"] == 4340.0 and f.sent[-1]["tp"] == 4350.0


def test_mt5_close_position_partial_builds_the_right_request():
    p = position(vol=0.05)
    f = FakeMT5(positions=[p])
    r = mt5(f).close_position(11, 0.025)
    assert r["ok"] and r["volume"] == 0.02
    [req] = f.sent
    assert req == {"action": 1, "position": 11, "symbol": "XAUUSD", "volume": 0.02, "type": SELL, "price": 4348.2,
                   "magic": 7, "comment": f"{TAG}2", "type_filling": 0}
    small = FakeMT5(positions=[position(vol=0.15)])
    small.info.volume_min = 0.1
    r = mt5(small).close_position(11, 0.1)                   # would leave 0.05 < the 0.1 minimum
    assert r["status"] == "rejected" and "minimum lot" in r["meaning"] and small.sent == []
    assert mt5(FakeMT5(positions=[position(vol=0.02)])).close_position(11, 0.015)["volume"] == 0.01
    assert mt5(FakeMT5(positions=[position(vol=0.05)])).close_position(11, 0.005)["status"] == "rejected"
    full = FakeMT5(positions=[position(typ=SELL, vol=0.01)])
    assert mt5(full).close_position(11)["ok"] and full.sent[0]["type"] == BUY and full.sent[0]["price"] == 4348.5


def test_mt5_close_position_falls_through_filling_modes_and_verifies_a_timeout():
    f = FakeMT5(positions=[position()], sends=[NS(retcode=10030), NS(retcode=10009, price=4348.2)])
    r = mt5(f).close_position(11)
    assert r["ok"] and [s["type_filling"] for s in f.sent] == [0, 1]

    def timed_out(req):                                      # the close went through, the answer did not
        f2.positions[0] = position(vol=round(0.05 - req["volume"], 8))
        return None
    f2 = FakeMT5(positions=[position()], sends=[timed_out])
    r = mt5(f2).close_position(11, 0.02)
    assert r["ok"] and len(f2.sent) == 1 and "despite" in r["attempts"][0]["note"]


def test_mt5_cancel_order_reports_retcodes():
    o = NS(ticket=21, magic=7, symbol="XAUUSD", comment=f"{TAG}1")
    f = FakeMT5(orders=[o], sends=[NS(retcode=10009)])
    r = mt5(f).cancel_order(21)
    assert r["ok"] and r["retcode"] == 10009 and f.sent == [{"action": 8, "order": 21, "magic": 7}]
    assert mt5(FakeMT5()).cancel_order(21)["meaning"].startswith("order 21 not found")
    g = FakeMT5(orders=[o], sends=[NS(retcode=10013)])
    assert mt5(g).cancel_order(21)["meaning"] == "10013 INVALID: invalid request"


def test_mt5_legs_of_classifies_positions_orders_and_closed_legs():
    live = position(ticket=11, k=3)
    other = position(ticket=12, magic=99)                    # another system's position with the same tag prefix
    order = NS(ticket=13, magic=7, symbol="XAUUSD", comment=f"{TAG}4", type=2, volume_current=0.01,
               price_open=4340.0, sl=4335.0, tp=4350.0, time_setup_msc=1_790_000_000_000)
    h = lambda t, pid, k, **kw: NS(ticket=t, position_id=pid, magic=7, symbol="XAUUSD", comment=f"{TAG}{k}", type=0,
                                   price_open=4345.0, sl=4340.0, tp=4350.0, time_setup_msc=1_790_000_000_000,
                                   volume_initial=0.01, **kw)
    deal = lambda e, reason, vol=0.01, t=0: NS(entry=e, type=0 if e == 0 else 1, price=4345.0 if e == 0 else 4350.0,
                                               volume=vol, reason=reason, time_msc=1_790_000_000_000 + t,
                                               symbol="XAUUSD", ticket=t)
    hist = [h(31, 31, 1), h(32, 32, 2), h(33, 0, 5, state=6), h(34, 34, 1), h(11, 11, 3)]
    deals = {31: [deal(0, 0), deal(1, 5, t=5)],              # closed by its TP
             32: [deal(0, 0), deal(1, 4, t=5)],              # closed by its stop
             34: [deal(0, 0, vol=0.02), deal(1, 3, t=5)]}    # half closed in the history only: still syncing → left out
    got = {lg.key: lg for lg in mt5(FakeMT5(positions=[live, other], orders=[order], hist=hist, deals=deals))
           .legs_of("d" * 32)}
    assert set(got) == {"11", "13", "31", "32", "33"}
    assert (got["11"].kind, got["11"].tp_index, got["11"].side, got["11"].fill, got["11"].sl) == \
        ("position", 3, "BUY", 4345.0, 4340.0)
    assert (got["13"].kind, got["13"].order_price, got["13"].side, got["13"].tp_index) == ("order", 4340.0, "BUY", 4)
    assert (got["31"].kind, got["31"].closed_reason, got["31"].tp_index) == ("closed", "tp", 1)
    assert got["32"].closed_reason == "sl" and got["33"].closed_reason == "expired" and got["33"].fill is None
    assert got["11"].opened_ms is not None and got["11"].pair == "XAUUSD"
    only_live = mt5(FakeMT5(positions=[live], hist=hist, deals=deals)).legs_of("d" * 32, history=False)
    assert [lg.key for lg in only_live] == ["11"]


def test_mt5_legs_reads_the_history_only_when_a_leg_leaves():
    live = position(ticket=11, k=1)
    f = FakeMT5(positions=[live], hist=[], deals={})
    calls = {"hist": 0}
    orig = f.history_orders_get

    def counting(a, b):
        calls["hist"] += 1
        return orig(a, b)
    f.history_orders_get = counting
    clock = {"t": 0.0}
    legs = MT5Legs(mt5(f), clock=lambda: clock["t"])
    for i in range(5):
        clock["t"] += 1.0
        assert [lg.kind for lg in legs.legs_of("d" * 32)] == ["position"]
    assert calls["hist"] == 1                                 # first sight only
    f.positions = []                                          # closed by TP at the broker
    f.hist = [NS(ticket=11, position_id=11, magic=7, symbol="XAUUSD", comment=f"{TAG}1", type=0, price_open=4345.0,
                 sl=4340.0, tp=4350.0, time_setup_msc=0)]
    f.deals = {11: [NS(entry=0, type=0, price=4345.0, volume=0.05, reason=0, time_msc=0, symbol="XAUUSD", ticket=1),
                    NS(entry=1, type=1, price=4350.0, volume=0.05, reason=5, time_msc=5, symbol="XAUUSD", ticket=2)]}
    clock["t"] += 1.0
    [lg] = legs.legs_of("d" * 32)
    assert (lg.kind, lg.closed_reason) == ("closed", "tp") and calls["hist"] == 2
    v = legs.venue(lg)
    assert (v.stops_level, v.spread, v.volume_min, v.digits) == (0.25, pytest.approx(0.3), 0.01, 2)
