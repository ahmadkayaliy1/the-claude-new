"""Data-access layer interfaces (spec §2.4: the engine must be swappable without touching other layers).

Writers (ingesters) and readers (engine, API) depend only on these protocols; concrete engines live in
``sqlite_store`` (hot) and ``parquet_store`` (cold archive).
"""
from __future__ import annotations

from typing import Protocol, Sequence

import numpy as np

from .tablespec import TableSpec

Columns = dict[str, np.ndarray]


class HotWriter(Protocol):
    def ensure_tables(self, specs: Sequence[TableSpec]) -> None: ...

    def upsert(self, spec: TableSpec, rows: Sequence[tuple]) -> int:
        """Insert rows, ignoring any whose key already exists. Returns the number actually inserted."""
        ...

    def replace(self, spec: TableSpec, rows: Sequence[tuple]) -> int:
        """Insert-or-replace (for rows that may legitimately be revised, e.g. a forming candle)."""
        ...

    def close(self) -> None: ...


class HotReader(Protocol):
    def last_time(self, spec: TableSpec) -> int | None: ...

    def first_time(self, spec: TableSpec) -> int | None: ...

    def last_key(self, spec: TableSpec) -> tuple | None: ...

    def count(self, spec: TableSpec) -> int: ...

    def read_range(self, spec: TableSpec, start_ms: int | None = None, end_ms: int | None = None,
                   columns: Sequence[str] | None = None, limit: int | None = None) -> Columns: ...

    def read_last(self, spec: TableSpec, n: int, columns: Sequence[str] | None = None) -> Columns: ...

    def close(self) -> None: ...
