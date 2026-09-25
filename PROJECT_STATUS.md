# Project Status — Automated Trading System

> Single source of truth for progress. Any agent resuming work: read **Overview**, **Human actions pending**,
> then find the first phase with status 🔄 or ⏳ and follow its **Exact next step**.
> Spec: `AI_Trading_System_Spec_EN.md`. Approved plan (Arabic): see Decision Log D-001.

## Overview
- **Started:** 2026-09-25. **Approx. completion:** 20%.
- **Current milestone:** M3 (Binance ingestion) — P3.1 REST client next
- **Summary:** M0 + M2 complete (hybrid storage decided by benchmark D-020, hot/cold stores, reader, validators, gap detection; 46 unit tests). M1 mostly done (P1.6/P1.9 wait for H1; P1.11 after backfill; P1.12 recorder running since 2026-09-25 08:15 UTC). Next: Binance ingestion (M3).

Status legend: ✅ Completed · 🔄 In Progress · ⏳ Not Started · ⚠️ Blocked (reason) · 👤 needs a human action

## Human actions pending (in the order they will be needed)
| # | Action | Needed by | Status |
|---|---|---|---|
| H1 | MT5 terminal → Tools → Options → Charts → **Max bars in chart = Unlimited**, then restart terminal | P1.6 | ⏳ |
| H2 | Keep the PC awake (no sleep) during the 3–7 day price-matching recording (must include a weekend) | P1.12 | ⏳ |
| H3 | Allow one close/reopen of the MT5 terminal during the multi-client probe | P1.9 | ⏳ |
| H4 | Put `GOOGLE_API_KEY` in `.env` and copy the actual free-tier RPM/RPD limits from AI Studio into config | P8.2 | ⏳ |
| H5 | Review the price-matching decision (Binance vs Windsor execution for BTC/ETH) | P7.2 | ⏳ |
| H6 | Approve demo-account test orders (0.01 lot) | P9.5 | ⏳ |
| H7 | Ops settings (sleep off, autostart, time sync) per `docs/ops_windows.md` | P5.3 | ⏳ |
| H8 | Confirm/adjust default risk parameters (0.5%/trade, 2% daily loss, RR ≥ 1.5, max 3 open) | P9.1 | ⏳ |
| H9 | Any switch to LIVE trading is the user's decision only | P9.7 / P11.4 | ⏳ |
| H10 | Demo account has ≈$158 free margin — open/reset a Windsor demo with a balance close to the intended live capital (risk sizing needs it) | P9.5 | ⏳ |

## Environment facts (observed 2026-09-25, read-only probes)
- Windows 11 Pro; CPU i3-1005G1 (2C/4T); RAM 7.7 GB; ~59 GB free on C:. Local TZ **"Middle East Standard Time"** (UTC+3 now).
- Python 3.12 at `C:\Program Files\Python312` (default `python` on PATH is 3.9 → always use `.venv`). Node v24, git 2.55.
- **MT5** at `C:\Program Files\MetaTrader 5`, build 6182, logged in **WindsorBrokers1-Demo** (trade_mode=0 demo, USD, leverage 1:2000, margin_mode=2 retail hedging). `WindsorBrokers1-Real1` also configured. `maxbars=100000` (limits history → H1).
- Windsor symbols carry an `@` suffix: `XAUUSD@` (digits 2, contract 100, spread ≈28 pts, stops_level 25), `BTCUSD@` (contract 1, spread ≈2600 pts ≈ $26, stops_level 2500), `ETHUSD@` (contract 10, spread ≈228 pts, stops_level 200). filling_mode=1 (FOK), trade_exemode=2 (market execution → `deviation` ignored).
- MT5 ticks: `bid/ask` only — `last=0, volume=0, volume_real=0`. Rates: `real_volume=0`, only `tick_volume`. `market_book_get` empty → **no DOM**.
- MT5 timestamps are **broker server time** (≈UTC+3 now). Naive datetimes passed to MT5 get the **local** TZ applied — local TZ == server offset today, but they diverge around DST switches (trap!).
- Windsor crypto CFDs trade on weekends; XAUUSD@ closes Fri 21:00 UTC (17:00 NY).
- Binance `api`, `fapi`, `data.binance.vision` reachable; local clock ≈ −0.2 s vs Binance; first-request RTT ≈ 600 ms.
- Binance Vision: spot timestamps are **microseconds from 2025-01-01**; BTCUSDT spot aggTrades 2026-08 monthly zip ≈ 358 MB (~14 M rows). Spot order-book history is NOT on Vision (futures `bookDepth` is).
- **Binance USDⓈ-M `XAUUSDT`** (`TRADIFI_PERPETUAL`, onboard 2025-12-11): ~$2 B/day quote volume, ~1.6 M trades/day → real trade flow usable as a **proxy** for gold (subject to P1.11).
- Gemini API: free tier exists for `gemini-3.8-flash` … `gemini-3.5-flash-lite`; free-tier content may be used by Google (we send market data only). Rate limits are per-account (AI Studio).

## Log of Major Design Decisions
- [2026-09-25] **D-001** Plan approved by user (Arabic plan: milestones M0–M11 below). Doc/UI/prompt language: **English**.
- [2026-09-25] **D-002** Execution venue for BTC/ETH **undecided**: start with Windsor MT5 for all pairs; Binance execution backend kept optional; decide after measured price-matching (M7).
- [2026-09-25] **D-003** AI during testing: **Gemini free tier only** (`ACTIVE_AI_PROVIDER=gemini`, model `gemini-3.8-flash`, fallback `gemini-3.5-flash-lite`). All other providers implemented but disabled. Reason: user does not want paid usage yet.
- [2026-09-25] **D-004** AI budget is ROI-based: a **Cost Governor** enforces daily/monthly USD caps and a `max_ai_cost_to_profit_ratio` over a rolling window (virtual PnL during paper/demo); on breach it degrades (cheaper mode → rarer triggers → pause/NO_TRADE).
- [2026-09-25] **D-005** Process model: independent OS processes (`ingest-binance`, `ingest-mt5`, `engine`, `executor`, `api`) + supervisor (`python -m tradingsystem run all`). Ingesters/executor avoid pandas/DuckDB to save RAM. Reason: isolation (MT5 failure must not stop Binance) and 4 GB-RAM target.
- [2026-09-25] **D-006** Storage hypothesis (to be confirmed by P2 benchmark): SQLite WAL hot store, one file per (venue, instrument) with a single writer; daily zstd Parquet cold archive; DuckDB as analytical reader; separate `app.db`. DuckDB `.db` rejected for live data (single read-write process lock). Keys: candles `open_time`, aggTrades `agg_id`, MT5 ticks synthetic `time_msc*1000+seq`.
- [2026-09-25] **D-007** No ZeroMQ/event bus in v1: DB is the source of truth; consumers poll SQLite (250–500 ms); dashboard gets push via WebSocket. Keep an `EventBus` interface for later if latency measurements demand it.
- [2026-09-25] **D-008** Pair ↔ instruments model with roles (`analysis_primary`, `flow_context`, `execution`, `quote_reference`); e.g. gold = `XAUUSD@` (MT5) + `XAUUSDT` perp (Binance flow_context, pending P1.11).
- [2026-09-25] **D-009** Resume logic: start live capture first, run backfill/gap-fill concurrently up to the first live key, then reconcile (idempotent upserts) — no gap during a long backfill.
- [2026-09-25] **D-010** Timeframes: 1m, 5m, 15m, 1h, 4h, 1d, 1w ingested natively per source (no resampling). Decision TF 15m (to be confirmed P6.15).
- [2026-09-25] **D-011** All timestamps stored as int64 **UTC milliseconds**; naive datetimes are forbidden (enforced by tests).
- [2026-09-25] **D-012** Default execution mode `paper`; order: paper → demo → live. Live requires Real-server match + explicit flag + typed confirmation.
- [2026-09-25] **D-013** Python deps in a dedicated `.venv` (Python 3.12). Binance live path uses raw `websockets` + `httpx` (not python-binance socket managers).
- [2026-09-25] **D-014** MT5 server time = measured piecewise model: EU summer-time rule (UTC+2/+3) since 2020-02; UTC+0/+1 with US DST before. Conversion via `ServerTimeModel`; in-order streams via `MonotonicServerClock`; never pass naive datetimes to MT5. Evidence: docs/exploration/mt5_time.md.
- [2026-09-25] **D-015** MT5 live poll interval = 50 ms (completeness is interval-independent; latency/CPU trade-off). Tick key = `time_msc*1000 + seq_within_ms` (stable ordering verified).
- [2026-09-25] **D-016** Binance USDⓈ-M WebSocket routing: book streams on `/public` (or legacy `/ws`); aggTrade/kline/markPrice/forceOrder on `/market`. Futures aggTrades REST covers only 2 days and OI/ratios ~30 days → history from Vision.
- [2026-09-25] **D-017** Vision backfill must detect the timestamp unit per file (spot µs since 2025) and header presence, verify CHECKSUM, and accept only expected filenames.
- [2026-09-25] **D-018** Order-book depth for analysis = periodic REST snapshots (spot 1000–5000 levels, futures 1000) every 60 s, reduced to cumulative liquidity within ±% bands (Vision `bookDepth` style); futures history from Vision `bookDepth`. depth20 streams rejected (span ≈0.007 % of price = microstructure noise for a 15m decision). bookTicker kept for spread/microprice.
- [2026-09-25] **D-019** MT5 access must live in its own OS process: MT5 Python calls hold the GIL, and a slow terminal (another client pulling history) froze a whole process for >30 s (both WS feeds of the recorder timed out at 08:28:30 UTC). Recorder fixed to poll MT5 in a subprocess.
- [2026-09-25] **D-020** Storage (confirms D-006 with numbers, docs/benchmarks/storage.md): SQLite WAL rowid tables as the hot store (live 20-row commit p50 0.18 ms, concurrent reader 0 errors); daily zstd Parquet cold archive (8.8 B/row vs 71 B/row); DuckDB for analytics over Parquet. Hot windows: aggTrades 2 d, ticks 3 d, book_ticker 2 d, liquidations/depth 30 d; candles and small derivatives tables stay hot permanently. Rejected: DuckDB file (second process cannot open while writer holds it; 8 ms commits), WITHOUT ROWID for integer keys (slower), scaled-integer prices (−13 % size only).

## Phases

### M0 — Foundation

### Phase P0.1: Git repository and ignore rules
- Status: ✅ Completed
- Description: `git init`, `.gitignore` (secrets, data, logs, venv, db/parquet, node_modules), `.gitattributes`.
- Affected files: `.gitignore`, `.gitattributes`
- What was done: repo initialised on branch `main`; ignore rules cover `.env*` (except `.env.example`), `config/*.local.yaml`, `data/`, `logs/`, `*.db*`, `*.parquet`, `*.zip`, `.venv/`, `web/node_modules/`, `web/dist/`.
- Why this way: spec §9 — secrets must never reach a repository; data files are large/binary.
- Notes/open issues: none.
- The exact next step: —

### Phase P0.2: PROJECT_STATUS.md and CLAUDE.md
- Status: ✅ Completed
- Description: this tracking file with all phases, decision log, environment facts, human actions; `CLAUDE.md` pointing agents here.
- Affected files: `PROJECT_STATUS.md`, `CLAUDE.md`
- What was done: file created with M0–M11 phases in §10.1 format.
- Why this way: spec §10 — must be the first artifact.
- Notes/open issues: `tools/check_status.py` (P0.7) validates that every phase has all 7 fields.
- The exact next step: —

### Phase P0.3: Python venv and packaging
- Status: ✅ Completed
- Description: `.venv` on Python 3.12, `pyproject.toml` (src layout, extras), `requirements.lock`, import smoke test.
- Affected files: `pyproject.toml`, `requirements.lock`, `.venv/`
- What was done: venv created from `C:\Program Files\Python312`; installed MetaTrader5 5.0.6180, numpy 2.5.3, pandas 3.0.6, pyarrow 25, duckdb 1.5.5, pydantic 2.13, httpx, websockets 16, orjson, fastapi, uvicorn, anthropic, openai, google-genai 2.25, pytest, ta, scipy; package installed editable; lock written with `pip freeze`. MT5 `initialize(path=...)` verified from the venv (attaches to WindsorBrokers1-Demo).
- Why this way: system default `python` is 3.9; isolates from the large global site-packages (torch/tensorflow). Extras keep lightweight processes lean.
- Notes/open issues: pandas 3.x / numpy 2.x are new majors — analysis code must be written against them (copy-on-write semantics in pandas 3).
- The exact next step: —

### Phase P0.4: Settings (config.yaml + .env)
- Status: ✅ Completed
- Description: `config/config.yaml` (committed, no secrets) + optional `config/config.local.yaml` + `.env`; pydantic validation; config hash.
- Affected files: `config/config.yaml`, `.env.example`, `.env` (local, ignored), `src/tradingsystem/core/settings.py`, `tests/unit/test_settings.py`
- What was done: strict pydantic models (`extra=forbid`, typo-proof); pairs→instruments with roles; per-MT5-profile symbol maps; risk, execution, AI (8 providers: gemini, anthropic, openai, grok, groq, openrouter, deepseek, ollama), budget, API. One-line `.env` switches: ACTIVE_AI_PROVIDER, AGENT_MODE, TRIGGER_POLICY, EXECUTION_MODE, EXECUTION_TRIGGER, RESOURCE_PROFILE, `<PROVIDER>_MODEL`. Live mode refused unless `execution.live_confirmation` equals the exact phrase. API host must be loopback. `.env` created locally with a random DASHBOARD_TOKEN.
- Why this way: spec §4.1 (one-line provider switch), §9 (secrets only from env; scalability via config). Decided to commit `config.yaml` (no secrets) instead of an example copy — simpler, and local overrides go to `config.local.yaml`.
- Notes/open issues: python-dotenv does not strip inline comments on empty values → `.env.example` keeps comments on separate lines and the loader ignores values starting with `#`. Provider model ids/prices re-verified in P8.2.
- The exact next step: —

### Phase P0.5: Logging with secret redaction
- Status: ✅ Completed
- Description: per-process JSON rotating logs + console; redaction of secret env values and API-key shapes; UTC timestamps.
- Affected files: `src/tradingsystem/core/logsetup.py`, `tests/unit/test_logging.py`
- What was done: `setup_logging(process_name, ...)` / `setup_from_settings`; `Redactor` masks values of all secret env vars (from settings + any env name containing KEY/SECRET/PASSWORD/TOKEN) and patterns (sk-, sk-ant-, AIza, xai-, gsk_, Bearer); applied to the final formatted line so exceptions and `ctx` extras are covered.
- Why this way: spec §9 — keys never printed in full; redacting the final string is the only way to also catch tracebacks.
- Notes/open issues: module named `logsetup.py` (not `logging.py`) to avoid shadowing the stdlib.
- The exact next step: —

### Phase P0.6: Core utilities (time, instruments, timeframes)
- Status: ✅ Completed
- Description: UTC-ms helpers (naive datetime forbidden), timeframe enum/alignment, instrument registry, symbol maps.
- Affected files: `src/tradingsystem/core/{timeutil,timeframes,instruments}.py`, `tests/unit/test_timeutil.py`
- What was done: exact-integer `to_ms`, `from_ms`, `iso`, `parse_date_spec` (ISO / "-365d" / "earliest"); `Timeframe` enum with ms, Binance interval, MT5 constant name, UTC floor (weekly = Monday 00:00 UTC like Binance); `InstrumentRegistry` (key `venue:symbol`, spec table names e.g. `xauusd_candles_1m`, hot DB path `data/hot/{venue}/{SYMBOL}.db`, one pair per instrument enforced).
- Why this way: D-008, D-011.
- Notes/open issues: MT5 weekly/daily bars are aligned to *server* midnight, not UTC — P1.7/P4.2 decide how MT5 D1/W1 are stored (native open time converted to UTC).
- The exact next step: —

### Phase P0.7: CLI skeleton and status checker
- Status: ✅ Completed
- Description: `python -m tradingsystem {probe,bench,config,status,ingest,engine,executor,api,run}`; `tools/check_status.py`.
- Affected files: `src/tradingsystem/{cli,__main__}.py`, `tools/check_status.py`, `tests/unit/test_status_file.py`
- What was done: lazy dispatch (unimplemented commands print their planned phase); `probe`/`bench` run scripts from `research/probes` / `research/bench`; `config` prints a validated summary; `status` validates this file (93 phases, 7 fields each).
- Why this way: spec §11.7 — each layer runnable alone; lazy imports keep RAM low.
- Notes/open issues: —
- The exact next step: —

### M1 — Real data exploration (spec §1.3). Outputs → `docs/exploration/`

### Phase P1.1: Binance spot REST probe
- Status: ✅ Completed
- Description: exchangeInfo filters, klines all TFs, aggTrades, trades, depth, request weights, RTT.
- Affected files: `research/probes/probe_binance_spot.py`, `research/probes/_report.py`, `docs/exploration/binance_spot.md`
- What was done: measured. Klines all TFs back to 2017-08-17 with exact `taker_buy_base` (→ exact bar delta). **REST aggTrades history starts only at 2022-09-02** (older → Vision). Keep-alive RTT p50 ≈ 330 ms, cold ≈ 600 ms (high-latency route). Weight limit 6000/min; depth 1000 = 50 w, 5000 = 250 w. BTC top-20 levels span ≈0.005 % of price; 1000 levels ≈ ±0.2–0.35 %; 5000 ≈ ±1.2 %.
- Why this way: spec §1.3 — measure before designing.
- Notes/open issues: RTT ≈330 ms is irrelevant for a 15m decision TF but matters for execution-time price checks (use live WS quotes, not REST, at execution).
- The exact next step: —

### Phase P1.2: Binance spot WebSocket probe (30 min)
- Status: ✅ Completed
- Description: kline TFs, aggTrade, bookTicker, depth20 → msg/s, bytes/s, event→receive latency, kline close delay, top-20 span.
- Affected files: `research/probes/probe_binance_ws.py`, `docs/exploration/binance_ws.md`
- What was done: 30 min, 0 reconnects. Rates (BTC/ETH): bookTicker 86/52 msg/s, aggTrade ≈6 msg/s each, depth20 10 msg/s; total < 70 KB/s. Event→receive latency p50 ≈150 ms, p99 ≈1.2–1.5 s, max 3.4 s. Closed-kline delay after candle end p50 189 ms, max 0.94 s. depth20 spans only 0.007 % (BTC) / 0.019 % (ETH) of price.
- Why this way: sizing the WS design and the depth strategy.
- Notes/open issues: depth decision D-018. Latency spikes > 1 s → staleness checks must allow ~3 s before flagging a feed stale.
- The exact next step: —

### Phase P1.3: Binance USDⓈ-M futures probe
- Status: ✅ Completed
- Description: contracts, history depth per endpoint, derivatives context, XAUUSDT hours, WS routing, liquidations.
- Affected files: `research/probes/probe_binance_futures.py`, `docs/exploration/binance_futures.md`
- What was done: XAUUSDT = `TRADIFI_PERPETUAL` (onboard 2025-12-11), trades **24/7** (weekend ≈4–9 k trades/h vs 55–68 k weekdays). Funding history from listing; OI/ratio REST only ~30 days; **`/fapi/v1/aggTrades` searches only the last 2 days** (error -4166). **WS routing split:** book streams (`bookTicker`, `depth*`) on `/ws` or `/public`; `aggTrade`, `kline_*`, `markPrice`, `!forceOrder@arr` only on **`/market`**. Liquidation stream works on `/market` (partial by design).
- Why this way: spec §1.3/§3.1 — derivatives context and gold proxy availability.
- Notes/open issues: the ingester must route each futures stream to the right path (D-016).
- The exact next step: —

### Phase P1.4: Binance Vision probe
- Status: ✅ Completed
- Description: dataset inventory, earliest dates, checksums, headers, µs/ms, publish lag, disk projection.
- Affected files: `research/probes/probe_vision.py`, `docs/exploration/vision.md`
- What was done: spot klines/aggTrades from 2017-08; futures klines/aggTrades from 2020-01; futures `metrics` from 2020-09 (XAUUSDT from 2025-12-11); futures `bookDepth` from 2023-01 (XAUUSDT 2025-12-11); futures `bookTicker` ended 2024-03-30. Daily publish lag = 1 day. All sampled CHECKSUMs match. **Spot files: no header, µs timestamps from 2025**; futures files: header, ms. A stray `part-00000-…zip` file exists in futures/monthly/aggTrades/BTCUSDT → the downloader must accept only the expected filename pattern. Last-12-month aggTrades zip sizes: spot BTC 5.3 GB, spot ETH 5.3 GB, futures BTC 6.8 GB, futures ETH 8.0 GB, XAUUSDT (since listing) 1.0 GB.
- Why this way: spec §2.1-a (fastest backfill).
- Notes/open issues: disk budget (≈59 GB free) → defaults: spot aggTrades 12 months, futures aggTrades 90 days; exact Parquet sizes from P2.2.
- The exact next step: —

### Phase P1.5: MT5 static probe
- Status: ✅ Completed
- Description: symbol specs, order_calc_*, order_check only.
- Affected files: `research/probes/probe_mt5_static.py`, `docs/exploration/mt5_static.md`
- What was done: all 3 symbols: digits 2, tick 0.01, volume 0.01–(20 XAU / 5 crypto) step 0.01, FOK only, market execution, all order types + all expiration modes allowed, freeze level 0. USD per 1.0 price unit per lot = contract size (XAU 100, BTC 1, ETH 10). Swaps: XAU long −37.2 pts/short +21.15 pts; crypto −15 % p.a. (both sides), triple swap Wednesday. `order_check` passes market orders **with SL/TP attached** (market execution accepts SL/TP in the same request); SL inside stops_level → retcode 10016. **Demo account free margin ≈ $158** — far too small for %-risk sizing with 0.01-lot minimum (H10).
- Why this way: sizing and execution correctness.
- Notes/open issues: commission is not exposed by symbol_info → learned from demo deals (P9.5).
- The exact next step: —

### Phase P1.6: MT5 history depth 👤
- Status: ⏳ Not Started
- Description: after H1 (Max bars = Unlimited), find earliest bars per TF and earliest ticks per symbol by chunked requests; time downloads; terminal RSS.
- Affected files: `research/probes/probe_mt5_history.py`, `docs/exploration/mt5_history.md`
- What was done: —
- Why this way: spec §2.1-a — document the actual broker limit.
- Notes/open issues: depends on H1.
- The exact next step: ask user for H1, then run probe.

### Phase P1.7: MT5 server-time model
- Status: ✅ Completed
- Description: live + historical offset model, DST rules, local-TZ trap.
- Affected files: `research/probes/probe_mt5_time.py`, `docs/exploration/mt5_time.md`, `src/tradingsystem/ingest/mt5/servertime.py`, `tests/unit/test_servertime.py`
- What was done: live offset = +3 h exactly. Per-week offset from BTCUSD@ H1 vs Binance 1h return correlation (404 weeks, only 1 weak) and independently from the XAU weekly close (379/392 agree; the rest are US-holiday early closes). **Result:** since 2020-W06 the server follows **EU** summer time (UTC+2 / UTC+3, switches last Sunday Mar/Oct 01:00 UTC) — NOT US DST; before that (2019 → 2020-01) historical data is on UTC+0/+1 with US DST. `ServerTimeModel` + `MonotonicServerClock` implemented and tested (ambiguous fall-back hour, spring gap, regime boundary flagged uncertain 2020-02-01..10).
- Why this way: wrong alignment silently corrupts every analysis; data-derived, not assumed.
- Notes/open issues: the gold *session* follows New York (17:00 NY), so in the 2–3 US/EU DST gap weeks per year session times shift by 1 h in UTC — sessions must be modelled in NY time (P4.5). The count argument of `copy_rates_from_pos` must be < `maxbars` (else "Invalid params").
- The exact next step: —

### Phase P1.8: MT5 polling probe
- Status: ✅ Completed
- Description: poll intervals 20/50/100/250 ms; latency, CPU, completeness re-check.
- Affected files: `research/probes/probe_mt5_polling.py`, `docs/exploration/mt5_polling.md`
- What was done: `copy_ticks_from` call p50 ≈ 0.08 ms, p99 ≈ 0.3 ms; python CPU 0.1–1.3 %, terminal ≈1.5 % at all intervals. **Polled ticks identical (set + order) to a later `copy_ticks_range` re-fetch at every interval** → completeness does not depend on the interval; same-millisecond ticks (~1 %) arrive in a stable order → synthetic key `time_msc*1000+seq` is valid. Decision: 50 ms (D-015).
- Why this way: spec §2.1-b — smallest interval that does not overload the terminal.
- Notes/open issues: measured during a normal session (≈8 ticks/s for 3 symbols); re-check during a high-volatility burst in the P4.6 soak.
- The exact next step: —

### Phase P1.9: MT5 multi-client probe 👤
- Status: 🔄 In Progress
- Description: several processes on one terminal; shutdown isolation; terminal restart recovery (H3); login stability.
- Affected files: `research/probes/probe_mt5_multiclient.py`, `docs/exploration/mt5_multiclient.md`
- What was done: automatic part done — 3 processes attach without credentials; one calling `shutdown()` does not affect the others; the login never changes (and the recorder ran concurrently all along).
- Why this way: ingestion + executor + recorder all attach to MT5.
- Notes/open issues: manual part pending: terminal close/reopen while workers run (coincides with H1).
- The exact next step: when the user restarts the terminal for H1, run `probe mt5_multiclient --watch 180` during the restart (or inspect the recorder `events` stream for the reconnect).

### Phase P1.10: Session calendars
- Status: ✅ Completed
- Description: derive sessions/breaks per symbol from M1 history.
- Affected files: `research/probes/probe_sessions.py`, `docs/exploration/sessions.md`
- What was done: XAUUSD@ daily break 21:00–22:00 UTC (summer) = 17:00–18:00 New York; weekend Fri 21:00 → Sun 22:00 UTC; US-holiday early closes seen. BTCUSD@/ETHUSD@ trade 7 days except a weekly maintenance window **Sat 05:00–08:00 UTC**.
- Why this way: gap detection and "market closed" states must be session-aware.
- Notes/open issues: M1 window only ~14 weeks (maxbars) — re-run after H1 to confirm the winter pattern (expected 22:00–23:00 UTC).
- The exact next step: —

### Phase P1.11: Gold flow-proxy study
- Status: ⏳ Not Started
- Description: XAUUSDT perp vs XAUUSD@: basis, lead-lag (1 s/1 m), does perp delta predict XAUUSD@ returns, weekend drift/Monday gap → include/exclude decision.
- Affected files: `research/gold_flow/study.py`, `docs/exploration/gold_flow_proxy.md`
- What was done: —
- Why this way: spec §3.2 — no substitute data unless it demonstrably helps and is flagged.
- Notes/open issues: —
- The exact next step: write study once P1.3/P1.7 are done.

### Phase P1.12: Price-matching recorder (3–7 days) 👤
- Status: ⏳ Not Started
- Description: background recorder of Binance spot+perp bookTicker (BTC, ETH, XAUUSDT) and MT5 ticks (BTCUSD@, ETHUSD@, XAUUSD@) with local receive time + clock offset per minute → hourly Parquet.
- Affected files: `research/price_matching/recorder.py`, `data/research/price_matching/`
- What was done: —
- Why this way: spec §7.2 — decide by measurement; wall-clock bound, so start early.
- Notes/open issues: depends on H2.
- The exact next step: write recorder, start it.

### Phase P1.13: Data-availability table
- Status: ⏳ Not Started
- Description: consolidate "Actual Data Available per Pair" + preliminary "What is computable" matrix; each cell cites a probe report.
- Affected files: `docs/data_availability.md`, this file
- What was done: —
- Why this way: spec §1.3 — foundation of the analysis layer.
- Notes/open issues: —
- The exact next step: after P1.1–P1.11.

### M2 — Storage

### Phase P2.1: Benchmark data set
- Status: ✅ Completed
- Description: real data: 7 days BTCUSDT spot aggTrades, 12 months BTCUSDT 1m klines, 7 days XAUUSD@ ticks.
- Affected files: `research/bench/bench_prepare.py`, `data/research/bench/*.parquet`, `tests/fixtures/real/*` (+ `PROVENANCE.md`)
- What was done: 6,505,174 aggTrades (Vision, checksum-verified, µs→ms), 525,600 1m klines, 928,350 XAU ticks (server→UTC, synthetic key). Vision download+parse throughput ≈ **1 MB/s** on this connection. Parquet(zstd) footprint: aggTrades 8.8 B/row (≈62 % of the zip size), ticks 9.1 B/row, 1m candles 51.7 B/row. Real 3000-row slices saved as test fixtures.
- Why this way: spec §2.4 — benchmark with real volumes.
- Notes/open issues: at ≈1 MB/s a 12-month spot aggTrades backfill for BTC+ETH (~10.6 GB zip) takes ~3 h → backfill runs in the background after live capture starts (D-009).
- The exact next step: —

### Phase P2.2: Storage benchmark harness
- Status: ✅ Completed
- Description: SQLite rowid / WITHOUT ROWID / scaled-int, DuckDB file, hourly Parquet; bulk, live commits, re-insert, reads, concurrent reader/writer, bytes/row, RSS; plus DuckDB-over-SQLite.
- Affected files: `research/bench/bench_storage.py`, `research/bench/bench_duck_over_sqlite.py`, `docs/benchmarks/storage.md`, `docs/benchmarks/storage_runs.json`
- What was done: SQLite rowid: bulk 213 k rows/s, **live 20-row commit p50 0.18 ms / p99 0.79 ms**, concurrent reader+writer 0 errors, WAL ≤ 4 MB, but 71 B/row (with ts index) and slow big reads (1 M rows 4 s, 7-day 1m aggregation 8.5 s). WITHOUT ROWID: slower writes, no gain. Scaled-int prices: −13 % size, not worth the complexity. DuckDB file: fastest analytics (agg 0.19 s, 21 B/row) but live commit 8 ms and **a second process cannot open the file while the writer holds it**. Parquet: 15 B/row incl. tiny live files, agg 0.8 s via DuckDB, but live small files are unusable without compaction. DuckDB reading the live SQLite (sqlite extension) works with the writer unaffected, but 1 M-row reads are not faster (5.8 s) — only aggregations (1.9 s).
- Why this way: spec §2.4 — actual measurement, not assumption.
- Notes/open issues: peak RSS ≈1 GB in the bench includes the whole 6.5 M-row dataset held in memory by the harness; engine RSS is measured separately in P5.2.
- The exact next step: —

### Phase P2.3: Storage decision + retention policy
- Status: ✅ Completed
- Description: decide engine(s) and hot/cold retention per data type.
- Affected files: `config/config.yaml` (`storage:`), `src/tradingsystem/core/settings.py` (`StorageCfg`), this file (D-020)
- What was done: D-020 — hybrid confirmed. Hot SQLite: candles (all TFs, full history), funding/OI/metrics/mark price (small) permanently; raw aggTrades 2 days, ticks 3 days, book_ticker 2 days, liquidations/depth 30 days. Cold: one zstd Parquet file per table per UTC day; Vision backfill writes directly to cold Parquet. Heavy analytics read Parquet via DuckDB; per-candle derived tables (e.g. `footprint_1m`, P6.8) avoid re-reading raw trades.
- Why this way: best live-write latency and multi-process safety (SQLite) + 8× smaller, fast analytical history (Parquet).
- Notes/open issues: disk guard `min_free_disk_gb` stops backfills (never live capture).
- The exact next step: —

### Phase P2.4: DAL interfaces and table-spec registry
- Status: ✅ Completed
- Description: abstract store interfaces; table specs generated from config.
- Affected files: `src/tradingsystem/storage/{dal,tablespec}.py`, `tests/unit/test_sqlite_store.py`
- What was done: `HotWriter`/`HotReader` protocols; `TableSpec` (key, time column, typed columns, DDL: single-integer key → rowid alias, composite → WITHOUT ROWID); specs for candles (Binance vs MT5 layout, MT5 keeps raw `srv_time`), agg_trades, ticks (+`srv_msc`), book_ticker, depth bands, funding, open_interest, metrics, mark_price, liquidations. `table_specs(instrument)` builds all tables from config.
- Why this way: spec §2.3 (one table per pair × type) and §9 (config-only extension).
- Notes/open issues: —
- The exact next step: —

### Phase P2.5: SQLite hot store
- Status: ✅ Completed
- Description: pragmas, batched `ON CONFLICT DO NOTHING`, last key/time, range reads → numpy, read-only readers, checkpointing, retention delete.
- Affected files: `src/tradingsystem/storage/sqlite_store.py`, `tests/unit/test_sqlite_store.py`
- What was done: `SQLiteHotStore` (WAL, synchronous=NORMAL, busy timeout, per-call short transactions, `upsert`, `replace`, `upsert_many` (one transaction across tables), `read_range`, `read_last`, `delete_before`, `checkpoint`). Tests on real fixtures: re-insert → 0 rows; reader sees writer commits; read-only refuses writes; child process killed mid-transaction → no partial rows.
- Why this way: spec §2.2 idempotent upsert; §9 integrity.
- Notes/open issues: —
- The exact next step: —

### Phase P2.6: Parquet cold store + manifest + rollover
- Status: ✅ Completed
- Description: daily zstd Parquet per (instrument, table, UTC day); atomic writes; merge+dedupe; safe hot→cold rollover.
- Affected files: `src/tradingsystem/storage/{parquet_store,retention}.py`, `tests/unit/test_cold_store.py`
- What was done: `ParquetColdStore` (layout `data/cold/{venue}/{SYMBOL}/{table}/{YYYY}/{date}.parquet`; tmp→`os.replace`; per-day lock file with stale takeover; `write_day(merge=True)` unions + de-duplicates by key, sorted; refuses rows outside the day). `rollover()` moves each complete day older than `hot_days` (+grace): write cold → verify every hot key is in the file → delete from hot → WAL checkpoint; idempotent. The directory tree + Parquet metadata act as the manifest (no separate manifest DB → no second writer).
- Why this way: crash-safe at every step (data always in ≥1 place); Vision backfill and rollover can both fill the same day.
- Notes/open issues: rollover supports single-column keys (all high-volume tables); `depth` (composite key) stays hot 30 d and is pruned with `delete_before`.
- The exact next step: —

### Phase P2.7: Unified reader (hot + cold)
- Status: ✅ Completed
- Description: reads across the hot/cold boundary with key de-duplication.
- Affected files: `src/tradingsystem/storage/reader.py`, `tests/unit/test_cold_store.py`
- What was done: `InstrumentReader.read_range/last_time/first_time` → numpy columns sorted by key; duplicates left by an interrupted rollover are hidden (tested).
- Why this way: callers never need to know where a row lives.
- Notes/open issues: very large historical scans should use DuckDB over the Parquet glob (analysis layer).
- The exact next step: —

### Phase P2.8: Validators and gap detectors
- Status: ✅ Completed
- Description: row validation; candle-grid gaps (session-aware); aggTrade id gaps; known-gaps table.
- Affected files: `src/tradingsystem/storage/{validators,gaps}.py`, `src/tradingsystem/core/sessions.py`, `tests/unit/test_validation_gaps.py`
- What was done: `validate_rows` (vectorised; per-datatype rules: time range, OHLC consistency, grid alignment for Binance TFs incl. Monday-weekly, taker ≤ volume, positive prices/qty, ask ≥ bid, plausible funding) returns rejects **with reasons** (never silent). `candle_gaps` (UTC grid, optional session calendar) and `id_gaps`. `KNOWN_GAPS` table spec for source-confirmed gaps. Session calendars: `always_open` (Binance), `ny_metals_fx` (Sun 18:00 → Fri 17:00 NY, daily break 17–18 NY), `windsor_crypto_cfd` (Sat 05–08 UTC maintenance). All real fixtures validate clean; injected corruptions are caught with the right reason.
- Why this way: spec §9 — no silent gaps, no duplicates, validate every row; spec §0 — gaps are never filled synthetically.
- Notes/open issues: holidays are not in the calendars by design — they become source-confirmed known gaps.
- The exact next step: —

### M3 — Binance ingestion

### Phase P3.1: REST client
- Status: ⏳ Not Started
- Description: keep-alive, weight budget, 429/418 backoff, serverTime offset.
- Affected files: `src/tradingsystem/ingest/binance/rest.py`
- What was done: —
- Why this way: avoid IP bans.
- Notes/open issues: —
- The exact next step: after M2.

### Phase P3.2: Vision backfiller
- Status: ⏳ Not Started
- Description: listing, resumable downloads, checksum, streaming CSV→Arrow, µs→ms; klines → hot, aggTrades → cold. Defaults: klines since listing; aggTrades 12 months; XAUUSDT since 2025-12-11.
- Affected files: `src/tradingsystem/ingest/binance/vision.py`
- What was done: —
- Why this way: spec §2.1-a fastest backfill.
- Notes/open issues: —
- The exact next step: after P3.1.

### Phase P3.3: REST gap-fill
- Status: ⏳ Not Started
- Description: klines via startTime, aggTrades via fromId; closed candles only.
- Affected files: `src/tradingsystem/ingest/binance/spot.py`, `usdm.py`
- What was done: —
- Why this way: spec §2.2.
- Notes/open issues: —
- The exact next step: after P3.2.

### Phase P3.4: Resume state machine + ingestion events
- Status: ⏳ Not Started
- Description: per-table FSM (live-first, D-009); `ingestion_events` with outage durations.
- Affected files: `src/tradingsystem/ingest/common/{resume_fsm,events}.py`
- What was done: —
- Why this way: spec §2.2.
- Notes/open issues: —
- The exact next step: after P3.3.

### Phase P3.5: WebSocket live ingestion
- Status: ⏳ Not Started
- Description: combined streams; closed-kline upsert + forming row; batched aggTrades; bookTicker conflation; stall watchdog; 23 h reconnect; gap-fill after reconnect.
- Affected files: `src/tradingsystem/ingest/binance/{ws,service}.py`
- What was done: —
- Why this way: spec §2.1-b.
- Notes/open issues: —
- The exact next step: after P3.4.

### Phase P3.6: Depth ingestion
- Status: ⏳ Not Started
- Description: strategy chosen in P1.2 (e.g. REST snapshots binned to ±% buckets).
- Affected files: `src/tradingsystem/ingest/binance/depth.py`
- What was done: —
- Why this way: —
- Notes/open issues: —
- The exact next step: after P3.5.

### Phase P3.7: Futures context ingestion
- Status: ⏳ Not Started
- Description: funding, mark/premium, OI 5 m, Vision `metrics`, liquidations (partial), XAUUSDT if P1.11 approves.
- Affected files: `src/tradingsystem/ingest/binance/usdm.py`
- What was done: —
- Why this way: derivatives context raises accuracy for BTC/ETH.
- Notes/open issues: —
- The exact next step: after P3.5.

### Phase P3.8: Collector status and latest quote
- Status: ⏳ Not Started
- Description: `collector_status`, `latest_quote` tables updated by ingesters.
- Affected files: `src/tradingsystem/ingest/common/health.py`
- What was done: —
- Why this way: spec §8.1/§8.5.
- Notes/open issues: —
- The exact next step: after P3.5.

### Phase P3.9: Binance 24 h soak 👤
- Status: ⏳ Not Started
- Description: RSS flat, WAL bounded, zero gaps/dupes.
- Affected files: `docs/benchmarks/soak_binance.md`
- What was done: —
- Why this way: —
- Notes/open issues: needs PC on 24 h.
- The exact next step: after P3.8.

### M4 — MT5 ingestion

### Phase P4.1: Terminal manager
- Status: ⏳ Not Started
- Description: pinned path, owner-only login, account assertion, watchdog on hung calls.
- Affected files: `src/tradingsystem/ingest/mt5/terminal.py`
- What was done: —
- Why this way: multi-client safety (plan item 7).
- Notes/open issues: —
- The exact next step: after P1.9.

### Phase P4.2: Server-time ↔ UTC converter
- Status: ⏳ Not Started
- Description: from P1.7 model, incl. ambiguous hours; tests at DST transitions.
- Affected files: `src/tradingsystem/ingest/mt5/servertime.py`
- What was done: —
- Why this way: —
- Notes/open issues: —
- The exact next step: after P1.7.

### Phase P4.3: MT5 backfill (rates + ticks)
- Status: ⏳ Not Started
- Description: rates per TF; ticks day-chunked with synthetic key; verify M1-from-ticks == copy_rates M1.
- Affected files: `src/tradingsystem/ingest/mt5/backfill.py`
- What was done: —
- Why this way: —
- Notes/open issues: —
- The exact next step: after P4.2 + M2.

### Phase P4.4: MT5 live poller
- Status: ⏳ Not Started
- Description: poll at P1.8 interval; bar-close detection; receive time; 2 h re-fetch equality.
- Affected files: `src/tradingsystem/ingest/mt5/{poller,service}.py`
- What was done: —
- Why this way: spec §2.1-b.
- Notes/open issues: —
- The exact next step: after P4.3.

### Phase P4.5: Session-aware states
- Status: ⏳ Not Started
- Description: market-closed vs error; no false gaps on weekends/daily break.
- Affected files: `src/tradingsystem/core/sessions.py`
- What was done: —
- Why this way: —
- Notes/open issues: —
- The exact next step: after P4.4.

### Phase P4.6: MT5 24 h soak 👤
- Status: ⏳ Not Started
- Description: soak incl. a daily break.
- Affected files: `docs/benchmarks/soak_mt5.md`
- What was done: —
- Why this way: —
- Notes/open issues: —
- The exact next step: after P4.5.

### M5 — Operations

### Phase P5.1: Supervisor
- Status: ⏳ Not Started
- Description: spawn, heartbeats, backoff restarts, graceful Windows stop.
- Affected files: `src/tradingsystem/supervisor/*`
- What was done: —
- Why this way: spec §11.7 single run command.
- Notes/open issues: —
- The exact next step: after P3.5/P4.4.

### Phase P5.2: Resource profiles
- Status: ⏳ Not Started
- Description: `low` / `standard` profiles; measured RSS within budget (<≈1 GB on low).
- Affected files: `config/config.example.yaml`, `docs/benchmarks/resources.md`
- What was done: —
- Why this way: spec §11.8 (4 GB RAM minimum).
- Notes/open issues: —
- The exact next step: after P5.1.

### Phase P5.3: Ops runbook 👤
- Status: ⏳ Not Started
- Description: sleep off, Task Scheduler autostart, time sync, MT5 settings.
- Affected files: `docs/ops_windows.md`, `docs/mt5_setup.md`
- What was done: —
- Why this way: —
- Notes/open issues: —
- The exact next step: after P5.2.

### M6 — Quantitative analysis

### Phase P6.1: As-of frame loader
- Status: ⏳ Not Started
- Description: closed candles + flagged forming candle; refuses stale/gapped windows.
- Affected files: `src/tradingsystem/analysis/frames.py`
- What was done: — · Why this way: — · Notes/open issues: —
- The exact next step: after M3/M4.

### Phase P6.2: Indicators
- Status: ⏳ Not Started
- Description: EMA/SMA, RSI, MACD, ATR, Bollinger, ADX, realized vol (numpy); match `ta` on real data.
- Affected files: `src/tradingsystem/analysis/indicators/*`
- What was done: — · Why this way: — · Notes/open issues: —
- The exact next step: after P6.1.

### Phase P6.3: Pivots and structure labels
- Status: ⏳ Not Started
- Description: pivots with confirmation lag; HH/HL/LH/LL; causality test.
- Affected files: `src/tradingsystem/analysis/structure/pivots.py`
- What was done: — · Why this way: — · Notes/open issues: —
- The exact next step: after P6.1.

### Phase P6.4: BOS/CHoCH, EQH/EQL, premium/discount, sweeps
- Status: ⏳ Not Started
- Description: SMC structure + liquidity; causality test.
- Affected files: `src/tradingsystem/analysis/structure/{bos_choch,liquidity,premium_discount}.py`
- What was done: — · Why this way: — · Notes/open issues: —
- The exact next step: after P6.3.

### Phase P6.5: FVG and order blocks
- Status: ⏳ Not Started
- Description: FVG + OB with mitigation state; causality test.
- Affected files: `src/tradingsystem/analysis/structure/{fvg,order_blocks}.py`
- What was done: — · Why this way: — · Notes/open issues: —
- The exact next step: after P6.4.

### Phase P6.6: Price action patterns
- Status: ⏳ Not Started
- Description: candle patterns, ranges, breakouts.
- Affected files: `src/tradingsystem/analysis/structure/price_action.py`
- What was done: — · Why this way: — · Notes/open issues: —
- The exact next step: after P6.3.

### Phase P6.7: Bar delta / CVD
- Status: ⏳ Not Started
- Description: delta = 2×taker_buy − volume (exact from klines), CVD, divergences.
- Affected files: `src/tradingsystem/analysis/orderflow/delta.py`
- What was done: — · Why this way: — · Notes/open issues: —
- The exact next step: after P6.1.

### Phase P6.8: Footprint
- Status: ⏳ Not Started
- Description: aggTrades → `footprint_1m` → higher TFs; POC, stacked imbalances, absorption; volume equals kline volume.
- Affected files: `src/tradingsystem/analysis/orderflow/footprint.py`
- What was done: — · Why this way: — · Notes/open issues: —
- The exact next step: after P6.7.

### Phase P6.9: Profiles
- Status: ⏳ Not Started
- Description: real volume profile BTC/ETH; XAU TPO (real) + tick-volume profile flagged APPROX.
- Affected files: `src/tradingsystem/analysis/orderflow/profile.py`
- What was done: — · Why this way: — · Notes/open issues: —
- The exact next step: after P6.8.

### Phase P6.10: Depth and derivatives features
- Status: ⏳ Not Started
- Description: funding, OI vs price, basis, % depth imbalance.
- Affected files: `src/tradingsystem/analysis/orderflow/{depth,derivatives}.py`
- What was done: — · Why this way: — · Notes/open issues: —
- The exact next step: after P3.6/P3.7.

### Phase P6.11: Context features
- Status: ⏳ Not Started
- Description: DST-aware sessions/killzones, regime, MTF confluence, anchored VWAP (APPROX for XAU).
- Affected files: `src/tradingsystem/analysis/context/*`
- What was done: — · Why this way: — · Notes/open issues: —
- The exact next step: after P6.2.

### Phase P6.12: Capability registry
- Status: ⏳ Not Started
- Description: analyses declare required data types; outputs carry `data_quality` (real/approx/proxy/unavailable); `docs/capability_matrix.md`.
- Affected files: `src/tradingsystem/analysis/registry.py`
- What was done: — · Why this way: spec §3.2. · Notes/open issues: —
- The exact next step: after P6.2–P6.11.

### Phase P6.13: Snapshot builder
- Status: ⏳ Not Started
- Description: per pair / TF slice, token budget, deterministic, hashed.
- Affected files: `src/tradingsystem/analysis/snapshot.py`
- What was done: — · Why this way: spec §4.3. · Notes/open issues: —
- The exact next step: after P6.12.

### Phase P6.14: Speed budget
- Status: ⏳ Not Started
- Description: full 3-pair cycle < 10 s on low profile (excluding AI).
- Affected files: `docs/benchmarks/analysis_speed.md`
- What was done: — · Why this way: spec §3.3/§9. · Notes/open issues: —
- The exact next step: after P6.13.

### Phase P6.15: Replay + trade-rate study
- Status: ⏳ Not Started
- Description: replay harness; setup-event stats → target trades/day per pair (spec §6); confirm decision TF.
- Affected files: `src/tradingsystem/analysis/replay.py`, `docs/trade_rate_study.md`
- What was done: — · Why this way: spec §6. · Notes/open issues: —
- The exact next step: after P6.13.

### M7 — Price matching (spec §7.2)

### Phase P7.1: Price-matching analysis
- Status: ⏳ Not Started
- Description: basis by session, MT5 spread distribution, lead-lag at 100 ms/1 s, stale quotes, weekends, spot vs perp reference.
- Affected files: `research/price_matching/analyze.py`, `docs/price_matching.md`
- What was done: — · Why this way: — · Notes/open issues: needs P1.12 data.
- The exact next step: after P1.12 ≥ 72 h.

### Phase P7.2: Execution-venue decision 👤
- Status: ⏳ Not Started
- Description: A / B / hybrid with dynamic thresholds; decision log with numbers.
- Affected files: this file
- What was done: — · Why this way: — · Notes/open issues: H5.
- The exact next step: after P7.1.

### Phase P7.3: Price-space translation + basis check
- Status: ⏳ Not Started
- Description: translate levels between venues (preserve structural distances, live basis, correct bid/ask side), pre-trade basis check.
- Affected files: `src/tradingsystem/execution/price_mapping.py`
- What was done: — · Why this way: — · Notes/open issues: —
- The exact next step: after P7.2.

### Phase P7.4: Binance execution backend (only if Option B) 👤
- Status: ⏳ Not Started
- Description: Binance backend on testnet.
- Affected files: `src/tradingsystem/execution/backends/binance.py`
- What was done: — · Why this way: — · Notes/open issues: conditional.
- The exact next step: after P7.2.

### M8 — AI layer

### Phase P8.1: Output contract v1
- Status: ⏳ Not Started
- Description: pydantic model + JSON schema (normalized order types, entry range, TP list with close fractions, SL, valid_until, price_reference, structured management & next_review, data_quality_notes, contract_version) + semantic validators.
- Affected files: `src/tradingsystem/ai/contract.py`, tests
- What was done: — · Why this way: spec §5. · Notes/open issues: —
- The exact next step: can start right after M0.

### Phase P8.2: Provider adapters
- Status: ⏳ Not Started
- Description: Gemini first; then Anthropic, OpenAI, OpenAI-compatible (Grok, Groq, OpenRouter, DeepSeek, Ollama). Native structured output where supported.
- Affected files: `src/tradingsystem/ai/providers/*`
- What was done: — · Why this way: spec §4.1, D-003. · Notes/open issues: H4.
- The exact next step: after P8.1.

### Phase P8.3: Repair/retry, rate limits, cost accounting, Cost Governor
- Status: ⏳ Not Started
- Description: timeouts, RPM/RPD limiter, token/cost accounting, budget caps, cost-to-profit ratio, degradation ladder.
- Affected files: `src/tradingsystem/ai/{repair,budget}.py`
- What was done: — · Why this way: D-004. · Notes/open issues: —
- The exact next step: after P8.2.

### Phase P8.4: Prompt library
- Status: ⏳ Not Started
- Description: versioned prompts per mode + coordinators; mandatory risk, no-fabrication and data-quality clauses; prompt lint.
- Affected files: `src/tradingsystem/ai/prompts/*`
- What was done: — · Why this way: spec §4.2–§4.4. · Notes/open issues: —
- The exact next step: after P8.1.

### Phase P8.5: Orchestrator and modes
- Status: ⏳ Not Started
- Description: 4 required modes + `agent_per_pair_with_risk_reviewer` + `multi_provider_consensus`; parallelism cap; sub-agent failure policy.
- Affected files: `src/tradingsystem/ai/{orchestrator.py,modes/*}`
- What was done: — · Why this way: spec §4.2. · Notes/open issues: —
- The exact next step: after P6.13 + P8.4.

### Phase P8.6: Trigger policies + next-review scheduler
- Status: ⏳ Not Started
- Description: every_close / on_setup_event / hybrid; time & price conditions on live data.
- Affected files: `src/tradingsystem/ai/{triggers,review_scheduler}.py`
- What was done: — · Why this way: — · Notes/open issues: —
- The exact next step: after P8.5.

### Phase P8.7: Decision persistence
- Status: ⏳ Not Started
- Description: snapshot, prompt hash, raw/parsed output, usage, latency, config hash, git SHA — linked.
- Affected files: `src/tradingsystem/ai/store.py`
- What was done: — · Why this way: spec §8.3. · Notes/open issues: —
- The exact next step: after P8.5.

### Phase P8.8: Live run of all modes (Gemini free)
- Status: ⏳ Not Started
- Description: every mode on live data; valid outputs; requests/cost per cycle recorded.
- Affected files: `docs/ai_modes_run.md`
- What was done: — · Why this way: — · Notes/open issues: —
- The exact next step: after P8.7.

### M9 — Execution

### Phase P9.1: Risk gate
- Status: ⏳ Not Started
- Description: SL present & correct side, SL distance ≥ max(stops_level+spread, k·ATR), RR after costs, max risk %, effective leverage cap, max concurrent, daily loss kill switch, correlated exposure, spread vs SL, data freshness, recommendation age, market open, basis. Property test: nothing passes without SL.
- Affected files: `src/tradingsystem/execution/risk_gate.py`
- What was done: — · Why this way: spec §0/§7.3. · Notes/open issues: H8.
- The exact next step: after P8.1.

### Phase P9.2: Position sizing
- Status: ⏳ Not Started
- Description: `order_calc_profit` → lots, round down to volume_step, reject < volume_min, margin check.
- Affected files: `src/tradingsystem/execution/sizing.py`
- What was done: — · Why this way: spec §7.3. · Notes/open issues: —
- The exact next step: after P9.1.

### Phase P9.3: Paper backend
- Status: ⏳ Not Started
- Description: fills on real bid/ask ticks, pending triggers, correct-side exits.
- Affected files: `src/tradingsystem/execution/backends/paper.py`
- What was done: — · Why this way: spec §9 dry-run. · Notes/open issues: —
- The exact next step: after P9.2.

### Phase P9.4: MT5 backend
- Status: ⏳ Not Started
- Description: request builder (FOK, expiration), order_check, retcode map, post-fill slippage, SL/TP attach/modify, idempotency (magic + comment with rec id), account assertion.
- Affected files: `src/tradingsystem/execution/backends/mt5.py`, `retcodes.py`
- What was done: — · Why this way: spec §7.3. · Notes/open issues: —
- The exact next step: after P9.2.

### Phase P9.5: Demo tests 👤
- Status: ⏳ Not Started
- Description: every order type at min volume; forced errors; learn commission from deals.
- Affected files: `docs/exploration/mt5_demo_orders.md`
- What was done: — · Why this way: — · Notes/open issues: H6.
- The exact next step: after P9.4.

### Phase P9.6: Executor service
- Status: ⏳ Not Started
- Description: auto + manual queues, re-validation, position manager, outcomes from `history_deals_get` (pips/USD/% incl. commission & swap).
- Affected files: `src/tradingsystem/execution/{executor,position_manager,outcomes}.py`
- What was done: — · Why this way: spec §7.1/§8.3. · Notes/open issues: —
- The exact next step: after P9.5.

### Phase P9.7: Mode guards + kill switch 👤
- Status: ⏳ Not Started
- Description: live requires Real-server match + flag + typed confirmation.
- Affected files: `src/tradingsystem/execution/executor.py`
- What was done: — · Why this way: D-012. · Notes/open issues: H9.
- The exact next step: after P9.6.

### Phase P9.8: Virtual outcomes
- Status: ⏳ Not Started
- Description: outcomes of NO_TRADE / unexecuted recommendations from real subsequent prices.
- Affected files: `src/tradingsystem/execution/virtual_outcomes.py`
- What was done: — · Why this way: performance analysis + Cost Governor. · Notes/open issues: —
- The exact next step: after P9.3.

### Phase P9.9: Economic-calendar blackout (optional)
- Status: ⏳ Not Started
- Description: MQL5 service exports calendar; gate applies blackout for XAU.
- Affected files: `mql5/CalendarExport.mq5`, `risk_gate.py`
- What was done: — · Why this way: — · Notes/open issues: optional.
- The exact next step: after P9.1.

### M10 — API and dashboard

### Phase P10.1: FastAPI read API + WS publisher
- Status: ⏳ Not Started
- Description: read endpoints; WS push driven by DB polling; localhost bind; token auth.
- Affected files: `src/tradingsystem/api/*`
- What was done: — · Why this way: spec §8. · Notes/open issues: —
- The exact next step: after M3/M4.

### Phase P10.2: Web build pipeline
- Status: ⏳ Not Started
- Description: Vite + React + TS + lightweight-charts; `web/dist` served by API.
- Affected files: `web/*`
- What was done: — · Why this way: — · Notes/open issues: —
- The exact next step: after P10.1.

### Phase P10.3: Live panel
- Status: ⏳ Not Started
- Description: prices, latest candle, collector status + last update.
- Affected files: `web/src/*`
- What was done: — · Why this way: spec §8.1. · Notes/open issues: —
- The exact next step: after P10.2.

### Phase P10.4: Historical browser
- Status: ⏳ Not Started
- Description: pair/TF/range browsing with lazy loading and overlays.
- Affected files: `web/src/*`
- What was done: — · Why this way: spec §8.2. · Notes/open issues: —
- The exact next step: after P10.3.

### Phase P10.5: Recommendation log
- Status: ⏳ Not Started
- Description: snapshot → AI output → outcome chain.
- Affected files: `web/src/*`
- What was done: — · Why this way: spec §8.3. · Notes/open issues: —
- The exact next step: after P8.7.

### Phase P10.6: Execute Now
- Status: ⏳ Not Started
- Description: confirm dialog, idempotency key, gate result shown.
- Affected files: `web/src/*`, `src/tradingsystem/api/routes/*`
- What was done: — · Why this way: spec §8.4. · Notes/open issues: —
- The exact next step: after P9.6.

### Phase P10.7: Health page
- Status: ⏳ Not Started
- Description: ingestion latency, last error, connection status, RSS, WAL sizes, AI spend vs PnL.
- Affected files: `web/src/*`
- What was done: — · Why this way: spec §8.5. · Notes/open issues: —
- The exact next step: after P10.3.

### M11 — Integration and go-live

### Phase P11.1: One-week paper run 👤
- Status: ⏳ Not Started
- Description: full pipeline in paper mode; metrics report.
- Affected files: `docs/runs/paper_week1.md`
- What was done: — · Why this way: spec §9 safety mode. · Notes/open issues: —
- The exact next step: after M10.

### Phase P11.2: Agent-mode shadow comparison
- Status: ⏳ Not Started
- Description: run modes in shadow on the same triggers; pick default mode.
- Affected files: `docs/runs/mode_comparison.md`
- What was done: — · Why this way: spec §4.2. · Notes/open issues: —
- The exact next step: after P11.1.

### Phase P11.3: Demo auto-trading 2–4 weeks 👤
- Status: ⏳ Not Started
- Description: automatic mode on Windsor demo; performance report.
- Affected files: `docs/runs/demo.md`
- What was done: — · Why this way: spec §9 demo testing. · Notes/open issues: —
- The exact next step: after P11.2.

### Phase P11.4: Go-live checklist 👤
- Status: ⏳ Not Started
- Description: signed checklist; live at minimum risk — user's decision only.
- Affected files: `docs/go_live_checklist.md`
- What was done: — · Why this way: — · Notes/open issues: H9.
- The exact next step: after P11.3.
