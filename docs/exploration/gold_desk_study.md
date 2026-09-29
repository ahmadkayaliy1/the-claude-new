<!-- doc: gold desk study · 2026-09-29 -->
# Gold desk study (2026-09-29, read-only) — input to D-049

A four-agent read-only workflow (facts → practice → design → skeptic) run before the owner chose the gold shadow
desk (PROJECT_STATUS D-049). The binding spec is handoff §3.9.1 rows B11–B19 and the "Gold desk (D-049)" rules
under the checkpoint-B table; this file keeps the evidence. Scratch scripts named below lived in the session
scratchpad and are not kept.

## 1. Facts (production data and code, read-only)

## XAUUSD fact report for the gold desk redesign (read-only, 2026-09-29)

### Summary of the main findings

1. **At about $97 equity, XAU cannot trade under the current rules, and changing the stop timeframe alone does not fix it.**
   - With the 15m ATR floor, a stop of 2.9 or less was possible on only **0.9 %** of 5m closes over the last 30 days. Over the last 90 days it was 1.9 %.
   - With a 5m ATR floor, it would be possible on **70 %** of closes (30 days).
   - Even then, a second gate check, `effective_leverage` (max 10x), would refuse every trade. The minimum 0.01 lot is 1 oz, about $4,130 of exposure, which is about **42x** on $97. You only have not seen this failure yet because that check runs only after sizing passes (risk_gate.py:175-178).
   - Clearing it needs about **$413** of equity at 10x, or a separate leverage cap for XAU.
2. **Per-instance risk overrides already work.** `instances.XAUUSD.overrides: {risk: {...}}` is merged over the base config and validated, with no code change. Only `paths`, `pairs`, `instances` and `config_hash` are refused (settings.py:741-745).
3. **Context symbols can be recorded through config alone.** Adding an MT5 instrument with `datatypes: [candles]` under `pairs.XAUUSD` is enough. The catch: the live loop polls ticks for every MT5 instrument regardless of its datatypes.

---

### 1. Market data: `data/hot/mt5/XAUUSD.db` (opened mode=ro)

**Tables:** `xauusd_candles_{1m,5m,15m,1h,4h,1d,1w}` (open_time is int64 UTC ms; srv_time is UTC+3), `xauusd_ticks` (time_msc, bid, ask), `known_gaps` (2000 rows, all `source_no_data`), `forming_candles`, `vision_done`.

**Coverage:**

| Data | Range | Size |
|---|---|---|
| 1m / 5m / 15m / 1h candles | 2019-02-24 23:00 → 2026-09-29 06:02 UTC | 5m: 537,654 bars; 15m: 179,377 bars |
| Ticks | 2026-09-27 22:00 → 2026-09-29 06:04 UTC only | 274,570 ticks (about 1.3 days) |

**How the figures were computed:**
- ATR is **Wilder ATR14** of true range, the same as `indicators.atr = wilder(true_range)` used by the snapshot and the gate. It runs continuously over each timeframe.
- For every closed 5m bar, the "15m ATR" is the ATR of the last closed 15m bar. Hours are the bar's open hour in UTC.
- Candle `spread` is in points (1 point = 0.01 USD), so 22 means 0.22.
- Gold has moved from about $1,300 (2019) to about $4,130, so full-history ATR is not comparable with today's. **Use the 30-day and 90-day columns.**

#### ATR14 and the 2.9 USD stop threshold by UTC hour (last 30 days: 2026-08-30 → 09-29, 252–264 five-minute bars per hour)

"Share ≤2.9" is the share of 5m closes where 0.55 × ATR14 ≤ 2.9, i.e. ATR14 ≤ 5.27.

| Hour (UTC) | Median ATR 5m | Median ATR 15m | Median spread (points) | Share ≤2.9 with 5m ATR | Share ≤2.9 with 15m ATR |
|---|---|---|---|---|---|
| 00 | 3.68 | 6.61 | 25 | 88.6 % | 0 % |
| 01 | 5.15 | 7.41 | 27 | 56.8 % | 0 % |
| 02 | 5.12 | 7.71 | 24 | 62.1 % | 0 % |
| 03 | 4.72 | 7.82 | 24 | 75.8 % | 0 % |
| 04 | 3.77 | 7.49 | 25 | 90.2 % | 1.5 % |
| 05 | 3.84 | 7.26 | 28 | 93.9 % | 3.0 % |
| 06 | 4.55 | 7.93 | 21 | 81.0 % | 0 % |
| 07 | 4.52 | 8.10 | 20 | 96.8 % | 0 % |
| 08 | 4.67 | 8.11 | 21 | 79.4 % | 0 % |
| 09 | 4.24 | 8.03 | 21 | 79.8 % | 0 % |
| 10 | 4.00 | 7.68 | 22 | 84.5 % | 0.4 % |
| 11 | 4.04 | 7.57 | 22 | 84.1 % | 2.0 % |
| 12 | 5.03 | 8.29 | 22 | 59.5 % | 0 % |
| 13 | 6.08 | 9.54 | 23 | 23.8 % | 0 % |
| 14 | 7.03 | 10.44 | 22 | 7.5 % | 0 % |
| 15 | 6.55 | 10.54 | 22 | 9.1 % | 0 % |
| 16 | 5.72 | 10.42 | 23 | 35.3 % | 0 % |
| 17 | 4.87 | 9.77 | 22 | 64.3 % | 4.0 % |
| 18 | 4.32 | 9.15 | 22 | 81.3 % | 2.4 % |
| 19 | 3.95 | 8.55 | 22 | 84.6 % | 0 % |
| 20 | 3.36 | 7.93 | 24 | 93.8 % | 0 % |
| 21 | — | — | — | — | — |
| 22 | 3.12 | 7.25 | 29 | 90.9 % | 4.5 % |
| 23 | 2.91 | 6.53 | 26 | 92.4 % | 3.0 % |
| **All hours** | | | **23** | **70.3 %** | **0.92 %** |

Hour 21 has no bars in the 30-day or 90-day windows: it is the daily break, 21:00–22:00 UTC under US summer time.

**Other windows (all hours combined):**

| Window | Share ≤2.9 (5m ATR) | Share ≤2.9 (15m ATR) | Median spread | Busiest hours, median ATR 15m / 5m |
|---|---|---|---|---|
| 90 days | 72.6 % | 1.89 % | 22 points | 14–15 UTC: 10.2–10.5 / 6.6–7.1 |
| 365 days | 55.4 % | 3.89 % | 22 points | 15 UTC: 11.4 / 7.4 |
| Full history (2019 prices) | 93.4 % | 82.5 % | 13 points | not comparable |

#### Tick spread (ask − bid) by UTC hour, 09-27 22:00 → 09-29 06:04 only

- Overall median **0.27**, p90 0.29.
- Median by hour: 0.24–0.26 during London and New York (08–11 and 15–19 UTC), 0.27–0.29 in Asia (00–05 UTC).
- Rollover: hour 22 median **0.29**, p90 0.33. Hour 20 p90 is 0.31 and its max 1.02.
- The maximum is 0.44–0.52 in every hour.

#### Median bar range by session (bar open hour)

| Session | 15m range, 30 days | 15m range, 90 days | 1h range, 30 days | 1h range, 90 days |
|---|---|---|---|---|
| Asia 00–07 UTC | 7.42 | 7.43 | 16.69 | 15.84 |
| London 07–12 | 7.08 | 6.53 | 14.98 | 13.23 |
| NY 12–17 | **10.30** | **10.06** | **21.37** | **21.33** |
| Late 17–21 | 5.76 | 5.67 | 12.37 | 12.25 |
| Rollover 21–24 | 4.82 | 4.99 | 9.91 | 10.84 |

#### Asia range (00–07 UTC high − low, days with at least 60 five-minute bars)

| Window | Days | Median | p25 | p75 |
|---|---|---|---|---|
| 30 days | 22 | **44.4** | 36.5 | 61.2 |
| 90 days | 64 | 46.4 | 37.5 | 60.5 |
| 365 days | 257 | 49.8 | 37.5 | 68.9 |

#### Spread at rollover (5m candle spread column)

- 21:00–22:30 UTC median is **29 points** against a day median of 23 (30 days).
- 90 days: 29 vs 22. 365 days: 24 vs 22.
- p90 at rollover is 35; p90 over all hours is 29 (30 days).

#### Weekend gap (Friday's last 5m close → Sunday's first open; gaps longer than 40 h)

- 90 days (13 weekends): median absolute gap **11.64**, p90 39.7, max 41.25.
- 365 days (52 weekends): median 11.55, p90 38.8, max 101.66.
- Last six gaps: +11.64, −20.47, −9.38, −11.77, −2.85, −6.86. The market reopens Sunday at 22:00 UTC.

### 2. Code: stop floor, snapshot blocks, settings

Sources: worktree `bad8aad`, whose `src` matches production `0de05ab`, and `config/config.yaml`.

**Which ATR the gate uses**
- `ExecContext.atr` is documented as "ATR(14) of the decision timeframe".
- `executor.py:285-293 atr()` loads the primary instrument's frame at `pairs[pair].decision_timeframe` with the `TF_PLAN` bar count (15m: 320 bars), as of the recommendation's cycle time, and returns `ind.atr(...)[-1]` (Wilder).
- For XAU this is **15m on XAUUSD@** (it is both the analysis and the execution instrument).

**The gate rule (risk_gate.py:145-151)**
- `min_dist = max(stops_level + live spread, sl_atr_min_mult × atr)` and `risk_dist ≤ sl_atr_max_mult × atr`.
- The `spread_vs_sl` check requires spread ≤ 0.2 × stop distance.
- There is no timeframe parameter. Changing the ATR timeframe for XAU means changing `decision_timeframe` or adding code.

**`effective_leverage` (risk_gate.py:175-178)**
- Computes `lots × contract_size × entry / equity` against `max_effective_leverage: 10`.
- It runs **only when `size.ok`**, so it has never appeared for XAU.
- At the minimum lot and equity of $97 it would be about 42.5x and fail.

**How `snapshot.execution_costs()` builds `market.execution.costs` (snapshot.py:60-95)**
- It uses a conservative spread: the largest of the spread now, the 1-hour p90 and the 24-hour p95.
- It then takes the largest of three parts: `stops_level_plus_spread`, `spread_rule` (spread / 0.2) and `atr_floor` (0.5 × decision-timeframe ATR).
- `min_stop_distance` is that value × `STOP_BOUND_MARGIN` 1.10, rounded up. `max_stop` is 5 × ATR / 1.10, rounded down.
- `min_stop_set_by` names the largest part. For XAU it is always `atr_floor`; the ATR comes in as `dec_atr` (snapshot.py:417).
- The 24-hour spread read covers about 300k MT5 ticks, takes about 4 s, and is cached for 10 minutes (snapshot.py docstring). This is relevant to B7's < 3 s target.

**`_account()` builds `account.min_position_risk` (snapshot.py:343-360)**
- Only for execution symbols containing "USD".
- `per_unit = volume_min × contract_size`, which is 1.0 for XAU.
- `sl = the costs' min_stop`.
- `risk_pct = per_unit × sl / equity`.
- `max_stop_within_max_risk = equity × 3 % / per_unit`.

**Overrides and configuration**
- **`RiskCfg` has no per-pair fields.** `PairCfg` has no risk fields either.
- **`InstanceCfg.overrides`** is deep-merged over the base config for `run --instance XAUUSD`. It may not set paths, pairs, instances or config_hash, and `load_settings` validates every instance's merged view.
- So `instances.XAUUSD.overrides.risk.{max_effective_leverage, max_risk_per_trade_pct, sl_atr_min_mult}` works today. It changes only the XAU system's gate and snapshot, because the snapshot reads `self.s.risk`.

**`pairs.XAUUSD` as configured**

| Key | Value |
|---|---|
| asset_class | metal |
| decision_timeframe | 15m |
| timeframes | not set, so the global list: 1m, 5m, 15m, 1h, 4h, 1d, 1w |
| pip_size | 0.1 |
| footprint_bucket | 0.5 |
| flow_proxy_approved | false |

Instruments:
- **mt5 `XAUUSD@`**, roles `[analysis_primary, execution]`, datatypes `[candles, ticks]`, starts `earliest`. Contract: size 100, vmin 0.01, step 0.01, tick 0.01, stops_level 25 points, swap long −37.2 / short +21.15 points per night (about −$0.37 / +$0.21 per night at 0.01 lot), triple swap on Wednesday, commission 0.
- **binance_usdm `XAUUSDT`**, role `flow_context`, datatypes candles, agg_trades, mark_price, funding, open_interest, metrics, from 2025-12-11. Hot file `data/hot/binance_usdm/XAUUSDT.db` is 131 MB.

The global `risk.correlated_groups` is `[[BTCUSDT, ETHUSDT]]`.

### 3. Code: recording context symbols

**How it works now**
- `MT5LiveService` (ingest/mt5/service.py) records **every MT5 instrument of every enabled pair** (`reg.all()` where venue is mt5).
- On connect it runs `select_symbols`, which calls `symbol_select(n, True)`. So hidden symbols are made visible automatically; an unknown symbol raises `MT5Unavailable`.
- The loop runs every 100 ms and has two parts:
  - `_poll_ticks` runs for every sink **with no datatypes check**, so a context symbol would also get its ticks polled and stored.
  - `_poll_rates` runs once per second: `copy_rates_from_pos(0,3)` per timeframe in `inst.timeframes`.
- Each instrument gets its own hot DB, `data/hot/mt5/<FILE_STEM>.db`, with tables `<prefix>_candles_<tf>` and `<prefix>_ticks`. The backfill worker fills `start: {candles: earliest}`.
- Registration is per pair. The registry forbids one symbol appearing under two pairs.

**Cheapest way to add a symbol**
- Add under `pairs.XAUUSD.instruments`: `{venue: mt5, symbol_by_profile: {demo: "EURUSD@", ...}, roles: [...], datatypes: [candles], timeframes: [15m, 1h, 4h, 1d]}`.
- **Gaps to fix:**
  - (a) `Role` is `analysis_primary | flow_context | execution | quote_reference`. There is no macro or context role, and `flow_context` means an order-flow proxy (P1.11). A new role such as `cross_context` is needed.
  - (b) `_poll_ticks` and `_init_cursor` need a `"ticks" in inst.datatypes` guard to stay cheap.
  - (c) The session calendar is chosen by asset class and falls back to `ny_metals_fx`, which is wrong for US500CASH, US100CASH and WTI.
- The ingest process is shared (one ingest-mt5 for all pairs), so adding a symbol requires restarting ingest.

**Futures roll (USINDX.U26 → .Z26)**
- The symbol and file stem are fixed per instrument, so `USINDX.Z26` would write to `USINDX_Z26.db` and history would break at every roll.
- A roll needs one of these:
  - a config edit plus restart each quarter, with the reader stitching files; or
  - a resolver that picks the front month (symbol_info `expiration_time`, `trade_mode ≠ 0`) and writes to a fixed file stem, with a roll event and a price adjustment.
- **EURUSD@ is a cheap USD proxy with no roll** (EURUSD is about 57.6 % of the DXY weights).

**B6 design, per handoff §3.9.1** — a model for an `xau_context` reader:
- `analysis/cross.py` opens the sibling's hot DB `file:…?mode=ro` with a small cache.
- The Instrument is built from `settings.pairs[sibling]` when a `correlated_groups` group names it and the file exists.
- It computes the 96-bar 15m log-return correlation, beta, and the sibling's trend.
- Capability `cross_asset` is real or unavailable; a missing sibling returns unavailable with no exception; budget ≤ 100 ms.
- The same pattern works for `data/hot/mt5/EURUSD.db`, `XAGUSD.db` and so on, once they are recorded.

### 4. Decisions and AI usage (XAU `app.db` and shared `ai_usage.db`, both mode=ro)

**Range:** 2026-09-27 22:22 → 2026-09-29 05:36 UTC, **28 decisions**.

| Status / decision | Count |
|---|---|
| valid / NO_TRADE | 26 |
| valid / SELL (rejected) | 1 |
| error: "cycle deadline of 600s reached — cancelled" (09-29 05:33) | 1 |

- **By session:** asia 10, london_open 7, new_york 3, london 2, none recorded 6.
- **Higher-timeframe bias:** bearish in all 28. Regime: strong_trend 14, range 12, trend 2.

**The one SELL (09-28 17:07, confidence 62)**
- Model: entry 4128.3, stop 4135.5 (distance **7.2** from its entry), one take-profit 4114.5 (reward 13.8, RR 1.9), `risk_percent_suggested` 3.0.
- Its `sl_basis` quotes "venue's min_stop_distance (6.25)".
- Gate: executed entry 4126.44 (market), so the stop distance became 9.06.
  - Passed `sl_min_distance`: 9.06 ≥ max(0.50, 0.5 × ATR 5.67).
  - Passed `sl_max_distance`: ≤ 56.75.
  - Passed `spread_vs_sl` (spread 0.25).
  - **Failed `rr_after_costs`: 1.318 < 1.5** (the fill came 1.86 closer to the take-profit).
  - **Failed `position_size`: 0.01 lot risks 9.12 % ($9.06) > 3 %** at equity 99.36.
  - `effective_leverage` was not evaluated.
- Virtual outcome: `sl_first`, −1.0R.
- A validation error appeared on the recommendation: management.1 "candle_close needs a price > 0".

**The model's recent NO_TRADE notes**
- Each one names the blocker: `min_stop_distance` 3.3–4.33 > `max_stop_within_max_risk` 2.97–3.0.
- They call it the "venue min_stop_distance", which is wrong: it comes from `atr_floor`.
- The plan they state: short a confirmed break of 4110.81, or a failed retest of 4130–4135.

**decision_metrics:** 26 rows, mean `no_trade_counterfactual_atr` 1.60, `rejected_but_virtual_win` 0.

**AI usage per day** (`ai_decisions`; the shared ledger `pair=XAUUSD` gives 28 calls in total)

| UTC day | Calls (ledger) | Input tokens | Cached input tokens | Output tokens | API-equivalent USD | Failed calls |
|---|---|---|---|---|---|---|
| 09-27 (from 22:22 only) | 2 | 46,056 | 0 | 7,228 | 0.26 | 0 |
| 09-28 (full day) | **20** (19 in app.db) | **491,055** | 161,211 | 58,043 | **1.93** | 1 |
| 09-29 (to 05:36) | 6 (7 in app.db) | 147,784 | 47,415 | 15,194 | 0.56 | 0 |

- About 24.5k input tokens per call; `cost_usd` is 0 because calls run on the subscription.
- Ledger totals for comparison: BTC 97 calls / 2.17M input, ETH 94 / 2.16M, XAU 28 / 685k (09-27 → 09-29).

### Derived numbers from the figures above (arithmetic only)

**Equity needed for a 0.01 lot within the 3 % cap = stop floor / 0.03**

| Stop floor | Equity needed at 3 % | Equity needed at the 1 % target |
|---|---|---|
| 15m floor, off-peak (0.55 × 6.5–8 ≈ 3.6–4.4) | $120–147 | $360–440 |
| 15m floor, NY 13–16 UTC (0.55 × 10.4 ≈ 5.7) | about $191 | about $570 |
| 5m floor, overall median (0.55 × about 4.3 ≈ 2.4) | about $79 (fits now) | about $236 |
| Leverage cap 10x (independent of the stop) | ≥ about $413 | ≥ about $413 |

**Structural stops versus the 2.9 cap**
- The model's one real structural stop was 7.2–9.06.
- The median 15m bar range is 4.8–10.3 and the Asia range about 44.
- A 2.9 stop is smaller than one median 15m bar in every session, and only about 11 × spread (0.27).

### What could not be measured

- **Tick spread** covers only 1.3 days (09-27 22:00 → 09-29 06:04); older ticks are in cold parquet, which was not read. The hourly spread by week comes from the candle `spread` column. By MT5 convention I read it as the bar's lowest spread, which would understate the true spread; this is not verified in code or data.
- **The effect of the 1.1 margin combined with a conservative-spread floor** was not simulated per bar. The shares use ATR alone, which is always the binding part for XAU.
- **`snapshot_build_ms` for XAU**: not in the stored payloads (engine.py:671 keeps it in memory only).
- **Correlations with USINDX, EURUSD, XAGUSD or US500**: no history is recorded for them, so none could be computed.
- **Whether `max_effective_leverage` was intended to cover XAU**: no decision-log entry was checked.

Scratch scripts and outputs are in `C:\Users\Ahmed\AppData\Local\Temp\claude\C--the-claude-new\947f289d-f610-4130-91f5-2267ae498817\scratchpad\gold\`; `atr_out.txt` holds the full 90-day, 365-day and full-history tables.

## 2. How professionals trade gold intraday (web research)

## Professional intraday gold (XAUUSD) practice for a small AI-assisted desk

### 1. Sessions and exact UTC times

**Daylight-saving mismatch to watch:** the UK changes clocks on Sun 25 Oct 2026 and the US on Sun 1 Nov 2026. For the week between those dates, London-based and NY-based times move by different amounts. The system should store the rules in local time with a timezone and convert to UTC. It should not hard-code UTC times.

| Event | Local time | UTC in summer (BST/EDT) | UTC in winter (GMT/EST) |
|---|---|---|---|
| Sunday open (CME Globex and most CFD brokers) | 18:00 ET Sun | 22:00 Sun | 23:00 Sun |
| Asia session / range build | Tokyo, Singapore, Shanghai | ~23:00/00:00–07:00 | ~00:00–08:00 |
| Shanghai Gold Exchange auctions *(uncertain, verify)* | 10:15 / 14:15 Beijing | 02:15 / 06:15 | 02:15 / 06:15 (China has no DST) |
| London open | 08:00 London | 07:00 | 08:00 |
| LBMA Gold Price AM auction | 10:30 London | 09:30 | 10:30 |
| COMEX "day" session start (legacy pit hours) | 08:20 ET | 12:20 | 13:20 |
| US data (CPI, NFP, PCE, claims, retail sales) | 08:30 ET | 12:30 | 13:30 |
| NYSE cash open | 09:30 ET | 13:30 | 14:30 |
| ISM, JOLTS, UMich | 10:00 ET | 14:00 | 15:00 |
| LBMA Gold Price PM auction | 15:00 London | 14:00 | 15:00 |
| COMEX gold settlement | ~13:30 ET | 17:30 | 18:30 |
| FOMC statement / press conference | 14:00 / 14:30 ET | 18:00 / 18:30 | 19:00 / 19:30 |
| Daily rollover + Globex maintenance break | 17:00–18:00 ET | 21:00–22:00 | 22:00–23:00 |
| Friday close | 17:00 ET Fri | 21:00 | 22:00 |

- **Best liquidity:** the London–NY overlap, roughly 12:00–16:00 UTC in summer.
- **Rollover (17:00 NY):** spreads widen for about 20–30 minutes, swap is charged, and positions can't be managed during the CME break.
- **Weekend gaps:** Sunday-open gaps are common after geopolitical weekend news. Pros are flat before Friday close, or they size for the gap. Brokers apply the daily break to XAUUSD as well.

### 2. Intraday drivers and how pros combine them

- **US dollar.** Gold is priced in USD, and the WGC/LBMA describe the relationship as strongly negative. Intraday, gold vs DXY is usually **negative, roughly −0.3 to −0.7** on 5–15m returns. *(Uncertain: the size varies by regime, and in 2025–26 gold has decoupled at times.)* Without DXY, **EURUSD is the usual proxy**: it makes up ~58% of DXY, so gold vs EURUSD is **positive**. USDJPY is a second check and often reacts to yields.
- **Real yields / 10-year.** This is the classic driver, but the WGC and LBMA note that the inverse link broke down after 2022. Real rates rose about 250bp and gold still rose, helped by central-bank buying and risk hedging, with R² ≈ 3% in 2022–23. With no bond symbols on the broker, pros use proxies:
  - **USDJPY:** the most yield-sensitive major; rising USDJPY usually means rising US yields, which is bad for gold.
  - The first-minute move in USD pairs after data.
  - An external feed (FRED DGS10/DFII10) for daily context only. It is too slow for intraday use.
- **Silver (XAGUSD).** Correlation is **strongly positive, ~+0.7 to +0.9 intraday**. Pros use it as a confirmation check:
  - A gold breakout that silver also makes is healthier.
  - A new gold high without a silver high (divergence) is a warning sign.
  - The gold/silver ratio is context for regime and risk appetite, not an entry signal.
- **S&P 500.** The sign is **unstable and near zero on average**. In risk-off shocks gold can rise while stocks fall (safe-haven demand), or both can be sold together in a liquidation, as in March 2020. Use it as a regime flag, not a directional input.
- **Combining into a bias (common desk practice, a heuristic rather than a published formula):**
  1. Take the higher-timeframe (H4/D1) trend and structure first.
  2. Add a USD vote from EURUSD, confirmed by USDJPY.
  3. Add a silver-confirmation vote.
  4. Trade only when the votes agree with the structure. When they conflict, stay small or stand aside.

### 3. Gold price-action habits

- **Round numbers.** $10 and $50 levels attract orders, and $100 levels (e.g., $4,100) are major. $5 levels matter on scalps only. Resting stops and take-profits cluster just beyond round numbers, and price often "pokes" through by $1–3 before rejecting.
- **The sequence many desks describe:**
  1. Asia builds a range.
  2. London takes out one side of it (a stop run).
  3. Price either reverses into the range or continues.
  4. NY (12:30–15:00 UTC) often confirms or reverses the London move, especially around US data.

  Retail/SMC sources claim London sweeps the Asia high or low "70%+ of the time". **Treat that figure as unverified marketing.** The system should measure it on its own data before relying on it.
- **News spikes.** In the first 1–5 minutes after data, gold often moves $10–40, sometimes whipsaws both ways, and spreads widen several-fold. Pros don't trade the first candle. They wait for the spike's high/low, then trade a retest or a failed break.
- **Stop placement.** The stop goes behind the sweep wick or structure point, plus a buffer. The buffer should be at least spread + ~0.1–0.25 × ATR, and should clear the nearest round number. Rough sizes at current volatility (5m ATR $3–7, 15m ATR $6–12):
  - **Scalp (5m structure):** ~$3–6 stop.
  - **Intraday swing (15m/1h structure):** ~$8–20 stop.
  - A stop under ~0.5 × ATR of the entry timeframe is mostly noise and gets hit regularly.
- **Targets.** Opposite range edge, the next $10/$50 level, session VWAP, or the prior-day high/low. Pros want at least 1.5–2R after spread.

### 4. Risk on a $100 account with a 1-oz minimum

This is the hard constraint and it should be stated plainly. At 1 oz, a $1 move is $1 of P&L, so:

| Stop size | Loss at 1 oz | Share of a $100 account |
|---|---|---|
| $3 | $3 | 3% |
| $5 | $5 | 5% |
| $10 | $10 | 10% |
| $15 | $15 | 15% |

- **The professional norm is 0.5–1% risk per trade**, which here would be a $0.50–1 stop. That is below the spread-plus-noise floor, so **a textbook-risk trade is impossible on this account**.
- **Spread cost is large relative to risk.** $0.30 of spread is 6–10% of a $3–5 stop.

What a disciplined professional would honestly do:

1. **Set a hard per-trade dollar cap**, e.g., at most $5 (5%, already aggressive). **Reject any setup whose structural stop exceeds the cap.** Never tighten the stop to make a trade fit; that is the most common mistake.
2. **Trade only where small stops are valid.** That means 5m structure in calmer windows: late Asia into London open, and the NY hours after the data spike has settled. Most 15m or higher swing setups ($8–20 stops) simply don't fit, and the system should report that rather than force them.
3. **Daily stop:** 1–2 losses or about $8–10 (8–10%), then stop for the day. Maximum **2 trades per day**. Weekly stop around 15–20%.
4. **No averaging down, martingale, grid, or re-entry to "win it back".** One position at a time.
5. **The honest ceiling.** The expected edge after spread is small and the variance is large. At 5% risk, a normal run of 6–8 losses (routine even at a 45–50% win rate) takes 30–40% off the account. The rational path to "comfortable" gold trading is either more capital (≈$500–1,000 brings 1 oz to ~1% per $5–10 stop) or a broker offering micro lots (0.001 lot = 0.1 oz). Treat the $100 phase as a live test of the process, not income. *(My judgement, consistent with standard risk-management practice.)*

### 5. News: what moves gold and the blackout practice

Rough order of impact on intraday gold (it varies with what the Fed is focused on; when inflation is the focus, CPI tends to matter most):

1. **FOMC decision, statement, press conference, and dot plot** (quarterly).
2. **CPI**, especially core. Moves of 1–2% within minutes are possible.
3. **NFP / employment report**, including average hourly earnings and the unemployment rate.
4. **PCE** (core), which the Fed targets. Often partly pre-empted by CPI/PPI.
5. **PPI, retail sales, ISM manufacturing and services** (10:00 ET), **JOLTS**, and **GDP**. Medium impact.
6. **Weekly jobless claims** (Thursday 08:30 ET). Usually low impact, bigger when the labour market is the Fed's focus.
7. **Fed Chair and governor speeches**, plus geopolitical headlines. These are unscheduled and are handled by volatility and spread filters.

Blackout practice (desk convention, not an official standard):

| Event tier | No new entries from | Resume new entries |
|---|---|---|
| High impact (CPI, NFP, core PCE) | ~15–30 min before | ~15–30 min after, once the spike and spread normalise |
| FOMC | ~30–60 min before 14:00 ET | ~15–30 min after the press conference ends (~15:30 ET) |
| Medium impact | ~5–15 min before | ~5–15 min after |

- Open positions around news are either closed or protected by a stop that already allows for slippage. On a $100 account, flat is the sensible default.
- Resuming should also require the live spread to return near normal (e.g., at most 1.5× the median) and the 1m range to calm down, not just the clock to pass.

### Uncertainties

- The correlation sizes are typical ranges from practice, not measured values for 2026. The system should compute rolling correlations itself.
- The Asia-sweep success rate comes from retail sources and is unverified.
- The SGE auction times should be verified before use.
- Broker session and rollover times vary. Use the broker's own symbol sessions from MT5.

### Sources

- LBMA, "LBMA Precious Metal Prices" (auction times): https://www.lbma.org.uk/prices-and-data/lbma-precious-metal-prices
- ICE Benchmark Administration, "LBMA Gold and Silver Price": https://www.ice.com/iba/lbma-precious-metals
- LBMA, "LBMA Gold Price FAQs": https://www.lbma.org.uk/prices-and-data/lbma-gold-price/lbma-gold-price
- CME Group, "Holiday and Trading Hours": https://www.cmegroup.com/trading-hours.html
- For Traders, "Gold Futures Trading Hours" (GC/MGC schedule and 60-min break): https://fortraders.com/blog/gold-futures-trading-hours
- STARTRADER, "Gold Futures Trading Hours (GC) in ET + Daily Break": https://www.startrader.com/knowledge-intermediate/gold-futures-trading-hours-gc-et-daily-break/
- STARTRADER, "XAUUSD Trading Hours: Weekly Schedule, Sessions & Rollover Times": https://www.startrader.com/knowledge-intermediate/xauusd-trading-hours-open-close-best-times-to-trade-gold/
- FOREX.com, "Trading Rollover FAQs": https://www.forex.com/en-us/help-and-support/rollover/
- FXNX, "Rollover Window: When Swap Posts & Why Spreads Widen": https://fxnx.com/en/blog/rollover-window-when-swap-posts-why-spreads-widen
- Exness Help Center, "Instrument trading hours": https://get.exness.help/hc/en-us/articles/4405235684498-Instrument-trading-hours
- LBMA Alchemist, "An Update on Gold, Real Interest Rates and the Dollar": https://www.lbma.org.uk/alchemist/issue-90/an-update-on-gold-real-interest-rates-and-the-dollar
- World Gold Council, "Gold Market Commentary: When the dollar turns on itself" (Feb 2026): https://www.gold.org/goldhub/research/gold-market-commentary-february-2026
- World Gold Council, "Gold Demand Trends Q2 2026 – Outlook": https://www.gold.org/goldhub/research/gold-demand-trends/gold-demand-trends-q2-2026/outlook
- NordFX, "What Moves Gold Price" (CPI/PCE/NFP/FOMC impact): https://nordfx.com/en/traders-guide/fundamental-drivers-and-economic-news-that-move-gold-xauusd
- NordFX, "Best Time to Trade Gold (XAUUSD)": https://nordfx.com/traders-guide/best-time-to-trade-gold-xauusd-sessions-volatility-news
- Pro-Scalper, "XAUUSD Trading Sessions: When Does Gold Move Most?" (London–NY overlap): https://www.pro-scalper.com/xauusd-trading-sessions
- Aron Groups, "CPI and Gold": https://arongroups.co/forex-articles/cpi-gold/
- GrandAlgo, "Asian Session Trading Strategy: How London Sweeps the Asia Range" (source of the unverified 70% claim): https://grandalgo.com/blog/asian-session-trading-strategy
- FXNX, "Master XAUUSD Liquidity Sweep Strategies": https://fxnx.com/en/blog/master-xauusd-liquidity-sweep-strategies-stop-being-fuel
- FXNX, "London/NY Overlap: A Goldmine XAU/USD Strategy": https://fxnx.com/en/blog/london-ny-overlap-goldmine-strategy-xau-usd

## 3. Design draft — SUPERSEDED where it differs from handoff §3.9.1 B11–B19 (its option B, the 45× scalp desk, was rejected by D-049)

## Gold desk for Phase 5 checkpoint B: XAUUSD design (read-only study, 2026-09-29)

### A. Diagnosis

1. **Two separate things block gold.**
   - Stop floor: the floor is 0.5 × ATR14 of the **15m** timeframe × 1.1, which is 3.3–6.6. The widest stop the 0.01 lot allows under the 3 % cap is 2.92 at $97. That floor fits on only 0.9 % of 5m closes (30 days).
   - Leverage: 1 oz ≈ $4,130 of exposure, which is **42.5×** on $97 against `max_effective_leverage: 10`. This check only runs after sizing passes (risk_gate.py:175), so it has never shown up, but it would refuse every XAU trade even after the stop floor is fixed.
2. **The floor is ours, not the broker's.** Windsor's minimum is `stops_level` 0.25. The payload says `min_stop_set_by: atr_floor`, but the model calls it the "venue min_stop_distance", and the daily review repeated that. The mislabel pushes the model toward "nothing can be done", when the real lever is a rule we own.
3. **The model's natural setups are 15m swing ideas with 7–9 stops.** The one SELL had a 9.06 stop, which is 9.1 % risk at the minimum lot. No arrangement at $97 and 3 % allows that trade. Only 5m-structure stops of 2.0–2.9 can fit.
4. **Money is spent even though nothing can fit:** 24 calls and about 590 k input tokens a day, 26 of 28 answers NO_TRADE.
5. **Conclusion:** gold can trade on this account only as a small, intraday, 5m-structure desk with a leverage exception for XAU alone. Otherwise it should not trade at all until equity reaches about $413. There is no honest middle ground.

### B. Risk arrangement

The cap is equity × 3 % ÷ $1 per $1 move, which is **2.92 at $97.33**. The floor is 0.55 × ATR14 of the stop timeframe (0.5 × ATR with the 1.1 margin).

| | A. Watch only (status quo, done properly) | **B. Gold scalp desk (recommended)** | C. Wider stops or another venue |
|---|---|---|---|
| Stop floor | 15m ATR: 3.6–5.7 | 5m ATR: 2.0 (Asia) to 2.5 (London); NY 13–16 UTC 3.3–3.9, so no fit | 15m structure, 4–9 |
| Hours/day a trade can fit | ≈ 0.2 h (0.9 %) | ≈ 16 h on ATR alone (sum of hourly shares: 70 %). **≈ 14 h** after the rollover, Sunday-open and news blocks. NY 13–15 UTC fits 8–24 % of the time | — |
| Stop / risk per trade | — | 2.0–2.92 stop, so **2.1–3.0 %** risk | 4–9 % (breaks the 3 % cap) |
| Leverage | 10× cap: refused until equity ≈ $413 | XAU-only cap 45×. Gold stops by itself below equity ≈ $91.8 (4130 / 45) | — |
| Trades / day | 0 | at most 2 entries, stop after 1 loss; expect 0–2 | — |
| Downside | No gold trading or learning at $100. Needs about 4× the capital or a broker with micro lots | Loosens one limit (leverage) for XAU. The 1-oz exposure is exposed to gaps and slippage. A 2–2.9 stop is only 0.5–0.8 × ATR5m, so noise hits it often. The spread is 9–13 % of the stop. The 1–3 min answer latency hurts 5m entries | C1 breaks D-046 (d). C2 (XAUUSDT perp, small size) needs the P7.4 Binance backend, basis and funding handling. Not in checkpoint B |

**Recommendation: B, run first in shadow mode, demo only, and only with the owner's decision (H31).** `max_risk_per_trade_pct` stays 3 and `max_daily_loss_pct` stays 10, both unchanged. The one limit B loosens is `max_effective_leverage`, and for XAU only. It is paired with controls that remove the risk the leverage cap exists for: the position is never held overnight, over a weekend or into news, and it has a hard server-side stop.

#### B.1 Config: `pairs.<PAIR>.desk` (new `DeskCfg` in settings.py; `PairCfg.desk: DeskCfg | None = None`)

```yaml
pairs:
  XAUUSD:
    desk:                                   # committed default: mode off (identical to today)
      mode: off                             # off | shadow | trade
      brief: desks/xau                      # prompt appendix file (section C.6)
      stop_atr_timeframe: 5m                # {5m, 15m}; the ATR the SL floor/max use (gate and costs)
      min_rr: 2.0                           # must be ≥ risk.min_rr
      min_confidence: 60                    # must be ≥ risk.min_confidence
      max_entries_per_day: 2                # 1..5, per gold day (rolls 17:00 America/New_York)
      max_losses_per_day: 1                 # 1..3
      max_loss_pct_per_day: 4.0             # ≤ risk.max_daily_loss_pct
      max_effective_leverage: 45            # ≤ 50 hard bound; the only key allowed above the base value
      market_entry_max_slip_atr: 0.2        # MARKET: |touch − model entry| ≤ 0.2 × ATR(stop TF) ≈ 0.6–0.9
      fit_min_band: 0.3                     # cap − floor must leave ≥ 0.3 (≈ one spread) for a real stop
      timezone: America/New_York
      flat_at: "16:45"                      # daily: close the XAU position, cancel pending orders
      no_entry_from: "16:00"                # until reopen_grace after the 18:00 reopen
      reopen_grace_min: 15                  # Sunday: 60
      require_fresh_news: true              # calendar stale/absent → no XAU entries (fallback in C.4)
      skip_entry_calls_when_no_fit: true    # B12 in desk terms (section D)
```

**Validation** (`Settings` model_validator; the load fails on any breach):
- `desk` is allowed only on a pair whose `asset_class` is `metal`.
- `min_rr ≥ risk.min_rr`, `min_confidence ≥ risk.min_confidence`, and `max_loss_pct_per_day ≤ risk.max_daily_loss_pct`.
- `max_effective_leverage ∈ [risk.max_effective_leverage, 50]`.
- `stop_atr_timeframe ∈ {5m, 15m}`, and it must be ≤ `decision_timeframe`.

**Hardening:** `instances.*.overrides.risk` may only tighten each `RiskCfg` field (same comparisons). The override is a raw deep merge today (settings.py:741) and nothing uses it for risk, so this closes a back door without breaking anything.

#### B.2 Gate and executor changes (active only when `desk.mode != off`)

- **`executor.atr()`** uses `desk.stop_atr_timeframe` with that timeframe's `TF_PLAN` bar count. The existing `sl_min_distance`, `sl_max_distance` and `spread_vs_sl` checks stay textually identical; they just receive a different `ctx.atr`. `snapshot.execution_costs()` gets the same ATR and reports it as `stop_atr_tf`.
- **`effective_leverage`** uses cap = `desk.max_effective_leverage`. It is also evaluated, report-only, at `volume_min` when sizing fails. That can only add a failing line to an already refused trade.
- **New checks, AND-ed with the rest (they only add, never replace):**
  - `desk_rr`: the post-split RR must be ≥ 2.0.
  - `desk_confidence`: ≥ 60.
  - `desk_entries_today`, `desk_losses_today`, `desk_day_loss`: realised gold-day loss + this trade's risk ≤ 4 %.
  - `desk_window`: blocked from 16:00 ET to reopen + grace, and on Sunday until 19:00 ET.
  - `desk_market_slip`.
  - `desk_news_fresh`, plus B8's `news_blackout`.
  - `desk_mode`: in shadow it always fails with "shadow"; the idea is still recorded, and the virtual-outcome machinery scores it.
- **Daily flat job** (executor, per cycle and at `flat_at`): close the XAU positions of this magic and cancel pending orders. This is protective, so it runs even with the kill switch engaged. A failure retries every cycle and notifies. Nothing is held across rollover, swap (−$0.37/night long), the weekend gap (p90 $39.7 = 41 % of equity at 1 oz) or CPI/FOMC.
- **Snapshot / system vars:** `min_rr` and `min_confidence` for XAU come from the desk. These are existing placeholders with a constant value per pair, so the prompt cache is not disturbed.

#### B.3 Proof it cannot loosen BTC/ETH

1. **Schema:** a `desk` block on a non-metal pair fails to load. BTC/ETH `desk` is always `None`.
2. **Code:** every new branch is guarded by `desk is not None and desk.mode != "off"`. Test `test_gate_desk_none_identical`: a frozen copy of today's `evaluate()` and the new one return identical `checks`, `approved` and `size` on a corpus that includes every stored BTC/ETH gate input from the three instance DBs, plus randomised contexts. Test `test_btc_eth_prompt_unchanged`: the rendered system prompt for BTC/ETH equals the B7 version, with no appendix.
3. **Isolation:** each pair runs in its own process (D-042) and reads `s.pairs[pair].desk`. The global `RiskCfg` is never mutated.
4. **The instance-override guard** removes the only other path.

Note, unchanged: `max_daily_loss_pct` applies per instance (magic). The desk's 4 %/day makes gold's share of the account's day smaller, not larger.

### C. Analysis upgrades a gold trader expects

#### C.1 Intermarket block `market.intermarket` (XAU only)

- **Record through config.** Add these under `pairs.XAUUSD.instruments`:
  - `EURUSD@`: USD proxy, no futures roll, 57.6 % of DXY. Gold moves the same way.
  - `XAGUSD@`: silver confirmation.
  - Both with `role: cross_context`, `datatypes: [candles]`, `timeframes: [5m, 15m, 1h, 4h, 1d]`, `start: {candles: -400d}`.
- **Code gaps to fix:** a new `Role` literal; a `"ticks" in inst.datatypes` guard in `_poll_ticks` / `_init_cursor`; `reg.primary()` and similar lookups must ignore the new role. `ny_metals_fx` is the right calendar for both symbols.
- **Reader:** B6's `cross.py`, generalised so that a sibling is either the `correlated_groups` member (BTC/ETH) or a `cross_context` instrument (XAU).
- **Per symbol:** `chg_pct {1h, 4h, session}`, `trend_15m/1h`, `corr_96x15m`, `beta`, `corr_30d_1h`.
- **XAGUSD also gets** `confirms_xau_extreme`: whether silver made the same session high/low within 3 bars.
- **Votes:**
  - `votes {usd: ±1/0, silver: ±1/0, agree_with_htf: bool}`.
  - A vote becomes 0 when |corr| < 0.3, and the block says so; gold has decoupled from the dollar at times.
  - Votes are information only, **never a gate input**.
- **Cost and failure:** ≤ 100 ms, cached per closed 15m bar. A missing DB makes the capability unavailable, with no exception. About 170 tokens.
- **Deferred:**
  - USINDX: needs a front-month resolver, a fixed file stem and roll events.
  - US500CASH: its sign is unstable and its session calendar is wrong.
  - USDJPY@: yield proxy; not in the lead's list; one config line later if it exists.

#### C.2 Gold clock `market.gold_clock` (about 90 tokens; zoneinfo, never hard-coded UTC)

- NY time and the session.
- Asia range 00–07 UTC: high, low, width, width ÷ 30-day median (44.4), and whether it is complete.
- `london_swept {asia_high, asia_low, at}`.
- Next events in UTC: London open, LBMA AM 10:30 London, COMEX 08:20 ET, US data 08:30 ET, NYSE 09:30 ET, LBMA PM 15:00 London, COMEX settle 13:30 ET, rollover 17:00 ET.
- `entry_window: open | closed:<reason>` and `flat_at`.
- The UK/US DST mismatch (2026-10-25 → 11-01) is a unit test.

#### C.3 Levels

- `levels.round {r10: [..], r50: [..], r100: [..]}` with distance in ATR5m, about 40 tokens.
- B2 PW/PM levels apply unchanged.

#### C.4 News (B8) for gold

- **Tiers:**
  - High (CPI, core PCE, NFP): −30/+30 min.
  - FOMC: −60 min, until 45 min after the press conference starts.
  - Medium (PPI, retail sales, ISM, JOLTS, GDP): −10/+10.
  - Config: `news_blackout.tiers: {high: [30,30], fomc: [60,75], medium: [10,10]}`.
- **Resume only after the clock has passed and** the spread is ≤ 1.5 × its 1-hour median.
- **Flat before events:** open XAU positions and pending orders are closed or cancelled 5 min before a high or FOMC event.
- **Stale feed:** for **desk entries only**, a stale feed fails closed (the owner's money-path rule). This deviates from B8's "stale → fail open" and needs a D entry. BTC/ETH are unaffected.
- **No usable feed:** static fallback windows on weekdays, 08:15–08:50 and 09:45–10:15 ET, plus FOMC dates from a config list.
- **Payload:** `market.news` with the next 2 events, about 60 tokens.

#### C.5 B1–B6 and B9 applied to XAU

| Item | For XAU |
|---|---|
| B1 depth | n/a (no book). Collapse XAU's unavailable capability lines into one (−80 tokens) |
| B3 forming bar | 5m and 15m, useful |
| B4 profiles | approx from tick volume, 3 days, ≤ 100 tokens |
| B5 session stats | gold sessions (Asia / London / NY / late) plus the **measured** share of days where London takes the Asia high or low and closes back inside, over 30 and 90 days, causal. This replaces the unverified "70 %" claim |
| B6 | as generalised in C.1 |
| B9 | if H25 fails, compress the XAUUSDT proxy `derivatives` block to one line (funding rank, OI 24h change): −130 tokens |

#### C.6 Gold desk brief (`ai/prompts/desks/xau.md`, versioned header, ≈ 330 tokens)

> **Gold desk brief (XAUUSD).**
> - **Account reality.** 0.01 lot = 1 oz, so a $1 move = $1. `account.min_position_risk.fit_band` is [system floor, risk cap]. The floor is the SYSTEM's 0.5×ATR14(5m) rule; the venue's own minimum is only `stops_level`. A trade exists only if the structural stop falls inside the band. Never tighten a stop to fit: answer NO_TRADE and state the band you would need.
> - **Routine:**
>   1. Bias from 4h/1d structure.
>   2. Location: Asia range edges, PDH/PDL, PW/PM, $10/$50/$100 numbers. Stops cluster $1–3 beyond round numbers.
>   3. Confirmation: `intermarket.votes`. EURUSD up supports gold; silver must print the same break. A vote whose correlation is flagged weak does not count. Trade only when bias, location and at least one confirmation agree. Conflicting votes mean NO_TRADE.
>   4. Session: Asia builds the range; London often runs one side. Trade the failed break back inside, or a closed 5m break and retest, not the first push. Never the first candle after US data: wait for the spike's high and low and trade its retest once `market.news` clears.
>   5. Entry: prefer LIMIT or STOP orders at the level. Your answer arrives 1–3 minutes after the screen; use MARKET only when price is at your entry now.
>   6. Stop behind the sweep wick or structure, plus spread, plus a buffer, and beyond the round number.
>   7. Targets: opposite range edge, the next $10/$50 level, PDH/PDL; ≥ 2R after spread.
> - **Intraday only.** Gold positions close at 16:45 New York. No entries near rollover, the Sunday open or news (the gate enforces this). At most 2 entries and 1 loss per gold day.

**How it enters the prompt:**
- Add `render(role, system_vars, user_vars, appendix: str | None = None)`. It appends the versioned file after the legend at the end of the system prompt and records its version in `prompt_versions`. The library hash covers it through `rglob`.
- The orchestrator passes `pairs[pair].desk.brief` only when the desk mode is not off.
- **No new placeholder.** The text is constant per pair, so the cache prefix stays stable; there is one cache miss at deploy.
- **Rule 3 of core_rules** changes from "ATR($decision_tf)" to "ATR of `market.execution.costs.stop_atr_tf`". This rides on B7's version bump.
- **Legend v5:** "`min_stop_set_by: system_atr_floor` = our rule; the venue minimum is `stops_level`". The review prompt gets the same line.
- **Rejected alternatives:**
  - A new `$desk` placeholder: forbidden.
  - The playbook: adaptive, in the user message, not versioned.
  - Overriding `persona` through system_vars: skips version recording.

#### C.7 Token budget for XAU (≈ 3.6 JSON characters per token)

| Block | Δ tokens |
|---|---|
| 1m view dropped (decision c) | −600 |
| capabilities collapsed | −80 |
| XAUUSDT derivatives compressed (if B9 fails) | −130 |
| B2 +40, B3 +45, B4 +100, B5 gold +60 | +245 |
| gold clock +90, round numbers +40, news +60, intermarket +170, fit fields +45 | +405 |
| **Payload net** | **≈ −160** |
| Brief (+330) + legend and review lines (+60), system prompt, cached | +390 |
| **Per call** | **≈ +230** |

B7 acceptance for XAU: a dry build (no call) with the payload Δ ≤ 0 and total input ≤ today's median + 0.5 k.

### D. B12 in the desk's terms

An **entry call** happens only when all of these hold at the cycle:

```
desk_fit = (cap − floor ≥ fit_min_band)          # floor = 1.1 × max(stops+spread_cons, spread_cons/0.2, 0.5×ATR14(stop TF))
           ∧ leverage(volume_min) ≤ desk cap       # cap = equity × max_risk% / usd_per_unit_at_min_lot
           ∧ entry_window open ∧ news fresh ∧ outside blackout ∧ spread ≤ 1.5 × 1h median
           ∧ entries_today < 2 ∧ losses_today < 1 ∧ day_loss < 4 %
```

- **Otherwise:**
  - The trader trigger is suppressed and the setup signature advances, as in A5.
  - The screen is logged `skipped: no_fit` with floor, cap, the reason and the leverage value.
  - The review pack counts these skips per reason.
- **Kept:** review and event calls while a position or pending order is open (as approved), and the flat job.
- **Without a desk (BTC/ETH):** the plain B12 rule applies, i.e. `min_position_risk` at the minimum stop ≤ max risk.
- **Expected effect:** XAU drops from about 24 to about 12–16 calls/day, all of them in hours when a trade can exist (to be measured, M4).

### E. Checkpoint-B rows

| # | Component | Files | Rule / acceptance | Tests | Effort |
|---|---|---|---|---|---|
| B13 | Honest stop labels and fit fields | `analysis/snapshot.py` (`execution_costs`, `_account`), `payload_legend.md`, review prompt | `min_stop_set_by ∈ {system_atr_floor, venue_stops_plus_spread, spread_rule}`; `stop_atr_tf`; `min_position_risk.{fit_band, fits_now, leverage_at_min_lot, leverage_cap, equity_for_min_lot}`; all pairs, ≤ 45 tokens | values on stored XAU and BTC payloads; label text | 0.25 d |
| B14 | `DeskCfg` + validators + tighten-only instance overrides | `core/settings.py`, `config/config.yaml` (mode off) | section B.1; load fails for desk on non-metal pairs or on any looser value except the two named keys; `config.local.yaml` deep merge of `pairs.XAUUSD.desk` works | `test_desk_cfg.py` (bounds, asset-class guard, override guard, local merge) | 0.5 d |
| B15 | Desk gate, stop-TF ATR, leverage cap, daily flat | `execution/{risk_gate,executor}.py`, `analysis/engine.py` | section B.2; shadow never places anything; flat closes and cancels at 16:45 ET with the kill switch on; fail closed on missing counters | `test_gate_desk.py`, `test_gate_desk_none_identical` (golden over stored BTC/ETH inputs), `test_desk_flat.py` (DST weeks, Friday, retry) | 1.25 d |
| B16 | No-fit call suppression (= B12 for desk pairs) | `ai/triggers.py`, `analysis/engine.py` | section D; review/event calls kept while holding; key `skip_entry_calls_when_no_fit` | replay: suppression only when the fit rule is false; holding → calls kept | 0.25 d (on top of B12) |
| B17 | Context instruments and intermarket block | `core/settings.py` (Role), `ingest/mt5/service.py` (ticks guard), `analysis/cross.py`, config | section C.1; ≤ 100 ms; unavailable without an exception; ingest RAM Δ ≤ 30 MB | `test_cross.py` (context sibling, weak-corr zero vote), ticks-guard test | 1 d |
| B18 | Gold clock, Asia range and sweep, round numbers, gold session stats | `analysis/context.py`, `snapshot.py` | sections C.2, C.3, C.5 B5; causal; ≤ 190 tokens | DST mismatch week, causal sweep, round-number edges | 0.75 d |
| B19 | Desk brief and render appendix | `ai/prompts/__init__.py`, `ai/orchestrator.py`, `desks/xau.md`, `core_rules.md` rule 3 | section C.6; BTC/ETH system prompt unchanged by the appendix code; XAU stable across cycles | `test_prompt_appendix.py`, `test_prompt_versions` | 0.5 d |
| B20 | Desk replay (measurement gate) | `tools/desk_replay.py` → `docs/measurements/gold_desk.md` | M1–M6 below; read-only, idle time, one DB at a time | fixture replay | 0.75 d |

**Build order:**
1. B11.
2. B13.
3. B14.
4. B15 + B12/B16 (the money path, reviewed as one).
5. **B20.** If M2's fit share of allowed 5m closes is < 2 %, stop gold feature work here and ship option A (mode off, B12 suppression only).
6. B1–B5 with B18 folded into B5.
7. B6 + B17.
8. B8 with the C.4 tiers.
9. B19 + B7: one version bump; the ONE live call stays BTC; XAU gets a dry build and a render only.
10. B9, B10.

Extra effort: about 5.25 d.

**Measurements before enabling (B20):**

| # | What it measures | Method |
|---|---|---|
| M1 | fit on every stored XAU payload | with the 5m ATR as of each payload and that payload's equity |
| M2 | 90-day 5m replay | a structural-stop proxy (last confirmed 5m swing + spread + 0.1 × ATR5m); share of closes whose proxy stop is within [floor, cap], by hour; "2R before stop" labelled descriptive, not evidence of an edge |
| M3 | re-gating all stored XAU decisions with the desk rules | the SELL must still be refused (9.06 > 2.92; RR 1.318 < 2) |
| M4 | `replay_triggers.py` | XAU calls/day with and without suppression |
| M5 | tokens | dry payload and render per block (section C.7) |
| M6 | XAU `snapshot_build_ms` | with intermarket |

**Owner rows:**
- **H31 (decision, a D entry):** approve option B, meaning the XAU-only leverage cap of 45× and a demo-only gold desk. Declining means option A. The decision includes the XAU-only stale-feed fail-closed rule.
- **H32 (config.local.yaml, after H26b and M1–M5):**
  ```yaml
  pairs:
    XAUUSD:
      desk: {mode: shadow}
  ```
  After at least 3 gold days in shadow with ≥ 1 desk-approved idea and 0 rule breaches, change it to `{mode: trade}`, still on demo. XAU stays out of go-live (H9).
- **H26b:** ingest restart for the two context symbols, handled by `restart_all`; the session states it.

**Rollback switches:**
- `desk.mode: off`: XAU returns exactly to today's behaviour; restart the XAU instance.
- `skip_entry_calls_when_no_fit: false`.
- Remove the `cross_context` instruments: the capability becomes unavailable; restart ingest.
- `news_blackout.enabled: false`.
- All of these are listed in ops_windows §1b/§8.

### F. What not to do, and why

- **Don't raise `max_risk_per_trade_pct` or the 10 %/day, globally or for gold, and don't loosen risk through `instances.*.overrides`.** D-046 (d) forbids it. At 5–9 % per trade, a normal run of 6–8 losses takes 30–50 % off the account.
- **Don't let the model tighten a stop to fit the band.** A stop inside noise is the most common way small accounts bleed. The brief and rule 3 say NO_TRADE instead.
- **Don't switch XAU's `decision_timeframe` to 5m.** It changes the triggers and the call pressure; only the stop's ATR timeframe needs to change.
- **Don't hold gold overnight, over the weekend or into CPI/FOMC.** At 1 oz, the p90 weekend gap is 41 % of equity, and news spikes of $10–40 go straight through a $3 stop.
- **Don't make intermarket votes or session statistics gate inputs, and don't trust the "London sweeps Asia 70 %" claim.** Show measured numbers only; the correlations drift.
- **Don't add USINDX (roll handling), US500 (unstable sign, wrong calendar) or bond/yield feeds (FRED is daily and too slow) now.** EURUSD@ and XAGUSD@ cover the dollar and confirmation.
- **Don't enable the XAUUSDT flow proxy unless B9 passes (H25), and don't execute gold on Binance before P7.2/P7.4.**
- **Don't bring back the 1m view, MQL5, the persistent session or MCP for gold.** Owner decisions (c) and (a)/(b).
- **No averaging down, grids or re-entry "to win it back".** One gold position; one loss ends the gold day.
- **Don't put XAU in the first live pair.** ETH first per D-046; gold stays on demo until B8, B9 and the shadow evidence exist.

**Sources and scratch files:**
- Read-only sources: `C:\the_claude_new_wt\phase5\docs\handoff_operator_v2.md` (§3.9, §3.9.1) and `C:\the_claude_new_wt\phase5\PROJECT_STATUS.md` (P12.5, D-046…D-048). Code read in `C:\the_claude_new_wt\phase5\src\tradingsystem\`: `execution/risk_gate.py`, `core/settings.py`, `analysis/snapshot.py`, `ai/orchestrator.py`, `ai/prompts/`.
- My scratch script `C:\Users\Ahmed\AppData\Local\Temp\claude\C--the-claude-new\947f289d-f610-4130-91f5-2267ae498817\scratchpad\gold\paysize.py` measured the latest stored XAU payload read-only: 31.0 k characters, 1m block 3.2 k, `capabilities` 1.06 k, the XAUUSDT `derivatives` proxy block 0.75 k.

## 4. Skeptic review of the draft (its changes are folded into §3.9.1)

1. **High: the design's own numbers show 45× leverage is not a sound trade-off, and D-046 (d) points the other way.**
   - Evidence: D-046 (d) says the D-036 limits are not raised. It also says ideas whose 0.01 lot risks more than 3 % are "refused by design (no craziness)". The design's cap stops gold at equity $91.8. From $97.33 that leaves $5.5, or about 2 stop-outs at $2.9, before the desk switches itself off.
   - `ctx.equity` is the whole account's equity (`executor.py:367` `acct["equity"]`), so BTC/ETH losses also stop gold.
   - At 45×, a 2.2 % move (about $91) takes the whole account. The 365-day maximum weekend gap is $101.66.
   - Change: make option A (watch only, plus shadow) the recommendation. Split H31 into two rows. H31a approves shadow mode, which needs no leverage change: the gate can evaluate the desk rules and simply never place. H31b, the leverage exception, is asked only after B20 and shadow show an edge.

2. **High: tight stops lose to the spread and to latency, and the design never measures this.** A read-only replay (`rv_sim.py`, 90 days of 1m bars) used random entries from 5m closes, excluding 20–23 UTC, with a 0.27 spread, bid/ask-correct triggers and a 2R target:

   | Stop | Share reaching 2R first | Expectancy | Losses hit within 15 min | Median time to stop |
   |---|---|---|---|---|
   | 2.0 | 0.282 / 0.306 | −0.154 / −0.081 R | 93 % | 3 min |
   | 2.9 | 0.295 / 0.325 | | 80 % | 6 min |
   | 9.0 | | | 15–17 % | about 50 min |

   - First figure is BUY, second is SELL; break-even is 0.333.
   - XAU model latency in `ai_decisions.latency_ms` (n = 29) is p50 37 s, p90 114 s and max 147 s, plus about 4 s of snapshot build.
   - With a 2–2.9 stop, the median time to stop is about the same as the time it takes the model to answer.
   - Change: M2 must report the share reaching 2R first and the minutes to the stop, against this random-entry baseline, by hour.
   - Promotion rule: the desk may go live only if desk-approved shadow ideas beat the baseline by a margin set in advance, over at least 30 ideas. The current "≥ 1 idea, 3 days, 0 breaches" is a safety check, not evidence of an edge.

3. **High: a 5m scalp desk does not fit the call cadence.**
   - H30 sets `min_minutes_between_calls: 30` and `daily_calls_per_pair: 30`.
   - core_rules rule 7 sets `valid_until` to 1–4 candles of the decision timeframe, which is 15–60 min on 15m.
   - The model can look at most every 30 min, answers 0.6–2.5 min late, and places pending orders on 5m structure that live up to 60 min.
   - Change: do one of these.
     - Set a desk `valid_until` cap of 2 × 5m and clamp the pending order's server-side `expiration` (`mt5_backend.py:235` `ORDER_TIME_SPECIFIED`) to at most `no_entry_from`; or
     - admit that 5m timing cannot be done with this cadence and drop option B.

4. **High: "flat by 16:45" relies on the laptop, not the broker.**
   - MT5 positions have no server-side time exit, and the design cites no existing flat or close-all code path in the executor; it would be new code.
   - This laptop already has recorder outages (H28 since 09-28), sleep and boot events, and the VPN.
   - If the Friday flat fails at 45×, the weekend gap p90 of $39.7 is 41 % of equity, and the maximum of $101 is more than the whole account.
   - Change:
     - No desk entries after 12:00 ET on Friday.
     - Pending orders always expire server-side before `no_entry_from`.
     - The monitor raises an alarm if an XAU position exists after `flat_at` + 5 min.
     - D-046 says production moves to another machine before go-live. The desk can only reach `trade` after that move, not on this laptop.

5. **High: the build order puts the money path before the measurement that could cancel it.**
   - Order is B15 (1.25 d, money path) at step 4, then B20 at step 5.
   - M1, M2 and M4 need no desk code; they are replays.
   - Change: run B13, then B20 (M1, M2, M4), then H31, then B14/B15 only if the result is positive. The pass rule becomes "M2 beats the baseline" (issue 2), not "fit share ≥ 2 %". The fit share is always large, so that threshold is meaningless.

6. **Medium: the fit-hours figure is overstated.**
   - "≈ 16 h on ATR alone / ≈ 14 h" uses ATR5m ≤ 5.27 and ignores `fit_min_band 0.3`.
   - Measured (`rv_fit.py`, 30 days): the fit share is 57.3 % at $97.33 (ATR5m ≤ 4.76) and 52.5 % once 20–23 UTC is excluded, which is about 12 h.
   - It falls further after every loss (at $94.4 the cap is 2.83), and to 0 below $91.8.
   - Change: restate the figure as about 11–12 h at start, falling after each loss. The design also uses equity 97.33 while the facts show 99.36; use one value.

7. **Medium: the token math double-counts, and the cache claim does not hold.**
   - The −600 from dropping 1m (decision c) already pays for the B1–B5 blocks for every pair, so it cannot also pay for gold.
   - The gold-only additions are the +405 payload and the +390 brief and legend, about +800 per call, not +230.
   - The handoff (line 52) gives a cache read share of 0.33. With 30-min spacing against a 5-min cache lifetime, the brief is mostly paid uncached.
   - Change: acceptance for XAU becomes "gold-specific Δ ≤ +800 against the post-B7 baseline". Drop the "cached, one miss" argument.

8. **Medium: the checkpoint-B scope is creeping.**
   - Eight new rows (B13–B20) add about 5.25 d on top of B1–B12.
   - Intermarket needs new ingest, a new role, a restart (H26b) and a shared-ingest code change, for "information only, never a gate input" votes.
   - The owner asked for no craziness.
   - Change for checkpoint B: B13 (labels and fit fields, which fix the "venue min_stop" mislabel), B16/B12 suppression, B20 measurement, and B18's gold clock and news tiers folded into B5/B8.
   - Defer B14/B15/B17/B19 to a gold row that H31b gates.

9. **Medium: the stale-news fail-closed deviation is only half specified.**
   - The static fallback covers 08:15–08:50 and 09:45–10:15 ET plus FOMC dates.
   - Not covered: unscheduled speeches, the Wednesday 10:30 ET EIA report (a medium gold mover) and the Sunday-open liquidity.
   - Change: with the feed stale, allow no desk entries at all, with no fallback windows. The rule is simpler and actually fails closed.

10. **Medium: the tighten-only hardening leaves other risk paths open.**
    - Overrides can still set `execution.magic` (`settings.py:751` adds the offset to the overridden value).
    - `config.local.yaml` can set `desk.max_effective_leverage` up to 50 with no D entry.
    - Change: forbid `execution.magic` in overrides. Log a warning, and show it on the dashboard, whenever the desk leverage is above the base value.

11. **Medium: slippage is not bounded.**
    - `desk_day_loss` counts "this trade's risk ≤ 4 %" at the stop price.
    - At 1 oz, a $10 slip on a stop costs 10 %, the full daily limit, in one trade.
    - Change: state in H31b that realised loss per trade can exceed 3 % on a gap or slip. Have M2 report the worst 1m bar move through the stop.

12. **Low: the report-only leverage line has no stated scope.**
    - The design adds a report-only `effective_leverage` line when sizing fails.
    - If it is added for all pairs, `test_gate_desk_none_identical` breaks and the BTC/ETH `checks` change.
    - Change: say explicitly that the line is desk-only. Put leverage at minimum lot in the snapshot only (B13).

13. **Low: the kill-switch semantics change needs a decision entry.** The flat job trading while the kill switch is engaged changes what the switch means. Record it in a D entry and in ops_windows.

14. **Low: a professional would fix the instrument size, not the stop.**
    - The honest fix is a smaller contract, such as a micro-gold symbol or XAUUSDT with a small minimum quantity. Then a 9-stop structural trade risks about 1 %.
    - Change: add a read-only H row that lists the broker's XAU symbol variants (contract size, minimum volume) at the next MT5 session. Keep C2 as the named path after P7.4.

**Verdict.** Before this goes into §3.9.1:

- Make option A plus shadow the default recommendation.
- Move B20 before B14/B15, and add the random-entry baseline and the ≥ 30-idea edge criterion.
- Split H31 so no leverage change is asked without evidence.
- Pin the server-side order expiry, the Friday cut-off and the rule that trade mode only runs after the machine move.
- Correct the fit-hours figure (about 12 h) and the token figure (about +800).
- Cut checkpoint B to B13, B16, B20 and the gold clock and news tiers inside B5/B8.

BTC/ETH loosening is not an issue if the desk guard and golden test are built as written, apart from issue 12's scope.

Scratch scripts are in `C:\Users\Ahmed\AppData\Local\Temp\claude\C--the-claude-new\947f289d-f610-4130-91f5-2267ae498817\scratchpad\gold\`: `rv_sim.py`, `rv_fit.py`, `rv_lat.py`.

