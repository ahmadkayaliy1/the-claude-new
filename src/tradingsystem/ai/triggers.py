"""When to ask the AI (P8.6): trigger policies + next-review scheduler.

every_close      every decision-timeframe close (market open)
on_setup_event   only when the quant layer sees a candidate setup (≥1 strong or ≥2 weak reasons)
hybrid           setup events, due ``next_review`` conditions of the last decision, or idle timeout
A per-pair minimum spacing protects free-tier quotas; the Cost Governor can force ``on_setup_event``.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..core.timeutil import iso, parse_date_spec

STRONG_EVENTS = {"BOS", "CHoCH"}


@dataclass
class TriggerDecision:
    fire: bool
    reasons: list[str]
    strength: str          # "strong" | "weak" | "none" | "review" | "idle" | "close"


def setup_reasons(payload: dict) -> tuple[list[str], list[str]]:
    """(strong, weak) reasons found in a snapshot payload — evaluated on the latest *closed* bars only."""
    strong, weak = [], []
    tfs = payload.get("timeframes", {})
    dec = payload["meta"]["decision_timeframe"]
    bias = payload.get("confluence", {}).get("bias")
    for tf in (dec, "1h"):
        t = tfs.get(tf) or {}
        recent = t.get("recent") or []
        if not recent:
            continue
        last_bar = recent[-1][0]
        for e in (t.get("structure") or {}).get("events", []):
            if e["time"] != last_bar:
                continue
            if e["kind"] in STRONG_EVENTS:
                strong.append(f"{tf} {e['kind']} {e['dir']} through {e['level']}")
            elif e["kind"] == "sweep":
                # a sweep of liquidity that closed back inside is a classic reversal setup
                strong.append(f"{tf} liquidity sweep ({e['dir']}) at {e['level']}")
        close = (t.get("indicators") or {}).get("close")
        atr = (t.get("indicators") or {}).get("atr14")
        if close is None or not atr:
            continue
        for kind in ("order_blocks", "fvg"):
            for z in (t.get("zones") or {}).get(kind, []):
                inside = z["bottom"] <= close <= z["top"]
                aligned = bias in (None, "mixed") or (z["dir"] == bias)
                if inside and aligned:
                    weak.append(f"price inside {tf} {z['dir']} {kind[:-1] if kind.endswith('s') else kind} "
                                f"{z['bottom']}-{z['top']}")
        liq = t.get("liquidity") or {}
        for side, key in (("buy-side", "buy_side_above"), ("sell-side", "sell_side_below")):
            for p in liq.get(key, [])[:1]:
                if abs(p["level"] - close) <= 0.3 * atr:
                    weak.append(f"price within 0.3 ATR of {tf} {side} liquidity {p['level']}")
        pats = (t.get("patterns") or {}).get("last_bar", [])
        if any(x in pats for x in ("bullish_engulfing", "bearish_engulfing", "bullish_pin_bar", "bearish_pin_bar")) \
                and any(r.startswith(f"price inside {tf}") for r in weak):
            weak.append(f"{tf} reversal candle ({', '.join(pats)}) inside a zone")
    fp = (payload.get("orderflow") or {}).get("footprint") or {}
    bars = fp.get("bars") or []
    if bars:
        lb = bars[-1]
        if lb.get("stacked_buy") or lb.get("stacked_sell"):
            weak.append("stacked footprint imbalances on the last decision bar")
        if lb.get("absorption"):
            weak.append(f"absorption on the last decision bar ({lb['absorption']})")
    div = ((payload.get("orderflow") or {}).get("bar_delta") or {}).get("divergence")
    if div:
        weak.append(f"{div['type']} price/CVD divergence")
    return strong, weak


def review_due(last: dict | None, now: int, quote_mid: float | None, closed_bars: dict[str, float]) -> list[str]:
    """Reasons from the last valid decision's ``next_review`` (time elapsed or price conditions met)."""
    if not last or not last.get("recommendation"):
        return []
    rec = last["recommendation"]
    nr = rec.get("next_review") or {}
    out = []
    try:
        base = parse_date_spec(rec["timestamp"])
    except Exception:  # noqa: BLE001
        base = last["ts"]
    if nr.get("in_minutes") and now >= base + int(nr["in_minutes"]) * 60_000:
        out.append(f"next_review time reached ({nr['in_minutes']} min after {iso(base)})")
    for c in nr.get("conditions", []):
        k, v = c.get("kind"), c.get("value")
        if k == "minutes_elapsed" and now >= base + float(v) * 60_000:
            out.append(f"review condition: {v} minutes elapsed")
        elif k == "price_above" and quote_mid is not None and quote_mid > v:
            out.append(f"review condition: price above {v}")
        elif k == "price_below" and quote_mid is not None and quote_mid < v:
            out.append(f"review condition: price below {v}")
        elif k in ("candle_close_above", "candle_close_below"):
            close = closed_bars.get(c.get("timeframe") or "15m")
            if close is not None and ((k == "candle_close_above" and close > v) or (k == "candle_close_below" and close < v)):
                out.append(f"review condition: {c.get('timeframe')} close {'above' if k.endswith('above') else 'below'} {v}")
    return out


def decide(policy: str, payload: dict | None, *, last_call_ms: int | None, now: int, min_spacing_min: int,
           max_idle_min: int, review_reasons: list[str], at_close: bool) -> TriggerDecision:
    if last_call_ms is not None and now - last_call_ms < min_spacing_min * 60_000 and not review_reasons:
        return TriggerDecision(False, [f"spacing: last AI call {int((now - last_call_ms) / 60000)} min ago"], "none")
    if review_reasons:
        return TriggerDecision(True, review_reasons, "review")
    if not at_close or payload is None:
        return TriggerDecision(False, [], "none")
    if policy == "every_close":
        return TriggerDecision(True, [f"{payload['meta']['decision_timeframe']} close"], "close")
    strong, weak = setup_reasons(payload)
    if strong or len(weak) >= 2:
        return TriggerDecision(True, strong + weak, "strong" if strong else "weak")
    if policy == "hybrid" and (last_call_ms is None or now - last_call_ms >= max_idle_min * 60_000):
        return TriggerDecision(True, [f"idle: no AI review for ≥{max_idle_min} min"] + weak, "idle")
    return TriggerDecision(False, weak, "none")
