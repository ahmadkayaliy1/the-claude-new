# Test fixtures — provenance (real data only)

All files are unmodified slices of real market data (spec §0: no synthetic data). Generated 2026-09-25.

| file | source | slice |
|---|---|---|
| `btcusdt_aggtrades_3000.csv` | Binance Vision `data/spot/daily/aggTrades/BTCUSDT` (checksum-verified), µs→ms normalised | 3000 consecutive aggTrades from 2026-09-18T12:07:08.657Z |
| `btcusdt_candles_1m_3000.csv` | Binance Vision `data/spot/monthly/klines/BTCUSDT/1m` | 3000 consecutive 1m candles from 2025-11-09T10:40Z |
| `xauusd_ticks_3000.csv` | MetaTrader 5, WindsorBrokers1-Demo, `XAUUSD@` `copy_ticks_range` (server→UTC via ServerTimeModel, key = time_msc*1000+seq) | 3000 consecutive ticks from 2026-09-21T10:40:35.664Z |

Produced by `research/bench/bench_prepare.py` (cached Parquet) and sliced with pyarrow.
| `payload_xauusd.json` | `SnapshotBuilder.build("XAUUSD", as_of)` on the production database (MT5 WindsorBrokers1-Demo + Binance USDⓈ-M XAUUSDT), 2026-09-25 ≈10:50 UTC | one complete real snapshot payload |
| `claude_code_not_logged_in.json` | Claude Code CLI 2.1.282 `claude -p --output-format json --json-schema …` stdout, run with a clean environment before sign-in (no model call) | one complete result envelope (error: not logged in) |
| `claude_code_auth_status_logged_out.json` | Claude Code CLI 2.1.282 `claude auth status --json` stdout before sign-in | complete output |
| `XAUUSDT-1d-2025-12.zip` (+ `.CHECKSUM`) | Binance Vision `data/futures/um/monthly/klines/XAUUSDT/1d/`, downloaded unmodified 2026-09-26 (SHA-256 matches the published CHECKSUM) | the whole monthly file (1337 bytes) |
| `vision_listing_xauusdt_1d.xml` | S3 listing `GET data.binance.vision?delimiter=/&prefix=data/futures/um/monthly/klines/XAUUSDT/1d/`, 2026-09-26 | one real ListBucketResult page |
