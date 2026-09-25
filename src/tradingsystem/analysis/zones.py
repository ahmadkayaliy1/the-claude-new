"""Fair value gaps and order blocks with mitigation state (P6.5) — causal.

FVG (3-candle imbalance): bullish when low[i] > high[i-2], bearish when high[i] < low[i-2]; known at close of i.
Order block: the last opposite-colour candle of the leg that produced a structure break (known at the break
close). Mitigation = price trading back into the zone; a close through the far side invalidates it (breaker).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .structure import StructureState

F = np.ndarray


@dataclass
class Zone:
    kind: str                 # "fvg" | "order_block"
    direction: str            # "bullish" | "bearish"
    top: float
    bottom: float
    origin_idx: int           # candle(s) forming the zone
    known_idx: int            # bar at which the zone is known
    fill: float = 0.0         # 0 = untouched, 1 = fully traded through
    first_touch_idx: int | None = None
    invalidated_idx: int | None = None
    strength: float = 0.0     # size (FVG) or displacement (OB) in ATR units

    @property
    def mid(self) -> float:
        return (self.top + self.bottom) / 2

    def active(self) -> bool:
        return self.invalidated_idx is None and self.fill < 1.0


def fair_value_gaps(high: F, low: F, close: F, atr_values: F, *, min_atr: float = 0.1) -> list[Zone]:
    zones: list[Zone] = []
    for i in range(2, len(close)):
        a = atr_values[i] if not np.isnan(atr_values[i]) else np.nan
        if low[i] > high[i - 2]:
            size = low[i] - high[i - 2]
            if np.isnan(a) or size >= min_atr * a:
                zones.append(Zone("fvg", "bullish", float(low[i]), float(high[i - 2]), i - 1, i,
                                  strength=float(size / a) if a and not np.isnan(a) else 0.0))
        elif high[i] < low[i - 2]:
            size = low[i - 2] - high[i]
            if np.isnan(a) or size >= min_atr * a:
                zones.append(Zone("fvg", "bearish", float(low[i - 2]), float(high[i]), i - 1, i,
                                  strength=float(size / a) if a and not np.isnan(a) else 0.0))
    return zones


def order_blocks(open_: F, high: F, low: F, close: F, atr_values: F, st: StructureState) -> list[Zone]:
    zones: list[Zone] = []
    for ev in st.events:
        if ev.kind not in ("BOS", "CHoCH"):
            continue
        t = ev.idx
        start = ev.pivot_idx
        if ev.direction == "bullish":
            # leg origin = lowest low between the broken swing high and the break; OB = last down candle ≤ origin
            seg = range(start, t + 1)
            origin = min(seg, key=lambda k: low[k])
            ob = next((k for k in range(origin, max(start - 20, -1), -1) if close[k] < open_[k]), None)
            if ob is None:
                continue
            disp = (close[t] - low[origin]) / atr_values[t] if atr_values[t] and not np.isnan(atr_values[t]) else 0.0
            zones.append(Zone("order_block", "bullish", float(high[ob]), float(low[ob]), ob, t, strength=float(disp)))
        else:
            seg = range(start, t + 1)
            origin = max(seg, key=lambda k: high[k])
            ob = next((k for k in range(origin, max(start - 20, -1), -1) if close[k] > open_[k]), None)
            if ob is None:
                continue
            disp = (high[origin] - close[t]) / atr_values[t] if atr_values[t] and not np.isnan(atr_values[t]) else 0.0
            zones.append(Zone("order_block", "bearish", float(high[ob]), float(low[ob]), ob, t, strength=float(disp)))
    return zones


def update_mitigation(zones: list[Zone], high: F, low: F, close: F, upto: int | None = None) -> list[Zone]:
    """Fill/touch/invalidation state as of bar ``upto`` (inclusive; default = last bar)."""
    end = len(close) - 1 if upto is None else upto
    for z in zones:
        z.fill, z.first_touch_idx, z.invalidated_idx = 0.0, None, None
        h = z.top - z.bottom
        for j in range(z.known_idx + 1, end + 1):
            if z.direction == "bullish":
                if low[j] <= z.top:
                    z.first_touch_idx = z.first_touch_idx if z.first_touch_idx is not None else j
                    z.fill = max(z.fill, min(1.0, (z.top - low[j]) / h if h > 0 else 1.0))
                if close[j] < z.bottom:
                    z.invalidated_idx = j
                    break
            else:
                if high[j] >= z.bottom:
                    z.first_touch_idx = z.first_touch_idx if z.first_touch_idx is not None else j
                    z.fill = max(z.fill, min(1.0, (high[j] - z.bottom) / h if h > 0 else 1.0))
                if close[j] > z.top:
                    z.invalidated_idx = j
                    break
    return zones


def nearest_active(zones: list[Zone], price: float, k: int = 3) -> list[Zone]:
    act = [z for z in zones if z.active()]
    return sorted(act, key=lambda z: min(abs(price - z.top), abs(price - z.bottom)))[:k]
