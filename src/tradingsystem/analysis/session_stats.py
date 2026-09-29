"""Session statistics (Phase 5 B5): what each cash session usually does on this pair, from closed 15m bars.

Sessions are the ones ``context.SESSION_HOURS`` defines (Tokyo 09-18, London 08-17, New York 08-17, each in its own
zone, so their UTC hours move with that zone's DST). For the last 30 UTC days, per session: the mean range of the
session's bars in ATR units (the 15m ATR(14) of the last bar closed before the session started, so the yardstick
never contains the session itself) and the share of sessions that closed above their open. A session enters the mean
only when it is complete (its window ends at or before the UTC day start the statistics are cut at) and at least 85 %
of its bars exist (a holiday or an outage is not a quiet session).

Two parts on purpose: :func:`session_history` is a function of bars before a cut and is cached once per UTC day by the
snapshot builder; :func:`current_sessions` compares the session in progress — CLOSED bars only — with that mean.
Nothing here reads a bar at or after the ``cut`` / ``as_of`` it is given.
"""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import numpy as np

from . import context as ctx
from . import indicators as ind

UTC = dt.timezone.utc
DAY_MS = 86_400_000
MINUTE_MS = 60_000
STATS_DAYS = 30
MIN_COVER = 0.85            # share of a window's bars that must exist for it to count
MAX_ATR_AGE_MS = 4 * DAY_MS  # the ATR yardstick may be this old (a session after a weekend closure)


def local_ms(day: dt.date, hour: int, minute: int, zone: ZoneInfo) -> int:
    """The UTC instant (ms) of a wall-clock time on a local date (zoneinfo: the DST rules of that zone and date)."""
    return int(dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone).timestamp() * 1000)


def session_window(name: str, day: dt.date) -> tuple[int, int]:
    """(start, end) in UTC ms of session ``name`` on its LOCAL date ``day``."""
    tz, a, b = ctx.SESSION_HOURS[name]
    z = ZoneInfo(tz)
    return local_ms(day, a, 0, z), local_ms(day, b, 0, z)


def windows_between(name: str, start_ms: int, end_ms: int) -> list[tuple[int, int]]:
    """Every window of session ``name`` that lies entirely inside [start_ms, end_ms], oldest first."""
    z = ZoneInfo(ctx.SESSION_HOURS[name][0])
    d0 = dt.datetime.fromtimestamp(start_ms / 1000, tz=UTC).astimezone(z).date() - dt.timedelta(days=1)
    d1 = dt.datetime.fromtimestamp(end_ms / 1000, tz=UTC).astimezone(z).date() + dt.timedelta(days=1)
    out = []
    d = d0
    while d <= d1:
        s, e = session_window(name, d)
        if s >= start_ms and e <= end_ms:
            out.append((s, e))
        d += dt.timedelta(days=1)
    return out


def _atr_ref(ot: np.ndarray, atr: np.ndarray, tf_ms: int, start: int) -> float | None:
    """ATR at the last bar that had closed by ``start`` (None when there is none or it is stale / not finite)."""
    j = int(np.searchsorted(ot, start - tf_ms, side="right")) - 1
    if j < 0 or start - int(ot[j]) > MAX_ATR_AGE_MS:
        return None
    a = float(atr[j])
    return a if np.isfinite(a) and a > 0 else None


def window_stats(ot, o, h, lo, c, atr, tf_ms: int, start: int, end: int) -> tuple[float, bool] | None:
    """(range in ATR, closed above its open) of the bars opening in [start, end), or None when the window is not
    covered (< 85 % of its bars) or has no ATR yardstick."""
    i0, i1 = int(np.searchsorted(ot, start)), int(np.searchsorted(ot, end))
    if i1 - i0 < MIN_COVER * ((end - start) // tf_ms):
        return None
    a = _atr_ref(ot, atr, tf_ms, start)
    if a is None:
        return None
    return float(h[i0:i1].max() - lo[i0:i1].min()) / a, bool(c[i1 - 1] > o[i0])


def closed_only(ot, arrays, tf_ms: int, cut: int):
    """The bars that had closed by ``cut`` (open_time + tf <= cut) — the guard every function below starts from."""
    keep = np.asarray(ot, dtype=np.int64) + tf_ms <= cut
    return (np.asarray(ot, dtype=np.int64)[keep], *(np.asarray(a, dtype=float)[keep] for a in arrays))


def session_history(open_time, open_, high, low, close, tf_ms: int, cut: int, days: int = STATS_DAYS) -> dict:
    """{session: [complete sessions used, mean range in ATR, up-close %]} over the ``days`` before ``cut`` (a UTC day
    start), from bars closed by ``cut``. A session with no usable window is left out."""
    ot, o, h, lo, c = closed_only(open_time, (open_, high, low, close), tf_ms, cut)
    if len(ot) < 30:
        return {}
    atr = ind.atr(h, lo, c)
    out: dict = {}
    for name in ctx.SESSION_HOURS:
        rs, ups = [], []
        for s, e in windows_between(name, cut - days * DAY_MS, cut):
            r = window_stats(ot, o, h, lo, c, atr, tf_ms, s, e)
            if r is not None:
                rs.append(r[0])
                ups.append(r[1])
        if rs:
            out[name] = [len(rs), round(float(np.mean(rs)), 2), round(100.0 * sum(ups) / len(ups))]
    return out


def current_sessions(open_time, open_, high, low, close, tf_ms: int, as_of: int, hist: dict) -> dict:
    """{session: [range so far in ATR, that range / the session's mean range, minutes into the session]} for every
    session in progress at ``as_of`` that has at least one closed bar, from CLOSED bars only (the bar still forming is
    never read). The mean comes from ``hist`` (complete sessions before the day); no mean → the ratio is None."""
    ot, o, h, lo, c = closed_only(open_time, (open_, high, low, close), tf_ms, as_of)
    if len(ot) < 30:
        return {}
    atr = ind.atr(h, lo, c)
    out: dict = {}
    for name, (tz, _a, _b) in ctx.SESSION_HOURS.items():
        day = dt.datetime.fromtimestamp(as_of / 1000, tz=UTC).astimezone(ZoneInfo(tz)).date()
        s, e = session_window(name, day)
        if not s <= as_of < e:
            continue
        i0 = int(np.searchsorted(ot, s))
        if i0 >= len(ot):
            continue
        a = _atr_ref(ot, atr, tf_ms, s)
        if a is None:
            continue
        r = float(h[i0:].max() - lo[i0:].min()) / a
        mean = (hist.get(name) or [0, 0.0])[1]
        out[name] = [round(r, 2), round(r / mean, 2) if mean else None, round((as_of - s) / MINUTE_MS)]
    return out
