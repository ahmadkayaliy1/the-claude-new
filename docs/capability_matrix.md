# Capability matrix (generated — `python -m tradingsystem engine --capabilities`)

| analysis | BTCUSDT | ETHUSDT | XAUUSD |
|---|---|---|---|
| price_indicators | **real** — real OHLC of binance_spot:BTCUSDT | **real** — real OHLC of binance_spot:ETHUSDT | **real** — real OHLC of mt5:XAUUSD@ |
| market_structure_smc | **real** — real OHLC of binance_spot:BTCUSDT | **real** — real OHLC of binance_spot:ETHUSDT | **real** — real OHLC of mt5:XAUUSD@ |
| price_action | **real** — real OHLC of binance_spot:BTCUSDT | **real** — real OHLC of binance_spot:ETHUSDT | **real** — real OHLC of mt5:XAUUSD@ |
| liquidity | **real** — real OHLC of binance_spot:BTCUSDT | **real** — real OHLC of binance_spot:ETHUSDT | **real** — real OHLC of mt5:XAUUSD@ |
| volume_weighted | **real** — exchange traded volume | **real** — exchange traded volume | **approx** — tick volume only — broker reports no traded volume |
| bar_delta_cvd | **real** — exact taker-buy volume in Binance klines | **real** — exact taker-buy volume in Binance klines | **unavailable** — no trades / real volume on the broker feed; proxy binance_usdm:XAUUSDT pending validation (P1.11) |
| footprint | **real** — Binance aggTrades (taker side known) | **real** — Binance aggTrades (taker side known) | **unavailable** — no trades / real volume on the broker feed; proxy binance_usdm:XAUUSDT pending validation (P1.11) |
| volume_profile | **real** — traded volume (footprint / kline volume) | **real** — traded volume (footprint / kline volume) | **approx** — tick-volume distribution — not traded volume |
| time_profile_tpo | **real** — time-at-price from real 1m bars | **real** — time-at-price from real 1m bars | **real** — time-at-price from real 1m bars |
| order_book_depth | **real** — REST depth snapshots in ±% bands | **real** — REST depth snapshots in ±% bands | **unavailable** — broker DOM is empty (P1.5) |
| derivatives | **real** — USDⓈ-M perpetual of the same asset | **real** — USDⓈ-M perpetual of the same asset | **proxy** — binance_usdm:XAUUSDT perpetual (different instrument) |
