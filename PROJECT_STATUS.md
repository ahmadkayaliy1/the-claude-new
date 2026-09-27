# Project Status — Automated Trading System

> Single source of truth for progress. Any agent resuming work: read **Overview**, **Human actions pending**,
> then find the first phase with status 🔄 or ⏳ and follow its **Exact next step**.
> Spec: `AI_Trading_System_Spec_EN.md`. Approved plan (Arabic): see Decision Log D-001.

## Overview
- **Started:** 2026-09-25. **Approx. completion:** 91%.
- **Current milestone:** v2 Phase 4 (P12.4 "watches and learns") is done on branch `feat/phase4-watches-learns` (worktree `C:\the_claude_new_wt\phase4`) and waits for the user: H20 (`stop_all.bat` → merge → `start_all.bat`), then H19 (the operator tasks), H18 (Telegram, optional). Production still runs Phase 3 (main `6e24353`, three per-pair systems since 2026-09-27 09:18 UTC). Next after that: P12.5 "goes deeper".
- **Summary:** M0–M10 built; demo/auto execution with Claude via the user's subscription (D-030…D-037). v2 target (D-038…D-040, docs/handoff_operator_v2.md): Phase 1 (P12.1) — venue costs and stop bounds in the snapshot, compact model view (−37 %), operator memory, gate reasons, 30-day record per pair. Phase 2 (P12.2, D-042) — every pair runs as its own system (`start.bat BTCUSDT`): own app.db, logs, dashboard port and MT5 magic (10 %/day per pair), account-wide 25 % drawdown stop, same-direction guard, live account + positions in the model's payload, shared AI ledger with a per-pair share, machine-wide locks (MT5 terminal/history/placement, Claude CLI starts), one-time migration. Both are merged and run in production (three per-pair systems since 2026-09-27). Phase 3 (P12.3, D-043/D-044) — charts sent to Claude, 5-minute screening with calls only on change (≤ 40/pair/day), position management by Python from the rules Claude declares plus Claude's bounded actions on live trades, per-role models with optional escalation, prompts v6 — is merged and running since 2026-09-27 09:18 UTC (H16 done). Phase 4 (P12.4, D-045) — decision metrics and attribution, a bounded per-pair tuning overlay and playbook written only by `tools/tune.py`, daily/weekly Claude (Opus) review sessions and a diagnosis from Task Scheduler with a read-only allow-list, proposals in separate worktrees, a pure-Python 15-min monitor that engages kill switches, notifications (log + toast + optional Telegram), a usage gauge (observe only), dashboard tabs — is done on its branch, awaiting H20/H19.

Status legend: ✅ Completed · 🔄 In Progress · ⏳ Not Started · ⚠️ Blocked (reason) · 👤 needs a human action

## Human actions pending (in the order they will be needed)
| # | Action | Needed by | Status |
|---|---|---|---|
| H1 | MT5 terminal → Tools → Options → Charts → **Max bars in chart = Unlimited**, then restart terminal | P1.6 | ✅ done by the agent at the user's request (common.ini MaxBars → terminal now reports 100,000,000; backup `common.ini.bak-20260925`) |
| H2 | Stop the laptop from sleeping (lid close = Do nothing on AC **and** battery, keep the charger in, Wi-Fi power saving off) — double-click `scripts\apply_power_settings.bat` (run as administrator if refused); exact steps and `powercfg` commands in `docs/ops_windows.md` §2–§4; `scripts\check_ops.bat` verifies. The 41-min stall on 2026-09-25 and the 21:39 one were lid-close sleeps | P1.12 / P5.3 | ⏳ |
| H3 | Allow one close/reopen of the MT5 terminal during the multi-client probe | P1.9 | ✅ |
| H4 | Put `GOOGLE_API_KEY` in `.env` and copy the actual free-tier RPM/RPD limits from AI Studio into config (since D-030 Gemini is the *fallback* provider, used while Claude is unavailable) | P8.2 | ⏳ |
| H5 | Review the price-matching decision (Binance vs Windsor execution for BTC/ETH) | P7.2 | ⏳ |
| H6 | Approve demo-account orders | P9.5 | ✅ user 2026-09-26: run automatically on the MT5 demo account and monitor (D-036) |
| H7 | Start the system yourself with `scripts\start.bat` (then `scripts\start_recorder.bat`); optional autostart: `scripts\install_autostart.bat` (try `-DryRun` first); time sync per `docs/ops_windows.md` | P5.3 | ⏳ |
| H8 | Risk parameters | P9.1 | ✅ user 2026-09-26: never lose more than 10 %/day — D-036 (1 % target / 3 % max per trade / 10 % daily incl. worst case / 4 % correlated / 3 open) |
| H9 | Any switch to LIVE trading is the user's decision only | P9.7 / P11.4 | ⏳ |
| H10 | Demo balance vs intended live capital | P9.5 | ✅ user's intended live capital ≈ $100 (all 3 pairs); the current demo (≈$158) is close enough for realistic tests |
| H11 | Sign the Claude Code CLI in with your Claude subscription once: `claude auth login` in a terminal (or `claude setup-token` and put the token in `.env` as `CLAUDE_CODE_OAUTH_TOKEN`) | P8.9 | ✅ signed in (verified 2026-09-26, §2 of the handoff) |
| H12 | **Remove the crypto-miner** found on the laptop (2026-09-26): `C:\ProgramData\WindowsTask\` (MicrosoftHost.exe, AppHost.exe, audiodg.exe, WinRing0x64.sys, winlogon.bat/new.xml), persistence `HKLM\...\Run "Realtek HD Audio" = C:\ProgramData\ReaItekHD\taskhostw.exe` and the Startup shortcut `WindowsFormsApp10 - Shortcut.lnk`; 2.1 GB RSS, 50 % CPU. Defender full/offline scan as administrator, remove the entries, reboot, change passwords (Windows, e-mail, broker, Claude). The agent never touches it | all | ⏳ |
| H13 | After every reboot nothing auto-starts: run `scripts\start.bat` + `scripts\start_recorder.bat`, or install autostart once with `scripts\install_autostart.bat` (`-DryRun` first) | P5.3 | ✅ autostart installed 2026-09-26; per-pair tasks since 2026-09-27 |
| H14 | Apply v2 Phases 1+2 and switch to one system per pair: `git merge --ff-only feat/instances` in `C:\the_claude_new`, then double-click `scripts\switch_to_pairs.bat` (stops the all-pairs system, copies each pair's history, one autostart task per pair, starts every pair), then `scripts\start_recorder.bat`. Staying on the all-pairs system is also possible: `scripts\restart.bat` after the merge | P12.2 | ✅ done 2026-09-27: merged, `switch_to_pairs.bat` run, three systems up; XAUUSD stopped for the weekend by the user |
| H15 | Phase 3 prerequisite: `.venv\Scripts\pip install -r requirements.lock` (matplotlib/pillow become declared; already installed, so a no-op — verified: the live call rendered charts with the production venv) | P12.3 | ⏳ (optional) |
| H16 | Apply Phase 3: in `C:\the_claude_new`: `git merge --ff-only feat/phase3-sees-manages`, then `.venv\Scripts\python.exe -m tradingsystem config` (the "phase 3:" line shows charts/management/actions on), then `scripts\restart_all.bat`; then `scripts\status_all.bat` | P12.3 | ✅ done 2026-09-27 09:17 UTC (merge) + 09:18 UTC (restart_all): `config` shows charts=on management=on position_actions=on calls/pair/day=40; first cycles with 6 charts valid (ETH 60 s, BTC 144 s) |
| H17 | Optional after Phase 3: in `config\config.local.yaml` put `escalation: {enabled: true}` under ONE `ai:` block (a second `ai:` line is refused) for Opus confirmation of strong setups; `models: {escalation: {model: fable}}` in the same block to use Fable | P12.3 | ⏳ |
| H18 | Phase 4, optional, any time after H20 — Telegram (docs/notifications.md §1): a bot from @BotFather, press Start in its chat, the chat id from `getUpdates`, then `TELEGRAM_BOT_TOKEN=` and `TELEGRAM_CHAT_ID=` in `C:\the_claude_new\.env` (no restart); test: `.venv\Scripts\python.exe tools\notify.py --level info --title Test --text Hello`. Without it: log line + Windows toast | P12.4 | ⏳ (optional) |
| H19 | Phase 4, AFTER H20, in `C:\the_claude_new`: `scripts\install_operator_tasks.bat -DryRun`, then `scripts\install_operator_tasks.bat` — `TradingSystemOps-Monitor` (every 15 min), `-ReviewDaily` (04:30 UTC), `-ReviewWeekly` (Sunday 06:00 UTC); `scripts\check_ops.bat` lists them; `-Uninstall` removes them. Then retire the desktop app's 3-hourly monitor task | P12.4 | ⏳ |
| H20 | Apply Phase 4 (BEFORE H19), in `C:\the_claude_new`, with the systems stopped: `scripts\stop_all.bat`, `git merge --ff-only feat/phase4-watches-learns`, `.venv\Scripts\python.exe -m tradingsystem config` (the new "phase 4:" line: adaptive=on notify=on toast=on telegram=… monitor=on diagnose=on sessions=on gauge=observe only), `scripts\start_all.bat`, then `scripts\status_all.bat` (merge-then-restart would let the running Phase 3 engines re-read the new trader template, which they cannot fill, until the restart) | P12.4 | ⏳ |
| H21 | Optional, a week after H20: calibrate `ai.usage.weekly_token_budget` / `five_hour_token_budget` in `config.local.yaml` (inside ONE `ai:` block) from the gauge's own weighted `7 d` / `5 h` numbers (cache reads at 0.1) in the health report and the review packs, not the ledger's raw sums; `enforce: true` only if the gauge should ration calls | P12.4 | ⏳ |
| H22 | Phase 5 (only for the bounded MCP tools): `.venv\Scripts\pip install mcp` | P12.5 | ⏳ |
| H23 | Phase 5: compile and attach `tools\mql5\CalendarExport.mq5` in the MT5 terminal (news blackout for gold) | P12.5 | ⏳ |
| H24 | Optional Phase 5: opt one pair into `session_mode: persistent` for a 24-h measurement | P12.5 | ⏳ |
| H25 | Phase 5: set `flow_proxy_approved: true` for XAUUSD only if the gold-proxy study passes its thresholds | P12.5 | ⏳ |
| H26 | Apply Phase 5: `git merge --ff-only feat/phase5-goes-deeper`, then `scripts\restart_all.bat` | P12.5 | ⏳ |

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
- [2026-09-25] **D-021** User's intended live capital ≈ **$100** across BTC/ETH/XAU. Measured consequence (Windsor contract specs, not advice): at the 0.01-lot minimum a typical 15m-structure SL risks ≈5–15 % (XAU), 3–6 % (BTC), 1.5–3 % (ETH) of $100. The risk gate will compute and display the *actual* risk % at the executable lot size and reject anything above `max_risk_per_trade_pct` — the user must choose that limit knowingly (asked at P9.1).
- [2026-09-25] **D-022** Git: local commits after each phase (user approved); nothing is pushed; `.env` and `data/` are ignored.
- [2026-09-25] **D-023** MT5 terminal `MaxBars` raised from 100,000 to unlimited (terminal reports 100,000,000) by editing `common.ini` while the terminal was closed (user asked the agent to do H1). `mt5.initialize(path)` relaunches a closed terminal and auto-logs into the saved account.
- [2026-09-25] **D-024** MT5 terminal serves API clients one at a time and a history download can block it for minutes → the MT5 backfill requests hour-sized tick / week-sized M1 chunks and yields while the live poller's heartbeat is > 5 s old. Tick completeness is unaffected by such delays (cursor catches up), only latency.
- [2026-09-25] **D-025** Self-healing ingestion: the Binance live service snapshots the last live aggTrade id at disconnect (outage gap-fill) and audits the last 2 h every 5 min, re-fetching aggTrade id holes and missing ≤1h candles from REST. Found and fixed a real 479-trade hole after a network flap (verified: footprint == kline volume per minute).
- [2026-09-25] **D-026** Futures bookTicker dropped from live ingestion (≈850 msg/s starved the event loop → keepalive timeouts); the research recorder still captures it for price matching. WebSocket pings relaxed to 60 s / 90 s.
- [2026-09-25] **D-027** AI levels are expressed in the *analysis* instrument's price space (`meta.price_reference`); the execution layer translates them to the execution instrument by the live basis (P7.3). Gold analyses and executes on the same instrument.
- [2026-09-25] **D-028** Dashboard = FastAPI + vanilla JS (no build step) + locally vendored TradingView lightweight-charts 4.2.3 — lighter than React/Vite on 4 GB machines and editable without a toolchain.
- [2026-09-25] **D-029** Production runs under the supervisor (`run all`, Windows Job Object, process-tree kills). The research recorder (P1.12) runs separately until its 3–7 day window ends.
- [2026-09-25] **D-030** AI brain = Claude on the user's own Claude subscription through the local Claude Code CLI (`ai.active_provider: claude_code`, model `sonnet`, effort medium) with Gemini free as `ai.fallback_provider` (supersedes D-003's Gemini-first default). Claude only analyses (no tools, no files, no execution); data, the risk gate and execution stay deterministic in our code. Personal use of one's own subscription; it shares the plan's usage limits with interactive use (daily cap `rpd: 120`). API keys are never passed to the CLI, so nothing is billed per token.
- [2026-09-26] **D-031** Backfill robustness (audit, 57 findings / 4 areas, fixed on branches fix/a-ingest…fix/d-exec and integrated): transient failures (network, terminal link) are retried after 5→60 min instead of marking the pass done and sleeping until the daily slot; Vision zips stream to `data/vision_cache` (resumable, CHECKSUM first, deleted after ingest; an object that never matches its CHECKSUM becomes permanent after 3 passes); MT5 IPC failures are never recorded as source gaps / earliest bar / done tick days; dead backfill workers are restarted; MT5 candle holes after outages are healed live.
- [2026-09-26] **D-032** Production is started by the user (`scripts\start.bat` → `run all --detach`), never from an agent's shell: processes an agent session starts die with that session (production stopped 2026-09-25 22:07 UTC). The supervisor is suspend-aware, single-instance, keeps the MT5 terminal outside its job, and logs child stderr.
- [2026-09-26] **D-033** AI call rationing (protects the shared subscription limits): a pending review is consumed only by an answered cycle and re-fires no sooner than `review_floor_minutes` (5); failures back off up to `max_backoff_minutes` (120); a cycle has a deadline (`cycle_deadline_s` 600); the heartbeat runs independently of AI calls; per-pair failure isolation; the recommendation timestamp/valid_until are set by the system from the cycle (never trusted from the model); a data gate stores 'skipped' (no call) when the decision bar is missing or the history is too short (XAUUSD until the MT5 4h/1d backfill fills).
- [2026-09-26] **D-034** Execution: paper tick cursors persist per leg (SL/TP hits during downtime are replayed); bounded tick reads; per-decision isolation with reconciliation (an unreachable MT5 terminal is 'unknown', never 'nothing placed'); risk gate: pending orders count toward max open, a single pair's stacked exposure is capped at `max_correlated_risk_pct`, crossed pending orders rejected (`pending_price_valid`); API Host-header allow-list (DNS rebinding). Awaits the user's H8 confirmation.
- [2026-09-26] **D-035** Claude Code output mode = schema in the prompt (`structured_output: prompt`), not `--json-schema`: measured on the first live cycle, `--json-schema` made the CLI run a tool round-trip (≈50 k input tokens and 2.5–4.5 min per attempt) and the strict text-length limits forced a repair call (BTC SELL / ETH NO_TRADE took 6–7 min, so they were already past `max_recommendation_age_s`). Prompt mode on the same real BTC payload: 1 turn, 21.8 k in / 4.9 k out, 79 s, valid first time. Free-text fields are now trimmed instead of rejected and a NO_TRADE risk block is dropped (the SL/side/TP/RR rules stay strict); two CLI calls may run at once (`max_concurrency: 2`, set 1 on 4 GB machines).
- [2026-09-26] **D-036** (user) Automatic execution on the MT5 **demo** account (`EXECUTION_MODE=demo`, `EXECUTION_TRIGGER=auto` in `.env`), monitored by the agent. Risk with the $100 demo balance: target 1 %, max 3 % per trade (the 0.01-lot minimum sets the size: BTC/ETH usually 1.5–3 %, gold usually skipped), daily loss ≤ 10 % enforced as a worst case (realised today + every open SL risk + the new trade), BTC+ETH group ≤ 4 %, ≤ 3 open. Emergency stop: `scripts\kill_switch_on.bat`.
- [2026-09-26] **D-037** MT5 history backfill order: 1h–1w candles of every instrument first, then 1m–15m, then ticks (a multi-year BTC 1m re-scan was starving the XAUUSD/ETHUSD 4h/1d history the engine's data gate needs); in-step heartbeats every 30 s.
- [2026-09-26] **D-038** (user) Target v2 — *Claude as operator*: Claude analyses and decides, Python fetches/computes/executes; Claude keeps reviewing errors and results and improves within bounds; one fully independent system instance per pair (`start.bat BTCUSDT`); comprehensive per-pair analysis incl. **candle-chart images**. Design and phases: `docs/handoff_operator_v2.md`.
- [2026-09-26] **D-039** (user) Learning autonomy: prompts/playbook and soft parameters change automatically only inside code-enforced bounds (tools/tune.py, logged, expiring, revertible); code, risk limits and execution logic change only through proposals the user approves. Daily loss limit **10 % per pair instance** (user's choice) + an account-wide drawdown stop of 25 % from the high-water mark (lead's safety floor).
- [2026-09-26] **D-040** Operator session model: the user asked for a persistent Claude session; it is built as persistent *memory* (`operator_notes` fed back per pair) with event-driven single-turn calls by default, and a true persistent stream-json session as a measured, switchable second mode — a session polling 24/7 would exhaust the shared subscription limits within hours. Measured: ~23 k input / 3–5 k output tokens and ~50 s per call; ~1.1 M tokens/day per pair at today's cadence → one instance first.
- [2026-09-26] **D-041** The model reads a compact *view* of the snapshot (`ai/model_view.py`, legend in the cached system prompt) instead of the stored payload (−37 % tokens, nothing dropped); the stored/hashed payload stays the source of truth for triggers, the data gate and the dashboard. The snapshot states the execution venue's costs and the stop bounds exactly as the risk gate computes them, and the model keeps per-pair operator notes that are fed back on the next cycle (v2 Phase 1).
- [2026-09-27] **D-042** One independent system per pair (D-038/D-039, v2 Phase 2): `--instance <PAIR>` (TS_INSTANCE) runs a pair with its own state `data/instances/<PAIR>` (app.db, run/, STOP_ALL, KILL_SWITCH), logs `logs/<PAIR>`, dashboard port and MT5 magic (base + offset) — so the 10 %/day limit, the open-trade limit and the new same-direction guard are per pair; market data stays shared (disjoint instruments, one writer per file). Shared by every system: the AI usage ledger `data/shared/ai_usage.db` (rpd counts every pair, one pair ≤ 60 %), an account-wide drawdown stop (25 % below the equity peak, sticky until `reset_drawdown_stop.bat`, fail-closed on an unreadable peak file), machine-wide locks for the MT5 terminal launch, MT5 history calls and MT5 order placement (gate → broker listing, so the BTC+ETH correlated cap holds across systems), and 15 s spacing of Claude CLI starts. A pair adopts the all-pairs system's orders on its own pair (placed before the switch). The all-pairs system still works unchanged and never runs next to a per-pair one (command line + lock checks). The model's payload now carries the live account (equity, today's result, drawdown, this pair's positions and pending orders). Reviewed by 3 lenses with adversarial verification (25 confirmed findings fixed, among them an autostart installer that always aborted in PowerShell 5.1 and a per-pair kill switch that did nothing in the all-pairs mode).
- [2026-09-27] **D-043** (user) Phases 3–5 scope and four choices (full spec: docs/handoff_operator_v2.md §3.6–3.10). (1) Position management = "Claude leads, code protects": Python executes the management rules the model declares with a trade (breakeven with a spread buffer, trailing, partial close, time stop) and on later reviews the model may tighten a stop, take profit, adjust a TP or cancel a pending order through `position_actions`; it can never widen or remove a stop or add size; every action passes a gate and protective actions keep running under the kill switch. (2) Notifications when the desktop app is closed: Telegram bot (token/chat id in .env) plus a Windows toast and a log line. (3) Models per role, changeable at any time in config (`ai.models`): Sonnet for decisions now, Opus for reviews, optional escalation of strong setups to Opus/Fable that may only confirm or downgrade. (4) Daily cap 40 calls per pair (every ledger row counts), with Python screening every 5 minutes and calling only on change; a ledger-based usage gauge rations further. Phase order: 3 "sees and manages" (charts, screening, position management, prompts v6), 4 "watches and learns" (metrics, bounded tuning, reviews from Task Scheduler, monitor, notifier), 5 "goes deeper" (enrichments, persistent session opt-in, MCP tools, news blackout, gold proxy). No operator session ever writes to git in the production checkout: runtime tuning lives under data/, proposals in separate worktrees.
- [2026-09-27] **D-044** Phase 3 implementation choices (P12.3, within D-043): (1) screening — the 5m timeframe is confirmation only (its zones and liquidity are not locations; a 5m reversal candle counts only at a 15m/1h location); a weak call needs a 15m/1h location AND new price action there (`ai.weak_needs_location`); a sweep is keyed by its pool and the first bar of its run of consecutive sweeping bars; a call without an answer gives back its setup and events; an event call that a due setup would have made costs no event budget. Measured 21–36 setup/idle calls per pair per day. (2) Images go as ONE stream-json user line of shape `message`, probed once per machine; a text-only provider gets no charts and is told so. (3) The input-token target (≤ 23 k) is missed by 8 % (24.9 k: images + the longer v6 system prompt, which is a cache read after the first call) — accepted; levers are config only. (4) Protective management waits through a closed market or a terminal refusal (never given up after N tries); a request the venue refuses as invalid is retried 3 times; an unconfirmed MT5 timeout is never re-sent blindly; a model action is planned once and resumed exactly. (5) At the 0.01-lot minimum a single position carries its real TP index and `tp_hit` on a nearer target becomes `price_reached` at that target. (6) Escalation always runs on the active provider with `ai.models.escalation` (never the fallback); a withheld trade keeps its protective `position_actions` as a NO_TRADE whose notes say the trade was not placed. (7) Config: a repeated key in a YAML file is refused; `instances.<PAIR>.overrides` for per-pair settings (validated by every all-pairs load).
- [2026-09-27] **D-045** Phase 4 implementation choices (P12.4, within D-039/D-043; as-built list in docs/handoff_operator_v2.md §3.8 4.8). (1) Operator sessions run from a Python runner (`tools/operator/run_session.py`; `run_session.ps1` only launches it) under Task Scheduler tasks named `TradingSystemOps-*` (a `TradingSystem-*` name is deleted by `install_autostart.ps1`), UTC start boundaries, task time limits 130/190 min as a backstop beyond every `operator.*_timeout_min`. (2) Session bounds: Bash only for `Bash(.venv/Scripts/python.exe tools/<tool>.py *)` — a space before the `*` (a glued `*` matched `tools/tune.py/../<any file>`), the diagnosis adds `kill_switch.py --pair *` (one pair per session, never the last pair still trading); no git rules (`--output=<file>`); reads bounded to the checkout and the data root, `.env`, credential paths and the user profile denied; a scrubbed environment; the marker `TS_OPERATOR_SESSION=1` makes the tools refuse file arguments and the global switch (tune.py playbook only as `--text`, propose.py no `--body-file` and base `main` only, kill_switch.py no `--all`, review_pack `--out` only under data/reviews, demo_order_test.py refuses) and records the actor as `operator-session:<review id>`; every text tune.py stores is refused when it holds a configured secret or a key-looking token; a diff guard compares `git status` before and after. (3) Sessions are ledger rows (role review/diagnose, pair NULL) outside the pairs' 40/day quota but inside the usage gauge; the gauge counts only `claude_code` providers, weights a cache-read token 0.1 (its budgets are in weighted tokens), has a 5-point step-down in the engines, and only observes until H21 (`ai.usage.enforce: false`). (4) Tuning: revert is always allowed and never counts as the day's change; the spec's `$$` lint rule dropped (values are inserted verbatim); a wider denylist (confidence 80–99, avoid/never NO_TRADE, disregard, mixed-script words, invisible marks, Unicode hyphens and digits); tune.py exit codes 0 applied / 2 refused / 3 invalid / 1 error; `adaptive.yaml` guarded against aliases, size, nesting depth (32; evidence ≤ 8), value count and unexpected keys everywhere it is read (services, API, pack), parsed with libyaml. (5) Notifier: critical never rate-limited, info and warn have separate 20/h budgets, the toast goes before Telegram, the Telegram values follow `.env` without a restart (a change needs two agreeing reads). (6) Monitor: per-pair switch on an order burst or the daily loss, the global switch only for a real MT5 account's drop > 10 % against a recent baseline; 'system not running' (and that pair's stale heartbeats) need two runs; undelivered notifications are re-sent without re-detecting the event; a diagnosis at most every 3 h and never while a review holds the session lock; a config it cannot read exits 3 with a log file and a toast. (7) Paper settlement emits `outcome` events like MT5 (they wake the model). (8) Metrics: an executed trade is scored only after the bar it closed in is closed and stored; the backfill of older decisions runs 20 per 60-s pass. (9) `prompt_versions` keyed by (prompt_hash, library_hash). (10) The review-session budget: the live daily review was 27.3 k unique context + 7.4 k output (34.7 k, the ≤ 30 k target missed by 16 %; the ledger records 204 k input because every turn re-reads the context) — accepted, the levers are the turn limits and the pack size. (11) Phase 4 is applied with the systems stopped (stop_all → merge → start_all): the v5 trader template adds `$tp_hint`, which a running Phase 3 engine cannot fill.

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
- Status: ✅ Completed
- Description: after H1 (Max bars = Unlimited), earliest bars per TF and earliest ticks per symbol.
- Affected files: `research/probes/probe_mt5_history.py`, `docs/exploration/mt5_history.md`
- What was done: M1 history from 2019-02-24 (XAUUSD@), 2015-11-15 (BTCUSD@), 2019-06-27 (ETHUSD@); **XAUUSD@ ticks only from 2024-08-17** (≈200–360 k ticks/day, ≈145 M ticks ≈ 1.3 GB Parquet). BTC/ETH tick depth left to the backfill (config keeps 90 days).
- Why this way: spec §2.1-a — document the broker's real limit.
- Notes/open issues: deep history requests stall the terminal for other clients for minutes → backfill is chunked (month of bars / day of ticks per call) and probes must not run concurrently with it. The binary-search probe was stopped after the key results (retry sleeps made it slow).
- The exact next step: —

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
- Status: ✅ Completed
- Description: several processes on one terminal; shutdown isolation; terminal restart recovery (H3); login stability.
- Affected files: `research/probes/probe_mt5_multiclient.py`, `docs/exploration/mt5_multiclient.md`
- What was done: 3 processes attach without credentials; one `shutdown()` does not affect the others; login never changes. Restart test (2026-09-25 09:55 UTC): with the terminal closed, `initialize(path=…)` from a worker **launched the terminal itself and it auto-logged into the saved demo account**; all 3 watch workers then read 1739+ ticks with 0 failures.
- Why this way: ingestion + executor + recorder all attach to MT5.
- Notes/open issues: recovery path for a terminal closed mid-session = re-`initialize(path)` (auto-relaunch) — implemented in the terminal manager (P4.1) and exercised in the P4.6 soak.
- The exact next step: —

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
- Status: ✅ Completed
- Description: keep-alive, weight budget, 429/418 backoff, serverTime offset.
- Affected files: `src/tradingsystem/ingest/binance/{rest,markets}.py`
- What was done: async `BinanceRest` (per-minute weight budget synced from `X-MBX-USED-WEIGHT-1M`, 429/418 honour `Retry-After`, exponential backoff for network/5xx, NTP-style clock offset). `markets.py` holds measured per-market facts (limits, weights, Vision layout, REST aggTrades reach).
- Why this way: avoid IP bans; the live service and backfill worker get separate budgets (spot 2000 + live; futures 1200 + live; IP limits 6000/2400).
- Notes/open issues: futures aggTrades cost weight 20/request → large futures holes wait for Vision.
- The exact next step: —

### Phase P3.2: Vision backfiller
- Status: ✅ Completed
- Description: listing, checksum-verified downloads, streaming parse, µs→ms; klines → hot, aggTrades → cold; resumable; daily re-run.
- Affected files: `src/tradingsystem/ingest/binance/{vision,backfill}.py`
- What was done: `VisionClient.plan` (monthly files for whole months, daily otherwise; strict filename regex), SHA-256 check, header auto-detect, per-value µs→ms, bounded-block CSV parsing; `VisionIngestor` (klines validated → hot; aggTrades split per UTC day → merged cold Parquet; metrics → hot; progress in `vision_done`). `BinanceBackfill` worker (own process): candles gap-driven (REST for small spans, Vision for large), funding (REST), metrics (Vision daily), aggTrades newest→oldest, then REST bridging of **every** id hole in the recent window (spot ≤ 2 M ids, futures ≤ 300 k and within the 2-day REST reach; larger holes wait for the next Vision file). Re-runs daily at 03:00 UTC. Source-confirmed gaps → `known_gaps`. Disk guard stops backfill below `min_free_disk_gb`. Test (3 days × 5 instruments, scratch dir): 15 cold day files, bridge filled 287,099 ids, **integrity check: 0 gaps, 0 duplicates in every table**.
- Why this way: spec §2.1-a fastest path + D-009/D-020; CPU isolation from the WS loop (D-019 lesson).
- Notes/open issues: full production backfill ≈ 16 GB of zips at ≈1 MB/s → several hours in the background.
- The exact next step: —

### Phase P3.3: REST gap-fill
- Status: ✅ Completed
- Description: klines via startTime, aggTrades via fromId, metrics/funding; closed candles only.
- Affected files: `src/tradingsystem/ingest/binance/fetch.py`, `service.py`
- What was done: async paginators (`klines`, `agg_trades_from_id`, `funding`, `metrics_5m` from the 5 ratio endpoints). Live service gap-fill starts from **pre-live marks** (last stored row captured before the WebSockets connect) — fixes a real bug where early live rows made tables look up to date — and, after a reconnect, from the outage start; aggTrades from the last seen id to the first live id.
- Why this way: spec §2.2 — fetch exactly what is missing, idempotently.
- Notes/open issues: —
- The exact next step: —

### Phase P3.4: Resume state machine + ingestion events
- Status: ✅ Completed
- Description: per-table resume (live-first, D-009); `ingestion_events` with outage durations.
- Affected files: `src/tradingsystem/ingest/common/appdb.py`, `ingest/binance/service.py`, `tools/check_integrity.py`
- What was done: `app.db` (`collector_status`, `latest_quote`, `ingestion_events`); connect/disconnect/gap-fill events with durations. Restart test: run 100 s → stop 40 s → run 110 s → integrity: **0 candle gaps, 0 aggTrade id gaps, 0 duplicates** across all 5 instruments.
- Why this way: spec §2.2 steps 1–5.
- Notes/open issues: the resume "FSM" is realised as pre-live marks + outage-start gap-fill + the backfill worker's gap-driven passes rather than a separate class.
- The exact next step: —

### Phase P3.5: WebSocket live ingestion
- Status: ✅ Completed
- Description: combined streams; closed-kline upsert + forming candle; batched writes; conflation; stall watchdog; 23 h rotation; gap-fill after reconnect.
- Affected files: `src/tradingsystem/ingest/binance/{ws,service,parsers}.py`
- What was done: `StreamConnection` (reconnect with backoff, stall timeout 30–60 s, planned rotation < 24 h); routing per D-016 (spot combined; USDⓈ-M `/market` + `/public`); handlers for kline (closed → candles, all → `forming_candles`), aggTrade, bookTicker (price-change conflation), markPrice (last per minute), forceOrder. Buffers flushed every 0.5 s in a worker thread, validated first, one transaction per instrument. 150 s live test: 200 k messages, 48 k rows, 0 rejected, 0 reconnects.
- Why this way: spec §2.1-b; never block the WS loop.
- Notes/open issues: —
- The exact next step: —

### Phase P3.6: Depth ingestion
- Status: ✅ Completed
- Description: D-018 — REST snapshots every 60 s reduced to ±% liquidity bands.
- Affected files: `src/tradingsystem/ingest/binance/parsers.py` (`depth_bands`), `service.py`
- What was done: spot 5000-level snapshots (weight 250) every 60 s → cumulative qty/notional within ±0.1/0.25/0.5/1/2/5 % bands (bands beyond the snapshot's reach are omitted, never extrapolated).
- Why this way: D-018.
- Notes/open issues: futures depth history available from Vision `bookDepth` if P6.10 needs it.
- The exact next step: —

### Phase P3.7: Futures context ingestion
- Status: ✅ Completed
- Description: funding, mark price, OI, metrics, liquidations.
- Affected files: `src/tradingsystem/ingest/binance/{service,fetch,parsers}.py`
- What was done: funding (REST, history + 30-min poll), mark/index/funding per minute (markPrice@1s), open interest (60 s poll), metrics (5 ratio endpoints every 5 min + Vision daily history), liquidations (forceOrder, flagged partial by nature).
- Why this way: derivatives context for BTC/ETH and the gold proxy.
- Notes/open issues: XAUUSDT flow use still subject to P1.11.
- The exact next step: —

### Phase P3.8: Collector status and latest quote
- Status: ✅ Completed
- Description: `collector_status`, `latest_quote` tables updated by ingesters.
- Affected files: `src/tradingsystem/ingest/common/appdb.py`
- What was done: status every 5 s per venue (state, last data time, messages, reconnects, rows written/rejected, REST requests/errors, clock offset); latest bid/ask per instrument every 0.5 s; backfill progress under `binance_backfill`.
- Why this way: spec §8.1/§8.5.
- Notes/open issues: —
- The exact next step: —

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
- Status: ✅ Completed
- Description: pinned path, credentials only when configured, account assertion, symbol selection.
- Affected files: `src/tradingsystem/ingest/mt5/terminal.py`
- What was done: `MT5Terminal.connect()` (initialize with pinned path; asserts server and demo/real trade mode against the profile → `AccountMismatch`), `select_symbols`, `ensure`, `account()` snapshot.
- Why this way: D-019/D-023 — never ingest from or trade on an unexpected account.
- Notes/open issues: hung MT5 calls cannot be pre-empted in-process (GIL) → the supervisor (P5.1) restarts a process whose heartbeat (`collector_status.updated_ms`, every 2 s) goes stale.
- The exact next step: —

### Phase P4.2: Server-time ↔ UTC converter
- Status: ✅ Completed
- Description: measured piecewise model incl. ambiguous hours; tests at DST transitions.
- Affected files: `src/tradingsystem/ingest/mt5/servertime.py`, `tests/unit/test_servertime.py`
- What was done: implemented during P1.7 (D-014); 11 tests.
- Why this way: D-014.
- Notes/open issues: —
- The exact next step: —

### Phase P4.3: MT5 backfill (rates + ticks)
- Status: ✅ Completed
- Description: rates per TF gap-driven on the server-time grid; ticks per UTC day newest-first → cold.
- Affected files: `src/tradingsystem/ingest/mt5/{backfill,convert}.py`, `tests/unit/test_mt5_convert.py`
- What was done: `MT5Backfill` worker (own process, daily re-run 04:00 UTC): earliest-bar discovery, `mt5_candle_gaps` (server grid, session-aware), month chunks via `copy_rates_range(epoch seconds)`, validation, source gaps → `known_gaps`; ticks day by day → cold Parquet, progress in `vision_done`, stops after 10 trading days without ticks. Tick keys `utc_ms*1000+seq` identical to the live path (tested on real XAU ticks, incl. a poll cut mid-millisecond).
- Why this way: spec §2.1-a; D-019 (heavy calls isolated from the live poller).
- Notes/open issues: production backfill started 2026-09-25 10:33 UTC (≈7 y of M1 bars × 3 symbols, ≈2 y of XAU ticks).
- The exact next step: —

### Phase P4.4: MT5 live poller
- Status: ✅ Completed
- Description: 50 ms tick polling with cursor de-dup; closed bars each second; forming bar; flush 0.5 s; heartbeat 2 s.
- Affected files: `src/tradingsystem/ingest/mt5/service.py`, `tools/verify_mt5_ticks.py`
- What was done: `MT5LiveService` (pre-live cursor resume from hot/cold, short gap-fill of rates, reconnect with backoff and `resumed` events with outage duration, market-closed status). Test: run 75 s → stop 30 s → run 60 s → **stored ticks identical to a fresh `copy_ticks_range` re-fetch for all 3 symbols (0 missing, 0 extra, 0 dup)**; candles 0 gaps on the server grid (integrity checker made MT5-aware).
- Why this way: spec §2.1-b / §2.2.
- Notes/open issues: production live capture started 2026-09-25 10:33 UTC.
- The exact next step: —

### Phase P4.5: Session-aware states
- Status: ✅ Completed
- Description: market-closed vs error; no false gaps on weekends/daily break.
- Affected files: `src/tradingsystem/core/sessions.py`, `ingest/mt5/service.py`, `ingest/mt5/convert.py`
- What was done: status `market_closed` when every MT5 symbol's calendar is closed; gap detection skips closed-session bars (NY-time calendar for gold, Saturday maintenance for Windsor crypto).
- Why this way: spec §9 — no silent gaps, but no false alarms either.
- Notes/open issues: holidays surface as source-confirmed known gaps.
- The exact next step: —

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
- Status: ✅ Completed
- Description: one command runs every layer; restarts; graceful Windows stop; no orphans; survives sleep.
- Affected files: `src/tradingsystem/supervisor/{supervisor,control,procs,winops}.py`, `core/logsetup.py`, `ingest/mt5/terminal.py`, `scripts/*.bat|ps1`
- What was done: `run all` spawns ingest-binance, ingest-mt5, engine, executor, api in a Windows Job Object; restart with backoff; stale heartbeat → tree kill + restart. Audit fixes (2026-09-26, D-032): `run all --detach | --stop | --status` (the supervisor runs outside any console/job, so it no longer dies with the shell or agent session that started it — production had stopped at 22:07 UTC on 2026-09-25 for that reason); single-instance lock per data dir (an older lock-less build is detected and refused); suspend/clock-jump aware watchdog (QueryUnbiasedInterruptTime; resume grace instead of killing healthy children after sleep); its own `supervisor` heartbeat row; child stdout/stderr → rotating `logs/<svc>.stderr.log`; the MT5 terminal is started by the supervisor outside its job and never killed with a child tree, and services under it never launch a closed terminal themselves (`TS_MT5_NO_LAUNCH`); keep-awake while running.
- Why this way: spec §11.7; D-019; audit findings stall F2/F7/F8, OPS-03/04/05/06.
- Notes/open issues: `other_supervisors()` matches any `-m tradingsystem run` on the machine (one data dir in practice); a starved supervisor (stall) grants repeated grace periods.
- The exact next step: —

### Phase P5.2: Resource profiles
- Status: 🔄 In Progress
- Description: `low` / `standard` profiles; measured RSS.
- Affected files: `config/config.yaml` (`resources`), dashboard Health tab
- What was done: measured on the dev PC (2026-09-25 11:14 UTC, all services + both backfills + research recorder): executor 154 MB, binance backfill worker 126 MB, ingest-binance 85, engine 82, api 78, mt5 backfill worker 74, ingest-mt5 63, supervisor 35 MB → core services ≈ 500 MB, ≈ 700 MB while backfilling; MT5 terminal 392 MB. The PC itself was at 97.8 % RAM because of other apps (Claude desktop 1.4 GB, MicrosoftHost 1.4 GB, ChatGPT, Edge…).
- Why this way: spec §11.8 (must run on 4 GB).
- Notes/open issues: on a 4 GB PC the budget is ≈ Windows 2–2.5 GB + MT5 0.4 GB + our ≈0.5–0.7 GB → workable only with other apps closed; `low` profile knobs (DuckDB limits, smaller SQLite cache, engine-in-subprocess) not yet wired into every service.
- The exact next step: wire `resources.<profile>` into engine/backfill (DuckDB memory, cache sizes) and re-measure with `RESOURCE_PROFILE=low`.

### Phase P5.3: Ops runbook 👤
- Status: 🔄 In Progress
- Description: sleep off, Task Scheduler autostart, time sync, MT5 settings.
- Affected files: `docs/ops_windows.md`, `scripts/{start,stop,restart,status,start_recorder,check_ops,install_autostart,uninstall_autostart}.bat`
- What was done: runbook with exact power (lid/sleep buttons/Wi-Fi power saving), time-sync, Windows Update and MT5 steps; double-click scripts: start (detached), stop, restart, status, start_recorder (P1.12), check_ops (verifies the settings); autostart installer/uninstaller for Task Scheduler (written, never run by the agent).
- Why this way: the audit proved the 41-min stall was a lid-close sleep on battery (not software); OPS-01/OPS-08/BF-13.
- Notes/open issues: power settings and autostart are the user's to apply (H2/H7).
- The exact next step: user applies `docs/ops_windows.md` §2–§4 and starts with `scripts\start.bat`; then `scripts\check_ops.bat` should be all green.

### M6 — Quantitative analysis

### Phase P6.1: As-of frame loader
- Status: ✅ Completed
- Description: closed candles + flagged forming candle; stale/gap flags.
- Affected files: `src/tradingsystem/analysis/frames.py`
- What was done: `load_frame(reader, inst, tf, bars, as_of, calendar)` → only bars closed by `as_of`; forming bar from `forming_candles`; quality flags (`stale` when the session is open and the last bar ended > max(2 TF, 3 min) ago; recent gaps for Binance ≤1h TFs).
- Why this way: spec §9 — never analyse silently broken data; as-of semantics make replay possible.
- Notes/open issues: —
- The exact next step: —

### Phase P6.2: Indicators
- Status: ✅ Completed
- Description: EMA/SMA, RSI, MACD, ATR, Bollinger, ADX, VWAP (anchored/session), realized vol, percentile rank.
- Affected files: `src/tradingsystem/analysis/indicators.py`, `tests/unit/test_indicators.py`
- What was done: numpy implementations; **match the `ta` library** on real BTCUSDT 1m candles (RSI/ATR 1e-6, MACD/ADX ≤1e-3) and pass truncate-at-t causality tests.
- Why this way: speed on a 4 GB i3 + verifiable correctness.
- Notes/open issues: —
- The exact next step: —

### Phase P6.3: Pivots and structure labels
- Status: ✅ Completed
- Description: pivots with confirmation lag; structure state.
- Affected files: `src/tradingsystem/analysis/structure.py`, `tests/unit/test_structure.py`
- What was done: fractal pivots (L/R bars, known at i+R); forward-walking state machine.
- Why this way: no look-ahead by construction.
- Notes/open issues: —
- The exact next step: —

### Phase P6.4: BOS/CHoCH, EQH/EQL, premium/discount, sweeps
- Status: ✅ Completed
- Description: SMC structure + liquidity; causality test.
- Affected files: `src/tradingsystem/analysis/structure.py`, `tests/unit/test_structure.py`
- What was done: close-based BOS/CHoCH, wick-only sweeps, equal-high/low liquidity pools (tol 0.1 ATR) with sweep tracking, premium/discount with OTE on a dealing range extended to the running extreme. Causality: events/pivots/zones computed on data truncated at t equal the full-run results known by t (4 cut points on real data).
- Why this way: spec §3.1 SMC / liquidity sweeps.
- Notes/open issues: —
- The exact next step: —

### Phase P6.5: FVG and order blocks
- Status: ✅ Completed
- Description: FVG + OB with mitigation state; causality test.
- Affected files: `src/tradingsystem/analysis/zones.py`
- What was done: 3-candle FVGs (min 0.1 ATR), order blocks = last opposite candle of the breaking leg with displacement strength, mitigation (touch, fill %, invalidation/breaker) as of any bar; nearest-active helper. Mitigation-as-of-t equality tested.
- Why this way: spec §3.1 SMC / order blocks.
- Notes/open issues: —
- The exact next step: —

### Phase P6.6: Price action patterns
- Status: ✅ Completed
- Description: candle patterns, ranges.
- Affected files: `src/tradingsystem/analysis/price_action.py`
- What was done: doji, pin bars, engulfing, inside/outside bar, marubozu on closed bars; range/compression state in ATR units.
- Why this way: spec §3.1 price action.
- Notes/open issues: —
- The exact next step: —

### Phase P6.7: Bar delta / CVD
- Status: ✅ Completed
- Description: exact bar delta, CVD, divergences.
- Affected files: `src/tradingsystem/analysis/orderflow.py`, `tests/unit/test_orderflow.py`
- What was done: delta = 2·taker_buy − volume; CVD; price-vs-CVD divergence on the last two confirmed pivots.
- Why this way: spec §3.1 order flow (real for Binance).
- Notes/open issues: —
- The exact next step: —

### Phase P6.8: Footprint
- Status: ✅ Completed
- Description: aggTrades → per-bar volume at price; POC, stacked imbalances, absorption.
- Affected files: `src/tradingsystem/analysis/orderflow.py`, `tests/unit/test_orderflow.py`
- What was done: footprint per 1m merged to the decision TF; diagonal imbalances (3:1, stacks ≥3), absorption heuristic. **Verified on live data: per-minute footprint volume == Binance kline volume and buy volume == taker-buy** (after the audit fix). Computed on the fly for the last 8 decision bars (fast enough; no derived table needed).
- Why this way: spec §3.1 footprint.
- Notes/open issues: the verification test first exposed a real 479-trade hole after a network flap → fixed (D-025).
- The exact next step: —

### Phase P6.9: Profiles
- Status: ✅ Completed
- Description: volume profile BTC/ETH; XAU TPO (real) + tick-volume profile (approx).
- Affected files: `src/tradingsystem/analysis/orderflow.py`
- What was done: value area (70 %), footprint-based profile (real), TPO from real 1m bars, tick-volume profile labelled `approx`.
- Why this way: spec §3.2 — no stand-in data without disclosure.
- Notes/open issues: —
- The exact next step: —

### Phase P6.10: Depth and derivatives features
- Status: ✅ Completed
- Description: funding, OI change, positioning ratios, liquidations, mark/index.
- Affected files: `src/tradingsystem/analysis/snapshot.py` (`_derivatives`)
- What was done: funding (last 3), OI change 1h/4h/24h, top-trader/global long-short and taker ratios, mark vs index, liquidations last hour (flagged partial); gold receives XAUUSDT derivatives flagged `proxy`.
- Why this way: derivatives context raises accuracy for crypto (spec §3.1 "anything valuable").
- Notes/open issues: depth-band features collected (P3.6) but not yet summarised in the payload — add if prompts/analysis show value.
- The exact next step: —

### Phase P6.11: Context features
- Status: ✅ Completed
- Description: sessions/killzones, reference levels, regime, MTF confluence, VWAP.
- Affected files: `src/tradingsystem/analysis/context.py`, `indicators.py`
- What was done: Tokyo/London/New York sessions and ICT killzones in their own time zones (DST-correct); PDH/PDL/PDC, day/week open, Asia range (UTC day for crypto, 17:00-NY trading day for gold); ADX/DI regime + ATR percentile; EMA 20/50/200 stack; weighted multi-timeframe confluence score; session VWAP (approx for tick volume).
- Why this way: spec §3.1 / §3.3.
- Notes/open issues: —
- The exact next step: —

### Phase P6.12: Capability registry
- Status: ✅ Completed
- Description: per pair × analysis: real / approx / proxy / unavailable + reason.
- Affected files: `src/tradingsystem/analysis/registry.py`, `docs/capability_matrix.md`
- What was done: derived from configured instruments (venue, datatypes, `flow_proxy_approved`). Gold: delta/footprint unavailable (proxy pending P1.11), volume-weighted/volume profile approx, TPO real, derivatives proxy, depth unavailable. Matrix document generated.
- Why this way: spec §3.2 decision table, grounded in measured data.
- Notes/open issues: flipping `flow_proxy_approved` after P1.11 switches gold order flow to `proxy`.
- The exact next step: —

### Phase P6.13: Snapshot builder
- Status: ✅ Completed
- Description: per-pair deterministic payload with data-quality flags; hashed.
- Affected files: `src/tradingsystem/analysis/snapshot.py`, `tests/unit/test_snapshot.py`
- What was done: `SnapshotBuilder.build(pair, as_of, account, history)` → meta (price_reference = analysis instrument, execution instrument, config hash, data warnings, payload hash), account, market (causal quotes at as_of from stored bookTicker/ticks, execution spread, basis), capabilities, reference levels, 7 timeframes (recent candles, structure/events, premium/discount, nearest active FVGs/OBs, liquidity pools, indicators, patterns, range, regime), confluence, order flow (delta/CVD/divergence, footprint of the last 8 decision bars, TPO / tick-volume profile), derivatives, history. ≈4–7 k tokens per pair. Integration tests: identical hash on rebuild, **no timestamp after as_of**, flags per capability.
- Why this way: spec §4.3 — everything the trader needs, nothing fabricated.
- Notes/open issues: `history` filled by the decision store (P8.7).
- The exact next step: —

### Phase P6.14: Speed budget
- Status: ✅ Completed
- Description: full 3-pair cycle well inside the decision TF.
- Affected files: `docs/benchmarks/analysis_speed.md` (numbers below)
- What was done: measured on the i3-1005G1 while both ingesters + backfills ran: BTCUSDT 1.45 s (cold, incl. imports), ETHUSDT 0.39 s, XAUUSD 0.22 s → ≈2 s for all pairs vs a 15-minute decision timeframe.
- Why this way: spec §3.3/§9.
- Notes/open issues: re-measure on the `low` profile in P5.2.
- The exact next step: —

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
- Status: ✅ Completed
- Description: translate levels analysis → execution instrument; pre-trade basis check.
- Affected files: `src/tradingsystem/execution/price_mapping.py`, `tests/unit/test_risk_gate.py`
- What was done: `translate(rec, basis, tick)` shifts entry/SL/TPs/review levels by the live basis (execution mid − analysis mid), preserving structural distances; `check_basis` rejects when the live basis deviates from its 60-min median (sampled per minute from stored bookTicker/ticks) by more than `execution.max_basis_deviation_pct`. Spread side handled by the gate (BUY fills at ask, SL on bid).
- Why this way: spec §7.2, D-027.
- Notes/open issues: threshold is provisional until P7.1 measures the basis distribution.
- The exact next step: —

### Phase P7.4: Binance execution backend (only if Option B) 👤
- Status: ⏳ Not Started
- Description: Binance backend on testnet.
- Affected files: `src/tradingsystem/execution/backends/binance.py`
- What was done: — · Why this way: — · Notes/open issues: conditional.
- The exact next step: after P7.2.

### M8 — AI layer

### Phase P8.1: Output contract v1
- Status: ✅ Completed
- Description: pydantic contract + JSON schema + semantic validators.
- Affected files: `src/tradingsystem/ai/contract.py`, `tests/unit/test_contract.py`
- What was done: `Recommendation` (normalised order types, entry price/range, ≤4 TPs with close fractions, SL, valid_until, price_reference, structured management + next_review, data_quality_notes, contract_version) with semantic rules (BUY/SELL need matching order type, entry, SL on the correct side, TPs in direction and ordered, risk fields; NO_TRADE carries no levels; aware UTC timestamps); `rr_computed()` from worst fill; `TimeframeAssessment`, `RecommendationSet`, `AssessmentSet`, `RiskReview` (reject ⇒ NO_TRADE). 15 tests.
- Why this way: spec §0/§5 — invalid trades can never pass the contract.
- Notes/open issues: —
- The exact next step: —

### Phase P8.2: Provider adapters
- Status: 🔄 In Progress
- Description: Gemini first; Anthropic, OpenAI, OpenAI-compatible (Grok, Groq, OpenRouter, DeepSeek, Ollama).
- Affected files: `src/tradingsystem/ai/providers/{base,gemini,anthropic_claude,openai_chat,__init__}.py`, `config/config.yaml`
- What was done: common `LLMProvider` (latency, JSON extraction, per-model pricing, `priced`), provider-specific transport schemas (unsupported keywords stripped; full contract validated client-side). Gemini via google-genai (`response_json_schema`); Anthropic via the official SDK (`output_config.format` json_schema + effort, cached system prompt, server-side refusal fallbacks `fallbacks="default"`, default model `claude-opus-5`, prices opus-5 5/25, sonnet-5 2/10, haiku-4-5 1/5 USD/MTok); OpenAI chat completions with `structured_output` native/json_object/prompt per endpoint. SDK parameter names verified by introspection of the installed packages.
- Why this way: spec §4.1 — switch provider with one line (`ACTIVE_AI_PROVIDER`).
- Notes/open issues: no live call made yet — needs H4 (`GOOGLE_API_KEY`). OpenAI/Grok/DeepSeek prices not configured → blocked by the Cost Governor until set.
- The exact next step: when the user adds `GOOGLE_API_KEY` (and RPM/RPD) run a live contract-valid generation on a real snapshot (after P6.13).

### Phase P8.3: Repair/retry, rate limits, cost accounting, Cost Governor
- Status: ✅ Completed
- Description: timeouts, RPM/RPD limiter, token/cost accounting, caps, cost-to-profit ratio, degradation ladder.
- Affected files: `src/tradingsystem/ai/{budget,repair}.py`, `tests/unit/test_ai_budget_repair.py`
- What was done: `UsageStore` (`app.db:ai_usage`), `RateLimiter` (RPM spacing + persisted RPD), `CostGovernor` (unknown price → blocked; daily/monthly caps; ROI ladder 0→1→2→3), `generate_validated` (repair loop feeding exact validator errors back, bounded retries, refusal/budget handling). 8 control-flow tests with a scripted provider double.
- Why this way: D-004.
- Notes/open issues: `profit_fn` gets wired to paper/demo outcomes in M9.
- The exact next step: —

### Phase P8.4: Prompt library
- Status: ✅ Completed
- Description: versioned prompts per role with mandatory clauses; lint.
- Affected files: `src/tradingsystem/ai/prompts/**`, `tests/unit/test_prompts.py`
- What was done: shared persona (top-down professional trader) + 12 non-negotiable rules (evidence only, data-quality flags, structural SL ≥ k·ATR, targets from real liquidity, RR ≥ min from worst fill, NO_TRADE as a decision, execution realism incl. bid/ask side, order-type discipline, account constraints, calibrated confidence, consistency with recent decisions, re-analysis conditions); roles: agent_per_pair, single_agent_global, timeframe_analyst, coordinator (conflict-resolution rules), risk_reviewer. `string.Template` rendering, stable system prompts (cacheable), `prompt_hash`, `library_hash`. 11 lint tests.
- Why this way: spec §4.2–§4.4.
- Notes/open issues: prompts will be tuned after the first real outputs (P8.8/P11.2).
- The exact next step: —

### Phase P8.5: Orchestrator and modes
- Status: ✅ Completed
- Description: 4 required modes + risk-reviewer + multi-provider consensus; parallelism cap; failure policy.
- Affected files: `src/tradingsystem/ai/orchestrator.py`, `tests/unit/test_orchestrator.py`, `tests/fixtures/real/payload_xauusd.json`
- What was done: `Orchestrator.run_cycle` builds + stores payloads (with recent-decision history), then dispatches: single_agent_global (RecommendationSet; a missing pair → invalid), agent_per_pair, agent_per_timeframe (6 analysts over all pairs → per-pair coordinator), agent_per_pair_and_timeframe (6 analysts per pair → coordinator), agent_per_pair_with_risk_reviewer (reviewer can approve/modify/reject; an unreviewable trade is withheld), multi_provider_consensus (deterministic strict-majority aggregation, lowest confidence). Cost Governor degrades the mode (level ≥1 → agent_per_pair, 3 → paused). Semaphore = `max_parallel_calls`. 6 tests with a scripted provider and a real payload.
- Why this way: spec §4.2 — all architectures ready, one active via `AGENT_MODE`.
- Notes/open issues: live run pending H4.
- The exact next step: —

### Phase P8.6: Trigger policies + next-review scheduler
- Status: ✅ Completed
- Description: every_close / on_setup_event / hybrid; next_review time & price conditions.
- Affected files: `src/tradingsystem/ai/triggers.py`, `src/tradingsystem/analysis/engine.py`
- What was done: setup detection on closed bars (strong: BOS/CHoCH or liquidity sweep on the decision TF/1h at the last bar; weak: price inside a bias-aligned OB/FVG, within 0.3 ATR of unswept liquidity, reversal candle in a zone, stacked footprint imbalances, absorption, price/CVD divergence) → fire on ≥1 strong or ≥2 weak; next_review (minutes, price above/below, candle close above/below); hybrid idle timeout (120 min); per-pair spacing 15 min; market-closed pairs skipped. `engine --triggers` on live data fired for ETH (1h CHoCH + stacked imbalances + divergence) and XAU (1h liquidity sweep).
- Why this way: quality over quantity (spec §6) and free-tier quotas (D-003).
- Notes/open issues: thresholds to be calibrated by the P6.15 replay study.
- The exact next step: —

### Phase P8.7: Decision persistence
- Status: ✅ Completed
- Description: snapshot, prompt hash, raw/parsed output, usage, latency, config hash, git SHA — linked.
- Affected files: `src/tradingsystem/ai/store.py`
- What was done: `app.db` tables `ai_payloads` (zlib JSON by payload hash), `ai_decisions` (status, decision, levels, RR, validity, raw text, errors, cost, tokens, latency, execution_state, outcome & virtual-outcome fields), `ai_sub_outputs` (analysts / reviewer / consensus members). `recent()` feeds the snapshot `history` block.
- Why this way: spec §8.3.
- Notes/open issues: outcomes are written by the execution layer (M9).
- The exact next step: —

### Phase P8.8: Live run of all modes (Gemini free)
- Status: ⏳ Not Started
- Description: every mode on live data; valid outputs; requests/cost per cycle recorded.
- Affected files: `docs/ai_modes_run.md`
- What was done: — · Why this way: — · Notes/open issues: —
- The exact next step: after P8.7.

### Phase P8.9: Claude on the user's subscription (Claude Code CLI)
- Status: 🔄 In Progress
- Description: use the user's existing Claude subscription as the AI brain instead of pay-per-token APIs; Gemini free as fallback.
- Affected files: `src/tradingsystem/ai/providers/claude_code.py`, `ai/providers/{base,__init__}.py`, `ai/orchestrator.py`, `analysis/engine.py`, `core/settings.py`, `config/config.yaml`, `.env.example`, `tests/unit/test_claude_code_provider.py`, `tests/fixtures/real/claude_code_*.json`
- What was done: provider `claude_code` spawns `claude -p` with our system prompt file, snapshot on stdin, `--json-schema`, `--tools ""`, `--strict-mcp-config`, `--setting-sources ""`, `--no-session-persistence`, empty working dir (pure analysis). Allow-listed child env: none of our secrets, no `ANTHROPIC_API_KEY`/`ANTHROPIC_BASE_URL`; API-key sign-ins refused (`authMethod`/`apiKeySource`). Login checked with `claude auth status` (first check inline, later ones in a background thread); "not signed in" → 5-min cooldown; usage limit → cooldown until reset; calls serialized; a cancelled/timed-out call kills its CLI process; timeout 180 s. Orchestrator routes to `ai.fallback_provider` (gemini) while unavailable; a fallback equal to the active provider is ignored (never a startup error). Reviewed adversarially twice (billing safety, secret isolation, subprocess behaviour). Verified against the real CLI (full prompt, full schema, real 26 k payload).
- Why this way: D-030 — the subscription is already paid; the same load via the API ≈ $50–100/month, too much for a $100 account.
- Notes/open issues: shares the plan's 5-hour/weekly limits with interactive use (`rpd: 120`). The success-path parser test uses the real envelope structure; replace with a real capture after H11. First live cycle 2026-09-26 00:45 UTC: BTCUSDT SELL_LIMIT (RR 1.99, conf 57) and ETHUSDT NO_TRADE — valid, but slow/expensive until D-035.
- The exact next step: after H11 (sign-in) and the restart, run `engine --once --pairs BTCUSDT` (one real call), save its CLI output as a fixture, measure latency/RSS/tokens, then watch one day of paper cycles for limit usage.

### M9 — Execution

### Phase P9.1: Risk gate
- Status: ✅ Completed
- Description: deterministic, AI-independent pre-order checks.
- Affected files: `src/tradingsystem/execution/risk_gate.py`, `tests/unit/test_risk_gate.py`
- What was done: kill switch, SL present, market open, quote freshness, basis, validity window, recommendation age, confidence floor (55), SL side, SL ≥ max(stops_level+spread, 0.5 ATR) and ≤ 5 ATR, spread ≤ 20 % of SL, RR after costs from the executable fill ≥ min_rr, % risk sizing (rejects when even the minimum lot exceeds max risk), effective leverage cap, max open positions, correlated BTC/ETH exposure, daily loss limit. Every check is returned with detail. 16 table-driven tests incl. the $100-account case (0.01 lot gold, 12-point SL = 12 % → rejected).
- Why this way: spec §0 / §7.3; D-021.
- Notes/open issues: H8 — the user must choose `max_risk_per_trade_pct` knowingly for a $100 account.
- The exact next step: —

### Phase P9.2: Position sizing
- Status: ✅ Completed
- Description: % risk → lots, rounded down; minimum-lot rule.
- Affected files: `src/tradingsystem/execution/sizing.py`
- What was done: lots = floor(risk_usd / (|entry−SL| × USD-per-unit-per-lot) / step)·step; below the minimum lot → use the minimum only if its real risk ≤ max, else reject; actual risk % always reported. USD-per-unit-per-lot confirmed with `order_calc_profit` on the demo (XAU 100, BTC 1).
- Why this way: spec §7.3.
- Notes/open issues: —
- The exact next step: —

### Phase P9.3: Paper backend
- Status: ✅ Completed
- Description: tick-accurate simulation on real bid/ask.
- Affected files: `src/tradingsystem/execution/backends/paper.py`, `tests/unit/test_paper_backend.py`
- What was done: legs per TP (split by close fraction on the volume step; single leg when too small), market fills at ask/bid, limit/stop triggers on the correct side, SL exits on the opposite quote (gap slippage), TP as limit, expiry of pending orders, breakeven after TP k, idempotent placement, account equity/unrealised/risk by pair, realised PnL for the Cost Governor. Tests on real XAU ticks.
- Why this way: spec §9 dry-run mode.
- Notes/open issues: trailing-ATR management rule not simulated yet (breakeven only).
- The exact next step: —

### Phase P9.4: MT5 backend
- Status: ✅ Completed
- Description: request builder, order_check, retcodes, idempotency, account assertion.
- Affected files: `src/tradingsystem/execution/backends/mt5_backend.py`, `src/tradingsystem/execution/retcodes.py`
- What was done: FOK filling from symbol spec, SL/TP attached, pending expiration converted to server time, multi-TP legs, `order_check` before any send, duplicate detection by comment tag, bounded retries only for retryable codes (timeouts verified before resending), post-fill slippage, full TRADE_RETCODE map, SL modify / cancel helpers, account assertion (server + demo/real). **Dry run on the demo: both MARKET and BUY_LIMIT two-leg requests passed `order_check`** — no order sent.
- Why this way: spec §7.3 — no silent failures.
- Notes/open issues: real sends only after H6.
- The exact next step: —

### Phase P9.5: Demo tests 👤
- Status: ✅ Completed
- Description: every order path on the demo account at the minimum size; real costs.
- Affected files: `tools/demo_order_test.py`
- What was done: 2026-09-26 01:19 UTC, ETHUSD@ 0.01 through the production `MT5Backend`: BUY_LIMIT 7 % below the market with SL/TP and a 30-min expiry (server time +3 h verified) → found at the broker with magic + tag → cancelled → settled "not filled"; MARKET BUY → filled at the requested price (no slippage), SL/TP attached → closed → deals read: −$0.22 = the spread, commission 0, swap 0. Executor smoke test in demo mode: account seen by the gate (balance 99.78, realised today −0.22).
- Why this way: H6 approved by the user (D-036).
- Notes/open issues: BTC/XAU paths not exercised separately (same code; different contract sizes).
- The exact next step: —

### Phase P9.6: Executor service
- Status: 🔄 In Progress
- Description: manual/auto queues, re-validation, paper simulation, outcomes.
- Affected files: `src/tradingsystem/execution/executor.py`
- What was done: `python -m tradingsystem executor` — candidates (manual: queued by the dashboard; auto: every new valid trade), live quotes, basis check + translation, gate, backend placement (paper / MT5), full gate detail stored on the decision, paper tick advancement from the stored execution ticks, outcome settlement (pips / USD / %), heartbeat. Smoke-tested live in paper mode ($100 paper account).
- Why this way: spec §7.1.
- Notes/open issues: MT5 position management (breakeven/trailing on live positions) and outcome settlement from `history_deals_get` for demo/live still to be written (needed before P9.5/P11.3).
- The exact next step: implement MT5 position manager + deal-based outcomes, then run P9.5 with the user's approval.

### Phase P9.7: Mode guards + kill switch 👤
- Status: ⏳ Not Started
- Description: live requires Real-server match + flag + typed confirmation.
- Affected files: `src/tradingsystem/execution/executor.py`
- What was done: — · Why this way: D-012. · Notes/open issues: H9.
- The exact next step: after P9.6.

### Phase P9.8: Virtual outcomes
- Status: ✅ Completed
- Description: outcome of every trade idea on real subsequent prices.
- Affected files: `src/tradingsystem/execution/executor.py` (`evaluate_virtual`), `tests/unit/test_virtual_outcomes.py`
- What was done: entry trigger within validity, then SL vs TP1 first touch on real 1m bars of the analysis instrument (same-bar ambiguity counted as SL), R multiple; stored in `ai_decisions.virtual_outcome/virtual_r`.
- Why this way: measure AI quality independently of execution constraints (e.g. the small account).
- Notes/open issues: —
- The exact next step: —

### Phase P9.9: Economic-calendar blackout (optional)
- Status: ⏳ Not Started
- Description: MQL5 service exports calendar; gate applies blackout for XAU.
- Affected files: `mql5/CalendarExport.mq5`, `risk_gate.py`
- What was done: — · Why this way: — · Notes/open issues: optional.
- The exact next step: after P9.1.

### M10 — API and dashboard

### Phase P10.1: FastAPI read API + WS publisher
- Status: ✅ Completed
- Description: read endpoints; WebSocket push; localhost bind; token.
- Affected files: `src/tradingsystem/api/app.py`
- What was done: `/api/pairs|status|candles|decisions|decisions/{id}|performance|events`, `/ws` (1 s push of quotes, collector/engine/executor state, new decisions — server-side DB polling), stale-heartbeat flag, cached process scan; bound to 127.0.0.1.
- Why this way: spec §8, D-007.
- Notes/open issues: —
- The exact next step: —

### Phase P10.2: Web build pipeline
- Status: ✅ Completed
- Description: front-end delivery.
- Affected files: `web/index.html`, `web/static/{app.js,style.css}`, `web/static/vendor/lightweight-charts*`
- What was done: D-028 — no build step: vanilla JS + TradingView lightweight-charts 4.2.3 vendored locally (Apache-2.0 licence file included), served by FastAPI.
- Why this way: lighter on 4 GB machines, no Node toolchain at runtime, easier for any agent to modify.
- Notes/open issues: —
- The exact next step: —

### Phase P10.3: Live panel
- Status: ✅ Completed
- Description: prices, latest decision, collector status.
- Affected files: `web/static/app.js`
- What was done: per-pair cards (analysis price, execution bid/ask/spread, basis, quote ages, latest recommendation with levels) and the collector table (state, last data, heartbeat, counters, last error). Verified in the browser with live data.
- Why this way: spec §8.1.
- Notes/open issues: —
- The exact next step: —

### Phase P10.4: Historical browser
- Status: ✅ Completed
- Description: instrument/TF/range browsing with overlays.
- Affected files: `web/static/app.js`, `/api/candles`
- What was done: any instrument × timeframe × date range (hot + cold), candlestick chart, latest recommendation's entry/SL/TP lines when the instrument matches its price reference. Verified with real BTCUSDT 15m history.
- Why this way: spec §8.2.
- Notes/open issues: —
- The exact next step: —

### Phase P10.5: Recommendation log
- Status: ✅ Completed
- Description: snapshot → AI output → gate → outcome chain.
- Affected files: `web/static/app.js`, `/api/decisions/{id}`
- What was done: decisions table (status, decision, RR, execution state, outcome, virtual outcome, cost) and a detail view: summary, reasoning, instructions, data-quality notes, capability flags, gate checks, recommendation JSON, sub-agent outputs, paper legs, full payload, raw model output.
- Why this way: spec §8.3.
- Notes/open issues: to be exercised with real decisions once H4 is done.
- The exact next step: —

### Phase P10.6: Execute Now
- Status: 🔄 In Progress
- Description: confirm dialog, idempotency, gate result shown.
- Affected files: `web/static/app.js`, `src/tradingsystem/api/app.py`
- What was done: button per recommendation (enabled only for valid, unexpired, not-yet-executed trades) → confirm → `POST /api/decisions/{id}/execute` with `X-Dashboard-Token` header (CSRF-safe) and Origin check → `execution_state='queued'` → executor re-validates with live prices; repeated clicks are idempotent.
- Why this way: spec §8.4 / §7.1 manual mode.
- Notes/open issues: end-to-end click test needs a real decision (after H4).
- The exact next step: after the first real recommendation, click Execute Now in paper mode and verify the gate result and paper legs in the detail view.

### Phase P10.7: Health page
- Status: ✅ Completed
- Description: latency, errors, RSS, disk, AI spend.
- Affected files: `web/static/app.js`
- What was done: processes RSS, free disk, healthy-collector count, AI usage today per provider, ingestion events (disconnects with outage duration, audit fills, gate rejections, supervisor restarts).
- Why this way: spec §8.5.
- Notes/open issues: —
- The exact next step: —

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

### M12 — v2: Claude as operator (docs/handoff_operator_v2.md, D-038…D-043)

### Phase P12.1: Operator memory, venue costs, compact model view (v2 Phase 1)
- Status: ✅ Completed
- Description: the model plans with the venue's real limits and costs, remembers its own plan per pair, sees its gate rejections and track record, and reads a compact view of the snapshot.
- Affected files: `src/tradingsystem/analysis/snapshot.py` (`execution_costs`, `_spreads`), `src/tradingsystem/ai/model_view.py` (new), `ai/orchestrator.py`, `ai/store.py` (`memory`, `performance`, `gate_reason`), `ai/contract.py` (`operator_notes`), `ai/budget.py` (usage extras), `ai/providers/claude_code.py`, `ai/prompts/shared/{core_rules,payload_legend}.md` + every `system.md`, `core/settings.py` (ContractCfg costs, `risk.min_confidence`), `execution/executor.py`, `ingest/mt5/terminal.py`, `config/config.yaml`, tests `test_phase1_operator.py`, `test_execution_costs.py`, `test_prompts.py`, `test_mt5_no_launch.py`
- What was done: `market.execution.costs` (spread now / 1-h median / 24-h p95 / max from stored ticks, stops level, swaps per night at the minimum lot, commission, and the min/max stop distance computed exactly like the risk gate: max(stops level + spread, 0.5 × ATR, spread / 20 %) … 5 × ATR); `account.min_position_risk` uses it. The model reads `model_view(payload)` (short UTC times, column tables for zones and structure events, one data status per timeframe, fewer raw higher-TF candles, grouped capabilities, nulls dropped): −37 % on real payloads, while the stored payload, the trigger policy, the data gate and the dashboard are untouched; a legend in the cached system prompt explains the format. `core_rules` v3: the gate's stop bounds, realistic (p95) spread, swaps, pending-order validity, minimum confidence, maximum recommendation age, gate reasons, and rule 13 (memory). `operator_notes` (≤ 600 chars) is fed back as `memory` on the next cycle; `performance` (30-day record incl. virtual outcomes) and `history.gate_reason` are in every payload. `ai_usage` stores api-equivalent USD, turns and cache writes. Claude CLI starts are staggered 15 s and the OAuth-refresh race / 403 is retried instead of failing the pair (observed after the 2026-09-26 reboot). Services attach only to an MT5 terminal running ≥ 30 s (a loading terminal made `initialize()` launch a second copy inside the supervisor job, 2026-09-26 16:57). **Live acceptance (one call, BTCUSDT, DB copy): valid, 17,996 input tokens (was ~22,900; target ≤ 19,000), 2,501 output, 36 s, 1 turn; NO_TRADE conf 30 with concrete operator notes that were fed back.** 356 unit tests pass.
- Why this way: D-041.
- Notes/open issues: the stop-bound check could not be exercised live (the call returned NO_TRADE); it is covered by unit tests with Windsor's real specs. Cache: the first call writes the prompt cache (17,994 tokens); later calls read the system part.
- The exact next step: —

### Phase P12.2: One instance per pair (v2 Phase 2)
- Status: ✅ Completed (branch feat/instances; the user merges and switches — H14)
- Description: `start.bat BTCUSDT` runs a fully independent system for one pair (own state, logs, port, magic); 10 %/day per instance + 25 % account drawdown stop; sibling-aware supervisor; global MT5/CLI guards; shared AI ledger; migration script.
- Affected files: `core/settings.py` (PathsCfg.state/shared, `_apply_instance`, `InstanceCfg`, `instances:`), `core/filelock.py` (new), `cli.py` (`--instance`, `config --instances`), `supervisor/{supervisor,control,procs}.py`, `execution/{executor,risk_gate}.py`, `execution/drawdown.py` + `execution/exposure.py` (new), `execution/backends/{mt5_backend,paper}.py`, `analysis/engine.py` (`live_account`), `ai/{budget,orchestrator}.py`, `ai/providers/claude_code.py` (`claim_start`), `ingest/mt5/backfill.py`, `api/app.py`, prompts `core_rules` v5 / `payload_legend` v3, `scripts/*.bat|ps1` (per-pair variants, `*_all`, `switch_to_pairs`, `reset_drawdown_stop`), `tools/migrate_instance.py` (new), `tools/health_report.py`, `docs/ops_windows.md` §1a.
- What was done: D-042. 413 unit tests (tests/unit/test_phase2_instances.py, test_phase2_review_fixes.py). Checked on this PC: a pair's supervisor refuses to start next to the running all-pairs system; status per pair; migration on a copy of the production app.db (BTCUSDT 23 decisions, ETHUSDT 20, ledger 39 rows seeded once, source untouched); the health report on production data (read-only); `install_autostart.ps1 -DryRun` in every mode and its layout detection; kill-switch, `*_all` and reset scripts in a scratch project. Review: 3 lenses + adversarial verification, 25 of 26 findings confirmed and fixed (autostart scripts aborting on another task's stderr under ErrorActionPreference=Stop; per-pair kill switch ignored by the all-pairs system / unknown names reported as success; un-migrated pairs startable through the refusal message or restart_all; corrupt peak file clearing a tripped stop; BTC+ETH passing the correlated cap together; stop.bat silent about running pairs; and 19 smaller ones); an independent re-review of the fixes found 6 more (switch_to_pairs with arguments lost its own folder, the peak backup held the pre-trip state, placement-lock waits adding up past the watchdog, the settle wait skipped when placing raised, the health report dropping a dead pair, the migration guard blocking a pair without history) — fixed and tested.
- Why this way: D-038/D-039/D-042. Separate processes per pair keep a crash, a hang or a data outage of one pair away from the others; market data stays shared so nothing is downloaded twice.
- Notes/open issues: not live-tested — needs the merge and the switch (H14). Known and rare (pre-existing): a pair supervisor whose command line lacks `--instance` and whose environment cannot be read looks like the all-pairs one to `stop.bat` without a pair. The three pairs together start 3× the services (≈3× RAM of one pair; the i3/8 GB laptop ran all pairs in one system so far — watch `status_all.bat` / health report after the switch). Deferred review item (c) from Phase 1 (gate_reason prices) is now covered by the legend.
- The exact next step: after H14, check `scripts\status_all.bat` and `tools\health_report.py` (one section per pair), one AI cycle per pair, and the executors' `exposure` / `account_drawdown` in their status rows; then P12.3.

### Phase P12.3: "Sees and manages" — charts, 5-minute screening, position management, prompts v6 (v2 Phase 3)
- Status: ✅ Completed — merged (`dfa19da`) and running in production since 2026-09-27 09:18 UTC (H16 done)
- Description: six chart images (720×400, levels/zones/liquidity/structure/holdings drawn) sent with the text over stream-json; the prompt cache fixed (equity out of the system prompt); Python screens every 5m close and calls Claude only on change (≤ 40 calls/pair/day, event triggers on fills/closes/outcomes/actions); per-role models (`ai.models`) with optional escalation of strong setups; P9.6 position management (declared rules executed deterministically: breakeven, trailing, partials, time stop) plus bounded `position_actions` by the model (tighten/close/cancel only); prompts v6 (persona v2, rules 13–16, legend v4); the gate measures RR on the single leg actually placed at the 0.01-lot minimum.
- Affected files: new `analysis/charts.py`, `execution/management.py`, `execution/action_gate.py`, `tools/replay_triggers.py`, prompts `escalation/*`, `docs/measurements/phase3_live.md`, fixture `tests/fixtures/real/claude_code_stream_json_result.jsonl`; changed `ai/providers/{base,claude_code,__init__}.py`, `ai/{repair,budget,triggers,orchestrator,contract,store}.py`, `ai/prompts/**`, `analysis/engine.py`, `core/settings.py`, `cli.py`, `execution/{executor,risk_gate,price_mapping,sizing}.py`, `execution/backends/{mt5_backend,paper}.py`, `api/app.py`, `web/static/{app.js,style.css}`, `config/config.yaml`, `pyproject.toml`, `requirements.lock`, `docs/ops_windows.md` §1b; tests `test_charts`, `test_claude_code_images`, `test_triggers_screen`, `test_management`, `test_position_actions`, `test_roles_escalation`, `test_phase3_core`, `test_replay_budget` (integration) and extensions.
- What was done: D-043 + D-044. 606 unit tests pass (+ `test_replay_budget -m integration`). **The ONE live call** (`engine --once --pairs BTCUSDT`, scratch data root, Sonnet, 6 charts): valid first attempt, 24 864 input / 2 608 output tokens, 43.4 s, 1 turn, stream-json user-line shape `message` (probed once, recorded), charts +37 MB engine RSS, the model saw the live BUY and held it (`position_actions` []); the input target (≤ 23 k) was missed by 8 % — +2.3 k images, +2.2 k system prompt v6 (a cache read from the second call on), details and levers in `docs/measurements/phase3_live.md`. **Screening budget** (read-only replay of stored data, setup + idle calls): BTC 36 / ETH 35 / XAU 32 calls per day on a Friday, 30–32 on the weekend, XAU 21 on a Thursday — within the 40 cap with room for reviews and events; `snapshot_build_ms` median 0.2–0.5 s, p95 ≤ 2.3 s → the full payload is built at every 5m screen. **Review:** 6 lenses (money path, AI path, engine/triggers, charts/API/web, prompts vs code, config/ops/rollback) each checked by an independent skeptic: 41 findings, 39 confirmed and fixed (1 refuted, 1 left open — see notes); the fixes re-reviewed by 3 lenses + skeptics: 17 findings, 16 confirmed and fixed; a final independent check of those fixes confirmed them and found 2 more small defects in the resumed-plan logic (a venue-deferred leg of a two-leg stop move abandoned; a superseded stop sending a false 'refused' event) — fixed with tests. Main fixes: protective actions wait through a closed market / terminal refusal instead of being given up; a timeout is never re-sent blindly; a model action is planned once and resumed exactly; single-leg trades number their leg by its real target and turn `tp_hit` on a nearer target into `price_reached`; management waits for the bar just closed; checked live basis; escalation never falls back to Gemini; a withheld trade keeps its protective actions (as a NO_TRADE that says so); a failed call gives back its setup and events; the event cursor survives restarts; duplicate YAML keys refused; per-pair overrides validated by the all-pairs check. Also found on the way: `engine --once` never waited for the background sign-in check (fixed, `wait_for_provider`).
- Why this way: D-038 (charts), D-043 (Claude leads, code protects; 40/day; per-role models), D-044 (implementation choices). Position management moved here from Phase 5 because the first live BTC position (2026-09-26) showed the gap.
- Notes/open issues: (1) input tokens 24.9 k vs the 23 k target — accepted for now (D-044); levers without code: fewer/smaller charts (`ai.charts.timeframes`, `width/height`), `ai.charts.enabled: false`. (2) RAM: +37 MB per engine with charts (≈ +110 MB for three pairs on a laptop with ≈ 0.4 GB free) — per-pair switch `instances.<PAIR>.overrides: {ai: {charts: {enabled: false}}}`; watch `status_all.bat` after H16. (3) Left open from the review (uncertain, no deterministic harm): rule 9 calibrates confidence to TP1 while at the minimum lot the executed target is the largest-fraction TP. (4) BTC/ETH candle_close exits, structure trailing and Claude's priced actions (stop/target moves) wait while the live basis is unavailable (a quote > 60 s old or the basis off its 60-min median); close/cancel do not. A priced model action still waiting after `risk.max_recommendation_age_s` (5 min) is refused as expired (Claude is told). (5) All-pairs layout only: the executor event cursor is one for all pairs, so a restart can re-wake an already handled event of another pair (per-pair production unaffected). (6) The CLI capability file (`cli_capabilities.json`) lives in the CLI work folder `%TEMP%\tradingsystem-claude-code\` (one per machine) instead of `data/shared/`. (7) Seen once in production (2026-09-26 23:07 UTC): a BTC call skipped by the data gate because the Binance analysis price was 134 s old — rare, not Phase 3. (8) Code rollback to pre-Phase-3 commits needs the Phase 3 keys removed from `config.local.yaml` first (ops §1b). **Live after H16 (2026-09-27 09:18–09:25 UTC):** all services live, RAM 0.98 GB free with three systems; ETH first cycle 25 210 input / 3 766 output tokens, 60 s, valid NO_TRADE (6 images, +33 MB RSS); BTC first cycle 25 059 / 9 665, **144 s** (above the 120 s target: a long plan with 3 management rules), valid SELL conf 60 — rejected by the gate `rr_after_costs: 1.50 ≥ 1.5` (the RR after costs was 1.4996, printed with 2 decimals → follow-up (9)); `snapshot_build_ms` 15.3 s (BTC) / 6.8 s (ETH) on the first build after the restart (cold caches + backfill passes; the replay medians were 0.2–0.5 s — watch the steady state, the spec's 3 s rule applies if it stays high); two `data_not_ready` warnings for the 15m bars around the restart (the feeds reconnected within 20 s). Earlier that night, before Phase 3 (on Phase 2 code): the BTC BUY `00eded5a` closed +2.55 USD and a second BUY `d9633718` +1.52 USD — equity 99.11 → 103.85. XAU closed until Sunday 22:00 UTC (no Phase 3 cycle yet). (9) Follow-up for Phase 4: the gate's `rr_after_costs` detail prints 2 decimals while the comparison is exact (`1.50 ≥ 1.5` shown as a failure) — print 3 decimals and compare with a 1e-6 tolerance, so the model's `gate_reason` is not contradictory.
- The exact next step: the remaining §3.10 checks as trades happen (chart thumbnails in the dashboard — seen for BTC/ETH; management rows on the next open position; a kill switch that blocks new orders while a protective stop move still applies; XAU's first cycle after Sunday 22:00 UTC); watch `snapshot_build_ms` in the engine status over a day. Then P12.4 on worktree `C:\the_claude_new_wt\phase4`, branch `feat/phase4-watches-learns` (Opus 5.5) — include follow-up (9).

### Phase P12.4: "Watches and learns" — metrics, bounded tuning, operator sessions, monitor, notifier (v2 Phase 4)
- Status: ✅ Completed on branch `feat/phase4-watches-learns` (worktree `C:\the_claude_new_wt\phase4`) — awaiting the user's H20 (stop_all → merge → start_all), then H19 (operator tasks), H18 optional
- Description: `decision_metrics` (MFE/MAE in R, TP hits, exit reason, slippage, costs, counterfactuals) and attribution columns; a per-pair adaptive overlay under `data/adaptive/<PAIR>/` (bounded keys, 14-day expiry, hot-reloaded, single writer `tools/tune.py` with policy in code) and a linted playbook injected into the user prompt; review packs and daily/weekly Claude review sessions (Opus, read-only allow-list) plus a diagnose session, run from Task Scheduler; proposals for everything else via `tools/propose.py` in separate worktrees; a pure-Python 15-min monitor with per-pair/global kill-switch rules; Telegram + toast + log notifier; a ledger-based usage gauge in the rationing ladder; dashboard tabs Operator/Tuning/Proposals/Reviews and a kill-switch ON button.
- Affected files: spec docs/handoff_operator_v2.md §3.8 (as built: 4.8) — new `core/{adaptive,playbook,tunables,notify,killswitch}.py`, `ai/usage_gauge.py`, `execution/metrics.py`, `tools/{tune,review_pack,propose,monitor,notify,kill_switch}.py`, `tools/operator/{run_session.py,run_session.ps1,session_args.py,prompts/*}`, `scripts/{install_operator_tasks.ps1,.bat,notify.ps1,monitor.bat}`, docs `learning_loop.md`, `operator_sessions.md`, `monitoring.md`, `notifications.md`, `measurements/phase4_live.md`, fixture `tests/fixtures/real/claude_code_session_result.json`; changed `ai/{store,orchestrator,budget,prompts/__init__}.py`, prompt `agent_per_pair/instructions.md` v5 (`$tp_hint`), `analysis/engine.py`, `execution/{executor,risk_gate,exposure,management}.py`, `mt5_backend.decision_result`, `core/{settings,logsetup}.py`, `api/app.py`, `web/*`, `cli.py` ("phase 4:" line), `tools/{health_report,demo_order_test}.py`, `scripts/check_ops.ps1`, `config/config.yaml`, `.env.example`, `docs/ops_windows.md` §8
- What was done: D-045. 1208 unit tests pass. Folded in from Phase 3's first live hours: the gate's `rr_after_costs` detail shows 3 decimals and compares with a 1e-6 tolerance (P12.3 note 9); the monitor and the health report show `snapshot_build_ms` and warn above 3 s; the notifier works with toast + log alone. **The ONE live call** (one daily review session, Opus, scratch data root with copies of the three app.dbs and the ledger, hot/cold as junctions): status ok, 10 turns, 119 s, 27.3 k unique context (cache creation 27 314 + 176 610 cache reads), 7.4 k output (34.7 k: the ≤ 30 k target missed by 16 %, accepted in D-045), API-equivalent $0.40, 0 permission denials, clean diff guard, no tuning (too few resolved outcomes: BTC 4, ETH 1, XAU 0 of 20), summary 1 500 chars to log + toast — docs/measurements/phase4_live.md; it read the trader's `decision` ledger rows as escalations, so the pack now labels roles. Review: 7 lenses + skeptic verifiers (41 findings, 39 confirmed: 7 medium, 32 low — all fixed, plus a YAML nesting-depth cap the fixers found), the fixes re-reviewed by 6 lenses + skeptics (22 findings, 20 confirmed: 1 high — the allow-list's glued `*` let `tools/tune.py/../<any file>` through; fixed with `Bash(<command> *)`, checked against the CLI's matcher — and 19 low, fixed), then a final independent check (4 checks + skeptics: the round-2 fixes, the session security boundary end to end, merge/restart readiness on copies of the production databases, completeness against the spec; 14 findings, all real, all fixed, plus one the fixers found). Among them: the sessions Read/Grep/Glob allow rules (reads now stay in the checkout and the data root, the user profile is denied), one pair per diagnosis and never the last pair still trading, the session recorded as the actor, a late-logon false alarm, and the merge order (stop_all, merge, start_all: the v5 template adds $tp_hint, which a running Phase 3 engine cannot fill). On copies of the production app.dbs the additive migrations take 0.14 s, are idempotent and main code still reads them; the metrics backfill needs 3 passes per pair (about 3 min); every production config combination validates.
- Why this way: D-039 (bounded autonomy), D-043 (Telegram; no git writes by operator sessions; Opus for reviews), D-045 (implementation choices).
- Notes/open issues: (1) the usage-gauge budgets are guesses: it only observes (`ai.usage.enforce: false`) until H21. (2) Tuning needs ≥ 20 resolved virtual outcomes per pair in the window (≥ 10 for activity keys): expect the first change only after days of ideas. (3) In the all-pairs layout `tune.py` records `tuning_changes` in the pair's instance app.db, not the all-pairs one (production is per-pair). (4) A decision executed but never settled gets no metrics row; `evaluate_virtual` reads are unbounded (pre-existing); MT5 trades settled before Phase 4 have no commission/swap (shown as unknown). (5) The health report, the review pack and the session gate use the gauge's plain thresholds (the 5-point step-down lives in the engines). (6) A process that never sent a notification keeps its start-up copy of deleted `TELEGRAM_*` lines — empty the values instead (docs/notifications.md §4). (7) The live call's toast (≈ 12:58 UTC) is for the owner to confirm. (8) Watch `snapshot_build_ms` (monitor/health report) after the restart; the first build is slow. (9) `review_pack.py` takes ≈ 11 s per run, 10.8 s of it in the pre-existing `procs.running_supervisors` scan.
- The exact next step: the user's H20 (`stop_all.bat` → merge → `start_all.bat`), then H19 (install the operator tasks from `C:\the_claude_new`), H18 optional. Then verify: `tradingsystem config` shows the "phase 4:" line; `scripts\check_ops.bat` lists the three `TradingSystemOps-*` tasks; the first monitor run's Last Result (0 or 1) and `logs\monitor.jsonl`; the dashboard tabs (Operator, Tuning, Proposals, Reviews); `decision_metrics` rows appearing (the backfill takes ≈ 3 passes of 60 s per pair); the first daily review at 04:30 UTC (`data\reviews\*_daily.session.json`, the summary toast/Telegram). H21 after a week. Then P12.5 on worktree `C:\the_claude_new_wt\phase5`, branch `feat/phase5-goes-deeper` (§3.9).

### Phase P12.5: "Goes deeper" — enrichments, persistent session (opt-in), MCP tools, news blackout, gold proxy (v2 Phase 5)
- Status: ⏳ Not Started
- Description: payload v4 (depth bands, OI from `metrics`, prev week/month levels, BTC–ETH correlation, session statistics, forming bar; ≤ +1.5 k tokens), persistent stream-json session mode measured only when the user opts one pair in, ≤ 3 read-only as-of-bound MCP tools behind `needs_detail`, news blackout for XAU from an MQL5 calendar export, the gold-proxy study P1.11, daily profile tables, and the measured inputs of the go-live checklist.
- Affected files: spec docs/handoff_operator_v2.md §3.9 — `analysis/{snapshot,context,model_view}.py`, new `analysis/{cross,news}.py`, `ai/providers/claude_code.py` (`SessionRunner`), new `tools/mcp_server.py`, `tools/mql5/CalendarExport.mq5`, `research/gold_flow/study.py`, `docs/go_live_checklist.md`
- What was done: —
- Why this way: D-040 (persistent session only measured/optional), D-043.
- Notes/open issues: persistent mode needs 300–500 MB more RAM per pair (opt-in only); MCP turns cost +25–30 k tokens each (capped 2/pair/day); the news file fails open with a warning. User actions H22–H26.
- The exact next step: after H20; worktree `C:\the_claude_new_wt\phase5`, branch `feat/phase5-goes-deeper`.

