"""Hot → cold rollover (P2.6). Runs inside the process that owns the instrument's hot DB (single writer).

For each closed UTC day older than the hot window (+ grace):
  read the day from SQLite → merge into the day's Parquet file (atomic) → verify every hot key is in the
  file → only then delete the day from SQLite. A crash at any point leaves data in at least one place.

While the free disk is below ``storage.cold_archive_min_free_gb`` the live writers skip the rollover
(:class:`ColdArchiveGuard`, Phase 5 A7): the hot store keeps the rows and a later rollover moves every eligible day.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

import numpy as np
import pyarrow as pa

from ..core.instruments import Instrument
from ..core.timeutil import MS_PER_DAY, MS_PER_HOUR, now_ms as _now_ms
from . import disk
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


class ColdArchiveGuard:
    """Whether a live writer may roll over now: not while the free disk under ``path`` (the cold root) is below
    ``min_free_gb`` (``storage.cold_archive_min_free_gb``; 0 = always). Writing a day file needs room for the merged
    copy before the rename, and the hot rows are kept anyway, so waiting is safe: the first rollover after room is
    made moves every eligible day. ``warn`` gets one message per UTC day while archiving is skipped (the owner writes
    it as an ``ingestion_events`` row). An unreadable free-disk figure lets the rollover run (the old behaviour; a
    failed write keeps the hot rows)."""

    def __init__(self, path: Path, min_free_gb: float, warn: Callable[[str], None], *,
                 free: Callable[[Path], float] | None = None, clock: Callable[[], int] = _now_ms) -> None:
        self.path, self.min_free_gb, self._warn = Path(path), float(min_free_gb), warn
        self._free = free or (lambda p: disk.free_gb(p))           # looked up per call (tests patch disk.free_gb)
        self._clock = clock
        self._warned_day: int | None = None
        self._skipping = False
        self.skips = 0

    def allowed(self) -> bool:
        if self.min_free_gb <= 0:
            return True
        try:
            gb = self._free(self.path)
        except OSError as exc:
            log.warning("free disk of %s unreadable (%r) — rollover runs", self.path, exc)
            return True
        if gb >= self.min_free_gb:
            if self._skipping:
                log.info("cold archive resumed: %.1f GB free", gb)
                self._skipping = False
            return True
        self._skipping = True
        self.skips += 1
        day = self._clock() // MS_PER_DAY
        if self._warned_day != day:
            self._warned_day = day
            msg = (f"free disk {gb:.1f} GB < {self.min_free_gb:g} GB (storage.cold_archive_min_free_gb): cold archive "
                   f"skipped, rows stay in the hot store until there is room")
            log.warning("%s", msg)
            try:
                self._warn(msg)
            except Exception:  # noqa: BLE001 — the warning must never stop the writer
                log.exception("cold-archive warning could not be recorded")
        return False
