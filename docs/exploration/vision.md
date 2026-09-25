# Binance Vision — historical dumps inventory (P1.4)

_Generated 2026-09-25T08:23:27.759Z by `research/probes/probe_vision.py` from live/real data. Re-run to refresh._


## Inventory

| market | freq | type | symbol | tf | files | first | last | publish lag |
|---|---|---|---|---|---|---|---|---|
| spot | monthly | klines | BTCUSDT | 1m | 109 | BTCUSDT-1m-2017-08.zip | BTCUSDT-1m-2026-08.zip |  |
| spot | daily | klines | BTCUSDT | 1m | 3326 | BTCUSDT-1m-2017-08-17.zip | BTCUSDT-1m-2026-09-24.zip | 1 d |
| spot | monthly | aggTrades | BTCUSDT |  | 109 | BTCUSDT-aggTrades-2017-08.zip | BTCUSDT-aggTrades-2026-08.zip |  |
| spot | daily | aggTrades | BTCUSDT |  | 3326 | BTCUSDT-aggTrades-2017-08-17.zip | BTCUSDT-aggTrades-2026-09-24.zip | 1 d |
| spot | monthly | aggTrades | ETHUSDT |  | 109 | ETHUSDT-aggTrades-2017-08.zip | ETHUSDT-aggTrades-2026-08.zip |  |
| futures/um | monthly | klines | BTCUSDT | 1m | 80 | BTCUSDT-1m-2020-01.zip | BTCUSDT-1m-2026-08.zip |  |
| futures/um | monthly | aggTrades | BTCUSDT |  | 81 | BTCUSDT-aggTrades-2020-01.zip | part-00000-0fae7358-a956-4804-a6d4-1682c07a5127-c000.zip |  |
| futures/um | monthly | aggTrades | ETHUSDT |  | 80 | ETHUSDT-aggTrades-2020-01.zip | ETHUSDT-aggTrades-2026-08.zip |  |
| futures/um | daily | aggTrades | XAUUSDT |  | 288 | XAUUSDT-aggTrades-2025-12-11.zip | XAUUSDT-aggTrades-2026-09-24.zip | 1 d |
| futures/um | monthly | fundingRate | BTCUSDT |  | 80 | BTCUSDT-fundingRate-2020-01.zip | BTCUSDT-fundingRate-2026-08.zip |  |
| futures/um | daily | metrics | BTCUSDT |  | 2215 | BTCUSDT-metrics-2020-09-01.zip | BTCUSDT-metrics-2026-09-24.zip | 1 d |
| futures/um | daily | metrics | XAUUSDT |  | 288 | XAUUSDT-metrics-2025-12-11.zip | XAUUSDT-metrics-2026-09-24.zip | 1 d |
| futures/um | daily | bookDepth | BTCUSDT |  | 1360 | BTCUSDT-bookDepth-2023-01-01.zip | BTCUSDT-bookDepth-2026-09-24.zip | 1 d |
| futures/um | daily | bookDepth | XAUUSDT |  | 287 | XAUUSDT-bookDepth-2025-12-11.zip | XAUUSDT-bookDepth-2026-09-24.zip | 1 d |
| futures/um | daily | bookTicker | BTCUSDT |  | 320 | BTCUSDT-bookTicker-2023-05-16.zip | BTCUSDT-bookTicker-2024-03-30.zip | 909 d |
| futures/um | daily | klines | XAUUSDT | 1m | 288 | XAUUSDT-1m-2025-12-11.zip | XAUUSDT-1m-2026-09-24.zip | 1 d |


## Disk projection (compressed zip sizes, last 12 months)

| dataset | files | total GB (zip) | avg MB/file |
|---|---|---|---|
| spot/monthly/aggTrades/BTCUSDT | 12 | 5.3 | 452.3 |
| spot/monthly/aggTrades/ETHUSDT | 12 | 5.31 | 453.3 |
| futures/um/monthly/aggTrades/BTCUSDT | 12 | 6.81 | 580.7 |
| futures/um/monthly/aggTrades/ETHUSDT | 12 | 8.04 | 685.9 |
| futures/um/daily/aggTrades/XAUUSDT | 288 | 1.03 | 3.7 |
| futures/um/daily/bookDepth/XAUUSDT | 287 | 0.14 | 0.5 |
| futures/um/daily/metrics/XAUUSDT | 288 | 0 | 0 |

Zip-compressed CSV sizes; Parquet(zstd) sizes are measured in the storage benchmark (P2.2).


## Format checks (checksum, header, timestamp unit)

| file | sha256 | header | ts unit | first line |
|---|---|---|---|---|
| BTCUSDT-1m-2024-06-03.zip | OK | no | ms | 1717372800000,67765.62000000,67771.86000000,67763.85000000,67763.86000000,8.27795000,1717372859999,560963.46618310,769,3 |
| BTCUSDT-1m-2026-09-22.zip | OK | no | µs | 1790035200000000,86620.01000000,86625.82000000,86575.00000000,86576.01000000,31.99896000,1790035259999999,2771139.217032 |
| BTCUSDT-aggTrades-2024-06-03.zip | OK | no | ms | 3025112050,67765.62000000,0.00069000,3622440589,3622440589,1717372800006,True,True |
| BTCUSDT-aggTrades-2026-09-22.zip | OK | no | µs | 4070153058,86620.01000000,0.00127000,6701690135,6701690135,1790035200088876,False,True |
| XAUUSDT-aggTrades-2026-09-22.zip | OK | yes | ms | agg_trade_id,price,quantity,first_trade_id,last_trade_id,transact_time,is_buyer_maker |
| XAUUSDT-metrics-2026-09-22.zip | OK | yes | n/a | create_time,symbol,sum_open_interest,sum_open_interest_value,count_toptrader_long_short_ratio,sum_toptrader_long_short_r |
| XAUUSDT-bookDepth-2026-09-22.zip | OK | yes | n/a | timestamp,percentage,depth,notional |
| XAUUSDT-fundingRate-2026-08.zip | OK | yes | ms | calc_time,funding_interval_hours,last_funding_rate |

Spot files switched to **microsecond** timestamps from 2025-01-01; futures files stay in milliseconds. The backfiller must detect the unit per file (value range), never assume.

