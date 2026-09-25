"""P2.2 — Storage benchmark on real data (spec §2.4: evaluate, don't assume).

Engines: sqlite_rowid · sqlite_worowid · sqlite_int (scaled-integer prices) · duckdb_file · parquet_hourly
Workloads per engine (each engine runs in its own process → isolated peak RSS):
  bulk load · live micro-batch commits (p50/p99) · idempotent re-insert · reads (max key, last 1h, last 24h,
  7-day 1m aggregation) · concurrent reader process while a writer process commits (latency, errors, WAL size)
Data: data/research/bench/*.parquet (prepared by bench_prepare.py from Binance Vision + MT5).
"""
from __future__ import annotations

import json
import multiprocessing as mp
import os
import shutil
import sqlite3
import statistics
import sys
import time
from pathlib import Path

import numpy as np
import psutil
import pyarrow as pa
import pyarrow.parquet as pq

from tradingsystem.core.settings import PROJECT_ROOT

DATA = PROJECT_ROOT / "data" / "research" / "bench"
WORK = PROJECT_ROOT / "data" / "research" / "bench_work"
DOCS = PROJECT_ROOT / "docs" / "benchmarks"
H = 3_600_000
SQLITE_ENGINES = ("sqlite_rowid", "sqlite_worowid", "sqlite_int")
ENGINES = (*SQLITE_ENGINES, "duckdb_file", "parquet_hourly")
LIVE_COMMITS, LIVE_ROWS = 600, 20


def pctl(v, q):
    return float(np.percentile(v, q)) if len(v) else float("nan")


def peak_mb() -> float:
    mi = psutil.Process().memory_info()
    return getattr(mi, "peak_wset", mi.rss) / 2**20


def dir_size(p: Path) -> int:
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) if p.is_dir() else (
        sum(Path(str(p) + s).stat().st_size for s in ("", "-wal", "-shm") if Path(str(p) + s).exists()))


# ----------------------------------------------------------------------------------- SQLite
def sqlite_connect(path: Path, readonly: bool = False) -> sqlite3.Connection:
    uri = f"file:{path.as_posix()}{'?mode=ro' if readonly else ''}"
    con = sqlite3.connect(uri, uri=True, timeout=10, isolation_level=None, check_same_thread=False)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA temp_store=MEMORY")
    con.execute("PRAGMA cache_size=-16000")
    return con


def sqlite_schema(engine: str) -> str:
    if engine == "sqlite_worowid":
        return ("CREATE TABLE IF NOT EXISTS t (agg_id INTEGER PRIMARY KEY, price REAL, qty REAL, first_id INTEGER, "
                "last_id INTEGER, ts INTEGER, m INTEGER) WITHOUT ROWID")
    if engine == "sqlite_int":
        return ("CREATE TABLE IF NOT EXISTS t (agg_id INTEGER PRIMARY KEY, price INTEGER, qty INTEGER, "
                "first_id INTEGER, last_id INTEGER, ts INTEGER, m INTEGER)")
    return ("CREATE TABLE IF NOT EXISTS t (agg_id INTEGER PRIMARY KEY, price REAL, qty REAL, first_id INTEGER, "
            "last_id INTEGER, ts INTEGER, m INTEGER)")


def rows_of(tbl: pa.Table, engine: str):
    cols = [tbl[c].to_numpy() for c in ("agg_id", "price", "qty", "first_id", "last_id", "ts", "is_buyer_maker")]
    if engine == "sqlite_int":
        cols[1] = np.round(cols[1] * 100).astype(np.int64)
        cols[2] = np.round(cols[2] * 1e8).astype(np.int64)
    cols[6] = cols[6].astype(np.int64)
    return list(zip(*(c.tolist() for c in cols)))


def sqlite_insert(con, engine, tbl) -> None:
    con.execute("BEGIN")
    con.executemany("INSERT INTO t VALUES (?,?,?,?,?,?,?) ON CONFLICT DO NOTHING", rows_of(tbl, engine))
    con.execute("COMMIT")


# ----------------------------------------------------------------------------------- engine run
def run_engine(engine: str, q: mp.Queue) -> None:
    tbl = pq.read_table(DATA / "btcusdt_agg_trades.parquet")
    n = tbl.num_rows
    bulk_tbl, live_tbl = tbl.slice(0, n - LIVE_COMMITS * LIVE_ROWS), tbl.slice(n - LIVE_COMMITS * LIVE_ROWS)
    wd = WORK / engine
    shutil.rmtree(wd, ignore_errors=True)
    wd.mkdir(parents=True)
    res: dict = {"engine": engine, "rows": n}
    ts_max = int(pa.compute.max(tbl["ts"]).as_py())

    if engine in SQLITE_ENGINES:
        path = wd / "db.sqlite"
        con = sqlite_connect(path)
        con.execute(sqlite_schema(engine))
        con.execute("CREATE INDEX IF NOT EXISTS t_ts ON t(ts)")
        t0 = time.perf_counter()
        for off in range(0, bulk_tbl.num_rows, 100_000):
            sqlite_insert(con, engine, bulk_tbl.slice(off, 100_000))
        res["bulk_s"] = time.perf_counter() - t0
        lat = []
        for i in range(LIVE_COMMITS):
            b = live_tbl.slice(i * LIVE_ROWS, LIVE_ROWS)
            t1 = time.perf_counter()
            sqlite_insert(con, engine, b)
            lat.append((time.perf_counter() - t1) * 1000)
        res["live_ms"] = [pctl(lat, 50), pctl(lat, 99)]
        t1 = time.perf_counter()
        sqlite_insert(con, engine, live_tbl.slice(live_tbl.num_rows - 1000))
        res["reinsert_1000_ms"] = (time.perf_counter() - t1) * 1000
        res["count_ok"] = con.execute("SELECT count(*) FROM t").fetchone()[0] == n
        reads = {}
        t1 = time.perf_counter(); con.execute("SELECT max(agg_id) FROM t").fetchone(); reads["max_key_ms"] = (time.perf_counter() - t1) * 1000
        for label, span in (("last_1h_ms", H), ("last_24h_ms", 24 * H)):
            t1 = time.perf_counter()
            rows = con.execute("SELECT agg_id, price, qty, ts, m FROM t WHERE ts >= ?", (ts_max - span,)).fetchall()
            arr = np.array(rows, dtype=np.float64)
            reads[label] = (time.perf_counter() - t1) * 1000
            reads[label.replace("_ms", "_rows")] = len(arr)
        t1 = time.perf_counter()
        con.execute("SELECT ts/60000 AS m1, sum(qty), sum(CASE WHEN m=0 THEN qty ELSE 0 END) FROM t GROUP BY m1").fetchall()
        reads["agg_1m_7d_ms"] = (time.perf_counter() - t1) * 1000
        res["reads"] = reads
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.close()
        res["bytes_per_row"] = dir_size(path) / n
        # concurrent writer + reader processes
        res["concurrent"] = concurrent_test(engine, path, live_tbl, ts_max)
    elif engine == "duckdb_file":
        import duckdb
        path = wd / "db.duckdb"
        con = duckdb.connect(str(path), config={"memory_limit": "512MB", "threads": 2})
        con.execute("CREATE TABLE t (agg_id BIGINT PRIMARY KEY, price DOUBLE, qty DOUBLE, first_id BIGINT, "
                    "last_id BIGINT, ts BIGINT, m BOOLEAN)")
        t0 = time.perf_counter()
        for off in range(0, bulk_tbl.num_rows, 100_000):
            batch = bulk_tbl.slice(off, 100_000)  # noqa: F841 (referenced by DuckDB replacement scan)
            con.execute("INSERT OR IGNORE INTO t SELECT * FROM batch")
        res["bulk_s"] = time.perf_counter() - t0
        lat = []
        for i in range(LIVE_COMMITS):
            batch = live_tbl.slice(i * LIVE_ROWS, LIVE_ROWS)  # noqa: F841
            t1 = time.perf_counter()
            con.execute("INSERT OR IGNORE INTO t SELECT * FROM batch")
            lat.append((time.perf_counter() - t1) * 1000)
        res["live_ms"] = [pctl(lat, 50), pctl(lat, 99)]
        batch = live_tbl.slice(live_tbl.num_rows - 1000)  # noqa: F841
        t1 = time.perf_counter()
        con.execute("INSERT OR IGNORE INTO t SELECT * FROM batch")
        res["reinsert_1000_ms"] = (time.perf_counter() - t1) * 1000
        res["count_ok"] = con.execute("SELECT count(*) FROM t").fetchone()[0] == n
        reads = {}
        t1 = time.perf_counter(); con.execute("SELECT max(agg_id) FROM t").fetchone(); reads["max_key_ms"] = (time.perf_counter() - t1) * 1000
        for label, span in (("last_1h_ms", H), ("last_24h_ms", 24 * H)):
            t1 = time.perf_counter()
            arr = con.execute("SELECT agg_id, price, qty, ts, m FROM t WHERE ts >= ?", [ts_max - span]).fetchnumpy()
            reads[label] = (time.perf_counter() - t1) * 1000
            reads[label.replace("_ms", "_rows")] = len(arr["ts"])
        t1 = time.perf_counter()
        con.execute("SELECT ts//60000 AS m1, sum(qty), sum(CASE WHEN NOT m THEN qty ELSE 0 END) FROM t GROUP BY m1").fetchall()
        reads["agg_1m_7d_ms"] = (time.perf_counter() - t1) * 1000
        res["reads"] = reads
        con.execute("CHECKPOINT")
        # a second process trying to read while this one holds the file
        res["concurrent"] = {"reader_open": probe_duckdb_reader(path)}
        con.close()
        res["bytes_per_row"] = dir_size(path) / n
    else:  # parquet_hourly: buffered writer → one file per UTC hour; live = one small file per flush
        import duckdb
        hours = (bulk_tbl["ts"].to_numpy() // H)
        t0 = time.perf_counter()
        bounds = np.nonzero(np.diff(hours))[0] + 1
        starts = np.concatenate([[0], bounds])
        ends = np.concatenate([bounds, [len(hours)]])
        for s, e in zip(starts, ends):
            pq.write_table(bulk_tbl.slice(int(s), int(e - s)), wd / f"h{hours[s]}.parquet", compression="zstd")
        res["bulk_s"] = time.perf_counter() - t0
        lat = []
        for i in range(LIVE_COMMITS):
            b = live_tbl.slice(i * LIVE_ROWS, LIVE_ROWS)
            t1 = time.perf_counter()
            pq.write_table(b, wd / f"live_{i:05d}.parquet", compression="zstd")
            lat.append((time.perf_counter() - t1) * 1000)
        res["live_ms"] = [pctl(lat, 50), pctl(lat, 99)]
        res["reinsert_1000_ms"] = float("nan")
        res["count_ok"] = "append-only (no key enforcement)"
        con = duckdb.connect(config={"memory_limit": "512MB", "threads": 2})
        glob = (wd / "*.parquet").as_posix()
        reads = {}
        t1 = time.perf_counter(); con.execute(f"SELECT max(agg_id) FROM read_parquet('{glob}')").fetchone(); reads["max_key_ms"] = (time.perf_counter() - t1) * 1000
        for label, span in (("last_1h_ms", H), ("last_24h_ms", 24 * H)):
            t1 = time.perf_counter()
            arr = con.execute(f"SELECT agg_id, price, qty, ts, is_buyer_maker FROM read_parquet('{glob}') WHERE ts >= ?",
                              [ts_max - span]).fetchnumpy()
            reads[label] = (time.perf_counter() - t1) * 1000
            reads[label.replace("_ms", "_rows")] = len(arr["ts"])
        t1 = time.perf_counter()
        con.execute(f"SELECT ts//60000 AS m1, sum(qty), sum(CASE WHEN NOT is_buyer_maker THEN qty ELSE 0 END) "
                    f"FROM read_parquet('{glob}') GROUP BY m1").fetchall()
        reads["agg_1m_7d_ms"] = (time.perf_counter() - t1) * 1000
        res["reads"] = reads
        res["files"] = len(list(wd.glob("*.parquet")))
        res["bytes_per_row"] = dir_size(wd) / n
        res["concurrent"] = {"note": "files are immutable once written → readers never block writers"}
    res["peak_rss_mb"] = peak_mb()
    q.put(res)


def probe_duckdb_reader(path: Path) -> str:
    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    p = ctx.Process(target=_duck_reader, args=(str(path), q))
    p.start()
    p.join(30)
    return q.get() if not q.empty() else "timeout"


def _duck_reader(path: str, q) -> None:
    import duckdb
    try:
        c = duckdb.connect(path, read_only=True)
        c.execute("SELECT count(*) FROM t").fetchone()
        q.put("OK (read-only open succeeded while writer open)")
    except Exception as exc:  # noqa: BLE001
        q.put(f"FAILED: {type(exc).__name__}: {str(exc)[:120]}")


def concurrent_test(engine: str, path: Path, live_tbl: pa.Table, ts_max: int, seconds: float = 20) -> dict:
    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    stop = ctx.Event()
    w = ctx.Process(target=_writer, args=(engine, str(path), seconds, q))
    r = ctx.Process(target=_reader, args=(str(path), ts_max, stop, q))
    r.start()
    w.start()
    w.join()
    stop.set()
    r.join()
    out = {}
    for _ in range(2):
        out.update(q.get())
    return out


def _writer(engine: str, path: str, seconds: float, q) -> None:
    con = sqlite_connect(Path(path))
    base = con.execute("SELECT max(agg_id), max(ts) FROM t").fetchone()
    aid, ts = base
    lat, errors, wal_max = [], 0, 0
    end = time.time() + seconds
    while time.time() < end:
        rows = []
        for _ in range(LIVE_ROWS):
            aid += 1
            ts += 5
            rows.append((aid, 84000.0 if engine != "sqlite_int" else 8400000, 1.0 if engine != "sqlite_int" else 100000000,
                         aid, aid, ts, 0))
        t1 = time.perf_counter()
        try:
            con.execute("BEGIN")
            con.executemany("INSERT INTO t VALUES (?,?,?,?,?,?,?) ON CONFLICT DO NOTHING", rows)
            con.execute("COMMIT")
        except sqlite3.OperationalError:
            errors += 1
        lat.append((time.perf_counter() - t1) * 1000)
        wal = Path(path + "-wal")
        if wal.exists():
            wal_max = max(wal_max, wal.stat().st_size)
        time.sleep(0.05)
    q.put({"writer_commits": len(lat), "writer_ms_p50": pctl(lat, 50), "writer_ms_p99": pctl(lat, 99),
           "writer_errors": errors, "wal_max_mb": wal_max / 2**20})
    con.close()


def _reader(path: str, ts_max: int, stop, q) -> None:
    con = sqlite_connect(Path(path), readonly=True)
    lat, errors = [], 0
    while not stop.is_set():
        t1 = time.perf_counter()
        try:
            con.execute("SELECT agg_id, price, qty, ts, m FROM t WHERE ts >= ?", (ts_max - H,)).fetchall()
        except sqlite3.OperationalError:
            errors += 1
        lat.append((time.perf_counter() - t1) * 1000)
    q.put({"reader_queries": len(lat), "reader_ms_p50": pctl(lat, 50), "reader_ms_p99": pctl(lat, 99),
           "reader_errors": errors})
    con.close()


def candles_and_ticks() -> dict:
    """Secondary data types on the SQLite rowid engine (the working hypothesis)."""
    out = {}
    wd = WORK / "secondary"
    shutil.rmtree(wd, ignore_errors=True)
    wd.mkdir(parents=True)
    c = pq.read_table(DATA / "btcusdt_candles_1m.parquet")
    con = sqlite_connect(wd / "candles.sqlite")
    con.execute("CREATE TABLE k (open_time INTEGER PRIMARY KEY, open REAL, high REAL, low REAL, close REAL, volume REAL, "
                "quote_volume REAL, taker_buy_base REAL, taker_buy_quote REAL, trades INTEGER)")
    cols = [c[x].to_numpy().tolist() for x in ("open_time", "open", "high", "low", "close", "volume", "quote_volume",
                                               "taker_buy_base", "taker_buy_quote", "trades")]
    t0 = time.perf_counter()
    con.execute("BEGIN")
    con.executemany("INSERT INTO k VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING", list(zip(*cols)))
    con.execute("COMMIT")
    out["candles_bulk_rows_s"] = c.num_rows / (time.perf_counter() - t0)
    t0 = time.perf_counter()
    rows = con.execute("SELECT * FROM k ORDER BY open_time DESC LIMIT 5000").fetchall()
    np.array(rows, dtype=np.float64)
    out["candles_last5000_ms"] = (time.perf_counter() - t0) * 1000
    t0 = time.perf_counter()
    np.array(con.execute("SELECT * FROM k").fetchall(), dtype=np.float64)
    out["candles_full_year_ms"] = (time.perf_counter() - t0) * 1000
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.close()
    out["candles_bytes_per_row"] = dir_size(wd / "candles.sqlite") / c.num_rows
    t = pq.read_table(DATA / "xauusd_ticks.parquet")
    con = sqlite_connect(wd / "ticks.sqlite")
    con.execute("CREATE TABLE x (key INTEGER PRIMARY KEY, time_msc INTEGER, bid REAL, ask REAL, flags INTEGER)")
    cols = [t[x].to_numpy().tolist() for x in ("key", "time_msc", "bid", "ask", "flags")]
    t0 = time.perf_counter()
    con.execute("BEGIN")
    con.executemany("INSERT INTO x VALUES (?,?,?,?,?) ON CONFLICT DO NOTHING", list(zip(*cols)))
    con.execute("COMMIT")
    out["ticks_bulk_rows_s"] = t.num_rows / (time.perf_counter() - t0)
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.close()
    out["ticks_bytes_per_row"] = dir_size(wd / "ticks.sqlite") / t.num_rows
    out["parquet_aggtrades_bytes_per_row"] = (DATA / "btcusdt_agg_trades.parquet").stat().st_size / pq.read_metadata(
        DATA / "btcusdt_agg_trades.parquet").num_rows
    out["parquet_ticks_bytes_per_row"] = (DATA / "xauusd_ticks.parquet").stat().st_size / t.num_rows
    out["parquet_candles_bytes_per_row"] = (DATA / "btcusdt_candles_1m.parquet").stat().st_size / c.num_rows
    return out


def main() -> None:
    engines = sys.argv[1:] or list(ENGINES)
    ctx = mp.get_context("spawn")
    results = []
    for e in engines:
        q = ctx.Queue()
        p = ctx.Process(target=run_engine, args=(e, q))
        t0 = time.time()
        p.start()
        res = q.get()
        p.join()
        res["wall_s"] = time.time() - t0
        results.append(res)
        print(json.dumps(res, default=str))
    secondary = candles_and_ticks()
    print(json.dumps(secondary))
    write_report(results, secondary)
    shutil.rmtree(WORK, ignore_errors=True)


def write_report(results: list[dict], sec: dict) -> None:
    DOCS.mkdir(parents=True, exist_ok=True)
    runs_path = DOCS / "storage_runs.json"
    runs = json.loads(runs_path.read_text()) if runs_path.exists() else []
    runs.append({"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "results": results, "secondary": sec})
    runs_path.write_text(json.dumps(runs, indent=1, default=str))
    L = ["# Storage benchmark (P2.2)", "",
         f"Real data: {results[0]['rows']:,} BTCUSDT spot aggTrades (7 days, Binance Vision); machine: i3-1005G1, "
         f"7.7 GB RAM, SSD. Run {len(runs)} (all runs in `storage_runs.json`).", "",
         "| engine | bulk rows/s | live commit p50/p99 ms (20 rows) | re-insert 1000 ms | bytes/row | peak RSS MB | "
         "max key ms | last 1h ms | last 24h ms | 7d 1m-agg ms | concurrent |",
         "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        rd = r["reads"]
        cc = r.get("concurrent", {})
        conc = (f"writer p99 {cc['writer_ms_p99']:.1f} ms, reader p99 {cc['reader_ms_p99']:.1f} ms, errors "
                f"{cc['writer_errors']}/{cc['reader_errors']}, WAL max {cc['wal_max_mb']:.1f} MB"
                if "writer_ms_p99" in cc else next(iter(cc.values()), ""))
        L.append(f"| {r['engine']} | {r['rows'] / r['bulk_s']:,.0f} | {r['live_ms'][0]:.2f} / {r['live_ms'][1]:.2f} | "
                 f"{r['reinsert_1000_ms']:.1f} | {r['bytes_per_row']:.1f} | {r['peak_rss_mb']:.0f} | "
                 f"{rd['max_key_ms']:.2f} | {rd['last_1h_ms']:.1f} ({rd['last_1h_rows']:,}) | "
                 f"{rd['last_24h_ms']:.0f} ({rd['last_24h_rows']:,}) | {rd['agg_1m_7d_ms']:.0f} | {conc} |")
    L += ["", "## Secondary data types (SQLite rowid) and Parquet footprint", "",
          "| metric | value |", "|---|---|"] + [f"| {k} | {v:,.1f} |" for k, v in sec.items()]
    (DOCS / "storage.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    print("report:", DOCS / "storage.md")


if __name__ == "__main__":
    main()
