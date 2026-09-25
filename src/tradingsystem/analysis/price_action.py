"""Candlestick / price-action patterns (P6.6) on closed candles; each flag at i uses bars ≤ i only."""
from __future__ import annotations

import numpy as np

F = np.ndarray


def patterns(open_: F, high: F, low: F, close: F, i: int) -> list[str]:
    """Patterns completed at bar i (a closed bar)."""
    if i < 2:
        return []
    o, h, l, c = open_[i], high[i], low[i], close[i]
    rng = h - l
    if rng <= 0:
        return ["zero_range"]
    body = abs(c - o)
    upper, lower = h - max(o, c), min(o, c) - l
    po, pc = open_[i - 1], close[i - 1]
    out = []
    if body <= 0.1 * rng:
        out.append("doji")
    if lower >= 2 * body and lower >= 0.6 * rng:
        out.append("bullish_pin_bar")
    if upper >= 2 * body and upper >= 0.6 * rng:
        out.append("bearish_pin_bar")
    if c > o and pc < po and c >= po and o <= pc and body > abs(pc - po):
        out.append("bullish_engulfing")
    if c < o and pc > po and c <= po and o >= pc and body > abs(pc - po):
        out.append("bearish_engulfing")
    if h <= high[i - 1] and l >= low[i - 1]:
        out.append("inside_bar")
    if h > high[i - 1] and l < low[i - 1]:
        out.append("outside_bar")
    if body >= 0.9 * rng:
        out.append("bullish_marubozu" if c > o else "bearish_marubozu")
    return out


def range_state(high: F, low: F, close: F, atr_values: F, lookback: int = 30) -> dict | None:
    """Is the market compressing in a range? (width of the last ``lookback`` bars in ATR units)."""
    if len(close) < lookback or np.isnan(atr_values[-1]) or atr_values[-1] <= 0:
        return None
    hi, lo = float(high[-lookback:].max()), float(low[-lookback:].min())
    width_atr = (hi - lo) / atr_values[-1]
    return {"high": hi, "low": lo, "width_atr": round(float(width_atr), 2), "is_range": bool(width_atr < 6),
            "position": round((close[-1] - lo) / (hi - lo), 3) if hi > lo else None}
