"""Cold archive: one zstd Parquet file per (instrument, table, UTC day) — D-020, P2.6.

Layout: ``data/cold/{venue}/{SYMBOL}/{table}/{YYYY}/{YYYY-MM-DD}.parquet``

* The directory tree *is* the manifest: a day file exists only after an atomic tmp→rename, so presence
  means complete; row counts/min/max come from Parquet metadata.
* ``write_day(merge=True)`` unions with an existing file and de-duplicates by key (a day may be filled both
  from Binance Vision and from the hot store), then rewrites atomically, sorted by key.
* A per-day lock file serialises writers of the same day across processes.
"""
from __future__ import annotations

import datetime as dt
import os
import time
from pathlib import Path
from typing import Iterator, Sequence

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from ..core.instruments import Instrument
from ..core.timeutil import MS_PER_DAY
from .tablespec import TableSpec

UTC = dt.timezone.utc
_ARROW = {"INTEGER": pa.int64(), "REAL": pa.float64(), "TEXT": pa.string()}


def arrow_schema(spec: TableSpec) -> pa.Schema:
    return pa.schema([pa.field(c.name, _ARROW[c.sql_type], nullable=c.nullable) for c in spec.columns])


def day_of(ts_ms: int) -> dt.date:
    return dt.datetime.fromtimestamp(ts_ms / 1000, tz=UTC).date()


def day_start_ms(day: dt.date) -> int:
    return (day - dt.date(1970, 1, 1)).days * MS_PER_DAY


class _DayLock:
    def __init__(self, path: Path, stale_s: float = 600) -> None:
        self.path, self.stale_s = path, stale_s

    def __enter__(self) -> "_DayLock":
        deadline = time.time() + 120
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                return self
            except FileExistsError:
                try:
                    if time.time() - self.path.stat().st_mtime > self.stale_s:
                        self.path.unlink(missing_ok=True)   # holder died
                        continue
                except FileNotFoundError:
                    continue
                if time.time() > deadline:
                    raise TimeoutError(f"lock busy: {self.path}")
                time.sleep(0.2)

    def __exit__(self, *exc: object) -> None:
        self.path.unlink(missing_ok=True)


class ParquetColdStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    # ------------------------------------------------------------------ paths
    def table_dir(self, inst: Instrument, spec: TableSpec) -> Path:
        return self.root / inst.venue / inst.file_stem / spec.name

    def day_path(self, inst: Instrument, spec: TableSpec, day: dt.date) -> Path:
        return self.table_dir(inst, spec) / f"{day:%Y}" / f"{day:%Y-%m-%d}.parquet"

    def days(self, inst: Instrument, spec: TableSpec) -> list[dt.date]:
        d = self.table_dir(inst, spec)
        if not d.exists():
            return []
        return sorted(dt.date.fromisoformat(p.stem) for p in d.glob("*/*.parquet"))

    def has_day(self, inst: Instrument, spec: TableSpec, day: dt.date) -> bool:
        return self.day_path(inst, spec, day).exists()

    # ------------------------------------------------------------------ writes
    def write_day(self, inst: Instrument, spec: TableSpec, day: dt.date, table: pa.Table, *, merge: bool = True) -> int:
        """Atomically write (or merge into) one day file. Returns the row count of the resulting file."""
        schema = arrow_schema(spec)
        table = table.select(list(schema.names)).cast(schema)
        lo, hi = day_start_ms(day), day_start_ms(day) + MS_PER_DAY
        tcol = table[spec.time_col]
        if table.num_rows and (pc.min(tcol).as_py() < lo or pc.max(tcol).as_py() >= hi):
            raise ValueError(f"rows outside {day} passed to write_day({spec.name})")
        path = self.day_path(inst, spec, day)
        path.parent.mkdir(parents=True, exist_ok=True)
        with _DayLock(path.with_suffix(".lock")):
            if merge and path.exists():
                table = pa.concat_tables([pq.read_table(path, schema=schema), table])
            table = _dedupe_sorted(table, spec)
            tmp = path.with_suffix(f".tmp{os.getpid()}")
            pq.write_table(table, tmp, compression="zstd", row_group_size=256_000)
            os.replace(tmp, path)
        return table.num_rows

    def day_writer(self, inst: Instrument, spec: TableSpec, day: dt.date, *, dense_key: bool = False) -> "DayWriter":
        """Streaming writer for one day (bounded memory) — see :class:`DayWriter`."""
        return DayWriter(self, inst, spec, day, dense_key=dense_key)

    # ------------------------------------------------------------------ reads
    def day_rows(self, inst: Instrument, spec: TableSpec, day: dt.date) -> int:
        p = self.day_path(inst, spec, day)
        return pq.read_metadata(p).num_rows if p.exists() else 0

    def iter_days(self, inst: Instrument, spec: TableSpec, start_ms: int | None, end_ms: int | None) -> Iterator[Path]:
        for d in self.days(inst, spec):
            lo = day_start_ms(d)
            if end_ms is not None and lo >= end_ms:
                break
            if start_ms is not None and lo + MS_PER_DAY <= start_ms:
                continue
            yield self.day_path(inst, spec, d)

    def read_range(self, inst: Instrument, spec: TableSpec, start_ms: int | None = None, end_ms: int | None = None,
                   columns: Sequence[str] | None = None) -> pa.Table:
        schema = arrow_schema(spec)
        cols = list(columns or schema.names)
        parts = []
        for p in self.iter_days(inst, spec, start_ms, end_ms):
            t = pq.read_table(p, columns=list(dict.fromkeys([*cols, spec.time_col])), schema=schema)
            mask = None
            if start_ms is not None:
                mask = pc.greater_equal(t[spec.time_col], start_ms)
            if end_ms is not None:
                m2 = pc.less(t[spec.time_col], end_ms)
                mask = m2 if mask is None else pc.and_(mask, m2)
            parts.append((t.filter(mask) if mask is not None else t).select(cols))
        if not parts:
            return schema.empty_table().select(cols)
        return pa.concat_tables(parts)

    def last_time(self, inst: Instrument, spec: TableSpec) -> int | None:
        days = self.days(inst, spec)
        if not days:
            return None
        t = pq.read_table(self.day_path(inst, spec, days[-1]), columns=[spec.time_col])
        return int(pc.max(t[spec.time_col]).as_py()) if t.num_rows else None

    def last_key(self, inst: Instrument, spec: TableSpec) -> tuple[int, int] | None:
        """(max key, time of that row) from the newest day file — single integer key tables only."""
        days = self.days(inst, spec)
        if not days:
            return None
        t = pq.read_table(self.day_path(inst, spec, days[-1]), columns=[spec.key[0], spec.time_col])
        if not t.num_rows:
            return None
        i = int(pc.index(t[spec.key[0]], pc.max(t[spec.key[0]])).as_py())
        return int(t[spec.key[0]][i].as_py()), int(t[spec.time_col][i].as_py())

    def first_time(self, inst: Instrument, spec: TableSpec) -> int | None:
        days = self.days(inst, spec)
        if not days:
            return None
        t = pq.read_table(self.day_path(inst, spec, days[0]), columns=[spec.time_col])
        return int(pc.min(t[spec.time_col]).as_py()) if t.num_rows else None

    def contains_keys(self, inst: Instrument, spec: TableSpec, day: dt.date, keys: np.ndarray) -> bool:
        """True when every key in ``keys`` (single-column key) is present in the day file."""
        p = self.day_path(inst, spec, day)
        if not p.exists():
            return len(keys) == 0
        stored = pq.read_table(p, columns=[spec.key[0]])[spec.key[0]].to_numpy()
        return bool(np.isin(keys, stored, assume_unique=False).all())


class DayWriter:
    """Streams one UTC day of rows to a temp Parquet file batch by batch; ``commit()`` installs it atomically.

    Memory stays at one batch (BF-04) — no whole-day accumulation. Rows should arrive sorted by a single integer
    key (Vision files are); otherwise the temp file is sorted/de-duplicated in memory at commit. When the day file
    already exists and ``dense_key`` (consecutive ids, e.g. aggTrade ids) lets the key ranges prove containment,
    the superset file wins without a merge; any other overlap falls back to the in-memory merge of ``write_day``.
    """

    def __init__(self, store: ParquetColdStore, inst: Instrument, spec: TableSpec, day: dt.date, *,
                 dense_key: bool = False) -> None:
        if len(spec.key) != 1:
            raise ValueError("DayWriter supports single-column keys only")
        self.spec, self.schema, self.dense_key = spec, arrow_schema(spec), dense_key
        self.path = store.day_path(inst, spec, day)
        self.tmp = self.path.with_suffix(f".stmp{os.getpid()}")
        self.lo, self.hi = day_start_ms(day), day_start_ms(day) + MS_PER_DAY
        self.rows = 0
        self._w: pq.ParquetWriter | None = None
        self._last: int | None = None
        self._sorted = True

    def write(self, table: pa.Table) -> None:
        if not table.num_rows:
            return
        table = table.select(list(self.schema.names)).cast(self.schema)
        t = table[self.spec.time_col]
        if pc.min(t).as_py() < self.lo or pc.max(t).as_py() >= self.hi:
            raise ValueError(f"rows outside {self.path.stem} passed to DayWriter({self.spec.name})")
        k = table[self.spec.key[0]].to_numpy()
        if (self._last is not None and int(k[0]) <= self._last) or bool((np.diff(k) <= 0).any()):
            self._sorted = False
        self._last = int(k.max()) if self._last is None else max(self._last, int(k.max()))
        if self._w is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._w = pq.ParquetWriter(self.tmp, self.schema, compression="zstd")
        self._w.write_table(table, row_group_size=256_000)
        self.rows += table.num_rows

    def abort(self) -> None:
        if self._w is not None:
            self._w.close()
            self._w = None
        self.tmp.unlink(missing_ok=True)

    def commit(self) -> int:
        """Install the day file; returns the row count of the resulting file."""
        if self._w is None:
            return pq.read_metadata(self.path).num_rows if self.path.exists() else 0
        self._w.close()
        self._w = None
        try:
            with _DayLock(self.path.with_suffix(".lock")):
                if not self._sorted:
                    self._rewrite(_dedupe_sorted(pq.read_table(self.tmp, schema=self.schema), self.spec))
                if self.path.exists():
                    new, old = _key_range(self.tmp, self.spec), _key_range(self.path, self.spec)
                    if old is not None and new is not None and self.dense_key and _dense(old) \
                            and old[0] <= new[0] and new[1] <= old[1]:
                        return old[2]                                   # existing file already holds every row
                    if not (new is not None and old is not None and self.dense_key and _dense(new)
                            and new[0] <= old[0] and old[1] <= new[1]):
                        both = pa.concat_tables([pq.read_table(self.path, schema=self.schema),
                                                 pq.read_table(self.tmp, schema=self.schema)])
                        self._rewrite(_dedupe_sorted(both, self.spec))   # rare fallback: in-memory merge
                n = pq.read_metadata(self.tmp).num_rows
                os.replace(self.tmp, self.path)
                return n
        finally:
            self.tmp.unlink(missing_ok=True)

    def _rewrite(self, table: pa.Table) -> None:
        pq.write_table(table, self.tmp, compression="zstd", row_group_size=256_000)


def _key_range(path: Path, spec: TableSpec) -> tuple[int, int, int] | None:
    """(min key, max key, rows) from Parquet statistics — no data read; None when statistics are missing."""
    md = pq.read_metadata(path)
    if md.num_rows == 0:
        return None
    col = md.schema.names.index(spec.key[0])
    lo = hi = None
    for i in range(md.num_row_groups):
        st = md.row_group(i).column(col).statistics
        if st is None or not st.has_min_max:
            return None
        lo = st.min if lo is None else min(lo, st.min)
        hi = st.max if hi is None else max(hi, st.max)
    return int(lo), int(hi), md.num_rows


def _dense(r: tuple[int, int, int]) -> bool:
    return r[1] - r[0] + 1 == r[2]


def _dedupe_sorted(table: pa.Table, spec: TableSpec) -> pa.Table:
    if table.num_rows == 0:
        return table
    sort_keys = [(k, "ascending") for k in spec.key]
    table = table.sort_by(sort_keys)
    if len(spec.key) == 1:
        k = table[spec.key[0]].to_numpy()
        keep = np.ones(len(k), dtype=bool)
        keep[1:] = k[1:] != k[:-1]
    else:
        cols = [table[c].to_numpy() for c in spec.key]
        keep = np.ones(table.num_rows, dtype=bool)
        same = np.ones(table.num_rows - 1, dtype=bool)
        for c in cols:
            same &= c[1:] == c[:-1]
        keep[1:] = ~same
    return table.filter(pa.array(keep)) if not keep.all() else table
