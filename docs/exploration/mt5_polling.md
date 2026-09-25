# MetaTrader 5 — polling interval measurement (P1.8)

_Generated 2026-09-25T08:37:30.502Z by `research/probes/probe_mt5_polling.py` from live/real data. Re-run to refresh._

90 s per interval, symbols ['XAUUSD@', 'BTCUSD@', 'ETHUSD@']. CPU % is per logical core (4 cores on this PC).

| interval ms | calls | call p50 ms | call p99 ms | python CPU % | terminal CPU % | ticks/s (all) | same-ms ticks |
|---|---|---|---|---|---|---|---|
| 20 | 13026 | 0.081 | 0.259 | 1.3 | 1.5 | 10.1 | 118 |
| 50 | 5301 | 0.077 | 0.3 | 0.6 | 1.8 | 8.03 | 93 |
| 100 | 2682 | 0.078 | 0.255 | 0.3 | 1.2 | 7.21 | 71 |
| 250 | 1077 | 0.073 | 0.248 | 0.1 | 1.4 | 8.3 | 101 |


## Completeness check vs copy_ticks_range re-fetch

| interval ms | XAUUSD@ | BTCUSD@ | ETHUSD@ |
|---|---|---|---|
| 20 | identical (set+order) | identical (set+order) | identical (set+order) |
| 50 | identical (set+order) | identical (set+order) | identical (set+order) |
| 100 | identical (set+order) | identical (set+order) | identical (set+order) |
| 250 | identical (set+order) | identical (set+order) | identical (set+order) |

