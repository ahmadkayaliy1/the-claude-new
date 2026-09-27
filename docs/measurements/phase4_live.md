# Phase 4 — the one live call: a daily review session (2026-09-27)

## Setup

`tools/operator/run_session.py --kind daily` from the worktree (`feat/phase4-watches-learns` at `aee722b` + the pack
labels below), on a scratch data root:
- `hot` and `cold` were `mklink /J` junctions to production, removed afterwards with `rmdir`.
- SQLite-backup copies of the three pairs' `app.db` and of the shared ledger were taken at 12:56:33 UTC, seconds before
  the run.
- The production logs were copied.
- `TRADINGSYSTEM_CONFIG` pointed at a full config copy with absolute paths.
- `EXECUTION_MODE=demo`, `EXECUTION_TRIGGER=auto` were set, matching production's `.env` switches (not secrets).
- `TS_MONITOR_NO_DIAGNOSE=1`.
- A git-ignored `.venv` junction in the worktree made the allow-list's relative `.venv/Scripts/python.exe` resolve.

The worktree has no `.env`, so Telegram was skipped and the summary went to the log and a Windows toast.

The `--dry-run` beforehand printed the exact command:
- **Model and limits:** `opus`, `--effort high`, `--max-turns 30`.
- **Flags:** `--setting-sources=`, `--permission-mode dontAsk --permission-prompts none`.
- **Data root:** `--add-dir <scratch data>`.
- **Tools:** `--tools Read,Grep,Glob,Bash`.
- **Deny list:** editors and the web; `.env`, `~/.claude`, `~/.ssh`, `~/.aws`, `~/.config` and `*.credentials.json` reads.
- **Allow list:** Bash only for the five `tools/*.py` prefixes.

It also printed the environment changes (names only). Removed: `CLAUDECODE`, every `CLAUDE_CODE_*`, `CLAUDE_EFFORT`,
`ANTHROPIC_BASE_URL`, and the calling session's OAuth/staging variables. Kept: `TRADINGSYSTEM_CONFIG`. Added:
`PYTHONUTF8`, `MSYS_NO_PATHCONV`.

## Result

| measure | target | result |
|---|---|---|
| session end | finished | **`success`**, `end_turn` — status `ok` |
| turns | ≤ 30 | **10** |
| time | ≤ 20 min | **119 s** wall (CLI 98 s, API 78 s) |
| tokens | ≤ 30 k | **missed.** 27.3 k unique context (cache creation 27 314 + fresh 18) and 7 415 output (5 237 of it thinking) = 34.7 k (+16 %). Over the 10 turns the CLI re-read that context 176 610 times from the cache, so the ledger records 204 k input + 7.4 k output; the usage gauge (cache reads at 0.1) counts ≈ 52 k. API-equivalent $0.40 (Opus list price). Accepted in D-045: a multi-turn session re-sends its context every turn, so the spec's single-call budget cannot hold; the levers are `operator.daily_max_turns` and the pack size. |
| permission denials | 0 | **0**: every command the model ran matched the allow-list |
| diff guard | clean | **clean** (git status of the worktree identical before and after) |
| tune.py | within policy | **no change**: "no pair has enough resolved outcomes (BTCUSDT 4, ETHUSDT 1, XAUUSD 0, against 20 needed)". No `tuning_changes` row and no `data/adaptive` file on the scratch root. |
| proposals | — | none; no branch or worktree was created |
| ledger | one row | recorded: role `review`, pair NULL. It stays outside the pairs' request quota and counts in the usage gauge. |
| summary | ≤ 1500 chars, sent | level `info`, 1 500 chars. Sent through `core.notify`: log line ✓, toast (no failure logged; the owner confirms on screen), Telegram skipped (no `.env` — H18) |
| pack | ≤ ~10 k tokens | 13.7 k chars (≈ 4 k tokens); prompt 16.5 k chars |

The redacted result document is `tests/fixtures/real/claude_code_session_result.json`, and
`test_the_real_session_result_is_parsed` checks it.

## What the review said

These are real observations on production data of the last 24 h:
- **BTCUSDT:** 45 answers, 4 ideas, 2 placed, both closed in profit (+4.07 USD). The gate rejected 2: one with
  `rr_after_costs`, which would have lost −1 R, and one with `sl_max_distance`, which would have won +0.80 R. There were 4
  AI errors (the OAuth refresh race, a 403, the subscription session limit, one 180 s timeout); all of them are handled.
- **ETHUSDT:** 36 of 37 answers were NO_TRADE while price stayed in a 2706–2724 box. One idea was rejected for
  `correlated_exposure`; it would have won +1.03 R virtual.
- **XAUUSD:** closed for the weekend. The old Binance XAUUSDT heartbeat kill loop (22:57–23:19 UTC, fixed in `6a10a26`)
  shows in the window.
- **Late bars:** 5 `data_not_ready` events around the 09:18 restart.
- **Cache-read share:** 0.28. The session explained it as calls being far apart. The real reason is that the payload and
  the images are not cacheable; only the system prompt (≈ 6.6 k of ≈ 25 k) is.
- **Metrics:** empty, because Phase 4 is not merged yet.
- **Mistake:** it read the 13 `decision`-role ledger rows (the trader's cycle calls since Phase 3) as escalations.

Fixed after the run: the pack now says `escalations 0 (ai.escalation.enabled is off)` when there are none, and labels
each ledger role. The reviewer also noticed that the copied heartbeats froze while it worked (the scratch copies do not
advance). In production they do.
