# The monitor: `tools/monitor.py` (Phase 4, §3.8)

A small pure-Python watchdog for production. It needs no Claude, no desktop app and no network of its own: every
15 minutes it reads what the running systems already write, notifies you of problems (log line, Windows toast,
Telegram — docs/ops_windows.md §8) and, for a few rules, engages a **kill switch**. It never restarts anything and
never turns a switch off.

It replaces the Claude desktop-app 3-hourly check (retired with H19).

---

## 1. When it runs

| How | Command | Output |
|---|---|---|
| Task Scheduler, every 15 min (H19) | task `TradingSystemOps-Monitor` → `pythonw tools\monitor.py --quiet` | none; the exit code is the task's *Last Result* |
| By hand | `scripts\monitor.bat` | the findings, then `pause` (`/nopause` skips it) |
| By hand, safe | `scripts\monitor.bat --dry-run` | the findings only: no switch, no notification, no state file, no diagnosis |

Other flags: `--json` (the result as JSON), `--no-diagnose` (never start a Claude diagnosis from this run),
`--instance PAIR`, `--all-pairs-system`.

| Exit code | Meaning |
|---|---|
| **0** | nothing at warning level or above |
| **1** | at least one warning or critical finding (also when it was already notified earlier), or the state file could not be written |
| **3** | the config could not be read (e.g. a bad `config\config.local.yaml`): **nothing was checked**. One timestamped line goes to `logs\monitor-config-error.log` (a fixed path: without a config there is no logs setting; rotated once at 1 MB), a critical toast *Monitor cannot read its config* is shown (skipped under `TS_NOTIFY_DISABLE`), and the error is printed unless `--quiet`. Fix the config; the next run picks it up |

The monitor logs to `logs\monitor.jsonl` (never to the console, so `pythonw` is fine). It honours
`TRADINGSYSTEM_CONFIG` like every tool, so a scratch data root is monitored as a whole. `monitor.enabled: false`
turns it into a no-op (exit 0).

## 2. Which systems

The same choice as `tools\health_report.py`: the all-pairs system when it runs; otherwise every pair whose supervisor
runs plus every pair that should run (its own `data\instances\<PAIR>\app.db`, not stopped by you). A pair that should
run but has no supervisor is a warning (`System not running`) only when two runs at least 5 min apart see it —
always, not only while the machine settles (§4): after any logon (right after a boot, hours after an overnight update
restart, a logoff/logon) the keep-alive tasks start the supervisors 90-150 s late, and the monitor cannot tell when you
logged on. A system that is really down is therefore reported one run (15 min) later. Every other health-report note
is a warning at once (`System check`). A system stopped by you (`run\manual_stop`, no live supervisor) is skipped. A
pair whose supervisor runs but whose `app.db` this monitor cannot find is a warning (`monitor cannot see this system`):
the task runs from another checkout or data root.

Everything is read **read-only**: each system's `app.db` (heartbeats, events, quotes), its `run\supervisor.json`,
`data\shared\account_peak.json` and `data\reviews\`.

## 3. The rules

Thresholds are in the `monitor:` block of `config/config.yaml` (defaults shown).

| Check | Rule | Level | Action |
|---|---|---|---|
| Stale heartbeat | a `collector_status` row not `stopped` without a beat for > `stale_heartbeat_min` (15) — sleep-corrected, see §4 | warn | — |
| MT5 IPC hung | `executor` or `mt5` in `reconnecting`/`error` with a last error matching `IPC` / `-10004` / `-10005` for ≥ `ipc_hung_min` (5) — the start is the executor's `failing_since` or the first run that saw it | critical | none: restarting the MT5 terminal stays **your** decision |
| Position without SL | an executor `exposure` row of kind `position` where **any** leg has no SL (`sl_missing`; `sl` is the first leg's, for display) | critical (reminded hourly) | — |
| Order burst | more than `order_burst_per_hour` (3) `order` events of one pair in the last hour | critical | **that pair's** `data\instances\<PAIR>\KILL_SWITCH` |
| Daily loss near the limit | today ≤ −(`risk.max_daily_loss_pct` − `daily_loss_warn_margin_pct`) % (−8 % with 10/2); with no band (margin ≥ limit, e.g. the code defaults 2/2) at 80 % of the limit | warn | — |
| Daily loss limit | today ≤ −`risk.max_daily_loss_pct` % | critical | the pair's switch (all-pairs system: the global one), **once per UTC day** |
| Equity drop | equity fell > `equity_drop_warn_pct` (5) % since the account's previous sample (the baseline, carried up to 7 days) | warn | — |
| Equity drop, large | … > `equity_drop_kill_pct` (10) % of an identified MT5 account (`mt5:…`) against a baseline at most 1 h old, not counting the time the machine slept or was off since that sample (a reboot: everything from the sample to the boot) | critical | the **global** `data\KILL_SWITCH` |
| Equity drop, large, not comparable | the same drop of a **paper** account, of an unidentified one (`mt5:<mode>:?`, the executor row had no `account_drawdown`) or against an older baseline: more than 1 h **awake** without a sample — the monitor did not run, or its runs found no equity in the executor row (an MT5 outage, the executor loop failing, a logon long after a reboot) | warn | none: the global switch stops every system |
| Drawdown stop tripped | `account_peak.json` (or the executor row) shows the account's stop tripped | critical, **once** per trip | — (the executors already refuse) |
| Stale quote | `latest_quote` older than `quote_stale_min` (10) while the instrument's market was open through that whole window | warn | — |
| Low RAM / disk | free RAM < `free_ram_warn_mb` (300), free disk of the data drive < `free_disk_warn_gb` (5) | warn | — |
| Restart loop | more than `restart_loop_per_hour` (3) `exited`/`killed` events of one service in the last hour | warn | — |
| Outage | a `disconnect` without a later `resumed` (MT5) / `connect` (Binance) for > `outage_warn_min` (10) | warn | — |
| VPN | an adapter in `vpn_adapter_names` went up or down since the last run (a missing adapter is down) | info | — |
| Daily review overdue | no `data\reviews\*_daily.md` newer than `review_overdue_hours` (30); before the first one, counted from the monitor's first run; skipped with `operator.enabled: false` | warn | — |
| Slow snapshot build | the engine's `snapshot_build_ms` (the median of its recent builds, else the last) above `snapshot_build_warn_ms` (3000) on **two runs in a row** | warn | — |
| A broken check | an exception inside one check (the others still run) | warn | — |

Equity is compared **per account**, not per pair: the three pair systems share one MT5 account and report the same
equity, which is therefore counted once.

## 4. Sleep, reboot and clock jumps

After a sleep every wall-clock heartbeat looks stale by the time asleep, and `StartWhenAvailable` runs the missed
monitor task right after the resume. So:

* the age of a beat written before the supervisor's last recorded **suspend** (`supervisor.json` `last_gap`, or the
  `system_suspend` event) is reduced by the time asleep;
* a system is **settling** while the machine booted, resumed or had a clock jump / stall within the stale window, the
  machine slept since the previous monitor run (wall time minus awake time > 60 s), or the machine slept since the
  supervisor's last beat and that beat is recent on the awake clock. While settling, a stale heartbeat, stale quote
  or outage must be seen on **two consecutive runs at least 5 min apart**; until then it is listed as `waiting`. A
  pair with no supervisor (§2) always must;
* the equity baseline (§3) does not age while the machine sleeps or is off: every run that carries it without a newer
  sample adds the time slept since the previous run, and after a reboot everything from the sample to the boot counts.

Without any of that, a stale heartbeat is reported on the first run that sees it.

## 5. Notifications and deduplication

Each finding has a stable key (e.g. `stale:BTCUSDT:engine`, `burst:BTCUSDT:BTCUSDT:<ts>`) and goes to
`core.notify` as `monitor:<key>` with title `Monitor: …`. The monitor keeps its own memory of what it sent
(`alerts` in the state file):

* a new key, or a key whose level rose, is sent;
* a finding that persists is sent again as a reminder after 6 h (warn) or 1 h (critical); info never repeats;
* one-shot findings (a drawdown trip, a daily-loss limit after its switch) are sent once;
* a finding that disappears is remembered for a **cool-down** — 6 h for a warning, 2 h for info and critical,
  counted from when it was last seen. If it comes back within it, it persists: not sent again (unless its level rose
  or its reminder is due) and not new, so it starts no diagnosis — a condition flapping across its threshold (free RAM
  near the line) is one alert, not one every 30 min. After the cool-down it is a new episode and sent again.

Criticals are queued first. The run waits for the notifier — 15 s plus 12 s per queued notification, at most 240 s —
and, before it exits, once more (15 s); delivery is judged after that last wait. A notification that reached **no**
sink — every sink in use still pending when the last wait ended, or failed — is kept in `pending_notify` and
**re-sent** by the next runs: the same level, title and text (plus the time it was detected) under the key
`monitor:<key>:retry<n>`, since the failed attempt already claimed `monitor:<key>` in the notifier's 30-min dedupe.
Only the notification is owed; the event itself is handled once, whatever the sinks did: its alert counts as
notified, a one-shot finding is recorded, a burst stays counted (a switch you turned off is never engaged again for
it), the equity baseline and the VPN state move on, and a re-send is not a new finding (it starts no diagnosis). A
notification gets at most **3 attempts** (the first and 2 re-sends) and is dropped with a log line after that, or when
it is older than its level's reminder interval (6 h warn, 1 h critical; info has none) — the normal reminder takes
over — or when its key is sent afresh (its level rose, its reminder was due). One sink delivering is enough (the toast
shown, Telegram timed out); a sink that is off or not configured does not count, and a notification that is a log
line only by configuration (`notify.enabled: false`, below `notify.min_level`, `TS_NOTIFY_DISABLE`), deduped or
rate-limited counts as handled.

## 6. Kill switches

The monitor only **engages** switches (`core/killswitch.py`, content `{ts, actor: "monitor", reason, scope}`); an
existing switch is left as it is. To go on after reviewing, run `scripts\kill_switch_off.bat [PAIR]` as usual.

* An order burst engages the pair's switch once per burst: after you turn it off, the orders already counted never
  engage it again; only more new orders than the limit do.
* The daily-loss limit engages the switch once per UTC day: turned off, it stays off for the rest of that day.
* An equity drop compares a sample with the account's previous one, so each drop engages the global switch once — only
  for an identified MT5 account and a recent baseline (at most 1 h old, not counting the time the machine slept or was
  off since that sample); a paper account, an unidentified account or an older baseline (over 1 h awake without a
  sample) only warns.
* Open positions keep their SL/TP at the broker; protective management goes on while a switch is on.

## 7. Diagnosis sessions

When a run sends at least one new warning or critical finding, it starts **one** Claude diagnosis session
(`tools\operator\run_session.py --kind diagnose --reason "<the new findings>"`, detached, with a hidden console of its
own, outside the task's job — see docs/operator_sessions.md), unless:

* `monitor.diagnose_enabled: false`, or `operator.enabled: false`,
* the environment variable `TS_MONITOR_NO_DIAGNOSE` is set (anything but `0`) — set it for a scratch or test run,
* the previous diagnosis started less than `diagnose_every_hours` (3) ago,
* the run was `--dry-run` or `--no-diagnose`,
* the state file cannot be written (`state not persisted`): `last_diagnose_ms` is saved **before** the session
  starts, so a state that cannot be saved never starts a session on every run,
* an operator session (the daily or weekly review, another diagnosis) holds `data\shared\locks\operator_session.lock`
  (`a review session is running`): the diagnosis would end `busy` at once. It stays **wanted** (`diagnose_wanted`):
  the next runs start it for those findings while they persist, although they are no longer new, for up to 2 h, and
  the 3-hour interval is not used up.

A finding that is already known (no reminder due) never starts a session, nor does the re-send of an undelivered
notification (§5). The session gets the new findings as its `--reason` and can read all of the run's findings in
`data\shared\monitor_state.json` (`run.findings`).

## 8. The state file

`data\shared\monitor_state.json` (the monitor's only file besides kill switches; replaced atomically, retried while a
reader holds it). Delete it to start over — the only cost is one run without the between-runs comparisons. When it
cannot be written, the run exits 1 and sends a critical *Monitor: state file not saved* (key `monitor:state_error`,
so the notifier's dedupe sends it at most every 30 min): until it is fixed, every run re-sends its newer findings.

| Key | What |
|---|---|
| `first_run_ms` | the monitor's first run (daily-review rule before the first review) |
| `run` | last run: `ts`, awake clock, boot time, exit code, `findings`, `held` |
| `alerts` | per finding key: level, first/last seen, last notified; `cleared_ms` = gone, kept for its cool-down (§5) |
| `pending_notify` | per finding key: a notification that reached no sink — level, title, text, pair, when it was first detected, attempts so far — re-sent by the next runs (§5) |
| `once`, `acted` | one-shot findings sent; daily-loss switches already engaged (by key) |
| `equity` | per account: the baseline (`equity`, `ts` of the sample), the boot of the run that last took or carried it (`boot_ms`) and, once carried, the time slept or off since the sample (`slept_ms`) |
| `burst_seen` | per system and pair: the newest order already counted in a burst |
| `ipc` | per system and collector: since when the IPC error is seen |
| `seen` | per system and kind: first sighting of each stale item (the two-run rule) |
| `slow_build` | per system: the slow snapshot build seen on the previous run |
| `vpn` | last state of each watched adapter |
| `last_diagnose_ms`, `diagnose` | the last diagnosis started (pid, findings) |
| `diagnose_wanted` | a diagnosis put off by a running operator session (or a failed start): since when, for which findings (§7) |

## 9. In the health report

`tools\health_report.py` shows the same signals for a person or a review session:

* the engine line: `snapshot_build median of N … ms (last …, max …)`, and a `!! snapshot build …` line when the median
  (else the last build) is above `monitor.snapshot_build_warn_ms`;
* `adaptive overlay <PAIR>: adaptive <hash> playbook <hash>` (and `AI paused until …`) while an overlay is in force;
* `!! kill switch ON (<scope>): <file> — <ts> <actor> <reason>` for every switch the system obeys;
* `usage gauge: level N — 7 d … M tokens (… % of the weekly budget), 5 h …` (`!!` at level 2, or level 1 when
  `ai.usage.enforce` is on).

## 10. False positives

Every threshold is a `monitor:` key (override it in `config\config.local.yaml`, in one `monitor:` block). A mistake
there (an unknown key, `equity_drop_warn_pct` not below `equity_drop_kill_pct`, a second `monitor:` block) stops the
monitor: every run then exits **3**, writes `logs\monitor-config-error.log` and shows a critical toast (§1) — run
`scripts\monitor.bat --dry-run` after an edit to see it at once. A holiday
closes a market the session calendar still counts as open: expect a stale-quote warning then. A switch engaged by
mistake costs missed trades, never a loss: check the reason in the file (or the health report) and run
`kill_switch_off.bat`.
