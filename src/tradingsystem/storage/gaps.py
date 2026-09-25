"""Gap detection (P2.8): candle-grid holes (session-aware) and aggTrade-id holes.

Policy (spec §0/§9): a detected gap is re-requested from the source. If the source confirms it has no data
(exchange outage, broker holiday), it is recorded in ``known_gaps`` with that reason and never filled with
synthetic values.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..core.sessions import SessionCalendar
from ..core.timeframes import Timeframe
from .tablespec import Column, TableSpec

KNOWN_GAPS = TableSpec(
    name="known_gaps", datatype="system", key=("table_name", "start"), time_col="start",
    columns=(Column("table_name", "TEXT"), Column("start", "INTEGER"), Column("end", "INTEGER"),
             Column("kind", "TEXT"), Column("detected", "INTEGER"), Column("note", "TEXT", True)),
)


@dataclass(frozen=True)
class Gap:
    start: int          # first missing key/time (inclusive)
    end: int            # last missing key/time (inclusive)
    count: int


def candle_gaps(open_times: np.ndarray, tf: Timeframe, start: int, end: int,
                calendar: SessionCalendar | None = None) -> list[Gap]:
    """Missing bar opens on the UTC grid in [start, end] (inclusive), skipping closed-session minutes.

    ``calendar`` applies to intraday TFs; a bar counts as expected when the session is open at its open time.
    """
    if end < start:
        return []
    first = tf.floor(start)
    if first < start:
        first += tf.ms
    grid = np.arange(first, tf.floor(end) + 1, tf.ms, dtype=np.int64)
    if calendar is not None and tf.ms < 86_400_000:
        mask = np.fromiter((calendar.is_open(int(t)) for t in grid), dtype=bool, count=len(grid))
        grid = grid[mask]
    missing = np.setdiff1d(grid, open_times.astype(np.int64), assume_unique=False)
    return _runs(missing, tf.ms)


def id_gaps(ids: np.ndarray) -> list[Gap]:
    """Holes in a consecutive integer id sequence (e.g. Binance agg_id)."""
    if len(ids) < 2:
        return []
    ids = np.unique(ids.astype(np.int64))
    d = np.diff(ids)
    out = []
    for i in np.nonzero(d > 1)[0]:
        out.append(Gap(int(ids[i] + 1), int(ids[i + 1] - 1), int(d[i] - 1)))
    return out


def _runs(values: np.ndarray, step: int) -> list[Gap]:
    if len(values) == 0:
        return []
    breaks = np.nonzero(np.diff(values) != step)[0]
    starts = np.concatenate([[0], breaks + 1])
    ends = np.concatenate([breaks, [len(values) - 1]])
    return [Gap(int(values[s]), int(values[e]), int(e - s + 1)) for s, e in zip(starts, ends)]
