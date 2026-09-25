# Binance spot WebSocket — 30 min measurement (P1.2)

_Generated 2026-09-25T08:53:06.671Z by `research/probes/probe_binance_ws.py` from live/real data. Re-run to refresh._

Clock offset (Binance − local) = 221 ms (RTT 326 ms); latencies below are on Binance's clock. Reconnects: 0.


## Message rates

| stream | msgs | msg/s | KB/s |
|---|---|---|---|
| btcusdt@aggTrade | 10746 | 5.97 | 1.19 |
| btcusdt@bookTicker | 155568 | 86.43 | 12.42 |
| btcusdt@depth20@100ms | 17979 | 9.99 | 13.35 |
| btcusdt@kline_15m | 888 | 0.49 | 0.18 |
| btcusdt@kline_1m | 889 | 0.49 | 0.18 |
| ethusdt@aggTrade | 10782 | 5.99 | 1.19 |
| ethusdt@bookTicker | 94367 | 52.43 | 7.44 |
| ethusdt@depth20@100ms | 17862 | 9.92 | 12.9 |
| ethusdt@kline_15m | 829 | 0.46 | 0.17 |
| ethusdt@kline_1m | 834 | 0.46 | 0.16 |


## Event → receive latency (ms)

| kind | n | p50 | p90 | p99 | max |
|---|---|---|---|---|---|
| aggTrade | 21528 | 159 | 304 | 1454 | 3421 |
| kline_1m | 1723 | 147 | 219 | 1211 | 2990 |
| kline_15m | 1717 | 147 | 219 | 1211 | 2990 |


## Kline close delay (receive − candle end, ms)

| kind | n | p50 | p90 | max |
|---|---|---|---|---|
| kline_1m | 60 | 189 | 250 | 936 |
| kline_15m | 4 | 249 | 250 | 250 |


## depth20 price span (% of mid, best-20 bid to best-20 ask)

| stream | p50 % | p90 % | max % |
|---|---|---|---|
| btcusdt@depth20@100ms | 0.0071 | 0.0093 | 0.0176 |
| ethusdt@depth20@100ms | 0.0194 | 0.0219 | 0.0319 |

