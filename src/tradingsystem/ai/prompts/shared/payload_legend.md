<!-- prompt: shared/payload_legend · version 1 -->
## Reading the payload

- Times are UTC and written `MM-DD HH:MM` in the year of `meta.as_of` (another year is written in full); `meta.as_of` is the cycle time. Fields whose value is unknown are omitted.
- `timeframes.<tf>.recent` rows are `[open_time, open, high, low, close, volume]` (tick volume for MT5-sourced pairs). Higher timeframes list only their last few candles; `structure`, `zones`, `liquidity` and `indicators` summarise the whole analysed history (`bars` candles).
- `timeframes.<tf>.data` is `"ok"` or the data problems of that timeframe (stale, gaps, short history) — treat affected evidence with caution.
- `timeframes.<tf>.structure.events` rows are `[time, kind, dir, level]` (kind: BOS, CHoCH or sweep).
- `timeframes.<tf>.zones.fvg` and `.order_blocks` rows are `[dir, top, bottom, formed, fill_pct, touched (1/0), strength_atr, age_bars]`.
- `capabilities.real` lists the analyses computed from real data; `approx`, `proxy` and `unavailable` map an analysis to the reason it is weaker or missing.
- `account.equity` is the account size the system sizes positions with; open positions and pending orders are not part of the payload yet.
- `market.execution` is the venue that executes orders. Your levels stay in the analysis instrument's prices; the system translates them by `market.basis_exec_minus_analysis` before sending (distances are preserved). `market.execution.costs` gives the venue's spread statistics, stops level, swaps, commission and the stop distances the system accepts (`min_stop_distance`, `max_stop_distance`).
- `memory` holds the notes you wrote for yourself on the previous cycle for this pair (`notes`), with their time and your decision then.
- `performance` is your record on this pair over the last days: cycles, trade ideas, how many the risk gate rejected or executed, and the virtual outcome of every trade idea measured on real prices (TP1 first, stop first, not triggered).
