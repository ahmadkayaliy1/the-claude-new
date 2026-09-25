# Project Specification Document: A Fully Integrated, AI-Driven Automated Trading System

**Status:** A foundational document (Master Specification) addressed to an AI Implementation Agent.
**Purpose:** This document is the single source of reference the Implementation Agent must build the entire system upon. Any technical decision not explicitly resolved here is deliberately left to your judgment (the Implementation Agent), on the condition that you document the decision and its rationale in the phase-tracking file described in Section 10.

---

## 0. General Principle Governing Every Decision in This Project

Before writing a single line of code, read this principle — it is the deciding factor whenever you are in doubt:

- **Realism first:** All data, every calculation, and every recommendation must be based on real, actual data from the available sources (Binance API / Binance Vision / MetaTrader Windsor). Any simulation, random estimation, or filling in of missing data with artificial data (mock/fake/simulated data) is strictly and absolutely forbidden on any path that leads to a real trading decision.
- **Wherever a decision is left to you, make it as a professional quantitative/trading-systems engineer (Quant/Trading Systems Engineer) would** — someone building a system that will execute real trades with real money, not an experimental or demonstration system — while grounding it in real-world data, exactly as if you were building this system for actual deployment and not for something hypothetical.
- **Priorities in case of conflict:** speed and accuracy of ingestion and storage > data completeness > depth of analysis > number of indicators. Do not sacrifice accuracy for additional complexity, and do not sacrifice speed for a secondary feature. At the same time, pursue as many different, non-conflicting features as possible, to the extent that they raise accuracy — meaning it's acceptable to reduce speed somewhat in exchange for a meaningful gain in accuracy.
- **Risk management is a precondition for existence:** any trade recommendation without a Stop Loss and a calculated risk weighting is considered invalid and must not reach the user or automated execution at all — or it may reach the user, but must be clearly flagged as high risk with appropriately low confidence.
- Every design decision not explicitly specified (the type of database, the number of Agents, the default active AI provider, etc.) is yours to make, and must be logged along with its justification in the phase-tracking file, provided these decisions remain realistic and grounded in real-world practice, consistent with high accuracy and maximum benefit.

---

## 1. Scope of Trading Pairs and Data Sources

### 1.1 Pairs
- `XAUUSD` (Gold)
- `BTCUSDT`
- `ETHUSDT`

The system must be built so that adding a new pair in the future (a commodity, another cryptocurrency, etc.) is a matter of configuration, not a code rewrite.

### 1.2 Currently Available Data Sources

| Source | Used For | Connection Method |
|---|---|---|
| Binance API (REST + WebSocket) | `BTCUSDT`, `ETHUSDT` | Python library (official / `python-binance`, or a direct REST/WS connection) |
| Binance Vision (Historical Data Dumps) | Quickly backfilling historical data for `BTCUSDT`/`ETHUSDT` before switching to live ingestion via the API | Direct file download (faster than pulling all historical candles via REST) |
| MetaTrader 5 — Broker "Windsor" | `XAUUSD` (and also as a price reference for `BTCUSDT`/`ETHUSDT` if we need to execute through it — see Section 7) | Python `MetaTrader5` library (local connection to the terminal) |

**Decision left to you:** After actually inspecting what the MetaTrader5 API provides for the Windsor broker on `XAUUSD` (Ticks? Real Volume, or only Tick Volume? DOM market depth?), determine precisely which real data types are actually available, and document this clearly — it will directly determine what is computable in Section 4.

### 1.3 Exploration Principle
Do not assume in advance the limits of what's available. Build an actual exploration step (a connectivity test + pulling a sample of every possible data type: Klines, Trades, Order Book Depth, Ticks, Tick Volume, etc.) from both sources, and document, in the tracking file, a table of "Actual Data Available per Pair" before starting any analysis. This table is the foundation the entirety of Section 4 is built on.

---

## 2. Data Ingestion Layer

This layer is a **script/service that is completely independent** of the rest of the system (analysis, AI, execution, Dashboard). Its sole responsibility is ingestion and storage at the highest possible speed and accuracy, with no analytical logic inside it whatsoever.

### 2.1 Two Phases per Source/Pair

**a) Historical Ingestion (Backfill) — First Run Only**
- The start date is read from `config` (`start_date`, customizable per pair individually if needed).
- Fetch everything available from `start_date` up to the moment of execution, in the fastest way possible (use Binance Vision for large historical datasets instead of a slow REST route, then use REST to fill any small remaining gap up to the present moment).
- For MetaTrader/Windsor: use `copy_rates_range` / `copy_ticks_range` (or the equivalent) to pull the largest historical range actually available from the broker (MT5 is typically limited by the depth of history the server provides — document the actual limit you find). Investigate what's appropriate here so you can determine exactly what data types we can actually obtain from it, which in turn tells us what analysis is feasible for XAUUSD — or for any other forex data we may add in the future.

**b) Continuous Live Ingestion (Live/Streaming Ingestion)**
- Once Backfill is complete, the script automatically switches to continuous listening mode:
  - Binance: WebSocket streams (Klines for every required timeframe + trades/aggTrades + order book diff, if you decide they're useful for analysis) instead of polling, to reduce latency.
  - MetaTrader: since there is no true WebSocket in the MT5 API, build a high-frequency polling loop with the lowest possible response time (actually measure the smallest interval that doesn't overwhelm the terminal and doesn't miss ticks).

### 2.2 Resume/Gap-Fill Logic — Mandatory for Every Table
Every time the script starts:
1. For every table (pair × data type), read the last actually stored timestamp.
2. If the table is empty → run a full Backfill (Section 2.1-a).
3. If the table already contains data → fetch everything missing from the last timestamp up to the current moment (Gap-Fill), verify there is no duplication (an idempotent upsert, not a blind append), then automatically switch to continuous live ingestion mode (2.1-b).
4. This logic must operate independently for each table/pair, such that if the connection to one source (e.g., MetaTrader) drops, it does not stop ingestion for the other pairs from Binance.
5. Log every disconnection/resumption event, with its timestamp and the outage duration.

### 2.3 Table Structure
- **A separate table for each pair, for each data type** (not one unified table for everything). A suggested naming scheme (you may modify it):
  - `btcusdt_candles_1m`, `btcusdt_candles_5m`, ... (for every required timeframe)
  - `btcusdt_trades`, `btcusdt_orderbook_snapshots` (if you decide they're useful and available)
  - `xauusd_candles_1m`, `xauusd_ticks` (depending on what's actually available from Windsor)
  - And so on, for every pair × every data type that Section 4 determines is necessary for it.
- The goal: complete isolation, so that each pair's data is processed/read independently of the others at computation time, avoiding any conflict or slowdown caused by reading one large shared table.

### 2.4 Database Selection — Decision Left to You
Requirement: the highest possible write speed (insert/upsert) and the highest possible read speed for tick-level data volume over a long time horizon. Actually evaluate — not just assume — between:
- **Parquet** (via DuckDB as a query engine on top of Parquet files) — excellent for large analytical reads (columnar), but frequent live writes (very small, continuous appends) require careful design (buffering + batch flush) to avoid an excessive number of small files.
- **SQLite (with WAL mode)** — very fast writes/reads for medium-to-high ingestion-rate scenarios, operationally simple, and well suited to giving each pair its own dedicated file.
- **TimescaleDB/PostgreSQL**, if you judge that the data volume and read/write concurrency justify a full database server.
- Any other option you consider suitable (such as DuckDB directly as a `.db` file, or LMDB for extremely dense tick data).

Make your choice, document the reason for it (with actual measurement/benchmark results if possible, even a simplified one), and make the Data Access Layer abstract, so the underlying engine can be swapped later without modifying the rest of the system — whatever you end up using and deciding. Even if the option you choose isn't listed above, that's fine; what matters is having a fast write path and a fast read path. A local database is preferable unless there is a compelling reason otherwise — in which case, decide on the alternative and document your reasoning.

---

## 3. Quantitative Analysis Layer

This layer reads from the database and computes everything necessary **before** sending it to the AI layer (Section 5), exactly as a professional trader prepares their screens before making a decision.

### 3.1 List of Required Analyses (Maximum Coverage Required, Data Permitting)
- **Order Flow** (Delta, Cumulative Delta, actual Buy/Sell Volume from trades)
- **Footprint** (volume maps within each candle/price level)
- **Liquidity Sweep** (Stop Hunts, Equal Highs/Lows sweeps)
- **SMC** (Smart Money Concepts: BOS, CHoCH, Fair Value Gaps, Premium/Discount zones, etc.)
- **Order Block** (bullish/bearish order blocks, Mitigation)
- **Price Action** (market structure, price patterns, reversal candles, etc.)
- **Indicators** (classic and modern technical indicators — it's left to you to determine the most suitable ones in terms of accuracy/speed: EMA/SMA, RSI, MACD, ATR, VWAP, Bollinger Bands, Volume Profile, and any other indicator you consider valuable for raising accuracy)

There may also be something not listed here that is highly important and widely used in the market — or even something not commonly used, but that provides a significantly high degree of accuracy for trades — and it may be added, along with an explanation of the reasoning.

### 3.2 The "What Suits Each Pair" Principle — Mandatory
**Not every pair is analyzed the same way.** For each pair, based on the actual data available for it (not a prior assumption), you must evaluate which of the analyses above can be computed correctly and realistically:
- Illustrative example only (not a final decision imposed on you — verify it yourself): Gold via the MT5 broker typically has no "central market" and no real Trades data in the same sense as Binance, so genuine Order Flow/Footprint may not be accurately computable for it (unlike BTC/ETH, where actual trades are available from Binance).
- **Your task:** for each pair, build an explicit decision table (Analysis type → Available/Not available → the reason, grounded in actual data) and document it. It is forbidden to compute any analysis on the list using substitute/approximate data that creates the false impression of being real data (for example, do not silently use Tick Volume as a stand-in for Real Volume without explicitly disclosing in the output that it is an estimate, not a real volume figure). That said, where a calculation is approximate but genuinely raises accuracy, that's not a problem — include it, but flag that it is approximate and note why you chose to include it.

### 3.3 Depth of Analysis and Indicators — Your Decision, Governed by One Standard
There is no maximum limit on the number of indicators/strategies computed, provided they genuinely serve to raise decision accuracy without breaking the speed budget (the processing time for a live batch must stay well below the time interval of the smallest timeframe used for execution). If you add a derived/helper column, after completing the core calculations, for any purpose that serves the analysis, this is permitted and encouraged.

### 3.4 Timeframes
You determine the set of timeframes needed for each pair to support the trading style in Section 6 (between Scalping and Swing) — you typically need a small timeframe for precise entry timing and a higher timeframe to establish overall direction/context. Document your choice. Even if we end up using all 6 standard timeframes, or more, that's not a problem — what matters is that accuracy stays high.

---

## 4. AI Decision Layer

### 4.1 AI Providers — All of the Following Must Be Supported as a Config-Switchable Setting
Build a Provider Abstraction layer that supports, at minimum:
- **Anthropic Claude** (Messages API)
- **OpenAI** (GPT models)
- **Google Gemini**
- **xAI Grok**
- Any additional suitable provider you consider appropriate (for example, local/open-source providers via Ollama, if you want a no-API-cost fallback option).

For each provider, prepare in the settings file (`.env` / `config.yaml`) the variables needed to run it, such as (an illustrative example — actually fill in the rest for every provider):

```
# Anthropic
ANTHROPIC_API_KEY=
ANTHROPIC_MODEL=            # e.g. claude-sonnet-..., per the latest version at implementation time

# OpenAI
OPENAI_API_KEY=
OPENAI_MODEL=

# Google Gemini
GOOGLE_API_KEY=
GEMINI_MODEL=

# xAI Grok
XAI_API_KEY=
GROK_MODEL=

# Active provider selection
ACTIVE_AI_PROVIDER=anthropic   # anthropic | openai | gemini | grok
```

Make switching providers a one-line change in the settings only, with no code modification at all.

If there are any fully free AI agents/providers you'd recommend using that fit this purpose, include them as options too — they don't all have to be paid.

### 4.2 Multi-Agent Architectures — All Possibilities Must Be Prepared as Ready Config Options
The user has not yet settled on which architecture will be used, so **all** of the following possibilities must actually be built and made ready, with only one activated at a time via a simple setting:

| Mode (`AGENT_MODE`) | Description |
|---|---|
| `single_agent_global` | A single Agent analyzes all pairs and all timeframes together, every analysis cycle |
| `agent_per_pair` | An independent Agent per pair (3 agents), possibly more if we add pairs later — each specialized solely in its own pair |
| `agent_per_timeframe` | An independent Agent per timeframe (analyzing all pairs within that timeframe), plus a "Coordinator/Aggregator" Agent that combines the outputs across all timeframes for each pair and issues the final decision |
| `agent_per_pair_and_timeframe` | Each pair has its own set of sub-agents (one per timeframe specific to that pair), plus a dedicated Coordinator Agent for that pair that merges the final decision for it |
| Any additional logical possibility you see fit | Document it the same way |

For each of the modes above, prepare in advance:
- The full **System Prompt** specific to that mode (the agent's persona, its exact role, its boundaries), designed to give us the best possible accuracy and the highest analytical performance.
- Clear **Instructions** for exactly what data the agent should receive and exactly what it must output (tie this to the format in Section 5).
- If a Coordinator (Aggregator) Agent exists, prepare the Prompt specifying how it merges the sub-agents' outputs and resolves any conflicts between them.

### 4.3 Content Sent to the AI
Every request sent to any Agent must contain:
- A summary of the current market structure for the pair (and the timeframe, if the architecture is split by timeframe).
- All results from the Quantitative Analysis layer (Section 3) applicable specifically to that pair (respect the "What Suits Each Pair" table).
- Sufficient near-term historical context (not just the current moment), to avoid decisions made without adequate context.
- Explicit, standing instructions in every Prompt requiring the model to enforce risk management (rejecting any trade suggestion that lacks a logical SL consistent with the pair's current volatility — e.g., based on ATR or market structure, not an arbitrary number).

Most importantly, all of this should be consistent with high accuracy. There may be columns that exist only to help derive other columns and aren't useful on their own — we don't have to send those. Alternatively, we could send everything. This is your call.

### 4.4 Writing the Prompts — Quality Standard
- Top priority: **the highest possible analytical accuracy**, not the number of analyses mentioned in the Prompt.
- Make the model "act like a professional trader sitting in front of real analysis screens," not like a model that's simply told abstract facts.
- Explicitly prohibit, in every Prompt, any "fabrication" of data or assumptions not grounded in the inputs actually sent to it.

---

## 5. Unified Output Format (Output Contract) — Mandatory for All Modes

Regardless of the active agent architecture, the final form reaching the user/execution layer must be unified (e.g., structured JSON) and must, at minimum, contain:

```json
{
  "pair": "BTCUSDT",
  "timestamp": "...",
  "market_summary": "A brief, dense summary of the current market condition",
  "decision": "BUY | SELL | NO_TRADE",
  "order_type": "MARKET | LIMIT Buy | LIMIT Sell | STOP_BUY | STOP_SELL",
  "entry": { "price": 0.0, "range_min": 0.0, "range_max": 0.0 },
  "take_profit": 0.0,       // may be multiple levels
  "stop_loss": 0.0,         // may be multiple levels
  "risk_management": {
    "risk_percent_suggested": 0.0,
    "risk_reward_ratio": 0.0,
    "invalidation_reason": "What would invalidate this trade"
  },
  "confidence": "0-100, or a qualitative rating",
  "instructions": "Practical steps to maximize potential profit (trailing stop, target adjustment, partial exit, etc.)",
  "next_review": { "in_minutes": 60, "or_condition": "An alternative price/time condition for re-analysis before this period elapses, if market conditions call for it" },
  "reasoning_trace": "The analytical basis the decision was built on (concise)"
}
```

Adjust the fields as you see fit, but preserve the underlying principle: **concise + directly machine-executable + transparent about risk management + explicitly states when re-analysis is required**.

**Note on `decision: NO_TRADE`:** this must be a legitimate, expected option carrying the same weight as BUY/SELL — the system is not meant to force a trade to exist in every analysis cycle (see Section 6).

---

## 6. Trading Style

- The required style is **a middle ground between Scalping and Swing** — not just 1-2 trades a day, and not 20-30 trades a day.
- Priority: **the quality, accuracy, and profit ratio of each trade — not how many there are.**
- You determine, precisely, based on each pair's volatility and historical behavior, the approximate target trade rate per pair/day that serves this balance, and document the reasoning behind this estimate (not an arbitrary number). In addition, I don't want it rigidly deciding it can only ever make two trades — leave this flexible. The idea behind this is simply that we want a trader that is neither excessively risk-taking nor excessively passive (i.e., it should seize any clear opportunity).
- Supported order types: **Market (immediate execution at the current price)**, **Limit Buy**, **Limit Sell**, **Stop Buy**, **Stop Sell**, and also an **Entry Range** instead of a single fixed price, whenever that is more accurate.

---

## 7. Execution Layer

### 7.1 Two Execution Modes
1. **Manual:** the recommendation appears on the Dashboard (Section 8) with an execute button for each pair. When pressed, the interface button immediately invokes the execution Python script using that exact recommendation's data.
2. **Fully Automatic:** a Python script receives the AI's output (the format in Section 5) and executes it directly, without human intervention, with risk management mandatorily applied before sending any order (rejecting execution of any recommendation that doesn't meet the risk-management conditions defined in the settings, even if it came from the AI).

Both modes are activated via a simple config setting, and actual execution must run on **MetaTrader Windsor** (via the `MetaTrader5` library), since it is the platform actually available for execution. If this turns out not to suit one of the pairs in question, let's look at what's possible — there may be more than one execution platform, with each pair executed on whichever platform suits it. In other words, keep this configurable.

### 7.2 Price-Matching Between the Analysis Source and the Execution Platform — Must Be Resolved by Actual Testing, Not Assumption
We have a sensitive decision here that you must make after **actual inspection and real measurement**, not a theoretical assumption:
- **Option A:** analyze `BTCUSDT`/`ETHUSDT` entirely on Binance data, and execute the trade on MetaTrader Windsor for the same pair.
- **Option B:** analyze and execute both `BTCUSDT`/`ETHUSDT` directly on Binance (if execution through Binance itself is available and desired), while Gold is fully analyzed and executed on MetaTrader (which, in any case, is the only option possible for Gold, since it isn't available on Binance).

**Specifically required of you:**
1. Actually measure the difference between `BTCUSDT`/`ETHUSDT` price movement on Binance versus its bid/ask movement on MetaTrader Windsor for the same pair (Spread, Slippage, any time delay/latency, and whether there is a systematic price deviation between the two sources).
2. Based on the result of this actual measurement, settle on one of the options above (or a blend, or dynamic logic that checks the match at the moment of each trade before execution and rejects execution if the deviation exceeds a certain threshold, configurable in the settings).
3. Document the decision and the measurements it was based on in the phase-tracking file.

### 7.3 Mandatory Requirements for the Execution Code
- Actual, strict enforcement of risk management (position size calculated from a % risk of capital, not a fixed size).
- Handling of every possible MetaTrader error condition (order rejected, slippage beyond the allowed limit, connection loss, market closed, etc.) with no silent failures.
- Complete logging of every execution attempt, successful or failed, with the reason.
- Unambiguous confirmation logic: no order is executed unless every mandatory field (Entry, SL, TP, Order Type) is present and numerically valid.

---

## 8. Dashboard

A web interface that must display, both live (real-time) and historically:

1. **Live data**: the price of each pair, the latest candle/update, the status of each data collector (running/stopped/error), and the time of its actual last update.
2. **Historical data**: the ability to browse stored data for each pair and each timeframe over a chosen period.
3. **A recommendation/trade log linked to its source**: for every recommendation the AI issues, the following must be saved and linked together:
   - The full data snapshot that was actually sent to that Agent at the moment the decision was made (Snapshot/Payload).
   - The full output the AI returned (the complete format from Section 5).
   - The subsequent outcome of the trade (not executed / in progress / closed in profit / closed at a loss / closed manually), with the actual figures (Pips/USD/%).
   - This builds a complete history that can later be analyzed for the system's own performance.
4. **A manual execute button for each pair**: next to every displayed recommendation, an "Execute Now" button immediately invokes the execution script (Section 7) with precisely that recommendation's data. If it isn't pressed, the recommendation stays "not executed" in the log, with no effect whatsoever on the real account.
5. **System health indicators**: ingestion response time, the last error that occurred, and connection status with both Binance and MetaTrader.

You choose the appropriate technology for building the Dashboard (Backend API + web interface) in a way that serves the "real-time" requirement (live updates, via WebSocket or Server-Sent Events, rather than slow polling-based updates).

---

## 9. Non-Functional Requirements (Mandatory Across All Layers)

- **Speed:** every path from data ingestion to the issuance of a recommendation must stay clearly faster than the smallest trading timeframe in use.
- **Accuracy and data integrity:** no silent gaps, no duplicate rows, no data mixing between pairs, and validation of every row before it's stored.
- **Reliability:** automatic reconnection whenever any data source is interrupted, without this stopping the other pairs.
- **Security:** all API keys and MetaTrader credentials must be read from environment variables / a settings file that is never pushed to any public repository, and must never be printed in full in the logs.
- **Scalability:** adding a new pair, a new AI provider, or a new analysis type later must be a matter of configuration, not restructuring.
- **A safety mode before full live operation:** it's strongly recommended to provide a "Paper/Dry-Run" mode (simulated execution that is only logged, with no real orders sent), activatable from the config, to test the entire pipeline before connecting it to an actual live trading account. Document your recommendation on using it before any full automatic activation. In addition, we also want an option to test the system fully on a demo account — i.e., non-real funds — to see how it performs.

---

## 10. Project Management: The Phase-Tracking File (Mandatory — the First Thing You Create)

Before writing another single line of code, create a **`PROJECT_STATUS.md`** file at the project root (or whatever name you prefer, but state it clearly), and in it break the entire project down into **very small, granular phases** (micro-tasks), such that each phase can be completed and verified independently. The goal: any Agent (you, later, or an entirely different Agent) should be able to read this file alone and know exactly where work stopped, why, and how to continue — without needing to re-read the entire codebase.

### 10.1 Minimum Required File Structure
```markdown
# Project Status — Automated Trading System

## Overview
[A brief summary of what has been accomplished so far, and the approximate completion percentage]

## Log of Major Design Decisions
- [Date] We chose [Database X] because [...]
- [Date] We adopted the [agent_mode] architecture because [...]
- [Date] We decided to execute BTC/ETH on [Binance/MetaTrader] because of [the result of the price-deviation measurement]
- ...

## Phases

### Phase 1: [Phase Title]
- Status: ✅ Completed / 🔄 In Progress / ⏳ Not Started / ⚠️ Blocked (with reason)
- Description: [What exactly this phase includes]
- Affected files: [...]
- What was done: [Details of what was actually implemented]
- Why this way: [The reason for any implementation decision within this phase]
- Notes/open issues: [if any]
- The exact next step: [so whoever continues knows precisely where to actually start]

### Phase 2: ...
(and so on, for every micro-phase across all of Sections 1-9 above)
```

### 10.2 Rules for Maintaining This File
- **Update the file the moment each phase is completed** — not at the end of the day, and not with any delay.
- Every subsequent change to a prior decision (a refactor, changing the database provider, etc.) is logged as a new line in the "Decision Log" explaining what changed and why, **without deleting the prior entry** (we want a complete history of decisions, not just the final state).
- Whenever you stop for any reason (end of session, an error requiring human intervention, etc.), the very last thing you do is make sure the "exact next step" field of the most recently active phase is precise and sufficient for anyone to resume the work unambiguously.

---

## 11. Closing Instructions to the Implementation Agent

1. Read this entire document before doing any implementation.
2. Actually begin by exploring the real available data (Section 1.3) before making any assumption.
3. Create `PROJECT_STATUS.md` and break the project into phases (Section 10) as your very first actual task.
4. Make every decision left to you in this document with professional judgment grounded in real data and actual measurements, and document each one the moment it's made.
5. Do not introduce any artificial data or behavior into any path that leads to a real trading decision.
6. Risk management is a non-negotiable condition in any output or execution.
7. Make every layer (ingestion, storage, analysis, AI, execution, Dashboard) able to run and be tested independently of the others. At the same time, we should be able to run all of them together with a single run command.
8. On deployment: keep in mind that this will not run on servers with extravagant specifications — it should work on an ordinary PC or laptop. At the same time, I don't want speed sacrificed entirely because of this — performance will naturally differ, but we need to account for, say, the minimum resource level the project can run on, while also having it run faster on higher-end specs. Limited resources must never prevent the project from working at all.

Minimum resources that must be accounted for:
- **CPU:** Core i3, 13th Gen
- **RAM:** 4GB
- **GPU:** Intel (integrated)
- **Storage:** 256GB SSD

If we run it on these specifications, it must work — meaning no RAM exhaustion or anything similar — while at the same time, if we run it on higher specifications, it performs even better. Just like any normal system on the market.
