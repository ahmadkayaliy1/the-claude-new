# Operator sessions — Claude reviews the system (Phase 4, §3.8 component 3)

Claude looks at the running system on a schedule, with read-only tools and a few exact commands. It can tune only
the bounded, expiring per-pair overlay (`tools/tune.py`, see `docs/learning_loop.md`); everything else becomes a
proposal the owner decides on. The pure-Python monitor (`docs/monitoring.md`) watches every 15 minutes and starts a
short diagnosis session when it finds a warning.

| kind | when | model (config) | turns / time limit | pack window |
|---|---|---|---|---|
| `daily` | 04:30 UTC, Task Scheduler | `ai.models.review` (opus / high) | `operator.daily_max_turns` 30 / `daily_timeout_min` 20 | `operator.daily_pack_hours` 24 h |
| `weekly` | Sunday 06:00 UTC | `ai.models.review` | `weekly_max_turns` 40 / `weekly_timeout_min` 40 | `weekly_pack_hours` 168 h |
| `diagnose` | started by `tools/monitor.py` (at most every `monitor.diagnose_every_hours`) | `ai.models.monitor` (sonnet / low) | `diagnose_max_turns` 12 / `diagnose_timeout_min` 15 | 6 h, pack ≤ 16 k characters |

**Cost.** The spec's targets (a daily review ≤ 30 k tokens, a diagnosis ≤ 8 k) are single-call sizes; they cannot
hold for a session under the ledger convention (§2 step 7: input + cache reads + cache writes, summed over every turn
— each turn re-sends the whole context plus the tool output so far). A diagnosis' first request is `_system.md`
(8.4 k characters) + the prompt on stdin (`diagnose.md` ≈ 2.1 k + the pack, capped at 16 k; a dry run on a copy of
production data, 3 pairs, 6 h: 12.5 k characters with a 10.2 k pack) ≈ 21-27 k characters ≈ 6-7 k tokens of text,
plus the CLI's own tool definitions — so 8 k is at best that first request. An n-turn diagnosis records at least n
times it (an estimate for 4 turns: 30-50 k). The live daily review (10 turns) recorded 204 k input for a 27.3 k-token
context (`docs/measurements/phase4_live.md`, D-045). The levers are `operator.*_max_turns` and the pack caps
(`PACK_MAX_CHARS` in `session_args.py`).

## 1. Install the scheduled tasks (H19)

From `C:\the_claude_new` after the merge (the tasks point into the checkout the script lives in):

```
scripts\install_operator_tasks.bat -DryRun      look first
scripts\install_operator_tasks.bat              register
scripts\install_operator_tasks.bat -Uninstall   remove the three tasks again
```

* `TradingSystemOps-Monitor` — every 15 min: `pythonw tools\monitor.py --quiet` (limit 10 min);
* `TradingSystemOps-ReviewDaily` — 04:30 UTC: `powershell -File tools\operator\run_session.ps1 -Kind daily` (limit 130 min);
* `TradingSystemOps-ReviewWeekly` — Sunday 06:00 UTC: `… -Kind weekly` (limit 190 min).

**Time limits.** The session runner ends a review itself at `operator.daily_timeout_min` / `weekly_timeout_min`
(config; 20 / 40 by default, see §2 step 5). The tasks' `ExecutionTimeLimit` is only a backstop for a hung runner:
130 / 190 min = the largest value the settings accept (`OperatorCfg`: 120 / 180) + 10 min. So the config's deadline
always governs, and raising `*_timeout_min` (within its bounds) needs no re-install. Task Scheduler's limit ends the
task's whole job — PowerShell, Python and the CLI — before the runner can write its session file, ledger row or
notification, which is why it must never be the one that fires (pinned by
`test_review_task_limits_are_a_backstop_beyond_every_config_value`). `-DailyLimitMinutes` / `-WeeklyLimitMinutes`
override the backstop; a value below 130 / 190 is reported as a `WARNING` with the largest `*_timeout_min` it leaves
room for. A diagnosis is not a task: the monitor starts it outside its own job, and `diagnose_timeout_min` alone
limits it.

Same principal and settings as `install_autostart.ps1`: your account, "run only when user is logged on" (the Claude
CLI sign-in lives in your user profile), not elevated, on battery too, a missed run starts as soon as possible, a
running one is never started twice. The review times are UTC: the triggers' `StartBoundary` ends in `Z`
(synchronized across time zones), so daylight saving time does not move them; the installer reads it back and says
`verified`. The names are **not** `TradingSystem-*`: `install_autostart.ps1` treats every such task (except
`TradingSystem-MT5`) as a per-pair keep-alive and deletes it when the layout changes (pinned by
`test_install_autostart_never_matches_the_operator_task_names`). `uninstall_autostart.bat` does not remove them; use
`-Uninstall` here. Retire the desktop app's 3-hourly monitor task yourself once these run.

## 2. What one session does (`tools/operator/run_session.py`)

`run_session.ps1` is only a launcher (PowerShell 5.1 drops empty arguments, pipes stdin as ASCII and decodes native
output with the OEM code page); the work is Python:

1. **One at a time.** `data/shared/locks/operator_session.lock`: a review waits up to 5 min for a running diagnosis;
   a diagnosis never waits (status `busy`). `operator.enabled: false` → nothing runs (`disabled`).
2. **The review pack** (`tools/review_pack.py`, below) → `data/reviews/<ts>_<kind>.md|.json`. With the usage gauge
   enforcing (`ai.usage.enforce`) at level 2, daily/weekly reviews are skipped (`gauge_paused`); a diagnosis runs.
3. **The prompt** = `tools/operator/prompts/<kind>.md` (the checklist) + the whole pack markdown, written to
   `<ts>_<kind>.prompt.md` and given to the CLI on stdin from that file — the pack costs no Read turn. The system
   prompt is `prompts/_system.md` (role, evidence rules, the exact commands, tune.py's policy, the summary format).
4. **Sign-in check**: `claude auth status` after the machine-wide 15 s CLI start stagger (`claude_code.claim_start`,
   shared with the engines — two CLIs refreshing the OAuth token together lose the race); only a subscription
   sign-in is accepted (D-030), else `not_signed_in` and nothing is spent. Skipped when a long-lived
   `CLAUDE_CODE_OAUTH_TOKEN` is configured (as the provider does).
5. **The session**: `claude -p` (command line below), working directory = the checkout, stdout/stderr to files under
   `data/reviews/`, a deadline that ends the whole run `KILL_MARGIN_S` (90 s) before `operator.<kind>_timeout_min`
   — on the deadline the process **tree** is killed (the model's tool children too), status `timeout`. This deadline
   is the one that governs; the scheduled task's limit lies beyond every value the config accepts (§1).
6. **The result**: `success` → `ok`; `error_max_turns` → `max_turns` — both are a normal end (the second has no
   summary). Errors are classified from the error text only (`not_signed_in`, `usage_limit`, `sign_in_transient`,
   `other`) — never from a successful answer that mentions a "usage limit".
7. **The ledger**: one row in the shared AI ledger (`data/shared/ai_usage.db` in the per-pair layout) when
   `operator.record_usage`: provider `claude_code`, purpose `operator_<kind>`, role `review` / `diagnose`, pair NULL,
   input = input + cache reads + cache writes (the project's convention; the larger of `usage` and the per-model
   sums), `cost_usd` 0 and the API-equivalent price in `api_equivalent_usd`. The row counts toward the provider's
   `rpd` (every row of the provider does) but toward no pair's daily cap, and toward the usage gauge.
8. **The diff guard**: `git status --porcelain --untracked-files=all` of the checkout (no optional locks) plus a
   content hash of every file already dirty, before and after. Any difference → a `review_touched_checkout`
   notification listing the paths. Nothing is ever reverted. (Writes into git-ignored paths such as `data/` are
   not seen — the session has no tool that writes there except tune.py and propose.py.)
9. **The summary**: the text after the last `## SUMMARY` heading of the final message (first line
   `level: info|warn|critical`), at most `operator.summary_max_chars` (1500), is sent through `core/notify.py` (log
   line, toast, Telegram) with the turn and token counts. A `max_turns` or failed session sends a warning instead.
10. **`<ts>_<kind>.session.json`**: written as `running` before the CLI starts and completed at the end.

Exit codes (Task Scheduler's "Last Result"): 0 finished (`ok` or `max_turns`) or dry run, 1 failed (`pack_failed`,
`cli_missing`, `venv_missing`, `not_signed_in`, `no_time`, `error`, `timeout`), 2 not run (`disabled`, `busy`,
`gauge_paused`), 3 the config cannot be read (`load_settings` failed, e.g. a bad `config\config.local.yaml`): one
timestamped line in `logs\operator-session-config-error.log` of the checkout, a best-effort critical toast "Operator
session cannot read its config", the error on stderr — nothing runs until the config is fixed (`check_ops.bat` points
there). The log is `logs/operator-session.jsonl`.

### Files in `data/reviews/` (shared by every system; the dashboard's Reviews tab)

| file | what |
|---|---|
| `<ts>_<kind>.md` / `.json` | the review pack (`<ts>` = `YYYYMMDDTHHMMSSZ`, UTC) |
| `<ts>_<kind>.prompt.md` | exactly what went to the CLI's stdin |
| `<ts>_<kind>.cli.stdout.json` / `.cli.stderr.txt` | the CLI's raw output (the result document) |
| `<ts>_<kind>.session.json` | status, model, args, env changes (names only), pack, CLI exit, turns, usage, API-equivalent cost, permission denials, the redacted final text, summary + level, diff guard, ledger |

A daily review **happened** when `<ts>_daily.session.json` exists with status `ok` or `max_turns`; a `_daily.md` pack
alone may come from a dry run or a failed start.

## 3. The command line (`tools/operator/session_args.py`)

`python tools\operator\session_args.py --kind daily` prints it (JSON) without running anything.

```
claude -p --model <m> --effort <e> --output-format json --no-session-persistence --setting-sources=
  --strict-mcp-config --permission-mode dontAsk --permission-prompts none --max-turns <operator.*_max_turns>
  --system-prompt-file <checkout>\tools\operator\prompts\_system.md [--add-dir <data root>]
  --tools Read,Grep,Glob,Bash
  --disallowedTools Edit Write NotebookEdit WebFetch WebSearch "Read(**/.env)" "Read(.env)" "Read(**/.env.*)"
    "Read(~/.claude/**)" "Read(~/.claude.json)" "Read(~/.ssh/**)" "Read(~/.aws/**)" "Read(~/.config/**)"
    "Read(**/.credentials.json)"   [neither checkout nor data root under the home directory] "Read(~/**)"
  --allowedTools
    "Bash(.venv/Scripts/python.exe tools/health_report.py *)" "Bash(.venv/Scripts/python.exe tools/review_pack.py *)"
    "Bash(.venv/Scripts/python.exe tools/tune.py *)" "Bash(.venv/Scripts/python.exe tools/propose.py *)"
    "Bash(.venv/Scripts/python.exe tools/notify.py *)"
    [diagnose only] "Bash(.venv/Scripts/python.exe tools/kill_switch.py --pair *)"
```

* `dontAsk` + `--permission-prompts none`: anything not allowed is denied, never asked; the denials are in the
  session file (`result.permission_denials`) — a denied command costs a turn, which is why `_system.md` spells out
  the exact command forms.
* `--setting-sources=` (equals form, one argument): no settings, hooks or CLAUDE.md; `--strict-mcp-config`: no MCP.
* **Reads: no allow rule for Read, Grep or Glob.** In Claude Code 2.1.282 the three share one path check: the Read
  deny rules first, then a path inside a working directory (the checkout and every `--add-dir`) is allowed by itself,
  then the path allow rules, else "ask" — which `dontAsk` turns into a denial. A tool-wide allow rule (the former bare
  `Read` / `Grep` / `Glob`) is applied to that "ask" and allowed every file on the machine outside the deny list
  (`~/.claude.json`, the CLI's state, included). Grep and Glob obey the Read deny rules for their path and skip every
  denied file they search. Rule roots: `~/` = the home directory, `C:/…` = that drive, anything else = the working
  directory (so `**/.env` covers only the checkout). `Read(~/**)` — the whole user profile — is added when neither the
  checkout nor the data root lies under the home directory (production, `C:\the_claude_new`); a deny rule wins over
  the working-directory allow, so a scratch data root under `%TEMP%` must not get it. The evidence (quoted from the
  CLI) is in `session_args.py`'s docstring, "Reads".
* **A space before every `*`.** Claude Code (2.1.282) compiles a rule whose only `*` is a trailing ` *` to
  `<command>( .*)?` — the bare command or the command, a space and arguments. A glued `tools/tune.py*` would be
  `tools/tune[.]py.*`, which also matches `tools/tune.py/../<any file>`: Windows collapses the `..`, so the session
  could run any Python file (a `.env` line in a traceback, pip, `demo_order_test.py`), and python.exe arguments are
  not path-checked by the CLI. `test_no_rule_lets_a_path_through_an_allowed_tool` emulates the CLI's matcher.
* The diagnosis' kill-switch rule names `--pair`: one pair's switch only. `--all` (every system) is the monitor's
  decision (§3.8) or the owner's; `kill_switch.py` refuses it in a session as well (below).
* **No git rule** (a deviation from the §3.8 text): the pack carries the git facts (sha, branch, last 5 commits,
  dirty flag and files), and `git diff` / `git log` accept `--output=<file>` — a file write through a read-only-looking
  prefix. `demo_order_test.py`, `migrate_instance.py` and every script are not reachable.
* `--add-dir` only when the data root lies outside the checkout (a scratch live run); the working directory is
  derived from the location of `run_session.py`, never hard-coded.
* The allow-list paths are relative: the checkout needs `.venv\Scripts\python.exe` (production has it; a worktree
  needs a junction, `mklink /J .venv C:\the_claude_new\.venv` — git-ignored). Without it a run refuses
  (`venv_missing`); a dry run reports it.

Environment of the CLI (and so of every command the model runs): only what Windows and the CLI need (the provider's
`ENV_KEEP`), plus `TRADINGSYSTEM_CONFIG` / `TS_INSTANCE` / `TS_NOTIFY_DISABLE` (the tools must see the runner's data
root), `CLAUDE_CODE_GIT_BASH_PATH` when you set it, the subscription token when configured, and
`PYTHONIOENCODING=utf-8`, `PYTHONUTF8=1` (tool output has "≥", "→"), `MSYS_NO_PATHCONV=1` (Git Bash would turn
`/nopause`-style arguments into paths). Never `CLAUDECODE`, other `CLAUDE_CODE_*`, `CLAUDE_EFFORT`, `ANTHROPIC_*`
(an inherited API key would bill per token) or any secret from `.env`.

**The session marker `TS_OPERATOR_SESSION=1`** is added too, with the session's review id as
`TS_OPERATOR_REVIEW_ID` (never inherited from the runner's own environment), so every command the session runs
inherits both. The allow-listed tools refuse what only the owner may do while the marker is set: `tune.py … playbook`
takes `--text` only (a FILE or `-` is refused — a file the tool opened would get around the session's Read denials);
`propose.py` refuses `--body-file` and any `--base` other than `main`; `kill_switch.py` refuses `--all`, a second
pair and the last pair still trading (§6). `kill_switch.py`, `tune.py` and `propose.py` record the actor
`operator-session:<review id>` (`operator-session` without a valid id) whatever `--actor` says, so the switch file,
`tuning_changes` / `changes.jsonl`, the proposal and the notifications always show that a session did it. The prompts
say so (`_system.md`, `diagnose.md`), so a session does not waste a turn on them.

## 4. The review pack (`tools/review_pack.py`)

```
python tools\review_pack.py --hours 24 --kind daily            → data\reviews\<ts>_daily.md + .json
python tools\review_pack.py --hours 6 --pair BTCUSDT --print   → markdown on stdout, nothing written
```

Read-only (SQLite `mode=ro`, plain file reads). Systems as `tools/health_report.py` picks them (imported, not
duplicated). Contents: versions and hashes (git sha/branch/last commits/dirty, config, prompt library, per pair the
prompt / library / playbook / adaptive hashes of the latest decision and of the files); AI usage of the window by
role with the cache-read share and the usage gauge; the health report's lines; log ERROR counts; the global kill
switch and `TUNING_FREEZE`; per pair the funnel (screen closes and fired triggers by strength from the engine log →
ledger calls by role → stored answers by status and trigger strength → decisions → ideas → execution and gate
failures by check → placed → broker outcomes with P&L, virtual outcomes, mean R → decision-metric means: MFE/MAE in
R, TP1/2/3 hit shares, minutes to resolve, exit reasons, rejected-but-virtual-win, spread at the gate, slippage,
commission, swap, NO_TRADE counterfactual), attribution by session / regime / setup kind, position actions and rule
executions, escalation verdicts, the adaptive overlay as the services read it (`core.adaptive.read_files` +
`compute_effective`: an invalid file is reported and its entries listed NOT APPLIED, expired entries and a disabled
overlay are not in force, one line of effective values per pair), the playbook (shown as in force only when the
services use it, else labelled "on disk, NOT in force"), the recent `changes.jsonl` lines and `tuning_changes` rows, the pair's kill switch, the last operator
notes; the operator context (open proposals, the last sessions' summaries, the monitor's state); the last 25 trade
ideas of the window. The markdown is cut to ≤ 40 k characters (fewer ideas and detail rows first; the JSON keeps
everything). `--kind` only names the files (`adhoc` by default); `--out` must be `data\reviews` or a folder under
it (anything else, UNC and `//` paths included, exits 3). The usage section counts operator sessions whose usage is
unknown (timed out or crashed before the CLI printed its result).

## 5. Proposals (`tools/propose.py`)

```
python tools\propose.py --slug xau-asia-filter --title "Skip the Asia session for XAU" --pair XAUUSD
                        --review-id 20260927T043000Z_weekly --body "## Problem ... ## Numbers ... ## Proposed change
                        ... ## Risk ... ## Test plan ..."   [--body-file F] [--dry-run]
```

`git worktree add -b proposal/<date>-<slug> C:\the_claude_new_wt\proposal-<date>-<slug> <main's sha>` (next to the
other worktrees, derived from the repository's main checkout; from the base's commit as it was counted, so the base
cannot move in between), then there: `docs/proposals/<date>-<slug>.md` and a row in `PROJECT_STATUS.md` ("Human
actions pending"), committed on the branch; one line in `data/shared/proposals.jsonl` (`status: awaiting_user`, the
dashboard's Proposals tab) and a notification. The production working tree is never written. An existing worktree or
branch of that name is refused (exit 2); a body without the five sections, a bad slug (3-40 lowercase letters,
digits, hyphens) or an unknown pair is invalid (exit 3); a git failure after the worktree was created removes that
worktree and branch again (exit 1).

**What a merge brings in is always stated.** The document header, the notification and the `proposals.jsonl` record
(`base`, `base_sha`, `commits_not_in_main`) name the base, its short sha and the number of commits on the branch
that are not in main (`git rev-list --count main..<branch>`). From `main` that is 1 — the document's own commit. A
human may pass `--base <branch>`; the extra commits are then spelled out ("merging brings ALL of them into main") and
the notification is a warning. An operator session (`TS_OPERATOR_SESSION=1`) proposes from `main` only: another
`--base` is invalid (exit 3), and a branch that ends up with anything but its one commit not in main is removed again
and refused (exit 2).

**`--body-file`** is for humans: a session passes `--body` (the tool refuses `--body-file` there — a file it opened
would get around the session's Read denials), and a file named `.env*` or `*credential*` is refused before it is
opened.

**A missing record.** When the `proposals.jsonl` append fails after the commit (a disk or permission error), the
branch is still the proposal: the failure is logged, the notification (a warning) says the record is missing, the
success line is printed with a `warning:` line, and the exit code is 0 — a retry would only create a second branch.
The dashboard and the next review pack do not list such a proposal; add the line by hand or delete the branch.

**Accept**: `git merge --ff-only proposal/<date>-<slug>` in `C:\the_claude_new` (the document becomes a pending task
in PROJECT_STATUS.md; implementing it is a normal change) — first check the header's "commits not in main"
(`git log main..proposal/<date>-<slug>`). **Reject**: `git worktree remove
C:\the_claude_new_wt\proposal-<date>-<slug>` then `git branch -D proposal/<date>-<slug>`.

## 6. The kill switch in a diagnosis (`tools/kill_switch.py`)

```
python tools\kill_switch.py --pair BTCUSDT --reason "order burst: 5 orders in 40 min"
python tools\kill_switch.py --all --reason "..."      the global switch needs --all explicitly
python tools\kill_switch.py --status
```

ON only (OFF stays `scripts\kill_switch_off.bat [PAIR]` — deliberate friction). Writes `data\instances\<PAIR>\KILL_SWITCH`
(or `data\KILL_SWITCH`) under the data root of the loaded settings (`TRADINGSYSTEM_CONFIG` honoured — a scratch run
never reaches production) with who and why; an existing switch is kept as it is; a critical notification. Replaces
`kill_switch_on.bat /nopause` for sessions: the .bat writes relative to its own checkout and Git Bash turns `/nopause`
into a path.

A diagnosis may engage **one pair's** switch only: its allow-list rule is `kill_switch.py --pair *`, and with
`TS_OPERATOR_SESSION=1` the tool refuses `--all` (exit 2, "a session may engage one pair's switch only"). The rule
alone would let a session engage every pair one by one (or in one `&&` chain), so the tool also bounds it, writing
nothing when it refuses (exit 2): the switch records the actor `operator-session:<review id>`, and a second pair is
refused once another pair's switch carries this review's actor; a switch that would leave no pair trading — the
global switch on, or every other pair's switch on (the configured `instances:`, else the enabled pairs) — is refused
too. A pair that is already on stays "already on" (exit 0, nothing written). The global switch is the monitor's
decision (the equity drop) or the owner's (`--all` by hand, or `kill_switch_on.bat`). Exit codes: 0 engaged (or
already on; `--status`), 1 not written, 2 refused, 3 invalid.

## 7. By hand

```
powershell -NoProfile -ExecutionPolicy Bypass -File tools\operator\run_session.ps1 -Kind daily -DryRun
.venv\Scripts\python.exe tools\operator\run_session.py --kind daily --dry-run
.venv\Scripts\python.exe tools\operator\run_session.py --kind diagnose --reason "stale heartbeat ETHUSDT"
```

The dry run builds the pack and the prompt and prints the exact command, the working directory, the environment
changes (names; secrets hidden) and any problem — no sign-in check, no CLI, no ledger row, no notification, no
session file.

**The one live call (§3.8 4.4)** on a scratch data root: a full copy of `config/config.yaml` with absolute
`paths.data_dir` / `paths.logs_dir` (`TRADINGSYSTEM_CONFIG` disables `config.local.yaml`), a copy of production data
there, `set TRADINGSYSTEM_CONFIG=<scratch config>` and `TS_MONITOR_NO_DIAGNOSE=1`, the `.venv` junction in the
worktree, then `run_session.py --kind daily`. The session's tools inherit `TRADINGSYSTEM_CONFIG` and so see the
scratch root; the data root is outside the checkout, so it is added with `--add-dir`. Record from the session file:
tokens (`result.usage`, target ≤ 30 k), turns, `permission_denials`, whether tune.py was called and what the scratch
`tuning_changes` say, the summary, `diff_guard.clean`.

## 8. When something goes wrong

| status / symptom | meaning, what to do |
|---|---|
| `not_signed_in` | `claude auth login` once in a terminal (H11); an API-key sign-in is refused on purpose |
| `max_turns` | the session used all its turns without a summary — read `result.permission_denials` (denied commands cost turns) and the prompt/checklist |
| `timeout` | the deadline killed the CLI tree; there is no result document, so the usage is unknown: the ledger row holds 0 tokens and its `error` starts with `usage_unknown: ` (with the elapsed seconds); the usage gauge (engine status, the health report's gauge line with `!!`, the pack's gauge and so the session gate's reason) and the review pack count such sessions (`ai/budget.py` `USAGE_UNKNOWN_PREFIX`). The same holds for an `error` without a result document |
| `busy` | another session held the lock (or the CLI start stagger did not clear) — the next scheduled run tries again |
| `gauge_paused` | the usage gauge is enforcing at level 2 (`ai.usage`); diagnoses still run |
| `venv_missing` | the checkout has no `.venv\Scripts\python.exe` (a worktree) — create the junction |
| `review_touched_checkout` notification | the checkout changed during a session — look at `git status`; nothing was reverted |
