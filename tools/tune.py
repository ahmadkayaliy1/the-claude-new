"""Tune one pair's adaptive overlay (§3.8) — the ONLY writer of ``data/adaptive/<PAIR>/`` and ``tuning_changes``.

    python tools/tune.py --pair BTCUSDT set min_confidence_floor 60 --reason "..." --evidence-json "{...}"
                         [--window-hours 168] [--review-id 2026-09-27_daily] [--expires-days 14]
    python tools/tune.py --pair BTCUSDT playbook --text "..." | FILE | - --reason "..." --evidence-json "{...}"
    python tools/tune.py --pair BTCUSDT revert KEY [--reason "..."]
    python tools/tune.py [--pair BTCUSDT] list [--json]
    --dry-run: check everything and print what would be done; nothing is written.   --actor NAME (default operator)

Keys, bounds and directions: ``tradingsystem.core.adaptive.KEYS`` (docs/learning_loop.md). The policy is enforced here,
under the pair's lock, against the pair's own app.db: ``data/TUNING_FREEZE`` or ``adaptive.enabled: false`` refuse
every change; at most ``adaptive.max_changes_per_day`` changes per pair and UTC day; ``adaptive.cooldown_days`` per
key; enough resolved virtual outcomes of the pair in the window (strategy keys ``min_samples_strategy``, activity keys
``min_samples_activity``); no change when more than ``unhealthy_freeze_pct`` of the window's hours were unhealthy; the
direction rule against the value in force now; a ``playbook.md`` the services would reject (missing, hash mismatch,
lint) refuses every change until ``revert playbook``. ``revert`` is always allowed (logged; not a change of the day, no
cooldown).

What it reads: an operator session (``TS_OPERATOR_SESSION=1`` in its environment) passes the playbook only as
``--text``; the FILE and ``-`` forms are for a human, and FILE must be a regular file under the data root or the
checkout, reached without a symlink or junction, not named ``.env*`` or ``*credential*`` (the session's Read denials
must not be bypassed through this tool). Every free text it stores (playbook, tp_hint, reason, evidence, review id,
actor) is refused when it contains the value of a configured secret or a key/token shape the log redactor masks; the
text is never echoed.

Writes: only ``adaptive.yaml`` / ``playbook.md`` / ``changes.jsonl`` under ``data/adaptive/<PAIR>/`` (atomic replace),
the ``tuning_changes`` table of the pair's app.db, and the lock file ``data/shared/locks/adaptive_<PAIR>.lock``.

Exit codes: 0 applied (or: would apply with --dry-run; listed), 2 refused by the policy (reasons printed),
3 invalid request (unknown pair/key, unparsable value or JSON, missing argument), 1 unexpected error.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import math
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any, Callable, NamedTuple, TextIO

CHECKOUT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CHECKOUT / "src"))

from tradingsystem.core import adaptive as ad  # noqa: E402
from tradingsystem.core.filelock import FileLock  # noqa: E402
from tradingsystem.core.logsetup import get_redactor  # noqa: E402
from tradingsystem.core.playbook import lint, lint_hint  # noqa: E402
from tradingsystem.core.settings import INSTANCE_ENV, Settings, load_settings  # noqa: E402
from tradingsystem.core.timeutil import MS_PER_DAY, MS_PER_HOUR, MS_PER_MINUTE, iso  # noqa: E402
from tradingsystem.core.timeutil import now_ms as _now_ms  # noqa: E402

EXIT_OK, EXIT_ERROR, EXIT_REFUSED, EXIT_INVALID = 0, 1, 2, 3
LOCK_TIMEOUT_S = 10.0
HEARTBEAT_GAP_MS = 15 * MS_PER_MINUTE           # §3.8: a heartbeat gap longer than this makes its hours unhealthy
STOP_LOOKBACK_MS = 7 * MS_PER_DAY               # a system stopped before the window and restarted inside it
UNHEALTHY_STATUSES = ("error", "skipped", "budget_blocked")
SUPERVISED = ("supervisor:engine", "supervisor:executor", "supervisor:ingest-mt5")
GAP_EVENTS = ("system_suspend", "clock_jump", "stall")
RESOLVED = ("tp1_first", "sl_first")
WINDOW_HOURS = (24, 720)
PAIR_RE = re.compile(r"^[A-Z0-9]{2,20}$")
ACTOR_RE = re.compile(r"^[\w.@:+-]{1,40}$")
MAX_SOURCE_BYTES = 64 * 1024
SESSION_ENV = "TS_OPERATOR_SESSION"             # "1" in every operator session's environment (the session runner)
MIN_SECRET_CHARS = 8                            # shorter secret values would match ordinary words and numbers
EFFECTIVE_FIELD = {"min_confidence_floor": "min_confidence", "min_minutes_between_calls": "min_minutes_between_calls",
                   "max_idle_minutes": "max_idle_minutes", "review_floor_minutes": "review_floor_minutes",
                   "trigger.weak_min": "weak_min", "trigger.liquidity_atr": "liquidity_atr",
                   "pair.ai_paused_until": "ai_paused_until_ms", "tp_hint": "tp_hint", "playbook": "playbook"}
_UNTIL_REL = re.compile(r"^\+?(\d+(?:\.\d+)?)([mhd])$")

Loader = Callable[[str | None], Settings]


class Invalid(Exception):
    """The request cannot be understood (exit 3)."""


class Refused(Exception):
    """A well-formed request the policy does not allow (exit 2)."""


class PathRefused(Refused):
    """A write outside the pair's adaptive directory or tuning_changes table — never done."""


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:          # argparse would exit 2, which means "refused" here
        raise Invalid(f"{self.prog}: {message}")


# --------------------------------------------------------------------------- the pair and its files
class Target(NamedTuple):             # not a dataclass: tests load this file outside sys.modules
    pair: str
    s: Settings          # the pair's own settings (its system: instance overrides applied)
    dir: Path            # data/adaptive/<PAIR>
    app_db: Path         # the pair's app.db: decisions, events and tuning_changes


def _default_loader(pair: str | None) -> Settings:
    # "" = the all-pairs view even when TS_INSTANCE is inherited from the environment
    return load_settings(extra_env={INSTANCE_ENV: pair or ""})


def target(pair_arg: str | None, loader: Loader) -> Target:
    """The pair's settings and paths, resolved like the services do (its own system when it is a configured
    instance, else the all-pairs system)."""
    pair = (pair_arg or "").strip().upper()
    if not PAIR_RE.fullmatch(pair):
        raise Invalid(f"--pair {pair_arg!r}: not a pair name" if pair_arg else "--pair is required")
    base = loader(None)
    if pair not in base.pairs:
        raise Invalid(f"--pair {pair}: not a configured pair ({', '.join(sorted(base.pairs))})")
    ps = loader(pair) if pair in base.instances else base
    return Target(pair, ps, ad.adaptive_dir(ps, pair), ps.paths.state() / "app.db")


def _inside(t: Target, path: Path) -> Path:
    """The path allow-list: every file write goes through here."""
    root, p = t.dir.resolve(), Path(path).resolve()
    if p == root or root not in p.parents:
        raise PathRefused(f"write outside {root} refused: {p}")
    return p


def _write(t: Target, name: str, text: str) -> None:
    ad.atomic_write(_inside(t, t.dir / name), text)


def _append(t: Target, record: dict) -> None:
    ad.append_jsonl(_inside(t, t.dir / ad.CHANGES_FILE), record)


def _unlink(t: Target, name: str) -> None:
    _inside(t, t.dir / name).unlink(missing_ok=True)


def _db(t: Target, *, readonly: bool = False) -> sqlite3.Connection | None:
    """The pair's app.db (None when its system never ran here — tune.py never creates one)."""
    expected = (t.s.paths.state() / "app.db").resolve()
    if t.app_db.resolve() != expected:
        raise PathRefused(f"database outside the pair's state refused: {t.app_db}")
    if not t.app_db.exists():
        return None
    con = sqlite3.connect(f"file:{t.app_db.as_posix()}?mode={'ro' if readonly else 'rw'}", uri=True, timeout=10,
                          isolation_level=None)
    if not readonly:
        ad.ensure_tuning_table(con)
    return con


def _has_table(con: sqlite3.Connection, name: str) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _current(t: Target, *, lenient: bool = False) -> tuple[ad.AdaptiveCfg, str]:
    """(overlay, playbook text). Invalid files refuse the change (they must be fixed by hand, never overwritten
    blindly) — so does a ``playbook.md`` the services reject (missing, not matching its hash in ``adaptive.yaml``,
    failing the lint): they then ignore the whole overlay, and a change "applied" on top of it would never be in force.
    ``lenient`` (revert, which repairs such a pair) accepts all of that, entries up to the hard 30-day expiry (a
    lowered max_expiry_days) and a tp_hint that today's lint refuses (written under an older one)."""
    try:
        text = (t.dir / ad.YAML_FILE).read_text(encoding="utf-8")
    except FileNotFoundError:
        text = None
    days = ad.HARD_MAX_EXPIRY_DAYS if lenient else t.s.adaptive.max_expiry_days
    try:
        cfg = ad.load_cfg_text(text, max_expiry_days=days, lenient=lenient)
    except Exception as exc:  # noqa: BLE001 — ValidationError / YAMLError
        todo = "fix or delete it by hand"
        if not lenient:
            try:                                        # would revert accept it? then that is the repair
                ad.load_cfg_text(text, max_expiry_days=ad.HARD_MAX_EXPIRY_DAYS, lenient=True)
                todo = f"run: tune.py --pair {t.pair} revert KEY (the entry it names), or fix it by hand"
            except Exception:  # noqa: BLE001
                pass
        raise Refused(f"{t.dir / ad.YAML_FILE} is invalid ({ad._short(exc)}) - {todo}") from None
    pb = ""
    if cfg.playbook is not None:
        path = t.dir / ad.PLAYBOOK_FILE
        try:
            pb = ad.normalize_text(path.read_text(encoding="utf-8"))
            problem = ("does not match its hash in adaptive.yaml (edited by hand?)"
                       if ad.text_hash(pb) != cfg.playbook.value else "fails the lint" if lint(pb) else None)
        except FileNotFoundError:
            pb, problem = "", "is missing but adaptive.yaml references it"
        except UnicodeDecodeError:
            pb, problem = "", "is not UTF-8 text"
        if problem is not None and not lenient:
            raise Refused(f"{path} {problem}: the services ignore the whole overlay until it is repaired - "
                          f"run: tune.py --pair {t.pair} revert playbook")
    return cfg, pb


# --------------------------------------------------------------------------- measurements for the policy
def resolved_samples(con: sqlite3.Connection | None, pair: str, start: int, end: int) -> int:
    """Resolved virtual outcomes (TP1 or SL first) of the pair's decisions made in [start, end]."""
    if con is None or not _has_table(con, "ai_decisions"):
        return 0
    return int(con.execute(
        f"SELECT count(*) FROM ai_decisions WHERE pair=? AND ts>=? AND ts<=? AND virtual_outcome IN "
        f"({','.join('?' * len(RESOLVED))})", (pair, start, end, *RESOLVED)).fetchone()[0])


def _names_tf(warnings: Any, tf: str) -> bool:
    """A snapshot data warning ("15m: gaps (80 bars)") names the decision timeframe."""
    if not warnings:
        return False
    try:
        items = json.loads(warnings) if isinstance(warnings, str) else warnings
    except ValueError:
        items = [warnings]
    if not isinstance(items, list):
        items = [items]
    return any(str(w).split(":", 1)[0].strip() == tf for w in items)


def unhealthy_hours(con: sqlite3.Connection | None, pair: str, decision_tf: str, start: int, end: int) -> set[int]:
    """Indexes of the window's hours (0 = the hour starting at ``start``) that were unhealthy (§3.8): an ai_decisions
    row of the pair with status error/skipped/budget_blocked or a data warning naming the decision TF; a killed /
    exited engine, executor or ingest-mt5 (supervisor events); a heartbeat gap > 15 min — a suspend / clock jump /
    stall of the supervisor, a stop of the whole system until its engine started again, or an engine / executor
    heartbeat that is older than 15 min now."""
    hours: set[int] = set()
    if con is None:
        return hours

    def mark(a: int, b: int) -> None:                       # [a, b)
        a, b = max(a, start), min(b, end)
        if b > a:
            hours.update(range((a - start) // MS_PER_HOUR, (b - 1 - start) // MS_PER_HOUR + 1))

    if _has_table(con, "ai_decisions"):
        cols = {r[1] for r in con.execute("PRAGMA table_info(ai_decisions)")}
        warn = "data_warnings" if "data_warnings" in cols else "NULL"
        for ts, status, warnings in con.execute(
                f"SELECT ts, status, {warn} FROM ai_decisions WHERE pair=? AND ts>=? AND ts<?", (pair, start, end)):
            if status in UNHEALTHY_STATUSES or _names_tf(warnings, decision_tf):
                mark(ts, ts + 1)
    if _has_table(con, "ingestion_events"):
        stopped_at: int | None = None
        for ts, collector, event, duration in con.execute(
                "SELECT ts, collector, event, duration_ms FROM ingestion_events WHERE ts>=? AND ts<? AND "
                "collector LIKE 'supervisor:%' ORDER BY ts, id", (start - STOP_LOOKBACK_MS, end)):
            if collector in SUPERVISED and event in ("killed", "exited"):
                mark(ts, ts + 1)
            elif collector == "supervisor:all" and event in GAP_EVENTS and (duration or 0) > HEARTBEAT_GAP_MS:
                mark(ts - int(duration), ts)
            elif collector == "supervisor:all" and event == "stopped":
                stopped_at = ts
            elif collector == "supervisor:engine" and event == "started" and stopped_at is not None:
                if ts - stopped_at > HEARTBEAT_GAP_MS:
                    mark(stopped_at, ts)
                stopped_at = None
    if _has_table(con, "collector_status"):
        for (updated,) in con.execute(
                "SELECT updated_ms FROM collector_status WHERE collector IN ('engine', 'executor')"):
            if updated and end - int(updated) > HEARTBEAT_GAP_MS:
                mark(int(updated), end)
    return hours


def changes_today(con: sqlite3.Connection | None, pair: str, now: int) -> int:
    """Changes (not reverts) of the pair since UTC midnight."""
    if con is None or not _has_table(con, "tuning_changes"):
        return 0
    return int(con.execute("SELECT count(*) FROM tuning_changes WHERE pair=? AND ts>=? AND new_value IS NOT NULL",
                           (pair, now - now % MS_PER_DAY)).fetchone()[0])


def last_change(con: sqlite3.Connection | None, pair: str, key: str) -> int | None:
    if con is None or not _has_table(con, "tuning_changes"):
        return None
    r = con.execute("SELECT max(ts) FROM tuning_changes WHERE pair=? AND key=? AND new_value IS NOT NULL",
                    (pair, key)).fetchone()
    return int(r[0]) if r and r[0] is not None else None


# --------------------------------------------------------------------------- request parsing
def _parse_until(raw: str, now: int) -> int:
    text = raw.strip()
    m = _UNTIL_REL.fullmatch(text)
    if m:
        span = {"m": MS_PER_MINUTE, "h": MS_PER_HOUR, "d": MS_PER_DAY}[m.group(2)]
        return now + int(float(m.group(1)) * span)
    if text.isdigit() and len(text) >= 12:
        return int(text)
    try:
        when = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise Invalid(f"pair.ai_paused_until {raw!r}: give +6h / +2d / +90m, an ISO UTC time ending in Z, "
                      f"or UTC ms") from None
    if when.tzinfo is None:
        raise Invalid(f"pair.ai_paused_until {raw!r}: no time zone (write it in UTC with a trailing Z)")
    return int(when.timestamp() * 1000)


def parse_value(spec: ad.KeySpec, raw: str, now: int) -> Any:
    if spec.kind == "int":
        try:
            return int(raw.strip(), 10)
        except ValueError:
            raise Invalid(f"{spec.key} {raw!r}: not a whole number") from None
    if spec.kind == "float":
        try:
            v = float(raw.strip())
        except ValueError:
            raise Invalid(f"{spec.key} {raw!r}: not a number") from None
        if not math.isfinite(v):
            raise Invalid(f"{spec.key} {raw!r}: not a finite number")
        return round(v, 4)
    if spec.kind == "until":
        return _parse_until(raw, now)
    if spec.kind == "text":
        return ad.normalize_text(raw)
    raise Invalid("the playbook is set with: tune.py --pair P playbook --text \"...\"")


def _key(raw: str) -> ad.KeySpec:
    spec = ad.KEYS.get(raw.strip())
    if spec is None:
        raise Invalid(f"unknown key {raw!r} (keys: {', '.join(ad.KEYS)})")
    return spec


def _secret_values(s: Settings) -> set[str]:
    """The values of the configured secrets (Settings.secret_env_names(): the environment, else ``.env``), at least
    MIN_SECRET_CHARS long."""
    try:
        from tradingsystem.ai.providers.base import secret
    except Exception:  # noqa: BLE001 — the environment alone then
        secret = None
    values: set[str] = set()
    for name in s.secret_env_names():
        for v in (os.environ.get(name, ""), (secret(name) if secret is not None else None) or ""):
            if len(v.strip()) >= MIN_SECRET_CHARS:
                values.add(v.strip())
    return values


def _guard(s: Settings, what: str, *texts: str | None) -> None:
    """Refuse (exit 3) a free text tune.py would store — in adaptive.yaml, playbook.md, changes.jsonl, tuning_changes,
    the trader prompt and the dashboard — when it holds a configured secret's value or a key / token shape the log
    redactor masks. The text itself is never echoed."""
    items = [x for x in texts if x]
    if not items:
        return
    secrets, redact = _secret_values(s), get_redactor()
    if any(v in x for x in items for v in secrets) or any(redact(x) != x for x in items):
        raise Invalid(f"{what}: contains the value of a configured secret or a key / token-like string - not stored "
                      "(the text is not shown)")


def _change_args(a: argparse.Namespace, s: Settings) -> dict[str, Any]:
    reason = (a.reason or "").strip()
    if len(reason) < 3 or len(reason) > ad.MAX_REASON_CHARS:
        raise Invalid(f"--reason: 3..{ad.MAX_REASON_CHARS} characters")
    _guard(s, "--reason", reason)
    if a.evidence_json is None:
        raise Invalid("--evidence-json is required (the numbers behind the change, as JSON)")
    try:
        evidence = json.loads(a.evidence_json)
    except ValueError as exc:
        raise Invalid(f"--evidence-json is not JSON: {exc}") from None
    except RecursionError:
        raise Invalid(f"--evidence-json: nested at most {ad.MAX_EVIDENCE_DEPTH} levels deep") from None
    if evidence is None or len(json.dumps(evidence)) > ad.MAX_EVIDENCE_CHARS:
        raise Invalid(f"--evidence-json: a JSON value of at most {ad.MAX_EVIDENCE_CHARS} characters")
    if ad.json_depth(evidence) > ad.MAX_EVIDENCE_DEPTH:
        raise Invalid(f"--evidence-json: nested at most {ad.MAX_EVIDENCE_DEPTH} levels deep "
                      "(adaptive.yaml refuses deeper documents)")
    _guard(s, "--evidence-json", a.evidence_json, json.dumps(evidence, ensure_ascii=False, default=str))
    wh = a.window_hours if a.window_hours is not None else s.adaptive.default_window_hours
    if not WINDOW_HOURS[0] <= wh <= WINDOW_HOURS[1]:
        raise Invalid(f"--window-hours {wh}: {WINDOW_HOURS[0]}..{WINDOW_HOURS[1]}")
    days = a.expires_days if a.expires_days is not None else s.adaptive.max_expiry_days
    if not 1 <= days <= s.adaptive.max_expiry_days:
        raise Invalid(f"--expires-days {days}: 1..{s.adaptive.max_expiry_days} (adaptive.max_expiry_days)")
    rid = a.review_id.strip() if a.review_id else None
    if rid is not None and (len(rid) > 120 or any(ch.isspace() for ch in rid)):
        raise Invalid("--review-id: at most 120 characters, no spaces")
    _guard(s, "--review-id", rid)
    return {"reason": reason, "evidence": evidence, "window_hours": int(wh), "expires_days": int(days),
            "review_id": rid}


def _actor(a: argparse.Namespace, s: Settings) -> str:
    actor = (a.actor or "").strip()
    if not ACTOR_RE.fullmatch(actor):
        raise Invalid(f"--actor {a.actor!r}: 1..40 characters of letters, digits and . @ : + - _")
    _guard(s, "--actor", actor)
    return actor


# --------------------------------------------------------------------------- the policy
def _gate(t: Target) -> None:
    """Refusals that do not depend on the request."""
    if ad.freeze_path(t.s).exists():
        raise Refused(f"tuning is frozen by the user ({ad.freeze_path(t.s)} exists)")
    if not t.s.adaptive.enabled:
        raise Refused("adaptive.enabled is false for this pair's system")


def _in_force(t: Target, cfg: ad.AdaptiveCfg, playbook: str, key: str, now: int) -> tuple[Any, ad.Entry | None]:
    """(value the consumers use now, the unexpired overlay entry or None)."""
    eff = ad.compute_effective(t.s, cfg, playbook, now)
    live, _ = ad.active_entries(cfg, now)
    return getattr(eff, EFFECTIVE_FIELD[key]), live.get(key)


def policy_problems(t: Target, con: sqlite3.Connection | None, spec: ad.KeySpec, value: Any, *, cfg: ad.AdaptiveCfg,
                    playbook: str, window_hours: int, now: int) -> list[str]:
    """Every reason the policy refuses setting ``spec.key`` to ``value`` ([] = allowed). ``value`` is the playbook
    text for the playbook."""
    a, key = t.s.adaptive, spec.key
    problems: list[str] = []
    # bounds and content
    if spec.kind in ("int", "float") and not spec.lo <= value <= spec.hi:
        problems.append(f"{key} {value} is outside [{spec.lo}, {spec.hi}]")
    elif spec.kind == "until" and not now < value <= now + ad.MAX_PAUSE_DAYS * MS_PER_DAY:
        problems.append(f"{key} {iso(value)} must be in the future and at most {ad.MAX_PAUSE_DAYS} days ahead")
    elif spec.kind == "text":
        problems += [f"tp_hint: {p}" for p in lint_hint(value)]
    elif spec.kind == "playbook":
        problems += [f"playbook: {p}" for p in lint(value)]
    # direction, against the value in force now
    current, live = _in_force(t, cfg, playbook, key, now)
    if spec.direction and isinstance(value, (int, float)):
        if (spec.direction == "up" and value < current) or (spec.direction == "down" and value > current):
            verb = "raised" if spec.direction == "up" else "lowered"
            problems.append(f"{key} may only be {verb}: it is {current} now, {value} would move it the other way")
        elif value == current and live is None:
            problems.append(f"{key} is already {current} (config) - the change would have no effect")
    # history of the pair
    if con is None:
        problems.append(f"no decision history: {t.app_db} does not exist (the pair's system never ran here)")
        return problems
    n_today = changes_today(con, t.pair, now)
    if n_today >= a.max_changes_per_day:
        problems.append(f"{n_today} change(s) of {t.pair} today already (max {a.max_changes_per_day} per UTC day)")
    last = last_change(con, t.pair, key)
    if last is not None and a.cooldown_days and now - last < a.cooldown_days * MS_PER_DAY:
        problems.append(f"cooldown: {key} changed {iso(last)}; next change from "
                        f"{iso(last + a.cooldown_days * MS_PER_DAY)} ({a.cooldown_days} days per key)")
    start = now - window_hours * MS_PER_HOUR
    need = a.min_samples_strategy if spec.group == "strategy" else a.min_samples_activity
    n = resolved_samples(con, t.pair, start, now)
    if n < need:
        problems.append(f"{n} resolved virtual outcomes of {t.pair} in the last {window_hours} h - a {spec.group} "
                        f"key needs {need}")
    bad = unhealthy_hours(con, t.pair, t.s.pairs[t.pair].decision_timeframe.value, start, now)
    pct = 100.0 * len(bad) / window_hours
    if pct > a.unhealthy_freeze_pct:
        problems.append(f"frozen: {len(bad)} of the last {window_hours} h ({pct:.0f} %) were unhealthy "
                        f"(> {a.unhealthy_freeze_pct:g} %)")
    return problems


# --------------------------------------------------------------------------- commands
def _say(out: TextIO | None, text: str) -> None:
    if out is not None:
        out.write(text + "\n")
        out.flush()


def _display(key: str, value: Any) -> str:
    if value is None or value == "":
        return "(none)"
    if key == "pair.ai_paused_until":
        return str(iso(value))
    if key == "playbook":
        return f"{ad.text_hash(value)} ({len(value)} chars)"
    text = str(value)
    return json.dumps(text, ensure_ascii=False) if isinstance(value, str) else text


def _notify(s: Settings, title: str, text: str, *, key: str, pair: str) -> None:
    """core.notify when it exists (never required: a change is recorded in changes.jsonl and tuning_changes)."""
    try:
        from tradingsystem.core import notify as nt
        nt.notify(s, "info", title, text, key=key, pair=pair)
    except Exception:  # noqa: BLE001
        logging.getLogger("tune").debug("notification not sent", exc_info=True)


def _flush_notify() -> None:
    try:
        from tradingsystem.core import notify as nt
        nt.flush()
    except Exception:  # noqa: BLE001
        pass


def _apply(t: Target, con: sqlite3.Connection, *, key: str, cfg: ad.AdaptiveCfg, new_cfg: ad.AdaptiveCfg,
           old_value: Any, new_value: Any, playbook: str | None, old_playbook: str, row: dict, now: int) -> int:
    """Row first (inside a write transaction), then the files, then COMMIT; the old files come back when anything
    fails. Returns the tuning_changes id."""
    old_yaml = (t.dir / ad.YAML_FILE).read_text(encoding="utf-8") if (t.dir / ad.YAML_FILE).exists() else None
    con.execute("BEGIN IMMEDIATE")
    try:
        con.execute("UPDATE tuning_changes SET reverted_ms=? WHERE pair=? AND key=? AND reverted_ms IS NULL "
                    "AND new_value IS NOT NULL", (now, t.pair, key))
        cur = con.execute(
            "INSERT INTO tuning_changes(ts, pair, key, old_value, new_value, reason, evidence, window_hours, "
            "expires_ms, review_id, actor, reverted_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL)",
            (now, t.pair, key, _json(old_value), _json(new_value), row["reason"], _json(row.get("evidence")),
             row.get("window_hours"), row.get("expires_ms"), row.get("review_id"), row["actor"]))
        cid = int(cur.lastrowid)
        try:
            if playbook is not None:
                _write(t, ad.PLAYBOOK_FILE, playbook + "\n")
            _write(t, ad.YAML_FILE, ad.dump_cfg(new_cfg))
            if key == "playbook" and new_cfg.playbook is None:
                _unlink(t, ad.PLAYBOOK_FILE)            # after the yaml stops referencing it
            con.execute("COMMIT")
        except BaseException:
            _restore(t, old_yaml, old_playbook if cfg.playbook is not None else None)
            raise
        return cid
    except BaseException:
        if con.in_transaction:
            con.execute("ROLLBACK")
        raise


def _restore(t: Target, old_yaml: str | None, old_playbook: str | None) -> None:
    try:
        if old_playbook is not None:
            _write(t, ad.PLAYBOOK_FILE, old_playbook + "\n")
        if old_yaml is not None:
            _write(t, ad.YAML_FILE, old_yaml)
        else:
            _unlink(t, ad.YAML_FILE)
    except Exception:  # noqa: BLE001
        logging.getLogger("tune").warning("could not restore %s", t.dir, exc_info=True)


def _json(v: Any) -> str | None:
    """A value as stored in tuning_changes: JSON text; SQL NULL for "none" (a revert's new_value = the config)."""
    return None if v is None or v == "" else json.dumps(v, ensure_ascii=False, default=str)


def _set(a: argparse.Namespace, t: Target, spec: ad.KeySpec, value: Any, now: int, out: TextIO | None) -> int:
    """Shared by ``set`` and ``playbook`` (``value`` = the playbook text)."""
    args, actor = _change_args(a, t.s), _actor(a, t.s)
    with FileLock(ad.lock_path(t.s, t.pair)).hold(timeout=LOCK_TIMEOUT_S) as got:
        if not got:
            raise Refused(f"busy: another tune.py holds {ad.lock_path(t.s, t.pair)} - try again in a minute")
        _gate(t)
        cfg, old_pb = _current(t)
        con = _db(t)
        try:
            problems = policy_problems(t, con, spec, value, cfg=cfg, playbook=old_pb,
                                       window_hours=args["window_hours"], now=now)
            if problems:
                raise Refused(f"{t.pair} {spec.key} {_display(spec.key, value)}: " + "; ".join(problems))
            old_value, _ = _in_force(t, cfg, old_pb, spec.key, now)
            expires = now + args["expires_days"] * MS_PER_DAY
            stored = ad.text_hash(value) if spec.kind == "playbook" else value
            entry = {"value": stored, "set_ms": now, "expires_ms": expires, "reason": args["reason"],
                     "evidence": args["evidence"], "window_hours": args["window_hours"],
                     "review_id": args["review_id"]}
            try:
                new_cfg = cfg.with_entry(spec.key, entry, max_expiry_days=t.s.adaptive.max_expiry_days)
            except ValueError as exc:
                raise Refused(f"{t.pair} {spec.key}: {ad._short(exc)}") from None
            what = (f"{t.pair} {spec.key} {_display(spec.key, old_value)} -> {_display(spec.key, value)} "
                    f"until {iso(expires)}")
            if a.dry_run:
                _say(out, f"dry-run: would apply: {what}")
                return EXIT_OK
            row = {**args, "expires_ms": expires, "actor": actor}
            cid = _apply(t, con, key=spec.key, cfg=cfg, new_cfg=new_cfg, old_value=old_value or None,
                         new_value=value, playbook=value if spec.kind == "playbook" else None, old_playbook=old_pb,
                         row=row, now=now)
        finally:
            if con is not None:
                con.close()
        _record(t, {"ts": now, "iso": iso(now), "pair": t.pair, "action": "set", "key": spec.key,
                    "old": _line_value(spec.key, old_value), "new": _line_value(spec.key, value), "set_ms": now,
                    "expires_ms": expires, "expires": iso(expires), "reason": args["reason"],
                    "evidence": args["evidence"], "window_hours": args["window_hours"],
                    "review_id": args["review_id"], "actor": actor, "change_id": cid,
                    **({"text": value} if spec.kind == "playbook" else {})})
    _say(out, f"applied: {what} (change {cid})")
    _notify(t.s, f"Tuning {t.pair}", f"{what} - {args['reason'][:300]} (by {actor})",
            key=f"tune:{t.pair}:{spec.key}:{now}", pair=t.pair)
    return EXIT_OK


def _line_value(key: str, value: Any) -> Any:
    if key == "playbook":
        return {"hash": ad.text_hash(value), "chars": len(value)} if value else None
    return value if value != "" else None


def _record(t: Target, record: dict) -> None:
    try:
        _append(t, record)
    except OSError:                                     # the row and the files are the record; the line is a copy
        logging.getLogger("tune").warning("changes.jsonl not written for %s", t.pair, exc_info=True)


def cmd_set(a: argparse.Namespace, t: Target, now: int, out: TextIO | None) -> int:
    spec = _key(a.key)
    if spec.kind == "playbook":
        raise Invalid("the playbook is set with: tune.py --pair P playbook --text \"...\" (a human may also give a "
                      "FILE or - for stdin)")
    value = parse_value(spec, a.value, now)
    if spec.kind == "text":
        _guard(t.s, spec.key, value)
    return _set(a, t, spec, value, now, out)


def _playbook_file(t: Target, source: str) -> Path:
    """The FILE form of ``playbook`` (a human's draft; a session never gets here): a regular file under the data root
    or the checkout, reached without a symlink or junction below that root, whose name is not a secrets file's
    (``.env*``, ``*credential*``). Anything else is refused (exit 3) before it is opened — no network or device path
    is touched — and nothing of a file's content is ever echoed."""
    if source.replace("\\", "/").startswith("//"):
        raise Invalid(f"playbook {source}: network and device paths are refused")
    path = Path(os.path.abspath(source))
    if path.drive.startswith(("\\\\", "//")):
        raise Invalid(f"playbook {source}: network and device paths are refused")
    name = path.name.lower()
    if name.startswith(".env") or "credential" in name:
        raise Invalid(f"playbook {source}: a secrets file ({path.name}) is never read")
    roots = (t.s.paths.data(), CHECKOUT)
    for root in roots:
        real_root = Path(os.path.realpath(root))
        for base in dict.fromkeys((Path(os.path.abspath(root)), real_root)):
            if base not in path.parents:
                continue
            try:
                real = Path(os.path.realpath(path, strict=True))
            except OSError:
                raise Invalid(f"playbook {source}: cannot be read (not found)") from None
            if real != real_root / path.relative_to(base) or path.is_symlink() or path.is_junction():
                raise Invalid(f"playbook {source}: reached through a symlink or junction - refused")
            if not real.is_file():
                raise Invalid(f"playbook {source}: not a regular file")
            return real
    raise Invalid(f"playbook {source}: only a file under the data root or the checkout is read "
                  f"({', '.join(str(r) for r in roots)})")


def cmd_playbook(a: argparse.Namespace, t: Target, now: int, out: TextIO | None, stdin: TextIO | None) -> int:
    if (a.source is None) == (a.text is None):
        raise Invalid("playbook: give --text, a FILE or - (stdin)")
    if a.text is None and os.environ.get(SESSION_ENV) == "1":
        # the session's Read denials (.env, ~/.claude, credential files) must not be bypassed through this tool
        raise Invalid("playbook: FILE and - (stdin) are refused in an operator session - sessions use --text")
    if a.text is not None:
        # a session passes the playbook on ONE command line (the Bash allow-list matches the command text): the two
        # characters "\\n" (backslash, n) stand for a line break
        # when the text has none of its own (as tools/propose.py does)
        raw = a.text if "\n" in a.text else a.text.replace("\\n", "\n")
    elif a.source == "-":
        raw = (stdin or sys.stdin).read(MAX_SOURCE_BYTES + 1)
    else:
        path = _playbook_file(t, a.source)
        try:
            with open(path, encoding="utf-8") as fh:
                raw = fh.read(MAX_SOURCE_BYTES + 1)
        except (OSError, UnicodeDecodeError) as exc:
            raise Invalid(f"playbook {a.source}: cannot be read ({type(exc).__name__})") from None
    if len(raw) > MAX_SOURCE_BYTES:
        raise Invalid(f"playbook: larger than {MAX_SOURCE_BYTES} bytes")
    text = ad.normalize_text(raw)
    _guard(t.s, "playbook", text)
    return _set(a, t, ad.KEYS["playbook"], text, now, out)


def cmd_revert(a: argparse.Namespace, t: Target, now: int, out: TextIO | None) -> int:
    """Always allowed (even frozen or disabled): back to the config value; logged, not a change of the day."""
    spec, actor = _key(a.key), _actor(a, t.s)
    reason = (a.reason or "revert").strip()[:ad.MAX_REASON_CHARS] or "revert"
    _guard(t.s, "--reason", reason)
    with FileLock(ad.lock_path(t.s, t.pair)).hold(timeout=LOCK_TIMEOUT_S) as got:
        if not got:
            raise Refused(f"busy: another tune.py holds {ad.lock_path(t.s, t.pair)} - try again in a minute")
        cfg, old_pb = _current(t, lenient=True)
        entry = cfg.entry(spec.key)
        if entry is None:
            _say(out, f"nothing to revert: {t.pair} {spec.key} has no overlay value")
            return EXIT_OK
        old = old_pb if spec.kind == "playbook" else entry.value
        what = f"{t.pair} {spec.key} {_display(spec.key, old)} -> config value"
        if a.dry_run:
            _say(out, f"dry-run: would revert: {what}")
            return EXIT_OK
        new_cfg = cfg.with_entry(spec.key, None, max_expiry_days=ad.HARD_MAX_EXPIRY_DAYS, lenient=True)
        con = _db(t)
        cid: int | None = None
        try:
            row = {"reason": f"revert: {reason}", "actor": actor}
            if con is not None:
                cid = _apply(t, con, key=spec.key, cfg=cfg, new_cfg=new_cfg, old_value=old, new_value=None,
                             playbook=None, old_playbook=old_pb, row=row, now=now)
            else:                                       # no app.db on this data root: the files and the line only
                _write(t, ad.YAML_FILE, ad.dump_cfg(new_cfg))
                if spec.kind == "playbook":
                    _unlink(t, ad.PLAYBOOK_FILE)
        finally:
            if con is not None:
                con.close()
        _record(t, {"ts": now, "iso": iso(now), "pair": t.pair, "action": "revert", "key": spec.key,
                    "old": _line_value(spec.key, old), "new": None, "set_ms": entry.set_ms,
                    "reason": reason, "actor": actor, "change_id": cid})
    _say(out, f"reverted: {what}" + (f" (change {cid})" if cid is not None else ""))
    _notify(t.s, f"Tuning {t.pair}", f"reverted {what} - {reason[:300]} (by {actor})",
            key=f"tune:{t.pair}:{spec.key}:{now}", pair=t.pair)
    return EXIT_OK


def status(t: Target, now: int) -> dict[str, Any]:
    """Read-only picture of one pair: values in force, entries, policy state (for ``list``)."""
    a = t.s.adaptive
    doc: dict[str, Any] = {"pair": t.pair, "dir": str(t.dir), "enabled": a.enabled,
                           "frozen_by_user": ad.freeze_path(t.s).exists()}
    try:
        cfg, pb = _current(t)
        doc["valid"] = True
    except Refused as exc:
        cfg, pb, doc["valid"], doc["error"] = ad.AdaptiveCfg(), "", False, str(exc)
    eff = ad.compute_effective(t.s, cfg, pb, now)
    doc["effective"] = eff.as_detail()
    doc["entries"] = {k: {"value": _line_value(k, pb) if k == "playbook" else e.value, "set": iso(e.set_ms),
                          "expires": iso(e.expires_ms), "expired": e.expires_ms <= now, "reason": e.reason,
                          "review_id": e.review_id} for k, e in cfg.entries().items()}
    con = None
    try:
        con = _db(t, readonly=True)
    except sqlite3.Error:
        con = None
    try:
        wh = a.default_window_hours
        start = now - wh * MS_PER_HOUR
        bad = unhealthy_hours(con, t.pair, t.s.pairs[t.pair].decision_timeframe.value, start, now) if con else set()
        cool = {}
        for k in ad.KEYS:
            last = last_change(con, t.pair, k)
            if last is not None and now - last < a.cooldown_days * MS_PER_DAY:
                cool[k] = iso(last + a.cooldown_days * MS_PER_DAY)
        doc["policy"] = {
            "window_hours": wh, "resolved_virtual_outcomes": resolved_samples(con, t.pair, start, now),
            "min_samples_strategy": a.min_samples_strategy, "min_samples_activity": a.min_samples_activity,
            "unhealthy_hours": len(bad), "unhealthy_pct": round(100.0 * len(bad) / wh, 1),
            "freeze_above_pct": a.unhealthy_freeze_pct, "changes_today": changes_today(con, t.pair, now),
            "max_changes_per_day": a.max_changes_per_day, "cooldown_until": cool,
            "app_db": str(t.app_db) if con is not None else None}
    except sqlite3.Error as exc:
        doc["policy"] = {"error": str(exc)}
    finally:
        if con is not None:
            con.close()
    # a playbook change as its hash and length ("old" / "new"), never the text (defence in depth: `list` is what a
    # session runs, and the text is in playbook.md and tuning_changes)
    doc["recent_changes"] = [{k: v for k, v in r.items() if k != "text"}
                             for r in ad.read_changes(t.s, t.pair, limit=5)]
    return doc


def cmd_list(a: argparse.Namespace, loader: Loader, now: int, out: TextIO | None) -> int:
    if a.pair:
        targets = [target(a.pair, loader)]
    else:
        base = loader(None)
        pairs = sorted(p for p, c in base.pairs.items() if c.enabled or ad.adaptive_dir(base, p).exists())
        targets = [target(p, loader) for p in pairs]
    docs = [status(t, now) for t in targets]
    if a.json:
        _say(out, json.dumps({"now": iso(now), "pairs": docs}, ensure_ascii=False, default=str, indent=1))
        return EXIT_OK
    for d in docs:
        flags = [] if d["enabled"] else ["adaptive.enabled false"]
        flags += ["TUNING_FREEZE on"] if d["frozen_by_user"] else []
        _say(out, f"## {d['pair']} - {d['dir']}" + (f" - {', '.join(flags)}" if flags else ""))
        if not d.get("valid", True):
            _say(out, f"!! {d['error']}")
        eff = d["effective"]
        for k, spec in ad.KEYS.items():
            e = d["entries"].get(k)
            in_force = eff["playbook_chars"] if k == "playbook" else eff.get(EFFECTIVE_FIELD[k].replace(
                "ai_paused_until_ms", "ai_paused_until"))
            shown = f"{in_force} chars" if k == "playbook" else ("-" if in_force in (None, "") else in_force)
            tail = (f"  overlay {e['value']} set {e['set']} until {e['expires']}"
                    f"{' (EXPIRED)' if e['expired'] else ''} - {e['reason'][:80]}" if e else "")
            _say(out, f"  {k:<26} {shown!s:<26}{tail}".rstrip())
        p = d.get("policy", {})
        if "error" in p:
            _say(out, f"  policy: unavailable ({p['error']})")
        else:
            _say(out, f"  policy (last {p['window_hours']} h): {p['resolved_virtual_outcomes']} resolved virtual "
                      f"outcomes (strategy keys need {p['min_samples_strategy']}, activity keys "
                      f"{p['min_samples_activity']}); unhealthy {p['unhealthy_pct']} % (frozen above "
                      f"{p['freeze_above_pct']:g} %); changes today {p['changes_today']}/{p['max_changes_per_day']}"
                      + ("" if p["app_db"] else " - no app.db for this pair"))
            for k, until in p["cooldown_until"].items():
                _say(out, f"  cooldown: {k} until {until}")
        for r in d["recent_changes"]:
            _say(out, f"  {r.get('iso')} {r.get('action')} {r.get('key')} {r.get('old')} -> {r.get('new')} "
                      f"({r.get('actor')})")
    return EXIT_OK


# --------------------------------------------------------------------------- CLI
def _common(p: argparse.ArgumentParser, *, sub: bool) -> None:
    """--pair / --dry-run / --actor are accepted before and after the command."""
    d = argparse.SUPPRESS
    p.add_argument("--pair", default=d if sub else None, help="the pair (e.g. BTCUSDT)")
    p.add_argument("--dry-run", action="store_true", default=d if sub else False,
                   help="check and print; write nothing")
    p.add_argument("--actor", default=d if sub else "operator", help="who asks (default: operator)")


def _change_opts(p: argparse.ArgumentParser) -> None:
    p.add_argument("--reason", required=True)
    p.add_argument("--evidence-json", dest="evidence_json")
    p.add_argument("--window-hours", dest="window_hours", type=int)
    p.add_argument("--review-id", dest="review_id")
    p.add_argument("--expires-days", dest="expires_days", type=int)


def _parser() -> argparse.ArgumentParser:
    p = _Parser(prog="tune.py", description="Tune one pair's adaptive overlay (docs/learning_loop.md).")
    _common(p, sub=False)
    cmds = p.add_subparsers(dest="cmd", required=True, parser_class=_Parser)
    c = cmds.add_parser("set", help="set a key: " + ", ".join(k for k in ad.KEYS if k != "playbook"))
    _common(c, sub=True)
    c.add_argument("key")
    c.add_argument("value")
    _change_opts(c)
    c = cmds.add_parser("playbook", help="replace the pair's playbook (--text; outside a session also a FILE under "
                                         "the data root or the checkout, or - for stdin)")
    _common(c, sub=True)
    c.add_argument("source", nargs="?")
    c.add_argument("--text")
    _change_opts(c)
    c = cmds.add_parser("revert", help="back to the config value (always allowed)")
    _common(c, sub=True)
    c.add_argument("key")
    c.add_argument("--reason", default="revert")
    c = cmds.add_parser("list", help="values in force, entries, policy state")
    _common(c, sub=True)
    c.add_argument("--json", action="store_true")
    return p


def run(argv: list[str] | None = None, *, loader: Loader | None = None, now_ms: int | None = None,
        stdin: TextIO | None = None, out: TextIO | None = None) -> int:
    """The whole tool without process setup (tests pass ``loader`` / ``now_ms``; the CLI has no clock override)."""
    out = out if out is not None else sys.stdout
    loader = loader or _default_loader
    try:
        a = _parser().parse_args(argv)
        now = int(now_ms) if now_ms is not None else _now_ms()
        if a.cmd == "list":
            return cmd_list(a, loader, now, out)
        t = target(a.pair, loader)
        if a.cmd == "set":
            return cmd_set(a, t, now, out)
        if a.cmd == "playbook":
            return cmd_playbook(a, t, now, out, stdin)
        return cmd_revert(a, t, now, out)
    except SystemExit as exc:                            # --help
        return int(exc.code) if isinstance(exc.code, int) else EXIT_OK
    except Invalid as exc:
        _say(out, f"invalid: {exc}")
        return EXIT_INVALID
    except Refused as exc:
        _say(out, f"refused: {exc}")
        return EXIT_REFUSED
    except Exception as exc:  # noqa: BLE001
        _say(out, f"error: {type(exc).__name__}: {exc}")
        return EXIT_ERROR


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr, sys.stdin):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 — pythonw (None) or a stream without reconfigure
            pass
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    rc = run(argv)
    _flush_notify()
    return rc


if __name__ == "__main__":
    sys.exit(main())
