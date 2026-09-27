"""Gate for the model's actions on live trades (Phase 3, D-043: "Claude leads, code protects").

A ``position_actions`` entry of a valid decision targets one earlier decision of this pair (its live positions or
pending orders, the *legs*). Every action is checked here, deterministically, before the executor applies it:

* ownership and freshness: the legs belong to this system and pair; the decision is younger than
  ``risk.max_recommendation_age_s``; the quote is fresh and the market open;
* ``modify_sl``: only TIGHTER than the current stop and on the protective side of the price; a tighter stop that is
  still closer to the price than the venue's stops level + spread is *deferred* — kept and retried every loop until
  it fits or the leg closes (also when the price crosses it meanwhile, the market closes or the decision ages) —
  never loosened, never removed;
* ``modify_tp``: on the profit side of the price, at least the stops level away; one leg (``leg``) when several
  are open;
* ``close``: a fraction of the open volume, rounded down to the volume step; at the minimum lot a fraction ≥ 0.5
  closes everything, a smaller one is refused (the lot cannot be split);
* ``cancel_order``: pending orders only;
* rate limits: at most ``max_per_pair_per_day`` applied actions per pair per UTC day and one stop change per
  position every ``min_minutes_between_sl_changes``.

Nothing here talks to a broker: ``evaluate`` returns per-leg plans with the full check list; the executor applies
the ``apply`` plans through the leg backend and records every plan in ``position_actions``.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable

from ..core.settings import PositionActionsCfg
from .management import Leg, Venue

QUOTE_MAX_AGE_S = 30.0


@dataclass
class ActionContext:
    now_ms: int
    pair: str
    rec_ts_ms: int                      # the timestamp of the decision carrying the action
    max_age_s: int                      # risk.max_recommendation_age_s
    market_open: bool
    cfg: PositionActionsCfg
    applied_today: int                  # actions already applied for this pair today
    last_sl_change_ms: Callable[[str], int | None]
    deferred_legs: frozenset[str] = frozenset()   # legs whose stop move of this action is already deferred


@dataclass
class LegPlan:
    leg: Leg | None
    op: str                              # set_sl | set_tp | close | cancel | none
    value: float | None = None
    volume: float | None = None
    status: str = "apply"                # apply | rejected | deferred
    checks: list[tuple[str, bool, str]] = field(default_factory=list)

    @property
    def reason(self) -> str:
        bad = [f"{n}: {d}" for n, ok, d in self.checks if not ok]
        return "; ".join(bad) if bad else "ok"


def _round_down(v: float, step: float) -> float:
    return round(math.floor(v / step + 1e-9) * step, 8)


def evaluate(action: dict, legs: list[Leg], venue_for: Callable[[Leg], Venue], ctx: ActionContext) -> list[LegPlan]:
    """Plans for one action (already translated to execution prices). A failure of an action-wide check rejects every
    leg; per-leg checks decide per leg."""
    kind, op = action["target"]["kind"], action["action"]
    base: list[tuple[str, bool, str]] = []

    def add(name: str, ok: bool, detail: str) -> bool:
        base.append((name, bool(ok), detail))
        return bool(ok)

    age = (ctx.now_ms - ctx.rec_ts_ms) / 1000
    waiting = op == "modify_sl" and bool(ctx.deferred_legs)
    add("not_expired", age <= ctx.max_age_s or waiting,
        f"decision {age:.0f}s old (≤{ctx.max_age_s}s)" + ("; a deferred stop waits until it fits" if waiting else ""))
    add("market_open", ctx.market_open, "open" if ctx.market_open else "execution market closed")
    add("daily_limit", ctx.applied_today < ctx.cfg.max_per_pair_per_day,
        f"{ctx.applied_today} actions applied today (< {ctx.cfg.max_per_pair_per_day})")
    want = "order" if kind == "order" else "position"
    mine = [lg for lg in legs if lg.kind == want and lg.pair == ctx.pair]
    add("own_target", bool(mine), f"{len(mine)} live {want}(s) of {ctx.pair} for this decision"
        if mine else f"no live {want} of {ctx.pair} for this decision")
    if not all(ok for _, ok, _ in base):
        held = [lg for lg in mine if lg.key in ctx.deferred_legs]
        if waiting and held and all(ok or n in ("market_open", "daily_limit") for n, ok, _ in base):
            # a deferred stop keeps waiting through a closed session / a full daily count
            return [LegPlan(lg, "set_sl", value=float(action["value"]), status="deferred", checks=list(base))
                    for lg in held]
        return [LegPlan(None, "none", status="rejected", checks=list(base))]

    if op == "cancel_order":
        return [LegPlan(lg, "cancel", checks=list(base)) for lg in mine]
    if op == "modify_tp":
        if action.get("leg"):
            mine = [lg for lg in mine if lg.tp_index == int(action["leg"])]
            if not mine:
                return [LegPlan(None, "none", status="rejected",
                                checks=base + [("leg", False, f"no open leg at TP{action['leg']}")])]
        elif len(mine) > 1:
            return [LegPlan(None, "none", status="rejected",
                            checks=base + [("leg", False, f"{len(mine)} legs open — say which one (leg)")])]
        return [_plan_tp(lg, float(action["value"]), venue_for(lg), base) for lg in mine]
    if op == "modify_sl":
        return [_plan_sl(lg, float(action["value"]), venue_for(lg), base, ctx) for lg in mine]
    if op == "close":
        return _plan_close(mine, float(action.get("fraction") or 1.0), venue_for, base)
    return [LegPlan(None, "none", status="rejected", checks=base + [("action", False, f"unknown action {op}")])]


def _fresh(v: Venue, checks: list) -> bool:
    ok = v.quote_age_s <= QUOTE_MAX_AGE_S
    checks.append(("quote_fresh", ok, f"quote age {v.quote_age_s:.1f}s (≤{QUOTE_MAX_AGE_S:.0f}s)"))
    return ok


def _plan_sl(lg: Leg, sl: float, v: Venue, base: list, ctx: ActionContext) -> LegPlan:
    checks = list(base)
    p = LegPlan(lg, "set_sl", value=round(sl, v.digits), checks=checks)
    held = lg.key in ctx.deferred_legs                   # already waiting: only a final reason ends it
    if not _fresh(v, checks):
        p.status = "deferred" if held else "rejected"
        return p
    buy = lg.side == "BUY"
    cur = lg.sl
    tighter = cur is None or (sl > cur if buy else sl < cur)
    checks.append(("tighter_only", tighter, f"new stop {sl} vs current {cur} ({'must be higher' if buy else 'must be lower'})"))
    price = v.bid if buy else v.ask                      # a BUY's stop triggers on the bid, a SELL's on the ask
    side_ok = sl < price if buy else sl > price
    checks.append(("protective_side", side_ok, f"stop {sl} {'below' if buy else 'above'} the {'bid' if buy else 'ask'} {price}"))
    last = ctx.last_sl_change_ms(lg.key)
    gap_ok = last is None or ctx.now_ms - last >= ctx.cfg.min_minutes_between_sl_changes * 60_000
    checks.append(("sl_change_spacing", gap_ok, f"last stop change {((ctx.now_ms - last) / 60_000) if last else 0:.0f} min ago "
                                                 f"(≥ {ctx.cfg.min_minutes_between_sl_changes} min)"))
    if not tighter:
        p.status = "rejected"                            # final: the stop is already at least as tight
        return p
    if not (side_ok and gap_ok):
        # a waiting stop the price crossed (or a spacing clash) keeps waiting for the price to come back, as the
        # position manager does; a new request on the wrong side is refused
        p.status = "deferred" if held else "rejected"
        return p
    need = v.stops_level + v.spread
    dist = (price - sl) if buy else (sl - price)
    fits = dist >= need and dist > v.freeze_level
    checks.append(("stops_level", fits, f"{dist:.5g} from the price (≥ stops level + spread {need:.5g})"))
    if not fits:
        p.status = "deferred"                            # a valid tighter stop, just too close for now
    return p


def _plan_tp(lg: Leg, tp: float, v: Venue, base: list) -> LegPlan:
    checks = list(base)
    p = LegPlan(lg, "set_tp", value=round(tp, v.digits), checks=checks)
    if not _fresh(v, checks):
        p.status = "rejected"
        return p
    buy = lg.side == "BUY"
    price = v.ask if buy else v.bid
    dist = (tp - price) if buy else (price - tp)
    ok = dist >= v.stops_level and dist > v.freeze_level
    checks.append(("profit_side", ok, f"take-profit {tp} is {dist:.5g} beyond the {'ask' if buy else 'bid'} {price} "
                                      f"(≥ stops level {v.stops_level:.5g})"))
    if not ok:
        p.status = "rejected"
    return p


def _plan_close(legs: list[Leg], fraction: float, venue_for: Callable[[Leg], Venue], base: list) -> list[LegPlan]:
    """Close ``fraction`` of the open volume: whole legs nearest-target first, then part of the next one when the
    volume step and the minimum lot allow it (both the closed part and the rest must be ≥ the minimum lot)."""
    v0 = venue_for(legs[0])
    checks = list(base)
    if not _fresh(v0, checks):
        return [LegPlan(lg, "close", status="rejected", checks=list(checks)) for lg in legs]
    total = sum(lg.volume for lg in legs)
    want = _round_down(total * fraction, v0.volume_step) if fraction < 1 else total
    if fraction < 1 and want < v0.volume_min - 1e-12:
        if fraction >= 0.5:
            want = total
            checks.append(("volume", True, f"{fraction:.0%} of {total} lots is below the minimum lot — closing all "
                                           "(a fraction ≥ 50 % is honoured as a full close)"))
        else:
            return [LegPlan(None, "none", status="rejected",
                            checks=checks + [("volume", False, f"{fraction:.0%} of {total} lots is below the minimum "
                                                               f"lot {v0.volume_min} — cannot split")])]
    plans: list[LegPlan] = []
    left = want
    for lg in sorted(legs, key=lambda x: (x.tp_index, x.key)):
        if left <= 1e-12:
            break
        v = venue_for(lg)
        if left >= lg.volume - 1e-12:
            plans.append(LegPlan(lg, "close", volume=lg.volume, checks=list(checks)))
            left = round(left - lg.volume, 8)
            continue
        part = _round_down(left, v.volume_step)
        if part >= v.volume_min - 1e-12 and lg.volume - part >= v.volume_min - 1e-12:
            plans.append(LegPlan(lg, "close", volume=part, checks=list(checks)))
            left = round(left - part, 8)
        break
    if not plans:
        return [LegPlan(None, "none", status="rejected",
                        checks=checks + [("volume", False, f"no leg can be closed for {fraction:.0%} of {total} lots")])]
    return plans
