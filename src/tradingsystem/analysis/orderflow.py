"""Order flow (P6.7–P6.9): bar delta / CVD, footprint from aggTrades, volume / TPO profiles.

Data-quality contract (spec §3.2):
* Binance klines carry the exact aggressive-buy volume → bar delta and CVD are **real**.
* Footprint needs real trades → **real** for Binance instruments (aggTrades), unavailable for MT5 CFDs.
* Gold: TPO (time-at-price from real M1 bars) is **real**; a tick-volume profile is **approx** and must be
  labelled so wherever it is shown.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

F = np.ndarray


# --------------------------------------------------------------------------- delta / CVD
def bar_delta(volume: F, taker_buy: F) -> F:
    """Aggressive buy − aggressive sell volume per bar (exact from Binance klines)."""
    return 2.0 * taker_buy - volume


def cvd(delta: F, anchor: int = 0) -> F:
    out = np.full(len(delta), np.nan)
    out[anchor:] = np.cumsum(delta[anchor:])
    return out


def delta_divergence(price_pivots: list[tuple[int, float, str]], cvd_values: F) -> dict | None:
    """Compare the last two confirmed swing highs (or lows) of price with CVD at the same bars.

    ``price_pivots``: [(idx, price, kind)] of confirmed pivots, chronological.
    Bearish divergence: price higher high, CVD lower high. Bullish: price lower low, CVD higher low.
    """
    out = None
    for kind, word in (("high", "bearish"), ("low", "bullish")):
        ps = [p for p in price_pivots if p[2] == kind][-2:]
        if len(ps) < 2 or np.isnan(cvd_values[ps[0][0]]) or np.isnan(cvd_values[ps[1][0]]):
            continue
        (i0, p0, _), (i1, p1, _) = ps
        c0, c1 = cvd_values[i0], cvd_values[i1]
        if kind == "high" and p1 > p0 and c1 < c0:
            out = {"type": word, "price_pivots": [i0, i1], "price": [p0, p1], "cvd": [float(c0), float(c1)]}
        if kind == "low" and p1 < p0 and c1 > c0:
            cand = {"type": word, "price_pivots": [i0, i1], "price": [p0, p1], "cvd": [float(c0), float(c1)]}
            out = cand if out is None or i1 > out["price_pivots"][1] else out
    return out


# --------------------------------------------------------------------------- footprint
@dataclass
class FootprintBar:
    open_time: int
    levels: F            # bucket lower edges (ascending)
    buy: F               # aggressive buy volume per level
    sell: F              # aggressive sell volume per level

    @property
    def volume(self) -> float:
        return float(self.buy.sum() + self.sell.sum())

    @property
    def delta(self) -> float:
        return float(self.buy.sum() - self.sell.sum())

    @property
    def poc(self) -> float:
        return float(self.levels[np.argmax(self.buy + self.sell)])


def footprint(ts: F, price: F, qty: F, is_buyer_maker: F, bar_ms: int, bucket: float) -> list[FootprintBar]:
    """Volume at price per bar from aggTrades (``is_buyer_maker`` = 1 → aggressive SELL)."""
    if len(ts) == 0:
        return []
    bars = ts // bar_ms * bar_ms
    lvl = np.floor(price / bucket) * bucket
    out: list[FootprintBar] = []
    for b in np.unique(bars):
        m = bars == b
        levels, inv = np.unique(lvl[m], return_inverse=True)
        buy = np.bincount(inv, weights=np.where(is_buyer_maker[m] == 0, qty[m], 0.0), minlength=len(levels))
        sell = np.bincount(inv, weights=np.where(is_buyer_maker[m] == 1, qty[m], 0.0), minlength=len(levels))
        out.append(FootprintBar(int(b), levels, buy, sell))
    return out


def merge_bars(bars: list[FootprintBar], bar_ms: int) -> list[FootprintBar]:
    """Aggregate footprints to a higher timeframe (e.g. 1m → 15m)."""
    groups: dict[int, list[FootprintBar]] = {}
    for fb in bars:
        groups.setdefault(fb.open_time // bar_ms * bar_ms, []).append(fb)
    out = []
    for t, fbs in sorted(groups.items()):
        levels = np.unique(np.concatenate([f.levels for f in fbs]))
        buy, sell = np.zeros(len(levels)), np.zeros(len(levels))
        for f in fbs:
            idx = np.searchsorted(levels, f.levels)
            np.add.at(buy, idx, f.buy)
            np.add.at(sell, idx, f.sell)
        out.append(FootprintBar(t, levels, buy, sell))
    return out


def imbalances(fb: FootprintBar, ratio: float = 3.0, min_stack: int = 3) -> dict:
    """Diagonal imbalances (buy at p vs sell at p−1 level) and stacked runs."""
    b, s = fb.buy, fb.sell
    n = len(b)
    buy_imb = np.zeros(n, dtype=bool)
    sell_imb = np.zeros(n, dtype=bool)
    for i in range(1, n):
        if b[i] >= ratio * max(s[i - 1], 1e-12) and b[i] > 0:
            buy_imb[i] = True
    for i in range(n - 1):
        if s[i] >= ratio * max(b[i + 1], 1e-12) and s[i] > 0:
            sell_imb[i] = True

    def stacks(mask: F) -> list[tuple[float, float]]:
        runs, start = [], None
        for i, v in enumerate(list(mask) + [False]):
            if v and start is None:
                start = i
            elif not v and start is not None:
                if i - start >= min_stack:
                    runs.append((float(fb.levels[start]), float(fb.levels[i - 1])))
                start = None
        return runs
    return {"buy_imbalances": int(buy_imb.sum()), "sell_imbalances": int(sell_imb.sum()),
            "stacked_buy": stacks(buy_imb), "stacked_sell": stacks(sell_imb)}


def absorption(fb: FootprintBar, bar_open: float, bar_close: float, share: float = 0.3) -> str | None:
    """Heavy aggressive volume at the bar's extreme that failed to move price (a classic absorption print)."""
    if len(fb.levels) < 3 or fb.volume <= 0:
        return None
    top = fb.levels >= np.quantile(fb.levels, 0.8)
    bot = fb.levels <= np.quantile(fb.levels, 0.2)
    if fb.buy[top].sum() >= share * fb.volume and bar_close <= bar_open:
        return "buy_absorbed_at_high"       # buyers hit the offer at the high, price closed down
    if fb.sell[bot].sum() >= share * fb.volume and bar_close >= bar_open:
        return "sell_absorbed_at_low"
    return None


# --------------------------------------------------------------------------- profiles
def value_area(levels: F, vol: F, pct: float = 0.70) -> dict | None:
    if len(levels) == 0 or vol.sum() <= 0:
        return None
    order = np.argsort(levels)
    levels, vol = levels[order], vol[order]
    poc_i = int(np.argmax(vol))
    lo = hi = poc_i
    total, acc = vol.sum(), vol[poc_i]
    while acc < pct * total and (lo > 0 or hi < len(vol) - 1):
        up = vol[hi + 1] if hi < len(vol) - 1 else -1
        dn = vol[lo - 1] if lo > 0 else -1
        if up >= dn:
            hi += 1
            acc += vol[hi]
        else:
            lo -= 1
            acc += vol[lo]
    return {"poc": float(levels[poc_i]), "vah": float(levels[hi]), "val": float(levels[lo]),
            "coverage": round(float(acc / total), 3)}


def profile_from_footprint(bars: list[FootprintBar]) -> dict | None:
    if not bars:
        return None
    m = merge_bars(bars, 10**15)[0]
    return value_area(m.levels, m.buy + m.sell)


def tpo_profile(high: F, low: F, bucket: float) -> dict | None:
    """Time-at-price: each bar adds one unit to every bucket it traded through (real, needs no volume)."""
    if len(high) == 0:
        return None
    lo_all, hi_all = np.floor(low.min() / bucket) * bucket, np.floor(high.max() / bucket) * bucket
    levels = np.arange(lo_all, hi_all + bucket / 2, bucket)
    counts = np.zeros(len(levels))
    for h, l in zip(high, low):
        a = int(round((np.floor(l / bucket) * bucket - lo_all) / bucket))
        b = int(round((np.floor(h / bucket) * bucket - lo_all) / bucket))
        counts[a:b + 1] += 1
    return value_area(levels, counts)


def tick_volume_profile(high: F, low: F, tick_volume: F, bucket: float) -> dict | None:
    """APPROXIMATE: spreads each bar's tick count evenly over its range (not traded volume)."""
    if len(high) == 0:
        return None
    lo_all = np.floor(low.min() / bucket) * bucket
    levels = np.arange(lo_all, np.floor(high.max() / bucket) * bucket + bucket / 2, bucket)
    vol = np.zeros(len(levels))
    for h, l, v in zip(high, low, tick_volume):
        a = int(round((np.floor(l / bucket) * bucket - lo_all) / bucket))
        b = int(round((np.floor(h / bucket) * bucket - lo_all) / bucket))
        vol[a:b + 1] += v / (b - a + 1)
    out = value_area(levels, vol)
    if out:
        out["data_quality"] = "approx"
    return out
