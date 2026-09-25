"""Hot → cold rollover (P2.6). Runs inside the process that owns the instrument's hot DB (single writer).

For each closed UTC day older than the hot window (+ grace):
  read the day from SQLite → merge into the day's Parquet file (atomic) → verify every hot key is in the
  file → only then delete the day from SQLite. A crash at any point leaves data in at least one place.
"""
from __future__ import annotations

import logging

import numpy as np
import pyarrow as pa

from ..core.instruments import Instrument
from ..core.timeutil import MS_PER_DAY, MS_PER_HOUR
from .parquet_store import ParquetColdStore, arrow_schema, day_of, day_start_ms
from .sqlite_store import SQLiteHotStore
from .tablespec import TableSpec

log = logging.getLogger(__name__)


def columns_to_arrow(spec: TableSpec, cols: dict[str, np.ndarray]) -> pa.Table:
    schema = arrow_schema(spec)
    arrays = []
    for f in schema:
        v = cols[f.name]
        arrays.append(pa.array(v.tolist() if v.dtype == object else v, type=f.type))
    return pa.Table.from_arrays(arrays, schema=schema)


def rollover(hot: SQLiteHotStore, cold: ParquetColdStore, inst: Instrument, spec: TableSpec, *,
             hot_days: int, now_ms: int, grace_hours: int = 2) -> dict[str, int]:
    """Move every complete day older than ``hot_days`` from the hot store to Parquet. Returns stats."""
    if len(spec.key) != 1:
        raise ValueError("rollover supports single-column keys only")
    first = hot.first_time(spec)
    if first is None:
        return {"days": 0, "rows": 0}
    # a day D is eligible when D+1 00:00 + grace <= now - hot_days
    cutoff_day = day_of(now_ms - grace_hours * MS_PER_HOUR) .toordinal() - hot_days
    day = day_of(first)
    moved_days = moved_rows = 0
    while day.toordinal() < cutoff_day:
        lo = day_start_ms(day)
        hi = lo + MS_PER_DAY
        cols = hot.read_range(spec, lo, hi)
        n = len(cols[spec.key[0]])
        if n:
            cold.write_day(inst, spec, day, columns_to_arrow(spec, cols), merge=True)
            if not cold.contains_keys(inst, spec, day, cols[spec.key[0]]):
                raise RuntimeError(f"rollover verification failed for {spec.name} {day} — hot rows kept")
            hot.delete_before(spec, hi)
            moved_days += 1
            moved_rows += n
            log.info("rolled %s %s: %d rows → cold", spec.name, day, n)
        day = day.fromordinal(day.toordinal() + 1)
    if moved_rows:
        hot.checkpoint("PASSIVE")
    return {"days": moved_days, "rows": moved_rows}
