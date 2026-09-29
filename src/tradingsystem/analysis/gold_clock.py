"""Gold clock and round-number levels (Phase 5 B16, D-049) for a desk pair (XAU).

* the Asia range: high / low / width of 00:00-07:00 UTC of the current UTC day (closed 15m bars), its width against the
  median of the last 30 complete Asia ranges, and ``complete`` once 07:00 UTC has passed;
* ``london_swept``: whether the London window (08:00-12:00 London wall clock of that UTC date) has traded beyond the
  Asia high / low and whether the last closed bar of the window so far closed back inside the Asia range. It reads
  ``none`` until then (and always before the Asia range is complete);
* the next events (London open 08:00 London, LBMA gold auctions 10:30 and 15:00 London, COMEX open 08:20 ET and settle
  13:30 ET, the 08:30 ET US-data slot, NYSE open 09:30 ET, the 17:00 ET rollover) as UTC instants computed from
  each event's own zone with zoneinfo, so the London / New York offset (which differs for a week in March and one in
  autumn: UK clocks change 2026-10-25, US 2026-11-01) is never assumed;
* the MEASURED history (:func:`gold_history`): the share of the last 30 / 90 days on which London took an Asia extreme
  and the last closed bar of its window was back inside the range — computed from the stored bars, no constants;
* round-number levels: the nearest $10 and $50 multiples above and below the price with their distance in ATR.

Every function reads only bars that had closed by the instant it is given.
"""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import numpy as np

from ..core.timeutil import iso
from . import context as ctx
from .session_stats import DAY_MS, MIN_COVER, MINUTE_MS, closed_only, local_ms

UTC = dt.timezone.utc
NY = ZoneInfo("America/New_York")
LDN = ZoneInfo("Europe/London")
ASIA_UTC_HOURS = (0, 7)              # the gold Asia range, UTC
LONDON_WINDOW = (8, 12)              # the London sweep window, London wall clock
WIDTH_DAYS = 30
SWEEP_WINDOWS = (30, 90)
NEXT_EVENTS = 3
# (name, zone, hour, minute): every weekday, each read in its own zone
EVENTS = (("ldn_open", LDN, 8, 0), ("comex_open", NY, 8, 20), ("us_data", NY, 8, 30), ("nyse_open", NY, 9, 30),
          ("lbma_am", LDN, 10, 30), ("comex_settle", NY, 13, 30), ("lbma_pm", LDN, 15, 0), ("rollover", NY, 17, 0))
ROUND_STEPS = (10, 50)


def next_events(as_of: int, n: int = NEXT_EVENTS) -> list[list]:
    """The next ``n`` events after ``as_of``: [name, UTC time, minutes away], soonest first. Weekdays of the event's own
    zone only (no holiday calendar — like the session calendars)."""
    t = dt.datetime.fromtimestamp(as_of / 1000, tz=UTC)
    found = []
    for name, z, h, m in EVENTS:
        local_day = t.astimezone(z).date()
        for back in range(-1, 4):
            d = local_day + dt.timedelta(days=back)
            if d.weekday() >= 5:
                continue
            at = local_ms(d, h, m, z)
            if at > as_of:
                found.append((at, name))
                break
    found.sort()
    return [[name, iso(at), round((at - as_of) / MINUTE_MS)] for at, name in found[:n]]


def asia_window(day: dt.date) -> tuple[int, int]:
    s = int(dt.datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp() * 1000)
    return s + ASIA_UTC_HOURS[0] * 3_600_000, s + ASIA_UTC_HOURS[1] * 3_600_000


def london_window(day: dt.date) -> tuple[int, int]:
    return local_ms(day, LONDON_WINDOW[0], 0, LDN), local_ms(day, LONDON_WINDOW[1], 0, LDN)


def asia_range(ot, high, low, tf_ms: int, day: dt.date, cut: int) -> dict | None:
    """{high, low, width, complete} of the Asia window of UTC date ``day`` from the bars closed by ``cut``. Complete =
    ``cut`` is at or after 07:00 UTC (and then ≥ 85 % of the window's bars must exist, else None); before that the
    range so far (at least one bar)."""
    s, e = asia_window(day)
    i0 = int(np.searchsorted(ot, s))
    i1 = int(np.searchsorted(ot, min(e - 1, cut - tf_ms), side="right"))     # bars in the window, closed by ``cut``
    complete = cut >= e
    if i1 <= i0 or (complete and i1 - i0 < MIN_COVER * ((e - s) // tf_ms)):
        return None
    hi, lo = float(np.max(high[i0:i1])), float(np.min(low[i0:i1]))
    return {"high": hi, "low": lo, "width": hi - lo, "complete": complete}


def london_sweep(ot, high, low, close, tf_ms: int, rng: dict, day: dt.date, cut: int) -> tuple[str | None, bool | None] | None:
    """(side, back_inside) of London's window against a COMPLETE Asia range: side ``high`` / ``low`` / ``both`` when a
    closed bar traded beyond that extreme, back_inside = the last closed bar of the window so far closed inside the
    range. (None, None) = no sweep yet; None = no closed London bar yet / the Asia range is not complete."""
    if not rng or not rng["complete"]:
        return None
    s, e = london_window(day)
    i0, i1 = int(np.searchsorted(ot, s)), int(np.searchsorted(ot, min(e - 1, cut - tf_ms), side="right"))
    if i1 <= i0:
        return None
    up = bool(np.max(high[i0:i1]) > rng["high"])
    dn = bool(np.min(low[i0:i1]) < rng["low"])
    if not (up or dn):
        return None, None
    return ("both" if up and dn else "high" if up else "low"), bool(rng["low"] <= close[i1 - 1] <= rng["high"])


def gold_history(open_time, open_, high, low, close, tf_ms: int, cut: int) -> dict:
    """The measured gold history before the UTC day start ``cut``: ``asia_width_median`` and ``asia_days`` over the last
    30 complete Asia ranges, and ``sweep`` = {"30": [days, swept %, swept-and-back-inside %], "90": [...]} over the
    days with a complete Asia range and a covered London window (percentages of those days)."""
    ot, _o, h, lo, c = closed_only(open_time, (open_, high, low, close), tf_ms, cut)
    if not len(ot):
        return {}
    days = []                                       # (date, width, sweep result | 'nodata')
    day0 = dt.datetime.fromtimestamp(cut / 1000, tz=UTC).date()
    for back in range(1, max(SWEEP_WINDOWS) + 1):
        d = day0 - dt.timedelta(days=back)
        rng = asia_range(ot, h, lo, tf_ms, d, cut)
        if rng is None:
            continue
        s, e = london_window(d)
        i0, i1 = int(np.searchsorted(ot, s)), int(np.searchsorted(ot, e))
        if i1 - i0 < MIN_COVER * ((e - s) // tf_ms):
            days.append((back, rng["width"], None))
            continue
        sw = london_sweep(ot, h, lo, c, tf_ms, rng, d, cut)
        days.append((back, rng["width"], sw if sw is not None else (None, None)))
    out: dict = {}
    widths = [w for b, w, _ in days if b <= WIDTH_DAYS]
    if widths:
        out["asia_width_median"] = float(np.median(widths))
        out["asia_days"] = len(widths)
    sweep = {}
    for n in SWEEP_WINDOWS:
        rows = [sw for b, _w, sw in days if b <= n and sw is not None]
        if rows:
            swept = sum(1 for side, _ in rows if side)
            back = sum(1 for side, inside in rows if side and inside)
            sweep[str(n)] = [len(rows), round(100.0 * swept / len(rows)), round(100.0 * back / len(rows))]
    if sweep:
        out["sweep"] = sweep
    return out


def clock_block(as_of: int, open_time, high, low, close, tf_ms: int, hist: dict, desk_window: str, d: int) -> dict:
    """``market.gold_clock`` at ``as_of`` from the 15m frame's closed bars and the day's cached ``hist``
    (:func:`gold_history`)."""
    ot, _o, h, lo, c = closed_only(open_time, (open_time, high, low, close), tf_ms, as_of)
    active = ctx.sessions(as_of)["active"]
    out: dict = {"session": "+".join(active) if active else "none"}
    day = dt.datetime.fromtimestamp(as_of / 1000, tz=UTC).date()
    rng = asia_range(ot, h, lo, tf_ms, day, as_of) if len(ot) else None
    if rng is not None:
        block = {"high": round(rng["high"], d), "low": round(rng["low"], d), "width": round(rng["width"], d),
                 "complete": rng["complete"]}
        med = hist.get("asia_width_median")
        if rng["complete"] and med:
            block["width_x_median"] = round(rng["width"] / med, 2)
        out["asia_range"] = block
        sw = london_sweep(ot, h, lo, c, tf_ms, rng, day, as_of)
        out["london_swept"] = ("none" if sw is None or sw[0] is None
                               else {"side": sw[0], "back_inside": sw[1]})
    if hist.get("sweep"):
        out["london_asia_sweep_days"] = hist["sweep"]
    out["next"] = next_events(as_of)
    out["desk_window"] = desk_window
    return out


def round_levels(price: float, atr: float | None, d: int) -> dict:
    """{step: [level below, ATR distance, level above, ATR distance]} for the $10 and $50 multiples strictly around
    ``price`` (distance None without an ATR)."""
    out = {}
    for step in ROUND_STEPS:
        below = np.ceil(price / step) * step - step
        above = np.floor(price / step) * step + step
        out[str(step)] = [round(float(below), d), round((price - below) / atr, 2) if atr else None,
                          round(float(above), d), round((above - price) / atr, 2) if atr else None]
    return out
