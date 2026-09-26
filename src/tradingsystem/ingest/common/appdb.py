"""Shared operational DB ``data/app.db`` (P3.4/P3.8): collector status, latest quotes, ingestion events.

Written by several processes (each with tiny transactions) — SQLite WAL serialises writers safely.
The dashboard reads it for live status (spec §8.1/§8.5); ingestion events keep every disconnect/resume
with its duration (spec §2.2.5).
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from ...core.timeutil import now_ms
from ...storage.sqlite_store import connect

_DDL = [
    """CREATE TABLE IF NOT EXISTS collector_status (
        collector TEXT PRIMARY KEY, state TEXT NOT NULL, last_data_ms INTEGER, last_error TEXT,
        last_error_ms INTEGER, detail TEXT, updated_ms INTEGER NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS latest_quote (
        instrument TEXT PRIMARY KEY, ts INTEGER NOT NULL, bid REAL, ask REAL, last REAL,
        source TEXT, recv_ms INTEGER NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS ingestion_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, collector TEXT NOT NULL,
        event TEXT NOT NULL, detail TEXT, duration_ms INTEGER)""",
    "CREATE INDEX IF NOT EXISTS ingestion_events_ts ON ingestion_events(ts)",
]

STATES = ("starting", "backfilling", "gap_filling", "live", "market_closed", "reconnecting", "error", "stopped")


class AppDB:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._con = connect(self.path, cache_mb=4)
        self._lock = threading.Lock()
        with self._lock:
            for stmt in _DDL:
                self._con.execute(stmt)

    def _exec(self, sql: str, params: tuple) -> None:
        with self._lock:
            for attempt in range(5):
                try:
                    self._con.execute(sql, params)
                    return
                except sqlite3.OperationalError as exc:   # busy beyond busy_timeout — retry, never crash ingestion
                    if attempt == 4:
                        raise
                    if "locked" not in str(exc) and "busy" not in str(exc):
                        raise

    def set_status(self, collector: str, state: str, *, last_data_ms: int | None = None, error: str | None = None,
                   detail: dict[str, Any] | None = None, clear_error: bool = False) -> None:
        """Upsert a collector row. ``error=None`` keeps the previous error unless ``clear_error`` (recovered)."""
        if state not in STATES:
            raise ValueError(state)
        now = now_ms()
        err_sql = ("last_error=excluded.last_error, last_error_ms=excluded.last_error_ms" if clear_error else
                   "last_error=COALESCE(excluded.last_error, collector_status.last_error), "
                   "last_error_ms=COALESCE(excluded.last_error_ms, collector_status.last_error_ms)")
        self._exec(
            f"""INSERT INTO collector_status(collector, state, last_data_ms, last_error, last_error_ms, detail, updated_ms)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(collector) DO UPDATE SET state=excluded.state,
                 last_data_ms=COALESCE(excluded.last_data_ms, collector_status.last_data_ms), {err_sql},
                 detail=COALESCE(excluded.detail, collector_status.detail), updated_ms=excluded.updated_ms""",
            (collector, state, last_data_ms, error, now if error else None,
             json.dumps(detail, default=str) if detail is not None else None, now))

    def upsert_quote(self, instrument: str, ts: int, bid: float | None, ask: float | None, last: float | None,
                     source: str) -> None:
        self._exec("INSERT OR REPLACE INTO latest_quote VALUES (?,?,?,?,?,?,?)",
                   (instrument, ts, bid, ask, last, source, now_ms()))

    def add_event(self, collector: str, event: str, detail: str | None = None, duration_ms: int | None = None) -> None:
        self._exec("INSERT INTO ingestion_events(ts, collector, event, detail, duration_ms) VALUES (?,?,?,?,?)",
                   (now_ms(), collector, event, detail, duration_ms))

    def statuses(self) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._con.execute("SELECT * FROM collector_status ORDER BY collector")
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    def close(self) -> None:
        with self._lock:
            self._con.close()
