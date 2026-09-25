"""MT5 server time ↔ UTC conversion (P1.7 measured model, P4.2).

MetaTrader returns timestamps as "server wall-clock time encoded as a Unix epoch". Measured for
WindsorBrokers (docs/exploration/mt5_time.md, BTC-correlation + XAU-close, 379/392 weeks agree):

* **since 2020-02 (week 06)**: EET/EEST — UTC+2, UTC+3 while EU summer time is in force
  (last Sunday of March 01:00 UTC → last Sunday of October 01:00 UTC).
* **2019 → 2020-01 (week 05)**: UTC+0, UTC+1 while US DST is in force (historical data base).

Around the October fall-back the server clock repeats one hour (03:00–04:00 server) — those server
timestamps are *ambiguous*; around the March spring-forward one server hour does not exist.
Streams processed in order resolve ambiguity with :class:`MonotonicServerClock`.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Literal

from ...core.timeutil import MS_PER_HOUR

UTC = dt.timezone.utc


def _last_sunday(year: int, month: int) -> dt.date:
    d = dt.date(year, month + 1, 1) - dt.timedelta(days=1) if month < 12 else dt.date(year, 12, 31)
    return d - dt.timedelta(days=(d.weekday() + 1) % 7)


def _nth_sunday(year: int, month: int, n: int) -> dt.date:
    d = dt.date(year, month, 1)
    first = d + dt.timedelta(days=(6 - d.weekday()) % 7)
    return first + dt.timedelta(weeks=n - 1)


def eu_summer_time(utc_ms: int) -> bool:
    """EU summer time: last Sunday of March 01:00 UTC → last Sunday of October 01:00 UTC."""
    t = dt.datetime.fromtimestamp(utc_ms / 1000, tz=UTC)
    start = dt.datetime.combine(_last_sunday(t.year, 3), dt.time(1), tzinfo=UTC)
    end = dt.datetime.combine(_last_sunday(t.year, 10), dt.time(1), tzinfo=UTC)
    return start <= t < end


def us_dst(utc_ms: int) -> bool:
    """US DST (post-2007 rules): 2nd Sunday of March 07:00 UTC → 1st Sunday of November 06:00 UTC."""
    t = dt.datetime.fromtimestamp(utc_ms / 1000, tz=UTC)
    start = dt.datetime.combine(_nth_sunday(t.year, 3, 2), dt.time(7), tzinfo=UTC)
    end = dt.datetime.combine(_nth_sunday(t.year, 11, 1), dt.time(6), tzinfo=UTC)
    return start <= t < end


@dataclass(frozen=True)
class Regime:
    start_utc_ms: int                 # regime applies from this UTC instant (inclusive)
    base_hours: int                   # standard-time offset
    dst: Literal["eu", "us", "none"]  # which summer-time rule adds +1 h
    uncertain_until_utc_ms: int = 0   # data before this instant (within the regime) is flagged uncertain

    def offset_hours(self, utc_ms: int) -> int:
        if self.dst == "eu":
            return self.base_hours + (1 if eu_summer_time(utc_ms) else 0)
        if self.dst == "us":
            return self.base_hours + (1 if us_dst(utc_ms) else 0)
        return self.base_hours


def _ms(y: int, m: int, d: int) -> int:
    return int(dt.datetime(y, m, d, tzinfo=UTC).timestamp() * 1000)


# Measured for WindsorBrokers1-Demo. The regime switch happened between 2020-01-27 and 2020-02-09;
# the weekend of 2020-02-01/02 is used as boundary and the two weeks around it are flagged uncertain.
WINDSOR_REGIMES: tuple[Regime, ...] = (
    Regime(start_utc_ms=0, base_hours=0, dst="us"),
    Regime(start_utc_ms=_ms(2020, 2, 1), base_hours=2, dst="eu", uncertain_until_utc_ms=_ms(2020, 2, 10)),
)


class AmbiguousServerTime(ValueError):
    pass


class NonexistentServerTime(ValueError):
    pass


class ServerTimeModel:
    def __init__(self, regimes: tuple[Regime, ...] = WINDSOR_REGIMES) -> None:
        if not regimes or regimes[0].start_utc_ms != 0:
            raise ValueError("first regime must start at 0")
        self.regimes = tuple(sorted(regimes, key=lambda r: r.start_utc_ms))

    def regime_at(self, utc_ms: int) -> Regime:
        cur = self.regimes[0]
        for r in self.regimes:
            if r.start_utc_ms <= utc_ms:
                cur = r
            else:
                break
        return cur

    def offset_hours(self, utc_ms: int) -> int:
        return self.regime_at(utc_ms).offset_hours(utc_ms)

    def utc_to_server(self, utc_ms: int) -> int:
        return utc_ms + self.offset_hours(utc_ms) * MS_PER_HOUR

    def candidates(self, server_ms: int) -> list[int]:
        """All UTC instants that map to ``server_ms`` (0: gap, 1: normal, 2: ambiguous)."""
        out = []
        for k in range(-2, 6):
            utc = server_ms - k * MS_PER_HOUR
            if self.offset_hours(utc) == k:
                out.append(utc)
        return sorted(out)

    def server_to_utc(self, server_ms: int, *, prefer: Literal["raise", "earlier", "later"] = "raise") -> int:
        c = self.candidates(server_ms)
        if len(c) == 1:
            return c[0]
        if not c:
            if prefer == "raise":
                raise NonexistentServerTime(f"server time {server_ms} does not exist (spring-forward gap)")
            # a timestamp inside the skipped hour: interpret it with the offset valid just before the switch
            return server_ms - self.offset_hours(server_ms - 6 * MS_PER_HOUR) * MS_PER_HOUR
        if prefer == "raise":
            raise AmbiguousServerTime(f"server time {server_ms} is ambiguous: {c}")
        return c[0] if prefer == "earlier" else c[-1]

    def is_uncertain(self, utc_ms: int) -> bool:
        r = self.regime_at(utc_ms)
        return utc_ms < r.uncertain_until_utc_ms


class MonotonicServerClock:
    """Converts an in-order stream of server timestamps, resolving the repeated fall-back hour by order.

    While inside an ambiguous window the earlier (summer-time) interpretation is used until the server
    clock jumps backwards, after which the later interpretation is used.
    """

    def __init__(self, model: ServerTimeModel) -> None:
        self.model = model
        self._last_server: int | None = None
        self._after_fallback_until: int | None = None  # server ms until which "later" applies

    def to_utc(self, server_ms: int) -> int:
        c = self.model.candidates(server_ms)
        if self._last_server is not None and server_ms < self._last_server - 30 * 60 * 1000 and len(c) == 2:
            self._after_fallback_until = server_ms + MS_PER_HOUR
        self._last_server = server_ms
        if len(c) == 2:
            use_later = self._after_fallback_until is not None and server_ms < self._after_fallback_until
            return c[1] if use_later else c[0]
        if len(c) == 1:
            return c[0]
        return self.model.server_to_utc(server_ms, prefer="later")
