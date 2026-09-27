"""When to ask the AI (P8.6, Phase 3 §3.7.2): trigger policies, 5-minute screening, next-review scheduler.

every_close      every decision-timeframe close (market open)
on_setup_event   only when the quant layer sees a candidate setup (≥1 strong or ≥ ``weak_min`` weak reasons)
hybrid           setup events, due ``next_review`` conditions of the last decision, or idle timeout
A per-pair minimum spacing protects quotas; ``next_review`` triggers may come sooner but never before
``review_floor_min`` (and the engine fires them at most once per decision), and a per-pair back-off after failed
cycles overrides both (F1). The Cost Governor can force ``on_setup_event``.

Phase 3 ("called only on change"): Python screens every close of the screen timeframe (5m). Every setup reason has a
stable ``key`` (the same setup on the next screen has the same key); the engine stores the keys seen at the last
dispatch (``last_signature``) and only *new* keys can wake the model again. Executor events and price/candle review
conditions still wake it (with the short review floor); a time-based review fires only when something moved.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Literal

from ..core.timeutil import iso

STRONG_EVENTS = {"BOS", "CHoCH"}
REVERSAL_PATTERNS = ("bullish_engulfing", "bearish_engulfing", "bullish_pin_bar", "bearish_pin_bar")
TIME_REVIEWS = {"minutes_elapsed"}                                  # + next_review.in_minutes
CONDITION_REVIEWS = {"price_above", "price_below", "candle_close_above", "candle_close_below"}


@dataclass
class TriggerDecision:
    fire: bool
    reasons: list[str]
    strength: str          # "strong" | "weak" | "none" | "review" | "idle" | "close" | "event"
    signature: frozenset[str] = frozenset()     # every current setup key — persisted by the engine at dispatch


@dataclass(frozen=True)
class Reason:
    """One setup reason. ``key`` identifies the setup itself (timeframe, kind, direction and the bar/level that
    defines it) — never the current price — so an unchanged setup has the same key on every screen."""
    key: str
    text: str
    strength: Literal["strong", "weak"]
    kind: str = "other"          # structure | location (decision-TF/1h zone or liquidity) | pattern | flow | other


# ------------------------------------------------------------------ setup scan
def _scan_tf(tf: str, t: dict, bias: str | None, liquidity_atr: float,
             structure: Literal["strong", "weak"], *, location: bool = True,
             at_location: bool = False) -> list[Reason]:
    """Reasons on one timeframe's latest *closed* bar. ``structure`` is the strength of its BOS/CHoCH/sweep events
    (strong on the decision TF and 1h; weak on the screen TF — a 5m break alone is noise). ``location=False`` (the 5m
    screen TF): its zones and liquidity are not locations — only its structure breaks, and a reversal candle when
    price is at a decision-TF / 1h location (``at_location``), count as confirmations."""
    out: list[Reason] = []
    recent = t.get("recent") or []
    if not recent:
        return out
    last_bar = recent[-1][0]
    for e in (t.get("structure") or {}).get("events") or []:
        if e["time"] != last_bar:
            continue
        if e["kind"] in STRONG_EVENTS:
            text = f"{tf} {e['kind']} {e['dir']} through {e['level']}"
        elif e["kind"] == "sweep":
            # a sweep of liquidity that closed back inside is a classic reversal setup
            text = f"{tf} liquidity sweep ({e['dir']}) at {e['level']}"
        else:
            continue
        # a sweep is keyed by its pool, so consecutive bars sweeping the same level are one setup, not one per bar
        anchor = e["level"] if e["kind"] == "sweep" else e["time"]
        out.append(Reason(f"{tf}:{e['kind']}:{e['dir']}:{anchor}", text, structure, "structure"))
    ind = t.get("indicators") or {}
    close, atr = ind.get("close"), ind.get("atr14")
    if not location:
        pats = (t.get("patterns") or {}).get("last_bar") or []
        if at_location and any(x in pats for x in REVERSAL_PATTERNS):
            out.append(Reason(f"{tf}:pattern:{last_bar}", f"{tf} reversal candle ({', '.join(pats)}) at the "
                                                          "location", "weak", "pattern"))
        return out
    if close is None or not atr:
        return out
    in_zone = False
    for kind in ("order_blocks", "fvg"):
        name = kind[:-1] if kind.endswith("s") else kind
        for z in (t.get("zones") or {}).get(kind) or []:
            inside = z["bottom"] <= close <= z["top"]
            aligned = bias in (None, "mixed") or (z["dir"] == bias)
            if inside and aligned:
                in_zone = True
                out.append(Reason(f"{tf}:{name}:{z['dir']}:{z['top']}-{z['bottom']}",
                                  f"price inside {tf} {z['dir']} {name} {z['bottom']}-{z['top']}", "weak", "location"))
    liq = t.get("liquidity") or {}
    for side, key, short in (("buy-side", "buy_side_above", "buy"), ("sell-side", "sell_side_below", "sell")):
        for p in (liq.get(key) or [])[:1]:
            if abs(p["level"] - close) <= liquidity_atr * atr:
                out.append(Reason(f"{tf}:liq:{short}:{p['level']}",
                                  f"price within {liquidity_atr:g} ATR of {tf} {side} liquidity {p['level']}", "weak",
                                  "location"))
    pats = (t.get("patterns") or {}).get("last_bar") or []
    if in_zone and any(x in pats for x in REVERSAL_PATTERNS):
        out.append(Reason(f"{tf}:pattern:{last_bar}", f"{tf} reversal candle ({', '.join(pats)}) inside a zone",
                          "weak", "pattern"))
    return out


def _div_anchor(div: dict, bar_time: str | None) -> str | None:
    """The divergence's identity across screens. The stored block carries the last two pivots as frame *indices*
    (they slide by one every new bar) and prices, not times — so a time field is used when present, else the last
    pivot's price (fixed for the same pivot), else the decision bar."""
    for k in ("last_pivot_time", "time"):
        if div.get(k):
            return str(div[k])
    times = div.get("pivot_times") or []
    if times:
        return str(times[-1])
    prices = div.get("price") or []
    return str(prices[-1]) if prices else bar_time


def scan_setups(payload: dict, *, liquidity_atr: float = 0.3, screen_tf: str | None = None) -> list[Reason]:
    """Setup reasons in a stored snapshot payload, evaluated on the latest *closed* bars only: structure/zones/
    liquidity/reversal candles on the decision TF and 1h (their structure events strong), the same on ``screen_tf``
    when given (its structure events weak), then order flow on the last decision bar (weak)."""
    tfs = payload.get("timeframes") or {}
    dec = payload["meta"]["decision_timeframe"]
    bias = (payload.get("confluence") or {}).get("bias")
    scans: list[tuple[str, Literal["strong", "weak"]]] = []
    for tf in (dec, "1h"):
        if tf not in (s[0] for s in scans):         # a 1h decision TF is scanned once, not twice
            scans.append((tf, "strong"))
    out: list[Reason] = []
    for tf, structure in scans:
        out += _scan_tf(tf, tfs.get(tf) or {}, bias, liquidity_atr, structure)
    if screen_tf and screen_tf in tfs and screen_tf not in (s[0] for s in scans):
        at_loc = any(r.kind == "location" for r in out)
        out += _scan_tf(screen_tf, tfs.get(screen_tf) or {}, bias, liquidity_atr, "weak", location=False,
                        at_location=at_loc)
    dec_recent = (tfs.get(dec) or {}).get("recent") or []
    dec_bar = dec_recent[-1][0] if dec_recent else None
    of = payload.get("orderflow") or {}
    bars = (of.get("footprint") or {}).get("bars") or []
    if bars:
        lb = bars[-1]
        bt = lb.get("time") or dec_bar
        if lb.get("stacked_buy") or lb.get("stacked_sell"):
            out.append(Reason(f"fp:{bt}:stacked", "stacked footprint imbalances on the last decision bar", "weak",
                              "flow"))
        if lb.get("absorption"):
            out.append(Reason(f"fp:{bt}:absorption", f"absorption on the last decision bar ({lb['absorption']})",
                              "weak", "flow"))
    div = (of.get("bar_delta") or {}).get("divergence")
    if div:
        out.append(Reason(f"div:{div['type']}:{_div_anchor(div, dec_bar)}", f"{div['type']} price/CVD divergence",
                          "weak", "flow"))
    return out


def setup_signature(reasons: Iterable[Reason]) -> frozenset[str]:
    """The keys of ``reasons`` — what the engine persists at dispatch and later compares against."""
    return frozenset(r.key for r in reasons)


def setup_reasons(payload: dict, *, liquidity_atr: float = 0.3,
                  screen_tf: str | None = None) -> tuple[list[str], list[str]]:
    """(strong, weak) reason texts found in a snapshot payload — evaluated on the latest *closed* bars only
    (back-compatible view of :func:`scan_setups`)."""
    found = scan_setups(payload, liquidity_atr=liquidity_atr, screen_tf=screen_tf)
    return [r.text for r in found if r.strength == "strong"], [r.text for r in found if r.strength == "weak"]


# ------------------------------------------------------------------ next_review
def _review_items(last: dict | None, now: int, quote_mid: float | None,
                  closed_bars: dict[str, float]) -> list[tuple[str, str]]:
    """Due ``next_review`` items of the last valid decision as ("time"|"condition", text), in declaration order."""
    if not last or not last.get("recommendation"):
        return []
    rec = last["recommendation"]
    nr = rec.get("next_review") or {}
    out: list[tuple[str, str]] = []
    base = int(last["ts"])
    if nr.get("in_minutes") and now >= base + int(nr["in_minutes"]) * 60_000:
        out.append(("time", f"next_review time reached ({nr['in_minutes']} min after {iso(base)})"))
    for c in nr.get("conditions", []):
        k, v = c.get("kind"), c.get("value")
        if k == "minutes_elapsed" and now >= base + float(v) * 60_000:
            out.append(("time", f"review condition: {v} minutes elapsed"))
        elif k == "price_above" and quote_mid is not None and quote_mid > v:
            out.append(("condition", f"review condition: price above {v}"))
        elif k == "price_below" and quote_mid is not None and quote_mid < v:
            out.append(("condition", f"review condition: price below {v}"))
        elif k in ("candle_close_above", "candle_close_below"):
            close = closed_bars.get(c.get("timeframe") or "15m")
            if close is not None and ((k == "candle_close_above" and close > v)
                                      or (k == "candle_close_below" and close < v)):
                out.append(("condition", f"review condition: {c.get('timeframe')} close "
                                         f"{'above' if k.endswith('above') else 'below'} {v}"))
    return out


def review_due(last: dict | None, now: int, quote_mid: float | None, closed_bars: dict[str, float]) -> list[str]:
    """Reasons from the last valid decision's ``next_review`` (time elapsed or price conditions met). Times count
    from the stored row (``last['ts']``), never from a model-written timestamp. ``quote_mid`` must be in the
    recommendation's price space (the analysis instrument)."""
    return [text for _, text in _review_items(last, now, quote_mid, closed_bars)]


def review_due_split(last: dict | None, now: int, quote_mid: float | None,
                     closed_bars: dict[str, float]) -> tuple[list[str], list[str]]:
    """:func:`review_due` split into (time reasons, condition reasons): ``in_minutes``/``minutes_elapsed`` only say
    "look again" (they fire only when something changed), price/candle conditions say "the market did X" (they fire
    on their own, with the review floor)."""
    items = _review_items(last, now, quote_mid, closed_bars)
    return [t for k, t in items if k == "time"], [t for k, t in items if k == "condition"]


# ------------------------------------------------------------------ the decision
def _spacing_block(last_call_ms: int | None, now: int, need_ms: int) -> str | None:
    if last_call_ms is None:
        return None
    gap = now - last_call_ms
    if gap < need_ms:
        return f"spacing: last AI call {int(gap / 60000)} min ago (next after {need_ms // 60000} min)"
    return None


def _event_reason(texts: list[str]) -> str:
    """Executor events coalesced into ONE reason with the ``event:`` prefix the prompt legend documents."""
    parts = [t[len("event:"):].strip() if t.startswith("event:") else t.strip() for t in texts]
    return "event: " + "; ".join(p for p in parts if p)


def _decide_legacy(policy: str, payload: dict | None, *, last_call_ms: int | None, now: int, min_spacing_min: int,
                   max_idle_min: int, review_reasons: list[str], at_close: bool, review_floor_min: int,
                   backoff_ms: int, weak_min: int, liquidity_atr: float, screen_tf: str | None) -> TriggerDecision:
    """The pre-Phase-3 rule (callers that pass no ``time_reasons``/``event_reasons``): every setup counts, not only
    new ones; any due review fires with the floor."""
    need = max(backoff_ms, (min(review_floor_min, min_spacing_min) if review_reasons else min_spacing_min) * 60_000)
    block = _spacing_block(last_call_ms, now, need)
    if block:
        return TriggerDecision(False, [block], "none")
    if review_reasons:
        return TriggerDecision(True, review_reasons, "review")
    if not at_close or payload is None:
        return TriggerDecision(False, [], "none")
    if policy == "every_close":
        return TriggerDecision(True, [f"{payload['meta']['decision_timeframe']} close"], "close")
    found = scan_setups(payload, liquidity_atr=liquidity_atr, screen_tf=screen_tf)
    sig = setup_signature(found)
    strong = [r.text for r in found if r.strength == "strong"]
    weak = [r.text for r in found if r.strength == "weak"]
    if strong or len(weak) >= weak_min:
        return TriggerDecision(True, strong + weak, "strong" if strong else "weak", sig)
    if policy == "hybrid" and (last_call_ms is None or now - last_call_ms >= max_idle_min * 60_000):
        return TriggerDecision(True, [f"idle: no AI review for ≥{max_idle_min} min"] + weak, "idle", sig)
    return TriggerDecision(False, weak, "none", sig)


# candidate kind → (strength reported, uses the short review floor)
_KINDS = {"event": ("event", True), "condition": ("review", True), "time": ("review", False),
          "strong": ("strong", False), "weak": ("weak", False), "close": ("close", False), "idle": ("idle", False)}


def _weak_at_location(found: list[Reason], new: list[Reason], screen_tf: str | None) -> bool:
    """Price is at a location and something new there is price action (not only a 5m candle or order flow)."""
    carries = any(r.kind in ("location", "structure")
                  or (r.kind == "pattern" and not (screen_tf and r.key.startswith(f"{screen_tf}:"))) for r in new)
    return carries and any(r.kind == "location" for r in found)


def decide(policy: str, payload: dict | None, *, last_call_ms: int | None, now: int, min_spacing_min: int,
           max_idle_min: int, review_reasons: list[str], at_close: bool, review_floor_min: int = 5,
           backoff_ms: int = 0, weak_min: int = 2, liquidity_atr: float = 0.3, screen_tf: str | None = None,
           last_signature: frozenset[str] | None = None, time_reasons: list[str] | None = None,
           event_reasons: list[str] | None = None, at_decision_close: bool | None = None,
           decision_bar_since_last_call: bool = True, move_atr: float | None = None,
           screen_move_atr: float = 0.5, weak_needs_location: bool = False) -> TriggerDecision:
    """Whether to call the model now.

    Phase 3 path (the engine passes ``time_reasons``/``event_reasons``): ``review_reasons`` are the price/candle
    conditions only. Candidates, strongest first: executor events → condition reviews → time reviews (only when
    ``changed`` = a decision bar closed since the last call and a new weak reason or a move > ``screen_move_atr``
    ATR) → at a screen close: every_close fires at a decision close; else new strong / ≥ ``weak_min`` new weak
    setup keys (not in ``last_signature``; None = first screen, all new) → hybrid idle at a decision close.
    Spacing is checked for the strongest candidate: the review floor for events and condition reviews, the minimum
    spacing otherwise, and the failure back-off above both. ``signature`` always holds every current setup key.
    ``weak_needs_location``: weak setups call only while price is at a decision-TF / 1h location (inside a zone or
    near liquidity — seen before or new) and one of the new reasons is price action (a new location, a structure
    break, or a decision-TF reversal candle); new confirmations without a location are not a setup, and 5m candles
    or order flow alone at a location the model already saw wait for its own ``next_review`` conditions.
    Without those arguments the pre-Phase-3 behaviour is kept exactly."""
    if time_reasons is None and event_reasons is None:
        return _decide_legacy(policy, payload, last_call_ms=last_call_ms, now=now, min_spacing_min=min_spacing_min,
                              max_idle_min=max_idle_min, review_reasons=review_reasons, at_close=at_close,
                              review_floor_min=review_floor_min, backoff_ms=backoff_ms, weak_min=weak_min,
                              liquidity_atr=liquidity_atr, screen_tf=screen_tf)
    at_dec = at_close if at_decision_close is None else at_decision_close
    found = scan_setups(payload, liquidity_atr=liquidity_atr, screen_tf=screen_tf) if payload is not None else []
    sig = setup_signature(found)
    seen = last_signature or frozenset()
    new_strong = [r.text for r in found if r.strength == "strong" and r.key not in seen]
    new_weak_r = [r for r in found if r.strength == "weak" and r.key not in seen]
    new_weak = [r.text for r in new_weak_r]
    n_new_weak = len({r.key for r in new_weak_r})          # a key counts once, however many texts share it
    moved = move_atr is not None and move_atr > screen_move_atr
    changed = decision_bar_since_last_call and (n_new_weak >= 1 or moved)

    cands: list[tuple[str, list[str]]] = []
    if event_reasons:
        cands.append(("event", [_event_reason(event_reasons)]))
    if review_reasons:
        cands.append(("condition", list(review_reasons)))
    if time_reasons and changed:
        why = [f"price moved {move_atr:.2f} ATR since the last call"] if moved else []
        cands.append(("time", list(time_reasons) + why + new_weak))
    if at_close and payload is not None:
        if policy == "every_close":
            if at_dec:
                cands.append(("close", [f"{payload['meta']['decision_timeframe']} close"]))
        else:
            if new_strong:
                cands.append(("strong", new_strong + new_weak))
            elif n_new_weak >= weak_min and (not weak_needs_location or _weak_at_location(found, new_weak_r,
                                                                                            screen_tf)):
                cands.append(("weak", new_weak))
            if policy == "hybrid" and at_dec and (last_call_ms is None or now - last_call_ms >= max_idle_min * 60_000):
                cands.append(("idle", [f"idle: no AI review for ≥{max_idle_min} min"]
                              + [r.text for r in found if r.strength == "weak"]))
    if not cands:
        return TriggerDecision(False, [r.text for r in found], "none", sig)

    kind, first = cands[0]
    strength, floor = _KINDS[kind]
    need = max(backoff_ms, (min(review_floor_min, min_spacing_min) if floor else min_spacing_min) * 60_000)
    block = _spacing_block(last_call_ms, now, need)
    reasons = list(first)
    for k, rs in cands[1:]:                     # what else woke up at the same time (not the idle/close filler)
        if k not in ("idle", "close"):
            reasons += [r for r in rs if r not in reasons]
    if block:
        return TriggerDecision(False, [f"{block}; waiting: {kind}"] + reasons, "none", sig)
    return TriggerDecision(True, reasons, strength, sig)
