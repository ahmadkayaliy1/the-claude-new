"""P2.2b — Can DuckDB read a live SQLite WAL file (sqlite extension) while another process writes it?

Compares heavy reads (last 24 h of aggTrades, 1m aggregation over 7 days) via python sqlite3 vs DuckDB's
sqlite scanner, with a concurrent writer process committing 20-row batches every 50 ms.
"""
from __future__ import annotations

import multiprocessing as mp
import shutil
import sqlite3
import time
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

from tradingsystem.core.settings import PROJECT_ROOT

DATA = PROJECT_ROOT / "data" / "research" / "bench"
WORK = PROJECT_ROOT / "data" / "research" / "bench_work2"
H = 3_600_000


def writer(path: str, stop, q) -> None:
    con = sqlite3.connect(path, isolation_level=None, timeout=10)
    con.execute("PRAGMA journal_mode=WAL")
    aid, ts = con.execute("SELECT max(agg_id), max(ts) FROM t").fetchone()
    lat = []
    while not stop.is_set():
        rows = [(aid + i + 1, ts + 5 * i, 84000.0, 1.0, aid + i + 1, aid + i + 1, 0) for i in range(20)]
        aid += 20
        ts += 100
        t1 = time.perf_counter()
        con.execute("BEGIN")
        con.executemany("INSERT INTO t VALUES (?,?,?,?,?,?,?) ON CONFLICT DO NOTHING", rows)
        con.execute("COMMIT")
        lat.append((time.perf_counter() - t1) * 1000)
        time.sleep(0.05)
    q.put({"commits": len(lat), "p99_ms": float(np.percentile(lat, 99))})


def main() -> None:
    import duckdb

    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True)
    path = WORK / "agg.db"
    tbl = pq.read_table(DATA / "btcusdt_agg_trades.parquet")
    con = sqlite3.connect(str(path), isolation_level=None)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE t (agg_id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, price REAL NOT NULL, qty REAL NOT NULL, "
                "first_id INTEGER NOT NULL, last_id INTEGER NOT NULL, is_buyer_maker INTEGER NOT NULL)")
    con.execute("CREATE INDEX t_ts ON t(ts)")
    cols = [tbl[c].to_numpy() for c in ("agg_id", "ts", "price", "qty", "first_id", "last_id")]
    cols.append(tbl["is_buyer_maker"].to_numpy().astype(np.int64))
    for off in range(0, tbl.num_rows, 200_000):
        con.execute("BEGIN")
        con.executemany("INSERT INTO t VALUES (?,?,?,?,?,?,?)", zip(*(c[off:off + 200_000].tolist() for c in cols)))
        con.execute("COMMIT")
    ts_max = con.execute("SELECT max(ts) FROM t").fetchone()[0]
    con.close()

    ctx = mp.get_context("spawn")
    q, stop = ctx.Queue(), ctx.Event()
    w = ctx.Process(target=writer, args=(str(path), stop, q))
    w.start()
    time.sleep(1)
    res = {}
    # python sqlite3
    c = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    t0 = time.perf_counter()
    rows = c.execute("SELECT agg_id, ts, price, qty, is_buyer_maker FROM t WHERE ts >= ?", (ts_max - 24 * H,)).fetchall()
    np.array(rows, dtype=np.float64)
    res["sqlite3_last24h_ms"] = (time.perf_counter() - t0) * 1000
    c.close()
    # DuckDB sqlite scanner
    d = duckdb.connect(config={"threads": 2, "memory_limit": "512MB"})
    t0 = time.perf_counter()
    d.execute("INSTALL sqlite; LOAD sqlite;")
    res["duckdb_ext_load_ms"] = (time.perf_counter() - t0) * 1000
    d.execute(f"ATTACH '{path.as_posix()}' AS s (TYPE sqlite, READ_ONLY)")
    t0 = time.perf_counter()
    arr = d.execute("SELECT agg_id, ts, price, qty, is_buyer_maker FROM s.t WHERE ts >= ?", [ts_max - 24 * H]).fetchnumpy()
    res["duckdb_last24h_ms"] = (time.perf_counter() - t0) * 1000
    res["duckdb_last24h_rows"] = len(arr["ts"])
    t0 = time.perf_counter()
    d.execute("SELECT ts//60000 AS m, sum(qty), sum(CASE WHEN is_buyer_maker=0 THEN qty ELSE 0 END) FROM s.t GROUP BY m").fetchall()
    res["duckdb_agg7d_ms"] = (time.perf_counter() - t0) * 1000
    t0 = time.perf_counter()
    n_before = d.execute("SELECT count(*) FROM s.t").fetchone()[0]
    time.sleep(1)
    d.execute("DETACH s")
    d.execute(f"ATTACH '{path.as_posix()}' AS s (TYPE sqlite, READ_ONLY)")
    n_after = d.execute("SELECT count(*) FROM s.t").fetchone()[0]
    res["duckdb_sees_new_commits"] = n_after > n_before
    stop.set()
    w.join()
    res["writer"] = q.get()
    print(res)
    shutil.rmtree(WORK, ignore_errors=True)


if __name__ == "__main__":
    main()
