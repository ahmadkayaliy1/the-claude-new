# Windows operations runbook (P5.3, H2, H7)

This runbook covers keeping the trading system running on the Windows laptop. It has two parts:

- **What you do yourself.** Windows power, network, time and autostart settings. Nothing in the code changes
  these settings.
- **What the software does for you.** The supervisor in `run all` handles sleep, clock jumps, crashes and the
  MT5 terminal on its own.

Every command below goes into a terminal opened in the project folder (`C:\the_claude_new`). Commands marked
*(admin)* need an elevated terminal ("Run as administrator"). Try the others in a normal terminal first.

**Quick check at any time:** double-click `scripts\check_ops.bat`. It is read-only and lists every setting
from this page with `ok` or `FIX`, plus the command that fixes it.

---

## 0. Checklist

| # | What | Where | Status on 2026-09-26 |
|---|---|---|---|
| 1 | Lid, sleep button and power button must never put the laptop to sleep (AC **and** battery) | §2.1 | all three = Sleep → **FIX** |
| 2 | Sleep / hibernate after idle = Never | §2.2 | already 0 (never) |
| 3 | Keep the charger plugged in; ideally put the charger and router on a UPS | §2.3 | — |
| 4 | Wi-Fi power saving = Maximum Performance (AC and battery); adapter may not be turned off | §3 | battery = Medium → **FIX** |
| 5 | Clock synchronised (Windows Time service, hourly sync) | §4 | not synchronized → **FIX** |
| 6 | MT5 terminal: saved login, settings below | §5 | — |
| 7 | Autostart installed: `scripts\install_autostart.bat` | §6 | not installed |
| 8 | Windows Update active hours set | §6.3 | — |

---

## 1. Daily use

Double-click a script in `scripts\`. Each one waits for a key press at the end so you can read the result.

| Script | What it does |
|---|---|
| `start.bat` | Starts every service and the dashboard **in the background**, in a hidden console window. You can close any window afterwards. Also resumes autostart after a `stop.bat`. |
| `stop.bat` | Graceful stop: each service flushes its data and exits (up to ~90 s, then it is killed). Also **pauses autostart** until you run `start.bat` again. |
| `status.bat` | Supervisor pid and heartbeat, each service (pid, restarts), the MT5 terminal, each collector's heartbeat, and the dashboard URL. |
| `restart.bat` | `stop.bat`, then `start.bat`. |
| `check_ops.bat` | Read-only check of every Windows setting on this page. |

The same actions from a terminal:

```bat
.venv\Scripts\python.exe -m tradingsystem run all --detach   &rem start in the background
.venv\Scripts\python.exe -m tradingsystem run --stop
.venv\Scripts\python.exe -m tradingsystem run --status
.venv\Scripts\python.exe -m tradingsystem run all            &rem foreground: Ctrl+C stops everything
```

- **Foreground mode dies with its window.** If you close the window, log off or shut down, every service
  stops at once with no flush. On 2026-09-25 at 22:07 UTC all services ended with exit code `0x40010004`
  because their console session ended. For unattended running, use `start.bat` or the autostart.
- **Start it from Explorer or the autostart.** `start.bat` (double-clicked in Explorer) and the autostart
  task give the supervisor its own hidden console, outside the window that started it. Starting it from inside
  an IDE or agent terminal can tie it to that session, and it may stop when the session ends.
- **Only one supervisor can run per data folder.** A second `start.bat` or an autostart run just reports
  "already running". A supervisor from before this change, which has no lock, is recognised by its command
  line: the new one refuses to start next to it. **Run `stop.bat` once** to end it; every version honours the
  `data\STOP_ALL` file that `stop.bat` writes.
- **Dashboard:** http://127.0.0.1:8765. The collector row **supervisor** is the supervisor's own heartbeat.
  If it turns "stale", the supervisor is hung or not running.

---

## 1a. One system per pair (D-042)

Each pair can run as its own, fully independent system: its own supervisor and services, its own
`data\instances\<PAIR>\app.db` (decisions, memory, paper account, heartbeats), its own logs in `logs\<PAIR>\`,
its own dashboard port and its own MT5 magic number. So the daily loss limit (10 %), the open-trade limit and the
same-direction guard apply per pair. Market data (`data\hot`, `data\cold`) stays shared.

| Pair | Dashboard | MT5 magic |
|---|---|---|
| BTCUSDT | http://127.0.0.1:8766 | base + 1 |
| ETHUSDT | http://127.0.0.1:8767 | base + 2 |
| XAUUSD | http://127.0.0.1:8768 | base + 3 |

(`instances:` in `config\config.yaml`; the all-pairs system keeps port 8765 and the base magic.)

**Switching once** from the all-pairs system: double-click `scripts\switch_to_pairs.bat`. It stops every
system, gives every pair its own app.db (a copy of `data\app.db` with that pair's history, `tools\migrate_instance.py`),
replaces the autostart task `TradingSystem` with one task per pair, and starts every pair. With pairs named
(`switch_to_pairs.bat BTCUSDT`) only those get a task and start; every pair still gets its history, so another one
is added later with `start.bat ETHUSDT` and `install_autostart.ps1 -Pair BTCUSDT,ETHUSDT`. Each pair's system uses
about as much memory as the all-pairs system (≈0.9 GB on this laptop): start with one pair when RAM is short.
`data\app.db` is not
changed, so going back is `stop_all.bat`, then
`powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_autostart.ps1 -AllPairsSystem`, then `start.bat`.
A pair's system refuses to start while it has no app.db of its own but `data\app.db` exists (it would start with an
empty history): use `switch_to_pairs.bat` or `tools\migrate_instance.py <PAIR>` first.

| Script | What it does |
|---|---|
| `start.bat BTCUSDT` / `stop.bat BTCUSDT` / `status.bat BTCUSDT` / `restart.bat BTCUSDT` | The same as without a pair, for that pair's system only. |
| `start_all.bat` / `stop_all.bat` / `status_all.bat` / `restart_all.bat` | Every pair in `instances:` (stop_all also stops the all-pairs system). |
| `kill_switch_on.bat` / `kill_switch_off.bat` | Every system (`data\KILL_SWITCH`). With a pair: that pair only (`data\instances\<PAIR>\KILL_SWITCH`), in its own system and in the all-pairs system; an unknown pair name changes nothing and says so. |
| `reset_drawdown_stop.bat` | Re-arms trading after the account-wide drawdown stop (below). |

- **The two layouts never run together.** A pair's system refuses to start while the all-pairs system runs, and
  the all-pairs system refuses while any pair runs. Pairs run next to each other. `stop.bat` without a pair says so
  (and exits with an error) when only per-pair systems are running: use `stop_all.bat` or `stop.bat <PAIR>`.
- **Shared between the pairs:** the MT5 terminal (one of them starts it if it is missing; history downloads take
  turns), the Claude sign-in (CLI starts are at least 15 s apart on the whole machine) and the AI request caps
  (`rpd` counts every pair; each pair may make at most `ai.daily_calls_per_pair` = 40 calls a day, every ledger
  row counted — first attempts, repairs and escalations; Phase 3, D-043). The usage is in `data\shared\ai_usage.db`.
- **Account-wide drawdown stop (25 %, `risk.account_drawdown_stop_pct`).** Every system records the account's
  equity peak in `data\shared\account_peak.json`. When the equity falls 25 % below that peak, every system stops
  opening trades (open positions keep their stops) until you run `scripts\reset_drawdown_stop.bat`, even if the
  equity recovers. Run it also after a withdrawal, which otherwise looks like a loss.
- **The BTC/ETH correlated limit counts both systems.** Each system sees the other pairs' positions on the same
  account when it checks the 4 % correlated-risk limit.
- **Positions opened by the all-pairs system** (before the switch) are taken over by the pair's system: it
  manages, counts and settles them like its own.
- **Orders on the MT5 account are placed one system at a time** (a machine-wide lock from reading the account to the
  broker listing the new order), so two pairs never pass the correlated-risk limit on the same account snapshot.

---

## 1b. What Claude sees and does (Phase 3, D-043)

- **Charts.** Every call carries six candle charts (1w, 1d, 4h, 1h, 15m, 5m; 720×400) with the levels, zones,
  liquidity, structure and your live trades drawn on them. The latest set is in `data\instances\<PAIR>\charts\<PAIR>\`
  and in the dashboard (Decisions → a decision → "Charts rendered for the model"). A text-only fallback provider
  (Gemini) gets no charts and is told so.
- **Called only when something changed.** Python screens every 5-minute close; Claude is called on a new setup, one
  of its own review conditions, an executor event (fill, target or stop hit, closed position, outcome, the result of
  its own actions) or the 2-hour idle review — at most **40 calls per pair per day** (`ai.daily_calls_per_pair`;
  repairs and confirmations count too).
- **Trades are managed.** The system executes the management plan Claude declares with a trade (breakeven after the
  first target with a spread buffer, trailing, partial close when the size allows, time stop), and on later calls
  Claude may tighten a stop, take profit, adjust a target or cancel a pending order. It can never widen or remove a
  stop or add size; every action is checked first and listed in the dashboard ("Actions applied").
- **The kill switch blocks new orders only.** Protective actions (tightening a stop, closing, cancelling) keep running
  while a kill switch is on — they reduce risk. The system's own management rules wait while the market is closed
  (gold's daily break, weekends) or the terminal refuses (Algo Trading off, no connection) and go through when trading
  resumes; a tighter stop of Claude's that is already waiting keeps waiting while the market is closed. A NEW action of
  Claude's (close, cancel, stop or target change) sent while the market is closed, or refused by the terminal, is
  refused — Claude is told on its next call and decides again.
- **Models per role** (`ai.models` in `config\config.yaml`; override in `config\config.local.yaml`): the decisions use
  the provider's model (Sonnet); `escalation.enabled: true` makes Opus (or `models.escalation.model: fable`) confirm or
  downgrade strong setups before they can be executed.

Rollback switches in `config\config.local.yaml` (then `scripts\restart_all.bat`), no code change:

| Problem | Switch |
|---|---|
| Charts use too much RAM or time | `ai:` → `charts: {enabled: false}` (every pair) |
| … only for some pairs (e.g. charts for BTC only) | `instances:` → `ETHUSDT: {overrides: {ai: {charts: {enabled: false}}}}` and `XAUUSD: {overrides: {ai: {charts: {enabled: false}}}}` (one line each under `instances:`) |
| Trade management misbehaves | `execution:` → `management: {dry_run: true}` (records what it would do) or `enabled: false` |
| Claude's own actions on live trades misbehave | `execution:` → `position_actions: {enabled: false}` |
| Too many calls | `ai:` → `daily_calls_per_pair: 25` |

**Each section once.** Several switches of one section go into ONE block — a second `execution:` or `ai:` line would
replace the first, so the system now refuses to start with "duplicate key" instead:

```yaml
execution:
  management: {dry_run: true}
  position_actions: {enabled: false}
ai:
  charts: {enabled: false}
  daily_calls_per_pair: 25
```

(The optional Opus confirmation of strong setups, H17, is not a rollback: `ai:` → `escalation: {enabled: true}` goes
into the same `ai:` block when you want it.)

Check what is in force with `.venv\Scripts\python.exe -m tradingsystem config` (the "phase 3:" line; it also checks
every pair's `overrides`) and for one pair with `.venv\Scripts\python.exe -m tradingsystem --instance XAUUSD config`,
before `restart_all.bat`. A pair's `overrides` win over the same setting in `.env`.

**Going back to the code before Phase 3** (a `git` checkout of an older commit): first remove every Phase 3 key from
`config\config.local.yaml` (`ai.charts`, `ai.models`, `ai.escalation`, `ai.daily_calls_per_pair`,
`ai.event_calls_per_day`, `ai.screen_*`, `ai.weak_*`, `ai.liquidity_atr`, `execution.management`,
`execution.position_actions`, `instances.*.overrides`) — the older code refuses unknown keys and no service would
start. The databases need nothing: the older code ignores the new tables and columns.

---

## 2. Never let the laptop sleep (H2)

**What happened on 2026-09-25.** The laptop was on battery. At 11:28:59 UTC the lid was closed and Windows went
into S3 sleep (Kernel-Power 42, "Button or Lid"). Every process froze for 41 minutes, until 12:09:56 (clock
delta 2 456 893 ms). A second, short sleep followed at 12:10:16. Market data from that window cannot be
recovered. On resume, the old supervisor also killed healthy services because their heartbeats looked 41
minutes old. That second problem is fixed in the code (§2.4). The sleep itself can only be prevented by the
settings below.

### 2.1 Lid, sleep button and power button

Run these as your own user; add *(admin)* only if `powercfg` says access is denied. `0` means Do nothing and
`4` means Turn off the display:

```bat
powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0
powercfg /setdcvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0
powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS SBUTTONACTION 0
powercfg /setdcvalueindex SCHEME_CURRENT SUB_BUTTONS SBUTTONACTION 0
powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS PBUTTONACTION 4
powercfg /setdcvalueindex SCHEME_CURRENT SUB_BUTTONS PBUTTONACTION 4
powercfg /setactive SCHEME_CURRENT
```

- **Battery (DC) values are required.** The 41-minute sleep happened on battery.
- **Verify with `/qh`, not `/q`:** `powercfg /qh SCHEME_CURRENT SUB_BUTTONS`. These settings are hidden, and
  `/q` prints nothing for them on this laptop. Expect `Current AC/DC Power Setting Index: 0x00000000` for
  LIDACTION and SBUTTONACTION, and `0x00000004` for PBUTTONACTION. `scripts\check_ops.bat` shows the same.
- **The values belong to one power plan.** If you switch plans (Balanced, Best performance, …), run the
  commands again.
- **Optional:** show "Lid close action" in Control Panel → Power Options:
  `powercfg -attributes SUB_BUTTONS LIDACTION -ATTRIB_HIDE`.
- **Holding the power button for 4+ seconds still forces the laptop off.** That is hardware and is fine.
- **Never close the running laptop into a bag.** With the lid set to Do nothing it keeps running and heats up.

### 2.2 Idle sleep and hibernate

These are already `0` (never) on this laptop. Re-apply them after a plan change:

```bat
powercfg /change standby-timeout-ac 0
powercfg /change standby-timeout-dc 0
powercfg /change hibernate-timeout-ac 0
powercfg /change hibernate-timeout-dc 0
```

- **Turning the display off is fine.** `powercfg /change monitor-timeout-ac 10` saves power.
- **While it runs, the supervisor also blocks idle sleep** through `SetThreadExecutionState`
  (`supervisor.keep_awake: true`). That does **not** cover the lid, the buttons or a critical battery; only
  §2.1 and §2.3 do.
- **See what is keeping the PC awake:** `powercfg /requests` *(admin)*.
- **See which sleep states the laptop supports:** `powercfg /a`. This laptop uses S3 (classic sleep). If it
  ever reports "Standby (S0 Low Power Idle)" (Modern Standby) instead, the settings above still apply.

### 2.3 Power supply

- **Keep the charger connected while the system runs.** The ~25 Wh battery lasts about an hour. At the
  critical level Windows **hibernates**, and that action (`BATACTIONCRIT = 2`) should stay as it is because it
  protects the data. `check_ops` shows it.
- **Put the charger and the router/modem on a UPS if you can.** On 2026-09-25, AC power dropped for a few
  seconds several times (11:06, 12:21 and 12:49 UTC, Kernel-Power 105), and the network degraded after 11:08.

### 2.4 What the software does about sleep and clock jumps

- **It judges services on awake time, not the wall clock.** The supervisor measures heartbeats on the Windows
  *unbiased interrupt time*, which stops while the PC sleeps. A heartbeat counts as stale only when it has
  stopped *changing* for `stale_s` (90–120 s) of awake time. Wall-clock steps never count.
- **It notices when its own loop was interrupted.** Around each 5 s loop sleep, the supervisor compares the
  awake clock, the tick count and the wall clock. From that it detects:
  - a **suspend**: tick count − awake time > 5 s
  - a **clock jump**: wall clock − tick count > 10 s
  - a **stall**: its own loop took more than 20 s of awake time
- **After any of these it waits before judging again.** It writes an `ingestion_events` row (`system_suspend`,
  `clock_jump` or `stall`, with the duration), resets every heartbeat baseline and makes **no stale kills for
  180 s**. That gives the network and MT5 time to reconnect. A service that is still frozen after that grace
  is restarted as usual. The last gap also appears in `status.bat` and in `data\run\supervisor.json`.

---

## 3. Network (Wi-Fi)

On resume after the sleep, the Intel Wireless-AC 9461 reset, and DNS failed for about 5 minutes.

```bat
powercfg /setacvalueindex SCHEME_CURRENT 19cbb8fa-5279-450e-9fac-8a3d5fedd0c1 12bbebe6-58d6-4636-95bb-3217ef867c1a 0
powercfg /setdcvalueindex SCHEME_CURRENT 19cbb8fa-5279-450e-9fac-8a3d5fedd0c1 12bbebe6-58d6-4636-95bb-3217ef867c1a 0
powercfg /setactive SCHEME_CURRENT
```

This sets Wireless Adapter Settings → Power Saving Mode to Maximum Performance. Battery is currently set to
Medium Power Saving.

Next, turn off the adapter's own power management:

1. Open Device Manager.
2. Expand Network adapters → Intel(R) Wireless-AC 9461 → Properties.
3. On the **Power Management** tab, untick **"Allow the computer to turn off this device to save power"**.

To check it *(admin)*: `Get-NetAdapterPowerManagement -Name "Wi-Fi"`, then look at
`AllowComputerToTurnOffDevice`.

- **An Ethernet cable avoids all of this** if one is available.
- **A VPN client (ExpressVPN is installed) can add a reconnect delay after resume.** Disconnect it while
  recording, unless you need it.

---

## 4. Time synchronisation (H7)

Every timestamp in the system is UTC milliseconds, and latency and price matching compare our clock with
Binance's and MT5's. Right now `w32tm /query /status` reports **Leap Indicator 3 (not synchronized)**. The
source is `time.windows.com,0x9` and the last sync was 2026-09-25 19:17 local.

```bat
w32tm /query /status
w32tm /stripchart /computer:time.windows.com /samples:3 /dataonly
```

The first command shows the status; the second shows the current offset, which should be within ±0.05 s.

**Fix** *(admin)*:

```bat
sc config w32time start= auto
net start w32time
w32tm /config /manualpeerlist:"time.windows.com,0x9 time.google.com,0x9" /syncfromflags:manual /update
reg add HKLM\SYSTEM\CurrentControlSet\Services\W32Time\TimeProviders\NtpClient /v SpecialPollInterval /t REG_DWORD /d 3600 /f
w32tm /config /update
w32tm /resync /force
```

- **These commands do four things.** They keep the service running, use two time servers, sync every hour
  (`0x9` = client mode plus the special interval) and sync now.
- **Also check Settings → Time & language → Date & time.** "Set time automatically" should be **On**; then
  click "Sync now".
- **The supervisor tolerates clock steps** (§2.4). Frequent large jumps point to a failing CMOS battery or a
  wrong time zone. Check the time zone setting, even though the system itself only uses UTC.

---

## 5. MT5 terminal

- **Our processes never start the terminal.** If `mt5.initialize(path)` finds the terminal closed, it starts
  the terminal as a child of the calling service. A watchdog kill or a supervisor crash would then close it.
  So, in order of preference, the terminal is started by:
  1. the **TradingSystem-MT5** task at logon (§6);
  2. otherwise the supervisor: before it starts `ingest-mt5`, and whenever the terminal is missing (checked
     every 15 s, at most one start per 60 s). It uses that task if it is registered, otherwise a short-lived
     `cmd /c start`, so the terminal is never inside our job or process tree;
  3. never a service.
- **The supervisor protects a terminal that a service started anyway.** Tree kills always spare
  `terminal64.exe`, and at shutdown the supervisor releases it instead of closing it. `status.bat` then shows
  `inside the supervisor job`. Close MT5 once at a quiet moment; the supervisor restarts it outside within
  ~15 s.
- **Closing MT5 on purpose** (for example to edit `common.ini`, as in D-023): run `stop.bat` first, or the
  supervisor starts it again. To turn that off permanently, set `supervisor.manage_mt5_terminal: false` in
  `config\config.local.yaml`.
- **Terminal settings:**
  - **Login:** log into the demo account once with "Save password", so the terminal logs in by itself after a
    restart.
  - **Tools → Options → Server:** turn off "Enable news". This is optional and saves CPU and RAM.
  - **Tools → Options → Charts:** set Max bars in chart to Unlimited (already done, D-023). Keep as few charts
    open as possible; the terminal uses ~400 MB.
  - **Tools → Options → Expert Advisors:** "Allow algorithmic trading" is needed only for demo/live execution
    (H6). Paper mode needs none.
- **Terminal copies:** never run two copies of the same `terminal64.exe`. The live account uses its own
  portable copy (`C:/MT5_Live`, plan item 7).
- **LiveUpdate:** an MT5 update restarts the terminal by itself. The ingest service reconnects, and the
  watchdog restarts it if an MT5 call hangs.

---

## 6. Autostart (Task Scheduler)

### 6.1 Install

Double-click `scripts\install_autostart.bat`, or run:

```bat
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_autostart.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_autostart.ps1 -DryRun
```

The `-DryRun` form shows what would be registered and changes nothing. One system per pair (§1a):
add `-AllPairs` (every pair in `instances:`) or `-Pair BTCUSDT,ETHUSDT`; that registers `TradingSystem-<PAIR>`
tasks instead of `TradingSystem` (their logon delays 30 s apart) and removes the other layout's task.
`switch_to_pairs.bat` does this for you. Without an option the script keeps the layout in use (a plain double-click
after the switch re-registers the per-pair tasks); `-AllPairsSystem` goes back to the single task. The script creates two tasks for
your account. Both run only while you are logged on (MT5 needs your desktop), not elevated, on battery too, at
normal priority, and as soon as possible after a missed run:

| Task | Trigger | Action |
|---|---|---|
| `TradingSystem-MT5` | at logon | starts the MT5 terminal. No time limit; the Windows default of 72 h would close it. |
| `TradingSystem` | 90 s after logon, then every 5 min | `pythonw -m tradingsystem run all --detach --auto`: starts the supervisor (hidden) if it is not running, and replaces it if its heartbeat has been dead for 5 min of awake time. Otherwise it does nothing and returns within seconds. |

- **If it says "Access is denied",** run it from an elevated PowerShell. The tasks still run as you, not
  elevated.
- **Verify:** `scripts\check_ops.bat`, or `taskschd.msc` → Task Scheduler Library → `TradingSystem*`.
- **Remove:** `scripts\uninstall_autostart.bat` (removes every `TradingSystem*` task, per-pair ones too). Stop
  the system first with `stop.bat` (or `stop_all.bat`).

### 6.2 Stop, start and autostart

- **`stop.bat` also pauses the autostart.** It writes `data\run\manual_stop`, so the 5-minute keep-alive
  leaves the system down, even across reboots, until you run `start.bat`.
- **A crash or hang is fixed automatically.** A crashed supervisor, or one hung for 5 minutes, is replaced by
  the keep-alive within 5 minutes.
- **After a reboot, nothing starts until you log in** (MT5 needs an interactive session). Automatic sign-in
  (`netplwiz`) is possible, but it weakens the laptop's security. Your choice.

### 6.3 Windows Update

Windows Update restarts can happen while the system runs. Set **Settings → Windows Update → Advanced options →
Active hours** to cover your trading hours. During a recording window (H2), use "Pause updates". After a
restart, the autostart brings everything back once you sign in.

---

## 7. Where to look when something is wrong

| Where | What |
|---|---|
| `scripts\status.bat` | Overall state. `NO HEARTBEAT` means the supervisor is hung. `NOT running` next to MT5 means the terminal is missing. Per pair: `status.bat <PAIR>` or `status_all.bat`. |
| `tools\health_report.py` | Health and performance of the last hours (every pair's system by default; `--instance <PAIR>`). |
| `data\run\supervisor.json` | Rewritten every 5 s: pid, heartbeat (awake clock), services, MT5 terminals, last sleep or clock jump. A pair's system: `data\instances\<PAIR>\run\supervisor.json`, and its logs are in `logs\<PAIR>\`. |
| `logs\supervisor.jsonl` | Starts, exits (with exit code and the last lines of output), watchdog kills, sleep/clock-jump detection, MT5 terminal starts. |
| `logs\supervisor-ctl.jsonl` | `start.bat`, `stop.bat` and autostart actions. |
| `logs\<service>.jsonl` | The service's own log. An uncaught exception appears as `uncaught <Type>` with the full traceback. |
| `logs\<service>.stderr.log` | Raw stdout/stderr of the service: errors before logging started, native crash dumps (faulthandler), library warnings. Rolled to `.1`/`.2` at a restart above 5 MB. **Not redacted**, so do not share it publicly. |
| Dashboard → events | `supervisor:all` system_suspend / clock_jump / stall; `supervisor:<service>` started / exited / killed / heartbeat_unreadable; `supervisor:mt5` terminal_started / terminal_in_job. |

**Exit codes in `exited` events:**

| Code | Meaning |
|---|---|
| `1` | Python error; see `<service>.jsonl`. |
| `0xC0000005` | Access violation (native crash, often inside the MT5 DLL); see `<service>.stderr.log`. |
| `0x40010004` | The console session ended (window closed, logoff or shutdown). |

A watchdog restart appears as a `killed` event (with the reason), not as `exited`.

**If a heartbeat cannot be read** (app.db locked or missing, or a missing row), the supervisor does not kill
anything. It logs a warning, and after about 30 s it writes a `heartbeat_unreadable` event. A broken app.db
therefore never causes a restart loop.

**Config:** the `supervisor:` block in `config\config.yaml`:

| Key | Meaning |
|---|---|
| `keep_awake` | Block idle sleep while running. |
| `manage_mt5_terminal` | Start a missing terminal outside our processes. |
| `mt5_task` | Name of the MT5 autostart task. |
| `child_log_max_bytes` / `child_log_backups` | Size and number of the `<service>.stderr.log` files. |
