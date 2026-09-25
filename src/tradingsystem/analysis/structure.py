"""Market structure & liquidity (P6.3/P6.4) — causal by construction.

* Swing pivots use L bars left / R bars right; a pivot at bar i is only *known* at bar i+R (``confirm_idx``).
* Structure breaks are decided on candle **closes** beyond the last confirmed swing (wick-only excursions are
  recorded as liquidity *sweeps*, not breaks). BOS = break in the trend direction, CHoCH = first break against it.
* Equal highs/lows (within tol × ATR) that remain unbroken are liquidity pools.
Every event carries the index/time at which it became known, so downstream code can never use it earlier.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

F = np.ndarray


@dataclass(frozen=True)
class Pivot:
    idx: int            # bar of the extreme
    confirm_idx: int    # first bar at which the pivot is known
    price: float
    kind: str           # "high" | "low"


@dataclass(frozen=True)
class StructureEvent:
    idx: int            # bar whose close produced the event (known at that close)
    kind: str           # "BOS" | "CHoCH" | "sweep"
    direction: str      # "bullish" | "bearish"
    level: float        # the swing level broken / swept
    pivot_idx: int


@dataclass
class LiquidityPool:
    side: str           # "buy_side" (equal highs) | "sell_side" (equal lows)
    level: float
    touches: list[int]
    known_idx: int
    swept_idx: int | None = None


@dataclass
class StructureState:
    trend: str = "none"                     # "bullish" | "bearish" | "none"
    pivots: list[Pivot] = field(default_factory=list)
    events: list[StructureEvent] = field(default_factory=list)
    pools: list[LiquidityPool] = field(default_factory=list)
    last_high: Pivot | None = None
    last_low: Pivot | None = None
    range_high: Pivot | None = None         # dealing range (most recent confirmed swing high/low)
    range_low: Pivot | None = None


def pivots(high: F, low: F, left: int = 3, right: int = 3) -> list[Pivot]:
    out: list[Pivot] = []
    n = len(high)
    for i in range(left, n - right):
        h, l = high[i], low[i]
        if h > high[i - left:i].max() and h >= high[i + 1:i + right + 1].max():
            out.append(Pivot(i, i + right, float(h), "high"))
        if l < low[i - left:i].min() and l <= low[i + 1:i + right + 1].min():
            out.append(Pivot(i, i + right, float(l), "low"))
    out.sort(key=lambda p: (p.confirm_idx, p.idx))
    return out


def analyze_structure(high: F, low: F, close: F, atr_values: F, *, left: int = 3, right: int = 3,
                      eq_tol_atr: float = 0.1) -> StructureState:
    """Walk forward bar by bar; state at the end equals what a live system would have known."""
    st = StructureState()
    piv = pivots(high, low, left, right)
    by_confirm: dict[int, list[Pivot]] = {}
    for p in piv:
        by_confirm.setdefault(p.confirm_idx, []).append(p)
    broken_high: set[int] = set()
    broken_low: set[int] = set()
    for t in range(len(close)):
        # 1) pivots confirmed at this bar become known
        for p in by_confirm.get(t, []):
            st.pivots.append(p)
            tol = eq_tol_atr * (atr_values[t] if not np.isnan(atr_values[t]) else 0.0)
            if p.kind == "high":
                st.last_high = st.range_high = p
                _pool(st, "buy_side", p, t, tol)
            else:
                st.last_low = st.range_low = p
                _pool(st, "sell_side", p, t, tol)
        # 2) breaks (close beyond) and sweeps (wick beyond, close back inside)
        lh, ll = st.last_high, st.last_low
        if lh is not None and lh.idx not in broken_high and t > lh.confirm_idx - 1 and t > lh.idx:
            if close[t] > lh.price:
                kind = "CHoCH" if st.trend == "bearish" else "BOS"
                st.events.append(StructureEvent(t, kind, "bullish", lh.price, lh.idx))
                st.trend = "bullish"
                broken_high.add(lh.idx)
            elif high[t] > lh.price:
                st.events.append(StructureEvent(t, "sweep", "bearish", lh.price, lh.idx))
        if ll is not None and ll.idx not in broken_low and t > ll.idx:
            if close[t] < ll.price:
                kind = "CHoCH" if st.trend == "bullish" else "BOS"
                st.events.append(StructureEvent(t, kind, "bearish", ll.price, ll.idx))
                st.trend = "bearish"
                broken_low.add(ll.idx)
            elif low[t] < ll.price:
                st.events.append(StructureEvent(t, "sweep", "bullish", ll.price, ll.idx))
        # 3) liquidity pools swept
        for pool in st.pools:
            if pool.swept_idx is None and t > pool.known_idx:
                if (pool.side == "buy_side" and high[t] > pool.level) or (pool.side == "sell_side" and low[t] < pool.level):
                    pool.swept_idx = t
    return st


def _pool(st: StructureState, side: str, p: Pivot, t: int, tol: float) -> None:
    """Merge the new pivot into an unswept pool at (almost) the same price, or seed a candidate pool."""
    for pool in st.pools:
        if pool.side == side and pool.swept_idx is None and abs(pool.level - p.price) <= tol:
            pool.touches.append(p.idx)
            pool.level = max(pool.level, p.price) if side == "buy_side" else min(pool.level, p.price)
            pool.known_idx = t
            return
    st.pools.append(LiquidityPool(side, p.price, [p.idx], t))


def premium_discount(st: StructureState, price: float, high: F | None = None, low: F | None = None) -> dict | None:
    """Position of ``price`` inside the current dealing range (0 = range low, 1 = range high).

    The range is spanned by the latest confirmed swing high and low and extended to any extreme printed
    since (after a break the leg's running extreme is the new range edge), so position stays in [0, 1].
    """
    if st.range_high is None or st.range_low is None:
        return None
    hi, lo = st.range_high.price, st.range_low.price
    if high is not None and low is not None:
        start = min(st.range_high.idx, st.range_low.idx)
        hi, lo = max(hi, float(high[start:].max())), min(lo, float(low[start:].min()))
    hi, lo = max(hi, price), min(lo, price)
    if hi <= lo:
        return None
    pos = (price - lo) / (hi - lo)
    zone = "premium" if pos > 0.5 else "discount"
    ote = (0.62 <= 1 - pos <= 0.79) if st.trend == "bullish" else (0.62 <= pos <= 0.79)
    return {"range_high": hi, "range_low": lo, "equilibrium": (hi + lo) / 2, "position": round(pos, 3),
            "zone": zone, "in_ote": bool(ote)}


def equal_level_pools(st: StructureState, min_touches: int = 2) -> list[LiquidityPool]:
    return [p for p in st.pools if len(p.touches) >= min_touches]
