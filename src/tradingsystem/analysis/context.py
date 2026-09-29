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


# cash sessions: name -> (zone, local start hour, local end hour). Each is read in its own zone, so DST moves the UTC
# hours; the session statistics (B5) use the same table
SESSION_HOURS = {"asia": ("Asia/Tokyo", 9, 18), "london": ("Europe/London", 8, 17),
                 "new_york": ("America/New_York", 8, 17)}


def sessions(utc_ms: int) -> dict:
    """Active cash sessions and ICT killzones at ``utc_ms`` (each in its own local time → DST-correct)."""
    t = dt.datetime.fromtimestamp(utc_ms / 1000, tz=UTC)
    ny = t.astimezone(NY)
    hm = lambda x: x.hour + x.minute / 60  # noqa: E731
    active = [name for name, (tz, a, b) in SESSION_HOURS.items() if a <= hm(t.astimezone(ZoneInfo(tz))) < b]
    k = hm(ny)
    killzone = ("asia" if k >= 20 else "london_open" if 2 <= k < 5 else "ny_open" if 7 <= k < 10
                else "london_close" if 10 <= k < 12 else None)
    return {"active": active, "killzone": killzone, "ny_time": ny.strftime("%a %H:%M"),
            "weekday_utc": t.strftime("%a")}


def day_key(ms: int, day_roll: str = "utc") -> dt.date:
    """The trading day an instant belongs to: the UTC date, or ("ny17", FX/metals) the date after 17:00 New York."""
    t = dt.datetime.fromtimestamp(ms / 1000, tz=UTC)
    if day_roll == "ny17":
        return (t.astimezone(NY) + dt.timedelta(hours=7)).date()        # 17:00 NY belongs to the next trading day
    return t.date()


PERIOD_SLACK_DAYS = 5      # the first stored daily bar may lie this far after a month / year start (weekend, holiday)
WEEK_SLACK_DAYS = 1        # ... after a week's start (Monday key: the metals' Sunday-evening open is already Monday's)


def period_levels(open_time: F, open_: F, high: F, low: F, as_of: int, *, day_roll: str = "utc",
                  day_open: float | None = None) -> dict:
    """Previous week / month highs and lows and the month / year open from DAILY bars (Phase 5 B2).

    Only completed periods: the previous week is the last full Monday–Sunday week of the trading-day key
    (``day_roll``), the previous month the last full calendar month of that key; ``month_open`` / ``year_open`` are the
    open of the first daily bar of the current month / year. Bars stamped at or after ``as_of`` are ignored (causal).
    A level is left out when the bars do not reach back to its period's start (a short history is not a level).
    ``day_open`` (today's open, known from the first bar of the day) stands in for the month / year open on the first
    trading day of that period, when its daily bar has not closed yet."""
    keep = np.asarray(open_time, dtype=np.int64) < as_of
    if not keep.any():
        return {}
    keys = [day_key(int(x), day_roll) for x in np.asarray(open_time)[keep]]
    o, h, lo = (np.asarray(x, dtype=float)[keep] for x in (open_, high, low))
    today = day_key(as_of, day_roll)
    first = keys[0]

    def reaches(start: dt.date, slack: int = PERIOD_SLACK_DAYS) -> bool:
        return first <= start + dt.timedelta(days=slack)

    def pick(lo_key: dt.date, hi_key: dt.date) -> np.ndarray:            # bars with lo_key <= key < hi_key
        return np.array([lo_key <= k < hi_key for k in keys], dtype=bool)

    out: dict = {}
    monday = today - dt.timedelta(days=today.weekday())
    pw = pick(monday - dt.timedelta(days=7), monday)
    if pw.any() and reaches(monday - dt.timedelta(days=7), WEEK_SLACK_DAYS):
        out["pwh"], out["pwl"] = float(h[pw].max()), float(lo[pw].min())
    m0 = today.replace(day=1)
    pm0 = (m0 - dt.timedelta(days=1)).replace(day=1)
    pm = pick(pm0, m0)
    if pm.any() and reaches(pm0):
        out["pmh"], out["pml"] = float(h[pm].max()), float(lo[pm].min())
    cm = pick(m0, dt.date.max)
    if reaches(m0):
        if cm.any():
            out["month_open"] = float(o[cm][0])
        elif day_open is not None and (today - m0).days <= PERIOD_SLACK_DAYS:
            out["month_open"] = day_open
    y0 = today.replace(month=1, day=1)
    cy = pick(y0, dt.date.max)
    if reaches(y0):
        if cy.any():
            out["year_open"] = float(o[cy][0])
        elif day_open is not None and (today - y0).days <= PERIOD_SLACK_DAYS:
            out["year_open"] = day_open
    return out


def reference_levels(open_time: F, high: F, low: F, close: F, open_: F, as_of: int, *, day_roll: str = "utc",
                     daily: tuple[F, F, F, F] | None = None) -> dict:
    """Previous day high/low/close, current day/week open and the Asia-session range, from closed M1/M5 bars.

    ``day_roll``: "utc" (crypto) or "ny17" (FX/metals trading day rolls at 17:00 New York). ``daily`` =
    (open_time, open, high, low) of the closed daily bars adds the previous week / month levels and the month / year
    open (``period_levels``).
    """
    if len(close) == 0:
        return {}
    keys = np.array([day_key(int(x), day_roll) for x in open_time])
    today = day_key(as_of, day_roll)
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
    if daily is not None:
        out.update(period_levels(*daily, as_of, day_roll=day_roll, day_open=out.get("day_open")))
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
