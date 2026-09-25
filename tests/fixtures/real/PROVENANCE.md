# Test fixtures — provenance (real data only)

All files are unmodified slices of real market data (spec §0: no synthetic data). Generated 2026-09-25.

| file | source | slice |
|---|---|---|
| `btcusdt_aggtrades_3000.csv` | Binance Vision `data/spot/daily/aggTrades/BTCUSDT` (checksum-verified), µs→ms normalised | 3000 consecutive aggTrades from 2026-09-18T12:07:08.657Z |
| `btcusdt_candles_1m_3000.csv` | Binance Vision `data/spot/monthly/klines/BTCUSDT/1m` | 3000 consecutive 1m candles from 2025-11-09T10:40Z |
| `xauusd_ticks_3000.csv` | MetaTrader 5, WindsorBrokers1-Demo, `XAUUSD@` `copy_ticks_range` (server→UTC via ServerTimeModel, key = time_msc*1000+seq) | 3000 consecutive ticks from 2026-09-21T10:40:35.664Z |

Produced by `research/bench/bench_prepare.py` (cached Parquet) and sliced with pyarrow.
| `payload_xauusd.json` | `SnapshotBuilder.build("XAUUSD", as_of)` on the production database (MT5 WindsorBrokers1-Demo + Binance USDⓈ-M XAUUSDT), 2026-09-25 ≈10:50 UTC | one complete real snapshot payload |
