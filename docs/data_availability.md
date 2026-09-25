# Actual Data Available per Pair (spec §1.3) — P1.13

Every cell is backed by a measurement in `docs/exploration/*.md` (probe name in brackets).
Status: **preliminary** — P1.6 (MT5 depth after `maxbars=Unlimited`) and P1.11 (gold flow-proxy value) pending.
Last updated: 2026-09-25.

## Sources × data types

| Pair | Instrument (venue) | Candles (TFs, history) | Trades / aggTrades | Ticks (quotes) | Real volume | Order book | Derivatives context |
|---|---|---|---|---|---|---|---|
| **BTCUSDT** | `BTCUSDT` Binance spot — analysis primary | 1m…1w since 2017-08-17, incl. exact taker-buy volume [binance_spot] | aggTrades: Vision since 2017-08 (REST only since 2022-09); live WS [binance_spot, vision] | bookTicker live WS (no history) [binance_ws] | **Yes** (exchange volume, taker side exact) | REST snapshots ≤5000 levels (≈±1.2 %); **no spot history** on Vision [binance_spot, vision] | — |
| | `BTCUSDT` Binance USDⓈ-M perp — flow context | 1m… since 2019-09-08 | aggTrades Vision since 2020-01; REST 2 days only | bookTicker live (Vision history ended 2024-03) | Yes | Vision `bookDepth` (±1…5 % bands) since 2023-01; live REST | funding (since listing), OI & ratios (Vision `metrics` since 2020-09; REST 30 d), mark/index, liquidations (partial stream) [binance_futures] |
| | `BTCUSD@` Windsor MT5 — execution, quote reference | M1…W1 (H1 since 2015-11; M1 depth limited by maxbars, P1.6) [mt5_time] | **No** | bid/ask ticks, complete via polling [mt5_polling] | **No** (tick volume only) | **No** (DOM empty) [mt5_static] | swaps only |
| **ETHUSDT** | `ETHUSDT` Binance spot — analysis primary | 1m…1w since 2017-08-17 | aggTrades Vision since 2017-08 | bookTicker live | **Yes** | REST snapshots (≈±3.4 % at 5000 levels) | — |
| | `ETHUSDT` Binance USDⓈ-M perp — flow context | since 2019-11-27 | Vision since 2020-01 | live | Yes | Vision `bookDepth`; live REST | funding, OI, ratios, liquidations (partial) |
| | `ETHUSD@` Windsor MT5 — execution, quote reference | as BTCUSD@ | **No** | bid/ask ticks | **No** | **No** | swaps only |
| **XAUUSD** | `XAUUSD@` Windsor MT5 — analysis primary + execution | M1…W1; H1 since 2019-02-24 [mt5_time] | **No** | bid/ask ticks (~130 k/day), complete [mt5_polling, bench_prepare] | **No** — `real_volume=0`, `volume_real=0`; only **tick volume** | **No** (DOM empty) | swaps only |
| | `XAUUSDT` Binance USDⓈ-M perp (TRADIFI) — flow context *proxy* | since 2025-12-11 | aggTrades Vision since 2025-12-11 (~1.6 M trades/day); live WS `/market` | bookTicker live | Yes — **but of a different instrument** (perp on Binance, not spot gold) | Vision `bookDepth` since 2025-12-11 | funding, OI, metrics since 2025-12-11 |

## Time & session facts affecting every calculation

- MT5 timestamps are server wall-clock: UTC+2/+3 with **EU** summer time since 2020-02; UTC+0/+1 (US DST) before → converted with `ServerTimeModel` [mt5_time].
- XAUUSD@: daily break 17:00–18:00 New York; weekend Fri 17:00 → Sun 18:00 New York. BTCUSD@/ETHUSD@: weekly maintenance Sat 05:00–08:00 UTC [sessions].
- XAUUSDT perp trades 24/7 (weekend activity ≈ 7–15 % of weekdays) → weekend moves in the perp have no XAUUSD@ counterpart until Sunday's open [binance_futures].
- Local PC clock ≈ 0.2 s behind true time (measured continuously by the recorder) → time sync recommended (H7).

## Preliminary "what is computable" matrix (final version: `docs/capability_matrix.md`, P6.12)

| Analysis | BTCUSDT / ETHUSDT | XAUUSD |
|---|---|---|
| Indicators (EMA/SMA, RSI, MACD, ATR, BB, ADX) | real | real (price-based) |
| VWAP / volume-weighted indicators (MFI, OBV) | real | **approximate** (tick volume) — flagged |
| Market structure, BOS/CHoCH, liquidity sweeps, FVG, order blocks, premium/discount, price action | real (price-based) | real (price-based) |
| Bar delta / CVD | **real, exact** (taker-buy volume in klines) | proxy only via XAUUSDT perp (if P1.11 approves) — flagged *proxy* |
| Footprint (volume at price per candle) | real (aggTrades) | proxy via XAUUSDT perp (price basis-adjusted) or **unavailable**; tick-count-at-price = approximate |
| Volume profile (POC/VAH/VAL) | real | TPO (time-at-price) = real; tick-volume profile = approximate |
| Order-book imbalance | real (live snapshots; futures history via Vision) | proxy via XAUUSDT `bookDepth` or unavailable |
| Funding / OI / liquidations / long-short | real (futures context) | XAUUSDT only (proxy) |
| Spread / execution-cost modelling | real (MT5 ticks for execution venue) | real (MT5 ticks) |
