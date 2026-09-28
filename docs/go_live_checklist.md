# Go-live checklist (D-046, amended by D-047)

The owner decides go-live (H9) on this checklist **as it stands** at the end of the demo window. Nothing here trades
or changes a setting: `tools/go_live_inputs.py` measures what the files can prove, the owner ticks the rest, signs,
and decides.

```
.venv\Scripts\python.exe tools\go_live_inputs.py                          the measured table (pass/FAIL/n.a., value, n)
.venv\Scripts\python.exe tools\go_live_inputs.py --json                   the same as JSON
.venv\Scripts\python.exe tools\demo_report.py --out data\reviews\demo.md  the full evidence (or --print)
```

In the production checkout never use `demo_report.py`'s default output `docs\runs\demo.md`: a file under `docs\`
makes the checkout dirty and can block the next `git merge --ff-only` (docs/runs/README.md); the tool refuses that
default there (exit 3).

Both tools read the window `evaluation.demo_start_utc` + `evaluation.demo_days` (config.yaml: 2026-09-27T21:50:00Z,
the H20 restart, + 5 days → 2026-10-02T21:50Z), cut at now while it runs; `--since/--until` override it,
`--root <checkout>` reads another checkout's data and logs (a worktree reading production), everything read-only.
`go_live_inputs.py` always exits 0: it informs. The weekly review pack carries the same table and the weekly review
ends its summary with one paragraph "Go-live evidence so far".

**D-047 (2026-09-28):** the demo window is 5 days, not 28 — the owner's choice. Five days cannot reach 30 resolved
outcomes per pair, so the outcomes are REPORTED, not required, and the sample-size statement below applies with
more force. H18 (Telegram) and the rest of H12 are deferred by the owner; notifications are the log line and the
Windows toast.

## The checklist

`auto` = `go_live_inputs.py` judges it; `owner` = the owner ticks (the tool shows whatever evidence exists).

| # | Item | Threshold | Measured by | How it is measured / what the owner does | Tick |
|---|---|---|---|---|---|
| 1 | The demo days on Phase 4+ code complete | 5 days, window ended | auto | the window from the config; the git sha of the window's decisions is listed (the Phase 4 merge is 301f60c) | [ ] |
| 2 | Resolved outcomes per pair | **reported, not required** | auto (n.a.) | settled broker outcomes (won / lost / USD) and resolved virtual outcomes per pair, with n | [ ] |
| 3 | Availability of the 15-min cycles | ≥ 95 % of the market-open cycles, every pair | auto | `demo_report.py` availability: a market-open 15-min cycle (XAU's weekend and daily break and the crypto CFDs' Saturday 05–08 UTC maintenance excluded) is lost to a stopped system, a suspend, a killed/exited service, `data_not_ready`, a stale-data skip, `ai_not_ready`, `ai_quota`, the session limit, an interrupted or failed AI call, a `cycle_error`, or no 5-min screen in the engine log; the first cycle after a reopen (its decision bar lies in closed time: the engine waits for its first bar by design) is never lost | [ ] |
| 4 | Positions ever without SL | 0 | auto | monitor "position without SL" findings; placed ideas without a passing `stop_loss_present` gate check; open positions without SL now | [ ] |
| 5 | Daily-loss or drawdown trip | none | auto | `account_peak.json` trip, the executor's drawdown flag, monitor daily-loss / drawdown / critical equity-drop findings, gate refusals `daily_loss_limit` / `account_drawdown` | [ ] |
| 6 | Every gate rejection in an expected class | no class outside the list below | auto | every failed check of every rejected idea of the window, by class | [ ] |
| 7 | Calls/day and the subscription limit | every pair ≤ its daily cap every UTC day; ≤ 1 subscription-limit event in the window | auto | the shared ledger (claude_code rows of the pair, operator sessions excluded); the cap from the engine status (`quota_per_day`); limit rows within 10 min are one event | [ ] |
| 8 | Monitor MTTD | ≤ 15 min, proven by one drill | owner | the drill below; the tool shows the monitor runs, the largest gap between them and the detection delay of every finding that names its onset | [ ] |
| 9 | Median `snapshot_build_ms` | ≤ 3 s | auto | the engine status of each pair (median of its last builds) and no "slow snapshot build" finding in the window | [ ] |
| 10 | Free RAM | ≥ 1 GB now and no "Low free RAM" finding in the window | auto | `psutil` now; the monitor's findings (lowest value seen) | [ ] |
| 11 | Sleep / shutdown events in the window | 0 | auto | `system_suspend` events of every system; a supervisor start after a run that ended without a stop (a shutdown, restart, logoff or crash); every shutdown, boot and sleep in the Windows System log (User32 1074, Kernel-Power 41/42/107, Kernel-General 12/13, Kernel-Boot 27; read-only, best effort — an unread log is said, not counted); the last boot time. The boot time alone does not catch every shutdown: Fast Startup is on this laptop, and a Start-menu shutdown keeps it (2026-09-27). Supervisor shutdowns (stop_all / restart_all) are listed, not counted | [ ] |
| 12 | A backup restored once | one restore | owner (evidence) | docs/ops_windows.md §9; pass when `logs/backup.jsonl` records a restore, else the owner ticks (a rehearsal on a scratch root from a worktree logs there) | [ ] |
| 13 | Risk limits unchanged | 1 % target / 3 % max per trade / 10 % daily incl. worst case / 4 % correlated / 3 open (D-036) | auto (config) | `risk:` as loaded; the demo equity now is shown | [ ] |
| 14 | P7.2 decided or explicitly deferred | a D entry | owner | PROJECT_STATUS P7.2 (de facto venue: Windsor MT5) | [ ] |
| 15 | Tests green on the live commit | the full unit suite | owner | `.venv\Scripts\python.exe -m pytest tests/unit -q -p no:cacheprovider` on the exact commit that goes live, run from a worktree of that commit or with production stopped — never beside the running systems (the suite needs RAM this laptop does not have while they run); or cite the lead's recorded green run of that sha | [ ] |
| 16 | The live-refusal rehearsal | fails with "account mismatch" | owner | below | [ ] |
| 17 | The sample-size statement | signed knowingly | owner | below | [ ] |
| 18 | Go-live scope | ONE pair (ETHUSDT) at the minimum lot, capital ≈ $100, risk limits unchanged (D-046 d) | owner | the owner's live setup; everything else stays demo | [ ] |
| 19 | The decision role separated from development | a second Claude account or an API key with hard USD caps (D-046 e), or a D entry deferring it | owner | a D entry in PROJECT_STATUS.md (on the shared plan a development session's limit hit also stops the live pair's calls, its management calls included) | [ ] |

**Open point, not a gate:** D-046 says production moves to a machine that is not the development laptop "before
go-live", while the approved sequence (docs/handoff_operator_v2.md §3.9.1) puts that move under "Later". The owner
reconciles the two in a D entry.

### Expected gate rejection classes (item 6)

At ≈ $100 the gate refuses most ideas by design (D-046 d); these classes are the gate doing its job:

| Class | Why it is expected |
|---|---|
| `position_size_min_lot` | the 0.01-lot minimum risks more than `max_risk_per_trade_pct` (3 %) — most BTC and XAU ideas at this equity |
| `rr_after_costs` | reward-to-risk after spread and commission below `risk.min_rr` |
| `sl_min_distance`, `sl_max_distance` | the stop outside its ATR / broker bounds |
| `correlated_exposure` | the BTC+ETH group above `risk.max_correlated_risk_pct` (4 %) |
| `spread_vs_sl` | the spread too large a share of the stop distance |
| `confidence` | below the confidence floor (`risk.min_confidence` or the adaptive floor) |
| `kill_switch` | a kill switch was on (the owner or the monitor) |
| `max_open_positions`, `no_same_direction` | the position limits |
| `market_open`, `not_expired`, `recommendation_age`, `market_in_zone`, `pending_price_valid` | the idea no longer fits the market or the clock at the gate |
| `effective_leverage` | the leverage cap |
| `daily_loss_worst_case` | realised + open SL risk + this trade would pass the daily limit (a protective refusal, not a trip) |

**Not expected — each one is explained before go-live:** `stop_loss_present` (an idea without SL reached the gate),
`sl_side`, `is_trade`, `quote_fresh` (a stale feed at the gate), `basis` (analysis and execution prices apart),
`daily_loss_limit` and `account_drawdown` (trips — item 5), `position_size_other` (sized to zero for another reason
than the minimum lot), `not_placed: …` (the gate passed and the broker — or the paper book — refused the order: its
reason follows), and `not_gated: …` (a rejection without a gate record: no quote, the pair disabled, an exception).

### The monitor drill (item 8)

A harmless induced warning, timed by the owner:

1. Keep `monitor.diagnose_enabled: false` in `config\config.local.yaml` for the drill (otherwise the warning may start
   one billed diagnosis session within the day's budget).
2. In the `monitor:` block of `config\config.local.yaml`, change the EXISTING line `free_disk_warn_gb: 8` (H30) to
   `free_disk_warn_gb: 9999`, save, and note the time T0 (UTC). Never add a second `free_disk_warn_gb` line: a
   repeated key makes every config load fail — the monitor stops checking and no service can start or restart until
   it is fixed. Check the edit at once with `.venv\Scripts\python.exe -m tradingsystem config` (it must validate).
   The monitor reads the config on every run (every 15 min, `TradingSystemOps-Monitor`); the running systems do not
   use this key.
3. Wait for the "Low free disk" warning (toast, `logs\monitor.jsonl`). Its first detection T1 is in
   `data\shared\monitor_state.json` (`alerts.disk.first_ms`) and in the demo report's incident table.
4. Set the same line back to `free_disk_warn_gb: 8` — never delete it (the default, 5 GB, would apply) — and check
   again with `.venv\Scripts\python.exe -m tradingsystem config`; the next run clears the warning. MTTD = T1 − T0
   must be ≤ 15 min; write both times next to the tick.

### The live-refusal rehearsal (item 16)

Proves that live mode refuses the demo terminal. Run it on a **scratch data root**, never on production, only while
the owner watches, and only the executor:

1. A scratch folder, e.g. `C:\ts_rehearsal`, and a copy of `config\config.yaml` there with `paths.data_dir` /
   `paths.logs_dir` pointing into it, `execution.live_confirmation:` set to the exact phrase of
   `core/settings.py` (`LIVE_CONFIRMATION_PHRASE`), and `mt5.profiles.live.terminal_path` set to the **demo**
   terminal (`C:/Program Files/MetaTrader 5/terminal64.exe`).
2. In a new console: `set TRADINGSYSTEM_CONFIG=C:\ts_rehearsal\config.yaml`, `set EXECUTION_MODE=live`, then
   `.venv\Scripts\python.exe -m tradingsystem executor`.
3. Expected: the executor refuses to start with an account mismatch ("terminal is on 'WindsorBrokers1-Demo', expected
   'WindsorBrokers1-Real1'" / "account mismatch … refusing to trade"); no order line, ever. Stop it (Ctrl+C) if it
   has not exited, close the console (the two variables die with it), delete the scratch folder.
4. Any other outcome — above all an executor that starts — is a stop for go-live.

### The sample-size statement (item 17)

> Five days show the absence of catastrophic behaviour and the execution quality, not a statistical edge. Every rate
> in the demo report comes with its n, and no n there is large enough to estimate an edge. I decide go-live knowing
> this.

Signed: ______________________ Date (UTC): ______________

## After the decision

Go-live (H9) is ONE pair, ETHUSDT (it sizes best at $100), at the minimum lot, on a live account of ≈ $100 with the
risk limits of item 13 unchanged; BTCUSDT and XAUUSD stay on demo. More pairs only on the weekly review's evidence;
XAU stays excluded until the news blackout and the gold-proxy study pass (Phase 5 checkpoint B). Record the decision
as a D entry in PROJECT_STATUS.md with this checklist's measured table pasted in.
