# The learning loop: adaptive overlay, playbooks and `tools/tune.py` (Phase 4, §3.8)

The system measures its own decisions (`decision_metrics`, virtual outcomes), a Claude review session reads them
daily or weekly, and it may change **only** a small, bounded, per-pair overlay and the pair's playbook. Everything
else — risk, sizing, stops, the gate, code, prompts, `config.yaml` — can only be *proposed* (`tools/propose.py`,
docs/operator_sessions.md); you decide.

This page covers the overlay: what it may change, how the services read it, the tool that writes it, the policy that
tool enforces, and how you stop or undo it.

---

## 1. Where it lives

Everything is per pair, under the shared data root, and nothing is in git:

| File | Written by | What |
|---|---|---|
| `data/adaptive/<PAIR>/adaptive.yaml` | `tools/tune.py` only | the overlay: one entry per tuned key |
| `data/adaptive/<PAIR>/playbook.md` | `tools/tune.py` only | the pair's playbook (its hash is in `adaptive.yaml`) |
| `data/adaptive/<PAIR>/changes.jsonl` | `tune.py` (set, revert); the services (`expired`) | append-only history |
| `tuning_changes` in the pair's `app.db` | `tune.py`; the services set `reverted_ms` on expiry | one row per set / revert |
| `data/shared/locks/adaptive_<PAIR>.lock` | `tune.py` and the readers | the pair's lock |
| `data/TUNING_FREEZE` | **you** | while it exists, no change is applied (revert still works) |

The pair's `app.db` is the one its services use: `data/instances/<PAIR>/app.db` when the pair runs as its own system
(a configured `instances:` entry), else the all-pairs system's `data/app.db`.

No restart is needed: the services see a change within `adaptive.reload_check_s` (5 s).

## 2. What may be tuned

| Key | Bounds | Direction | Needs | Consumer (effective value) |
|---|---|---|---|---|
| `min_confidence_floor` | 55 … 80 | raise only | 20 outcomes | executor gate: `max(risk.min_confidence, floor)` |
| `min_minutes_between_calls` | 15 … 60 | up only | 10 | engine spacing (`max` with the config) |
| `max_idle_minutes` | 60 … 240 | up only | 10 | engine idle review (`max`) |
| `review_floor_minutes` | 5 … 30 | up only | 10 | engine + next-review floor (`max`) |
| `trigger.weak_min` | 2 … 3 | up only | 20 | setup screen: weak reasons needed (`max`) |
| `trigger.liquidity_atr` | 0.2 … 0.5 | down only | 20 | setup screen: "near liquidity" distance (`min`) |
| `pair.ai_paused_until` | future, ≤ now + 7 d | — | 10 | the engine dispatches no AI call until then |
| `tp_hint` | one line, ≤ 200 chars, linted | — | 20 | user prompt `$tp_hint` |
| `playbook` | ≤ 1500 chars, ≤ 12 bullets, linted | — | 20 | user prompt `$playbook` |

"Needs" = resolved virtual outcomes (`virtual_outcome` `tp1_first` or `sl_first`) of the pair's decisions in the
window: `adaptive.min_samples_strategy` (20) for strategy keys, `adaptive.min_samples_activity` (10) for keys that only
make the system call less.

Every direction makes the system **more selective or slower**, never looser. The consumers apply the direction again
against the config (`max` / `min`), so a stricter `config.yaml` always wins and a hand-edited file cannot loosen
anything. There are no risk or execution keys: `AdaptiveCfg` has none, and an unknown key makes the file invalid.

### Entry

Each key holds one entry:

```yaml
min_confidence_floor:
  value: 60
  set_ms: 1790334600000          # UTC ms
  expires_ms: 1791544200000      # ≤ set_ms + adaptive.max_expiry_days (14 d)
  reason: low-confidence longs lost 9 of 14 in the last week
  evidence: {virtual: {tp1_first: 5, sl_first: 9}, window: "168 h"}
  window_hours: 168
  review_id: 2026-09-25_daily
```

`playbook`'s `value` is the 16-hex hash of `playbook.md`. `pair.ai_paused_until`'s `value` is a UTC ms time.

**Every entry expires** (default and maximum `adaptive.max_expiry_days` = 14 days). An expired entry is simply
absent: the config value applies again. To keep a value longer, a later review sets it again (same value = renewal).

## 3. How the services read it (`core/adaptive.py`)

`AdaptiveStore(settings, pair, app_db=…, emit=…).effective(now_ms)` returns an `Effective` with the values in force:
`min_confidence`, `min_minutes_between_calls`, `max_idle_minutes`, `review_floor_minutes`, `weak_min`,
`liquidity_atr`, `ai_paused_until_ms` (only while in the future), `tp_hint` and `playbook` (`""` when none),
`adaptive_hash` (16 hex of the tuned values, `None` when nothing is tuned), `playbook_hash`, and `expires`.
The two hashes are stored with every decision, so the review can tell which overlay a decision was made under.

- The files are checked at most every `adaptive.reload_check_s` and re-read only when their modification time or size
  changed; the read happens under the pair's lock (a reader waits at most 0.25 s, then keeps the last values and
  looks again at the next check).
- **Missing file** = config values. **`adaptive.enabled: false`** = config values, whatever the files say.
- **Invalid file** (bad YAML, a value out of bounds, an unknown key, an expiry beyond the limit, a hint or playbook
  that fails the lint, a `playbook.md` whose hash does not match — i.e. edited by hand): the last good values stay in
  force and the process records one `adaptive_invalid` event (one per invalid version of the file). A service that
  starts with an invalid file uses the config values.
- **Expired entry**: absent from then on; exactly one `expired` line in `changes.jsonl` (de-duplicated across
  processes under the lock), `tuning_changes.reverted_ms` set by the process that was given the pair's `app_db`
  (the engine), and one `adaptive_expired` event.
- `effective()` never raises; on any unexpected error it returns the config values.

## 4. `tools/tune.py` — the only writer

```
.venv\Scripts\python.exe tools\tune.py --pair BTCUSDT set min_confidence_floor 60 ^
    --reason "low-confidence longs lost 9 of 14" --evidence-json "{\"tp1_first\": 5, \"sl_first\": 9}" ^
    --window-hours 168 --review-id 2026-09-25_daily
.venv\Scripts\python.exe tools\tune.py --pair BTCUSDT set pair.ai_paused_until +6h --reason "..." --evidence-json "{}"
.venv\Scripts\python.exe tools\tune.py --pair BTCUSDT set tp_hint "TP1 at the prior 15m swing" --reason ... --evidence-json ...
.venv\Scripts\python.exe tools\tune.py --pair BTCUSDT playbook playbook_draft.md --reason ... --evidence-json ...
.venv\Scripts\python.exe tools\tune.py --pair BTCUSDT playbook - --reason ... --evidence-json ...   (text on stdin)
.venv\Scripts\python.exe tools\tune.py --pair BTCUSDT revert min_confidence_floor --reason "made it worse"
.venv\Scripts\python.exe tools\tune.py [--pair BTCUSDT] list [--json]
```

- `--dry-run` checks everything and prints `dry-run: would apply: …`; nothing is written.
- `--actor NAME` (default `operator`) is recorded with the change; it grants nothing.
- `--pair`, `--dry-run` and `--actor` may stand before or after the command.
- `--reason` (3–500 chars) and `--evidence-json` (any JSON value, ≤ 4000 chars) are required for `set` and
  `playbook`. `--window-hours` (24–720, default `adaptive.default_window_hours` = 168) is the window for the sample
  count and the health check. `--expires-days` (1 … `adaptive.max_expiry_days`) shortens the expiry.
- `pair.ai_paused_until` accepts `+90m` / `+6h` / `+2d`, an ISO time **with** a zone (`2026-09-28T12:00Z`), or UTC ms.
- `playbook` takes a file, `-` for stdin, or `--text "…"`. The session has no file-writing tool, so stdin or
  `--text` is the usual way.

Output is one line: `applied: BTCUSDT min_confidence_floor 55 -> 60 until 2026-10-09T11:10:00.000Z (change 12)`,
`refused: … <every reason, separated by ;>`, or `invalid: …`.

| Exit code | Meaning |
|---|---|
| 0 | applied (or would apply with `--dry-run`; `list`; nothing to revert) |
| 2 | refused by the policy (the reasons are printed) — including out-of-bounds values and a busy lock |
| 3 | invalid request: unknown pair or key, unparsable value or JSON, missing or malformed argument |
| 1 | unexpected error (e.g. a database error) |

An applied change or revert sends an `info` notification through `core/notify.py` (key `tune:<PAIR>:<key>:<ts>`).
tune.py writes no log file: `changes.jsonl` and `tuning_changes` are the record.

### What it writes

Only `adaptive.yaml`, `playbook.md` and `changes.jsonl` under `data/adaptive/<PAIR>/` (every path is checked against
that directory before writing; files are written to a temp file and replaced atomically, retried while a reader holds
the file open), the `tuning_changes` table of the pair's own `app.db` (never created: without an `app.db` a change is
refused), and the pair's lock file. Tests prove that nothing else changes and that other paths and other pairs are
refused (`tests/unit/test_tune_policy.py`).

Order of a change: the `tuning_changes` row is written inside a transaction, then the files, then the transaction is
committed; if anything fails the old files are put back and the row is rolled back. The `changes.jsonl` line follows.

## 5. The policy (in code, under the pair's lock)

A change is refused (exit 2) when any of these holds; every reason is printed.

1. `data/TUNING_FREEZE` exists, or `adaptive.enabled` is false for the pair's system.
2. The value is out of bounds, or the tp_hint / playbook fails the lint (§6).
3. **Direction** — against the value in force now (config + overlay): a raise-only key may not go below it, a
   lower-only key may not go above it. The same value is accepted only as a renewal of an entry that is in force;
   otherwise it would have no effect and is refused.
4. **One change per pair per UTC day** (`adaptive.max_changes_per_day`). Reverts do not count.
5. **Cooldown per key** (`adaptive.cooldown_days` = 7) since the key was last *set*. Reverts neither count nor reset it.
6. **Samples**: fewer resolved virtual outcomes of the pair in the window than the key's group needs (§2).
7. **Health freeze**: more than `adaptive.unhealthy_freeze_pct` (25 %) of the window's hours were unhealthy. An hour
   is unhealthy when it has
   - an `ai_decisions` row of the pair with status `error`, `skipped` or `budget_blocked`, or whose `data_warnings`
     name the pair's decision timeframe (e.g. `"15m: gaps"`);
   - a `killed` or `exited` event of the engine, executor or ingest-mt5 (`supervisor:<service>` events);
   - a heartbeat gap longer than 15 minutes: a supervisor `system_suspend` / `clock_jump` / `stall` event longer
     than that, a `stopped` system whose engine `started` again more than 15 minutes later, or an engine / executor
     heartbeat (`collector_status`) older than 15 minutes now.

   Known limit: a supervisor that died without writing `stopped` leaves no trace of the gap (only the restart); such
   a gap is not counted.

`list` shows, per pair, the values in force, each entry with its expiry, the policy state (samples, unhealthy %,
changes today, cooldowns) and the last changes — the review session runs it before proposing a change.

### Revert

`revert KEY` is **always allowed** — also while `TUNING_FREEZE` exists, `adaptive.enabled` is false, or the change
limits are used up. Going back to the reviewed config is always safe, and a bad change must be undoable at once. A
revert is still logged: a `tuning_changes` row (`new_value` NULL, reason `revert: …`), the `reverted_ms` of the row it
undoes, a `changes.jsonl` line and a notification. It is **not** a change of the day and does not start a cooldown; the
original set still counts for both (a set, revert and set again on the same key waits for the cooldown).

## 6. Playbook and hint lint (`core/playbook.py`)

- Playbook: not empty, ≤ 1500 characters, ≤ 12 bullets (`-`, `*`, `+`, `•`, `1.`, `1)`). Hint: one non-empty line,
  ≤ 200 characters.
- Denylist (both) — the spec's regex, made robust: `risk per`, `lot`/`lots` (not "slot", "pilot", "plot"; "a lot"
  *is* refused), `leverage`, `stop loss distance|closer`, `ignore`, `override`, `always buy|sell|trade|long|short`,
  `never no_trade`, `confidence 80–99`, `daily loss`, `kill switch`, `min_rr`. Matched case-insensitively after
  Unicode normalisation (full-width letters), with zero-width characters removed, with markdown emphasis inside words
  removed, and across line breaks.
- No control characters and no code fences (the payload follows the playbook in a fenced block).
- `$` is allowed. The spec asked for `$$`, but `prompts._fill` checks only the template and inserts values verbatim,
  so `$$` would reach the model literally. Only template text needs `$$`.

The lint runs when tune.py writes and again when a service reads (a hand edit that fails it makes the file invalid).

## 7. `tuning_changes`

| Column | Set | Revert |
|---|---|---|
| `ts` | the set time (= the entry's `set_ms`) | the revert time |
| `key` | the dotted key (`trigger.weak_min`, `playbook`, …) | same |
| `old_value` | JSON of the value in force before (e.g. `55` from the config; the old playbook text) | JSON of the overlay value |
| `new_value` | JSON of the new value (the playbook text for `playbook`) | NULL |
| `reason`, `evidence`, `window_hours`, `review_id`, `actor` | from the request | `revert: <reason>`, actor |
| `expires_ms` | the entry's expiry | NULL |
| `reverted_ms` | set when the entry is replaced, reverted or expires | NULL |

## 8. Your controls

- **Stop all tuning:** create `data\TUNING_FREEZE` (any content). Delete it to allow tuning again.
- **Turn the overlay off:** `adaptive: {enabled: false}` in `config\config.local.yaml` (all systems), or under
  `instances.<PAIR>.overrides` for one pair, then restart. The services then use the config values, and tune.py
  refuses every change.
- **Undo one value:** `.venv\Scripts\python.exe tools\tune.py --pair BTCUSDT revert KEY`.
- **Undo everything of a pair:** revert each key, or stop tuning and delete `data\adaptive\<PAIR>\` (the services go
  back to the config values within 5 s).
- The dashboard's Tuning tab shows the values in force, their expiry, `changes.jsonl`, the playbook and the freeze
  flag.
