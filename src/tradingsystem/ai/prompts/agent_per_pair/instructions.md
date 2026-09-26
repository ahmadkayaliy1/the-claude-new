<!-- prompt: agent_per_pair/instructions · version 2 -->
Analysis cycle for **$pair** at **$now_utc** (UTC). Trigger: $trigger_reason.

The payload below is the desk's prepared screen set. Its sections:
- `meta` — pair, cycle time, decision timeframe, price reference (analysis instrument; the execution instrument is `meta.execution_instrument`), payload hash, `data_warnings`.
- `account` — equity (the configured paper-account equity; live balance, open positions and pending orders are not included yet), currency, execution mode, and `min_position_risk` for the execution instrument (risk of the minimum lot at the minimum stop distance).
- `market` — last bid/ask/spread of the execution venue, analysis-venue price, basis between them, session, whether the market is open, data freshness.
- `capabilities` — which analyses are real / approx / proxy / unavailable for this pair, and why.
- `timeframes.<tf>` — per timeframe: recent candles summary, trend & structure (swings, BOS/CHoCH), SMC zones (order blocks, FVGs, premium/discount, liquidity pools & sweeps), price-action patterns, indicators (EMA/RSI/MACD/ATR/ADX/Bollinger/VWAP), volume profile.
- `orderflow` — bar delta/CVD, footprint summary (POC, imbalances, absorption) for recent candles, with `data_quality`.
- `derivatives` — funding, open interest, long/short ratios, liquidations (crypto; gold only via proxy).
- `history` — your recent recommendations for $pair with outcome so far.

Decide for $pair. Follow the non-negotiable rules. Use `price_reference` = `$price_reference`, timestamp = `$now_utc`, and set `valid_until` no later than $max_valid_until.

```json
$payload
```
