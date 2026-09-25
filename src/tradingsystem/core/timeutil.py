"""UTC time helpers. Every stored timestamp in the system is int64 UTC milliseconds.

Naive datetimes are rejected everywhere: the MetaTrader5 package interprets them in the
*local* timezone of this PC, which silently corrupts alignment around DST switches.
"""
from __future__ import annotations

import datetime as _dt
import re
import time

UTC = _dt.timezone.utc

MS_PER_SECOND = 1_000
MS_PER_MINUTE = 60 * MS_PER_SECOND
MS_PER_HOUR = 60 * MS_PER_MINUTE
MS_PER_DAY = 24 * MS_PER_HOUR

_RELATIVE_RE = re.compile(r"^-(\d+)([dhm])$")


class NaiveDatetimeError(ValueError):
    """Raised when a timezone-less datetime reaches a time-sensitive API."""


def now_ms() -> int:
    """Current wall-clock time as UTC epoch milliseconds."""
    return time.time_ns() // 1_000_000


def ensure_aware(dt: _dt.datetime) -> _dt.datetime:
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise NaiveDatetimeError(f"naive datetime not allowed: {dt!r}")
    return dt


_EPOCH = _dt.datetime(1970, 1, 1, tzinfo=UTC)
_ONE_MS = _dt.timedelta(milliseconds=1)


def to_ms(dt: _dt.datetime) -> int:
    """Aware datetime -> UTC epoch ms (exact integer arithmetic, floors sub-ms)."""
    ensure_aware(dt)
    return (dt - _EPOCH) // _ONE_MS


def from_ms(ms: int) -> _dt.datetime:
    """UTC epoch ms -> aware UTC datetime."""
    return _dt.datetime.fromtimestamp(ms / 1000, tz=UTC)


def iso(ms: int | None) -> str | None:
    """UTC epoch ms -> ISO-8601 string with millisecond precision (for logs/UI)."""
    if ms is None:
        return None
    return from_ms(ms).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_date_spec(spec: str | _dt.date, *, now: int | None = None) -> int:
    """Parse a config date into UTC ms.

    Accepts ISO dates/datetimes ("2017-08-17", "2025-01-01T00:00:00Z") — interpreted as UTC
    when no offset is given, because config dates are always UTC by convention — or relative
    specs "-365d", "-12h", "-30m" measured back from ``now``, or ``"earliest"`` (returns 0:
    fetch as far back as the source allows).
    """
    if isinstance(spec, str) and spec.strip().lower() == "earliest":
        return 0
    if isinstance(spec, _dt.datetime):
        return to_ms(spec)
    if isinstance(spec, _dt.date):
        return to_ms(_dt.datetime(spec.year, spec.month, spec.day, tzinfo=UTC))
    text = str(spec).strip()
    m = _RELATIVE_RE.match(text)
    if m:
        qty, unit = int(m.group(1)), m.group(2)
        span = {"d": MS_PER_DAY, "h": MS_PER_HOUR, "m": MS_PER_MINUTE}[unit]
        return (now if now is not None else now_ms()) - qty * span
    text = text.replace("Z", "+00:00")
    parsed = _dt.datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return to_ms(parsed)


def floor_ms(ts: int, step_ms: int, offset_ms: int = 0) -> int:
    """Floor ``ts`` to a grid of ``step_ms`` starting at ``offset_ms``."""
    return ((ts - offset_ms) // step_ms) * step_ms + offset_ms
