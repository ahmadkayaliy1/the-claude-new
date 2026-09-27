"""Decision metrics (Phase 4, spec §3.8 component 1): what happened after each decision, measured on real bars.

The executor's housekeeping (every 60 s) runs :class:`MetricsJob`, which writes one ``decision_metrics`` row per valid
decision of this system's pairs:

* **executed trade** — once the venue settled it (``outcome``) and the 1m bar the last leg closed in has closed and is
  stored (the exit usually happens at that bar's extreme: without it a TP reached there reads as missed); again when a
  later settlement is newer than the row (an idea first scored virtually, executed afterwards from the manual queue).
  The window runs from the real fill (``outcome_detail.open_ms``; else the virtual fill) to the last leg's real close
  (``outcome_detail.close_ms``; else the settlement time, flagged in ``detail``). A close bar still missing
  ``MISSING_BARS_GRACE_MS`` after the close → scored on the bars stored, flagged ``detail.partial``.
* **trade idea not executed** (gate-rejected, expired, not placed, manual mode) — once ``virtual_outcome`` is known, on
  the virtual trade :func:`.executor.evaluate_virtual` scores: the same fill rules and entry, the ORIGINAL stop and
  every target, from the cycle time until the stop is touched, the farthest target is touched or the horizon
  (valid_until + 24 h) passes; the stop is checked before the targets on the same bar (as the virtual outcome).
* **NO_TRADE** — once ``COUNTERFACTUAL_BARS`` decision-timeframe bars have closed after the cycle time and the
  window's last 1m bar is stored; a window whose stored bars end at most ``COVER_TOL_MS`` early (a quiet minute, a
  session break) counts as complete only ``COVER_TOL_MS`` after its end (before that the bars may still be coming).

Everything is measured on the ANALYSIS instrument's 1m bars in the recommendation's own price space. Columns:

* ``mfe_r`` / ``mae_r`` — the largest move in the trade's favour / against it from the WORST entry edge (BUY: top of
  the entry zone, SELL: bottom — :meth:`..ai.contract.Recommendation.rr_computed`), in R of the ORIGINAL stop
  (|worst entry − stop|). Signed excursions: ``mae_r`` ≥ 1 means the stop was reached, ``mfe_r`` < 0 that the price
  never came back to the worst edge after the fill. On the bar that touched the stop (virtual trade) only its adverse
  extreme counts — the order inside a bar is unknown, read conservatively as for the virtual outcome; a virtual trade
  stopped on its fill bar takes its fill price (the trigger; MARKET: the entry price) as its favourable extreme, so
  both excursions are stored (``mae_r`` ≥ 1).
* ``tp1_hit`` … ``tp3_hit`` — the target was reached inside the window (a fourth target: ``detail.tp4_hit``).
* ``minutes_to_resolve`` — whole minutes from the cycle time (``recommendation.timestamp``) to the last leg's real
  close (paper leg ``close_ms``; MT5 closing deal time mapped server → UTC) or to the bar that decided the virtual
  outcome (its open time).
* ``exit_reason`` ∈ ``EXIT_REASONS``. Executed: how the last leg to close ended — the venue's reason (``tp``, ``sl``;
  paper ``rule`` / ``model`` closes); an MT5 ``other`` / ``cancelled`` leg is ``rule_close`` / ``model_close`` when
  ``position_actions`` holds our applied close / cancel of it (:meth:`.management.ActionLog.closed_by`); a trade whose
  orders never filled is ``not_triggered`` (or ``rule_close`` / ``model_close`` when we cancelled them). Virtual:
  tp1_first → ``tp``, sl_first → ``sl``, not_triggered → ``not_triggered``, unresolved_24h → ``expired`` (the horizon
  passed with the trade still open). ``open`` only for a leg a venue still reports live. NULL when the venue gives no
  reason that maps (a manual close or a stop-out — ``other``, kept in ``detail.legs``) or an MT5 trade settled
  before Phase 4 (no ``outcome_detail``).
* ``slippage`` — mean of the MT5 ``backend.placed[].slippage`` (fill − requested; 0.0 for pending orders, absent on a
  leg "created despite timeout"); paper: STOP fills ``fill − order price``, MARKET and LIMIT fills 0.0. Execution-
  instrument price units, sign as MT5 records it (positive = worse for a BUY, better for a SELL);
  ``detail.slippage_adverse`` is the same value signed so that positive is always against the trade.
* ``spread_at_gate`` — ask − bid of the execution quote the risk gate used (``execution_detail.spread_at_gate``;
  older rows: parsed from the ``spread_vs_sl`` check text, 2 decimals; else NULL). Execution-instrument units.
* ``commission`` (commission + fee) / ``swap`` — the settlement's split (``outcome_detail``; paper: 0.0).
* ``rejected_but_virtual_win`` — the risk gate refused the idea and its virtual outcome is ``tp1_first``.
* ``no_trade_counterfactual_atr`` — the largest move from the price at the cycle (close of the minute before the
  window) to any high / low of the next ``COUNTERFACTUAL_BARS`` decision bars, in decision-TF ATR14 — the ATR the
  model was shown (stored payload), else the decision-TF frame as of the cycle. ``detail`` has the move up / down.

Reads are bounded [start, end) windows of closed bars only (open_time + bar ≤ now); at most ``max_per_pass`` decisions
per run, oldest first, within a wall-time budget (it runs on the executor's loop thread, before the heartbeat). A
decision whose bars are not stored yet waits and is retried later without holding up the others; one decision's
failure never stops the others.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np

from ..ai.store import DecisionStore, _rejection
from ..core.instruments import InstrumentRegistry
from ..core.settings import Settings
from ..core.timeframes import Timeframe
from ..core.timeutil import MS_PER_HOUR, MS_PER_MINUTE, now_ms, parse_date_spec
from ..storage.reader import InstrumentReader
from ..storage.tablespec import spec_for
from .management import ActionLog

log = logging.getLogger("executor.metrics")

EXIT_REASONS = ("sl", "tp", "rule_close", "model_close", "expired", "not_triggered", "open")
VIRTUAL_EXIT = {"tp1_first": "tp", "sl_first": "sl", "not_triggered": "not_triggered", "unresolved_24h": "expired"}
COUNTERFACTUAL_BARS = 4                    # decision-TF bars after the cycle for a NO_TRADE's counterfactual
VIRTUAL_HORIZON_MS = 24 * MS_PER_HOUR      # after valid_until, as executor.evaluate_virtual
NOT_READY_RETRY_MS = 5 * MS_PER_MINUTE     # bars not stored yet / a virtual trade still running → asked again
ERROR_RETRY_MS = 5 * MS_PER_MINUTE         # a failing decision: retried with backoff …
ERROR_RETRY_MAX_MS = 6 * MS_PER_HOUR       # … up to this
MISSING_BARS_GRACE_MS = 6 * MS_PER_HOUR    # bars still missing this long after the window → scored on what exists
COVER_TOL_MS = 10 * MS_PER_MINUTE          # NO_TRADE: this long after its end, a window whose last stored bar is at
                                           # most this early counts as stored (no bar for a quiet minute / a break)
BUDGET_S = 5.0                             # wall time of one pass (the executor loop's heartbeat waits for it)
MAX_EXCLUDE = 500                          # waiting decisions left out by the query itself (SQL parameters)
_SPREAD = re.compile(r"spread ([\d.]+)")
_PARTIAL = re.compile(r":p\d+$")           # a paper partial close's own row (<leg id>:p<n>)
_PRIORITY = {"sl": 5, "model_close": 4, "rule_close": 3, "tp": 2, "expired": 1, "not_triggered": 1}


class MalformedDecision(ValueError):
    """The recommendation lacks what a metric needs (levels, times): scored as far as possible, never retried."""


# --------------------------------------------------------------------------- settlement → outcome_detail
def paper_outcome_detail(legs: list[dict]) -> dict:
    """``outcome_detail`` of a paper settlement from its ``paper_legs`` rows (the paper account has no commission,
    swap or fee)."""
    out = []
    for lg in legs:
        out.append({"leg": lg.get("id"), "tp_index": lg.get("tp_index"), "filled": lg.get("fill_price") is not None,
                    "reason": lg.get("close_reason") or lg.get("status"), "order_type": lg.get("order_type"),
                    "order_price": lg.get("order_price"), "fill_price": lg.get("fill_price"),
                    "open_ms": lg.get("fill_ms"), "close_ms": lg.get("close_ms")})
    opens = [x["open_ms"] for x in out if x["filled"] and x["open_ms"] is not None]
    closes = [x["close_ms"] for x in out if x["close_ms"] is not None]
    return {"venue": "paper", "commission": 0.0, "swap": 0.0, "fee": 0.0, "open_ms": min(opens) if opens else None,
            "close_ms": max(closes) if closes else None, "legs": out}


def mt5_outcome_detail(r: dict) -> dict:
    """``outcome_detail`` of an MT5 settlement from :meth:`.backends.mt5_backend.MT5Backend.decision_result`."""
    return {"venue": "mt5", "commission": r.get("commission"), "swap": r.get("swap"), "fee": r.get("fee"),
            "open_ms": r.get("open_ms"), "close_ms": r.get("close_ms"), "legs": list(r.get("exits") or [])}


# --------------------------------------------------------------------------- exit reasons
def leg_exit(reason: str | None, by: str | None = None) -> str | None:
    """One leg's end → an ``EXIT_REASONS`` value. ``by``: who closed / cancelled it according to ``position_actions``
    (rule | model | None), asked for a venue reason that does not say (MT5 ``other`` / ``cancelled``)."""
    r = (reason or "").strip().lower()
    if r in ("tp", "sl", "open"):
        return r
    if r == "pending":
        return "open"
    if r == "rule" or r.endswith("(rule)"):
        return "rule_close"
    if r == "model" or r.endswith("(model)"):
        return "model_close"
    if by in ("rule", "model"):
        return f"{by}_close"
    if r.startswith("expired") or r.startswith("cancelled"):
        return "not_triggered"
    return None


def decision_exit(legs: list[dict]) -> str | None:
    """How a trade ended: the last filled leg to close (a tie → the more adverse reason); no leg filled → who
    cancelled the orders, else ``not_triggered``. ``legs`` carry ``filled``, ``close_ms`` and ``exit``."""
    filled = [lg for lg in legs if lg.get("filled")]
    if any(lg.get("exit") == "open" for lg in filled):
        return "open"
    if filled:
        last = max(filled, key=lambda lg: (lg.get("close_ms") or 0, _PRIORITY.get(lg.get("exit"), 0)))
        return last.get("exit")
    ours = [lg.get("exit") for lg in legs if lg.get("exit") in ("model_close", "rule_close")]
    if ours:
        return "model_close" if "model_close" in ours else "rule_close"
    return "not_triggered" if legs else None


# --------------------------------------------------------------------------- levels and walks
@dataclass(frozen=True)
class Levels:
    buy: bool
    order_type: str
    entry: float              # worst entry edge (rr_computed): the R reference
    trigger: float | None     # the price a pending order fills at (executor.evaluate_virtual's entry)
    stop: float
    targets: tuple[float, ...]
    valid_until: int

    @property
    def risk(self) -> float:
        return abs(self.entry - self.stop)


def levels_of(rec: dict) -> Levels:
    """The trade's levels as the model set them (raises :class:`MalformedDecision` when they are incomplete)."""
    try:
        buy = rec["decision"] == "BUY"
        e = rec["entry"] or {}
        lo = e.get("range_min") if e.get("range_min") is not None else e.get("price")
        hi = e.get("range_max") if e.get("range_max") is not None else e.get("price")
        worst = hi if buy else lo
        trig = e.get("price") or ((e["range_max"] if buy else e["range_min"]) if e.get("range_min") is not None
                                  else None)
        tps = tuple(float(tp["price"]) for tp in rec.get("take_profits") or [])
        if worst is None or rec.get("stop_loss") is None or not tps:
            raise MalformedDecision("recommendation lacks entry / stop_loss / take_profits")
        if rec["order_type"] not in ("MARKET", "BUY_LIMIT", "BUY_STOP", "SELL_LIMIT", "SELL_STOP") or                 (rec["order_type"] != "MARKET" and trig is None):
            raise MalformedDecision(f"order type {rec['order_type']!r} without a fill rule / trigger price")
        return Levels(buy, rec["order_type"], float(worst), None if trig is None else float(trig),
                      float(rec["stop_loss"]), tps, parse_date_spec(rec["valid_until"]))
    except MalformedDecision:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise MalformedDecision(f"recommendation not scorable: {exc!r}"[:200]) from exc


@dataclass
class VirtualTrade:
    outcome: str | None                  # tp1_first | sl_first | not_triggered | unresolved_24h; None = undecided
    resolved_ms: int | None = None       # open time of the bar that decided ``outcome``
    fill_ms: int | None = None           # open time of the fill bar
    end_ms: int | None = None            # open time of the bar that ended the trade; None = still running
    best: float | None = None            # most favourable price from the fill to the end (stopped on the fill bar: the
                                         # fill price)
    worst: float | None = None           # most adverse price
    tp_hits: list[bool] = field(default_factory=list)


def virtual_trade(lv: Levels, cycle_ms: int, t: np.ndarray, h: np.ndarray, l: np.ndarray) -> VirtualTrade:
    """Walk the bars from the cycle time with :func:`.executor.evaluate_virtual`'s rules (fill, stop before target on
    one bar, ``not_triggered`` at valid_until, ``unresolved_24h`` after the horizon) and keep the trade alive after
    TP1 — with the original stop and every target — to the stop, the farthest target or the horizon."""
    horizon = lv.valid_until + VIRTUAL_HORIZON_MS
    vt = VirtualTrade(None, tp_hits=[False] * len(lv.targets))
    filled = lv.order_type == "MARKET"
    for bt, bh, bl in zip(t.tolist(), h.tolist(), l.tolist()):
        if bt < cycle_ms:
            continue
        if not filled:
            if bt >= lv.valid_until:
                vt.outcome, vt.resolved_ms, vt.end_ms = "not_triggered", bt, bt
                return vt
            hit = {"BUY_LIMIT": bl <= lv.trigger, "BUY_STOP": bh >= lv.trigger,
                   "SELL_LIMIT": bh >= lv.trigger, "SELL_STOP": bl <= lv.trigger}[lv.order_type]
            if not hit:
                continue
            filled = True
        if vt.fill_ms is None:
            vt.fill_ms = bt
        adverse, favourable = (bl, bh) if lv.buy else (bh, bl)
        vt.worst = adverse if vt.worst is None else (min if lv.buy else max)(vt.worst, adverse)
        if (bl <= lv.stop) if lv.buy else (bh >= lv.stop):
            if vt.outcome is None:
                vt.outcome, vt.resolved_ms = "sl_first", bt
            if vt.best is None:               # stopped on the fill bar: the fill price is the only favourable one known
                vt.best = lv.trigger if lv.trigger is not None else lv.entry
            vt.end_ms = bt
            return vt                         # stopped out: this bar's favourable extreme and targets do not count
        vt.best = favourable if vt.best is None else (max if lv.buy else min)(vt.best, favourable)
        for k, p in enumerate(lv.targets):
            if (bh >= p) if lv.buy else (bl <= p):
                vt.tp_hits[k] = True
        if vt.outcome is None and vt.tp_hits[0]:
            vt.outcome, vt.resolved_ms = "tp1_first", bt
        if all(vt.tp_hits):
            vt.end_ms = bt
            return vt
        if bt > horizon:
            if vt.outcome is None:
                vt.outcome, vt.resolved_ms = "unresolved_24h", bt
            vt.end_ms = bt
            return vt
    return vt


def excursions_r(lv: Levels, best: float | None, worst: float | None) -> tuple[float | None, float | None]:
    """(mfe_r, mae_r) of the price extremes from the worst entry edge, in R of the original stop."""
    risk = lv.risk
    if best is None or worst is None or not risk > 0:
        return None, None
    mfe = (best - lv.entry) if lv.buy else (lv.entry - best)
    mae = (lv.entry - worst) if lv.buy else (worst - lv.entry)
    return round(mfe / risk, 3), round(mae / risk, 3)


def spread_at_gate(detail: dict | None) -> tuple[float | None, str | None]:
    """(spread, source) from an execution_detail: the numeric key, else the ``spread_vs_sl`` check text."""
    if not isinstance(detail, dict):
        return None, None
    v = detail.get("spread_at_gate")
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v), "gate"
    for c in detail.get("gate") or []:
        if isinstance(c, dict) and c.get("check") == "spread_vs_sl" and (m := _SPREAD.search(str(c.get("detail")))):
            try:
                return float(m.group(1).rstrip(".")), "gate_text"
            except ValueError:
                return None, None
    return None, None


@dataclass
class _Bars:
    t: np.ndarray
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray

    def __len__(self) -> int:
        return len(self.t)


# --------------------------------------------------------------------------- the job
class MetricsJob:
    """One bounded pass per call of :meth:`run` (the executor's housekeeping). ``reader(key)`` is the executor's
    cached :class:`InstrumentReader`; ``actions`` the instance's ``position_actions`` (who closed an MT5 leg);
    ``paper_legs(decision_id)`` reads a paper trade's legs when it settled before ``outcome_detail`` existed."""

    def __init__(self, s: Settings, store: DecisionStore, registry: InstrumentRegistry,
                 reader: Callable[[str], InstrumentReader], *, actions: ActionLog | None = None,
                 max_per_pass: int = 20, paper_legs: Callable[[str], list[dict]] | None = None,
                 budget_s: float = BUDGET_S) -> None:
        self.s, self.store, self.reg, self.reader, self.actions = s, store, registry, reader, actions
        self.max_per_pass, self.paper_legs, self.budget_s = max_per_pass, paper_legs, budget_s
        self._later: dict[str, int] = {}          # decision → not before (ms): bars missing / still running / failing
        self._fails: dict[str, int] = {}

    # ---------------------------------------------------------------- pass
    def run(self, now_ms: int | None = None) -> int:
        """Score the due decisions (at most ``max_per_pass``, oldest first). Returns the rows written; never raises."""
        now = _now() if now_ms is None else int(now_ms)
        try:
            pairs = [p for p in self.s.enabled_pairs() if p in self.s.pairs]
            tf_max = max((self.s.pairs[p].decision_timeframe.ms for p in pairs), default=MS_PER_HOUR)
            self._later = {k: v for k, v in self._later.items() if v > now}
            waiting = sorted(self._later)[:MAX_EXCLUDE]           # beyond that: skipped here, after the read
            rows = self.store.pending_metrics(self.max_per_pass + len(self._later) - len(waiting), pairs=pairs,
                                              no_trade_before_ms=now - COUNTERFACTUAL_BARS * tf_max, exclude=waiting)
        except Exception:  # noqa: BLE001 — the executor loop must go on
            log.exception("decision metrics: the pending decisions could not be read")
            return 0
        written, tried, t0 = 0, 0, time.monotonic()
        for row in rows:
            did = row["id"]
            if tried >= self.max_per_pass or time.monotonic() - t0 > self.budget_s:
                break
            if self._later.get(did, 0) > now:
                continue
            tried += 1
            try:
                m = self.compute(row, now)
                if m is None:                     # not measurable yet: asked again later
                    self._later[did] = now + NOT_READY_RETRY_MS
                    continue
                self.store.save_metrics(m)
            except Exception as exc:  # noqa: BLE001 — one decision's failure never stops the others
                n = self._fails[did] = self._fails.get(did, 0) + 1
                self._later[did] = now + min(ERROR_RETRY_MAX_MS, ERROR_RETRY_MS * 2 ** (n - 1))
                if n == 1:
                    log.exception("decision metrics of %s failed - retried later", did[:8])
                else:
                    log.warning("decision metrics of %s failed again (%d): %r", did[:8], n, exc)
                continue
            self._fails.pop(did, None)
            self._later.pop(did, None)
            written += 1
        return written

    def compute(self, row: dict, now: int) -> dict | None:
        """The ``decision_metrics`` row of one pending decision (``pending_metrics`` shape), or None while the bars it
        needs are not stored yet. ``now`` is the data cut (closed bars, readiness)."""
        rec = _dict(row.get("recommendation"))
        cycle = _cycle_ms(rec, row["ts"])
        spread, spread_src = spread_at_gate(row.get("execution_detail"))
        # computed_ms is wall-clock time, comparable with outcome_ts (a newer settlement → the row is recomputed)
        base: dict[str, Any] = {"decision_id": row["id"], "computed_ms": _now(), "spread_at_gate": spread}
        detail: dict[str, Any] = {"v": 1, "cycle_ms": cycle}
        if spread_src:
            detail["spread_source"] = spread_src
        if row.get("decision") == "NO_TRADE":
            return self._no_trade(row, cycle, now, base, detail)
        base["rejected_but_virtual_win"] = int(
            row.get("execution_state") == "rejected" and row.get("virtual_outcome") == "tp1_first"
            and _rejection(json.dumps(row["execution_detail"]) if row.get("execution_detail") else None)[0] == "gate")
        try:
            lv = levels_of(rec)
        except MalformedDecision as exc:
            detail["error"] = str(exc)
            lv = None
        if row.get("execution_state") == "executed" and row.get("outcome") is not None:
            return self._executed(row, lv, cycle, now, base, detail)
        if lv is None:                           # nothing to walk: the row records why
            return {**base, "exit_reason": VIRTUAL_EXIT.get(row.get("virtual_outcome") or ""),
                    "detail": {**detail, "basis": "virtual", "virtual_outcome": row.get("virtual_outcome")}}
        return self._virtual(row, lv, cycle, now, base, detail)

    # ---------------------------------------------------------------- the three kinds
    def _virtual(self, row: dict, lv: Levels, cycle: int, now: int, base: dict, detail: dict) -> dict | None:
        inst = self.reg.primary(row["pair"])
        bar = inst.timeframes[0].ms
        horizon = lv.valid_until + VIRTUAL_HORIZON_MS
        b = self._bars(inst, cycle, horizon + 2 * bar, now)
        vt = virtual_trade(lv, cycle, b.t, b.h, b.l)
        partial = vt.end_ms is None
        if partial and now < horizon + bar + MISSING_BARS_GRACE_MS:
            return None                          # the virtual trade is still running (or its bars are not stored yet)
        mfe, mae = excursions_r(lv, vt.best, vt.worst)
        stored = row.get("virtual_outcome")
        detail.update(basis="virtual", virtual_outcome=stored, walk_outcome=vt.outcome, entry=lv.entry,
                      risk=round(lv.risk, 10), fill_ms=vt.fill_ms, resolved_ms=vt.resolved_ms, end_ms=vt.end_ms,
                      bars=len(b))
        if partial:
            detail["partial"] = "bars missing - scored on the bars stored"
        if vt.outcome and stored and vt.outcome != stored:
            detail["virtual_mismatch"] = True    # the stored outcome was scored on other bars (a gap filled since)
        self._tp_columns(base, detail, vt.tp_hits if vt.fill_ms is not None else None)
        return {**base, "mfe_r": mfe, "mae_r": mae,
                "minutes_to_resolve": _minutes(cycle, vt.resolved_ms),
                "exit_reason": VIRTUAL_EXIT.get(vt.outcome or stored or ""), "detail": detail}

    def _executed(self, row: dict, lv: Levels | None, cycle: int, now: int, base: dict, detail: dict) -> dict | None:
        od = row.get("outcome_detail") if isinstance(row.get("outcome_detail"), dict) else self._legacy_detail(row)
        legs = []
        for lg in (od or {}).get("legs") or []:
            if not isinstance(lg, dict):
                continue
            by = None
            if self.actions is not None and lg.get("leg") and (lg.get("reason") or "other") in ("other", "cancelled"):
                by = self.actions.closed_by(str(lg["leg"]))
            legs.append({**lg, "exit": leg_exit(lg.get("reason"), by), **({"closed_by": by} if by else {})})
        filled = row.get("outcome") != "not_filled"
        close_ms = (od or {}).get("close_ms")
        detail.update(basis="broker", outcome=row.get("outcome"), venue=(od or {}).get("venue"),
                      open_ms=(od or {}).get("open_ms"), close_ms=close_ms, legs=legs)
        if od is None:
            detail["no_outcome_detail"] = "settled before Phase 4: no venue split, exit reason or close time"
        ex = _dict(row.get("execution_detail"))
        if ex.get("single_leg"):
            detail["single_leg"] = ex["single_leg"]
        out = {**base, "exit_reason": decision_exit(legs) if legs else None,
               "minutes_to_resolve": _minutes(cycle, close_ms)}
        if od is not None:
            c, f = od.get("commission"), od.get("fee")
            out["commission"] = None if c is None and f is None else round((c or 0.0) + (f or 0.0), 4)
            out["swap"] = od.get("swap")
        slip = _slippage(ex, legs)
        if slip is not None:
            out["slippage"] = slip
            if lv is not None:
                detail["slippage_adverse"] = slip if lv.buy else -slip
            detail["slippage_units"] = ("fill - requested, execution-instrument price "
                                        "(+ = worse for BUY, better for SELL)")
        if not filled or lv is None:
            out["detail"] = detail
            return out
        # the excursion window: from the real fill (else the virtual fill) to the last close (else the settlement)
        inst = self.reg.primary(row["pair"])
        bar = inst.timeframes[0].ms
        end = close_ms
        if end is None:
            end = row.get("outcome_ts") or now
            detail["window_end"] = "settlement time (the venue gave no close time)"
        last = _floor(end, bar)                  # the bar the last leg closed in: often where the exit price was
        b = self._bars(inst, cycle, last + bar, now)
        if not (now >= last + bar and len(b) and b.t[-1] >= last):
            if now < end + MISSING_BARS_GRACE_MS:
                return None                      # still forming / not stored yet (a row written now would be final)
            detail["partial"] = "bars missing - scored on the bars stored"
        start = (od or {}).get("open_ms")
        if start is None:
            start = virtual_trade(lv, cycle, b.t, b.h, b.l).fill_ms or cycle
            detail["window_start"] = "virtual fill (the venue gave no fill time)"
        sel = (b.t >= _floor(start, bar)) & (b.t <= end)
        if not sel.any():
            detail["partial"] = "no bars between the fill and the close"
            out["detail"] = detail
            return out
        h, l = b.h[sel], b.l[sel]
        best, worst = (float(h.max()), float(l.min())) if lv.buy else (float(l.min()), float(h.max()))
        out["mfe_r"], out["mae_r"] = excursions_r(lv, best, worst)
        hits = [bool((h >= p).any()) if lv.buy else bool((l <= p).any()) for p in lv.targets]
        self._tp_columns(out, detail, hits)
        detail.update(entry=lv.entry, risk=round(lv.risk, 10), bars=int(sel.sum()))
        out["detail"] = detail
        return out

    def _no_trade(self, row: dict, cycle: int, now: int, base: dict, detail: dict) -> dict | None:
        pair = row["pair"]
        inst = self.reg.primary(pair)
        tf = self.s.pairs[pair].decision_timeframe if pair in self.s.pairs else Timeframe.M15
        bar = inst.timeframes[0].ms
        end = tf.floor(cycle) + COUNTERFACTUAL_BARS * tf.ms      # the 4th decision bar closing after the cycle
        if now < end:
            return None
        first = -(-cycle // bar) * bar                            # the first whole bar after the cycle time
        b = self._bars(inst, first - bar, end, now)
        win = b.t >= first
        last = int(b.t[win][-1]) if win.any() else None
        # the window's last bar stored; else a short gap at the end only once the bars had time to be ingested
        covered = last is not None and (last >= end - bar or
                                        (now >= end + COVER_TOL_MS and last >= end - bar - COVER_TOL_MS))
        if not covered and now < end + MISSING_BARS_GRACE_MS:
            return None
        detail.update(basis="no_trade", window=[first, end], bars=int(win.sum()), timeframe=tf.value)
        out = {**base, "detail": detail}
        if not win.any():
            detail["partial"] = "no bars stored for the window"
            return out
        if not covered:
            detail["partial"] = "bars missing - scored on the bars stored"
        before = b.t == first - bar
        ref = float(b.c[before][-1]) if before.any() else float(b.o[win][0])
        atr, src = self._atr(row, inst, tf, cycle)
        up, down = float(b.h[win].max()) - ref, ref - float(b.l[win].min())
        detail.update(ref_price=ref, atr=atr, atr_source=src, move_up=round(up, 10), move_down=round(down, 10))
        if atr and atr > 0:
            detail.update(up_atr=round(up / atr, 3), down_atr=round(down / atr, 3))
            out["no_trade_counterfactual_atr"] = round(max(up, down, 0.0) / atr, 3)
        else:
            detail["partial"] = "no decision-timeframe ATR"
        return out

    # ---------------------------------------------------------------- helpers
    def _bars(self, inst, start: int, end: int, now: int) -> _Bars:
        """Closed 1m bars of the analysis instrument in [start, end) — one bounded, indexed read."""
        tf1 = inst.timeframes[0]
        end = min(int(end), int(now))
        cols = ("open_time", "open", "high", "low", "close")
        if end <= start:
            return _Bars(*(np.empty(0, dtype=np.int64 if c == "open_time" else float) for c in cols))
        c = self.reader(inst.key).read_range(spec_for(inst, "candles", tf1), int(start), end, list(cols))
        t = np.asarray(c["open_time"], dtype=np.int64)
        keep = t + tf1.ms <= now                  # a forming bar is never scored
        return _Bars(t[keep], *(np.asarray(c[k], dtype=float)[keep] for k in cols[1:]))

    def _atr(self, row: dict, inst, tf: Timeframe, cycle: int) -> tuple[float | None, str | None]:
        """The decision-TF ATR14 the model was shown (stored payload), else computed as the snapshot does."""
        if row.get("payload_hash"):
            try:
                p = self.store.load_payload(row["payload_hash"]) or {}
                a = ((((p.get("timeframes") or {}).get(tf.value) or {}).get("indicators")) or {}).get("atr14")
                if a:
                    return float(a), "payload"
            except Exception:  # noqa: BLE001 — a damaged payload falls back to the bars
                log.debug("payload of %s not readable", row["id"][:8], exc_info=True)
        try:
            from ..analysis import indicators as ind
            from ..analysis.frames import load_frame
            from ..analysis.snapshot import TF_PLAN
            fr = load_frame(self.reader(inst.key), inst, tf, TF_PLAN.get(tf.value, (60, 0))[0], cycle)
            a = ind.atr(fr.high, fr.low, fr.close) if len(fr) else []
            if len(a) and not np.isnan(a[-1]):
                return float(a[-1]), "frame"
        except Exception:  # noqa: BLE001 — no decision-TF table / bars: the counterfactual stays NULL
            log.debug("decision-TF ATR of %s not computable", row["id"][:8], exc_info=True)
        return None, None

    def _legacy_detail(self, row: dict) -> dict | None:
        """A paper trade settled before ``outcome_detail`` existed: rebuilt from its legs (same app.db)."""
        if self.paper_legs is None or _dict(row.get("execution_detail")).get("mode") != "paper":
            return None
        legs = self.paper_legs(row["id"])
        return paper_outcome_detail(legs) if legs else None

    @staticmethod
    def _tp_columns(out: dict, detail: dict, hits: list[bool] | None) -> None:
        if hits is None:
            return
        for k in range(3):
            out[f"tp{k + 1}_hit"] = int(hits[k]) if k < len(hits) else None
        if len(hits) > 3:
            detail["tp4_hit"] = int(hits[3])


# --------------------------------------------------------------------------- small helpers
def _now() -> int:
    return now_ms()


def _cycle_ms(rec: dict, record_ts: int) -> int:
    """The cycle's as-of time (``recommendation.timestamp``, set by the system); the record time otherwise."""
    try:
        return parse_date_spec(rec["timestamp"])
    except (KeyError, TypeError, ValueError):
        return int(record_ts)


def _minutes(cycle: int, at: int | None) -> int | None:
    return None if at is None else max(0, int(at) - int(cycle)) // MS_PER_MINUTE


def _dict(v: Any) -> dict:
    return v if isinstance(v, dict) else {}


def _floor(ms: int, step: int) -> int:
    return int(ms) // step * step


def _slippage(ex: dict, legs: list[dict]) -> float | None:
    """Mean slippage of the trade's fills (see the module docstring for the units and the sign)."""
    placed = ((ex.get("backend") or {}).get("placed") or []) if isinstance(ex.get("backend"), dict) else []
    vals = [float(p["slippage"]) for p in placed if isinstance(p, dict) and isinstance(p.get("slippage"), (int, float))]
    if not vals and ex.get("mode") == "paper":
        for lg in legs:
            if not lg.get("filled") or _PARTIAL.search(str(lg.get("leg") or "")):
                continue
            ot = lg.get("order_type") or ""
            if ot.endswith("_STOP") and lg.get("fill_price") is not None and lg.get("order_price") is not None:
                vals.append(float(lg["fill_price"]) - float(lg["order_price"]))
            elif ot:
                vals.append(0.0)
    return round(sum(vals) / len(vals), 10) if vals else None


__all__ = ["EXIT_REASONS", "Levels", "MalformedDecision", "MetricsJob", "VirtualTrade", "decision_exit",
           "excursions_r", "leg_exit", "levels_of", "mt5_outcome_detail", "paper_outcome_detail", "spread_at_gate",
           "virtual_trade"]
