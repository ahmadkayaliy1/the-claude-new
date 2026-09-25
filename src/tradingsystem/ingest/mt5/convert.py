"""MT5 arrays → table rows (UTC) and MT5-aware gap detection (P4.2/P4.3).

Tick key = ``utc_ms * 1000 + seq`` where ``seq`` is the tick's index among ticks sharing the same
millisecond (MT5 returns them in a stable order — P1.8), so the live poller and the backfill produce
identical keys and idempotent upserts de-duplicate across both paths.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ...core.sessions import SessionCalendar
from ...core.timeframes import Timeframe
from ...storage.gaps import Gap
from .servertime import MonotonicServerClock, ServerTimeModel


def _none_if_zero(v: float) -> float | None:
    return None if v == 0 else float(v)


def ticks_to_rows(ticks: np.ndarray, clock: MonotonicServerClock, *, seq_start: int = 0,
                  prev_srv_msc: int | None = None) -> list[tuple]:
    """Rows (key, time_msc, srv_msc, bid, ask, last, volume_real, flags) for a chronological tick array.

    ``prev_srv_msc``/``seq_start`` continue the same-millisecond counter across polling batches.
    """
    rows: list[tuple] = []
    last_srv, seq = prev_srv_msc, seq_start - 1
    for t in ticks:
        srv = int(t["time_msc"])
        seq = seq + 1 if srv == last_srv else 0
        last_srv = srv
        utc = clock.to_utc(srv)
        rows.append((utc * 1000 + seq, utc, srv, float(t["bid"]), float(t["ask"]), _none_if_zero(t["last"]),
                     _none_if_zero(t["volume_real"]), int(t["flags"])))
    return rows


def rates_to_rows(rates: np.ndarray, model: ServerTimeModel) -> list[tuple]:
    """Rows (open_time, srv_time, open, high, low, close, tick_volume, spread, real_volume)."""
    out = []
    for r in rates:
        srv = int(r["time"]) * 1000
        out.append((model.server_to_utc(srv, prefer="earlier"), srv, float(r["open"]), float(r["high"]),
                    float(r["low"]), float(r["close"]), int(r["tick_volume"]), int(r["spread"]),
                    _none_if_zero(r["real_volume"])))
    return out


@dataclass
class TickCursor:
    """Resumable position in a symbol's tick stream (server-scale ms + ticks already seen at that ms)."""
    symbol: str
    last_srv_msc: int
    seen_at_last: int = 0

    def take_new(self, ticks: np.ndarray) -> np.ndarray:
        """Drop ticks already consumed (older ms, or the first ``seen_at_last`` at the boundary ms)."""
        if ticks is None or len(ticks) == 0:
            return ticks[:0] if ticks is not None else np.empty(0)
        tm = ticks["time_msc"].astype(np.int64)
        keep = tm > self.last_srv_msc
        at = np.nonzero(tm == self.last_srv_msc)[0]
        if len(at) > self.seen_at_last:
            keep[at[self.seen_at_last:]] = True
        return ticks[keep]

    def advance(self, new: np.ndarray) -> tuple[int, int]:
        """Update the cursor after consuming ``new``; returns (prev_srv_msc, seq_start) for key continuation."""
        prev = (self.last_srv_msc, self.seen_at_last)
        if len(new):
            tm = new["time_msc"].astype(np.int64)
            last = int(tm[-1])
            n_last = int((tm == last).sum())
            self.seen_at_last = n_last + (self.seen_at_last if last == self.last_srv_msc else 0)
            self.last_srv_msc = last
        return prev


def mt5_candle_gaps(srv_times: np.ndarray, tf: Timeframe, start_srv: int, end_srv: int, calendar: SessionCalendar,
                    model: ServerTimeModel) -> list[Gap]:
    """Missing bars on the *server-time* grid (MT5 H4/D1/W1 are aligned to server midnight, not UTC).

    A bar is expected when the session is open at its start, middle or last minute (converted to UTC).
    """
    if end_srv < start_srv:
        return []
    offset = 4 * 86_400_000 - 86_400_000 if tf is Timeframe.W1 else 0   # MT5 weeks start Sunday (server)
    step = tf.ms
    first = ((start_srv - offset) // step) * step + offset
    if first < start_srv:
        first += step
    grid = np.arange(first, ((end_srv - offset) // step) * step + offset + 1, step, dtype=np.int64)
    probes = (0, step // 2, step - 60_000)

    def expected(t: int) -> bool:
        for p in probes:
            try:
                if calendar.is_open(model.server_to_utc(int(t + p), prefer="earlier")):
                    return True
            except ValueError:
                continue
        return False

    grid = np.array([t for t in grid if expected(int(t))], dtype=np.int64)
    missing = np.setdiff1d(grid, srv_times.astype(np.int64))
    if len(missing) == 0:
        return []
    breaks = np.nonzero(np.diff(missing) != step)[0]
    starts = np.concatenate([[0], breaks + 1])
    ends = np.concatenate([breaks, [len(missing) - 1]])
    return [Gap(int(missing[s]), int(missing[e]), int(e - s + 1)) for s, e in zip(starts, ends)]
