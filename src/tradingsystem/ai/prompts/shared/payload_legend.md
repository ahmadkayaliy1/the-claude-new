<!-- prompt: shared/payload_legend · version 2 -->
## Reading the payload

- Times are UTC and written `MM-DD HH:MM` in the year of `meta.as_of` (another year is written in full); `meta.as_of` is the cycle time. `"none"` means nothing was detected (no divergence, no absorption, no killzone, no pattern). An omitted field means the value is not available — how reliable each analysis is, is stated by `capabilities` and the blocks' `data_quality`.
- `timeframes.<tf>.recent` rows are `[open_time, open, high, low, close, volume]` (tick volume for MT5-sourced pairs). Higher timeframes list only their last few candles; `structure`, `zones`, `liquidity` and `indicators` summarise the whole analysed history (`bars` candles).
- `timeframes.<tf>.data` is `"ok"` or the data problems of that timeframe (stale, missing last bar, short history, `gaps` as `[start time, missing bars]`) — treat affected evidence with caution. A single-timeframe analyst receives the same block under `timeframe.<tf>`.
- `timeframes.<tf>.structure.events` rows are `[time, kind, dir, level]` (kind: BOS, CHoCH or sweep).
- `timeframes.<tf>.zones.fvg` and `.order_blocks` rows are `[dir, top, bottom, formed, fill_pct, touched (1/0), strength_atr, age_bars]`.
- `capabilities.real` lists the analyses computed from real data; `approx`, `proxy` and `unavailable` map an analysis to the reason it is weaker or missing.
- `account.equity` is the configured account size; in demo and live mode (`account.mode`) the system sizes with the broker's live equity. Open positions and pending orders are not part of the payload yet.
- `market.execution` is the venue that executes orders. Your levels stay in the analysis instrument's prices; the system translates them by `market.basis_exec_minus_analysis` before sending (distances are preserved). `market.execution.costs` gives the venue's spread statistics, stops level, swaps, commission and the stop distances the system accepts (`min_stop_distance`, `max_stop_distance`).
- `memory` holds the notes you wrote for yourself on the previous cycle for this pair (`notes`), with their time and your decision then.
- `history[].rejected_by` says who stopped a trade idea: `gate` (the system's risk rules — `gate_reason` lists every failed check in the execution instrument's prices), `broker` or `system` (`reject_reason`).
- `performance` is your record on this pair over the last days: cycles, trade ideas, how many the risk gate rejected, were not placed for other reasons, expired or were executed, and the virtual outcome of every trade idea measured on real prices (TP1 first, stop first, not triggered).
