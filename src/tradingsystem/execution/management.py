"""Position management (P9.6, D-043 "Claude leads, code protects").

The model declares ``management`` rules with every trade (move the stop to breakeven after TP1, trail, take part of
the profit, a time stop …). This module applies them to the trade's live *legs* (MT5 positions / pending orders
by comment tag, paper legs) every executor loop — the same code for paper and MT5, so the paper account is an honest
rehearsal of the broker account.

* :class:`Leg` / :class:`Venue` / :class:`LegBackend` — the venue-neutral view of one leg and the four things we may
  do to it (tighten its stop, move its take-profit, close some or all of it, cancel it). :class:`MT5Legs` and
  :class:`PaperLegs` adapt the two backends; the model's own ``position_actions`` (``action_gate``) reuse them.
* :func:`evaluate_rules` — pure: rules + legs + quotes → planned actions. **A stop is only ever tightened** (BUY:
  higher, SELL: lower), never loosened or removed; a valid tighter stop that the venue would refuse right now (closer
  than stops level + spread, or inside the freeze level) is *deferred* and retried every loop, never dropped.
* :class:`ActionLog` — the per-instance record (``position_actions``, ``management_state``, ``leg_state``): every
  planned action is written *before* it is sent (``pending``) and finalised after, so a crash between sending and
  recording is reconciled from the broker's state on the next loop instead of being sent twice.
* :class:`PositionManager` — the loop step the executor calls; one decision's failure never stops the others, and it
  keeps running under a kill switch (protective actions reduce risk; only new orders are blocked).
"""
from __future__ import annotations

import copy
import json
import logging
import math
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Literal, Protocol

from ..core.settings import ManagementCfg, Settings
from ..core.timeutil import MS_PER_DAY, MS_PER_MINUTE
from ..core.timeutil import now_ms as _now_ms
from ..storage.sqlite_store import connect
from .backends.paper import PaperBackend, Tick

if TYPE_CHECKING:
    from .backends.mt5_backend import MT5Backend

log = logging.getLogger(__name__)

EPS = 1e-9
ACTIONS = ("move_sl_to_breakeven", "partial_close", "trail_atr", "trail_structure", "close_all")
TRIGGERS = ("tp_hit", "price_reached", "r_multiple", "minutes_elapsed", "candle_close")
TRAILING = ("trail_atr", "trail_structure")
LIVE = ("position", "order")
FINAL = ("applied", "rejected", "skipped")          # a row in one of these states is never applied again
SL_ACTIONS = ("set_sl", "modify_sl")               # position_actions.action values that moved a stop
STEP = 1000                  # trailing rules write one row per step: seq = rule_idx + STEP × step (step 0 = rule_idx)
FAIL_RETRY_MS = 60_000       # a broker refusal is retried after this long …
MAX_FAIL_ATTEMPTS = 3        # … at most this many times per action when the request itself is refused
PENDING_SETTLE_MS = 30_000   # an action sent with an unknown outcome is re-read (never re-sent) for this long
ERROR_REPEAT_MS = 15 * 60_000   # the same error of one decision is logged with a traceback / emitted this often
# refusals that say the venue cannot trade NOW (market closed, trading/algo trading disabled, no connection, no
# prices, busy, frozen) — or no answer at all: the action waits and is retried until it goes through or the leg
# closes, never given up (a protective close or stop must not be lost to a daily break or a terminal outage)
VENUE_WAIT = {10004, 10017, 10018, 10020, 10021, 10024, 10026, 10027, 10028, 10029, 10031}


# --------------------------------------------------------------------------- venue-neutral view of a trade
@dataclass
class Leg:
    """One leg of a decision: an MT5 position / pending order (``key`` = ticket) or a paper leg (``key`` = leg id).
    ``opened_ms`` is the fill time of a position, the placement time of a pending order (UTC ms)."""
    key: str
    decision_id: str
    pair: str
    symbol: str                                      # MT5 symbol or paper instrument key
    side: Literal["BUY", "SELL"]
    kind: Literal["position", "order", "closed"]
    volume: float
    fill: float | None = None
    order_price: float | None = None
    sl: float | None = None
    tp: float | None = None
    tp_index: int = 1                                # 1-based, from the comment tag / leg id
    opened_ms: int | None = None
    closed_reason: str | None = None                 # tp | sl | rule | model | expired | cancelled | other


@dataclass
class Venue:
    """What the venue allows right now for a leg's symbol (prices in execution units)."""
    bid: float
    ask: float
    spread: float
    stops_level: float                               # minimum stop distance, price units
    freeze_level: float                              # no modification within this distance, price units
    digits: int
    point: float
    tick_size: float
    volume_min: float
    volume_step: float
    quote_age_s: float


@dataclass
class ActionResult:
    ok: bool
    status: str                                      # applied | rejected | failed | skipped | deferred
    detail: str
    retcode: int | None = None


class LegBackend(Protocol):
    def legs_of(self, decision_id: str) -> list[Leg]: ...
    def venue(self, leg: Leg) -> Venue: ...
    def set_sl(self, leg: Leg, sl: float) -> ActionResult: ...
    def set_tp(self, leg: Leg, tp: float) -> ActionResult: ...
    def close(self, leg: Leg, volume: float) -> ActionResult: ...
    def cancel(self, leg: Leg) -> ActionResult: ...


class MarketView(Protocol):
    """Decision-timeframe facts of a pair, already in EXECUTION prices (the executor implements it, cached per bar)."""
    def decision_bar_open_ms(self, pair: str) -> int | None: ...        # open time of the latest CLOSED bar
    def atr(self, pair: str) -> float: ...
    def swing(self, pair: str, side: Literal["low", "high"]) -> float | None: ...   # last confirmed swing
    # optional: ``decision_bar_close(pair) -> float | None`` (close of that bar) — needed by candle_close triggers;
    # ``market_open(pair) -> bool`` — while False nothing is sent (the rules wait for the session)


# --------------------------------------------------------------------------- pure rule evaluation
@dataclass
class RuleContext:
    now_ms: int
    decision_bar_open_ms: int | None                 # latest closed decision-TF bar (trailing re-arms per bar)
    atr: float | None                                # decision-TF ATR, execution price units
    swing_low: float | None                          # last confirmed decision-TF swing, execution prices
    swing_high: float | None
    original_sl: float | None                        # the stop placed with the trade (R multiples use it)
    cfg: ManagementCfg
    rule_state: Callable[[str, int, str], dict | None] = lambda decision_id, rule_idx, leg: None
    decision_bar_close: float | None = None          # close of that bar (candle_close triggers)
    decision_tf_ms: int | None = None                # its length (a close counts only after the fill)


@dataclass
class PlannedAction:
    """``status``: apply (send it) · deferred (valid, the venue would refuse it now — retry) · skipped (final, never
    applies: recorded) · noop (nothing now; re-evaluated next loop) · bar_done (trailing: nothing this bar)."""
    rule_idx: int
    leg_key: str
    action: str                                      # set_sl | close | cancel
    value: float | None = None
    volume: float | None = None
    reason: str = ""
    status: str = "apply"
    rule: str = ""                                   # the management rule's action name
    trailing: bool = False
    bar_ms: int | None = None


def _decimals(x: float) -> int:
    s = f"{x:.10f}".rstrip("0")
    return len(s.split(".")[1]) if "." in s else 0


def to_tick(x: float, tick: float, mode: Literal["up", "down", "near"] = "near", digits: int = 8) -> float:
    """``x`` on the price grid; ``up``/``down`` choose the safe direction (a breakeven stop never lands below the
    break-even price, a trailing stop never lands closer to the price than computed)."""
    if not tick or tick <= 0:
        return round(x, digits)
    n = x / tick
    n = math.ceil(n - 1e-7) if mode == "up" else math.floor(n + 1e-7) if mode == "down" else round(n)
    return round(n * tick, max(digits, _decimals(tick)))


def tighter(side: str, new: float, current: float | None) -> bool:
    """True when ``new`` is a strictly tighter stop than ``current`` (no stop at all counts as the loosest)."""
    if current is None:
        return True
    return new > current + EPS if side == "BUY" else new < current - EPS


def exit_price(leg: Leg, v: Venue) -> float:
    """The price a leg would close at, and the one its stop triggers on: bid for a BUY, ask for a SELL."""
    return v.bid if leg.side == "BUY" else v.ask


def sl_room(leg: Leg, sl: float, v: Venue) -> tuple[bool, str]:
    """Whether the venue accepts ``sl`` now: at least stops level + spread from the exit price, outside the freeze
    level, on the protective side."""
    px = exit_price(leg, v)
    dist = (px - sl) if leg.side == "BUY" else (sl - px)
    need = v.stops_level + v.spread
    ok = dist >= need - EPS and dist > v.freeze_level
    where = "bid" if leg.side == "BUY" else "ask"
    return ok, (f"{dist:.5g} from the {where} {px} (needs ≥ stops level + spread {need:.5g}"
                + (f", > freeze level {v.freeze_level:.5g}" if v.freeze_level else "") + ")")


def breakeven_level(leg: Leg, v: Venue, spread_mult: float) -> float:
    """fill ± max(spread × mult, stops level + spread), on the profit side of the fill: closing there at least
    pays the spread back."""
    buf = max(v.spread * spread_mult, v.stops_level + v.spread)
    fill = float(leg.fill)
    if leg.side == "BUY":
        return to_tick(fill + buf, v.tick_size, "up", v.digits)
    return to_tick(fill - buf, v.tick_size, "down", v.digits)


def partial_volume(volume: float, fraction: float, step: float, vmin: float) -> tuple[float | None, str]:
    """Volume to close for ``fraction`` of a leg: rounded DOWN to the step; if the part or the rest would be below the
    minimum lot, a fraction ≥ 0.5 closes the whole leg and a smaller one closes nothing (None)."""
    part = round(math.floor(volume * fraction / step + 1e-7) * step, 8) if step > 0 else volume * fraction
    if part >= vmin - EPS and volume - part >= vmin - EPS:
        return part, f"close {part:g} of {volume:g} lots ({fraction:.0%})"
    if fraction >= 0.5:
        return volume, (f"{fraction:.0%} of {volume:g} lots cannot be split at the minimum lot {vmin:g} — "
                        "closing the whole leg")
    return None, f"cannot split the minimum lot: {fraction:.0%} of {volume:g} lots (min {vmin:g}, step {step:g})"


def partial_allocation(legs: list[Leg], fraction: float, vol_left: dict[str, float],
                       v: Venue) -> dict[str, tuple[float | None, str]]:
    """``partial_close`` of a trade held in several legs: ``fraction`` of the trade's live volume, taken from whole
    legs nearest target first, then a step-rounded part of the next leg when both parts stay ≥ the minimum lot.
    Per leg: (volume to close or None, note). The same rule the model's own ``close`` uses (action_gate)."""
    legs = sorted(legs, key=lambda lg: lg.tp_index)
    total = round(sum(vol_left[lg.key] for lg in legs), 8)
    step, vmin = v.volume_step, v.volume_min
    want = round(math.floor(total * fraction / step + 1e-7) * step, 8) if step > 0 else total * fraction
    out: dict[str, tuple[float | None, str]] = {}
    left = want
    for lg in legs:
        vol = vol_left[lg.key]
        head = f"{fraction:.0%} of the trade's {total:g} lots = {want:g}"
        if left <= EPS:
            out[lg.key] = (None, f"{head}: nothing to close on leg {lg.tp_index}")
        elif left >= vol - EPS:
            out[lg.key] = (vol, f"{head}: close the whole leg {lg.tp_index} ({vol:g} lots)")
            left = round(left - vol, 8)
        else:
            part = round(math.floor(left / step + 1e-7) * step, 8) if step > 0 else left
            if part >= vmin - EPS and vol - part >= vmin - EPS:
                out[lg.key] = (part, f"{head}: close {part:g} of leg {lg.tp_index} ({vol:g} lots)")
            else:
                out[lg.key] = (None, f"{head}: the rest ({left:g}) cannot be split from leg {lg.tp_index} "
                                     f"({vol:g} lots, min {vmin:g})")
            left = 0.0
    return out


def _trigger(rule: dict, leg: Leg, legs: list[Leg], ven: Callable[[Leg], Venue], ctx: RuleContext) -> str:
    """Why the rule's trigger holds for ``leg`` ("" = not met). Price triggers use the side of the level relative
    to the leg's entry (fill, or the order price of a pending order) to know which way "crossed" is."""
    trig, val = rule.get("trigger"), rule.get("value")
    if val is None:
        return ""
    val = float(val)
    if trig == "tp_hit":
        k = int(val)
        return f"TP{k} hit" if any(x.kind == "closed" and x.closed_reason == "tp" and x.tp_index == k
                                   for x in legs) else ""
    buy = leg.side == "BUY"
    ref = leg.fill if leg.kind == "position" else leg.order_price
    if trig == "minutes_elapsed":
        if leg.kind != "position" or leg.opened_ms is None:
            return ""
        return f"{val:g} min since the fill" if ctx.now_ms - leg.opened_ms >= val * MS_PER_MINUTE else ""
    if trig == "candle_close":
        close, bar = ctx.decision_bar_close, ctx.decision_bar_open_ms
        if close is None or bar is None or ref is None:
            return ""
        if leg.opened_ms is not None and bar + (ctx.decision_tf_ms or 0) <= leg.opened_ms:
            return ""                                   # that candle closed before the trade existed
        up = val >= ref
        return (f"decision-TF close {close} {'above' if up else 'below'} {val:g}"
                if (close > val if up else close < val) else "")
    if trig == "price_reached":
        if ref is None:
            return ""
        px, up = exit_price(leg, ven(leg)), val >= ref
        return f"{'bid' if buy else 'ask'} {px} reached {val:g}" if (px >= val if up else px <= val) else ""
    if trig == "r_multiple":
        if leg.kind != "position" or leg.fill is None or ctx.original_sl is None:
            return ""
        risk = (leg.fill - ctx.original_sl) if buy else (ctx.original_sl - leg.fill)
        if risk <= 0:
            return ""
        px = exit_price(leg, ven(leg))
        r = ((px - leg.fill) if buy else (leg.fill - px)) / risk
        return f"{r:.2f}R ≥ {val:g}R" if r >= val - EPS else ""
    return ""


def _mode(st: dict | None, trailing: bool, ctx: RuleContext) -> str:
    """check (evaluate the trigger) · replan (already triggered: plan again) · skip."""
    if st is None:
        return "check"
    s = st.get("status")
    if s == "dry_run":
        return "skip" if ctx.cfg.dry_run else "check"
    if s == "pending":
        return "replan" if ctx.now_ms - int(st.get("applied_ms") or 0) >= PENDING_SETTLE_MS else "reconcile"
    if s == "deferred":
        return "replan"
    if s == "failed":
        return "replan" if ctx.now_ms - int(st.get("applied_ms") or 0) >= FAIL_RETRY_MS else "skip"
    if s == "armed" and trailing:
        bar = ctx.decision_bar_open_ms
        return "replan" if bar is not None and st.get("last_bar_ms") != bar else "skip"
    return "skip"


def _stop_plan(base: dict, leg: Leg, v: Venue, target: float, label: str, running: dict[str, float | None],
               ctx: RuleContext, why: str) -> PlannedAction:
    trailing = base["trailing"]
    cur = leg.sl
    if not tighter(leg.side, target, cur):
        return PlannedAction(**base, action="set_sl", value=target, status="bar_done" if trailing else "skipped",
                             reason=f"{why} → {label} is not tighter than the current stop {cur}")
    if trailing and cur is not None and abs(target - cur) < ctx.cfg.min_sl_change_ticks * v.tick_size - EPS:
        return PlannedAction(**base, action="set_sl", value=target, status="bar_done",
                             reason=f"{why} → {label}: move {abs(target - cur):.5g} < "
                                    f"{ctx.cfg.min_sl_change_ticks} ticks")
    if not tighter(leg.side, target, running.get(leg.key)):
        return PlannedAction(**base, action="set_sl", value=target, status="noop",
                             reason=f"{why} → {label}: a tighter stop {running.get(leg.key)} is planned this loop")
    ok, room = sl_room(leg, target, v)
    return PlannedAction(**base, action="set_sl", value=target, status="apply" if ok else "deferred",
                         reason=f"{why} → {label}; {room}")


def _plan(idx: int, rule: dict, leg: Leg, ven: Callable[[Leg], Venue], running: dict[str, float | None],
          vol_left: dict[str, float], ctx: RuleContext, why: str,
          alloc: dict[str, tuple[float | None, str]] | None = None) -> PlannedAction:
    act = rule["action"]
    base = {"rule_idx": idx, "leg_key": leg.key, "rule": act, "trailing": act in TRAILING,
            "bar_ms": ctx.decision_bar_open_ms}
    if act == "close_all":
        if leg.kind == "order":
            return PlannedAction(**base, action="cancel", reason=f"{why} → cancel the pending order")
        return PlannedAction(**base, action="close", volume=vol_left[leg.key], reason=f"{why} → close the position")
    v = ven(leg)
    if act == "partial_close":
        if alloc is not None and leg.key in alloc:           # the trade is held in several legs
            vol, note = alloc[leg.key]
        else:
            vol, note = partial_volume(vol_left[leg.key], float((rule.get("params") or {}).get("fraction", 0)),
                                       v.volume_step, v.volume_min)
        if vol is None:
            return PlannedAction(**base, action="close", status="skipped", reason=f"{why} → {note}")
        return PlannedAction(**base, action="close", volume=vol, reason=f"{why} → {note}")
    buy = leg.side == "BUY"
    if act == "move_sl_to_breakeven":
        if leg.fill is None:
            return PlannedAction(**base, action="set_sl", status="noop", reason=f"{why} → no fill price yet")
        target = breakeven_level(leg, v, ctx.cfg.breakeven_buffer_spread_mult)
        return _stop_plan(base, leg, v, target, f"breakeven {target}", running, ctx, why)
    if ctx.decision_bar_open_ms is None:
        return PlannedAction(**base, action="set_sl", status="noop", reason=f"{why} → decision bar unknown")
    if act == "trail_atr":
        atr, k = ctx.atr, float((rule.get("params") or {}).get("atr_mult", 0))
        if atr is None or not math.isfinite(atr) or atr <= 0 or k <= 0:
            return PlannedAction(**base, action="set_sl", status="noop", reason=f"{why} → ATR unknown")
        px = exit_price(leg, v)
        target = to_tick(px - k * atr if buy else px + k * atr, v.tick_size, "down" if buy else "up", v.digits)
        return _stop_plan(base, leg, v, target, f"ATR trail {target} ({k:g}×{atr:.5g})", running, ctx, why)
    swing = ctx.swing_low if buy else ctx.swing_high
    if swing is None:          # not known yet (the bar just closed is not stored, no basis): asked again next loop
        return PlannedAction(**base, action="set_sl", status="noop", reason=f"{why} → no confirmed swing yet")
    pad = v.stops_level + v.spread
    target = to_tick(swing - pad if buy else swing + pad, v.tick_size, "down" if buy else "up", v.digits)
    return _stop_plan(base, leg, v, target, f"structure trail {target} (swing {swing})", running, ctx, why)


def evaluate_rules(rules: list[dict], legs: list[Leg], venue_for: Callable[[Leg], Venue],
                   ctx: RuleContext) -> list[PlannedAction]:
    """Plans for one decision's rules on its legs (all in execution prices), in rule order. Pure: no I/O besides
    ``venue_for`` and ``ctx.rule_state``.

    A rule fires once per (decision, rule, leg); trailing rules, once triggered, re-plan once per new decision-TF bar.
    Stop moves are checked against the leg's current stop AND against stops planned earlier in the same pass, so the
    plans applied in order only ever tighten. Only ``close_all`` touches pending orders (it cancels them)."""
    live = [lg for lg in legs if lg.kind in LIVE]
    if not live:
        return []
    memo: dict[str, Venue] = {}

    def ven(lg: Leg) -> Venue:
        if lg.key not in memo:
            memo[lg.key] = venue_for(lg)
        return memo[lg.key]

    running: dict[str, float | None] = {lg.key: lg.sl for lg in live}
    vol_left = {lg.key: float(lg.volume) for lg in live}
    gone: set[str] = set()
    plans: list[PlannedAction] = []
    for idx, rule in enumerate(rules):
        act = rule.get("action")
        if act not in ACTIONS or rule.get("trigger") not in TRIGGERS:
            log.warning("management rule %d ignored (unknown action/trigger): %s", idx, rule)
            continue
        trailing = act in TRAILING
        targets = [lg for lg in live if lg.key not in gone and (lg.kind == "position" or act == "close_all")]
        fired_any: str | None = None
        alloc: dict[str, tuple[float | None, str]] | None = None
        if act == "partial_close" and len(targets) > 1:
            alloc = partial_allocation(targets, float((rule.get("params") or {}).get("fraction", 0)), vol_left,
                                       ven(targets[0]))
        for lg in targets:
            mode = _mode(ctx.rule_state(lg.decision_id, idx, lg.key), trailing, ctx)
            if mode == "skip":
                continue
            if mode == "reconcile":            # only checked against the leg (PositionManager), never re-sent yet
                plans.append(PlannedAction(rule_idx=idx, leg_key=lg.key, action="reconcile", status="noop",
                                           rule=act, trailing=trailing, bar_ms=ctx.decision_bar_open_ms,
                                           reason="waiting for the outcome of the action sent"))
                continue
            why = "armed" if trailing else "retry"
            if mode == "check":
                if act == "close_all":             # a decision-wide rule: any leg meeting the trigger closes all
                    if fired_any is None:
                        fired_any = next((w for x in targets if (w := _trigger(rule, x, legs, ven, ctx))), "")
                    why = fired_any
                else:
                    why = _trigger(rule, lg, legs, ven, ctx)
                if not why:
                    continue
            p = _plan(idx, rule, lg, ven, running, vol_left, ctx, why, alloc)
            plans.append(p)
            if p.status != "apply":
                continue
            if p.action == "set_sl":
                running[lg.key] = p.value
            else:
                vol_left[lg.key] = round(vol_left[lg.key] - (p.volume if p.volume is not None else vol_left[lg.key]), 8)
                if p.action == "cancel" or vol_left[lg.key] <= EPS:
                    gone.add(lg.key)
    return plans


# --------------------------------------------------------------------------- persistence
_DDL = [
    """CREATE TABLE IF NOT EXISTS position_actions (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL,
        pair TEXT NOT NULL, source TEXT NOT NULL /* model|rule */, source_decision TEXT NOT NULL, seq INTEGER NOT NULL,
        target_decision TEXT NOT NULL, leg TEXT NOT NULL /* MT5 ticket or paper leg id */,
        action TEXT NOT NULL, requested TEXT,
        status TEXT NOT NULL /* pending|applied|rejected|failed|skipped|deferred */, detail TEXT,
        UNIQUE(source, source_decision, seq, target_decision, leg))""",
    """CREATE TABLE IF NOT EXISTS management_state (decision_id TEXT NOT NULL, rule_idx INTEGER NOT NULL,
        leg TEXT NOT NULL, status TEXT NOT NULL, last_bar_ms INTEGER, applied_ms INTEGER, detail TEXT,
        PRIMARY KEY(decision_id, rule_idx, leg))""",
    """CREATE TABLE IF NOT EXISTS leg_state (leg TEXT PRIMARY KEY, decision_id TEXT, kind TEXT, sl REAL,
        updated_ms INTEGER)""",
    "CREATE INDEX IF NOT EXISTS position_actions_pair_ts ON position_actions(pair, ts)",
    "CREATE INDEX IF NOT EXISTS position_actions_leg ON position_actions(leg, action)",
]
# a row is updated only while it is not final: pending/deferred/failed → anything; a dry-run 'skipped' row → a real one
_UPSERT = """INSERT INTO position_actions (ts, pair, source, source_decision, seq, target_decision, leg, action,
    requested, status, detail) VALUES (?,?,?,?,?,?,?,?,?,?,?)
    ON CONFLICT(source, source_decision, seq, target_decision, leg) DO UPDATE SET ts=excluded.ts,
    action=excluded.action, requested=COALESCE(excluded.requested, position_actions.requested),
    status=excluded.status, detail=excluded.detail
    WHERE position_actions.status IN ('pending','deferred','failed')
       OR (position_actions.status = 'skipped' AND json_extract(position_actions.detail, '$.dry_run') = 1
           AND COALESCE(json_extract(excluded.detail, '$.dry_run'), 0) != 1)"""


def _state(row) -> dict:
    return {"rule_idx": row[0], "leg": row[1], "status": row[2], "last_bar_ms": row[3], "applied_ms": row[4],
            "detail": json.loads(row[5]) if row[5] else {}}


class ActionLog:
    """What the system did (or refused, or deferred) on live trades, and the per-rule state that makes it idempotent
    across restarts. Lives in the instance's ``app.db``; the dashboard reads ``position_actions``."""

    def __init__(self, app_db: Path) -> None:
        self._con = connect(Path(app_db), cache_mb=1)
        self._lock = threading.Lock()
        self._pairs: dict[str, str] = {}
        with self._lock:
            for s in _DDL:
                self._con.execute(s)

    def close(self) -> None:
        with self._lock:
            self._con.close()

    def _pair_of(self, decision_id: str) -> str:
        if decision_id not in self._pairs:
            try:
                with self._lock:
                    r = self._con.execute("SELECT pair FROM ai_decisions WHERE id=?", (decision_id,)).fetchone()
            except Exception:  # noqa: BLE001 — no ai_decisions table (tests, fresh instance)
                r = None
            self._pairs[decision_id] = r[0] if r else ""
        return self._pairs[decision_id]

    # ---------------------------------------------------------------- position_actions
    def record(self, source: str, source_decision: str, seq: int, target_decision: str, leg: str, action: str,
               requested: dict | None, status: str, detail: dict, *, pair: str | None = None,
               now_ms: int | None = None) -> bool:
        """Insert or finalise one action row; False when that (source, source_decision, seq, target_decision, leg)
        already holds a final status (applied/rejected/skipped — the action must not be applied again). A pending,
        deferred or failed row is updated; so is a dry-run 'skipped' row by a real (non-dry-run) record
        (``requested`` None keeps the stored request). ``pair`` defaults to the target decision's pair from
        ``ai_decisions``."""
        ts = _now_ms() if now_ms is None else int(now_ms)
        p = pair if pair is not None else self._pair_of(target_decision)
        with self._lock:
            cur = self._con.execute(_UPSERT, (ts, p, source, source_decision, int(seq), target_decision, str(leg),
                                              action, None if requested is None else json.dumps(requested, default=str),
                                              status,
                                              json.dumps(detail, default=str)))
            return cur.rowcount > 0

    def applied_today(self, pair: str, source: str | None = None, *, now_ms: int | None = None) -> int:
        """Actions applied for ``pair`` since 00:00 UTC (optionally of one source: model | rule)."""
        day = ((_now_ms() if now_ms is None else now_ms) // MS_PER_DAY) * MS_PER_DAY
        q = "SELECT count(*) FROM position_actions WHERE pair=? AND status='applied' AND ts>=?"
        args: tuple = (pair, day)
        if source is not None:
            q, args = q + " AND source=?", args + (source,)
        with self._lock:
            return int(self._con.execute(q, args).fetchone()[0])

    def action_rows(self, source: str, source_decision: str, seq: int, target: str) -> dict[str, tuple]:
        """Every row of one action (all its legs): leg → (status, detail, ts)."""
        with self._lock:
            rows = self._con.execute("SELECT leg, status, detail, ts FROM position_actions WHERE source=? AND "
                                     "source_decision=? AND seq=? AND target_decision=?",
                                     (source, source_decision, int(seq), target)).fetchall()
        out = {}
        for leg, st, det, ts in rows:
            try:
                d = json.loads(det) if det else {}
            except ValueError:
                d = {}
            out[leg] = (st, d, int(ts or 0))
        return out

    def supersede_deferred(self, leg_key: str, by_decision: str, decision_ts: int, *, now_ms: int | None = None) -> int:
        """A newer decision moved (or asked to move) this leg's stop: older decisions' deferred stop moves of the
        leg are finished ('skipped') — a stale waiting stop never overrides the model's current plan."""
        ts = _now_ms() if now_ms is None else int(now_ms)
        with self._lock:
            cur = self._con.execute(
                "UPDATE position_actions SET status='skipped', ts=?, detail=json_set(COALESCE(detail,'{}'), "
                "'$.reason', ?) WHERE source='model' AND leg=? AND status='deferred' AND action='modify_sl' AND "
                "source_decision<>? AND source_decision IN (SELECT id FROM ai_decisions WHERE ts<?)",
                (ts, f"superseded by the stop of decision {by_decision[:8]}", str(leg_key), by_decision,
                 int(decision_ts)))
            return cur.rowcount

    def last_sl_change_ms(self, leg_key: str) -> int | None:
        """When a stop of this leg was last moved by us (rule or model)."""
        with self._lock:
            r = self._con.execute(f"SELECT max(ts) FROM position_actions WHERE leg=? AND status='applied' AND action "
                                  f"IN ({','.join('?' * len(SL_ACTIONS))})", (str(leg_key), *SL_ACTIONS)).fetchone()
        return int(r[0]) if r and r[0] is not None else None

    def closed_by(self, leg_key: str) -> str | None:
        """The source (rule | model) of our latest applied close/cancel of this leg, if any."""
        with self._lock:
            r = self._con.execute("SELECT source FROM position_actions WHERE leg=? AND status='applied' AND action IN "
                                  "('close','cancel','cancel_order') ORDER BY ts DESC, id DESC LIMIT 1",
                                  (str(leg_key),)).fetchone()
        return r[0] if r else None

    # ---------------------------------------------------------------- management_state
    def rule_state(self, decision_id: str, rule_idx: int, leg: str) -> dict | None:
        with self._lock:
            r = self._con.execute("SELECT rule_idx, leg, status, last_bar_ms, applied_ms, detail FROM management_state "
                                  "WHERE decision_id=? AND rule_idx=? AND leg=?",
                                  (decision_id, int(rule_idx), str(leg))).fetchone()
        return _state(r) if r else None

    def states_of(self, decision_id: str) -> dict[tuple[int, str], dict]:
        """Every rule state of one decision (one query per loop instead of one per rule and leg)."""
        with self._lock:
            rows = self._con.execute("SELECT rule_idx, leg, status, last_bar_ms, applied_ms, detail FROM "
                                     "management_state WHERE decision_id=?", (decision_id,)).fetchall()
        return {(r[0], r[1]): _state(r) for r in rows}

    def set_rule_state(self, decision_id: str, rule_idx: int, leg: str, status: str, *, last_bar_ms: int | None = None,
                       applied_ms: int | None = None, detail: dict | None = None) -> None:
        with self._lock:
            self._con.execute("INSERT OR REPLACE INTO management_state (decision_id, rule_idx, leg, status, last_bar_ms, "
                              "applied_ms, detail) VALUES (?,?,?,?,?,?,?)",
                              (decision_id, int(rule_idx), str(leg), status, last_bar_ms, applied_ms,
                               json.dumps(detail or {}, default=str)))

    # ---------------------------------------------------------------- leg_state
    def leg_transitions(self, legs: list[Leg], *, now_ms: int | None = None) -> list[tuple[str, Leg]]:
        """Fills and closes since the last call, each reported exactly once (persisted, so a restart neither repeats
        nor loses them): ``filled`` = a pending order became a position; ``closed`` = a position (or an order that
        filled in between) closed. A leg first seen already open or closed is recorded silently; an order that
        expired or was cancelled unfilled is recorded without an event."""
        ts = _now_ms() if now_ms is None else int(now_ms)
        out: list[tuple[str, Leg]] = []
        with self._lock:
            self._con.execute("BEGIN IMMEDIATE")
            try:
                for lg in legs:
                    row = self._con.execute("SELECT kind, sl FROM leg_state WHERE leg=?", (lg.key,)).fetchone()
                    prev, prev_sl = (row[0], row[1]) if row else (None, None)
                    if prev == "closed" or (prev == lg.kind and prev_sl == lg.sl):
                        continue
                    if prev == "order" and lg.kind == "position":
                        out.append(("filled", lg))
                    elif prev in LIVE and lg.kind == "closed" and not (
                            prev == "order" and lg.closed_reason in ("expired", "cancelled")):
                        if prev == "order":
                            out.append(("filled", lg))
                        out.append(("closed", lg))
                    self._con.execute("INSERT OR REPLACE INTO leg_state (leg, decision_id, kind, sl, updated_ms) "
                                      "VALUES (?,?,?,?,?)", (lg.key, lg.decision_id, lg.kind, lg.sl, ts))
                self._con.execute("COMMIT")
            except BaseException:
                self._con.execute("ROLLBACK")
                raise
        return out


# --------------------------------------------------------------------------- backends
def _result(r: dict) -> ActionResult:
    return ActionResult(bool(r.get("ok")), r.get("status") or ("applied" if r.get("ok") else "failed"),
                        str(r.get("meaning") or r.get("reason") or ""), r.get("retcode"))


class PaperLegs:
    """:class:`LegBackend` over :class:`.backends.paper.PaperBackend`: quotes are the real bid/ask of the execution
    instrument (``quotes``: instrument key → latest Tick or None); ``specs``: instrument key → {stops_level,
    freeze_level, tick_size, digits, volume_min, volume_step} (missing keys default to the paper conventions).
    ``reason`` is what a close records (``rule`` for the manager, ``model`` for the model's actions)."""

    def __init__(self, paper: PaperBackend, quotes: Callable[[str], Tick | None],
                 specs: Callable[[str], dict] | None = None, *, now: Callable[[], int] = _now_ms,
                 reason: str = "rule") -> None:
        self.paper, self.quotes, self.specs, self.now, self.reason = paper, quotes, specs or (lambda k: {}), now, reason

    def _quote(self, symbol: str) -> Tick:
        q = self.quotes(symbol)
        if q is None:
            raise RuntimeError(f"no quote for {symbol} — nothing is changed without a price")
        return q

    def legs_of(self, decision_id: str) -> list[Leg]:
        out = []
        for r in self.paper.decision_legs(decision_id):
            st = r["status"]
            kind = "position" if st == "open" else "order" if st == "pending" else "closed"
            reason = None
            if kind == "closed":
                reason = ("cancelled" if st == "cancelled" else "expired" if st == "expired" else
                          r["close_reason"] if r["close_reason"] in ("tp", "sl", "rule", "model") else "other")
            out.append(Leg(key=r["id"], decision_id=r["decision_id"], pair=r["pair"], symbol=r["instrument"],
                           side=r["side"], kind=kind, volume=float(r["volume"]), fill=r["fill_price"],
                           order_price=r["order_price"], sl=r["sl"], tp=r["tp"], tp_index=int(r["tp_index"] or 1),
                           opened_ms=r["created_ms"] if kind == "order" else r["fill_ms"], closed_reason=reason))
        return out

    def venue(self, leg: Leg) -> Venue:
        q, sp = self._quote(leg.symbol), self.specs(leg.symbol) or {}
        tick = float(sp.get("tick_size") or 0.01)
        return Venue(bid=q.bid, ask=q.ask, spread=q.ask - q.bid, stops_level=float(sp.get("stops_level") or 0.0),
                     freeze_level=float(sp.get("freeze_level") or 0.0), digits=int(sp.get("digits", _decimals(tick))),
                     point=float(sp.get("point") or tick), tick_size=tick,
                     volume_min=float(sp.get("volume_min") or 0.01), volume_step=float(sp.get("volume_step") or 0.01),
                     quote_age_s=max(0.0, (self.now() - q.time_msc) / 1000))

    def set_sl(self, leg: Leg, sl: float) -> ActionResult:
        return _result(self.paper.modify_leg(leg.key, sl=sl, quote=self._quote(leg.symbol)))

    def set_tp(self, leg: Leg, tp: float) -> ActionResult:
        return _result(self.paper.modify_leg(leg.key, tp=tp, quote=self._quote(leg.symbol)))

    def close(self, leg: Leg, volume: float, reason: str | None = None) -> ActionResult:
        r = self.paper.close_legs(leg.decision_id, volume, self._quote(leg.symbol), leg_ids=[leg.key],
                                  reason=reason or self.reason)
        return _result(r)

    def cancel(self, leg: Leg, reason: str | None = None) -> ActionResult:
        n = self.paper.cancel_decision(leg.decision_id, reason=f"cancelled ({reason or self.reason})",
                                       leg_ids=[leg.key])
        return ActionResult(n == 1, "applied" if n == 1 else "failed",
                            "cancelled" if n == 1 else "not a pending order any more")


class MT5Legs:
    """:class:`LegBackend` over :class:`.backends.mt5_backend.MT5Backend`. One ``positions_get``/``orders_get`` per
    loop (a short-lived snapshot shared by every decision); the deal history is read only for a decision seen for
    the first time or when one of its known legs left the live lists (a fill, a close) — closed legs never change."""

    LIVE_TTL_S = 0.5
    SPEC_TTL_S = 60.0
    HISTORY_RETRY_S = 2.0

    def __init__(self, backend: "MT5Backend", *, history_days: int = 45,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.b, self.history_days, self.clock = backend, history_days, clock
        self._live: tuple[float, Any, Any] = (-1e18, None, None)
        self._closed: dict[str, list[Leg]] = {}
        self._seen: dict[str, set[str]] = {}
        self._hist_at: dict[str, float] = {}
        self._info: dict[str, tuple[float, Any]] = {}

    def _snapshot(self) -> tuple[Any, Any]:
        t = self.clock()
        if t - self._live[0] > self.LIVE_TTL_S or self._live[1] is None:
            m = self.b.mt5
            self._live = (t, self.b._ask("positions", m.positions_get()), self.b._ask("orders", m.orders_get()))
        return self._live[1], self._live[2]

    def invalidate(self) -> None:
        self._live = (-1e18, None, None)

    def legs_of(self, decision_id: str) -> list[Leg]:
        pos, ords = self._snapshot()
        live = self.b.legs_of(decision_id, history=False, positions=pos, orders=ords)
        keys = {lg.key for lg in live}
        closed = self._closed.get(decision_id)
        seen = self._seen.setdefault(decision_id, set())
        missing = seen - keys - {lg.key for lg in closed or ()}
        if closed is None or (missing and self.clock() - self._hist_at.get(decision_id, -1e18) >= self.HISTORY_RETRY_S):
            since = int(time.time()) - self.history_days * 86_400
            full = self.b.legs_of(decision_id, since_s=since, history=True, positions=pos, orders=ords)
            closed = [lg for lg in full if lg.kind == "closed"]
            self._closed[decision_id], self._hist_at[decision_id] = closed, self.clock()
        seen |= keys | {lg.key for lg in closed}
        return live + closed

    def _symbol(self, symbol: str):
        t, info = self._info.get(symbol, (-1e18, None))
        if info is None or self.clock() - t > self.SPEC_TTL_S:
            info = self.b.mt5.symbol_info(symbol)
            if info is None:
                raise RuntimeError(f"MT5 symbol_info({symbol}) failed ({self.b.mt5.last_error()})")
            self._info[symbol] = (self.clock(), info)
        return info

    def venue(self, leg: Leg) -> Venue:
        info = self._symbol(leg.symbol)
        q = self.b.mt5.symbol_info_tick(leg.symbol)
        if q is None:
            raise RuntimeError(f"MT5 has no quote for {leg.symbol} ({self.b.mt5.last_error()})")
        t = self.b.model.server_to_utc(int(q.time_msc), prefer="earlier")
        return Venue(bid=q.bid, ask=q.ask, spread=q.ask - q.bid, stops_level=info.trade_stops_level * info.point,
                     freeze_level=getattr(info, "trade_freeze_level", 0) * info.point, digits=info.digits,
                     point=info.point, tick_size=getattr(info, "trade_tick_size", 0) or info.point,
                     volume_min=info.volume_min, volume_step=info.volume_step,
                     quote_age_s=max(0.0, (_now_ms() - t) / 1000))

    def set_sl(self, leg: Leg, sl: float) -> ActionResult:
        if leg.kind != "position":
            return ActionResult(False, "rejected", "stop moves apply to open positions")
        self.invalidate()
        return _result(self.b.modify_sl(int(leg.key), leg.symbol, sl))

    def set_tp(self, leg: Leg, tp: float) -> ActionResult:
        if leg.kind != "position":
            return ActionResult(False, "rejected", "take-profit moves apply to open positions")
        self.invalidate()
        return _result(self.b.modify_tp(int(leg.key), leg.symbol, tp))

    def close(self, leg: Leg, volume: float, reason: str | None = None) -> ActionResult:
        self.invalidate()
        return _result(self.b.close_position(int(leg.key), volume))

    def cancel(self, leg: Leg, reason: str | None = None) -> ActionResult:
        self.invalidate()
        return _result(self.b.cancel_order(int(leg.key)))


# --------------------------------------------------------------------------- the loop step
def rules_of(decision: dict) -> list[dict]:
    """The management rules in execution prices: ``execution_detail.executed_management`` (translated at placement);
    for decisions executed before Phase 3, ``recommendation.management`` with its price levels shifted by the basis
    that was applied to the order (``execution_detail.translation.basis``)."""
    det = _as_dict(decision.get("execution_detail"))
    if "executed_management" in det:
        return list(det.get("executed_management") or [])
    rules = copy.deepcopy(_as_dict(decision.get("recommendation")).get("management") or [])
    basis = (det.get("translation") or {}).get("basis")
    if basis:
        for r in rules:
            if r.get("trigger") in ("price_reached", "candle_close") and r.get("value") is not None:
                r["value"] = float(r["value"]) + float(basis)
    return rules


def _as_dict(x) -> dict:
    if isinstance(x, str):
        return json.loads(x) if x else {}
    return x or {}


def _took_effect(pend: dict, leg: Leg) -> bool:
    """An action recorded as pending (sent, outcome unknown after an interruption) that the leg already shows."""
    op = pend.get("op")
    if op == "set_sl":
        v = pend.get("value")
        return v is not None and leg.sl is not None and abs(leg.sl - float(v)) <= 1e-6 * max(1.0, abs(float(v)))
    if op == "close":
        return leg.volume <= float(pend.get("leg_volume_before") or 0) - float(pend.get("volume") or 0) + EPS
    if op == "cancel":
        return leg.kind == "closed"            # a cancelled order; one that filled meanwhile is a position to close
    return False


class PositionManager:
    """Applies each executed decision's ``management`` rules to its live legs (see the module docstring)."""

    def __init__(self, s: Settings, log: ActionLog, backend: LegBackend, market: MarketView,
                 emit: Callable[[str, dict], None]) -> None:
        self.s, self.log, self.backend, self.market, self._emit_fn = s, log, backend, market, emit
        self.cfg: ManagementCfg = s.execution.management
        self._warned: set[str] = set()
        self._errs: dict[str, tuple[str, int]] = {}      # key → (error signature, last time it was reported)

    def _report(self, key: str, exc_or_text, now: int, payload: dict, *, traceback: bool = True) -> None:
        """Log + emit ``mgmt_error`` for ``key`` — once per distinct error, then at most every ERROR_REPEAT_MS (a
        closed market or an MT5 outage must not write one traceback and one event row per loop)."""
        sig = repr(exc_or_text)[:120]
        prev = self._errs.get(key)
        if prev is not None and prev[0] == sig and now - prev[1] < ERROR_REPEAT_MS:
            log.debug("%s: %s (repeated)", key, sig)
            return
        self._errs[key] = (sig, now)
        if traceback and isinstance(exc_or_text, BaseException):
            log.error("%s: %r", key, exc_or_text, exc_info=exc_or_text)
        else:
            log.warning("%s: %s", key, exc_or_text)
        self._emit("mgmt_error", payload)

    def _emit(self, kind: str, payload: dict) -> None:
        try:
            self._emit_fn(kind, payload)
        except Exception:  # noqa: BLE001 — an event is a notification; it never stops management
            log.exception("could not emit %s", kind)

    def manage(self, decisions: list[dict], now_ms: int) -> None:
        """One pass over the executed, unsettled decisions. Each is isolated: an exception is logged and emitted as
        ``mgmt_error`` and the next decision is managed."""
        for d in decisions:
            key = f"position management of {d.get('pair')} {str(d.get('id'))[:8]}"
            try:
                self._one(d, now_ms)
                self._errs.pop(key, None)
            except Exception as exc:  # noqa: BLE001
                self._report(key, exc, now_ms, {"pair": d.get("pair"), "decision": d.get("id"),
                                                "error": repr(exc)[:300],
                                                "text": f"{d.get('pair')} management error: {exc!r}"[:200]})

    # ---------------------------------------------------------------- one decision
    def _one(self, d: dict, now: int) -> None:
        did = d["id"]
        legs = self.backend.legs_of(did)
        for ev, lg in self.log.leg_transitions(legs, now_ms=now):
            self._announce(ev, lg)
        if not self.cfg.enabled:
            return
        live = {lg.key: lg for lg in legs if lg.kind in LIVE}
        self._sweep(d, legs, live, now)
        rules = rules_of(d)
        if not rules or not live:
            return
        if (is_open := getattr(self.market, "market_open", None)) is not None \
                and self._fact("market_open", is_open, d["pair"]) is False:
            return                                   # nothing can be sent while the session is closed: rules wait
        states = self.log.states_of(did)
        ctx = self._context(d, rules, now, lambda _d, i, k: states.get((i, k)))
        venues: dict[str, Venue] = {}

        def venue_for(lg: Leg) -> Venue:
            if lg.symbol not in venues:
                venues[lg.symbol] = self.backend.venue(lg)
            return venues[lg.symbol]

        for p in evaluate_rules(rules, legs, venue_for, ctx):
            lg = live[p.leg_key]
            if self._execute(d, rules[p.rule_idx], p, lg, states.get((p.rule_idx, p.leg_key)), now):
                # later plans of this pass see the leg as it is now (their write-ahead records must describe the
                # leg just before their own send)
                if p.action == "set_sl":
                    lg.sl = p.value
                elif p.action == "close":
                    lg.volume = round(lg.volume - float(p.volume if p.volume is not None else lg.volume), 8)

    def _fact(self, name: str, fn: Callable, *args):
        """One market fact, or None when it cannot be computed now (missing bars, no data yet): only the rules that
        need it wait — breakeven, closes and time stops never depend on it."""
        try:
            return fn(*args)
        except Exception as exc:  # noqa: BLE001
            if name not in self._warned:
                self._warned.add(name)
                log.warning("management: %s(%s) unavailable (%r) — rules needing it wait", name, args[0], exc)
            return None

    def _context(self, d: dict, rules: list[dict], now: int, state: Callable) -> RuleContext:
        pair, m = d["pair"], self.market
        acts, trigs = {r.get("action") for r in rules}, {r.get("trigger") for r in rules}
        side = _as_dict(d.get("recommendation")).get("decision")
        lo = hi = close = atr = None
        if "trail_structure" in acts:
            lo = self._fact("swing", m.swing, pair, "low") if side != "SELL" else None
            hi = self._fact("swing", m.swing, pair, "high") if side != "BUY" else None
        if "trail_atr" in acts:
            atr = self._fact("atr", m.atr, pair)
        if "candle_close" in trigs and (fn := getattr(m, "decision_bar_close", None)) is not None:
            close = self._fact("decision_bar_close", fn, pair)
        pcfg = self.s.pairs.get(pair)
        sl0 = (_as_dict(d.get("execution_detail")).get("executed_levels") or {}).get("stop_loss")
        return RuleContext(now_ms=now, decision_bar_open_ms=self._fact("decision_bar_open_ms", m.decision_bar_open_ms,
                                                                       pair),
                           atr=atr, swing_low=lo, swing_high=hi, original_sl=float(sl0) if sl0 is not None else None,
                           cfg=self.cfg, rule_state=state, decision_bar_close=close,
                           decision_tf_ms=pcfg.decision_timeframe.ms if pcfg else None)

    def _announce(self, ev: str, lg: Leg) -> None:
        base = {"pair": lg.pair, "decision": lg.decision_id, "leg": lg.key, "tp_index": lg.tp_index, "side": lg.side,
                "volume": lg.volume}
        if ev == "filled":
            self._emit("mgmt_filled", {**base, "price": lg.fill, "text": f"{lg.pair} filled {lg.volume:g} @ {lg.fill}"})
            return
        reason = lg.closed_reason
        if reason in (None, "other"):
            reason = self.log.closed_by(lg.key) or reason or "other"
        text = (f"{lg.pair} TP{lg.tp_index} hit" if reason == "tp" else
                f"{lg.pair} stop hit (leg {lg.tp_index})" if reason == "sl" else
                f"{lg.pair} leg {lg.tp_index} closed ({reason})")
        self._emit("mgmt_position_closed", {**base, "reason": reason, "text": text})

    def _sweep(self, d: dict, legs: list[Leg], live: dict[str, Leg], now: int) -> None:
        """Finish the open (pending/deferred/failed) actions of legs that are no longer live."""
        did, by_key = d["id"], {lg.key: lg for lg in legs}
        for (idx, key), st in self.log.states_of(did).items():
            if st["status"] not in ("pending", "deferred", "failed") or key in live:
                continue
            det, lg = st["detail"] or {}, by_key.get(key)
            pend, how = det.get("pending") or {}, (lg.closed_reason if lg else None)
            op = pend.get("op") or det.get("op") or "set_sl"
            if st["status"] == "pending" and op in ("close", "cancel") and how not in ("sl", "tp"):
                status, note = "applied", "the leg is no longer live — the close took effect"
            else:
                status, note = "skipped", f"the leg closed ({how or 'gone'}) before the action completed"
            self.log.record("rule", did, int(det.get("seq", idx)), did, key, op, None, status,
                            {"reason": note}, pair=d["pair"], now_ms=now)
            self.log.set_rule_state(did, idx, key, "done", applied_ms=now, detail={"reason": note})

    def _finish(self, did: str, p: PlannedAction, key: str, det: dict, now: int, wrote: bool, note: str = "") -> None:
        """A rule's current step is over: one-shot rules are done; trailing rules wait for the next bar."""
        if p.trailing:
            steps = int(det.get("steps", 0)) + (1 if wrote else 0)
            self.log.set_rule_state(did, p.rule_idx, key, "armed", last_bar_ms=p.bar_ms, applied_ms=now,
                                    detail={"steps": steps, "reason": note or p.reason})
        else:
            self.log.set_rule_state(did, p.rule_idx, key, "done", applied_ms=now,
                                    detail={"reason": note or p.reason})

    def _execute(self, d: dict, rule: dict, p: PlannedAction, lg: Leg, st: dict | None, now: int) -> bool:
        """Carry out one plan; True when it was applied at the venue now."""
        did, pair, key = lg.decision_id, d["pair"], lg.key
        det = dict((st or {}).get("detail") or {})
        last_bar = (st or {}).get("last_bar_ms")
        pend = det.get("pending")
        if (st or {}).get("status") == "pending" and pend and _took_effect(pend, lg):
            # sent before an interruption (crash / exception) and the venue shows it: done — never sent twice
            self.log.record("rule", did, int(det.get("seq", p.rule_idx)), did, key, pend.get("op") or p.action,
                            {**pend, "rule": p.rule}, "applied",
                            {"reason": p.reason, "reconciled": "found applied at the venue after an interruption"},
                            pair=pair, now_ms=now)
            self._finish(did, p, key, det, now, wrote=True, note="reconciled after an interruption")
            return False
        if p.status == "noop":
            if p.trailing and st is None:            # triggered: from now on it trails without re-checking the trigger
                self.log.set_rule_state(did, p.rule_idx, key, "armed", detail={"steps": 0, "reason": p.reason})
            return False
        seq = p.rule_idx if not p.trailing else int(det.get("seq", p.rule_idx + STEP * int(det.get("steps", 0))))
        req = {"rule": p.rule, "trigger": rule.get("trigger"), "trigger_value": rule.get("value"),
               "params": rule.get("params") or {}, "op": p.action, "value": p.value, "volume": p.volume,
               "leg_volume_before": lg.volume, "sl_before": lg.sl, "tp_index": lg.tp_index}

        def rec(status: str, **extra) -> bool:
            return self.log.record("rule", did, seq, did, key, p.action, req, status, {"reason": p.reason, **extra},
                                   pair=pair, now_ms=now)

        open_row = {"seq": seq, "op": p.action, "steps": int(det.get("steps", 0))}
        if p.status == "bar_done":
            if "seq" in det:                         # this step's deferred row is superseded
                rec("skipped", superseded=True)
            self._finish(did, p, key, det, now, wrote="seq" in det)
            return False
        if p.status == "skipped":
            rec("skipped")
            self._finish(did, p, key, det, now, wrote=True)
            return False
        if p.status == "deferred":
            if (st or {}).get("status") != "deferred":
                rec("deferred")
                self.log.set_rule_state(did, p.rule_idx, key, "deferred", last_bar_ms=last_bar, applied_ms=now,
                                        detail={**open_row, "reason": p.reason})
            return False
        if self.cfg.dry_run:                         # record what would be done, touch nothing
            rec("skipped", dry_run=True)
            if p.trailing:
                self._finish(did, p, key, det, now, wrote=True)
            else:
                self.log.set_rule_state(did, p.rule_idx, key, "dry_run", applied_ms=now, detail={"reason": p.reason})
            return False
        if not rec("pending"):                       # a final row exists: never apply the same action twice
            self._finish(did, p, key, det, now, wrote=p.trailing, note="already recorded")
            return False
        pending = {"op": p.action, "value": p.value, "volume": p.volume, "leg_volume_before": lg.volume}
        counters = {"attempts": int(det.get("attempts", 0)), "waits": int(det.get("waits", 0))}
        self.log.set_rule_state(did, p.rule_idx, key, "pending", last_bar_ms=last_bar, applied_ms=now,
                                detail={**open_row, **counters, "pending": pending})
        ekey = f"{pair} {did[:8]} rule {p.rule_idx} ({p.rule}) on leg {key}"
        try:
            res = self._apply(p, lg)
        except Exception as exc:  # noqa: BLE001 — outcome unknown: stays 'pending', re-read (not re-sent) first
            self._report(ekey, exc, now, {"pair": pair, "decision": did, "leg": key, "rule": p.rule,
                                          "error": repr(exc)[:300], "text": f"{pair} {p.rule} error: {exc!r}"[:200]})
            return False
        extra = {"result": res.detail, "retcode": res.retcode}
        if res.status == "applied":
            self._errs.pop(ekey, None)
            rec("applied", **extra)
            self._finish(did, p, key, det, now, wrote=True)
            self._emit("mgmt_applied", {"pair": pair, "decision": did, "leg": key, "tp_index": lg.tp_index,
                                        "rule": p.rule, "op": p.action, "value": p.value, "volume": p.volume,
                                        "reason": p.reason, "text": _applied_text(pair, p, lg)})
            return True
        if res.status == "unknown":                  # sent, no confirmation: stays 'pending', re-read before a resend
            self._report(ekey, f"outcome unknown: {res.detail}", now,
                         {"pair": pair, "decision": did, "leg": key, "rule": p.rule, "error": res.detail,
                          "text": f"{pair} {p.rule}: outcome unknown ({res.detail})"[:200]}, traceback=False)
            return False
        if res.status == "deferred":
            rec("deferred", **extra)
            self.log.set_rule_state(did, p.rule_idx, key, "deferred", last_bar_ms=last_bar, applied_ms=now,
                                    detail={**open_row, "reason": res.detail})
        elif res.status == "failed" and (res.retcode is None or res.retcode in VENUE_WAIT):
            # the venue cannot trade now (closed, algo trading off, no connection …): wait, never give up
            w = counters["waits"] + 1
            rec("failed", waiting=True, wait=w, **extra)
            self.log.set_rule_state(did, p.rule_idx, key, "failed", last_bar_ms=last_bar, applied_ms=now,
                                    detail={**open_row, **counters, "waits": w, "reason": res.detail,
                                            "pending": pending})
            self._report(ekey, f"waiting for the venue: {res.detail}", now,
                         {"pair": pair, "decision": did, "leg": key, "rule": p.rule, "error": res.detail,
                          "text": f"{pair} {p.rule} waits for the venue: {res.detail}"[:200]}, traceback=False)
        elif res.status == "failed":
            n = counters["attempts"] + 1
            rec("failed", attempt=n, **extra)
            if n >= MAX_FAIL_ATTEMPTS:
                self._finish(did, p, key, det, now, wrote=True, note=f"gave up after {n} attempts: {res.detail}")
                self._emit("mgmt_error", {"pair": pair, "decision": did, "leg": key, "rule": p.rule,
                                          "error": res.detail, "text": f"{pair} {p.rule} failed {n}×: {res.detail}"[:200]})
            else:
                self.log.set_rule_state(did, p.rule_idx, key, "failed", last_bar_ms=last_bar, applied_ms=now,
                                        detail={**open_row, **counters, "attempts": n, "reason": res.detail,
                                                "pending": pending})
        else:                                        # rejected/skipped by the venue check (e.g. the stop moved meanwhile)
            rec(res.status if res.status in FINAL else "rejected", **extra)
            self._finish(did, p, key, det, now, wrote=True, note=res.detail)
        return False

    def _apply(self, p: PlannedAction, lg: Leg) -> ActionResult:
        if p.action == "set_sl":
            return self.backend.set_sl(lg, float(p.value))
        if p.action == "close":
            return self.backend.close(lg, float(p.volume if p.volume is not None else lg.volume))
        if p.action == "cancel":
            return self.backend.cancel(lg)
        return ActionResult(False, "rejected", f"unknown action {p.action}")


def _applied_text(pair: str, p: PlannedAction, lg: Leg) -> str:
    if p.action == "set_sl":
        return f"{pair} leg {lg.tp_index}: stop → {p.value} ({p.rule})"
    if p.action == "close":
        return f"{pair} leg {lg.tp_index}: closed {p.volume:g} lots ({p.rule})"
    return f"{pair} leg {lg.tp_index}: pending order cancelled ({p.rule})"


__all__ = ["ActionLog", "ActionResult", "Leg", "LegBackend", "MarketView", "MT5Legs", "PaperLegs", "PlannedAction",
           "PositionManager", "RuleContext", "VENUE_WAIT", "Venue", "breakeven_level", "evaluate_rules", "exit_price",
           "partial_allocation", "partial_volume", "rules_of", "sl_room", "tighter", "to_tick"]
