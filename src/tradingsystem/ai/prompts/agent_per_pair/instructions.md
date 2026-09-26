<!-- prompt: agent_per_pair/instructions · version 3 -->
Analysis cycle for **$pair** at **$now_utc** (UTC). Trigger: $trigger_reason.

The payload below is the desk's prepared screen set (see "Reading the payload"). Its sections:
- `meta` — pair, cycle time, decision timeframe, price reference (analysis instrument; the execution instrument is `meta.execution_instrument`), payload hash, `data_warnings`.
- `account` — equity, currency, execution mode, and `min_position_risk` for the execution instrument (risk of the minimum lot at the minimum stop distance the system accepts).
- `market` — session, analysis-venue price, the execution venue's bid/ask/spread, basis, and `execution.costs` (spread statistics, stops level, swaps, commission, accepted stop distances).
- `capabilities` — which analyses are real / approx / proxy / unavailable for this pair, and why.
- `levels` — reference levels (previous day high/low, day/week open, session ranges).
- `timeframes.<tf>` — per timeframe: recent candles, trend & structure (swings, BOS/CHoCH, sweeps), SMC zones (order blocks, FVGs, premium/discount, liquidity pools), price-action patterns, range state, regime, indicators (EMA/RSI/MACD/ATR/ADX/Bollinger/VWAP).
- `orderflow` — bar delta/CVD, footprint summary (POC, imbalances, absorption) for recent candles, time profile, with `data_quality`.
- `derivatives` — funding, open interest, long/short positioning, liquidations (crypto; gold only via proxy).
- `history` — your recent recommendations for $pair with outcome and, if rejected, the gate's reason.
- `memory` — your own notes from the previous cycle on $pair.
- `performance` — your record on $pair over the last days.

Decide for $pair. Follow the non-negotiable rules. Use `price_reference` = `$price_reference`, timestamp = `$now_utc`, set `valid_until` no later than $max_valid_until, and write `operator_notes` for your next cycle.

```json
$payload
```
