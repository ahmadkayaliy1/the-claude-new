# Handoff brief — "Claude as operator" (v2 target)

Written 2026-09-26 by the lead session (Fable 5.1) for the implementing session (Opus 5.5). Read this, then
`PROJECT_STATUS.md` (overview, decisions D-030…D-040, human actions H11–H13), then `CLAUDE.md`.
The user speaks Arabic (Levantine) — reply to them in Arabic; code, docs and commits stay in English.

## 0. Where things stand (verified 2026-09-26 16:40 UTC)

* The system is **stopped**. The user rebooted the laptop twice today (Windows boot events 12:45 and 18:55
  local = 09:45 / 15:55 UTC; the first shutdown was initiated from the Start menu at 11:42 local). Nothing
  auto-starts after a reboot (autostart never installed, H7): supervisor, MT5 terminal and the P1.12 recorder
  are all down since 08:42 UTC. Fix: the user runs `scripts\start.bat` + `scripts\start_recorder.bat`, or
  installs `scripts\install_autostart.bat` (recommended, see H13).
* **A crypto-miner runs on the laptop** (H12): `C:\ProgramData\WindowsTask\MicrosoftHost.exe` (unsigned,
  XMRig-style command line `-o stratum+tcp://conhost.xyz:3333 -u Default_CPU --donate-level=1 -k
  --cpu-max-threads-hint=50`, **2.1 GB RSS**, 50 % of the CPU threads), plus `AppHost.exe` (same args),
  `audiodg.exe` (fake), `WinRing0x64.sys`, `winlogon.bat` (installs an AppLocker policy from `new.xml`),
  `AMD.exe`/`AppModule.exe`; persistence via `HKLM\...\Run "Realtek HD Audio" = C:\ProgramData\ReaItekHD\taskhostw.exe`
  (typosquat, capital I) and a Startup-folder shortcut `WindowsFormsApp10 - Shortcut.lnk`. Defender real-time
  protection is on but has not removed it. This is why the machine has 0.2–0.4 GB free, why `claude auth status`
  took 55 s, and why the ≤ 1.5 GB RAM budget was impossible. **Only the user removes it (system/security
  change); the agent never kills or deletes it.** Until it is gone, plan RAM as if only ~1 GB were available.
* **Open ops issue (check in Phase 1):** since the user installed autostart (2026-09-26 16:57 UTC) the supervisor
  logs `terminal_in_job` for the MT5 terminal it launched itself via `cmd start` (events 16:57:28 terminal_started
  → 16:57:44 / 16:58:30 terminal_in_job, two different terminal pids). Either `spawn_outside()`'s
  CREATE_BREAKAWAY_FROM_JOB is refused when the supervisor runs under Task Scheduler / the detached launcher (the
  terminal then inherits that job), or `_check_terminal_job()` / `winops.in_job()` test "any job" instead of our
  job (a `JobObject.handle` of 0 is falsy-but-not-None and `IsProcessInJob(h, NULL)` means any job). Verify which,
  fix, and make sure a supervisor crash cannot close the terminal (that is the whole point of OPS-04).
* The `install_autostart.ps1` final listing used `Get-ScheduledTask`, which fails with 0x80041318 on a trigger
  that repeats indefinitely (PowerShell 5.1 bug; the task itself is valid and runs) — replaced by `schtasks`.
* Everything in `PROJECT_STATUS.md` up to D-037 is merged on `main` (HEAD `6fa8c4d` + branch
  `fix/report-tz` = `b9ff160`, a 1-line report fix, not yet merged by the user). 334 unit tests pass.
* Live numbers (data/app.db, 25 real Claude calls today): input **~23 k tokens/call** (4.9 k cached; the
  payload is ~17 k tokens = 31–33 k chars, not the 9 k of the small gold fixture), output ~3–5 k, latency
  35–110 s (median ~50 s). Cadence today: 1.5–1.9 calls/h/pair (mostly the model's own `next_review`
  re-fires) → ~40 calls/day/pair → **~1.1 M tokens/day per pair (~8 M/week)**. A 10 M-token session once
  exhausted the plan's limits, so **1 pair fits, 3 pairs at today's cadence do not** (~23 M/week) unless the
  cadence is cut (Phase 1/4 below). Of 25 decisions: 23 NO_TRADE, 2 trade ideas, 1 gate rejection (SL too tight
  for Windsor's spread), 0 executed, 0 broker outcomes.

## 0b. Progress log (newest first — read this before §4)

* **2026-09-26 19:10 UTC — Phase 1 (P12.1) is DONE on branch `feat/phase1-operator-memory` (commits `c844318`,
  `47e9e24`), 361 unit tests pass, reviewed by two independent agents, live-tested with one BTCUSDT call (18.0 k
  input tokens, valid, operator notes fed back). NOT merged yet: `main` is still `f01c86d` and production runs
  the old code. The user applies it with `git merge --ff-only feat/phase1-operator-memory` + `scripts\restart.bat`
  (+ `scripts\start_recorder.bat`). `PROJECT_STATUS.md` on that branch has the M12 phase blocks (P12.1…P12.5)
  and D-041.**
* Deferred from the Phase 1 review (do in Phase 2): (a) an executor guard that refuses a same-pair, same-direction
  candidate while a live order / position from a recent decision exists (today the prompt only *tells* the model
  every BUY/SELL is a new order); (b) live account state in the payload — broker equity, open positions, pending
  orders (the payload still carries the configured paper equity; the legend says so); (c) `history[].gate_reason`
  strings are in the execution instrument's prices — say so or render "actual vs required".
* Ops observed 2026-09-26 18:17–19:08 UTC: a **51-minute Binance outage** (DNS `getaddrinfo failed`, 121 WS
  disconnects in 3 h) while MT5 stayed connected; the engine correctly skipped 6 cycles ("analysis price 150–700 s
  old") and gap-fill healed every table afterwards. An **ExpressVPN TUN adapter is active** on the laptop
  ("Local Area Connection 4"); VPN tunnel drops are the likely cause of the DNS bursts. Ask the user whether the
  VPN is needed to reach Binance from their location; if not, run the trading laptop without it; if yes, the
  supervisor/health report should show the VPN adapter state and the monitor should flag outages > 10 min.
* The 3-hourly desktop monitor still depends on the app being open (Phase 4 moves it to Task Scheduler).

## 1. What the user decided (2026-09-26, verbatim intent)

1. **Claude actually runs it**: Claude is the AI that analyses the market; Python fetches data, computes, and
   executes what Claude decides. The user chose "a persistent Claude session that drives everything" over the
   per-decision call. The lead explained (accepted by the user) that it will be realised as *persistent memory
   + event-driven calls*: the session keeps its context per pair and is woken by Python on events; it must not
   poll 24/7 (the 5-hour limit would be gone in hours). See §3.1 for the two modes to build.
2. **Keeps learning**: while running, Claude keeps checking for errors and improving the operation — bugs,
   strategy problems, anything. Allowed autonomy (user's choice): **prompts/playbook and soft parameters
   within code-enforced bounds automatically, every change logged and revertible; any code change or risk-limit
   change only as a proposal the user approves.** Never risk limits, never execution logic.
3. **One pair per instance**: `start.bat BTCUSDT` starts a fully independent system for that pair (own data,
   processes, dashboard); several may run at once, each independent. **Daily loss limit 10 % per pair**
   (user's explicit choice; the lead adds one account-wide safety floor, §3.3).
4. **Analysis depth**: "the most suitable for highest accuracy", **plus candle-chart images** so Claude reads
   candle shapes / price action visually. The lead's choice: one rich call (compacted, enriched snapshot) +
   6 chart images + a bounded tool budget (max 3 turns) only when the snapshot is insufficient — not the
   6-analysts-plus-coordinator mode (5–7× usage).
5. Earlier standing decisions stay: paper→demo→live only by the user (D-012, H9); demo/auto now (D-036);
   the agent never merges into `main`, never starts/stops production, never edits `.env`, never changes Windows
   settings — the user runs `git merge --ff-only <branch>` and `scripts\restart.bat` (D-032).

## 2. Verified technical facts (do not re-derive)

* Claude Code CLI **2.1.282** at `C:\Users\Ahmed\.local\bin\claude.exe`, signed in with the user's **Claude Max**
  subscription (`authMethod claude.ai`, no API key). `--bare` disables OAuth → never use it. The Python
  `claude-agent-sdk` documents API-key auth only → not an option. Everything goes through the CLI.
* Images: stream-json input supports image content blocks (`{"type":"image","source":{"type":"base64",
  "media_type":"image/png","data":…}}`) with subscription auth. Verify the exact user-turn line shape with ONE
  tiny live call before building on it (the docs show `{"type":"user","message":{"role":"user","content":[…]}}`
  in older references and `{"type":"user","content":[…]}` in the guide's summary — test which 2.1.282 accepts).
* Chart rendering: `matplotlib 3.11` + `mplfinance 0.12.10b0` are installed in the venv (2026-09-26).
  Measured on real stored BTCUSDT 15m candles: 720×400 px PNG = 26 KB ≈ **384 image tokens**, 0.96 s
  render; 900×500 = 35 KB ≈ 600 tokens, 2.2 s. Six timeframes ≈ 2.3–3.6 k tokens per decision.
* Sessions: `--input-format stream-json --output-format stream-json`, `--session-id <uuid>` / `--resume <id>`
  (survives a process restart), `--autocompact <auto|100k–1M>` bounds context, `--replay-user-messages`,
  `--no-session-persistence` (current per-call mode). `--json-schema` costs a tool round-trip (D-035): keep the
  schema in the system prompt (`structured_output: prompt`). `--max-turns` exists in the binary but is hidden
  from `--help` (verify with one live call; env `CLAUDE_CODE_MAX_TURNS` also exists).
* Tools from Python: `--mcp-config '<json>' --strict-mcp-config --tools "" --allowedTools "mcp__ts__*"
  --permission-mode dontAsk --permission-prompts none` (the Python `mcp` package is NOT installed yet). Each
  extra turn re-sends the context (+25–30 k raw input; measured 51.8 k in / 28.8 k cached on a 2-turn call):
  practical max 3 turns, timeout ≥ 300 s.
* Usage accounting: cached tokens' weight in the subscription's 5-h/7-day limits is **not documented**; the
  binary references `api/oauth/usage` (five_hour / seven_day) that the CLI's `/usage` uses — unofficial; if used
  for a gauge, treat as best-effort data. `ai_usage` (app.db) records our calls; the user's own interactive
  sessions are invisible to it.
* MT5: one terminal, multi-client verified for 3 clients (P1.9), calls hold the GIL and serialise; a history
  backfill can block the terminal for minutes (D-024). Windsor: contract BTC 1 / ETH 10 / XAU 100, min lot 0.01,
  stops level 2500/200/25 points, spreads ≈ $26 / $2.2 / $0.5, hedging account, FOK filling, server UTC+3,
  crypto maintenance Sat 05:00–08:00 UTC, gold closed Fri 21:00 → Sun 22:00 UTC.
* Storage is already partitioned per instrument (`data/hot/{venue}/{SYMBOL}.db`, `data/cold/...`); what
  collides between instances is `data/app.db`, `logs/`, API port 8765, the supervisor's process-wide sibling
  detection (`procs.other_supervisors()` matches any `-m tradingsystem run`), `--stop` (can kill a sibling),
  the MT5 terminal launch, the RPD counter and the CLI concurrency semaphore (per app.db / per process).
* RAM (P5.2): full stack ≈ 500–700 MB per instance (+ MT5 392 MB + 170 MB per running CLI call). Lean instance
  (no per-instance api, `--no-backfill` once history is complete) ≈ 420 MB. With the miner gone the laptop has
  ~3–4 GB headroom; with it, ~1 GB.

## 3. Target design (the lead's decisions; implement in this order)

### 3.1 Operator = Claude with memory, woken by events (two modes, one config switch)

`ai.providers.claude_code.session_mode: per_call | persistent` (default `per_call`).

* **per_call + operator memory (build first, default):** keep the measured single-turn call. Add to the
  output contract a system-fed-back field `operator_notes` (≤ 600 chars: what I am watching, why I am in this
  trade, what would change my mind, levels to watch) stored on the decision and injected into the next call's
  `history` block for that pair (with the last 5 decisions + outcomes + gate reasons, which already exist).
  This gives continuity for ~300 tokens instead of re-sending 27 k tokens per turn.
* **persistent (build second, measure, keep behind the switch):** one long-lived `claude -p --input-format
  stream-json --output-format stream-json --session-id <per-pair uuid> --autocompact 100k --tools "" …`
  process per instance, held by the engine; each event (decision-TF close with a setup, review due, outcome
  settled) is one user turn (text + images); the process is restarted daily with `--resume`. Measure raw and
  cached tokens per decision against per_call for 24 h before recommending it to the user. Token math from the
  analysis: ≈ 2.6× the raw input of per_call; RAM 300–500 MB resident.
* **Bounded tools (Phase 5, optional):** ≤ 3 read-only, as-of-bound MCP tools (more candles of one TF, depth
  bands, footprint detail, past decisions with outcomes), `--max-turns 3`, timeout 300 s, only when the model
  sets `needs_detail` — build after a replay (P6.15) shows decisions fail for lack of data.

### 3.2 Comprehensive analysis (cheap first)

1. **Execution costs + gate rules into the snapshot and the cached system prompt** (fixes the observed
   rejection): `market.execution.costs` = stops_level, spread median/p95 (1 h / 24 h / by session from stored
   ticks or the MT5 candle `spread` column — say which), commission, swap long/short, triple-swap day,
   slippage median when ≥ 10 fills, basis median; `min_stop_distance = max(stops_level + spread, 0.5×ATR)`
   (today `snapshot.py:184` says 0.5×ATR only while `risk_gate.py:112` requires the max), `max_stop_distance`,
   `spread_pct_of_min_stop`; `_system_vars` gains stops_level, typical spread (rounded, cache-stable), max
   spread/SL ratio, SL ATR bounds, min confidence, max recommendation age, contract size, min lot,
   execution instrument; `core_rules.md` rules 3/6/7 rewritten as "the gate rejects …" constraints with a
   per-venue min-SL hint (≈ 5× typical spread). New `ContractCfg` fields for stops_level_points, swaps,
   commission (values in `docs/exploration/mt5_static.md`).
2. **Payload compaction, formatting only** (−3.5…4 k tokens/call): short timestamps (`MM-DD HH:MM`, as_of
   full ISO), `quality` → one status string, capabilities block moved into the system prompt text (static per
   pair), higher-TF `recent` bars trimmed to 3–4. Keep `tests/unit/test_snapshot.py` causality tests green.
3. **Feedback in the payload**: `history` gains `gate_reason` (first 160 chars of execution_detail.reason) and
   a 30-day `performance` block (decisions, trades, NO_TRADE %, virtual TP1-first/SL-first/unresolved, mean R,
   gate approval share).
4. **Trigger policy — "any opportunity" comes from Python, not from more Claude calls**: screen every 5m close
   in Python (the setup detector exists in `ai/triggers.py:26-77` but runs only at the 15m close,
   `engine.py:154-156`); call Claude on change (strong setup, ≥ 2 weak, price/candle review conditions);
   honour a pure time-based `next_review` only when something changed (new 15m bar + a weak reason or a
   > 0.5 ATR move); keep the 120-min idle review as the floor. Per-instance daily call cap (start 40/day).
5. **Charts**: `analysis/charts.py` renders 1w/1d/4h/1h/15m/5m candle+volume PNGs (720×400, last 80–120
   bars, key levels drawn: PDH/PDL, OBs, FVGs, liquidity pools, entry/SL/TP of the open trade) in a worker
   thread; the provider sends them as image blocks with the text payload (`images: true`, default on). Add the
   images' token count to `ai_usage`.
6. **Computable enrichments (after 1–5)**: prev-week/month H/L, month/year open; derivatives history (fix the
   OI nulls: read `metrics.sum_open_interest`, 30-d percentiles of funding/OI); depth imbalance bands (stored,
   unused); BTC–ETH correlation (crypto instances read the sibling's hot DB read-only); session/killzone
   statistics (real build, causal tests). New collection (DXY, news calendar via an MQL5 service, gold-proxy
   study P1.11, daily volume-profile tables) is Phase 5+.

### 3.3 One pair per instance

`python -m tradingsystem run all --instance BTCUSDT` (and `scripts\start.bat BTCUSDT` / `stop.bat` /
`status.bat` / `restart.bat` with the pair as `%1`; `*_all.bat` loop over `instances:`). The CLI sets
`TS_INSTANCE`; `load_settings()` overlays it: only that pair enabled, `data_dir = data/instances/<PAIR>`,
`logs_dir = logs/<PAIR>`, `api.port` and `magic` from a new `instances:` config block. Children and the
mp-spawned backfill workers inherit the env, so the per-data-dir mutex, state file, STOP_ALL and KILL_SWITCH
become per-instance for free.

* **Risk scope (user: 10 %/day per pair)**: magic per instance (`magic_base + index`) so `MT5Backend.account()`
  (sums by magic) sees only this instance's positions/orders/deals → the daily worst case, max-open and
  exposure caps are per pair as the user wants. **Account-wide safety floor (lead's addition, not optional):**
  `risk.account_drawdown_stop_pct: 25` — every executor reads the whole login's equity (ignores magic) against a
  high-water mark kept in `data/shared/account.json` (atomic writes) and refuses new orders when equity is
  ≥ 25 % below it; also refuse when `data/KILL_SWITCH` (shared) or the instance's own exists. Tests for both.
* Sibling-aware supervisor: `other_supervisors()` / `control.stop|status|detach` match only the same
  `--instance`; a global named mutex around the MT5 terminal launch (`TradingSystem.mt5launch.<sha(path)>`) and
  one around heavy MT5 history calls (`TradingSystem.mt5heavy`, held per chunk; `_live_healthy()` reads every
  instance's `mt5` heartbeat) so one instance's backfill never gets the others killed at 90 s stale.
* Shared AI ledger: `UsageStore` at `data/shared/ai_usage.db` when an instance is set (WAL); a global Windows
  named semaphore for the CLI (`max_concurrency_global: 1` on this laptop) + staggered dispatch; per-instance
  daily cap + a global cap.
* Binance budgets divided by the number of instances (REST 1500/min each; Vision download concurrency 1).
* Migration `tools/migrate_instances.py` (system stopped, same volume: `os.replace` the hot DBs and cold folders
  into `data/instances/<PAIR>/…`, copy `app.db`); health report and dashboard take `--instance`. A combined
  dashboard is a later build.
* Start with **one instance (BTCUSDT)**; add ETHUSDT after a 24-h measurement of RAM and usage; XAUUSD only
  once its 15m history is complete (it has 3 days) and P1.11/news blackout exist. Re-run the P1.9 multi-client
  probe with 9–10 clients before running three.

### 3.4 Learning loop (bounded autonomy)

* **Feedback data first** (nothing to learn from without it): decision attribution columns (trigger_strength,
  setup_kinds, session, regime, htf_bias, data_warnings), `decision_metrics` (MFE/MAE in R, TP1/2/3 hit,
  bars/minutes to resolve, exit reason, slippage, spread at gate, commission, swap, rejected-but-virtual-win,
  NO_TRADE counterfactual max move in ATR over 4 bars), persist `api_equivalent_usd`/`num_turns`/cache
  tokens in `ai_usage`, a prompt version registry (`prompt_versions` with prompt_hash, library_hash, version
  header, git sha), `library_hash` and `playbook_hash` stored per decision.
* **Autonomous surface (the ONLY things the review session may change, through `tools/tune.py`):**
  per-pair playbook `config/playbooks/<PAIR>.md` (≤ 1500 chars, ≤ 12 bullets, linted: denylist of risk words,
  `$` escaped; injected into the USER prompt via `$playbook` so the cached system prompt/prompt_hash stay
  stable) and `config/adaptive.yaml` keys with code-enforced bounds: min_confidence_floor [55, 80] raise-only,
  min_minutes_between_calls [15, 60], max_idle_minutes [60, 240], review_floor_minutes [5, 30],
  trigger.weak_min [2, 3], trigger.liquidity_atr [0.2, 0.5], pair.ai_paused_until (≤ 7 d), tp_hint ≤ 200 chars.
  Every change → `tuning_changes` table + `docs/tuning_log.md` + a git commit on branch `tuning/<date>`
  (reason, evidence JSON, window, expiry 14 d, rollback rule). Policy in code, not prose: ≥ 20 resolved
  virtual outcomes per pair for strategy keys (10 for activity-reducing keys), one change per pair per review,
  7-day cooldown per key, freeze when > 25 % of the window was unhealthy, autonomous direction "fewer/better"
  only (never towards more trades). `data/TUNING_FREEZE` / `enabled: false` stop all writes.
* **Proposal-only surface**: everything else (src/**, risk:, execution:, contract, core_rules/persona/system
  prompts, providers, .env, supervisor, scripts): branch `proposal/<date>-<slug>` + `docs/proposals/<slug>.md`
  (problem, numbers, diff, risk, test results) + a `PROJECT_STATUS.md` row + a push notification; the user
  merges. `tune.py` refuses paths outside its allow-list; `tests/unit/test_adaptive_bounds.py` asserts the gate
  uses `max(55, floor)` and AdaptiveCfg has no risk/execution keys; a post-run diff guard fails a review that
  touched anything else.
* **Review input**: `tools/review_pack.py` → `data/reviews/<ts>.md|json` (health, per-pair funnel: bars →
  triggers → calls → ideas → gate by check → placed → outcomes; idea table; errors; prompt/config hashes;
  ≈ 6–10 k tokens). Daily review (~20–25 k tokens) + weekly deep review (~80–100 k) + the 3-hourly monitor
  (~6–8 k/run). **Run them from Windows Task Scheduler with `claude -p --restricted --add-dir C:\the_claude_new
  --allowedTools "Read,Grep,Glob,Bash(.venv/Scripts/python.exe tools/*)" --permission-prompts none
  --max-budget-usd <n>`, not from the desktop app** (the desktop scheduled task ran 1 of 4 times today and did
  not notice the outage). Keep the SKILL.md rules (read-only except tune.py; KILL_SWITCH is the only emergency
  action). Push notifications need a channel when the app is closed — propose one (e.g. the app when open,
  else a log line + a Windows toast via `scripts\notify.ps1`).
* Label broker outcomes "SL/TP-only execution" until P9.6 (MT5 position management: breakeven, partials,
  trailing from `recommendation.management`) exists — build P9.6 in Phase 5 so outcomes match the plans.

### 3.5 Prerequisites and ops

* H12 malware removal (user). H13 autostart (`scripts\install_autostart.bat`, user; `-DryRun` first).
  H4 `GOOGLE_API_KEY` (user) so the Gemini fallback is alive when Claude cools down on a limit.
* Usage gauge: best-effort read of the subscription's 5-h/7-day utilisation (the CLI's `/usage` endpoint) fed
  into the engine's rationing ladder (> 70 % of the 7-day → reviews only; > 90 % → pause); if the endpoint is
  unusable, a conservative token budget per day per instance from `ai_usage` instead.
* The engine's data gate and the report must keep flagging `data_not_ready`; the monitor must detect an outage
  within one cycle (heartbeats older than 15 min) — today nobody noticed for 7.5 h.

## 4. Phases, acceptance tests, effort

| Phase | Scope | Acceptance | Effort |
|---|---|---|---|
| 0 (user) | H12 malware removal, H13 autostart, H4 Gemini key, restart system + recorder | `scripts\check_ops.bat` all ok; `tools/health_report.py` clean; free RAM > 2 GB | user |
| 1 | §3.2 items 1–3 + operator_notes (§3.1) + usage extras persisted | 334+ tests pass; one live BTC call: SL ≥ gate minimum, `operator_notes` stored and fed back; input tokens/call ≤ 19 k | hours–1 day |
| 2 | §3.3 instances (BTCUSDT first; ETHUSDT second) | two instances run 24 h without cross-kills; per-instance gate proven by test; account floor test; migration script rehearsed on a copy | 2–3 days |
| 3 | §3.2 item 5 charts + item 4 trigger policy + item 6 cheap enrichments | live call with 6 images validates; tokens/call measured before/after; 5m screening test on real fixtures; calls/day/pair ≤ 40 | 2–3 days |
| 4 | §3.4 learning loop + Task-Scheduler operator sessions + usage gauge | tune.py bounds/cooldown/min-samples tests; playbook lint; a dry review run produces a pack and a proposal branch; monitor detects a simulated stale heartbeat | 3–4 days |
| 5 | persistent session mode (measure), bounded tools, P9.6 position management, news blackout (XAU), gold-proxy study, profile tables | each behind a config flag with a 24-h measurement written to docs/ | as needed |

## 5. Working rules for the implementing session

* One git worktree per phase under `C:\the_claude_new_wt\<name>` (branch `feat/<name>`), never edit
  `C:\the_claude_new` directly while production runs; run tests with `PYTHONPATH=src
  C:/the_claude_new/.venv/Scripts/python.exe -m pytest tests/unit -q -p no:cacheprovider` from the worktree.
* Before each merge: adversarial review by an independent agent; then give the user the two commands
  (`git merge --ff-only <branch>`, `scripts\restart.bat`) — the user does them. Update `PROJECT_STATUS.md`
  (phase blocks, decisions, human actions) in the same branch.
* Real data only; every live Claude call costs the user's subscription — measure with ONE call, not ten.
* Never call MetaTrader5 from an agent while production runs except read-only and briefly; never send orders
  outside `tools/demo_order_test.py`; never touch `.env`, Windows settings, scheduled tasks or the malware.
* Keep the user informed in short Arabic messages: what changed, what they must run, what to expect.
