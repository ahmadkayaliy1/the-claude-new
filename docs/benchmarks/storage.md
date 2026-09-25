# Storage benchmark (P2.2)

Real data: 6,505,174 BTCUSDT spot aggTrades (7 days, Binance Vision); machine: i3-1005G1, 7.7 GB RAM, SSD. Run 1 (all runs in `storage_runs.json`).

| engine | bulk rows/s | live commit p50/p99 ms (20 rows) | re-insert 1000 ms | bytes/row | peak RSS MB | max key ms | last 1h ms | last 24h ms | 7d 1m-agg ms | concurrent |
|---|---|---|---|---|---|---|---|---|---|---|
| sqlite_rowid | 213,331 | 0.18 / 0.79 | 2.7 | 70.6 | 1001 | 0.71 | 72.7 (17,384) | 4078 (1,063,010) | 8533 | writer p99 2.2 ms, reader p99 111.4 ms, errors 0/0, WAL max 4.1 MB |
| sqlite_worowid | 135,403 | 0.33 / 3.03 | 3.5 | 78.0 | 998 | 0.64 | 44.2 (17,384) | 3348 (1,063,010) | 8306 | writer p99 12.0 ms, reader p99 121.6 ms, errors 0/0, WAL max 4.5 MB |
| sqlite_int | 125,113 | 0.20 / 0.89 | 1.8 | 61.1 | 1001 | 0.12 | 57.7 (17,384) | 3622 (1,063,010) | 9674 | writer p99 2.7 ms, reader p99 98.5 ms, errors 0/0, WAL max 4.4 MB |
| duckdb_file | 170,240 | 7.94 / 15.40 | 7.8 | 21.4 | 715 | 19.10 | 7.2 (17,384) | 117 (1,063,010) | 188 | FAILED: IOException: IO Error: Cannot open file "C:\the_claude_new\data\research\bench_work\duckdb_file\db.duckdb": The process cannot access |
| parquet_hourly | 646,766 | 3.86 / 9.64 | nan | 15.1 | 612 | 5265.75 | 433.9 (17,384) | 688 (1,063,010) | 794 | files are immutable once written → readers never block writers |

## Secondary data types (SQLite rowid) and Parquet footprint

| metric | value |
|---|---|
| candles_bulk_rows_s | 215,418.1 |
| candles_last5000_ms | 17.9 |
| candles_full_year_ms | 3,300.1 |
| candles_bytes_per_row | 84.7 |
| ticks_bulk_rows_s | 320,109.2 |
| ticks_bytes_per_row | 41.3 |
| parquet_aggtrades_bytes_per_row | 8.8 |
| parquet_ticks_bytes_per_row | 9.1 |
| parquet_candles_bytes_per_row | 51.7 |
