# MetaTrader 5 (Windsor) — actual history depth (P1.6)

_Measured 2026-09-25 by `research/probes/probe_mt5_history.py` after raising the terminal's `MaxBars` to unlimited
(terminal reports `maxbars = 100,000,000`). Times are UTC (converted with `ServerTimeModel`)._

## Earliest bar per timeframe

| symbol | M1 | M5 | H1 | D1 | M1 bars in last full month |
|---|---|---|---|---|---|
| XAUUSD@ | 2019-02-24 23:01 | 2019-02-24 23:00 | 2019-02-24 23:00 | 2019-02-24 | 28,969 |
| BTCUSD@ | 2015-11-15 22:00 | 2015-11-15 22:00 | 2015-11-15 22:00 | 2015-11-15 | 43,690 |
| ETHUSD@ | 2019-06-27 14:49 | 2019-06-27 14:45 | 2019-06-27 14:00 | 2019-06-26 | 43,666 |

The broker serves full M1 history back to those dates (bars are downloaded on demand and cached in the terminal's
`bases` folder — about 1.9 GB after this probe).

## Tick history

| symbol | earliest day with ticks (server date) | sample daily tick counts |
|---|---|---|
| XAUUSD@ | **2024-08-17** | 2026-06-15: 359,660 · 2025-06-16: 208,061 |
| BTCUSD@ | not measured (probe stopped — see note) | — |
| ETHUSD@ | not measured (probe stopped — see note) | — |

- Gold tick history ≈ 2 years (≈ 145 M ticks, ≈ 1.3 GB as Parquet). The MT5 backfill worker ingests it newest-first
  and records where tick history actually ends (`collector_status: mt5_backfill`).
- BTC/ETH tick depth on Windsor is only needed for price matching and paper fills; config keeps 90 days.

## Operational findings

- Deep history requests are slow on the broker side: the tick-history binary search for XAUUSD@ took ~17 min, and
  while the terminal was downloading, other clients (the recorder, a test ingester) got no answers for minutes.
  → Backfill requests must be chunked (one month of bars / one day of ticks per call — as implemented) and never run
  concurrently with exploration probes.
- `copy_rates_from_pos(count)` needs `count < maxbars`; with `maxbars = 100 M` use `copy_rates_range` instead.
