"""SQLite (WAL) hot store — one file per instrument, one writer process per file, many readers (D-006).

* Idempotent writes: ``INSERT … ON CONFLICT DO NOTHING`` on the table key (spec §2.2).
* Short transactions; readers use ``mode=ro`` connections and never block the writer (WAL).
* Only stdlib ``sqlite3`` + numpy → safe for the lean ingester processes (no pandas/DuckDB).
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Sequence

import numpy as np

from .tablespec import TableSpec

_NP_TYPES = {"INTEGER": np.int64, "REAL": np.float64}


def connect(path: Path, *, readonly: bool = False, cache_mb: int = 16, busy_timeout_ms: int = 10_000
            ) -> sqlite3.Connection:
    if readonly:
        if not path.exists():
            raise FileNotFoundError(path)
        con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, isolation_level=None,
                              check_same_thread=False, timeout=busy_timeout_ms / 1000)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False,
                              timeout=busy_timeout_ms / 1000)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")      # durable across app crashes; WAL keeps integrity
        con.execute("PRAGMA wal_autocheckpoint=1000")
    con.execute(f"PRAGMA cache_size=-{cache_mb * 1024}")
    con.execute("PRAGMA temp_store=MEMORY")
    con.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
    return con


class SQLiteHotStore:
    """Writer + reader for one instrument DB file. A process should hold at most one writer per file."""

    def __init__(self, path: Path, *, readonly: bool = False, cache_mb: int = 16) -> None:
        self.path = Path(path)
        self.readonly = readonly
        self._con = connect(self.path, readonly=readonly, cache_mb=cache_mb)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ writes
    def ensure_tables(self, specs: Sequence[TableSpec]) -> None:
        self._require_writer()
        with self._lock:
            self._con.execute("BEGIN")
            try:
                for spec in specs:
                    for stmt in spec.ddl():
                        self._con.execute(stmt)
                self._con.execute("COMMIT")
            except BaseException:
                self._con.execute("ROLLBACK")
                raise

    def upsert(self, spec: TableSpec, rows: Sequence[tuple]) -> int:
        return self._write(spec, rows, "INSERT INTO {t} ({c}) VALUES ({p}) ON CONFLICT DO NOTHING")

    def replace(self, spec: TableSpec, rows: Sequence[tuple]) -> int:
        return self._write(spec, rows, "INSERT OR REPLACE INTO {t} ({c}) VALUES ({p})")

    def upsert_many(self, batches: Sequence[tuple[TableSpec, Sequence[tuple]]]) -> dict[str, int]:
        """Several tables in ONE transaction (e.g. all timeframes of a flush) → fewer fsyncs."""
        self._require_writer()
        out: dict[str, int] = {}
        with self._lock:
            before = self._con.total_changes
            self._con.execute("BEGIN")
            try:
                for spec, rows in batches:
                    if rows:
                        start = self._con.total_changes
                        self._con.executemany(self._sql(spec, "INSERT INTO {t} ({c}) VALUES ({p}) ON CONFLICT DO NOTHING"), rows)
                        out[spec.name] = self._con.total_changes - start
                self._con.execute("COMMIT")
            except BaseException:
                self._con.execute("ROLLBACK")
                raise
            out["_total"] = self._con.total_changes - before
        return out

    def _write(self, spec: TableSpec, rows: Sequence[tuple], template: str) -> int:
        self._require_writer()
        if not rows:
            return 0
        sql = self._sql(spec, template)
        with self._lock:
            before = self._con.total_changes
            self._con.execute("BEGIN")
            try:
                self._con.executemany(sql, rows)
                self._con.execute("COMMIT")
            except BaseException:
                self._con.execute("ROLLBACK")
                raise
            return self._con.total_changes - before

    @staticmethod
    def _sql(spec: TableSpec, template: str) -> str:
        cols = spec.column_names
        return template.format(t=spec.name, c=", ".join(cols), p=", ".join("?" * len(cols)))

    def delete_before(self, spec: TableSpec, cutoff_ms: int) -> int:
        """Retention: remove rows older than ``cutoff_ms`` (only after they were archived cold)."""
        self._require_writer()
        with self._lock:
            before = self._con.total_changes
            self._con.execute(f"DELETE FROM {spec.name} WHERE {spec.time_col} < ?", (cutoff_ms,))
            return self._con.total_changes - before

    def checkpoint(self, mode: str = "PASSIVE") -> tuple[int, int, int]:
        with self._lock:
            return tuple(self._con.execute(f"PRAGMA wal_checkpoint({mode})").fetchone())  # type: ignore[return-value]

    def _require_writer(self) -> None:
        if self.readonly:
            raise PermissionError(f"{self.path} opened read-only")

    # ------------------------------------------------------------------ reads
    def _scalar(self, sql: str, params: tuple = ()) -> object:
        with self._lock:
            try:
                row = self._con.execute(sql, params).fetchone()
            except sqlite3.OperationalError as exc:
                if "no such table" in str(exc):
                    return None
                raise
        return row[0] if row else None

    def last_time(self, spec: TableSpec) -> int | None:
        return self._scalar(f"SELECT max({spec.time_col}) FROM {spec.name}")  # type: ignore[return-value]

    def first_time(self, spec: TableSpec) -> int | None:
        return self._scalar(f"SELECT min({spec.time_col}) FROM {spec.name}")  # type: ignore[return-value]

    def count(self, spec: TableSpec) -> int:
        return int(self._scalar(f"SELECT count(*) FROM {spec.name}") or 0)

    def last_key(self, spec: TableSpec) -> tuple | None:
        order = ", ".join(f"{k} DESC" for k in spec.key)
        with self._lock:
            try:
                row = self._con.execute(f"SELECT {', '.join(spec.key)} FROM {spec.name} ORDER BY {order} LIMIT 1").fetchone()
            except sqlite3.OperationalError as exc:
                if "no such table" in str(exc):
                    return None
                raise
        return tuple(row) if row else None

    def read_range(self, spec: TableSpec, start_ms: int | None = None, end_ms: int | None = None,
                   columns: Sequence[str] | None = None, limit: int | None = None) -> dict[str, np.ndarray]:
        cols = list(columns or spec.column_names)
        where, params = [], []
        if start_ms is not None:
            where.append(f"{spec.time_col} >= ?")
            params.append(start_ms)
        if end_ms is not None:
            where.append(f"{spec.time_col} < ?")
            params.append(end_ms)
        sql = f"SELECT {', '.join(cols)} FROM {spec.name}"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY " + ", ".join(spec.key)
        if limit:
            sql += f" LIMIT {int(limit)}"
        return self._fetch_columns(spec, sql, tuple(params), cols)

    def read_last(self, spec: TableSpec, n: int, columns: Sequence[str] | None = None) -> dict[str, np.ndarray]:
        cols = list(columns or spec.column_names)
        order = ", ".join(f"{k} DESC" for k in spec.key)
        inner = f"SELECT {', '.join(cols)} FROM {spec.name} ORDER BY {order} LIMIT {int(n)}"
        out = self._fetch_columns(spec, inner, (), cols)
        return {k: v[::-1].copy() for k, v in out.items()}   # chronological order

    def _fetch_columns(self, spec: TableSpec, sql: str, params: tuple, cols: list[str]) -> dict[str, np.ndarray]:
        types = {c.name: c.sql_type for c in spec.columns}
        with self._lock:
            try:
                rows = self._con.execute(sql, params).fetchall()
            except sqlite3.OperationalError as exc:
                if "no such table" in str(exc):
                    rows = []
                else:
                    raise
        if not rows:
            return {c: np.empty(0, dtype=_NP_TYPES.get(types[c], object)) for c in cols}
        out: dict[str, np.ndarray] = {}
        for i, c in enumerate(cols):
            t = types[c]
            if t in _NP_TYPES:
                col = [r[i] for r in rows]
                if any(v is None for v in col):
                    out[c] = np.array([np.nan if v is None else v for v in col], dtype=np.float64)
                else:
                    out[c] = np.fromiter(col, dtype=_NP_TYPES[t], count=len(col))
            else:
                out[c] = np.array([r[i] for r in rows], dtype=object)
        return out

    def close(self) -> None:
        with self._lock:
            self._con.close()

    def __enter__(self) -> "SQLiteHotStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
