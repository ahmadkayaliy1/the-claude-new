# Binance spot REST — actual data available (P1.1)

_Generated 2026-09-25T08:14:57.668Z by `research/probes/probe_binance_spot.py` from live/real data. Re-run to refresh._


## Latency

| mode | n | p50 ms | p90 ms | max ms |
|---|---|---|---|---|
| cold (new TLS) | 3 | 598.8 | 762 | 762 |
| keep-alive | 20 | 331.6 | 352.5 | 636.6 |


## Symbol filters

| symbol | status | tickSize | stepSize | minNotional | orderTypes |
|---|---|---|---|---|---|
| BTCUSDT | TRADING | 0.01000000 | 0.00001000 | 5.00000000 | LIMIT,LIMIT_MAKER,MARKET,STOP_LOSS,STOP_LOSS_LIMIT,TAKE_PROFIT,TAKE_PROFIT_LIMIT |
| ETHUSDT | TRADING | 0.01000000 | 0.00010000 | 5.00000000 | LIMIT,LIMIT_MAKER,MARKET,STOP_LOSS,STOP_LOSS_LIMIT,TAKE_PROFIT,TAKE_PROFIT_LIMIT |

- Rate limits: REQUEST_WEIGHT 6000/1M; ORDERS 100/10S; ORDERS 200000/1D; RAW_REQUESTS 300000/5M

## Klines (all timeframes)

Fields: `open_time, open, high, low, close, volume, close_time, quote_volume, trades, taker_buy_base, taker_buy_quote, ignore` — `taker_buy_base` gives the **exact** aggressive-buy volume per candle (delta = 2·taker_buy − volume).

| symbol | tf | earliest open | latest open (forming) | req ms | used weight/1m |
|---|---|---|---|---|---|
| BTCUSDT | 1m | 2017-08-17T04:00:00.000Z | 2026-09-25T08:15:00.000Z | 335.9 | 44 |
| BTCUSDT | 5m | 2017-08-17T04:00:00.000Z | 2026-09-25T08:15:00.000Z | 331.4 | 48 |
| BTCUSDT | 15m | 2017-08-17T04:00:00.000Z | 2026-09-25T08:15:00.000Z | 340.8 | 52 |
| BTCUSDT | 1h | 2017-08-17T04:00:00.000Z | 2026-09-25T08:00:00.000Z | 335 | 56 |
| BTCUSDT | 4h | 2017-08-17T04:00:00.000Z | 2026-09-25T08:00:00.000Z | 332.5 | 60 |
| BTCUSDT | 1d | 2017-08-17T00:00:00.000Z | 2026-09-25T00:00:00.000Z | 796.4 | 64 |
| BTCUSDT | 1w | 2017-08-14T00:00:00.000Z | 2026-09-21T00:00:00.000Z | 346.6 | 68 |
| ETHUSDT | 1m | 2017-08-17T04:00:00.000Z | 2026-09-25T08:15:00.000Z | 789.9 | 72 |
| ETHUSDT | 5m | 2017-08-17T04:00:00.000Z | 2026-09-25T08:15:00.000Z | 314.9 | 76 |
| ETHUSDT | 15m | 2017-08-17T04:00:00.000Z | 2026-09-25T08:15:00.000Z | 331.1 | 80 |
| ETHUSDT | 1h | 2017-08-17T04:00:00.000Z | 2026-09-25T08:00:00.000Z | 354.4 | 84 |
| ETHUSDT | 4h | 2017-08-17T04:00:00.000Z | 2026-09-25T08:00:00.000Z | 417.9 | 88 |
| ETHUSDT | 1d | 2017-08-17T00:00:00.000Z | 2026-09-25T00:00:00.000Z | 389.6 | 92 |
| ETHUSDT | 1w | 2017-08-14T00:00:00.000Z | 2026-09-21T00:00:00.000Z | 402 | 96 |

```
{'open_time': 1790324100000, 'open': '84121.75000000', 'high': '84138.59000000', 'low': '84121.74000000', 'close': '84138.58000000', 'volume': '5.41789000', 'close_time': 1790324159999, 'quote_volume': '455798.24654350', 'trades': 587, 'taker_buy_base': '5.35428000', 'taker_buy_quote': '450446.78364830', 'ignore': '0'}
```


## aggTrades

Fields: `a` agg id, `p` price, `q` qty, `f`/`l` first/last trade id, `T` time ms, `m` buyer-is-maker (m=true → aggressive SELL), `M` best-match.

| symbol | first agg id | first time | last 1000 span (s) | ≈ agg/s now | req ms | weight |
|---|---|---|---|---|---|---|
| BTCUSDT | 1489932692 | 2022-09-02T00:00:00.000Z | 107.7 | 9.3 | 666.3 | 102 |

```
{'a': 4073530410, 'p': '84138.58000000', 'q': '0.00061000', 'f': 6712374888, 'l': 6712374888, 'T': 1790324121215, 'm': True, 'M': True}
```


## trades (raw)

```
{'id': 6712374894, 'price': '84138.59000000', 'qty': '0.48666000', 'quoteQty': '40946.88620940', 'time': 1790324122643, 'isBuyerMaker': False, 'isBestMatch': True}
```


## Order book depth (REST snapshot)

| symbol | levels | req ms | weight | bid span % | ask span % | bid qty | ask qty |
|---|---|---|---|---|---|---|---|
| BTCUSDT | 20 | 387.7 | 136 | 0.0048 | 0.0031 | 3.242 | 3.69 |
| BTCUSDT | 100 | 335.5 | 141 | 0.0188 | 0.0165 | 10.702 | 7.412 |
| BTCUSDT | 1000 | 381.8 | 191 | 0.3488 | 0.2021 | 127.279 | 96.333 |
| BTCUSDT | 5000 | 545.8 | 441 | 1.2204 | 1.0941 | 297.979 | 431.071 |
| ETHUSDT | 20 | 856 | 446 | 0.0091 | 0.0103 | 99.029 | 123.392 |
| ETHUSDT | 100 | 359.4 | 451 | 0.051 | 0.0483 | 332.926 | 361.159 |
| ETHUSDT | 1000 | 378.9 | 501 | 0.764 | 0.6553 | 2,152.18 | 2,610.55 |
| ETHUSDT | 5000 | 912.9 | 751 | 3.4248 | 3.2778 | 8,327.21 | 7,863.62 |

Interpretation: the top 20 levels of BTCUSDT span only a few cents to dollars (tick 0.01), i.e. micro-structure noise for a 15m decision; depth for analysis should be measured as liquidity within ±x % bands (like Binance Vision futures `bookDepth`).


## bookTicker

```
{'symbol': 'BTCUSDT', 'bidPrice': '84138.58000000', 'bidQty': '2.99689000', 'askPrice': '84138.59000000', 'askQty': '2.51853000'}
```

