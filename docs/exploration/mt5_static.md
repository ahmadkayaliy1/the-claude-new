# MetaTrader 5 (Windsor) — symbol specifications (P1.5)

_Generated 2026-09-25T08:25:45.137Z by `research/probes/probe_mt5_static.py` from live/real data. Re-run to refresh._


## Terminal & account

| field | value |
|---|---|
| terminal build | (500, 6182, '5 Sep 2026') |
| connected | True |
| trade_allowed | True |
| tradeapi_disabled | False |
| maxbars | 100000 |
| ping_last (µs) | 101794 |
| server | WindsorBrokers1-Demo |
| trade_mode (0 demo/2 real) | 0 |
| currency | USD |
| leverage | 2000 |
| margin_mode (2=hedging) | 2 |
| limit_orders | 750 |
| margin_so_call / so_so | 100.0 / 20.0 |


## Symbol specifications

| field | XAUUSD@ | BTCUSD@ | ETHUSD@ |
|---|---|---|---|
| digits | 2 | 2 | 2 |
| point | 0.01 | 0.01 | 0.01 |
| trade_tick_size | 0.01 | 0.01 | 0.01 |
| trade_tick_value | 1 | 0.01 | 0.1 |
| trade_tick_value_profit | 1 | 0.01 | 0.1 |
| trade_tick_value_loss | 1 | 0.01 | 0.1 |
| trade_contract_size | 100 | 1 | 10 |
| volume_min | 0.01 | 0.01 | 0.01 |
| volume_max | 20 | 5 | 5 |
| volume_step | 0.01 | 0.01 | 0.01 |
| volume_limit | 0 | 0 | 0 |
| spread | 26 | 2600 | 276 |
| spread_float | True | True | True |
| trade_stops_level | 25 | 2500 | 200 |
| trade_freeze_level | 0 | 0 | 0 |
| trade_mode | 4 | 4 | 4 |
| trade_exemode | 2 | 2 | 2 |
| filling_mode | 1 | 1 | 1 |
| order_mode | 127 | 127 | 127 |
| expiration_mode | 15 | 15 | 15 |
| order_gtc_mode | 0 | 0 | 0 |
| trade_calc_mode | 0 | 2 | 2 |
| margin_initial | 0 | 0 | 0 |
| margin_maintenance | 0 | 0 | 0 |
| margin_hedged | 0 | 0 | 0 |
| swap_mode | 1 | 5 | 5 |
| swap_long | -37.2 | -15 | -15 |
| swap_short | 21.15 | -15 | -15 |
| swap_rollover3days | 3 | 3 | 3 |
| currency_base | XAU | USD | USD |
| currency_profit | USD | USD | USD |
| currency_margin | XAU | USD | USD |
| session_deals | 0 | 0 | 0 |
| session_volume | 0 | 0 | 0 |
| chart_mode | 0 | 0 | 0 |
| path | Metals Prime\XAUUSD@ | CFD Crypto@\BTCUSD@ | CFD Crypto@\ETHUSD@ |
| description | Spot Gold vs US Dollar (1 lot = 100 oz) | CFD BITCOIN vs US Dollar (1 lot = 1 coin) | CFD ETHEREUM vs US Dollar (1 lot = 10 coins) |

| decoded | XAUUSD@ | BTCUSD@ | ETHUSD@ |
|---|---|---|---|
| filling_mode | FOK | FOK | FOK |
| order_mode | MARKET\|LIMIT\|STOP\|STOP_LIMIT\|SL\|TP\|CLOSE_BY | MARKET\|LIMIT\|STOP\|STOP_LIMIT\|SL\|TP\|CLOSE_BY | MARKET\|LIMIT\|STOP\|STOP_LIMIT\|SL\|TP\|CLOSE_BY |
| expiration_mode | GTC\|DAY\|SPECIFIED\|SPECIFIED_DAY | GTC\|DAY\|SPECIFIED\|SPECIFIED_DAY | GTC\|DAY\|SPECIFIED\|SPECIFIED_DAY |


## Profit / margin per 1.00 lot (order_calc_*) at current prices

| symbol | bid | ask | spread | price move | profit BUY 1 lot | profit SELL 1 lot | margin 1 lot | USD per 1.0 price unit per lot |
|---|---|---|---|---|---|---|---|---|
| XAUUSD@ | 4,280.41 | 4,280.67 | 0.26 | 100 | 10,000 | 10,000 | 214.03 | 100 |
| BTCUSD@ | 84,346 | 84,372 | 26 | 1,000 | 1,000 | 1,000 | 168.74 | 1 |
| ETHUSD@ | 2,691.22 | 2,693.98 | 2.76 | 1,000 | 10,000 | 10,000 | 53.88 | 10 |

USD per price unit per lot = contract size for USD-quoted symbols → position size for a given risk is `risk_usd / (|entry − SL| × usd_per_unit)` rounded **down** to `volume_step`.


## order_check (validation only — nothing is sent)

| symbol | request | retcode (0 = would pass) | comment | margin | margin_free after |
|---|---|---|---|---|---|
| XAUUSD@ | market BUY 0.01 with SL/TP | 0 | Done | 2.14 | 155.81 |
| XAUUSD@ | BUY LIMIT 0.01 below | 0 | Done | 0 | 157.95 |
| XAUUSD@ | SL inside stops_level | 10016 | Invalid stops | 0 | 0 |
| BTCUSD@ | market BUY 0.01 with SL/TP | 0 | Done | 1.69 | 156.26 |
| BTCUSD@ | BUY LIMIT 0.01 below | 0 | Done | 0 | 157.95 |
| BTCUSD@ | SL inside stops_level | 10016 | Invalid stops | 0 | 0 |
| ETHUSD@ | market BUY 0.01 with SL/TP | 0 | Done | 0.54 | 157.41 |
| ETHUSD@ | BUY LIMIT 0.01 below | 0 | Done | 0 | 157.95 |
| ETHUSD@ | SL inside stops_level | 10016 | Invalid stops | 0 | 0 |

