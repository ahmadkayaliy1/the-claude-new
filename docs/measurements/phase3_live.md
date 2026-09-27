# Phase 3 — the one live call and the screening budget (2026-09-27)

## The live call

`engine --once --pairs BTCUSDT` from the worktree (`feat/phase3-sees-manages` at `2e08e1b` + the two fixes below), on
a scratch data root: `hot` and `cold` as `mklink /J` junctions to production (removed afterwards with `rmdir`, production
untouched), a SQLite-backup copy of `data/instances/BTCUSDT/app.db` taken seconds before the run (so the executor's
account report, with the live BUY `00eded5a`, was fresh), its own `shared/ai_usage.db`, config via
`TRADINGSYSTEM_CONFIG` (paths only). Charts on, escalation off (default), Sonnet (`ai.models.decision` = the provider's
own model).

| measure | target | result |
|---|---|---|
| status | valid, first attempt | **valid**, no repair (`status=valid`, 1 ledger row) |
| images | 6 | **6** (1w, 1d, 4h, 1h, 15m, 5m; 720×400; estimate 2 304 tokens) |
| input tokens | ≤ 23 k | **24 864 — missed by 1.9 k (8 %)**, see below (cache creation 24 862, cache read 0: the first call with the v6 prompts) |
| output tokens | — | 2 608 |
| latency | ≤ 120 s | **43.4 s** |
| turns | 1 | **1** |
| stream-json user-line shape | probed once | **`message`** (`{"type":"user","message":{"role":"user","content":[…]}}`), CLI 2.1.282, recorded in `%TEMP%\tradingsystem-claude-code\cli_capabilities.json` |
| `position_actions` | empty or valid | `[]` — the model saw the live BUY `00eded5a` (entry 84 343, SL 84 085.6) and held it: "stop still well clear of price, no action" |
| decision | — | NO_TRADE, confidence 40, `next_review` 30 min + 15m-close conditions at the range edges |
| engine RSS, charts | ≤ +60 MB per pair | **+37.3 MB** for the first set (logged by `analysis/charts.py`; +32.8 MB in the dry run); the engine process sat at ≈ 107 MB after the call |
| `snapshot_build_ms` | < 3 s (else a lighter screen payload) | 1 142 ms for this build (laptop under load: 3 production systems running); replay medians 166–545 ms, p95 213–2 273 ms, max 3 149 ms (three replays in parallel with production) → the full payload is built at every 5m screen |

The redacted stdout is `tests/fixtures/real/claude_code_stream_json_result.jsonl` (parsed and contract-validated by
`test_parse_real_stream_json_capture`).

### Input tokens: where the extra 5.3 k went

Production baseline (the same Sonnet agent, last 30 calls): median ≈ 19.6 k input, of which 6.6 k are a cache read of
the system prompt. This call: 24.9 k.

| part | tokens (≈) | note |
|---|---|---|
| six chart images | + 2.3 k | `ceil(720·400/750)` = 384 each |
| system prompt v6 (persona v2, core rules v6, legend v4, agent system v3) | + 2.2 k | +5.6 k characters: management semantics, `position_actions`, trigger prefixes, charts, single leg, playbook. It is the cached part: from the second call on it is a cache read |
| user side (instructions v4, charts note, playbook line, this cycle's payload) | + 0.9 k | payload size varies by cycle |

Levers without code, if the budget needs it: `ai.charts.timeframes` (each chart dropped saves 384), `ai.charts.width/height`
(640×360 → 308 per chart), `ai.charts.enabled: false` (text only, −2.3 k). Trimming the new rule/legend text was not done:
it is what tells the model how its management rules and position actions are executed.

### Two problems found on the way (fixed)

1. `engine --once` never waited for the Claude Code sign-in check, which runs in a background thread and answers
   "checking" until `claude auth status` returns; a one-shot run therefore always fell back (here to Gemini, which has
   no key) — no model call was made (0 ms, empty ledger). The long-running engine is not affected (it asks again on its
   next tick). Fix: `analysis/engine.py:wait_for_provider` (bounded 120 s, `LLMProvider.availability_pending`), test
   `test_once_waits_for_the_first_sign_in_answer`.
2. The measurement wrapper (not product code) piped the engine's stdout/stderr without reading them; after the call the
   engine blocked on the full pipe while printing the answer. The decision and the ledger row were already stored;
   the scratch processes were stopped. (Lesson for the next phases' live calls: redirect to files.)

## Screening budget — replay of stored data

`tools/replay_triggers.py <PAIR> --hours 24 [--end-ms …] --data-dir C:\the_claude_new\data` (read-only): at every 5m
close the engine's snapshot and `ai.triggers.decide` with the engine's inputs (last dispatch signature, price at the last
call, spacing, idle floor). Time-based `next_review` and executor events need the model's answers and live trades and
are not replayed: the numbers are the setup/idle part of the day; reviews and events come on top and the 40/day cap
(`_ration`: < 50 % left → strong + review + event only; < 20 % → review + event) bounds the total.

| window (UTC) | BTCUSDT | ETHUSDT | XAUUSD |
|---|---|---|---|
| Thu 09-24 20:00 → Fri 09-25 20:00 | **36**/day (strong 18, weak 14, idle 4) | **35** (20 / 13 / 2) | **32** (14 / 15 / 3) |
| Sat 09-26 00:45 → Sun 09-27 00:45 | **32** (13 / 16 / 3) | **30** (14 / 11 / 5) | market closed |
| Wed 09-23 20:00 → Thu 09-24 20:00 | — | — | **21** (9 / 5 / 7) |

History of the tuning (same data): the first screening version (5m zones and liquidity counted as weak setups) gave ≈ 50
calls/day for BTC; with "5m = confirmation only" (no 5m zones/liquidity; a 5m reversal candle counts only at a 15m/1h
location) and `ai.weak_needs_location` BTC was still 42 on the Friday — mostly the same pool swept by consecutive bars
(each bar a new key) and 5m candles/order flow repeating inside a zone the model had already seen. Final rules: a sweep
is keyed by its pool (`1h:sweep:bearish:<level>`), and a weak call needs a location **and** new price action there (a new
location, a structure break, or a decision-TF reversal candle); 5m candles or order flow alone at a known location wait
for the model's own `next_review` conditions. Integration test: `tests/unit/test_replay_budget.py -m integration`
(≤ `ai.daily_calls_per_pair` per pair on the last 24 h).
