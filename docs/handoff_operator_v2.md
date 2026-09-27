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

* **2026-09-28 — Phase 5 plan approved (D-046, §3.9.1); production still on Phase 3 (main `6e24353`) until H20;
  Phase 4 finished on `feat/phase4-watches-learns` (`301f60c`).** A five-analyst read-only review (spec coverage,
  tracker truth, Phase 5 readiness, operations, go-live readiness) found: ≈ 85–90 % of the master spec built, ≈ 15 %
  proven in production (2 days of demo: BTC 75 decisions, 3 broker trades +4.07/−2.52 USD, equity 101.33, drawdown
  2.45 %; ETH 70 decisions, 0 trades; XAU 0 cycles). Operational facts verified on the machine: the three sleeps of
  09-26/09-27 were **critical-battery hibernates** (Kernel-Power 524 at 5 %, charger unplugged) and two outages were
  **Start-menu shutdowns** (09-26 11:42, 09-27 09:10 local) — the power settings are correct; **no git remote and no
  backup exist**; the P1.12 recorder died at 06:06 UTC (29 MB, 1,142 parquet files, ≈ 21 h of the 72 h needed);
  free RAM 330–470 MB (Claude desktop + CLI ≈ 1 GB, Chrome ≈ 1.1 GB, EaseUS UPDATE SERVICE, `Ollama.lnk` in Startup);
  the 40/day cap is used up by early afternoon (BTC 39, ETH 38) and the two "session limit" errors (09-26 20:45 UTC)
  coincided with a development workflow on the same Max plan; `check_miner` CLEAN (offline scan, password rotation
  and `C:\ProgramData\KMSAuto` remain for the owner); payload defects (OI/taker ratio null, depth absent though
  "real"); ≈ 20 stale tracker rows. The owner's decisions: no MQL5 (feed-based news blackout), defer the persistent
  session and MCP, drop the 1m view to pay for the new blocks, capital stays $100 with unchanged risk limits, the
  shared Max plan for now with the cap at 30, ExpressVPN untouched. Plan: Phase 5 in two checkpoints (A "ready and
  safe": tracker reconciliation, backup + restore runbook, recorder task, monitor battery/RAM/recorder rules,
  interrupted cycles and transient retries, OI fix, demo report + go-live checklist; B "goes deeper": §3.9 items
  1, 3, 6, 12, 5, 4, 7 + the one live call, then 10, 11, 13), then a declared 28-day demo window and the go-live
  checklist (§3.9.1). New owner rows H27–H31 (charger/never shut down, recorder restart, git remote + backup
  off-machine, the `ai:`/`monitor:` block in config.local.yaml, branch/worktree cleanup). Next: the owner's H20 →
  H19 → H18 → H27–H30, merge this docs branch (`docs/phase5-plan`, ff, no restart), then the kickoff in §3.9.1.

* **2026-09-27 — Phase 4 (P12.4, D-045) is DONE on branch `feat/phase4-watches-learns` (worktree
  `C:\the_claude_new_wt\phase4`); production still runs Phase 3 on `main` until the user's H20.** 1208 unit tests.
  Built in the order of §3.8 4.1: decision metrics + attribution + prompt registry; the per-pair adaptive overlay +
  playbook (`tools/tune.py` the only writer, `core/tunables.py` the consumers' view); review packs, Claude operator
  sessions (Python runner, `TradingSystemOps-*` tasks) and proposals; the pure-Python monitor; the notifier (log +
  toast; Telegram after H18); the usage gauge (observe only until H21); dashboard tabs + an ON-only kill-switch
  button. Folded in: the gate's rr detail (3 decimals, 1e-6 tolerance), `snapshot_build_ms` in the monitor and the
  health report (warn > 3 s), a notifier that works with toast + log alone. The ONE live call (daily review, Opus,
  scratch root): ok, 10 turns, 119 s, 27.3 k unique context + 7.4 k output (the 30 k target missed by 16 %, accepted
  in D-045), 0 denials, clean diff guard, no tuning (too few resolved outcomes) — docs/measurements/phase4_live.md.
  Review: 7 lenses + skeptics (39 of 41 confirmed, fixed), re-review of the fixes (20 of 22 confirmed, fixed —
  among them a HIGH one: the allow-list's glued `tools/tune.py*` matched `tools/tune.py/../<any file>`; every rule
  is now `Bash(<command> *)`, checked against the CLI's matcher), then a final independent check (4 checks +
  skeptics: the round-2 fixes, the session security boundary end to end, merge/restart readiness on copies of the
  production databases, completeness against the spec; 14 findings, all real, all fixed, plus one the fixers found).
  Among them: the sessions Read/Grep/Glob allow rules (reads now stay in the checkout and the data root, the user
  profile is denied), one pair per diagnosis and never the last pair still trading, the session recorded as the
  actor, a late-logon false alarm, and the merge order (stop_all, merge, start_all: the v5 template adds $tp_hint,
  which a running Phase 3 engine cannot fill). On copies of the production app.dbs the additive migrations take 0.14
  s, are idempotent and main code still reads them; the metrics backfill needs 3 passes per pair (about 3 min);
  every production config combination validates.
  User steps: H20 `scripts\stop_all.bat` + `git merge --ff-only feat/phase4-watches-learns` + `tradingsystem
  config` ("phase 4:" line) + `scripts\start_all.bat` (not merge-then-restart: a running Phase 3 engine re-reads
  the v5 template and cannot fill `$tp_hint`); then H19 `scripts\install_operator_tasks.bat -DryRun` / without
  `-DryRun` from `C:\the_claude_new`; H18 (Telegram) optional; H21 after a week. Rollback switches:
  docs/ops_windows.md §8. As-built deviations from §3.8: its '4.8 As built'. Next: P12.5 (§3.9) on
  `C:\the_claude_new_wt\phase5`.

* **2026-09-27 09:18 UTC — Phase 3 is LIVE: the user merged `feat/phase3-sees-manages` (ff → `dfa19da`) and ran
  `restart_all.bat`; production = three per-pair systems on Phase 3 code.** `tradingsystem config`: charts=on,
  escalation=off, management=on, position_actions=on, 40 calls/pair/day. First cycles: ETH 25.2 k in / 3.8 k out,
  60 s, NO_TRADE; BTC 25.1 k / 9.7 k, 144 s (long plan), SELL conf 60 rejected by the gate at RR 1.4996 (shown
  "1.50 ≥ 1.5" — follow-up: 3 decimals + tolerance, PROJECT_STATUS P12.3 note (9)). `snapshot_build_ms` was 15 s / 7 s
  on the first build after the restart (cold; replay medians 0.2–0.5 s) — watch the steady state. RAM 0.98 GB free.
  Overnight on Phase 2 code two BTC BUYs closed in profit (+2.55, +1.52 USD; equity 99.11 → 103.85). XAU reopens
  Sunday 22:00 UTC. Next: Phase 4 (§3.8) on `C:\the_claude_new_wt\phase4`, branch `feat/phase4-watches-learns`.

* **2026-09-27 — Phase 3 (P12.3, D-044) is DONE on branch `feat/phase3-sees-manages` (worktree
  `C:\the_claude_new_wt\phase3`); production still runs Phase 2 on `main` until the user's H16.** 606 unit tests.
  The ONE live call (BTCUSDT, scratch data root, 6 charts, Sonnet): valid, 24.9 k input (target 23 k missed by 8 %),
  43 s, 1 turn, stream-json shape `message`, +37 MB RSS — `docs/measurements/phase3_live.md` (also the screening
  replay: 21–36 calls/pair/day). Review: 6 lenses + skeptics (39 of 41 confirmed and fixed), the fixes re-reviewed
  (16 of 17 confirmed and fixed), then a final independent check (2 more small defects, fixed). H16 = `git merge --ff-only
  feat/phase3-sees-manages`, `tradingsystem config` (the "phase 3:" line), `scripts\restart_all.bat`; rollback
  switches in `docs/ops_windows.md` §1b (one block per YAML section — duplicates are refused now). Lessons for the
  next phases' live calls: redirect the engine's stdout/stderr to files (a piped, unread stdout blocked the engine
  after the call), and `engine --once` now waits for the background sign-in check. Next: P12.4 on
  `C:\the_claude_new_wt\phase4`, branch `feat/phase4-watches-learns`, after H16 and the §3.10 checks.

* **2026-09-27 — Phases 3–5 fully specified (§3.6–3.10) and approved by the user; production runs three per-pair
  systems on main `82fdafa` (+ the XAU watchdog hotfix `6a10a26` once merged with this docs branch).** The next
  implementing session (Opus 5.5) starts Phase 3 on worktree `C:\the_claude_new_wt\phase3`, branch
  `feat/phase3-sees-manages`, following §3.7 in its build order; §3.6 lists the ground rules (one billed live call per
  phase on a scratch data root; never stop production from a worktree; additive DB changes; user-only actions as H rows).
  The user's four decisions are D-043 in PROJECT_STATUS.md; H15–H26 are the actions the phases will need from the user.
  Known state today: XAUUSD stopped for the weekend by the user (gold closed until Sunday 22:00 UTC; its ingest-binance
  kill loop is fixed in `6a10a26`), RAM is tight (≈0.4 GB free with three systems), the open BTC demo position (0.01 @
  84343, SL 84095, TP 85255) has no breakeven/trailing until Phase 3 lands, and something outside our code changed its
  TP from 85254.58 to 85251.83 after placement (the user was asked to check MT5's Journal/Experts and to disable EAs).

* **2026-09-27 \01:20 local (2026-09-26 22:20 UTC) — the user ran `switch_to_pairs.bat` (all three pairs) + the
  recorder; production now runs `82fdafa` as three per-pair systems (BTCUSDT 8766, ETHUSDT 8767, XAUUSD 8768).**
  Migration: BTC 28 decisions, ETH 24, XAU 0; ledger 48 rows. First cycles fine; BTC placed a demo BUY 0.01 at
  84343 (SL 84095, TP 85255, conf 60) at 22:17 UTC. Live findings: (1) XAUUSD's ingest-binance was killed by the
  watchdog every \2 min (its Binance *spot* row stayed "starting": no spot instrument) — fixed on the branch
  (`service_beats`, spot row "stopped"); needs a merge + `restart.bat XAUUSD`. (2) RAM: the three systems use
  ≈2.5 GB private (≈0.8 GB each); the laptop had 0.42 GB free, pagefile 3.3 GB, CPU 100 % during the start-up
  backfill pass (three backfill workers). Advice given: stop XAUUSD over the weekend (gold closed until Sunday
  22:00 UTC), close ChatGPT/Chrome while trading, consider a 16 GB RAM upgrade. (3) Tokens: a cycle is now ≈19–23 k
  input (cache created once per prompt version); CLI-reported API-equivalent ≈ $0.13–0.17 per call.

* **2026-09-27 — Phase 2 (P12.2, D-042) is DONE on branch `feat/instances` (it contains Phase 1 too; `main` is
  still `7b5e5da`, production still runs the all-pairs system on `f01c86d`-era code).** 413 unit tests pass. Review:
  3 lenses (processes/concurrency, money safety, ops scripts + migration), each finding adversarially verified: 25 of
  26 confirmed and fixed, then the fixes re-reviewed by an independent agent (6 more found and fixed). What the user does (H14): `git merge --ff-only feat/instances`,
  then `scripts\switch_to_pairs.bat` (optionally with pairs, e.g. `BTCUSDT` first — RAM: the all-pairs system uses
  ≈0.9 GB private and the laptop had 0.6 GB free with the Claude app, Chrome and ChatGPT open), then
  `scripts\start_recorder.bat`. Or stay on the all-pairs system: `scripts\restart.bat` after the merge.
* Phase 2 layout (details in `docs/ops_windows.md` §1a): `--instance <PAIR>` / `TS_INSTANCE`; state
  `data/instances/<PAIR>/` (app.db, run/, STOP_ALL, KILL_SWITCH), logs `logs/<PAIR>/`, ports 8766–8768, magic base+1..3;
  shared `data/shared/` (ai_usage.db ledger, account_peak.json, locks/); market data shared. The all-pairs system is
  unchanged and exclusive with the per-pair ones. Exit codes of `run all`: 3 = already running, 4 = refused
  (another kind of system, or a pair without its own app.db while `data/app.db` exists).
* Never run `run --stop` (or stop*.bat / switch_to_pairs.bat) from a worktree while production runs: the fallback of
  `control.stop` finds supervisors by command line machine-wide and may kill the production one after its timeout.
  Read-only checks (`run --status`, health report, DryRun) are safe.
* Phase 2 open points for the live check after H14: RAM with 2–3 pair systems; one AI cycle per pair (payload
  `account.open_positions` / `pending_orders` / `holdings_price_space`); the executors' `exposure` and
  `account_drawdown` in their status rows; `status_all.bat`; `tools\health_report.py` (one section per running system).

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
  from `--help` (verify with one live call; env `CLAUDE_CODE_MAX_TURNS` also exists). **Verified 2026-09-27 by the
  Phase 4 live call** (docs/measurements/phase4_live.md): `claude -p --max-turns 30 --permission-mode dontAsk
  --permission-prompts none --tools Read,Grep,Glob,Bash --allowedTools "Bash(<prefix>*)" … --disallowedTools …
  --setting-sources= --add-dir <dir> --output-format json` ran 10 turns to `end_turn` (`subtype: success`), every
  allowed command ran without a prompt and nothing was denied; the result JSON carries `num_turns`,
  `permission_denials`, `usage` and `total_cost_usd` (fixture `tests/fixtures/real/claude_code_session_result.json`).
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

### 3.6 Phases 3–5 — ground rules (approved 2026-09-27, supersedes §3.1 "bounded tools"/"persistent", §3.2 items 4–6, §3.4 and §3.5 where they differ)

The user's decisions of 2026-09-27 (D-043): position management = "Claude leads, code protects"; notifications = Telegram
bot + Windows toast + log; models = Sonnet for decisions, every role's model/effort configurable at any time, optional
stronger-model confirmation of strong setups; cap = 40 calls per pair per day. Order: Phase 3 "sees and manages", Phase 4
"watches and learns", Phase 5 "goes deeper" (position management moved from Phase 5 to Phase 3 because the open BTC
position showed the gap on day one).

| Rule | Enforcement |
|---|---|
| Production keeps running | one worktree per phase: `C:\the_claude_new_wt\phase3` (`feat/phase3-sees-manages`), `…\phase4` (`feat/phase4-watches-learns`), `…\phase5` (`feat/phase5-goes-deeper`); tests: `PYTHONPATH=src C:/the_claude_new/.venv/Scripts/python.exe -m pytest tests/unit -q -p no:cacheprovider`; **never** `run --stop`, `stop*.bat`, `switch_to_pairs.bat` from a worktree |
| Mergeable and restart-safe on its own | additive DB changes only (`CREATE TABLE IF NOT EXISTS`, `ALTER TABLE ADD COLUMN` behind a `PRAGMA table_info` guard as in `ai/budget.py:EXTRA_COLUMNS`); new behaviour behind config keys with safe defaults; old code ignores new columns |
| Fail closed on money paths | every new executor step catches its own exceptions → `ingestion_events` row, never blocks `process_candidates`; anything unverifiable at the broker is "unknown", never "done" |
| ONE billed Claude call per phase | on a scratch data root (`--data-dir <scratch>` with `<scratch>\hot`, `<scratch>\cold` as `mklink /J` junctions to production, a copy of the pair's app.db, own `shared\ai_usage.db`); a CLI input-format rejection (no API request) is not a billed call |
| User-only actions | `.env`, Task Scheduler, `pip install`, merges, restarts, `config.local.yaml` toggles — the agent writes the exact commands as H rows in `PROJECT_STATUS.md` |
| Review before merge | independent adversarial review of the branch (as in Phases 1–2), then the user merges (`git merge --ff-only`) and restarts (`scripts\restart_all.bat`) |
| No automated git writes from any operator session | runtime tuning state lives under `data/` (git-ignored); proposals are created in separate worktrees; the production checkout stays clean for ff-merges (deviation from the handoff's `tuning/<date>` commits — justified by D-032) |
| Secrets | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` via `ai/providers/base.py:secret()` (re-reads `.env`), redacted by `core/logsetup.py:get_redactor()`, added to `Settings.secret_env_names()` |

### 3.7 Phase 3 spec — "sees and manages"

Goal: Claude reads six chart images, is screened every 5 minutes by Python and called only on change (≤ 40/pair/day),
can be confirmed by a stronger model on strong setups, and the executor manages open trades from the rules Claude
declared while Claude may issue bounded actions on live trades; prompts rewritten (v6) as a professional desk brief; the
prompt cache fixed; the single-leg fallback made honest.

##### 3.1 Build order and files

| # | Component | Files (under `src/tradingsystem/` unless noted) | Reuse |
|---|---|---|---|
| 1 | Prompt-cache fix | `ai/orchestrator.py` (`_system_vars` → drop `account_equity/currency`; `_user_vars` gains them), `ai/prompts/shared/core_rules.md`, `ai/prompts/agent_per_pair/instructions.md` | `prompts/__init__.py:render`, `RenderedPrompt.prompt_hash` |
| 2 | Charts | new `analysis/charts.py`; `pyproject.toml` (declare matplotlib, pillow), `requirements.lock` | `analysis/frames.py:load_frame`, `analysis/indicators.py:ema/atr`, payload `levels`, `timeframes.<tf>.{zones,liquidity,structure}`, `account.{open_positions,pending_orders}`, `market.basis_exec_minus_analysis` |
| 3 | Image transport | `ai/providers/base.py` (`generate(..., images=None)` → `_call`), `ai/providers/claude_code.py` (`build_args(images)`, `user_message_line()`), other providers accept+ignore `images` (one warning), `ai/repair.py` (images on the first attempt only), `ai/budget.py` (`ai_usage` cols `role, images, image_tokens_est`) | `claude_code.py:parse` (already reads the last `result` line), `_staggered_start`, `claim_start` |
| 4 | 5-minute screening, on-change, cap, event triggers | `ai/triggers.py` (`setup_reasons(payload, *, weak_min, liquidity_atr, screen_tf)`, new `setup_signature`, `decide` gains `new_strong/new_weak/changed/events`), `analysis/engine.py` (`tick`, `evaluate`, `_ration`, `engine_kv`, executor-event polling), `ai/budget.py:RateLimiter.cap`, `core/settings.py:AICfg` | `engine._data_ready`, `store.last_decision/last_attempt_ts`, `appdb.add_event/statuses`, `triggers.review_due` |
| 5 | Per-role models + escalation | `core/settings.py` (`AIModelsCfg`, `AIEscalationCfg`), `ai/providers/__init__.py:make_provider(model=, effort=)`, `ai/orchestrator.py` (`_get(name, role)`, `_per_pair` escalation branch, `CycleRequest.strength`), new prompts `ai/prompts/escalation/{system,instructions}.md`, `ai/contract.py:EscalationReview` | the `risk_reviewer` branch in `_per_pair` (sub_outputs; failure → withheld) |
| 6 | Position management (P9.6) | new `execution/management.py` (`PositionManager`); `execution/backends/mt5_backend.py` (`modify_sl` hardened, `close_position(volume=)`, `cancel_order` detail, new `legs_of(decision_id)`), `execution/backends/paper.py` (generic rules, `modify_leg`, `close_legs`), `execution/price_mapping.py:translate` (shift management/action values), `execution/executor.py:step` | `mt5_backend.existing/specs/quote/_filling`, `retcodes.describe/retryable/SUCCESS`, `executor.atr`, `analysis/structure.py`, `exposure.aggregate` |
| 7 | Claude position actions | `ai/contract.py` (`PositionAction`, `Recommendation.position_actions`), `ai/orchestrator.py:_finalize`, new `execution/action_gate.py`, `execution/executor.py:process_actions`, `ai/store.py` (`actions_state`, `pending_actions()`) | `risk_gate.py` check style `add(name, ok, detail)`, `store.set_execution_state` |
| 8 | Prompts v6 | `ai/prompts/shared/trader_persona.md` v2, `shared/core_rules.md` v6, `shared/payload_legend.md` v4, `agent_per_pair/system.md` v3, `agent_per_pair/instructions.md` v4, new `escalation/*`; `ai/store.py` (`library_hash`, `trigger_strength` columns) | `prompts/__init__.py:library_hash` (exists, unused → stored per decision) |
| 9 | Gate on the leg actually placed | `execution/risk_gate.py` (`rr_after_costs` on the single leg when volume cannot split), move `split_volume` to `execution/sizing.py` (re-exported from `paper.py`) | `paper.py:split_volume`, `mt5_backend.build_requests` fallback rule |
| 10 | Dashboard minimum | `api/app.py` (`GET /api/charts/{pair}/{tf}` serving `data/instances/<PAIR>/charts/<tf>.png`), `web/static/app.js:showDecision` (operator_notes, position_actions, chart thumbnails) | existing routes |

##### 3.2 Behaviour rules

**Prompt cache.** `account_equity` leaves the system prompt (rule 8 → "equity is `account.equity` in the payload; the
cycle header repeats it"); `instructions.md` header line "Account equity ≈ $account_equity $account_currency".
Acceptance: `render()` with equity 99 and 158 → identical `prompt_hash`.

**Charts (`analysis/charts.py`).** Pure matplotlib, Agg backend set in the module, **no mplfinance, no pandas** (D-005;
pandas ≈ 60 MB RSS). Candles = `vlines` wicks + `bar` bodies, volume panel below (titled "tick volume" when
`Frame.volume_kind == "tick"`, i.e. XAU). API `render_set(reader, inst, as_of, payload, cfg) -> list[ChartImage(tf, png,
bars, width, height, token_est)]`; frames via `load_frame(..., cfg.bars[tf], as_of)`; the snapshot is untouched
(`payload_hash` unchanged). Overlays from the payload: `levels` lines; `zones.order_blocks/fvg` (≤ 3 each, translucent
rectangles from `formed` to the right edge); liquidity pools (dashed); last 6 `structure.events` (marker + BOS/CHoCH/sweep
label); EMA20/50; **this pair's holdings** (entry/SL/TP lines from `account.open_positions/pending_orders`, translated back
to the analysis space by subtracting `market.basis_exec_minus_analysis`). 720×400 px, dpi 100, deterministic PNG
(`metadata={"Software": None}`), `plt.close(fig)` in `finally`. Defaults: TFs `[1w,1d,4h,1h,15m,5m]`, bars `{1w:60,
1d:120, 4h:120, 1h:120, 15m:96, 5m:96}`. Rendered inside `Orchestrator._per_pair` with `asyncio.to_thread` (the engine
heartbeat is never blocked); per-pair cache `{tf: (last_closed_open_time, overlay_signature, png)}` → re-render only when
the bar or the overlays changed; latest PNGs also written to `data/instances/<PAIR>/charts/<tf>.png`. Token estimate
`ceil(w*h/750)` (= 384) per image → `ai_usage.image_tokens_est`, added to `est_input_tokens` in `_gen`. Config
`ai.charts: {enabled: true, width: 720, height: 400, timeframes: [...], bars: {...}, overlays: [levels, zones,
liquidity, structure, holdings, ema]}`; `enabled: false` = text-only rollback. Log the engine RSS delta after the first
render set.

**Image transport.** With images and provider `claude_code`: args `[-p, --input-format, stream-json, --output-format,
stream-json, --verbose, --model, M, --system-prompt-file, F, --tools, "", --strict-mcp-config, --setting-sources, "",
--no-session-persistence, (--effort E), (--fallback-model)]`; stdin = ONE line
`{"type":"user","message":{"role":"user","content":[{"type":"text","text":<user prompt>},{"type":"text","text":"Chart 1w — <pair> <analysis instrument>, last N closed candles, analysis prices"},{"type":"image","source":{"type":"base64","media_type":"image/png","data":"<b64>"}}, …]}}\n`
then stdin closed (`proc.communicate`). If the CLI rejects the line at its parser (non-zero exit within ~2 s, no `result`
line, no API call) try the alternate shape `{"type":"user","content":[…]}` once and record the winner in
`data/shared/cli_capabilities.json` (`{"stream_json_user_shape": "message"|"content", "cli_version": …}`) — a one-time
probe per machine; if both fail, log `charts_disabled_cli_shape` and fall back to the text/json path. `parse()` keeps
reading the last `{"type":"result"}` line (its `usage` block is used unchanged). Repair calls re-send text only. Without
images nothing changes.

**Screening and calls.** New `AICfg` keys: `screen_timeframe: 5m` (must be < decision TF and in the pair's timeframes),
`screen_move_atr: 0.5` [0.2, 1.0], `daily_calls_per_pair: 40` [10, 120], `event_calls_per_day: 6` [0, 20]; remove
`instance_max_rpd_share` (tell the user if `config.local.yaml` sets it — `extra="forbid"`). `RateLimiter.cap()`: instance →
`min(rpd, daily_calls_per_pair)`, all-pairs → `rpd`; the cap counts **every ledger row of the pair** (first attempts,
repairs, escalations — each spends the subscription).
Engine `tick()`: keep `processed[pair]` for the decision-TF close, add `processed_screen[pair]` for the 5m close (SETTLE
5 s; `_data_ready` on the 5m table; a missing 5m bar is skipped, DEBUG-logged once per bar — no `data_not_ready` spam).
At each 5m close build the payload once (`orch.payload`) and evaluate. **Measure the payload build time first** (log
`snapshot_build_ms`); if the 5-minute build exceeds 3 s on this laptop, build a lighter screen payload (decision TF +
1h + 5m only) for screening and the full one only for a dispatch.
`setup_signature(strong, weak) -> frozenset[str]`: structure `f"{tf}:{kind}:{dir}:{event_time}"`, zone
`f"{tf}:{zone_kind}:{dir}:{top}-{bottom}"`, liquidity `f"{tf}:liq:{side}:{level}"`, pattern `f"{tf}:pattern:{bar_time}"`,
footprint `f"fp:{bar_time}:{kind}"`, divergence `f"div:{type}:{last_pivot_time}"`. Persist per pair in new table
`engine_kv` (`last_signature`, `last_call_price` = analysis mid at dispatch via `orchestrator._analysis_mid`,
`last_event_id`), updated at dispatch only.
`setup_reasons` gains the 5m TF: 5m BOS/CHoCH/sweep count as **weak** (15m/1h stay strong); `weak_min` (2) and
`liquidity_atr` (0.3) become parameters (settings now, adaptive overlay in Phase 4).
`decide()` at any 5m close: `new_strong = strong − last_signature`, `new_weak = weak − last_signature`; fire on
`new_strong` or `len(new_weak) ≥ weak_min` (strength strong/weak), on price/candle review conditions (review), or on
executor events (event). Time-based `next_review.in_minutes` fires only when `changed` = a new decision-TF bar closed since
the last call **and** (`len(new_weak) ≥ 1` or `|mid − last_call_price| > screen_move_atr × timeframes[decision_tf].indicators.atr14`).
Idle floor unchanged (`max_idle_minutes`, at a decision-TF close). Spacing: strong/weak/idle ≥ `min_minutes_between_calls`;
review/event ≥ `review_floor_minutes`; back-off unchanged. Candle-close conditions: 5m ones at 5m closes, 15m at 15m.
`_ration` with cap 40: left/cap < 0.5 → strong + review + event; < 0.2 → review + event; 0 → none; event calls also capped
by `event_calls_per_day` (count `ai_decisions.trigger LIKE 'event:%'` today).
**Event triggers:** the engine polls `ingestion_events` (`collector='executor'`, `id > last_event_id`) every tick; waking
events: `order` (placed), `paper_filled`/`mgmt_filled`, `mgmt_position_closed` (reason SL/TP/rule/model), `outcome`,
`action_applied`, `action_rejected`; coalesced within 60 s into one reason `"event: BTCUSDT filled 0.01 @ 84350; TP1 hit"`.

**Roles and escalation.**
```yaml
ai:
  models:                                   # per role; model = sonnet|opus|fable or a full name; effort low|medium|high|max
    decision:   {model: sonnet, effort: medium}
    escalation: {model: opus,   effort: high}
    review:     {model: opus,   effort: high}     # Phase 4 sessions
    monitor:    {model: sonnet, effort: low}      # Phase 4 diagnosis
  escalation:
    enabled: false            # the user flips it in config.local.yaml
    on_strength: [strong]
    min_confidence: 60        # first answer must reach this
    max_per_day_per_pair: 6
    on_failure: withhold      # withhold | keep
```
`make_provider(settings, name, model=, effort=)`; `ClaudeCodeProvider.effort` instance attribute used by `build_args`;
`Orchestrator._get(name, role)` caches by `(name, model, effort)`; one `RateLimiter` per provider name (shared cap). In
`_per_pair`: after a valid BUY/SELL with `strength ∈ on_strength`, `confidence ≥ min_confidence`, today's escalations <
max and ≥ 2 calls left in the cap → render role `escalation` (same user text + charts + the first answer as `$proposal`);
contract `EscalationReview{verdict: confirm|downgrade, issues ≤ 8, confidence, final_recommendation}`; code enforces
`final.decision ∈ {first.decision, NO_TRADE}`, `final.confidence ≤ first.confidence`, levels unchanged on confirm (else the
record is `invalid: escalation altered levels`); stored as sub_output role `escalation`, ledger `role='escalation'`;
timeout 150 s (the decision must still pass `max_recommendation_age_s`); failure → `on_failure`.

**Position management (`execution/management.py`, pure functions testable without MT5).**
`evaluate_rules(rules, legs, quote, ctx) -> list[Action]`; legs from `MT5Backend.legs_of(decision_id)` (positions by tag
+ `history_deals_get(position=…)` to learn how a leg ended: `DEAL_REASON_TP/SL`) or `PaperBackend.decision_legs`.
Triggers (execution price space): `tp_hit k` → leg k closed by TP; `price_reached v` → bid (BUY)/ask (SELL) crossed v;
`r_multiple v` → `(px − fill)/(fill − sl0) ≥ v` with the original SL; `minutes_elapsed v` → since fill; `candle_close v`
→ at a decision-TF close beyond v. Actions: `move_sl_to_breakeven` → `fill ± max(spread × breakeven_buffer_spread_mult,
stops_level + spread)` on the safe side of the fill, never worse than the current SL; `partial_close params.fraction` →
volume rounded down to `volume_step`, ≥ `volume_min`, remainder ≥ `volume_min` else full close; `trail_atr
params.atr_mult ∈ [0.5, 5]` and `trail_structure` (last swing low/high ± `stops_level + spread`) → once per decision-TF
close (`management_state.last_bar_ms`), tighten only, ignore moves < `min_sl_change_ticks` (5); `close_all` → close open
legs + cancel pending legs. Time stop = `close_all` + `minutes_elapsed`. **A move that does not yet satisfy the venue
(closer than `stops_level + spread` to the current price) is deferred and retried every loop until it qualifies or the
leg closes — never rejected permanently.** New `ManagementRule` validators: `tp_hit` value integer ∈ [1, 4];
`partial_close` needs `fraction ∈ (0, 1)`; `trail_atr` needs `atr_mult ∈ [0.5, 5]`; `minutes_elapsed ≥ 5`;
`price_reached > 0`; `r_multiple ∈ (0, 10]`. `translate()` now shifts `management[].value` (price triggers) and
`position_actions[].value` — a real bug today.
MT5: `modify_sl(ticket, symbol, sl, tp)` rewritten — round to `digits`, refuse widening (BUY: `sl > current_sl` only),
require ≥ `stops_level + spread` from the current price and outside `trade_freeze_level`, retcode 10025 ("no changes") =
ok, `retryable()` codes retried twice, every attempt returned with `describe(code)`; `close_position(ticket, volume=None)`
partial (`TRADE_ACTION_DEAL` with `position=ticket`); `cancel_order` with retcode detail. Called only from
`PositionManager`/`process_actions`. The executor runs `manage_positions()` every loop (1 s) for MT5 and paper; per-loop
cost one `positions_get()` + ATR/structure cached per decision-TF bar. **Protective actions keep running under
`KILL_SWITCH`** (they reduce risk); only new orders are blocked — documented in `docs/ops_windows.md`.
Config `execution.management: {enabled: true, dry_run: false, breakeven_buffer_spread_mult: 1.0, min_sl_change_ticks: 5}`;
`dry_run` records `status='skipped'` with `detail.dry_run=true`.

**Claude position actions.** Contract: `Recommendation.position_actions: list[PositionAction] ≤ 4` (allowed with
NO_TRADE — the main use: hold but tighten). `PositionAction{target: {decision: <8-char id shown in
account.open_positions/pending_orders>, kind: position|order}, action: modify_sl|modify_tp|close|cancel_order,
value: float|None, fraction: float|None, reason: Text(200)}`; validators: `modify_sl/modify_tp` need `value > 0`;
`close` fraction ∈ (0, 1] default 1; `cancel_order` only for orders, the others only for positions; values in the
analysis price space. `_finalize` drops actions whose target is not in the payload's holdings (note in `errors`); sets
`actions_state='pending'` when any remain. Executor `process_actions()` (after `process_candidates`, before
`manage_positions`): `ai_decisions WHERE status='valid' AND actions_state='pending' AND ts ≥ started − 1 h`; resolve the
target (`id LIKE ? || '%' AND pair=? AND execution_state='executed'`; **exactly one match, else rejected `ambiguous_target`**);
legs via `legs_of`; translate the value by the live basis (`check_basis` as in `_handle`); `action_gate.evaluate(action,
legs, ctx)` checks: `own_target`, `not_expired` (`now − rec.timestamp ≤ max_recommendation_age_s`), `quote_fresh` (≤ 30 s),
`market_open`, `modify_sl`: tighter only + ≥ `stops_level + spread` from price + never None (a valid-but-too-close move is
deferred, see above); `modify_tp`: correct side, ≥ `stops_level` from price; `close`: volume rounding rule; `cancel_order`:
target is a pending order; rate limits `max_per_decision` (4), `max_per_pair_per_day` (12 applied),
`min_minutes_between_sl_changes` (15) per ticket. Apply via the backend; write a `position_actions` row per
(source_decision, seq, leg) with the check list; `actions_state='done'`; events `action_applied`/`action_rejected` (these
wake the model). Paper equivalents `PaperBackend.modify_leg(leg_id, sl=, tp=)`, `close_legs(decision_id, fraction,
quote)`, `cancel_decision` (exists). Config `execution.position_actions: {enabled: true, max_per_decision: 4,
max_per_pair_per_day: 12, min_minutes_between_sl_changes: 15}`.

**Prompts v6 (every file's `<!-- prompt: … · version N -->` header bumped).**
- `trader_persona.md` v2 — the professional process: (1) manage what is open before looking for new trades; (2) read the
  charts top-down (1w/1d/4h context → 1h structure → 15m setup → 5m trigger), then confirm every visual read against the
  payload numbers (numbers are authoritative for levels); (3) act only on confirmed things — a closed candle beyond a
  level, a sweep that closed back inside, a retest that held — never in anticipation; (4) write the plan as `management`
  rules and exact `next_review` conditions; (5) keep `operator_notes`. "If in doubt, NO_TRADE."
- `core_rules.md` v6 — rule 8 without `$account_equity`; rule 13 rewritten: earlier orders/positions are managed by the
  system from the `management` rules declared with them; on later cycles the trader may act through `position_actions`
  (tighten a stop, take profit with a fraction, adjust a TP, cancel a pending order), can never widen/remove a stop or add
  size, the system checks each action against the venue's stops level; BUY/SELL adds a NEW order (refused in the same
  direction); "return NO_TRADE with actions when the right move is to manage, not to add"; rule 14 (charts): the images
  show the same closed candles as `timeframes.<tf>.recent` in the analysis instrument's prices with levels/zones/
  liquidity/holdings overlaid — use them for shape, momentum, context; exact numbers from the payload; rule 15
  (single leg): at the 0.01-lot minimum the system usually cannot split take-profits — it places ONE position at the TP
  with the largest `close_fraction` (ties → nearest) and measures RR on that leg; give the largest fraction to the target
  you want executed; rule 16 (playbook): the `Playbook` section holds desk notes learned from this pair's record —
  guidance, never a reason to break rules 1–15.
- `payload_legend.md` v4 — `position_actions`, `management` semantics, `trigger_reason` prefixes (`event:`, `review
  condition:`, `idle:`), the chart list.
- `agent_per_pair/instructions.md` v4 — `$charts_note` ("Attached: 6 charts (1w, 1d, 4h, 1h, 15m, 5m), 720×400, last N
  closed candles each, analysis prices" | "No charts this cycle"), a `Playbook` block `$playbook` (Phase 3 passes "(no
  playbook yet)"), the equity line; `system.md` v3.
- `escalation/system.md` + `instructions.md` — a senior risk partner who sees the same screens and the trader's proposal
  and may only confirm or downgrade.
- `DecisionStore` columns `library_hash`, `trigger_strength` (from `CycleRequest.strength`).

**Gate on the leg actually placed.** In `risk_gate.evaluate`, after sizing: if `len(tps) > 1` and
`split_volume(size.lots, fractions, volume_step, volume_min) is None` → `rr_after_costs` uses only the largest-fraction TP
(ties → nearest, the `build_requests` rule) with detail `single leg at TPk`; `executed_levels.take_profits` then lists that
TP.

##### 3.3 DDL (per-instance `app.db`, created by the owning module; shared ledger for `ai_usage`)
```sql
CREATE TABLE IF NOT EXISTS position_actions (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, pair TEXT NOT NULL,
  source TEXT NOT NULL /* model|rule */, source_decision TEXT NOT NULL, seq INTEGER NOT NULL,
  target_decision TEXT NOT NULL, leg TEXT NOT NULL /* MT5 ticket or paper leg id */,
  action TEXT NOT NULL, requested TEXT, status TEXT NOT NULL /* pending|applied|rejected|failed|skipped|deferred */, detail TEXT,
  UNIQUE(source, source_decision, seq, target_decision, leg));
CREATE TABLE IF NOT EXISTS management_state (decision_id TEXT NOT NULL, rule_idx INTEGER NOT NULL, leg TEXT NOT NULL,
  status TEXT NOT NULL, last_bar_ms INTEGER, applied_ms INTEGER, detail TEXT, PRIMARY KEY(decision_id, rule_idx, leg));
CREATE TABLE IF NOT EXISTS engine_kv (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_ms INTEGER NOT NULL);
ALTER TABLE ai_decisions ADD COLUMN actions_state TEXT;  -- + library_hash TEXT, trigger_strength TEXT (PRAGMA-guarded)
ALTER TABLE ai_usage ADD COLUMN role TEXT;               -- + images INTEGER, image_tokens_est INTEGER
```

##### 3.4 Tests (unit, real fixtures in `tests/fixtures/real`)
| File | Proves |
|---|---|
| `test_charts.py` | 720×400 PNG from `btcusdt_candles_1m_3000.csv` (+5m resample), deterministic bytes, overlays drawn, 30 renders grow RSS < 20 MB and leave 0 open figures, token_est 384 |
| `test_claude_code_images.py` | args carry `--input-format stream-json --output-format stream-json --verbose`; user-line shape; `parse()` on a redacted real NDJSON capture (new fixture `claude_code_stream_json_result.jsonl`); repairs send no images; ledger rows carry `images/image_tokens_est/role` |
| `test_triggers_screen.py` | signature keys, dedupe (same reasons → no fire), `weak_min`, 5m events weak, the 0.5-ATR `changed` rule, idle floor, event coalescing, `event_calls_per_day`; `test_replay_budget`: replaying 24 h of stored `ai_payloads` fires ≤ 40/pair/day |
| `test_call_cap.py` | `cap()` = 40 in instance mode, counts repair/escalation rows; `_ration` at 20/8/0 |
| `test_roles_escalation.py` | scripted provider per role; escalation only on strong + conf ≥ 60; keep/downgrade only; never raises confidence or changes levels; failure → per config; daily max |
| `test_management.py` | paper on real XAU ticks: breakeven with spread buffer, partial rounding, trail once per bar tighten-only, time stop, deferred too-close move; MT5 fake: `modify_sl` refuses widening/inside stops level, 10025 ok, retry, `close_position(volume)`; idempotent state; **property test over 200 random rule sets: no rule ever loosens an SL** |
| `test_position_actions.py` | validators; `_finalize` drops unknown targets; ambiguous prefix rejected; gate checks incl. rate limits; basis translation; paper + MT5-fake application; `actions_state`; events |
| `test_prompts.py` (extend) | prompt_hash stable across equity; all roles render; version headers parsed; `library_hash` per decision; `$playbook` default |
| `test_gate_single_leg.py`, `test_price_mapping.py` (extend) | RR on the largest-fraction leg when the volume cannot split; management/action values translated |

**The ONE live call:** `engine --once --pairs BTCUSDT` from the worktree on the scratch data root with charts on.
Record in `docs/measurements/phase3_live.md`: input tokens (target ≤ 23 k incl. ≈ 2.3 k image tokens), output, latency
(≤ 120 s), `num_turns` = 1, valid first attempt, `position_actions` empty or valid, which user-line shape worked, engine
RSS before/after rendering, `snapshot_build_ms`.

##### 3.5 Acceptance
- All existing + new unit tests pass; `prompt_hash` invariant to equity.
- Live call valid with 6 images, ≤ 23 k input tokens, ≤ 120 s.
- Replay of 24 h of stored payloads: ≤ 40 calls/pair/day.
- Management on the XAU tick fixture produces the expected `position_actions` rows; property test holds.
- Engine RSS increase with charts ≤ 60 MB per pair (measured, in the docs).

##### 3.6 User actions (H rows) and rollbacks
H15 `.venv\Scripts\pip install -r requirements.lock` (no-op today; libs now declared) · H16 `git merge --ff-only
feat/phase3-sees-manages` + `scripts\restart_all.bat` · H17 (optional) `config.local.yaml`: `ai: {escalation: {enabled:
true}}`; `ai.models.escalation.model: fable` for Fable. Rollbacks without code: `ai.charts.enabled: false`,
`execution.management.dry_run: true`, `execution.position_actions.enabled: false`.

##### 3.7 Risks
RAM (0.4 GB free): charts +35–60 MB/engine — start with one pair's charts if needed; the docs restate the 16 GB RAM
recommendation. Live management touches demo positions: tighten-only invariants + property test + dry-run switch.
stream-json shape: one-time probe + text fallback. Cap counts repairs: expect ~25–30 first attempts/day.

### 3.8 Phase 4 spec — "watches and learns"

Goal: the system measures its own decisions; Claude reviews them daily/weekly from Task Scheduler with read-only tools,
tunes only a bounded per-pair overlay and playbook (auto, logged, expiring, revertible), proposes everything else; a
pure-Python monitor and a notifier watch production without the desktop app; the dashboard shows notes, tuning,
proposals, reviews.

##### 4.1 Components
| # | Component | Files | Reuse |
|---|---|---|---|
| 1 | Feedback metrics + attribution + prompt registry | `ai/store.py` (columns, `prompt_versions`, `pending_metrics()`), new `execution/metrics.py`, `execution/executor.py` (housekeeping), `ai/prompts/__init__.py` (`render` returns versions; `register_versions(store)`) | `executor.evaluate_virtual` (1m walk), `mt5_backend.decision_result` (extend to return commission/swap split), gate detail (+`spread`), `context.sessions`, `confluence.bias`, `timeframes.<dec>.regime` |
| 2 | Adaptive overlay + playbooks + `tune.py` | new `core/adaptive.py` (`AdaptiveCfg`, `AdaptiveStore`), new `core/playbook.py` (`lint`), new `tools/tune.py`, consumers in `analysis/engine.py`, `execution/executor.py` (gate floor), `ai/orchestrator.py` (`$playbook`, `$tp_hint`, `playbook_hash`, `adaptive_hash`) | pydantic style of `settings.py`, `core/filelock.FileLock`, `execution/drawdown._put` (atomic write), Phase-3 `setup_reasons(weak_min, liquidity_atr)` |
| 3 | Review packs + operator sessions + proposals | new `tools/review_pack.py`, `tools/propose.py`, `tools/operator/{run_session.ps1, session_args.py, prompts/_system.md, prompts/daily.md, prompts/weekly.md, prompts/diagnose.md}`, new `scripts/install_operator_tasks.ps1|.bat` (uninstall also removes them) | `tools/health_report.py:machine_report/system_report` (import, don't duplicate), `install_autostart.ps1` task pattern |
| 4 | Pure-Python monitor | new `tools/monitor.py` (+ `scripts/monitor.bat`), state `data/shared/monitor_state.json` | `appdb` tables, `procs.running_supervisors`, `control.read_state`, `psutil`, `drawdown._read` |
| 5 | Notifier | new `core/notify.py`, `tools/notify.py`, `scripts/notify.ps1`; hooks in engine, executor, management, monitor | `secret()`, `get_redactor()`, `httpx` |
| 6 | Usage gauge | new `ai/usage_gauge.py`; `engine._ration`, `engine.write_status` | `UsageStore` (+`tokens_since(since, pair=None)`) |
| 7 | Dashboard + API | `api/app.py` (`/api/operator/{pair}`, `/api/adaptive`, `/api/tuning_changes`, `/api/position_actions`, `/api/proposals`, `/api/reviews[/{name}]`, `POST /api/kill_switch` ON only), `web/index.html`, `web/static/{app.js,style.css}` | token/origin protection of `execute` |
| 8 | Docs | new `docs/learning_loop.md`, `docs/operator_sessions.md`, `docs/monitoring.md`; `docs/ops_windows.md` §8; `PROJECT_STATUS.md` (P12.4, D-043…D-046, H rows) | — |

##### 4.2 Behaviour rules

**Metrics (`decision_metrics`, executor housekeeping every 60 s)** for decisions with `outcome` or `virtual_outcome` and
no metrics row, from real 1m bars of the analysis instrument between `timestamp` and resolution: `mfe_r`, `mae_r` (R of
the original SL from the worst fill edge as in `rr_computed`), `tp1/2/3_hit`, `minutes_to_resolve`, `exit_reason ∈ {sl,
tp, rule_close, model_close, expired, not_triggered, open}` (from `position_actions`/deals), `slippage` (mean of
`backend.placed[].slippage`), `spread_at_gate`, `commission`, `swap`, `rejected_but_virtual_win`,
`no_trade_counterfactual_atr` (max |move| in decision-TF ATR over the next 4 bars). Attribution columns at record time:
`setup_kinds` (json), `session` (killzone or active), `regime`, `htf_bias`, `data_warnings`. `prompt_versions` upserted at
every render.

**Adaptive overlay.** Files `data/adaptive/<PAIR>/{adaptive.yaml, playbook.md, changes.jsonl}` + table
`tuning_changes` in the pair's app.db. Nothing in git, no restart: `AdaptiveStore.current()` re-reads on mtime change
(checked ≤ every 5 s), validates with `AdaptiveCfg`; an invalid file keeps the last good values + one `adaptive_invalid`
event; missing = defaults. Single writer `tools/tune.py` under `FileLock(data/shared/locks/adaptive_<PAIR>.lock)`, atomic
replace. Keys, bounds, autonomous direction, consumer:

| key | bounds | direction | consumer |
|---|---|---|---|
| `min_confidence_floor` | [55, 80] | raise only | executor gate `min_confidence = max(risk.min_confidence, floor)` |
| `min_minutes_between_calls` | [15, 60] | up only | engine |
| `max_idle_minutes` | [60, 240] | up only | engine |
| `review_floor_minutes` | [5, 30] | up only | engine + `_review_plan` |
| `trigger.weak_min` | [2, 3] | up only | `setup_reasons` |
| `trigger.liquidity_atr` | [0.2, 0.5] | down only | `setup_reasons` |
| `pair.ai_paused_until` | ≤ now + 7 d | — | engine skips dispatch (event `ai_paused`) |
| `tp_hint` | ≤ 200 chars, linted | — | user prompt `$tp_hint` |
| playbook | ≤ 1500 chars, ≤ 12 bullets, linted | — | user prompt `$playbook` |

Entry = `{value, set_ms, expires_ms ≤ set_ms + 14 d, reason, evidence, window_hours, review_id}`; an expired entry is
absent (default) — the engine appends one `expired` line and sets `tuning_changes.reverted_ms`; `adaptive_hash` and
`playbook_hash` stored per decision. `core/playbook.py:lint`: length/bullets; denylist regex
`(risk[_ ]per|lot|leverage|stop.?loss (distance|closer)|ignore|override|always (buy|sell|trade)|never no_trade|confidence (8|9)\d|daily loss|kill.?switch|min_rr)`;
`$` must be `$$`.
`tools/tune.py` (only writer): `--pair P set <key> <value> --reason … --evidence-json … --window-hours N --review-id R`,
`playbook <file>`, `revert <key>`, `list`, `--dry-run`. Policy in code: refuse when `data/TUNING_FREEZE` exists or
`adaptive.enabled: false`; ≤ 1 change per pair per calendar day; 7-day cooldown per key; strategy keys (floor, weak_min,
liquidity_atr, tp_hint, playbook) need ≥ 20 resolved virtual outcomes in the window, activity-reducing keys ≥ 10; freeze
when the window is > 25 % **unhealthy** (:= an `ai_decisions` row with status error/skipped/budget_blocked or
`data_warnings` naming the decision TF, or an hour with a `killed`/`exited` event for engine/executor/ingest-mt5 or a
heartbeat gap > 15 min); direction per table; exit 0 applied / 2 refused (reason) / 3 invalid.

**Review packs and operator sessions.** `tools/review_pack.py --hours 24|168 [--pair P] --out data/reviews/` →
`<ts>_daily.md|.json` (≤ ~10 k tokens; last 25 ideas): machine + per-system health (from `health_report`), per-pair funnel
(5m screens → triggers by strength → calls by role → valid → ideas → gate by check → placed → outcomes broker/virtual →
metric means), position actions and rule executions, escalation verdicts, log ERROR counts, adaptive values with expiry,
tuning changes, hashes (prompt/library/playbook/adaptive/config/git), usage by role and cache-read share, gauge level.
`tools/operator/run_session.ps1 -Kind daily|weekly|diagnose [-DryRun]`: builds the pack, reads CLI args from
`session_args.py --kind` (`ai.models.review/monitor`), runs
`claude -p --model <m> --effort <e> --output-format json --no-session-persistence --setting-sources "" --strict-mcp-config --permission-mode dontAsk --permission-prompts none --max-turns 30 --add-dir C:\the_claude_new --system-prompt-file tools\operator\prompts\_system.md --tools "Read,Grep,Glob,Bash" --disallowedTools "Edit,Write,NotebookEdit,WebFetch,WebSearch" --allowedTools "Read" "Grep" "Glob" "Bash(.venv/Scripts/python.exe tools/health_report.py*)" "Bash(.venv/Scripts/python.exe tools/review_pack.py*)" "Bash(.venv/Scripts/python.exe tools/tune.py*)" "Bash(.venv/Scripts/python.exe tools/propose.py*)" "Bash(.venv/Scripts/python.exe tools/notify.py*)" "Bash(git status*)" "Bash(git log*)" "Bash(git diff*)"`
with the kind's prompt + pack path on stdin; output → `data/reviews/<ts>_<kind>.session.json`; a **diff guard** compares
`git status --porcelain` before/after and notifies `review_touched_checkout` (never auto-reverts). `demo_order_test.py` and
`migrate_instance.py` are not in the list. The diagnosis kind may additionally run `scripts/kill_switch_on.bat /nopause
<PAIR>`. The session ends with a ≤ 1500-char summary that `run_session.ps1` sends via `tools/notify.py`.
`tools/propose.py --slug S --title T --body-file F --pair P`: creates worktree `C:\the_claude_new_wt\proposal-<date>-<slug>`
on branch `proposal/<date>-<slug>` from `main`, writes `docs/proposals/<date>-<slug>.md` (problem, numbers, proposed
diff/text, risk, test plan) + a `PROJECT_STATUS.md` row there, commits there, appends `data/shared/proposals.jsonl`
(`status: awaiting_user`), notifies; the production checkout is untouched; the user merges or deletes.
Task registration (user action, `scripts/install_operator_tasks.ps1 [-DryRun]`, same principal/settings pattern as
`install_autostart.ps1`): `TradingSystem-Monitor` every 15 min → `pythonw tools\monitor.py --quiet`;
`TradingSystem-Review-Daily` 04:30 UTC → `run_session.ps1 -Kind daily`; `TradingSystem-Review-Weekly` Sunday 06:00 UTC →
`-Kind weekly`; time limits 20/40 min; "run only when logged on" (the CLI login lives in the user profile). Also add
`claude -p --max-turns` and `--permission-mode dontAsk` to the "verify with one call" list.

**Monitor (`tools/monitor.py`, no Claude; exit 0 ok / 1 problems).** Reads every running/expected system
(`health_report.systems()` logic), the shared ledger, `monitor_state.json` (last equity per account, last event id per
system, alert dedupe). Checks → action: heartbeat stale > 15 min (any collector not `stopped`) → warn; MT5 IPC hung
(executor/mt5 `reconnecting|error` with last_error matching `IPC|-1000[45]` > 5 min) → critical (terminal restart stays a
proposal, never automatic); position without SL → critical; order burst > 3 `order` events/pair/hour → per-pair
`KILL_SWITCH` + critical; daily loss ≤ −(max − 2) % warn, ≤ −max % → per-pair `KILL_SWITCH`; equity drop between runs > 5 %
warn, > 10 % → global `data/KILL_SWITCH` + critical; drawdown stop tripped → critical once; `latest_quote` > 10 min while
open → warn; free RAM < 300 MB / disk < 5 GB → warn; restart loop (> 3 exited/killed per service per hour) → warn;
outage (`disconnect` without `resumed` > 10 min) → warn; VPN adapter up/down change (`monitor.vpn_adapter_names`) → info;
no daily review in 30 h → warn; ≥ 1 warn/critical and last diagnosis > 3 h ago → `run_session.ps1 -Kind diagnose`
(≤ 8 k tokens, `--max-turns 12`). The monitor writes only `KILL_SWITCH` files and its state file. Config block `monitor:`.

**Notifier (`core/notify.py:notify(level, title, text, key=None)`).** Always a log line; Windows toast via
`scripts/notify.ps1` (WinRT `ToastNotificationManager` under PS 5.1, no extra module); Telegram `sendMessage` via `httpx`
with `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` from `secret()` (silently skipped when unset), 10 s timeout, daemon thread —
never raises into the caller; redaction; rate limit 20/hour per process; dedupe by `key` within 30 min
(`data/shared/notify_state.json`). Events: order placed/filled/closed, outcome, position action applied/rejected,
management rule applied, escalation verdict, kill switch on/off, drawdown trip, monitor findings, proposal created, review
summaries, gauge level change. `tools/notify.py --level --title --text` for scripts/sessions.

**Usage gauge (`ai/usage_gauge.py`).** Ledger-based: rolling 7-day and 5-hour sums of input+output tokens vs
`ai.usage.weekly_token_budget` (default 12 M) and `five_hour_token_budget` (default 1.5 M) — **both are calibration
guesses**; level 0 < 70 %, 1 (reviews/events only) ≥ 70 %, 2 (pause except events) ≥ 90 %; the CLI's own usage-limit
cooldown remains the hard stop. `_ration` takes the max of the request ladder and the gauge; `write_status` publishes
`usage_gauge`; the health report and the review pack print it.

**Dashboard.** Tabs Operator (per pair: memory notes, last `position_actions`, `position_actions` table,
`management_state`, chart PNGs), Tuning (effective values with expiry, `changes.jsonl`, playbook, freeze flag), Proposals
(`proposals.jsonl`), Reviews (list + escaped markdown). `POST /api/kill_switch` writes this instance's switch (ON only; OFF
stays a script — deliberate friction). No config editing UI.

##### 4.3 DDL
```sql
ALTER TABLE ai_decisions ADD COLUMN setup_kinds TEXT;  -- + session, regime, htf_bias, data_warnings, playbook_hash, adaptive_hash (PRAGMA-guarded)
CREATE TABLE IF NOT EXISTS decision_metrics (decision_id TEXT PRIMARY KEY, computed_ms INTEGER NOT NULL, mfe_r REAL, mae_r REAL,
  tp1_hit INTEGER, tp2_hit INTEGER, tp3_hit INTEGER, minutes_to_resolve INTEGER, exit_reason TEXT, slippage REAL, spread_at_gate REAL,
  commission REAL, swap REAL, rejected_but_virtual_win INTEGER, no_trade_counterfactual_atr REAL, detail TEXT);
CREATE TABLE IF NOT EXISTS prompt_versions (prompt_hash TEXT PRIMARY KEY, role TEXT NOT NULL, library_hash TEXT NOT NULL,
  versions TEXT NOT NULL, git_sha TEXT, first_seen_ms INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS tuning_changes (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, pair TEXT NOT NULL, key TEXT NOT NULL,
  old_value TEXT, new_value TEXT, reason TEXT, evidence TEXT, window_hours INTEGER, expires_ms INTEGER, review_id TEXT, actor TEXT, reverted_ms INTEGER);
```

##### 4.4 Tests
`test_metrics.py` (MFE/MAE/TP hits on the 1m fixture; counterfactual), `test_adaptive.py` (bounds, expiry, mtime reload,
invalid file keeps last good, gate uses `max()`, `AdaptiveCfg` has no `risk`/`execution` keys), `test_tune_policy.py`
(min samples 20/10, cooldown, one/day, freeze > 25 % unhealthy, direction rules, `TUNING_FREEZE`, path allow-list),
`test_playbook_lint.py`, `test_review_pack.py` (seeded app.db → md ≤ 40 k chars), `test_monitor.py` (stale heartbeat, order
burst → the pair's file only, equity drop → global file, dedupe), `test_notify.py` (rate limit, dedupe, redaction, Telegram
payload via `httpx.MockTransport`, unset token → skipped), `test_usage_gauge.py`, `test_api_tabs.py` (routes; kill switch
POST needs token/origin), `test_propose.py` (git mocked; production checkout untouched), `test_prompt_versions.py`.

**The ONE live call:** one daily review session (`run_session.ps1 -Kind daily`, Opus) on a scratch data root against a
copy of production data. Record: tokens (target ≤ 30 k), turns, whether `tune.py` was called and refused/applied within
policy (assert from the scratch `tuning_changes`), the summary received on Telegram + toast, diff guard clean.

##### 4.5 Acceptance
Tests green; `tune.py` cannot write outside `data/adaptive/<PAIR>/` and refuses every out-of-bounds/direction/cooldown
case; the monitor detects a simulated stale heartbeat in one run and a simulated burst writes exactly the pair's switch;
review session ≤ 30 k tokens with a clean diff guard; dashboard tabs render on a seeded DB.

##### 4.6 User actions
H18 `.env`: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (BotFather; chat id from `getUpdates`) — optional · H19
`scripts\install_operator_tasks.bat` (`-DryRun` first); retire the desktop-app 3-hourly task · H20 merge + `restart_all.bat`
· H21 (optional) `config.local.yaml`: `ai.usage.weekly_token_budget` after a week of observation.

##### 4.7 Risks
A review session with Bash: exact allow-list prefixes, editors disallowed, diff guard, no order-sending tool. Tuning drift:
bounds + direction + 14-day expiry + one change/day + `TUNING_FREEZE`. Monitor false positives writing a switch:
conservative thresholds, dedupe, `kill_switch_off.bat`.

##### 4.8 As built (2026-09-27, D-045) — where the implementation differs from the text above
- **Tasks and runner:** `TradingSystemOps-Monitor` / `-ReviewDaily` / `-ReviewWeekly` (a `TradingSystem-*` name would be
  deleted by `install_autostart.ps1`); the runner is Python (`tools/operator/run_session.py`, `run_session.ps1` only
  launches it). The review tasks' `ExecutionTimeLimit` is 130/190 min, a backstop beyond every `operator.*_timeout_min`
  value — the runner's own deadline governs.
- **Session command line:** every Bash rule is `Bash(.venv/Scripts/python.exe tools/<tool>.py *)` with a space before
  the `*` (the CLI compiles it to `<command>( .*)?`; a glued `tools/tune.py*` matched `tools/tune.py/../<any file>`);
  no `git` rules (`git diff/log --output=<file>` writes files); Read, Grep and Glob are tools but no allow rules (the CLI
  allows reads inside the working directories — the checkout and `--add-dir` — by itself; a tool-wide rule allowed
  every other file), with denials for `.env`, `~/.claude`, `~/.claude.json`, `~/.ssh`, `~/.aws`, `~/.config`,
  `*.credentials.json` and, when neither the checkout nor the data root lives in the profile, `~/**`; the diagnosis may
  run only `tools/kill_switch.py --pair <PAIR>` — one pair per review, never the last pair still trading (the global
  switch is the monitor's). Every session exports `TS_OPERATOR_SESSION=1` and `TS_OPERATOR_REVIEW_ID`: with them
  `tune.py` takes the playbook only as `--text`, `propose.py` refuses `--body-file` and any base but `main`,
  `kill_switch.py` refuses `--all`, `review_pack.py --out` accepts only `data/reviews`, `demo_order_test.py` refuses to
  run, and every change is recorded as `operator-session:<review id>`.
- **Ledger and gauge:** sessions are ledger rows (role `review`/`diagnose`, pair NULL) outside the pairs' request quota;
  `UsageStore.tokens_since(since, pair=None, providers=None)` — the gauge counts only `claude_code` providers, a
  cache-read token as `ai.usage.cache_read_weight` (0.1) of a token (the budgets are in these weighted tokens, not the
  ledger's raw sums), with a 5-point step-down in the engines; it observes only until H21 (`ai.usage.enforce: false`).
- **Session size:** the live daily review was 27.3 k unique context + 7.4 k output (34.7 k, the ≤ 30 k target missed
  by 16 %; the ledger records 204 k input because every turn re-reads the context) — accepted (D-045); a diagnosis is
  likewise more than 8 k in the ledger.
- **Merge:** `instructions.md` v5 adds `$tp_hint`, which a running Phase 3 engine cannot fill (it re-reads the template
  from disk): apply Phase 4 with `stop_all.bat` → `git merge --ff-only` → `start_all.bat`, not merge-then-restart.
- **Notifier:** the Telegram values are read by `Notifier._telegram_creds` — `.env` first (re-read at most once a
  minute; an empty value = off), `os.environ` only when `.env` has no such line; the toast goes before Telegram; the rate
  limit is 20/hour per process for info and for warn each, critical exempt.
- **Playbook lint:** `$` is allowed (values are inserted verbatim; only templates need `$$`); the denylist is wider than
  the regex above (confidence 80–99 near "confidence", avoid/never … NO_TRADE, disregard, mixed Latin/Cyrillic/Greek
  words, line/paragraph separators).
- **`prompt_versions`:** `PRIMARY KEY (prompt_hash, library_hash)`, so an instructions-only edit is a new row.
- **Metrics:** an executed trade is scored only after the bar it closed in has closed and is stored; the backfill of
  older decisions runs 20 per pass (every 60 s).

### 3.9 Phase 5 spec — "goes deeper"

| # | Component | Files | Rule |
|---|---|---|---|
| 1 | Depth block | `analysis/snapshot.py:_depth`, `ai/model_view.py` | last `depth` snapshot ≤ 2 min old: per band (±0.1/0.25/0.5/1/2/5 %) bid/ask notional, imbalance `(bid−ask)/(bid+ask)`, 1-h change of the ±0.5 %/±1 % imbalance; ≤ 250 tokens; BTC/ETH only |
| 2 | OI fix + percentiles | `snapshot._derivatives` | `change_pct_1h/4h/24h` from `metrics.sum_open_interest`; 30-day percentile rank of OI and funding |
| 3 | Prev W/M levels | `analysis/context.py:reference_levels(daily=…)` | `pwh/pwl/pmh/pml/month_open/year_open` from the 1d frame (day roll respected); drawn on 1d/4h charts |
| 4 | BTC–ETH correlation | new `analysis/cross.py` | sibling hot DB read-only (`data/hot/binance_spot/<SIBLING>.db`): 96-bar 15m log-return corr + beta + the sibling's 15m trend; capability `cross_asset` |
| 5 | Session statistics | `context.py:session_stats` | last 30 d of 15m bars: per session mean range in ATR, up-close share, current session range vs mean — causal |
| 6 | Forming bar | `snapshot._analyze_tf` (15m/5m) + charts | `forming: [open_time,o,h,l,c,v,age_s]` from `Frame.forming`; hollow on the chart; never a trigger input |
| 7 | Versions | `PAYLOAD_VERSION "4"`, `VIEW_VERSION "3"`, legend v5 | token budget ≤ +1.5 k per call (measured) |
| 8 | Persistent session (measured, off) | `providers/claude_code.py:SessionRunner`, `ai.providers.claude_code.session_mode: per_call|persistent` | one `claude -p --input-format stream-json --output-format stream-json --verbose --session-id <per-pair uuid> --autocompact 100k --tools "" …` process held by the engine; one user line per event; daily restart with `--resume`; uuid in `engine_kv`; 24-h opt-in measurement in `docs/measurements/phase5_session.md` (tokens raw/cached, RAM 300–500 MB expected); default stays `per_call` |
| 9 | Bounded MCP tools | new `tools/mcp_server.py` (stdio; needs `mcp` pkg), provider args when `needs_detail` | as-of-bound tools `candles(tf, n≤300)`, `depth_bands()`, `decisions(n≤10)`; `--mcp-config … --strict-mcp-config --allowedTools "mcp__ts__*" --max-turns 3 --permission-mode dontAsk --permission-prompts none`, timeout 300 s; only when the previous answer set the new contract field `needs_detail` and ≤ 2/pair/day |
| 10 | News blackout (XAU) | `tools/mql5/CalendarExport.mq5` (user attaches), new `analysis/news.py`, gate check `news_blackout`, trigger suppression | the script writes `MQL5/Files/calendar.csv` (next 48 h, USD high impact); blackout ±15 min; payload `market.news`; stale file > 24 h → capability unavailable + warning, no blackout |
| 11 | Gold-proxy study P1.11 | `research/gold_flow/study.py` → `docs/exploration/gold_flow.md` | XAUUSDT perp delta vs XAUUSD@ returns, lead/lag; the user sets `flow_proxy_approved` only if \|corr\| ≥ 0.5 and sign-stable over 4 weeks |
| 12 | Profile tables | `snapshot._orderflow` | daily POC/VAH/VAL for the last 5 days (BTC/ETH real; XAU approx), ≤ 200 tokens |
| 13 | Go-live checklist inputs | `docs/go_live_checklist.md` | measured calls/day, tokens, RAM, gate approval share, action counts, monitor MTTD |

Tests: `test_snapshot_enrichments.py` (depth from a seeded table; OI from metrics; PW/PM causal; session stats causal;
forming bar excluded from triggers), `test_cross.py`, `test_news.py`, `test_session_runner.py` (framing, result parsing,
`--resume`, no orphan on cancel), `test_mcp_tools.py` (refuses future data), `test_model_view_v3.py`.
The ONE live call: a decision call with payload v4 + charts → token delta ≤ +1.5 k, valid. Persistent-session and MCP
measurements only if the user opts in (they replace, not add to, normal calls).
User actions: H22 `pip install mcp` (only for 9) · H23 attach `CalendarExport.mq5` in MT5 · H24 opt one pair into
`session_mode: persistent` for 24 h (optional) · H25 `flow_proxy_approved: true` only after the study · H26 merge + restart.
Risks: persistent-mode RAM on this laptop (opt-in only); MCP extra turns (+25–30 k tokens each, capped 2/day/pair); the news
file depends on the terminal (fail-open with a warning; never a silent trade during a known event when the file is fresh).

### 3.9.1 Phase 5 as approved (2026-09-28) — two checkpoints, the owner's decisions, the fold-ins

Approved by the user on 2026-09-28 after a five-analyst read-only review of production and the finished Phase 4 branch
(the review's verified facts are in §0b's 2026-09-28 entry). §3.9 stays the component spec; this section says what is
built, in which order, and what is deferred. Where the two differ, this section wins.

**The owner's decisions (D-046):**
- (a) No MetaEditor: the owner does not compile MQL5. Item 10 (news blackout for XAU) is built WITHOUT the MQL5 export:
  `analysis/news.py` reads a public weekly economic-calendar JSON feed that needs no key (verify the feed first —
  e.g. Forex Factory's "this week" JSON; if none is usable or its terms forbid it, item 10 becomes XAU-only prompt
  text and says so in the D entry); the XAU engine fetches it at most once an hour into `data/shared/news_calendar.json`
  (atomic write, redaction irrelevant — no secrets); the same semantics as §3.9: USD high impact, blackout ±15 min,
  payload `market.news`, gate check `news_blackout` fail-closed only while the file is fresh (≤ 24 h), stale →
  capability unavailable + one warning event, setup triggers suppressed inside the window; config
  `pairs.XAUUSD.news_blackout {enabled, minutes_before, minutes_after, impact, source_url, refresh_minutes}`.
  H23 is closed (nothing for the owner to compile).
- (b) Items 8 (persistent session) and 9 (MCP tools) are deferred: 330–470 MB free RAM against 300–500 MB per pair, an
  OAuth-refresh race on a long-lived process, no evidence of data-starved decisions, +25–30 k tokens per extra turn.
  Reserve the config key `ai.providers.claude_code.session_mode: per_call` (the only accepted value) with a design
  note (process lifecycle, one user line per event, per-turn result parsing, daily `--resume`, orphan kill, autocompact
  losing the persona); no `tools/mcp_server.py`, no `needs_detail` contract field — only a counter in the daily review
  pack of `data_quality_notes` that ask for more candles/depth/history, so the build decision can be made on evidence
  (build when ≥ 10 % of decisions ask). H22 and H24 → "not now".
- (c) Token budget: the new payload blocks are paid for by dropping the 1m timeframe from the model view (≈ 2.2 k
  chars); the per-decision budget stays net-neutral, measured by replay before the live call.
- (d) Capital: the account is and stays ≈ $100, on the live account too ("professional, normal trading — no craziness");
  the risk limits (1 % target / 3 % max per trade / 10 % daily incl. worst case / 4 % correlated / 3 open) are NOT
  raised; ideas whose 0.01-lot minimum risks more than 3 % (most BTC and XAU ideas at this equity) are refused by the
  gate by design and counted by class in the go-live checklist; the model manages what is open (D-043). Go-live starts
  with ONE pair (ETH sizes best at $100).
- (e) Subscription for the decision role (default, the owner may change it): the shared Max plan for now, with
  `ai.daily_calls_per_pair 30`, `min_minutes_between_calls 30`, `review_floor_minutes 20` in `config.local.yaml` (H30;
  the `monitor:` block goes in only after the H20 merge — Phase 3 code refuses unknown sections)
  and development sessions run right after a 5-hour reset (21:00 / 02:00 / 07:00 / 12:00 / 17:00 UTC); before go-live
  a second Claude account for production or an API key with hard USD caps (a D entry then).
- The ExpressVPN stays as it is: the owner wants it running; nothing in the code, the docs or the sessions asks to
  change, split or disable it. The monitor only learns its adapter name (`monitor.vpn_adapter_names`, H30) so the
  up/down flips are visible; the DNS/connect bursts (≈ 56 disconnects a day on BTC, all healed) are accepted.

**Delivery: one worktree `C:\the_claude_new_wt\phase5`, one branch `feat/phase5-goes-deeper` from `main` AFTER H20,
two ff-merge checkpoints, each with its own adversarial review (lenses → skeptic verifiers → fix → re-review → final
check), so the safety nets reach production while the demo runs.**

#### Checkpoint A — "ready and safe" (≈ 3 days, NO live Claude call) — H26a

| # | Item | Why |
|---|---|---|
| A1 | **PROJECT_STATUS.md reconciliation** (append, never delete). P1.13 ✅ (docs/data_availability.md exists; refresh its "P1.6 pending" line); P5.3 ✅ (remaining: charger + recorder task, H27/H28); P8.2 ✅ (adapters built; the Gemini live path untested until H4); P8.9 ✅ (production since 2026-09-26; the shared Max plan caused the two session-limit errors); P9.6 ✅ (delivered in P12.3, D-044); P9.7 ✅ (`settings.py` live guards + `mt5_backend.py` account check; live path unexercised by design until H9); P10.6 ✅ built, manual queue path never exercised (auto since D-036); P8.8 and P11.2 ⛔ obsolete (D-030/D-043); P11.1 ⛔ superseded by D-036 (virtual outcomes are the paper-equivalent measure); P1.12 🔄 with the facts (ran 2026-09-25 → 2026-09-27 06:06 UTC in 8 sessions, ≈ 21 h of MT5 ticks / 23 h of books, 1,142 parquet files, 29 MB, `data/research/price_matching/status.json`, dies on suspend, no autostart); P3.9/P4.6 ✅ superseded by production + one read-only `tools\check_integrity.py --since-hours 48` result pasted in; P5.2 next step rewritten (the four dead keys `duckdb_memory_mb`, `duckdb_threads`, `engine_cycle_in_subprocess`, `vision_download_concurrency`); P6.15 🔄 partial (call budget replayed in P12.3; trades/day and TF confirmation from Phase 4 `decision_metrics` after 2–4 weeks); P7.1 ⏳ after ≥ 72 h of recorder data, P7.2 ⏳👤 de-facto venue Windsor MT5, P7.4 ⏳ conditional; P9.9 → folded into P12.5 item 10; P1.11 → P12.5 item 11 (one output: docs/exploration/gold_flow.md; 290 days of XAUUSDT-perp × XAUUSD@ overlap already in cold parquet); P11.3 🔄 demo/auto since 2026-09-26 22:17 UTC with the numbers and the acceptance items; P11.4 ⏳👤 skeleton written in this checkpoint; P12.3 note (9) marked fixed in P12.4; H2 → settings ✅ + operational note (three critical-battery hibernates and two Start-menu shutdowns in 48 h, see §0b 2026-09-28); H4 with its consequence ("without it a Claude outage = skipped cycles, never a wrong trade"); H5 blocked by P7.1; H7 superseded by H13 except the recorder; H12 cleanup done 2026-09-26 with the remaining owner items (Defender offline scan, password rotation, `C:\ProgramData\KMSAuto`); H14 note (XAU running); H15 ✅; H17 cost note. | the "first ⏳ phase" rule must point at real work |
| A2 | **`tools/backup_state.py`**: SQLite backup API (or `VACUUM INTO`) for `data/instances/*/app.db` and `data/shared/ai_usage.db` (WAL-consistent), plus `account_peak.json`, `config/config.local.yaml`, `data/adaptive`, `data/reviews`, monitor/notify state, the CLI capability file → `backups/<UTC ts>.zip`, keep 14, never `.env` or stderr logs, `--verify` opens every copied DB and counts `ai_decisions`; a daily 03:30 UTC task in `install_operator_tasks.ps1` and a call from `restart_all.bat` before the stop; `docs/ops_windows.md` §9 "Backup and restore" + "Rebuild from zero" (Python 3.12 + MT5, clone from the remote, `requirements.lock`, restore, `.env` from the password manager, `claude auth login`, `install_autostart -DryRun`, `start_all`, `check_ops`) with a restore rehearsed on a scratch root and its time recorded. | no backup exists, no git remote (H29) |
| A3 | **Recorder**: a `TradingSystem-Recorder` keep-alive task (the pair tasks' 5-min pattern) in `install_autostart.ps1`, a recorder row in `tools/health_report.py`, a monitor rule "last flush > 30 min → warn". | P1.12 needs 72 h; it died at every suspend |
| A4 | **Monitor rules**: on battery > 5 min or battery < 30 % → warn, < 15 % → critical (`Win32_Battery`); commit charge > 85 % → warn; the recorder rule; a budget for diagnosis sessions (≤ 2/day, none while the gauge is at level ≥ 1) — needed on day one: the first monitor run after H20 (2026-09-27 22:20 UTC) started a billed Sonnet diagnosis for "low free RAM 147 MB" (ok, 114 s, clean guard, no action), so `monitor.diagnose_enabled: false` sits in config.local.yaml until this lands (the owner re-enables it then); the supervisor's `system_suspend` notifies on resume through `core/notify.py`; a test that the restart-loop rule fires on the 2026-09-26 XAU pattern (21 kills in an hour). `outage_warn_min` keeps its default; the owner sets 5 and the VPN adapter name in `config.local.yaml` (H30). | the 52-min hibernate was found by analysis, not by the monitor |
| A5 | **Engine/provider**: no trader call while the pair's execution market is closed unless a position is open (XAU's very first call, 2026-09-27 22:20 UTC on a Saturday, was a strong trigger from Friday's bars — the setup signature must still update so the Sunday reopen does not fire on that stale structure); a cycle interrupted by a suspend (awake-clock gap > 60 s inside the cycle) is stored as `interrupted`, not `error`, and does not raise the failure backoff; "Failed to refresh OAuth token" / "another Claude Code process is refreshing" / 403 "Request not allowed" are transient → one retry after 60 s without a failed cycle; a cancelled/deadline call writes a ledger row (tokens unknown) so daily counts match D-043; a session-aware quota reserve (≈ 40 % of `daily_calls_per_pair` kept for 12:00–21:00 UTC; config key, safe default); one prompt line (version bump) bounding `next_review.in_minutes` to ≥ 30 on NO_TRADE with no open position; optional if cheap: run two pairs' calls back-to-back when both trigger within 5 min so the second reads the cached prefix. | the cap binds by early afternoon; half the calls are short next_review reviews |
| A6 | **§3.9 item 2 now** (a production defect): OI `change_pct_1h/4h/24h` from `metrics.sum_open_interest` (last row ≤ 10 min old, changes vs the rows nearest as_of − 1 h / 4 h / 24 h; the 60-s table only for `last`); `oi_pct_rank_30d`, `funding_pct_rank_30d` (causal); positioning ratios = last NON-null value per column with its own timestamp (19 % of ETH metrics rows have a null taker ratio; both payloads show `taker_buy_sell_vol_ratio: null`). Fixture = real metrics rows with seeded gaps. | visible nulls in today's payloads |
| A7 | **Small fixes with tests**: "kill switch file present → a new BUY is refused with detail `kill switch engaged` AND a due breakeven rule / a model tighten-stop action is still applied"; Binance backfill workers exit when a pass is done and are respawned for the next; remove or wire the four dead resource knobs; apply `storage.min_free_disk_gb` to the MT5 backfill and let live writers skip the cold archive below 2 GB; check_ops/health_report hide the all-pairs block when per-pair systems run, print "n/a (no instrument)" for a venue row stopped by design, and show time-on-battery + last suspend; the engine logs one INFO line per hour while a market is closed; `PermissionError(13)` is its own disconnect reason in the health report; `cli_capabilities.json` moves to `data/shared`; `review_pack.py` caches the 10.8-s `running_supervisors` scan; `evaluate_virtual` reads are bounded and a decision executed-but-never-settled gets a metrics row; confirm the §0 `terminal_in_job` issue is fixed in main and record where. | S4, M4, S8, N5–N7 of the review |
| A8 | **Readiness deliverables**: `tools/demo_report.py` (a `demo` kind of `tools/review_pack.py` over 672 h: per-pair funnel screens → calls by role → valid → ideas → gate by check → placed → outcomes broker/virtual; `decision_metrics` means; MT5 deals history for the family's magics → equity curve, realised PnL, commission/swap; availability = share of 15m cycles lost to data_not_ready / ai_quota / system_suspend / killed / session limit; incident table with first-detection time from the monitor state; cost per day from the ledger; prompt/config/adaptive hashes; tuning changes; the sample-size statement) writing `docs/runs/demo.md`; `docs/go_live_checklist.md` with the thresholds below; `tools/go_live_inputs.py` printing the measured table (= §3.9 item 13); the weekly review prompt appends one paragraph "go-live evidence so far". | P11.3/P11.4, the owner's end state |
| A9 | One `tools\check_integrity.py --since-hours 48` run pasted into P3.9/P4.6. | closes two soak phases |

Then: full unit suite green from the worktree (`PYTHONPATH=src C:/the_claude_new/.venv/Scripts/python.exe -m pytest
tests/unit -q -p no:cacheprovider`; the 1208 Phase 4 tests stay green), the adversarial review, PROJECT_STATUS +
handoff §0b, and the checkpoint-A merge steps in Arabic: **H26a** = `git merge --ff-only <checkpoint-A commit>` +
`scripts\restart_all.bat` + the new tasks (`install_operator_tasks.bat` again for the backup task,
`install_autostart.bat` for the recorder task — the session states the exact commands).

#### Checkpoint B — "goes deeper" (≈ 6 days, exactly ONE billed live call) — H26b

| # | §3.9 | Item | Effort | Note |
|---|---|---|---|---|
| B1 | 1 | Depth block: last depth snapshot ≤ 120 s old else `data_quality: stale` and the capability downgraded (today `capabilities.real` claims depth while the payload has none); bands present only where the book reaches (BTC ±1 % partial, never ±2/±5 % — say so in legend v5); BTC/ETH only; ≤ 250 tokens; fixture = real rows exported from the hot DB | 0.5 d | |
| B2 | 3 | Prev W/M levels (`pwh/pwl/pmh/pml/month_open/year_open`) from the 1d frame with the pair's `day_roll`, completed periods only; the six names added to `charts.py`'s horizon filter (1d/4h charts) | 0.5 d | causal test |
| B3 | 6 | Forming bar for 15m/5m only, from `Frame.forming`, never in `recent`, never a trigger input — prove with `tools/replay_triggers.py` that the call count is unchanged; hollow bar on the 5m chart only (the 15m/1h chart caches stay keyed by closed bars) | 0.5 d | |
| B4 | 12 | Daily POC/VAH/VAL of the last 5 completed UTC days, computed once per day in a worker thread from aggTrades via footprint + value area, cached in `engine_kv`; XAU from the 1m tick-volume profile flagged approx; ≤ 200 tokens; never on the 5-min screen path | 1 d | |
| B5 | 5 | Session statistics: 30-day per-session mean range in ATR and up-close share cached per UTC day in `engine_kv`; current session vs mean from closed bars; ≤ 150 tokens | 1 d | causal + DST tests |
| B6 | 4 | `analysis/cross.py`: the sibling's hot DB opened `file:…?mode=ro` with a small cache, the Instrument built from `settings.pairs[sibling]` when a `correlated_groups` group names it and the file exists; 96-bar 15m log-return corr + beta + the sibling's trend; capability `cross_asset` real/unavailable; a missing sibling → unavailable without an exception; ≤ 100 ms | 0.75 d | last enrichment — decision value unproven |
| B7 | 7 | `PAYLOAD_VERSION "4"`, `VIEW_VERSION "3"`, legend v5; the 1m timeframe leaves the model view (decision c); budget net-neutral per decision, measured on every screen of a stored day with `tools/replay_triggers.py` — then **the ONE live call** (below) | 0.5 d | |
| B8 | 10 | News blackout for XAU per decision (a): feed-based `analysis/news.py`, `market.news`, gate `news_blackout`, trigger suppression, config `pairs.XAUUSD.news_blackout`; tests: fixture JSON, UTC/DST parsing, stale fails open, the gate refuses inside the window and the model is told why; if no feed is usable → XAU prompt text only, recorded | 1 d | |
| B9 | 11 | Gold-proxy study `research/gold_flow/study.py` → `docs/exploration/gold_flow.md`: one day-file at a time with pyarrow (≤ 500 MB RAM), run only while the laptop is idle and never on the trading path; XAUUSDT taker delta vs XAUUSD@ mid returns per 1-min/5-min bucket, corr at k = −5…+5 min, per week over the 290 overlapping days, rolling 4-week sign stability, weekends and the 21:00–22:00 UTC daily break excluded, Monday-gap check; pass/fail against |corr| ≥ 0.5 and 4-week sign stability → H25 only if it passes. In the same idle run, P7.1 `research/price_matching/analyze.py` over the recorder parquet if ≥ 72 h exist by then (spread distribution per session, basis median/p95, lead/lag at 100 ms / 1 s, stale-quote share, weekend behaviour → `docs/price_matching.md` with a proposed `max_basis_deviation_pct` from the measured p95, for H5) | 1.5 d | |
| B10 | 13 | Fill `tools/go_live_inputs.py` from the Phase 4 data now in production | 0.25 d | |

**Not built (recorded as decisions):** items 8 and 9 (decision b); the hollow forming bar on the 15m chart (cache
cost); P8.8/P11.2 mode comparisons (obsolete); P7.4 Binance backend (only if P7.2 = B).

**The ONE live call (B7):** `engine --once --pairs BTCUSDT` from the worktree on a scratch data root (hot/cold as
`mklink /J` junctions, a copy of BTC's app.db, own `shared\ai_usage.db`, stdout/stderr redirected to files, the engine
waits for the sign-in check), started right after a 5-hour reset. Acceptance: valid first attempt, 6 images, input
≤ 26.7 k and ≤ +1.5 k vs that day's production median, every new block present in the stored payload,
`snapshot_build_ms` < 3 s; numbers to `docs/measurements/phase5_live.md`; XAU verified by a dry payload build (no call).

**Tests (spec names):** `test_snapshot_enrichments.py` (depth from a seeded table of real rows incl. stale; OI from
metrics with gaps and null ratios; PW/PM causal with the broker day roll; session stats causal + DST; forming excluded
from triggers and `setup_signature` unchanged), `test_cross.py`, `test_news.py`, `test_model_view_v3.py`,
`test_prompt_versions` (bump), `test_go_live_inputs.py`, plus the checkpoint-A tests (backup verify/restore, monitor
battery/commit/recorder/diagnosis budget, interrupted cycle, transient retry, cancelled-call ledger row, quota
reserve, kill-switch protective, backfill worker exit).

**Review of checkpoint B** (lenses at least): payload/token budget, money path incl. the news gate, the research
jobs' resource use, config/ops/rollback, prompts vs code. Then PROJECT_STATUS.md (P12.5 block with numbers,
P1.11/P9.9/P11.4 cross-references, H22–H26 as they stand, D entries), handoff §0b + a "4.9 As built" note under §3.9
(deviations, live-call numbers, deferrals), `docs/ops_windows.md` §1b/§8 rollback switches for every new key, and the
merge/restart steps in Arabic (**H26b**) — stating whether the prompt/legend change requires `stop_all` → merge →
`start_all` (as Phase 4 did for its template) or a plain `restart_all`, and what to verify afterwards (§3.10 #4).

#### After Phase 5: the demo period and the go-live sequence

| When | What |
|---|---|
| Week 0 | The owner's steps (H20, H19, H18, H27–H30); the demo evaluation window is declared from the H20 date (+28 days). |
| Week 1 | Checkpoint A merged (H26a); the backup task live; the recorder reaches 72 h → P7.1 → H5 (`max_basis_deviation_pct` from the measured p95); first weekly review pack; H21 gauge calibration after the week. |
| Week 2 | Checkpoint B merged (H26b); XAU cycles observed; the gold-study verdict → H25 or not. |
| Weeks 2–4 | Demo continues on Phase 4+5 code; tuning only after ≥ 20 resolved outcomes per pair; no config changes except recorded ones; every incident goes into the demo report with its detection time. |
| Week 5 | `tools/demo_report.py` → `docs/runs/demo.md`; the checklist filled by `go_live_inputs.py`; the owner's D entries (pairs, subscription route, hardware); a live-refusal rehearsal on a scratch root (`EXECUTION_MODE=live` + the phrase against the demo terminal must fail "account mismatch"; a separate live terminal); a backup restored once; tests green on the exact commit. |
| Go-live (H9, the owner only) | ONE pair (ETH) at the minimum lot for two weeks, then add pairs; XAU excluded until item 10 and P1.11 pass. Production on a machine that is not the development laptop (a 16 GB upgrade or a small dedicated Windows box) removes sleep, battery, development RAM and shared-plan interference at once. |

**Go-live checklist thresholds** (the session writes them into `docs/go_live_checklist.md`, the owner signs):
≥ 28 demo days on Phase 4+ code; ≥ 30 resolved outcomes per live pair; availability ≥ 95 % of 15m cycles; 0 positions
ever without SL; no daily-loss or drawdown trip; every gate rejection in an expected class; calls/day ≤ cap with ≤ 1
subscription-limit error per week; monitor MTTD ≤ 15 min proven by one drill; median `snapshot_build_ms` ≤ 3 s; free
RAM ≥ 1 GB; 0 sleep/shutdown events in the last 14 days; a backup restored once; P7.2 decided; tests green on the live
commit; the sample-size statement acknowledged (four weeks show the absence of catastrophic behaviour and the execution
quality, not a statistical edge).

#### Kickoff text for the implementing session (Opus 5.5)

Prerequisites the owner completes first: H20 verified (the "phase 4:" line, `logs\monitor.jsonl`, `decision_metrics`
rows), H19, H18 (recommended), H27–H30, and this docs branch merged. Start right after a 5-hour reset.

```
Continue from docs/handoff_operator_v2.md: read §0b (newest entries: 2026-09-28 Phase 5 plan approved; 2026-09-27
Phase 4 DONE and Phase 3 live), §1, §2, §3.6 (ground rules), §3.8 "4.8 As built", §3.9 (the Phase 5 component spec)
and §3.9.1 (the approved plan: two checkpoints, the owner's decisions, the fold-ins, the tracker reconciliation list,
the live-call acceptance), §3.10 and §5; then PROJECT_STATUS.md (overview, H1–H31, D-043–D-046, P12.3/P12.4 notes,
P12.5), docs/measurements/phase3_live.md and phase4_live.md, docs/ops_windows.md §1b and §8, docs/monitoring.md,
docs/learning_loop.md, docs/notifications.md.

Verify first (read-only): `git log -1 main` descends from 301f60c (Phase 4 merged) and scripts\status_all.bat shows
the three per-pair systems up on Phase 4 code; if not, stop and tell me — Phase 5 branches from main AFTER H20.

Implement Phase 5 (P12.5) on worktree C:\the_claude_new_wt\phase5, branch feat/phase5-goes-deeper from main, exactly
as §3.9.1 specifies: checkpoint A "ready and safe" first (A1–A9, no live Claude call) → full unit suite green →
independent adversarial review (lenses → skeptic verifiers → fix → re-review → final check) → PROJECT_STATUS.md and
handoff §0b → the checkpoint-A merge steps in Arabic (H26a). After I confirm that merge: checkpoint B "goes deeper"
(B1–B10) with exactly ONE billed live call on a scratch data root as §3.9.1 defines → the same review cycle → the docs
(P12.5, §0b, "4.9 As built", ops_windows rollback switches) → the merge/restart steps in Arabic (H26b: state whether
stop_all → merge → start_all is required).

Hard rules: never stop, restart or migrate production from the worktree; never edit .env, config.local.yaml or
scheduled tasks (write the lines as H rows for me); never send MT5 orders; never call MetaTrader5 from the worktree
except read-only and briefly; SQLite reads on production files with uri mode=ro only; additive DB changes only, new
behaviour behind config keys with safe defaults; fail closed on money paths; exactly one billed live call for the
whole phase; keep tool runs light (this laptop has < 0.5 GB free RAM and production runs on it — no parallel heavy
subagents, research jobs one day-file at a time); do not touch the ExpressVPN configuration or ask me to.
```

### 3.10 Verification per phase (end-to-end)

1. Unit suite green from the worktree (`-p no:cacheprovider`).
2. The single live call per phase on the scratch data root; numbers written to `docs/measurements/`.
3. Independent adversarial review of the branch (Phase 1–2 pattern: lenses → verifiers → fix → re-review).
4. After the user's merge + `restart_all.bat`: `scripts\status_all.bat`, `tools\health_report.py --hours 1`, one real
   cycle per pair observed in the dashboard (charts thumbnails, position actions, management rows), Telegram message
   received (Phase 4), `tools\monitor.py` exit code, review pack produced by the scheduled task (Phase 4).
5. Phase 3 specifically: watch the open BTC position — breakeven/trailing rows appear in `position_actions` when the
   declared triggers hit; `kill_switch_on.bat BTCUSDT` blocks new orders but a protective SL move still applies.

## 4. Phases, acceptance tests, effort

| Phase | Scope | Acceptance | Effort |
|---|---|---|---|
| 0 (user) | H12 malware removal, H13 autostart, H4 Gemini key, restart system + recorder | `scripts\check_ops.bat` all ok; `tools/health_report.py` clean; free RAM > 2 GB | user |
| 1 | §3.2 items 1–3 + operator_notes (§3.1) + usage extras persisted | 334+ tests pass; one live BTC call: SL ≥ gate minimum, `operator_notes` stored and fed back; input tokens/call ≤ 19 k | hours–1 day |
| 2 | §3.3 instances (BTCUSDT first; ETHUSDT second) | two instances run 24 h without cross-kills; per-instance gate proven by test; account floor test; migration script rehearsed on a copy | 2–3 days |
| 3 | §3.7 "sees and manages": charts + stream-json images, prompt-cache fix, 5-min screening, 40/day cap, per-role models + escalation, P9.6 position management + Claude position actions + event triggers, prompts v6, gate on the placed leg | all tests + the listed new ones; ONE live call valid with 6 images ≤ 23 k input tokens ≤ 120 s; 24-h payload replay ≤ 40 calls/pair/day; no rule ever loosens an SL (property test); engine RSS +≤ 60 MB | 5–7 days |
| 4 | §3.8 "watches and learns": decision metrics, adaptive overlay + playbooks + tune.py, review packs, Task-Scheduler operator sessions (Opus), pure-Python monitor, Telegram/toast notifier, usage gauge, dashboard tabs | tune.py bounds/direction/cooldown/min-samples/freeze tests; monitor detects a simulated stale heartbeat and writes only the pair's switch on a burst; ONE daily review ≤ 30 k tokens with a clean diff guard | 5–6 days |
| 5 | §3.9 "goes deeper": depth block, OI fix, prev W/M levels, BTC–ETH correlation, session stats, forming bar (payload v4), persistent session (opt-in, measured), bounded MCP tools, news blackout (XAU), gold-proxy study, profile tables, go-live inputs | ONE live call: token delta ≤ +1.5 k; opt-in measurements written to docs/measurements/ | 4–6 days + measurements |

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
