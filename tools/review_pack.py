"""Review pack (§3.8, read-only): the evidence a review session (or a person) reads, in one bounded file.

    python tools/review_pack.py --hours 24 --kind daily                 → data/reviews/<ts>_daily.md + .json
    python tools/review_pack.py --hours 168 --kind weekly [--pair P] [--out DIR]
    python tools/review_pack.py --hours 6 --pair BTCUSDT --print        → the markdown on stdout, no file written

Contents: machine + per-system health (``tools/health_report.py``, imported — the same lines a person reads), per pair
the funnel (5m screens → triggers by strength → calls by role → answers → ideas → gate by check → placed → outcomes
broker/virtual → decision-metric means), attribution breakdowns (session / regime / setup kind), position actions and
rule executions, escalation verdicts, the adaptive values in force with their expiry (validated by the services' own
reader: an invalid overlay, an expired entry or an unreferenced playbook is never shown as in force), the playbook,
recent tuning changes, the kill switches, log ERROR counts, the last 25 trade ideas, hashes (prompt / library /
playbook / adaptive / config / git), git (sha, last commits, dirty flag — so a session needs no git command), AI usage
by role with the cache-read share, the operator sessions whose usage is unknown (timed out or crashed) and the usage
gauge level. The markdown stays under ~40 k characters (≈ 10 k tokens); the JSON holds the same data structured.

Everything is read through read-only SQLite URIs and plain file reads: the pack never writes a database, an adaptive
file or a switch. ``--kind`` only names the files (``adhoc`` when not given, so an ad-hoc pack is never mistaken for
the scheduled daily review). ``--out`` must be ``data/reviews`` or a directory under it (a UNC or ``//`` path is
refused before it is touched): the operator session's allow-list matches this tool's prefix, and a free directory
would be a file write the session's diff guard never sees — the session runner imports :func:`build_pack` instead.
Exit 0 written/printed, 1 unexpected error, 3 invalid arguments.

Phase 5 (A7/A8): the per-pair data functions take an optional upper bound ``until`` (``tools/demo_report.py`` builds
its ``demo`` report over a closed window on top of them — the pack itself always ends now); a build scans the running
supervisors once (:func:`one_supervisor_scan`: the health report asked twice, and each scan rebuilt psutil's ppid map
once per process on the machine); a weekly pack carries the go-live evidence so far (``tools/go_live_inputs.py`` over
the demo window, docs/go_live_checklist.md) for the weekly review's paragraph.
"""
from __future__ import annotations

import argparse
import collections
import contextlib
import hashlib
import importlib.util
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import types
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator, NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import yaml  # noqa: E402

from tradingsystem.ai.budget import USAGE_UNKNOWN_PREFIX  # noqa: E402
from tradingsystem.core.settings import INSTANCE_ENV, PROJECT_ROOT, Settings, load_settings  # noqa: E402
from tradingsystem.core.timeutil import MS_PER_HOUR, iso  # noqa: E402
from tradingsystem.core.timeutil import now_ms as _now_ms  # noqa: E402

TOOLS = Path(__file__).resolve().parent
MAX_MD_CHARS = 40_000            # ≈ 10 k tokens: the whole pack goes into the session's first message
IDEAS = 25                       # the last trade ideas listed one per line
KINDS = ("daily", "weekly", "diagnose", "adhoc")
TS_FORMAT = "%Y%m%dT%H%M%SZ"     # UTC; no ':' (Windows file names); sorts by time
NAME_RE = re.compile(r"^\d{8}T\d{6}Z_(daily|weekly|diagnose|adhoc)$")
EXIT_OK, EXIT_ERROR, EXIT_INVALID = 0, 1, 3
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
# the engine's screen log line (analysis/engine.py tick):
#   "BTCUSDT 5m close 2026-09-27T11:00:00.000Z: trigger=False (none) …"
_SCREEN_RE = re.compile(r"^(?P<pair>[A-Z0-9]+) (?P<tf>\w+) close \S+: "
                        r"trigger=(?P<fire>True|False) \((?P<strength>[^)]*)\)")
LOG_TAIL_BYTES = 2_000_000       # per log file for the ERROR counts (health_report reads the same tail)
SCREEN_LOG_MAX_BYTES = 60_000_000   # engine.jsonl + rotations read for the screen counts, at most
ADAPTIVE_GROUPS = ("trigger", "pair")   # nested groups of adaptive.yaml (trigger.weak_min, pair.ai_paused_until)
ADAPTIVE_INVALID = "adaptive.yaml invalid (services keep the last good values; config defaults after a restart): "
PLAYBOOK_NOT_IN_FORCE = "on disk, NOT in force (expired / unreferenced / hash mismatch)"
PLAYBOOK_MAX_BYTES = 256 * 1024  # tune.py writes ≤ 1500 characters; a larger playbook.md is reported, not read
ENTRY_STATES = {"expired": "EXPIRED", "disabled": "NOT APPLIED: adaptive.enabled is false",
                "not_applied": "NOT APPLIED: the file is invalid"}
NO_UNTIL = 2 ** 62               # "no upper bound" for the ts < until filters (a pack ends now)


class Invalid(Exception):
    """Bad arguments (exit 3)."""


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise Invalid(f"{self.prog}: {message}")


class Pack(NamedTuple):             # not a dataclass: tests load this file outside sys.modules
    """What :func:`build_pack` produced. ``md_path``/``json_path`` are None with ``write=False``."""
    pack_id: str
    md: str
    data: dict[str, Any]
    ledger: Path | None
    md_path: Path | None = None
    json_path: Path | None = None
    problems: tuple[str, ...] = ()


# --------------------------------------------------------------------------- small helpers
def pack_stamp(now: int) -> str:
    return time.strftime(TS_FORMAT, time.gmtime(now / 1000))


# what a ledger role is (the live review read the trader's 'decision' rows as escalations)
ROLE_NOTES = {"decision": " (the trader's cycle calls)", "agent_per_pair": " (trader calls recorded before Phase 3)",
              "escalation": " (stronger-model confirmations)", "review": " (operator review sessions)",
              "diagnose": " (monitor diagnosis sessions)"}


def reviews_dir(s: Settings) -> Path:
    """``data/reviews`` under the data root (shared by every system; honours a scratch root)."""
    return s.paths.data() / "reviews"


def checked_out_dir(s: Settings, raw: str) -> Path:
    """``--out`` confined to :func:`reviews_dir` or a directory under it (raises :class:`Invalid`). A UNC / ``//``
    path is refused before anything touches it (no outbound SMB connection); the containment is checked lexically
    first (nothing outside is resolved), then once more resolved (a junction inside that points out)."""
    head = raw.lstrip()[:2]
    if len(head) == 2 and all(c in "\\/" for c in head):
        raise Invalid(f"--out {raw!r}: network (UNC) and device paths are refused")
    base = Path(os.path.abspath(reviews_dir(s)))
    out = Path(os.path.abspath(raw))
    if not out.is_relative_to(base) or not out.resolve().is_relative_to(base.resolve()):
        raise Invalid(f"--out {raw!r}: must be {base} or a directory under it")
    return out


def ro(db: Path) -> sqlite3.Connection | None:
    """A read-only connection (never creates the file or runs DDL), None when the database does not exist."""
    if not db.exists():
        return None
    return sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=10)


def _cols(con: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _select(have: set[str], want: Iterable[str]) -> str:
    """Column list that survives an older database: a missing column reads as NULL."""
    return ", ".join(c if c in have else f"NULL AS {c}" for c in want)


def _rows(con: sqlite3.Connection, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
    cur = con.execute(sql, args)
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def _json(text: Any, default: Any = None) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def _hm(ms: int | None) -> str:
    return time.strftime("%m-%d %H:%M", time.gmtime(ms / 1000)) if ms else "-"


def _num(x: Any, nd: int = 2) -> str:
    if x is None:
        return "-"
    if isinstance(x, float):
        return f"{x:.{nd}f}"
    return str(x)


def _mean(xs: Iterable[Any]) -> float | None:
    v = [float(x) for x in xs if x is not None]
    return sum(v) / len(v) if v else None


def _short(text: Any, n: int) -> str:
    t = " ".join(str(text or "").split())
    return t if len(t) <= n else t[: n - 1] + "…"


def _counts(c: collections.Counter, n: int = 12) -> str:
    return ", ".join(f"{k} {v}" for k, v in c.most_common(n)) or "none"


def _upto(until: int | None) -> int:
    """The exclusive upper bound of a window: ``until`` or :data:`NO_UNTIL` (the pack's windows end now)."""
    return NO_UNTIL if until is None else int(until)


@contextlib.contextmanager
def one_supervisor_scan() -> Iterator[None]:
    """For one build, ``procs.running_supervisors()`` scans the machine once and answers every later ask from that
    scan (A7: the health report asks twice — ``systems`` and ``machine_report`` — and on Windows each scan rebuilt
    psutil's ppid map once per process on the machine, ≈ 10.8 s of a pack build on production with 330 processes).
    Only the plain ask is cached (``older_s`` is a start-race question and always scans); the original function is
    restored however the build ends, and a nested use keeps the outer cache."""
    from tradingsystem.supervisor import procs
    orig = procs.running_supervisors
    if getattr(orig, "_one_scan", False):
        yield
        return
    memo: dict[str, dict[int, str | None]] = {}

    def cached(older_s: float | None = None) -> dict[int, str | None]:
        if older_s is not None:
            return orig(older_s)
        if "scan" not in memo:
            memo["scan"] = orig()
        return dict(memo["scan"])

    cached._one_scan = True                   # type: ignore[attr-defined]
    procs.running_supervisors = cached
    try:
        yield
    finally:
        procs.running_supervisors = orig


def health_module() -> types.ModuleType:
    """``tools/health_report.py`` loaded by path (``tools/`` is not a package) — imported, never duplicated."""
    mod = sys.modules.get("ts_tools_health_report")
    if mod is None:
        spec = importlib.util.spec_from_file_location("ts_tools_health_report", TOOLS / "health_report.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        sys.modules["ts_tools_health_report"] = mod
    return mod


def choose_systems(pair: str | None) -> tuple[list[Settings], list[str]]:
    """The systems to report, exactly as the health report picks them (running + expected pairs, or the all-pairs
    system). ``pair``: that pair's system only (its own instance, else the system that trades it)."""
    hr = health_module()
    try:
        return hr.systems(types.SimpleNamespace(instance=pair, all_pairs_system=False))
    except ValueError:                        # not a configured instance: the all-pairs system trades it
        base = load_settings(extra_env={INSTANCE_ENV: ""})
        if pair and pair not in base.pairs:
            raise Invalid(f"--pair {pair}: not a configured pair ({', '.join(sorted(base.pairs))})") from None
        return [base], []


def system_pairs(s: Settings, only: str | None = None) -> list[str]:
    pairs = [s.paths.instance] if s.paths.instance else sorted(s.enabled_pairs())
    return [p for p in pairs if only is None or p == only]


def ledger_path(s: Settings, systems: list[Settings] | None = None) -> Path:
    """The AI usage ledger the systems write: one shared ``data/shared/ai_usage.db`` for per-pair systems (D-042),
    the all-pairs system's own ``app.db`` otherwise (``ai/budget.usage_db``). A tool runs without TS_INSTANCE, so
    ``usage_db(s)`` alone would point at the legacy all-pairs database while the pairs run."""
    systems = systems or [s]
    if any(x.paths.instance for x in systems):
        return s.paths.shared() / "ai_usage.db"
    return systems[0].paths.state() / "app.db"


def git_info(root: Path) -> dict[str, Any]:
    """sha, branch, the last 5 commits and the dirty flag of the checkout — read-only git (no optional locks, so a
    concurrent ``git merge`` by the user never meets our index.lock). The session itself runs no git command."""
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}

    def git(*args: str) -> str:
        p = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=15, env=env, creationflags=_NO_WINDOW)
        if p.returncode != 0:
            raise RuntimeError((p.stderr or p.stdout or f"git exit {p.returncode}").strip()[:200])
        return p.stdout

    try:
        sha = git("rev-parse", "--short", "HEAD").strip()
        branch = git("rev-parse", "--abbrev-ref", "HEAD").strip()
        log = [ln for ln in git("log", "-5", "--format=%h %cs %s").splitlines() if ln.strip()]
        dirty = [ln for ln in git("status", "--porcelain").splitlines() if ln.strip()]
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        return {"error": str(exc)[:200]}
    return {"sha": sha, "branch": branch, "last_commits": log, "dirty": bool(dirty), "dirty_files": len(dirty),
            "dirty_list": dirty[:15]}


def library_hash() -> str | None:
    try:
        from tradingsystem.ai.prompts import library_hash as lh
        return lh()
    except Exception:  # noqa: BLE001 — the pack reports what it can
        return None


# --------------------------------------------------------------------------- usage ledger + gauge
def usage_section(ledger: Path, since: int, until: int | None = None) -> dict[str, Any]:
    """Calls and tokens of the window by role (and by pair), with the cache-read share. ``input_tokens`` of a row
    already includes the cache reads and writes (the claude_code convention), ``cached_tokens`` = the reads.
    ``usage_unknown_sessions``: operator sessions of the window without a result document (their row holds 0 tokens
    and says ``usage_unknown:`` — the real spend is missing from the totals, not zero). ``until``: exclusive end of
    the window (None: now)."""
    con = ro(ledger)
    if con is None:
        return {"ledger": str(ledger), "error": "no ledger yet"}
    hi = _upto(until)
    try:
        have = _cols(con, "ai_usage")
        role = "COALESCE(role, purpose, '-')" if "role" in have else "COALESCE(purpose, '-')"
        turns = "COALESCE(sum(num_turns),0)" if "num_turns" in have else "0"
        api = "COALESCE(sum(api_equivalent_usd),0)" if "api_equivalent_usd" in have else "0"
        rows = con.execute(
            f"SELECT {role} AS r, COALESCE(pair,'-') AS p, count(*), COALESCE(sum(ok),0), "
            f"COALESCE(sum(input_tokens),0), COALESCE(sum(cached_tokens),0), COALESCE(sum(output_tokens),0), "
            f"{turns}, {api} FROM ai_usage "
            "WHERE ts>=? AND ts<? GROUP BY r, p ORDER BY r, p", (since, hi)).fetchall()
        unknown = con.execute("SELECT count(*) FROM ai_usage WHERE ts>=? AND ts<? AND substr(error, 1, ?)=?",
                              (since, hi, len(USAGE_UNKNOWN_PREFIX), USAGE_UNKNOWN_PREFIX)).fetchone()[0] \
            if "error" in have else 0
    except sqlite3.Error as exc:
        return {"ledger": str(ledger), "error": f"unreadable: {exc}"[:200]}
    finally:
        con.close()
    by_role: dict[str, dict[str, Any]] = {}
    by_pair: dict[str, dict[str, Any]] = {}
    tot = {"calls": 0, "ok": 0, "input": 0, "cached": 0, "output": 0, "turns": 0, "api_equivalent_usd": 0.0}
    for r, p, n, ok, inp, cached, out, turns_n, usd in rows:
        for agg in (by_role.setdefault(r, dict.fromkeys(tot, 0)), by_pair.setdefault(p, dict.fromkeys(tot, 0)), tot):
            agg["calls"] += n
            agg["ok"] += ok
            agg["input"] += inp
            agg["cached"] += cached
            agg["output"] += out
            agg["turns"] += turns_n
            agg["api_equivalent_usd"] += float(usd or 0)
    for agg in (*by_role.values(), *by_pair.values(), tot):
        agg["cache_read_share"] = round(agg["cached"] / agg["input"], 3) if agg["input"] else None
        agg["api_equivalent_usd"] = round(agg["api_equivalent_usd"], 2)
    return {"ledger": str(ledger), "by_role": by_role, "by_pair": by_pair, "total": tot,
            "usage_unknown_sessions": unknown}


def gauge_state(s: Settings, ledger: Path, now: int) -> dict[str, Any]:
    """The usage gauge (``ai/usage_gauge.py``) on this ledger; its absence or failure is reported, never fatal."""
    if not ledger.exists():
        return {"error": "no ledger yet"}
    try:
        from tradingsystem.ai.budget import UsageStore
        from tradingsystem.ai.usage_gauge import UsageGauge
    except Exception as exc:  # noqa: BLE001 — the gauge ships with the same phase; an older checkout has none
        return {"error": f"usage gauge unavailable ({type(exc).__name__})"}
    store = None
    try:
        store = UsageStore(ledger)
        st = UsageGauge(s, store).state(now)
        detail = dict(st.as_detail()) if hasattr(st, "as_detail") else dict(vars(st))
        for k in ("level", "enforce", "reason"):           # the session runner decides on these three
            detail.setdefault(k, getattr(st, k, None))
        return detail
    except Exception as exc:  # noqa: BLE001
        return {"error": f"usage gauge failed: {type(exc).__name__}: {exc}"[:200]}
    finally:
        if store is not None:
            try:
                store.close()
            except Exception:  # noqa: BLE001
                pass


# --------------------------------------------------------------------------- logs
def _log_files(logs: Path, stem: str) -> list[Path]:
    """``stem.jsonl`` then its rotations ``stem.jsonl.1`` … (newest first)."""
    out = [logs / f"{stem}.jsonl"]
    i = 1
    while (logs / f"{stem}.jsonl.{i}").exists() and i <= 20:
        out.append(logs / f"{stem}.jsonl.{i}")
        i += 1
    return [f for f in out if f.exists()]


def _log_ms(ts: str) -> int | None:
    """A log line's ISO timestamp (``2026-09-27T11:05:02.123+00:00``) → UTC ms; None when it is not one."""
    try:
        d = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return int(d.timestamp() * 1000) if d.tzinfo is not None else None


def screens(logs: Path, pair: str, since: int, until: int | None = None,
            slot_ms: int | None = None) -> dict[str, Any]:
    """Screen closes and triggers of the window from the engine's own log lines (the engine stores no row for a
    screen that did not call): per timeframe, and the fired ones by strength. ``until``: exclusive end (None: now).
    ``slot_ms`` (the demo report's availability): also ``slots`` — the starts of the ``slot_ms`` slots in which the
    engine logged a screen of the pair — and ``oldest_read``, the oldest screen line read (the log reaches back to
    there; a slot before it is unknown, not empty)."""
    since_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(since / 1000))
    until_iso = None if until is None else time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(until / 1000))
    by_tf: collections.Counter = collections.Counter()
    fired: collections.Counter = collections.Counter()
    slots: set[int] = set()
    oldest_all: str | None = None
    read = 0
    needle = f'"{pair} '.encode()
    files = _log_files(logs, "engine")
    for f in files:
        oldest = None
        try:
            size = f.stat().st_size
            with f.open("rb") as fh:                # streamed line by line: the machine has little free RAM
                if read + size > SCREEN_LOG_MAX_BYTES:
                    fh.seek(max(0, size - (SCREEN_LOG_MAX_BYTES - read)))
                    fh.readline()                   # the partial first line
                for raw in fh:
                    read += len(raw)
                    if b"trigger=" not in raw or needle not in raw:
                        continue
                    try:
                        j = json.loads(raw)
                    except ValueError:
                        continue
                    ts = str(j.get("ts", ""))
                    oldest = ts if oldest is None or ts < oldest else oldest
                    if ts < since_iso or (until_iso is not None and ts[:19] >= until_iso):
                        continue
                    m = _SCREEN_RE.match(str(j.get("msg", "")))
                    if not m or m.group("pair") != pair:
                        continue
                    by_tf[m.group("tf")] += 1
                    if m.group("fire") == "True":
                        fired[m.group("strength") or "-"] += 1
                    if slot_ms and (t := _log_ms(ts)) is not None:
                        slots.add(t // slot_ms * slot_ms)
        except OSError:
            continue
        if oldest is not None and (oldest_all is None or oldest < oldest_all):
            oldest_all = oldest
        if (oldest is not None and oldest < since_iso) or read >= SCREEN_LOG_MAX_BYTES:
            break                                  # this file already reaches back before the window
    out = {"closes_by_tf": dict(by_tf), "screens": sum(by_tf.values()), "triggers_by_strength": dict(fired),
           "log": str(files[0]) if files else None}
    if slot_ms:
        out.update(slots=sorted(slots), oldest_read=oldest_all)
    return out


def log_errors(dirs: Iterable[Path], since: int) -> dict[str, int]:
    """ERROR/CRITICAL lines of the window per log file (the tail each health report reads)."""
    since_iso = time.strftime("%Y-%m-%dT%H:%M", time.gmtime(since / 1000))
    out: dict[str, int] = {}
    seen: set[Path] = set()
    for d in dirs:
        for f in sorted(d.glob("*.jsonl")) if d.exists() else []:
            if f.resolve() in seen:
                continue
            seen.add(f.resolve())
            n = 0
            try:
                with f.open("rb") as fh:
                    fh.seek(max(0, f.stat().st_size - LOG_TAIL_BYTES))
                    for line in fh.read().decode("utf-8", "replace").splitlines():
                        if '"level": "ERROR"' not in line and '"level": "CRITICAL"' not in line:
                            continue
                        try:
                            j = json.loads(line)
                        except ValueError:
                            continue
                        if str(j.get("ts", "")) >= since_iso:
                            n += 1
            except OSError:
                continue
            if n:
                rel = f"{d.name}/{f.name}" if d.name not in ("logs", "") else f.name
                out[rel] = out.get(rel, 0) + n
    return out


# --------------------------------------------------------------------------- adaptive overlay (read directly)
def _flatten_entries(doc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for k, v in doc.items():
        if k in ADAPTIVE_GROUPS and isinstance(v, dict):
            for k2, v2 in v.items():
                if isinstance(v2, dict) and "value" in v2:
                    out[f"{k}.{k2}"] = v2
        elif isinstance(v, dict) and "value" in v:
            out[k] = v
    return out


def _text_hash(text: str) -> str:
    """The playbook hash as the services compute it (LF line ends, stripped; sha256, 16 hex)."""
    norm = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]


def _file_sha(path: Path) -> str:
    """sha256 (16 hex) of a file, streamed: an oversized file is hashed, never held in memory."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def _safe_iso(ms: Any) -> str | None:
    """``iso`` of an epoch-ms value from a file nobody validated (None / out-of-range values never raise)."""
    if not isinstance(ms, int) or isinstance(ms, bool):
        return None
    try:
        return iso(ms)
    except (OverflowError, OSError, ValueError):
        return str(ms)


def _entry(key: str, value: Any, set_ms: Any, expires_ms: Any, reason: Any, review_id: Any, window_hours: Any,
           state: str) -> dict[str, Any]:
    """One overlay entry for the pack. ``state``: ``in_force``, ``expired``, ``disabled`` (``adaptive.enabled``
    false) or ``not_applied`` (the services refuse the file) — only ``in_force`` is applied by the services."""
    if key == "pair.ai_paused_until":
        value = _safe_iso(value) or value
    if value is not None and not isinstance(value, (bool, int, float)):
        value = _short(value, 160)
    return {"key": str(key), "value": value, "set": _safe_iso(set_ms), "expires": _safe_iso(expires_ms),
            "in_force": state == "in_force", "state": state, "reason": _short(reason, 160),
            "review_id": None if review_id is None else _short(review_id, 120),
            "window_hours": window_hours if isinstance(window_hours, int) else None}


def _raw_entries(text: str, ad: types.ModuleType) -> list[dict[str, Any]]:
    """What an INVALID ``adaptive.yaml`` holds, listed as NOT applied. Parsed only behind the services' own size
    and alias guard: a merge-key bomb or deep nesting must neither stall nor crash the pack (nothing is listed
    then — the error line says why)."""
    if len(text) > ad.MAX_YAML_CHARS:
        return []
    try:
        if ad.has_alias(text):
            return []
        doc = yaml.safe_load(text)
    except (yaml.YAMLError, RecursionError, ValueError):
        return []
    if not isinstance(doc, dict):
        return []
    return [_entry(k, e.get("value"), e.get("set_ms"), e.get("expires_ms"), e.get("reason"), e.get("review_id"),
                   e.get("window_hours"), "not_applied")
            for k, e in sorted(_flatten_entries(doc).items(), key=lambda kv: str(kv[0]))]


def adaptive_info(s: Settings, pair: str, now: int) -> dict[str, Any]:
    """``data/adaptive/<PAIR>/`` as the services see it: the overlay goes through their own side-effect-free
    reader (``core.adaptive.read_files``: size, alias, duplicate-key, schema, expiry-bound and playbook-hash checks)
    and ``compute_effective``. An entry is in force only while it is live and ``adaptive.enabled`` is on; an invalid
    file is reported (the services keep their last good values — the config defaults after a restart) and its
    entries are listed as NOT applied. playbook.md is the playbook in force only when the effective playbook is
    non-empty; otherwise it is labelled :data:`PLAYBOOK_NOT_IN_FORCE`. No lock, no ``expired`` line, no database
    write — the pack must work whatever state the overlay module or the files are in."""
    d = s.paths.data() / "adaptive" / pair
    out: dict[str, Any] = {"dir": str(d), "enabled": s.adaptive.enabled, "entries": [], "effective": None,
                           "playbook": None, "changes": [], "file_hash": None}
    y = d / "adaptive.yaml"
    eff = None
    try:
        from tradingsystem.core import adaptive as ad
    except Exception as exc:  # noqa: BLE001 — the pack reports what it can
        ad = None
        out["error"] = f"adaptive validator unavailable ({type(exc).__name__}) — nothing is shown as in force"
    if ad is not None:
        try:
            if y.exists() and y.stat().st_size > 4 * ad.MAX_YAML_CHARS:     # ≥ 1 byte per character: never read
                raise ValueError(f"adaptive.yaml is larger than {ad.MAX_YAML_CHARS} characters")
            cfg, playbook = ad.read_files(s, pair, d)
            eff = ad.compute_effective(s, cfg, playbook, now)
            live, _ = ad.active_entries(cfg, now)
            for key, e in sorted(cfg.entries().items()):
                state = "expired" if key not in live else "in_force" if s.adaptive.enabled else "disabled"
                out["entries"].append(_entry(key, e.value, e.set_ms, e.expires_ms, e.reason, e.review_id,
                                             e.window_hours, state))
            out["effective"] = eff.as_detail()
        except (ValueError, yaml.YAMLError, RecursionError) as exc:
            eff, out["entries"] = None, []
            out["error"] = (ADAPTIVE_INVALID + " ".join(str(exc).split()))[:400]
        except OSError as exc:
            eff, out["entries"] = None, []
            out["error"] = f"adaptive files unreadable: {exc}"[:300]
        except Exception as exc:  # noqa: BLE001 — a validator bug is a problem line, never a failed pack
            eff, out["entries"] = None, []
            out["error"] = (f"adaptive validation failed ({type(exc).__name__}: {exc}) — nothing is shown as in "
                            "force")[:300]
    if y.exists():
        try:
            out["file_hash"] = _file_sha(y)
            if eff is None and ad is not None and y.stat().st_size <= 4 * ad.MAX_YAML_CHARS:
                out["entries"] = _raw_entries(y.read_bytes().decode("utf-8", "replace"), ad)
        except OSError as exc:
            out.setdefault("error", f"adaptive.yaml unreadable: {exc}"[:300])
    pbf = d / "playbook.md"
    if eff is not None and eff.playbook:
        out["playbook"] = {"in_force": True, "hash": eff.playbook_hash, "chars": len(eff.playbook),
                           "text": eff.playbook[:1600]}
    elif pbf.exists():
        try:
            if pbf.stat().st_size > PLAYBOOK_MAX_BYTES:
                out["playbook"] = {"in_force": False, "error": f"larger than {PLAYBOOK_MAX_BYTES} bytes — not read"}
            else:
                text = pbf.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n").strip()
                out["playbook"] = {"in_force": False, "state": PLAYBOOK_NOT_IN_FORCE, "hash": _text_hash(text),
                                   "chars": len(text), "text": text[:1600]}
        except OSError as exc:
            out["playbook"] = {"in_force": False, "error": str(exc)[:120]}
    ch = d / "changes.jsonl"
    if ch.exists():
        try:
            lines = ch.read_text(encoding="utf-8", errors="replace").split("\n")    # not splitlines(): U+2028
        except OSError:
            lines = []
        recs = []
        for line in lines[-40:]:
            try:
                rec = json.loads(line)
            except ValueError:
                continue                            # a torn last line (crash mid-append) or a bad one: skipped
            if isinstance(rec, dict):
                recs.append(rec)
        out["changes"] = [{k: (_short(v, 160) if isinstance(v, str) else v) for k, v in r.items()
                           if k not in ("evidence",)} for r in recs[-8:]]
    return out


# --------------------------------------------------------------------------- one pair
_DEC_COLS = ("id", "ts", "trigger", "trigger_strength", "status", "decision", "order_type", "confidence", "rr_computed",
             "execution_state", "execution_detail", "outcome", "outcome_pnl_usd", "virtual_outcome", "virtual_r",
             "errors", "setup_kinds", "session", "regime", "htf_bias", "data_warnings", "playbook_hash",
             "adaptive_hash", "prompt_hash", "library_hash", "input_tokens", "output_tokens", "latency_ms",
             "recommendation", "model")
_MET_COLS = ("decision_id", "mfe_r", "mae_r", "tp1_hit", "tp2_hit", "tp3_hit", "minutes_to_resolve", "exit_reason",
             "slippage", "spread_at_gate", "commission", "swap", "rejected_but_virtual_win",
             "no_trade_counterfactual_atr")


def _gate_failures(detail: dict[str, Any]) -> list[str]:
    gate = detail.get("gate") if isinstance(detail, dict) else None
    if isinstance(gate, list) and gate:
        return [str(g.get("check")) for g in gate if isinstance(g, dict) and not g.get("ok")]
    reason = (detail or {}).get("reason") if isinstance(detail, dict) else None
    return [x.split(" ")[0].rstrip(":") for x in str(reason or "").split("; ") if x][:3]


def _breakdown(ideas: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    """Ideas grouped by an attribution value: count, resolved virtual outcomes, TP1-first share, mean virtual R."""
    groups: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for d in ideas:
        v = d.get(key)
        vals = v if isinstance(v, list) else [v]
        for x in vals or [None]:
            groups[str(x) if x else "-"].append(d)
    out = {}
    for k, ds in groups.items():
        res = [d for d in ds if d.get("virtual_outcome") in ("tp1_first", "sl_first")]
        wins = sum(1 for d in res if d["virtual_outcome"] == "tp1_first")
        out[k] = {"ideas": len(ds), "resolved": len(res), "tp1_first_share": round(wins / len(res), 2) if res else None,
                  "mean_virtual_r": _round(_mean(d.get("virtual_r") for d in res))}
    return out


def _round(x: float | None, nd: int = 2) -> float | None:
    return None if x is None else round(x, nd)


def pair_report(s: Settings, pair: str, since: int, now: int, usage_rows: dict[str, dict[str, Any]],
                until: int | None = None, screen_slot_ms: int | None = None) -> dict[str, Any]:
    """The funnel and the rest of the per-pair evidence from the pair's system database (read-only). ``until``:
    exclusive end of the window (None: now — the pack); the adaptive values and the kill switch are always today's.
    ``screen_slot_ms``: :func:`screens` also returns the slots with a screen line (the demo report's availability)."""
    db = s.paths.state() / "app.db"
    hi = _upto(until)
    rep: dict[str, Any] = {"pair": pair, "db": str(db), "system": s.paths.instance or "all-pairs"}
    rep["screens"] = screens(s.paths.logs(), pair, since, until, slot_ms=screen_slot_ms)
    rep["calls_by_role"] = usage_rows
    rep["adaptive"] = adaptive_info(s, pair, now)
    rep["kill_switch"] = _kill_switch(s, pair)
    con = ro(db)
    if con is None:
        rep["error"] = "no app.db (never started)"
        return rep
    try:
        have = _cols(con, "ai_decisions")
        if not have:
            rep["error"] = "no ai_decisions table"
            return rep
        decs = _rows(con, f"SELECT {_select(have, _DEC_COLS)} FROM ai_decisions WHERE pair=? AND ts>=? AND ts<? "
                          "ORDER BY ts", (pair, since, hi))
        for d in decs:
            d["execution_detail"] = _json(d.get("execution_detail"), {})
            d["setup_kinds"] = _json(d.get("setup_kinds"), d.get("setup_kinds"))
            d["errors"] = _json(d.get("errors"), [])
        mhave = _cols(con, "decision_metrics")
        metrics = {}
        if mhave:
            ids = [d["id"] for d in decs]
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                q = (f"SELECT {_select(mhave, _MET_COLS)} FROM decision_metrics "
                     f"WHERE decision_id IN ({','.join('?' * len(chunk))})")
                for m in _rows(con, q, tuple(chunk)):
                    metrics[m["decision_id"]] = m
        rep["funnel"] = _funnel(decs, metrics)
        ideas = [d for d in decs if d.get("decision") in ("BUY", "SELL")]
        rep["attribution"] = {k: _breakdown(ideas, k) for k in ("session", "regime", "setup_kinds", "trigger_strength")}
        rep["position_actions"] = _position_actions(con, pair, since, until)
        rep["rule_executions"] = _rule_executions(con, pair, since, until)
        rep["escalations"] = {**_escalations(con, pair, since, until), "enabled": s.ai.escalation.enabled}
        rep["tuning_changes"] = _tuning_changes(con, pair, since, until)
        rep["prompt_versions"] = _prompt_versions(con, since, until)
        # the hashes in force = the latest decision that recorded them; the memory = the latest valid answer
        hcols = ("ts", "prompt_hash", "library_hash", "playbook_hash", "adaptive_hash")
        last = _rows(con, f"SELECT {_select(have, hcols)} FROM ai_decisions WHERE pair=? AND ts<? "
                          f"{'AND prompt_hash IS NOT NULL ' if 'prompt_hash' in have else ''}ORDER BY ts DESC LIMIT 1",
                     (pair, hi))
        memo = _rows(con, "SELECT ts, recommendation FROM ai_decisions WHERE pair=? AND status='valid' AND ts<? "
                          "ORDER BY ts DESC LIMIT 1", (pair, hi))
        rep["last_valid"] = {**(last[0] if last else {}), "ts": iso(last[0]["ts"]) if last else None}
        if memo:
            rec = _json(memo[0]["recommendation"], {}) or {}
            rep["last_valid"].update(notes_ts=iso(memo[0]["ts"]), operator_notes=_short(rec.get("operator_notes"), 600))
        rep["ideas"] = [_idea(d, metrics.get(d["id"])) for d in ideas]
    except sqlite3.Error as exc:
        rep["error"] = f"database unreadable: {exc}"[:200]
    finally:
        con.close()
    return rep


def _funnel(decs: list[dict[str, Any]], metrics: dict[str, dict[str, Any]]) -> dict[str, Any]:
    ideas = [d for d in decs if d.get("decision") in ("BUY", "SELL")]
    gate_fail: collections.Counter = collections.Counter()
    for d in ideas:
        if d.get("execution_state") == "rejected":
            gate_fail.update(_gate_failures(d.get("execution_detail") or {}))
    placed = [d for d in ideas if d.get("execution_state") == "executed"]
    broker: dict[str, dict[str, Any]] = {}
    for d in decs:
        if d.get("outcome"):
            b = broker.setdefault(d["outcome"], {"n": 0, "pnl_usd": 0.0})
            b["n"] += 1
            b["pnl_usd"] = round(b["pnl_usd"] + float(d.get("outcome_pnl_usd") or 0), 2)
    virt = collections.Counter(d.get("virtual_outcome") for d in ideas if d.get("virtual_outcome"))
    resolved = [d for d in ideas if d.get("virtual_outcome") in ("tp1_first", "sl_first")]
    ms = [metrics[d["id"]] for d in decs if d["id"] in metrics]
    ms_ideas = [m for m in ms if m.get("mae_r") is not None]            # a fill-bar stop-out has mae_r, mfe_r 0
    exit_reasons = collections.Counter(m.get("exit_reason") for m in ms if m.get("exit_reason"))
    nt = [m.get("no_trade_counterfactual_atr") for m in ms if m.get("no_trade_counterfactual_atr") is not None]
    # slippage is stored as fill - requested (+ = worse for a BUY, better for a SELL): signed by the side so that + is
    # always against the trade (as detail.slippage_adverse), else a BUY and a SELL filled equally worse cancel out
    adverse = [float(metrics[d["id"]]["slippage"]) * (1.0 if d["decision"] == "BUY" else -1.0) for d in ideas
               if d["id"] in metrics and metrics[d["id"]].get("slippage") is not None]
    # commission / swap NULL = unknown (an MT5 trade settled before Phase 4, a venue that gave none), never 0
    cost = {k: [float(m[k]) for m in ms if m.get(k) is not None] for k in ("commission", "swap")}
    return {
        "calls_stored": len(decs),
        "by_trigger_strength": dict(collections.Counter(d.get("trigger_strength") or "-" for d in decs)),
        "by_status": dict(collections.Counter(d.get("status") or "-" for d in decs)),
        "decisions": dict(collections.Counter(d.get("decision") or "-" for d in decs if d.get("status") == "valid")),
        "ideas": len(ideas),
        "execution": dict(collections.Counter(d.get("execution_state") or "-" for d in ideas)),
        "gate_failures_by_check": dict(gate_fail),
        "placed": len(placed),
        "broker_outcomes": broker,
        "virtual_outcomes": dict(virt),
        "virtual_resolved": len(resolved),
        "virtual_tp1_first": sum(1 for d in resolved if d["virtual_outcome"] == "tp1_first"),
        "mean_virtual_r": _round(_mean(d.get("virtual_r") for d in resolved)),
        "metrics": {
            "n": len(ms), "n_with_excursion": len(ms_ideas),
            "mean_mfe_r": _round(_mean(m.get("mfe_r") for m in ms_ideas)),
            "mean_mae_r": _round(_mean(m.get("mae_r") for m in ms_ideas)),
            "tp1_hit_share": _round(_mean(m.get("tp1_hit") for m in ms_ideas if m.get("tp1_hit") is not None)),
            "tp2_hit_share": _round(_mean(m.get("tp2_hit") for m in ms_ideas if m.get("tp2_hit") is not None)),
            "tp3_hit_share": _round(_mean(m.get("tp3_hit") for m in ms_ideas if m.get("tp3_hit") is not None)),
            "mean_minutes_to_resolve": _round(_mean(m.get("minutes_to_resolve") for m in ms), 0),
            "exit_reasons": dict(exit_reasons),
            "mean_slippage_adverse": _round(_mean(adverse), 6), "slippage_n": len(adverse),
            "mean_spread_at_gate": _round(_mean(m.get("spread_at_gate") for m in ms), 6),
            "commission": _round(sum(cost["commission"])) if cost["commission"] else None,
            "commission_n": len(cost["commission"]),
            "swap": _round(sum(cost["swap"])) if cost["swap"] else None, "swap_n": len(cost["swap"]),
            "rejected_but_virtual_win": sum(1 for m in ms if m.get("rejected_but_virtual_win")),
            "no_trade_n": len(nt), "no_trade_mean_counterfactual_atr": _round(_mean(nt)),
        },
        "tokens": {"input": sum(int(d.get("input_tokens") or 0) for d in decs),
                   "output": sum(int(d.get("output_tokens") or 0) for d in decs)},
        "mean_latency_s": _round(lat / 1000, 0) if (lat := _mean(d.get("latency_ms") for d in decs
                                                                 if d.get("latency_ms"))) is not None else None,
    }


def _idea(d: dict[str, Any], m: dict[str, Any] | None) -> dict[str, Any]:
    rec = _json(d.get("recommendation"), {}) or {}
    tps = [t.get("price") for t in rec.get("take_profits") or [] if isinstance(t, dict)]
    entry = rec.get("entry") or {}
    return {
        "id": d["id"][:8], "ts": d["ts"], "time": iso(d["ts"]), "pair": None, "decision": d.get("decision"),
        "order_type": d.get("order_type"), "confidence": d.get("confidence"), "rr": _round(d.get("rr_computed")),
        "entry": entry.get("price") or ([entry.get("range_min"), entry.get("range_max")] if entry else None),
        "sl": rec.get("stop_loss"), "tps": tps, "trigger_strength": d.get("trigger_strength"),
        "status": d.get("status"), "execution": d.get("execution_state"),
        "gate_failures": (_gate_failures(d.get("execution_detail") or {})
                          if d.get("execution_state") == "rejected" else []),
        "outcome": d.get("outcome"), "pnl_usd": d.get("outcome_pnl_usd"), "virtual_outcome": d.get("virtual_outcome"),
        "virtual_r": _round(d.get("virtual_r")), "session": d.get("session"), "regime": d.get("regime"),
        "htf_bias": d.get("htf_bias"), "setup_kinds": d.get("setup_kinds"),
        "escalation": next((e for e in d.get("errors") or [] if str(e).startswith("escalation")), None),
        "mfe_r": (m or {}).get("mfe_r"), "mae_r": (m or {}).get("mae_r"), "exit_reason": (m or {}).get("exit_reason"),
        "summary": _short(rec.get("market_summary"), 140),
    }


def _position_actions(con: sqlite3.Connection, pair: str, since: int, until: int | None = None) -> dict[str, Any]:
    have = _cols(con, "position_actions")
    if not have:
        return {"n": 0, "note": "no position_actions table"}
    rows = _rows(con, "SELECT ts, source, action, status, target_decision, leg, requested, detail "
                      "FROM position_actions WHERE pair=? AND ts>=? AND ts<? ORDER BY ts DESC",
                 (pair, since, _upto(until)))
    by = collections.Counter(f"{r['source']}/{r['action']}/{r['status']}" for r in rows)
    last = []
    for r in rows[:8]:
        det = _json(r.get("detail"), {}) or {}
        why = det.get("reason") or det.get("error") or det.get("note") if isinstance(det, dict) else None
        last.append({"time": iso(r["ts"]), "source": r["source"], "action": r["action"], "status": r["status"],
                     "target": str(r["target_decision"])[:8], "leg": r["leg"],
                     "requested": _short(r.get("requested"), 100), "why": _short(why, 120) if why else None,
                     "dry_run": bool(det.get("dry_run")) if isinstance(det, dict) else False})
    return {"n": len(rows), "by_source_action_status": dict(by), "last": last}


def _rule_executions(con: sqlite3.Connection, pair: str, since: int, until: int | None = None) -> dict[str, Any]:
    if not _cols(con, "management_state"):
        return {"n": 0, "note": "no management_state table"}
    rows = _rows(con, "SELECT m.status AS status, m.rule_idx AS rule_idx, m.applied_ms AS applied_ms "
                      "FROM management_state m JOIN ai_decisions d ON d.id = m.decision_id "
                      "WHERE d.pair=? AND COALESCE(m.applied_ms, m.last_bar_ms, 0)>=? "
                      "AND COALESCE(m.applied_ms, m.last_bar_ms, 0)<?", (pair, since, _upto(until)))
    return {"n": len(rows), "by_status": dict(collections.Counter(r["status"] for r in rows)),
            "applied": sum(1 for r in rows if r.get("applied_ms"))}


def _escalations(con: sqlite3.Connection, pair: str, since: int, until: int | None = None) -> dict[str, Any]:
    if not _cols(con, "ai_sub_outputs"):
        return {"n": 0}
    rows = _rows(con, "SELECT s.ok AS ok, s.output AS output, s.errors AS errors, s.label AS label, s.model AS model, "
                      "d.ts AS ts, d.decision AS decision, d.id AS id FROM ai_sub_outputs s JOIN ai_decisions d "
                      "ON d.id = s.decision_id WHERE s.role='escalation' AND d.pair=? AND d.ts>=? AND d.ts<? "
                      "ORDER BY d.ts DESC", (pair, since, _upto(until)))
    verdicts: collections.Counter = collections.Counter()
    last = []
    for r in rows:
        out = _json(r.get("output"), {}) or {}
        v = out.get("verdict") if r.get("ok") else "invalid"
        verdicts[v or "-"] += 1
        if len(last) < 5:
            last.append({"time": iso(r["ts"]), "decision_id": str(r["id"])[:8], "verdict": v, "label": r.get("label"),
                         "model": r.get("model"), "issues": [_short(x, 120) for x in (out.get("issues") or [])[:3]],
                         "final_decision": (out.get("final_recommendation") or {}).get("decision")})
    return {"n": len(rows), "verdicts": dict(verdicts), "last": last}


def _tuning_changes(con: sqlite3.Connection, pair: str, since: int, until: int | None = None) -> list[dict[str, Any]]:
    """The window's tuning changes plus the most recent ones before it (cooldown and one-per-day context); none made
    after ``until``. ``reverted`` is shown only when the revert happened before ``until``."""
    if not _cols(con, "tuning_changes"):
        return []
    hi = _upto(until)
    rows = _rows(con, "SELECT ts, key, old_value, new_value, reason, window_hours, expires_ms, review_id, actor, "
                      "reverted_ms FROM tuning_changes WHERE pair=? AND ts<? ORDER BY ts DESC LIMIT 12", (pair, hi))
    return [{"time": iso(r["ts"]), "in_window": r["ts"] >= since, "key": r["key"], "old": r["old_value"],
             "new": _short(r["new_value"], 80), "reason": _short(r["reason"], 140), "expires": iso(r["expires_ms"]),
             "review_id": r["review_id"], "actor": r["actor"],
             "reverted": iso(r["reverted_ms"]) if r["reverted_ms"] and r["reverted_ms"] < hi else None}
            for r in rows]


def _prompt_versions(con: sqlite3.Connection, since: int, until: int | None = None) -> list[dict[str, Any]]:
    if not _cols(con, "prompt_versions"):
        return []
    rows = _rows(con, "SELECT prompt_hash, role, library_hash, versions, git_sha, first_seen_ms FROM prompt_versions "
                      "WHERE first_seen_ms>=? AND first_seen_ms<? ORDER BY first_seen_ms DESC LIMIT 8",
                 (since, _upto(until)))
    return [{"prompt_hash": r["prompt_hash"], "role": r["role"], "library_hash": r["library_hash"],
             "versions": _json(r["versions"], r["versions"]), "git_sha": r["git_sha"],
             "first_seen": iso(r["first_seen_ms"])} for r in rows]


def _kill_switch(s: Settings, pair: str | None) -> dict[str, Any]:
    try:
        from tradingsystem.core.killswitch import kill_switch_path, read_reason
    except Exception:  # noqa: BLE001
        return {}
    p = kill_switch_path(s, pair)
    return {"on": p.exists(), "path": str(p), **({"reason": read_reason(p)} if p.exists() else {})}


# --------------------------------------------------------------------------- operator context
def _jsonl_tail(path: Path, n: int) -> list[dict[str, Any]]:
    """The last ``n`` records of an append-only jsonl; a torn or bad line is skipped."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").split("\n")      # not splitlines(): U+2028
    except OSError:
        return []
    out = []
    for line in lines[-(n * 4):]:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out[-n:]


def operator_context(s: Settings, out_dir: Path | None) -> dict[str, Any]:
    """What the previous operator sessions left: open proposals (so none is proposed twice), the last sessions'
    status and summary (did yesterday's change help?), and the monitor's state file (the findings behind a
    diagnosis)."""
    props = [{k: (_short(v, 160) if isinstance(v, str) else v) for k, v in r.items() if k != "body"}
             for r in _jsonl_tail(s.paths.shared() / "proposals.jsonl", 10)]
    sessions = []
    rdir = out_dir or reviews_dir(s)
    for f in sorted(rdir.glob("*.session.json"), reverse=True)[:3] if rdir.exists() else []:
        try:
            doc = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        sessions.append({"review_id": doc.get("review_id") or f.name.split(".")[0], "status": doc.get("status"),
                         "ended": doc.get("ended"), "turns": (doc.get("result") or {}).get("num_turns"),
                         "summary": _short(doc.get("summary"), 400)})
    mon = None
    mf = s.paths.shared() / "monitor_state.json"
    if mf.exists():
        try:
            mon = _short(mf.read_text(encoding="utf-8", errors="replace"), 3000)
        except OSError:
            mon = None
    return {"proposals": props, "previous_sessions": sessions, "monitor_state": mon}


def go_live_evidence(s: Settings, now: int) -> dict[str, Any]:
    """The weekly pack's go-live evidence so far (A8): ``tools/go_live_inputs.py`` over the demo window
    (``evaluation``), cut at ``now`` — the numbers behind the weekly review's "go-live evidence so far" paragraph. A
    failure is one line in the pack, never a failed pack."""
    try:
        mod = sys.modules.get("ts_tools_go_live_inputs")
        if mod is None:
            spec = importlib.util.spec_from_file_location("ts_tools_go_live_inputs", TOOLS / "go_live_inputs.py")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            sys.modules["ts_tools_go_live_inputs"] = mod
        return mod.evidence(s, now=now)
    except Exception as exc:  # noqa: BLE001 — the pack reports what it can
        return {"error": f"{type(exc).__name__}: {exc}"[:300]}


# --------------------------------------------------------------------------- health (tools/health_report.py)
def health(systems: list[Settings], notes: list[str], hours: float, now: int) -> dict[str, Any]:
    """The health report's own lines: machine (MT5, supervisors, recorder, ledger) and per system. A failing system
    report (locked or damaged database) is one problem line, not a failed pack."""
    hr = health_module()
    out: dict[str, Any] = {"machine": [], "notes": list(notes), "systems": {}}
    try:
        out["machine"] = hr.machine_report(systems[0], float(now))
    except Exception as exc:  # noqa: BLE001
        out["machine"] = [f"!! machine report failed: {type(exc).__name__}: {exc}"[:200]]
    for s in systems:
        name = s.paths.instance or "all-pairs"
        try:
            out["systems"][name] = hr.system_report(s, hours, float(now))
        except Exception as exc:  # noqa: BLE001
            out["systems"][name] = [f"!! health report of {name} failed: {type(exc).__name__}: {exc}"[:200]]
    return out


# --------------------------------------------------------------------------- the pack
def build_pack(s: Settings, *, hours: float, kind: str = "adhoc", out_dir: Path | None = None,
               pair: str | None = None, systems: list[Settings] | None = None, notes: list[str] | None = None,
               now: int | None = None, write: bool = True, git_root: Path | None = None,
               max_chars: int = MAX_MD_CHARS) -> Pack:
    """Collect, render (≤ ``max_chars``) and — with ``write`` — store ``<ts>_<kind>.md|.json`` in ``out_dir``
    (default ``data/reviews``). ``systems``/``notes`` default to the health report's choice. The machine's
    supervisors are scanned once for the whole build (:func:`one_supervisor_scan`)."""
    if kind not in KINDS:
        raise Invalid(f"kind {kind!r}: one of {', '.join(KINDS)}")
    if not 0 < hours <= 24 * 60:
        raise Invalid("--hours must be in (0, 1440]")
    with one_supervisor_scan():
        return _build_pack(s, hours=hours, kind=kind, out_dir=out_dir, pair=pair, systems=systems, notes=notes,
                           now=now, write=write, git_root=git_root, max_chars=max_chars)


def _build_pack(s: Settings, *, hours: float, kind: str, out_dir: Path | None, pair: str | None,
                systems: list[Settings] | None, notes: list[str] | None, now: int | None, write: bool,
                git_root: Path | None, max_chars: int) -> Pack:
    now = _now_ms() if now is None else int(now)
    since = now - int(hours * MS_PER_HOUR)
    pair = pair.upper() if pair else None
    if systems is None:
        systems, notes = choose_systems(pair)
    notes = list(notes or [])
    pack_id = f"{pack_stamp(now)}_{kind}"
    ledger = ledger_path(s, systems)
    usage = usage_section(ledger, since)
    usage["gauge"] = gauge_state(s, ledger, now)
    by_pair_role: dict[str, dict[str, dict[str, Any]]] = collections.defaultdict(dict)
    con = ro(ledger)
    if con is not None:
        try:
            have = _cols(con, "ai_usage")
            role = "COALESCE(role, purpose, '-')" if "role" in have else "COALESCE(purpose, '-')"
            for p, r, n, ok, inp, cached, out in con.execute(
                    f"SELECT pair, {role}, count(*), COALESCE(sum(ok),0), COALESCE(sum(input_tokens),0), "
                    "COALESCE(sum(cached_tokens),0), COALESCE(sum(output_tokens),0) FROM ai_usage WHERE ts>=? "
                    "AND pair IS NOT NULL GROUP BY pair, 2", (since,)):
                by_pair_role[p][r] = {"calls": n, "ok": ok, "input": inp, "cached": cached, "output": out}
        except sqlite3.Error:
            pass
        finally:
            con.close()
    per_pair: dict[str, dict[str, Any]] = {}
    for sysm in systems:
        for p in system_pairs(sysm, pair):
            per_pair[p] = pair_report(sysm, p, since, now, dict(by_pair_role.get(p, {})))
    ideas = []
    for p, rep in per_pair.items():
        for i in rep.pop("ideas", []):
            ideas.append({**i, "pair": p})
    ideas.sort(key=lambda d: d["ts"], reverse=True)
    lib = library_hash()
    hashes = {"config": s.config_hash or None, "library_code": lib,
              "per_pair": {p: {"prompt": (r.get("last_valid") or {}).get("prompt_hash"),
                               "library": (r.get("last_valid") or {}).get("library_hash"),
                               "playbook_decision": (r.get("last_valid") or {}).get("playbook_hash"),
                               "adaptive_decision": (r.get("last_valid") or {}).get("adaptive_hash"),
                               "playbook_file": ((r.get("adaptive") or {}).get("playbook") or {}).get("hash"),
                               "adaptive_file": (r.get("adaptive") or {}).get("file_hash")}
                           for p, r in per_pair.items()}}
    git = git_info(git_root or PROJECT_ROOT)
    hashes["git"] = git.get("sha")
    log_dirs = [sysm.paths.logs() for sysm in systems] + [s.paths.logs()]
    data: dict[str, Any] = {
        "pack_id": pack_id, "kind": kind, "hours": hours, "generated": iso(now),
        "window": {"since": iso(since), "until": iso(now), "since_ms": since, "until_ms": now},
        "pairs": list(per_pair), "systems": [x.paths.instance or "all-pairs" for x in systems],
        "execution_mode": s.execution.mode, "data_root": str(s.paths.data()),
        "git": git, "hashes": hashes, "usage": usage,
        "health": health(systems, notes, hours, now),
        "log_errors": log_errors(log_dirs, since),
        "kill_switch_global": _kill_switch(s, None),
        "tuning_freeze": (s.paths.data() / "TUNING_FREEZE").exists(), "adaptive_enabled": s.adaptive.enabled,
        "per_pair": per_pair, "ideas": ideas[:IDEAS], "ideas_in_window": len(ideas),
        "operator": operator_context(s, out_dir),
    }
    if kind == "weekly":
        data["go_live"] = go_live_evidence(s, now)
    md = render(data, max_chars)
    problems = [ln for sec in (data["health"]["machine"], data["health"]["notes"],
                               *data["health"]["systems"].values()) for ln in sec if str(ln).startswith("!!")]
    pack = Pack(pack_id, md, data, ledger, problems=tuple(problems))
    if write:
        out = out_dir or reviews_dir(s)
        out.mkdir(parents=True, exist_ok=True)
        pack = pack._replace(md_path=out / f"{pack_id}.md", json_path=out / f"{pack_id}.json")
        _write_text(pack.md_path, md)
        data["files"] = {"md": str(pack.md_path), "json": str(pack.json_path)}
        _write_text(pack.json_path, json.dumps(data, ensure_ascii=False, indent=1, default=str))
    return pack


def _write_text(path: Path, text: str) -> None:
    """Write-then-replace (a reader — the dashboard — never sees half a file); retried while a reader holds it."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    try:
        for attempt in range(20):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.05)
    finally:
        tmp.unlink(missing_ok=True)


# --------------------------------------------------------------------------- markdown
def _switch(ks: dict[str, Any]) -> str:
    return "ON " + json.dumps(ks.get("reason", {}), ensure_ascii=False) if ks.get("on") else "off"


def render(data: dict[str, Any], max_chars: int = MAX_MD_CHARS) -> str:
    """The markdown, shortened step by step until it fits: fewer ideas, fewer health lines, fewer detail rows; a
    last resort cut says so in the text."""
    for ideas, health_lines, rows in ((IDEAS, 60, 8), (15, 35, 5), (10, 20, 3), (5, 12, 2)):
        md = _render(data, ideas, health_lines, rows)
        if len(md) <= max_chars:
            return md
    note = "\n\n… (pack cut at the size limit — the JSON file has everything)\n"
    return md[: max_chars - len(note)] + note


def _render(d: dict[str, Any], n_ideas: int, n_health: int, n_rows: int) -> str:
    L: list[str] = []
    p = L.append
    w = d["window"]
    p(f"# Review pack — {d['kind']} — last {d['hours']:g} h — generated {d['generated']}")
    p(f"pack_id `{d['pack_id']}` · window {w['since']} → {w['until']} · systems {', '.join(d['systems'])} · "
      f"execution mode {d['execution_mode']} · data root {d['data_root']}")
    p("")
    # ---- versions
    g, h = d["git"], d["hashes"]
    p("## Versions and hashes")
    if g.get("error"):
        p(f"git: unavailable ({g['error']})")
    else:
        dirty = f"DIRTY ({g['dirty_files']} files)" if g["dirty"] else "clean"
        p(f"git {g['sha']} on {g['branch']} · {dirty}")
        for c in g.get("last_commits", [])[:5]:
            p(f"- {_short(c, 120)}")
    p(f"config {h.get('config')} · prompt library (code) {h.get('library_code')}")
    for pair, x in h["per_pair"].items():
        p(f"- {pair}: prompt {x['prompt']} · library {x['library']} · playbook {x['playbook_decision']} (file "
          f"{x['playbook_file']}) · adaptive {x['adaptive_decision']} (file {x['adaptive_file']})")
    p("")
    # ---- usage
    u = d["usage"]
    p("## AI usage (shared ledger)")
    if u.get("error"):
        p(f"ledger {u.get('ledger')}: {u['error']}")
    else:
        t = u["total"]
        p(f"total {t['calls']} calls ({t['ok']} ok) · in {t['input']:,} (cache-read share "
          f"{_num(t['cache_read_share'])}) · out {t['output']:,} · turns {t['turns']} · API-equivalent "
          f"${t['api_equivalent_usd']}")
        for r, x in sorted(u["by_role"].items()):
            p(f"- role {r}{ROLE_NOTES.get(r, '')}: {x['calls']} calls ({x['ok']} ok), in {x['input']:,} (cache-read "
              f"{_num(x['cache_read_share'])}), out {x['output']:,}")
        if u.get("usage_unknown_sessions"):
            p(f"!! {u['usage_unknown_sessions']} operator session(s) with unknown usage (timed out or crashed) — "
              "their tokens are missing from these totals, not zero")
    gz = u.get("gauge") or {}
    if gz.get("error"):
        p(f"usage gauge: {gz['error']}")
    else:
        p("usage gauge: " + ", ".join(f"{k} {_num(v)}" for k, v in gz.items() if not isinstance(v, (dict, list))))
    p("")
    # ---- health
    hh = d["health"]
    p("## Health (tools/health_report.py)")
    for ln in hh["machine"][:n_health]:
        p(ln)
    for ln in hh["notes"][:10]:
        p(ln)
    for name, lines in hh["systems"].items():
        for ln in lines[:n_health]:
            p(ln)
        if len(lines) > n_health:
            p(f"   … {len(lines) - n_health} more lines (JSON)")
    le = d["log_errors"]
    p("log ERROR/CRITICAL lines in the window: " + (", ".join(f"{k} {v}" for k, v in sorted(le.items())) or "none"))
    p(f"global kill switch: {_switch(d['kill_switch_global'])} · "
      f"TUNING_FREEZE: {'present' if d['tuning_freeze'] else 'absent'} · adaptive enabled: {d['adaptive_enabled']}")
    p("")
    # ---- per pair
    for pair, r in d["per_pair"].items():
        p(f"## {pair} (system {r['system']})")
        if r.get("error"):
            p(f"!! {r['error']}")
        sc = r.get("screens") or {}
        trig = sc.get("triggers_by_strength") or {}
        calls = r.get("calls_by_role") or {}
        f = r.get("funnel")
        closes = ", ".join(f"{k} {v}" for k, v in sorted((sc.get("closes_by_tf") or {}).items())) or "-"
        fired = ", ".join(f"{k} {v}" for k, v in sorted(trig.items())) or "-"
        by_role = ", ".join(f"{k} {x['calls']}" for k, x in sorted(calls.items())) or "-"
        p(f"funnel: screen closes {sc.get('screens', 0)} ({closes}) → triggers {sum(trig.values())} ({fired})"
          f" → ledger calls {sum(x['calls'] for x in calls.values())} ({by_role})")
        if f:
            m = f["metrics"]
            C = collections.Counter
            p(f"  → stored {f['calls_stored']} (status {_counts(C(f['by_status']))}; trigger "
              f"{_counts(C(f['by_trigger_strength']))}) → decisions {_counts(C(f['decisions']))}")
            p(f"  → ideas {f['ideas']} → execution {_counts(C(f['execution']))} · gate failures by check: "
              f"{_counts(C(f['gate_failures_by_check']))} → placed {f['placed']}")
            broker = ", ".join(f"{k} {v['n']} ({v['pnl_usd']:+} USD)"
                               for k, v in f["broker_outcomes"].items()) or "none"
            p(f"  → broker outcomes {broker} · virtual {_counts(C(f['virtual_outcomes']))} (resolved "
              f"{f['virtual_resolved']}, TP1 first {f['virtual_tp1_first']}, mean R {_num(f['mean_virtual_r'])})")
            p(f"  → metrics n {m['n']}: MFE {_num(m['mean_mfe_r'])} R, MAE {_num(m['mean_mae_r'])} R, TP1/2/3 hit "
              f"{_num(m['tp1_hit_share'])}/{_num(m['tp2_hit_share'])}/{_num(m['tp3_hit_share'])}, minutes to resolve "
              f"{_num(m['mean_minutes_to_resolve'], 0)}, exits {_counts(C(m['exit_reasons']))}, "
              f"rejected-but-virtual-win {m['rejected_but_virtual_win']}, spread@gate "
              f"{_num(m['mean_spread_at_gate'], 5)}, adverse slippage {_num(m['mean_slippage_adverse'], 5)} (n "
              f"{m['slippage_n']}), commission {_num(m['commission'])} (n {m['commission_n']}), swap "
              f"{_num(m['swap'])} (n {m['swap_n']}), "
              f"NO_TRADE counterfactual {_num(m['no_trade_mean_counterfactual_atr'])} ATR (n {m['no_trade_n']})")
            p(f"  tokens (stored calls) in {f['tokens']['input']:,} out {f['tokens']['output']:,} · mean latency "
              f"{_num(f['mean_latency_s'], 0)} s")
        att = r.get("attribution") or {}
        for key in ("session", "regime", "setup_kinds"):
            groups = att.get(key) or {}
            if groups:
                p(f"by {key}: " + "; ".join(f"{k} {v['ideas']} ideas/{v['resolved']} res/TP1 "
                                             f"{_num(v['tp1_first_share'])}/R {_num(v['mean_virtual_r'])}"
                                             for k, v in sorted(groups.items())))
        pa = r.get("position_actions") or {}
        if pa.get("n"):
            p(f"position actions {pa['n']}: {_counts(collections.Counter(pa['by_source_action_status']))}")
            for a in pa["last"][:n_rows]:
                p(f"- {a['time']} {a['source']} {a['action']} {a['status']} on {a['target']} leg {a['leg']}"
                  + (" (dry run)" if a["dry_run"] else "") + (f" — {a['why']}" if a.get("why") else ""))
        else:
            p("position actions: none in the window")
        re_ = r.get("rule_executions") or {}
        p(f"rule executions (management_state touched in the window): {re_.get('n', 0)} "
          f"({_counts(collections.Counter(re_.get('by_status') or {}))})")
        es = r.get("escalations") or {}
        if not es.get("n"):     # said explicitly: the live review read the 'decision' role rows as escalations
            p("escalations 0 (ai.escalation.enabled is " + ("on" if es.get("enabled") else "off") + ")")
        else:
            p(f"escalations {es['n']}: {_counts(collections.Counter(es['verdicts']))}")
            for e in es["last"][:n_rows]:
                p(f"- {e['time']} {e['decision_id']} {e['verdict']} ({e['label']}) {'; '.join(e['issues'])}")
        ad = r.get("adaptive") or {}
        if ad.get("error"):
            p(f"!! adaptive: {ad['error']}")
        ents = ad.get("entries") or []
        p("adaptive overlay: " + ("; ".join(f"{e['key']}={e['value']} ("
                                             + (f"until {e['expires']}" if e["in_force"]
                                                else ENTRY_STATES.get(e.get("state"), "NOT APPLIED"))
                                             + f"; {e['reason']})" for e in ents)
                                   or ("nothing listed (see the line above)" if ad.get("error")
                                       else "nothing set (config values in force)")))
        eff = ad.get("effective") or {}
        if eff and ents:
            p("effective values (config + overlay in force): " + ", ".join(
                f"{k} {_short(v, 120) if isinstance(v, str) else _num(v)}" for k, v in eff.items()
                if v is not None and not isinstance(v, (dict, list)) and k not in ("adaptive_hash", "playbook_hash")))
        pb = ad.get("playbook")
        if pb and pb.get("in_force") and pb.get("text"):
            p(f"playbook in force ({pb['chars']} chars, hash {pb['hash']}):")
            p("```")
            p(pb["text"])
            p("```")
        elif pb and pb.get("error"):
            p(f"playbook.md: {pb['error']} — NOT in force")
        elif pb:
            p(f"playbook.md {PLAYBOOK_NOT_IN_FORCE} ({pb.get('chars')} chars, hash {pb.get('hash')}): the trader's "
              "prompt does not carry it (the text is in the pack JSON)")
        else:
            p("playbook: none")
        for c in (ad.get("changes") or [])[-n_rows:]:
            p(f"- change: {json.dumps(c, ensure_ascii=False, default=str)[:240]}")
        tc = r.get("tuning_changes") or []
        if tc:
            p("tuning_changes (latest first): " + "; ".join(
                f"{x['time']} {x['key']} {x['old']}→{x['new']} by {x['actor']}"
                + (f" reverted {x['reverted']}" if x["reverted"] else "") for x in tc[:n_rows]))
        p(f"pair kill switch: {_switch(r.get('kill_switch') or {})}")
        lv = r.get("last_valid") or {}
        if lv.get("operator_notes"):
            p(f"last operator_notes ({lv.get('notes_ts')}): {lv['operator_notes']}")
        p("")
    # ---- go-live evidence (weekly packs)
    gl = d.get("go_live")
    if gl:
        p("## Go-live evidence so far (tools/go_live_inputs.py; thresholds: docs/go_live_checklist.md)")
        if gl.get("error"):
            p(f"go-live evidence unavailable: {gl['error']}")
        else:
            w, sm = gl["window"], gl["summary"]
            p(f"demo window {w['since']} → {w['planned_until']} ("
              + ("complete" if w["complete"] else f"so far {w['days_covered']:g} of {w['days']:g} days")
              + f") · pass {sm.get('pass', 0)} · FAIL {sm.get('FAIL', 0)} · n.a. {sm.get('n.a.', 0)}")
            for i in gl["items"]:
                p(f"- [{i['status']}] {i['item']} ({i['threshold']}): "
                  f"{_short(i['measured'], 320 if n_rows >= 5 else 160)}"
                  + ("" if i.get("n") is None else f" (n {i['n']})"))
        p("")
    # ---- operator context
    op = d.get("operator") or {}
    p("## Operator context")
    props = op.get("proposals") or []
    p("proposals (latest last): " + ("; ".join(f"{x.get('id') or x.get('slug')} [{x.get('status')}] {x.get('title')}"
                                               for x in props[-n_rows * 2:]) or "none"))
    for x in (op.get("previous_sessions") or [])[:n_rows]:
        p(f"- previous session {x['review_id']}: {x['status']}, {x['turns']} turns — "
          f"{x['summary'] or '(no summary)'}")
    if op.get("monitor_state"):
        p(f"monitor state: {_short(op['monitor_state'], 1200 if n_rows >= 5 else 400)}")
    p("")
    # ---- ideas
    ideas = d["ideas"][:n_ideas]
    p(f"## Last {len(ideas)} trade ideas of the window (of {d['ideas_in_window']})")
    if ideas:
        p("| time | pair | id | side/type | conf | rr | trig | exec (gate) | outcome | virtual | mfe/mae R | "
          "session/regime | summary |")
        p("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
        for i in ideas:
            ex = i["execution"] or "-"
            if i["gate_failures"]:
                ex += " (" + ", ".join(i["gate_failures"][:3]) + ")"
            oc = (f"{i['outcome']} {i['pnl_usd']:+}" if i["outcome"] and i["pnl_usd"] is not None
                  else (i["outcome"] or "-"))
            p(f"| {_hm(i['ts'])} | {i['pair']} | {i['id']} | {i['decision']}/{i['order_type'] or '-'} | "
              f"{_num(i['confidence'])} | {_num(i['rr'])} | {i['trigger_strength'] or '-'} | {ex} | {oc} | "
              f"{i['virtual_outcome'] or '-'} {_num(i['virtual_r'])} | {_num(i['mfe_r'])}/{_num(i['mae_r'])} | "
              f"{i['session'] or '-'}/{i['regime'] or '-'} | {_short(i['summary'], 90).replace('|', '/')} |")
    else:
        p("none")
    return "\n".join(L) + "\n"


# --------------------------------------------------------------------------- CLI
def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = _Parser(prog="review_pack.py", description=__doc__.split("\n\n")[0])
    ap.add_argument("--hours", type=float, default=24.0)
    ap.add_argument("--kind", choices=KINDS, default="adhoc", help="names the files (default adhoc)")
    ap.add_argument("--pair", help="only this pair")
    ap.add_argument("--out", help="data/reviews under the data root (the default) or a directory under it")
    ap.add_argument("--print", dest="print_only", action="store_true", help="print the markdown, write nothing")
    try:
        a = ap.parse_args(argv)
        s = load_settings()
        out_dir = checked_out_dir(s, a.out) if a.out else None       # before anything is built or written
        pack = build_pack(s, hours=a.hours, kind=a.kind, pair=a.pair, out_dir=out_dir, write=not a.print_only)
    except Invalid as exc:
        print(f"invalid: {exc}")
        return EXIT_INVALID
    except SystemExit as exc:           # --help
        return int(exc.code or 0)
    except Exception as exc:  # noqa: BLE001 — one clear line for the session / Task Scheduler
        print(f"error: {type(exc).__name__}: {exc}")
        return EXIT_ERROR
    if a.print_only:
        print(pack.md)
    else:
        print(f"pack {pack.pack_id}: {pack.md_path} ({len(pack.md):,} chars), {pack.json_path}; "
              f"{len(pack.problems)} health problem line(s)")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
