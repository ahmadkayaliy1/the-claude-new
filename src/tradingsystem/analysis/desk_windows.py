"""Gold desk call windows (Phase 5 B15, D-049): when a desk pair may be asked for an ENTRY idea.

Pure functions of the desk config, an instant and the execution venue's session calendar — the engine uses them to
suppress entry calls, ``market.gold_clock.desk_window`` (B16) reports the same state to the model.

Closed reasons (in the order they are tested): ``market_closed`` (the calendar), ``reopen_grace`` (within
``desk.reopen_grace_min`` minutes of the weekly reopen — the calendar's closed → open edge after a break longer than
the daily one, never a hardcoded clock time), ``friday_cutoff`` (Friday from ``desk.friday_cutoff`` in its zone),
``outside_window`` (in none of ``desk.windows``, each read in its own zone so DST moves the UTC hours), and inside a
window ``news_blackout`` / ``news_stale`` (B8: the caller passes :func:`desk_news_reason` of the stored news
calendar). Open = ``(True, "<window name>")``.
"""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from ..core.sessions import SessionCalendar
from ..core.settings import DeskCfg
from ..core.timeutil import MS_PER_HOUR, MS_PER_MINUTE

UTC = dt.timezone.utc
WEEKLY_BREAK_MS = 2 * MS_PER_HOUR          # a closed stretch this long before a reopen = the weekly one, not the daily break


def desk_news_reason(news_state) -> str | None:
    """B8 for a desk (D-049): ``news_blackout`` inside a release's window, ``news_stale`` while the calendar is not fresh
    (no money is at stake in shadow, so the desk fails closed on calls), else None. ``news_state`` is an
    :class:`.news.NewsState` or None (the pair has no blackout configured)."""
    if news_state is None:
        return None
    if not news_state.fresh:
        return "news_stale"
    return "news_blackout" if news_state.blackout is not None else None


def _hm(text: str) -> tuple[int, int]:
    h, m = text.split(":")
    return int(h), int(m)


def _local(now_ms: int, tz: str) -> dt.datetime:
    return dt.datetime.fromtimestamp(now_ms / 1000, tz=UTC).astimezone(ZoneInfo(tz))


def _reopen_at(cal: SessionCalendar, now_ms: int, grace_min: int) -> int | None:
    """The instant (minute) of the weekly reopen when ``now`` is open and it lies within ``grace_min`` minutes back,
    else None. Found by walking the calendar back minute by minute — no clock time is assumed."""
    if grace_min <= 0 or not cal.is_open(now_ms):
        return None
    t = now_ms - now_ms % MS_PER_MINUTE
    for back in range(0, grace_min + 1):
        m = t - back * MS_PER_MINUTE
        if not cal.is_open(m):
            reopen = m + MS_PER_MINUTE
            return reopen if not cal.is_open(reopen - WEEKLY_BREAK_MS) else None   # daily break: not the Sunday one
    return None


def desk_window_state(cfg: DeskCfg, now_ms: int, calendar: SessionCalendar, pair: str = "",
                      news: str | None = None) -> tuple[bool, str]:
    """``(True, window name)`` while entry calls are allowed, else ``(False, reason)`` (see the module text); ``news`` =
    :func:`desk_news_reason` at ``now_ms`` (None = no news block)."""
    if not calendar.is_open(now_ms):
        return False, "market_closed"
    if _reopen_at(calendar, now_ms, cfg.reopen_grace_min) is not None:
        return False, "reopen_grace"
    cut = _local(now_ms, cfg.friday_cutoff.tz)
    if cut.weekday() == 4 and (cut.hour, cut.minute) >= _hm(cfg.friday_cutoff.time):
        return False, "friday_cutoff"
    for w in cfg.windows:
        t = _local(now_ms, w.tz)
        if _hm(w.start) <= (t.hour, t.minute) < _hm(w.end):
            return (False, news) if news else (True, w.name)
    return False, "outside_window"


def desk_state_text(cfg: DeskCfg, now_ms: int, calendar: SessionCalendar, pair: str = "",
                    news: str | None = None) -> str:
    """The state as one word for the model / status: ``open`` or ``closed:<reason>`` (B16 reuses this)."""
    ok, why = desk_window_state(cfg, now_ms, calendar, pair, news)
    return "open" if ok else f"closed:{why}"


def last_window_end(cfg: DeskCfg, now_ms: int) -> int:
    """The UTC end (ms) of the latest window occurrence that ended at or before ``now`` (searched back 8 days; 0 when
    none) — the identity of the gap ``now`` lies in, so one event covers one gap between windows."""
    best = 0
    for w in cfg.windows:
        z = ZoneInfo(w.tz)
        day = _local(now_ms, w.tz).date()
        h, m = _hm(w.end)
        for back in range(0, 9):
            d = day - dt.timedelta(days=back)
            end = int(dt.datetime(d.year, d.month, d.day, h, m, tzinfo=z).timestamp() * 1000)
            if end <= now_ms:
                best = max(best, end)
                break
    return best
