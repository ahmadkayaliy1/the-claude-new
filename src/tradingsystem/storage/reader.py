"""Unified reader across the hot (SQLite) and cold (Parquet) stores (P2.7).

Returns numpy columns sorted by key with duplicates at the hot/cold boundary removed, so callers never
need to know where a row lives.
"""
from __future__ import annotations

from pathlib import Path
from typing import Sequence

import numpy as np

from ..core.instruments import Instrument
from .parquet_store import ParquetColdStore
from .sqlite_store import SQLiteHotStore
from .tablespec import TableSpec


class InstrumentReader:
    def __init__(self, inst: Instrument, data_dir: Path, *, cache_mb: int = 16) -> None:
        self.inst = inst
        self.hot_path = inst.hot_db_path(data_dir)
        self.cold = ParquetColdStore(data_dir / "cold")
        self._hot: SQLiteHotStore | None = None
        self._cache_mb = cache_mb

    @property
    def hot(self) -> SQLiteHotStore | None:
        if self._hot is None and self.hot_path.exists():
            self._hot = SQLiteHotStore(self.hot_path, readonly=True, cache_mb=self._cache_mb)
        return self._hot

    def read_range(self, spec: TableSpec, start_ms: int | None = None, end_ms: int | None = None,
                   columns: Sequence[str] | None = None) -> dict[str, np.ndarray]:
        cols = list(dict.fromkeys([*(columns or spec.column_names), *spec.key]))
        parts: list[dict[str, np.ndarray]] = []
        hot_first = self.hot.first_time(spec) if self.hot else None
        cold_end = end_ms if hot_first is None else (min(end_ms, hot_first + 1) if end_ms is not None else hot_first + 1)
        if cold_end is None or start_ms is None or start_ms < cold_end:
            tbl = self.cold.read_range(self.inst, spec, start_ms, cold_end, cols)
            if tbl.num_rows:
                parts.append({c: tbl[c].to_numpy(zero_copy_only=False) for c in cols})
        if self.hot is not None:
            h = self.hot.read_range(spec, start_ms, end_ms, cols)
            if len(h[cols[0]]):
                parts.append(h)
        if not parts:
            return {c: np.empty(0) for c in cols}
        merged = {c: np.concatenate([p[c] for p in parts]) for c in cols}
        return _dedupe(merged, spec)

    def last_time(self, spec: TableSpec) -> int | None:
        t = self.hot.last_time(spec) if self.hot else None
        return t if t is not None else self.cold.last_time(self.inst, spec)

    def first_time(self, spec: TableSpec) -> int | None:
        c = self.cold.first_time(self.inst, spec)
        return c if c is not None else (self.hot.first_time(spec) if self.hot else None)

    def close(self) -> None:
        if self._hot is not None:
            self._hot.close()
            self._hot = None


def _dedupe(cols: dict[str, np.ndarray], spec: TableSpec) -> dict[str, np.ndarray]:
    keys = [cols[k] for k in spec.key]
    order = np.lexsort(keys[::-1])
    cols = {c: v[order] for c, v in cols.items()}
    keys = [cols[k] for k in spec.key]
    keep = np.ones(len(keys[0]), dtype=bool)
    if len(keep) > 1:
        same = np.ones(len(keep) - 1, dtype=bool)
        for k in keys:
            same &= k[1:] == k[:-1]
        keep[1:] = ~same
    return {c: v[keep] for c, v in cols.items()} if not keep.all() else cols
