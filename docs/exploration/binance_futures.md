# Binance USDⓈ-M futures — actual data available (P1.3)

_Generated 2026-09-25T08:19:44.668Z by `research/probes/probe_binance_futures.py` from live/real data. Re-run to refresh._


## Contracts

| symbol | contractType | status | onboard | tickSize | stepSize |
|---|---|---|---|---|---|
| BTCUSDT | PERPETUAL | TRADING | 2019-09-08T17:55:00.000Z | 0.10 | 0.001 |
| ETHUSDT | PERPETUAL | TRADING | 2019-11-27T07:45:00.000Z | 0.01 | 0.001 |
| XAUUSDT | TRADIFI_PERPETUAL | TRADING | 2025-12-11T08:05:00.000Z | 0.01 | 0.001 |
| PAXGUSDT | PERPETUAL | TRADING | 2025-03-27T10:30:00.000Z | 0.0100 | 0.001 |


## 24h activity

| symbol | last | quote vol (M USDT) | trades 24h |
|---|---|---|---|
| BTCUSDT | 84192.80 | 13,240.9 | 3967693 |
| ETHUSDT | 2683.69 | 9,068.6 | 4923965 |
| XAUUSDT | 4284.33 | 1,950.1 | 1597544 |
| PAXGUSDT | 4275.3300 | 79.6 | 194779 |


## History depth per endpoint

| symbol | klines 1m | funding | OI hist 5m | L/S ratio 5m | taker ratio 5m | aggTrades REST |
|---|---|---|---|---|---|---|
| BTCUSDT | 2019-09-08T17:57:00.000Z | 2019-09-10T08:00:00.000Z | 2026-09-25T08:15:00.000Z | 2026-09-25T08:15:00.000Z | 2026-09-25T08:10:00.000Z | 2026-09-23T08:20:50.040Z |
| ETHUSDT | 2019-11-27T07:45:00.000Z | 2019-11-27T08:00:00.000Z | 2026-09-25T08:15:00.000Z | 2026-09-25T08:15:00.000Z | 2026-09-25T08:10:00.000Z | 2026-09-23T08:20:51.587Z |
| XAUUSDT | 2025-12-11T08:05:00.000Z | 2025-12-11T12:00:00.001Z | 2026-09-25T08:15:00.000Z | 2026-09-25T08:15:00.000Z | 2026-09-25T08:10:00.000Z | 2026-09-23T08:20:53.719Z |
| PAXGUSDT | 2025-03-27T10:30:00.000Z | 2025-03-27T12:00:00.000Z | 2026-09-25T08:15:00.000Z | 2026-09-25T08:15:00.000Z | 2026-09-25T08:10:00.000Z | 2026-09-23T08:20:58.035Z |

`openInterestHist` / ratio endpoints serve only the last ~30 days via REST (older → HTTP 400) and `/fapi/v1/aggTrades` only searches the last **2 days** (error -4166) → longer history comes from Binance Vision (`aggTrades`, `metrics`, `fundingRate`) or our own live collection.


## Mark price / funding / open interest now

| symbol | mark | index | last funding | next funding | open interest |
|---|---|---|---|---|---|
| BTCUSDT | 84205.50000000 | 84243.28652174 | 0.00002945 | 2026-09-25T16:00:00.000Z | 96376.970 |
| ETHUSDT | 2683.87000000 | 2684.77813953 | 0.00005047 | 2026-09-25T16:00:00.000Z | 2284545.321 |
| XAUUSDT | 4284.54249486 | 4282.59107507 | 0.00006259 | 2026-09-25T12:00:00.000Z | 117958.598 |
| PAXGUSDT | 4275.33000000 | 4277.77707456 | 0.00004248 | 2026-09-25T12:00:00.000Z | 23004.379 |


## XAUUSDT trading hours (1h klines, last 21 days)

| weekday (UTC) | hours with bars | avg trades/hour | min trades/hour |
|---|---|---|---|
| Fri | 72 | 64547 | 5548 |
| Sat | 72 | 4358 | 1340 |
| Sun | 72 | 9198 | 2090 |
| Mon | 72 | 55119 | 5125 |
| Tue | 72 | 59604 | 7436 |
| Wed | 72 | 65118 | 8762 |
| Thu | 72 | 68344 | 8915 |


## WebSocket routing (USDⓈ-M)

| stream | /ws (legacy) | /public/ws | /market/ws |
|---|---|---|---|
| btcusdt@bookTicker | 617 | 618 | 0 |
| btcusdt@depth20@100ms | 58 | 58 | 0 |
| btcusdt@aggTrade | 0 | 0 | 29 |
| btcusdt@kline_1m | 0 | 0 | 14 |
| btcusdt@markPrice@1s | 0 | 0 | 6 |
| xauusdt@aggTrade | 0 | 0 | 53 |
| xauusdt@bookTicker | 1145 | 1101 | 0 |

Messages received in 6 s per route. Binance split USDⓈ-M market streams: high-frequency book streams on `/public`, trade/mark/kline/liquidation streams on `/market`. The ingester must route per stream type.


## Liquidation stream sample (!forceOrder@arr on /market, 120 s)

| symbol | events in 120 s |
|---|---|
| ETHUSDT | 16 |
| BTCUSDT | 9 |
| NEARUSDT | 9 |
| DOGEUSDT | 6 |
| XRPUSDT | 6 |
| SOLUSDT | 5 |
| ONDOUSDT | 4 |
| ADAUSDT | 4 |
| ZECUSDT | 4 |
| SEIUSDT | 3 |
| 1000PEPEUSDT | 3 |
| LINKUSDT | 3 |
| AVAXUSDT | 2 |
| JUPUSDT | 2 |
| WLDUSDT | 2 |

Binance pushes at most one liquidation snapshot per symbol per second → the stream is **partial** (flag as such; low weight in analysis).

