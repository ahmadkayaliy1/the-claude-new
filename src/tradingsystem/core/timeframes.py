"""Timeframe definitions shared by every layer (ingestion, storage, analysis)."""
from __future__ import annotations

from enum import Enum

from .timeutil import MS_PER_DAY, MS_PER_HOUR, MS_PER_MINUTE, floor_ms

# 1970-01-01 was a Thursday; Binance weekly candles open on Monday 00:00 UTC.
_MONDAY_OFFSET_MS = 4 * MS_PER_DAY


class Timeframe(str, Enum):
    M1 = "1m"
    M5 = "5m"
    M15 = "15m"
    H1 = "1h"
    H4 = "4h"
    D1 = "1d"
    W1 = "1w"

    @property
    def ms(self) -> int:
        return _DURATION_MS[self]

    @property
    def binance_interval(self) -> str:
        return self.value

    @property
    def mt5_attr(self) -> str:
        """Name of the MetaTrader5 module constant (resolved lazily to avoid importing MT5)."""
        return _MT5_ATTR[self]

    def floor(self, ts_ms: int) -> int:
        """Open time of the (UTC-aligned) bar containing ``ts_ms``."""
        offset = _MONDAY_OFFSET_MS if self is Timeframe.W1 else 0
        return floor_ms(ts_ms, self.ms, offset)

    @classmethod
    def parse(cls, value: str | "Timeframe") -> "Timeframe":
        if isinstance(value, Timeframe):
            return value
        try:
            return cls(value)
        except ValueError as exc:
            raise ValueError(f"unknown timeframe {value!r}; expected one of {[t.value for t in cls]}") from exc


_DURATION_MS = {
    Timeframe.M1: MS_PER_MINUTE,
    Timeframe.M5: 5 * MS_PER_MINUTE,
    Timeframe.M15: 15 * MS_PER_MINUTE,
    Timeframe.H1: MS_PER_HOUR,
    Timeframe.H4: 4 * MS_PER_HOUR,
    Timeframe.D1: MS_PER_DAY,
    Timeframe.W1: 7 * MS_PER_DAY,
}

_MT5_ATTR = {
    Timeframe.M1: "TIMEFRAME_M1",
    Timeframe.M5: "TIMEFRAME_M5",
    Timeframe.M15: "TIMEFRAME_M15",
    Timeframe.H1: "TIMEFRAME_H1",
    Timeframe.H4: "TIMEFRAME_H4",
    Timeframe.D1: "TIMEFRAME_D1",
    Timeframe.W1: "TIMEFRAME_W1",
}

ALL_TIMEFRAMES: tuple[Timeframe, ...] = tuple(Timeframe)
