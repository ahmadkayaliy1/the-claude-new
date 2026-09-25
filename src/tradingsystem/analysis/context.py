"""Market context (P6.11): trading sessions & killzones (DST-aware), reference levels, regime, MTF confluence."""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import numpy as np

from . import indicators as ind

UTC = dt.timezone.utc
NY = ZoneInfo("America/New_York")
LDN = ZoneInfo("Europe/London")
TYO = ZoneInfo("Asia/Tokyo")

F = np.ndarray


def sessions(utc_ms: int) -> dict:
    """Active cash sessions and ICT killzones at ``utc_ms`` (each in its own local time → DST-correct)."""
    t = dt.datetime.fromtimestamp(utc_ms / 1000, tz=UTC)
    ny, ldn, tyo = t.astimezone(NY), t.astimezone(LDN), t.astimezone(TYO)
    hm = lambda x: x.hour + x.minute / 60  # noqa: E731
    active = []
    if 9 <= hm(tyo) < 18:
        active.append("asia")
    if 8 <= hm(ldn) < 17:
        active.append("london")
    if 8 <= hm(ny) < 17:
        active.append("new_york")
    k = hm(ny)
    killzone = ("asia" if k >= 20 else "london_open" if 2 <= k < 5 else "ny_open" if 7 <= k < 10
                else "london_close" if 10 <= k < 12 else None)
    return {"active": active, "killzone": killzone, "ny_time": ny.strftime("%a %H:%M"),
            "weekday_utc": t.strftime("%a")}


def reference_levels(open_time: F, high: F, low: F, close: F, open_: F, as_of: int, *, day_roll: str = "utc") -> dict:
    """Previous day high/low/close, current day/week open and the Asia-session range, from closed M1/M5 bars.

    ``day_roll``: "utc" (crypto) or "ny17" (FX/metals trading day rolls at 17:00 New York).
    """
    if len(close) == 0:
        return {}

    def day_key(ms: int) -> dt.date:
        t = dt.datetime.fromtimestamp(ms / 1000, tz=UTC)
        if day_roll == "ny17":
            n = t.astimezone(NY)
            return (n + dt.timedelta(hours=7)).date()        # 17:00 NY belongs to the next trading day
        return t.date()

    keys = np.array([day_key(int(x)) for x in open_time])
    today = day_key(as_of)
    out: dict = {}
    prev_days = sorted({k for k in keys if k < today})
    if prev_days:
        m = keys == prev_days[-1]
        out.update(pdh=float(high[m].max()), pdl=float(low[m].min()), pdc=float(close[m][-1]))
    m = keys == today
    if m.any():
        out["day_open"] = float(open_[m][0])
    week = dt.date.fromordinal(today.toordinal() - today.weekday())
    wm = keys >= week
    if wm.any():
        out["week_open"] = float(open_[wm][0])
    # Asia range of the current trading day (Tokyo 09:00–15:00)
    asia = []
    for i in np.nonzero(m)[0]:
        tt = dt.datetime.fromtimestamp(int(open_time[i]) / 1000, tz=UTC).astimezone(TYO)
        if 9 <= tt.hour < 15:
            asia.append(i)
    if asia:
        out["asia_high"], out["asia_low"] = float(high[asia].max()), float(low[asia].min())
    return out


def regime(high: F, low: F, close: F) -> dict:
    a, pdi, mdi = ind.adx(high, low, close)
    atr = ind.atr(high, low, close)
    pct = ind.percentile_rank(atr, min(100, len(atr)))
    last = lambda x: None if len(x) == 0 or np.isnan(x[-1]) else round(float(x[-1]), 2)  # noqa: E731
    adx_v = last(a)
    trend = None if adx_v is None else ("strong_trend" if adx_v >= 30 else "trend" if adx_v >= 22 else "range")
    vol_p = last(pct)
    vol = None if vol_p is None else ("high" if vol_p >= 0.8 else "low" if vol_p <= 0.2 else "normal")
    direction = None
    if not np.isnan(pdi[-1]) and not np.isnan(mdi[-1]):
        direction = "up" if pdi[-1] > mdi[-1] else "down"
    return {"adx": adx_v, "trend_strength": trend, "di_direction": direction, "atr_percentile": vol_p,
            "volatility": vol}


def ema_stack(close: F) -> dict:
    e20, e50, e200 = ind.ema(close, 20), ind.ema(close, 50), ind.ema(close, 200)
    v = lambda x: None if np.isnan(x[-1]) else float(x[-1])  # noqa: E731
    a, b, c = v(e20), v(e50), v(e200)
    align = None
    if None not in (a, b, c):
        align = "bullish" if a > b > c else "bearish" if a < b < c else "mixed"
    return {"ema20": a, "ema50": b, "ema200": c, "alignment": align}


def confluence(per_tf: dict[str, dict]) -> dict:
    """Weighted agreement of structure trend + EMA alignment across timeframes → score in [-1, 1]. Timeframes with
    neither reading (too little history) are left out and listed in ``missing`` instead of counting as neutral."""
    weights = {"1w": 3, "1d": 3, "4h": 2.5, "1h": 2, "15m": 1.5, "5m": 1, "1m": 0.5}
    score = total = 0.0
    missing = []
    for tf, d in per_tf.items():
        if d.get("trend") is None and d.get("ema_alignment") is None:
            missing.append(tf)
            continue
        w = weights.get(tf, 1)
        s = {"bullish": 1, "bearish": -1}.get(d.get("trend"), 0) * 0.6
        s += {"bullish": 1, "bearish": -1}.get(d.get("ema_alignment"), 0) * 0.4
        score += w * s
        total += w
    val = round(score / total, 3) if total else 0.0
    return {"score": val, "bias": "bullish" if val > 0.25 else "bearish" if val < -0.25 else "mixed",
            "missing": missing}
