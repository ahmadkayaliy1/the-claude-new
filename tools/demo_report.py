"""Demo evaluation report (Phase 5 A8, D-046/D-047): what the demo window showed, per pair and in total.

    python tools/demo_report.py                        → docs/runs/demo.md over settings.evaluation's window (refused
                                                         in a checkout whose data root holds a system's app.db: there
                                                         use --print or --out data\\reviews\\demo.md)
    python tools/demo_report.py --since 2026-09-27T21:50:00Z --until 2026-10-02T21:50:00Z --out FILE [--json FILE]
    python tools/demo_report.py --print [--pair ETHUSDT]           → the markdown on stdout, no file written
    python tools/demo_report.py --root C:\\the_claude_new --print   → another checkout's data and logs (a worktree
                                                                     reading production; this checkout's config)
    python tools/demo_report.py --mt5                   → + the broker's deals history (read-only, brief, locked)

The window: ``evaluation.demo_start_utc`` + ``evaluation.demo_days`` (``--since``/``--until`` override; ``--since``
alone runs ``demo_days`` from it), cut at now while it is still running (the report says "so far").

A ``demo`` kind of ``tools/review_pack.py`` (its data functions are imported, never duplicated), per pair and in
total: the funnel (5-min screens → calls by role → stored answers → ideas → gate rejections by class → placed →
outcomes broker/virtual), the decision-metric means (MFE/MAE, TP hits, exit reasons, adverse slippage, costs with
their known counts), the realised PnL / commission / swap / fee of the outcomes settled in the window (``outcome_pnl_usd``
and ``outcome_detail``), the equity path from what exists (``account_peak.json``, the executor status, the monitor's
equity sample, the settled outcomes in time order) — the MT5 deals history of the project's magics only behind
``--mt5`` (a running terminal only — never launched —, one ``history_deals_get`` under the machine-wide MT5 history
lock ``data/shared/locks/mt5_history.lock``, then ``shutdown``); availability = the share of the pair's 15-min cycles
in the window (market-open time of its execution instrument only: XAU's weekend and daily break and the crypto CFDs'
Saturday maintenance are excluded through ``core/sessions.py``) lost to a stopped system, a suspend, a killed or
exited service, ``data_not_ready``, a stale-data skip, ``ai_not_ready``, ``ai_quota``, the subscription's session
limit, an ``interrupted`` or failed AI call, a ``cycle_error``, or a slot without any 5-min screen in the engine log
(where the log reaches back; the first cycle after a reopen, whose decision bar lies in closed time, is never lost:
the engine waits for its first bar by design); an incident table (every warn/critical monitor finding with its first
detection from ``logs/monitor.jsonl`` and ``data/shared/monitor_state.json``, with the onset the finding names and the
detection delay, plus suspends, restarts, kills, cycle errors and failed orders), the supervisors' starts after a run
that ended without a stop (a shutdown, restart, logoff or crash) and — best effort, read-only — the boots, sleeps and
shutdowns of the Windows System log; the AI cost per UTC day from the shared ledger
(calls, tokens raw and gauge-weighted, cache-read share, API-equivalent USD only where ``ai.providers.*.model_prices``
has a price — the CLI's own figure beside it); the prompt / library / playbook / adaptive / config / git hashes seen;
the tuning changes; and the sample-size statement. Every number states its n.

Read-only everywhere: SQLite through ``file:…?mode=ro`` URIs, plain file reads, one ``wevtutil qe`` of the System
log (a query, at most :data:`POWER_LOG_TIMEOUT_S`), no ledger object (the usage store would run DDL); the only write is
the report file (and ``--json``). Built to run in < 60 s and < 300 MB on production data (the engine logs are
streamed, at most ``review_pack.SCREEN_LOG_MAX_BYTES`` per pair; measured 2026-09-28 on the first demo night: 1.4 s,
68 MB peak). In the production checkout use ``--print`` or ``--out`` under the data root: a changed file under
``docs/`` makes the checkout dirty and can block the next ``git merge --ff-only`` (docs/runs/README.md) — the default
``docs/runs/demo.md`` is refused (exit 3) when this checkout's data root holds a system's ``app.db`` and no ``--root``
is given.
``tools/go_live_inputs.py`` judges the collected data against docs/go_live_checklist.md.
Exit 0 written/printed, 1 unexpected error, 3 invalid arguments.
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import importlib.util
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import types
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tradingsystem.ai.budget import CANCELLED_PREFIX, USAGE_UNKNOWN_PREFIX  # noqa: E402
from tradingsystem.core.settings import INSTANCE_ENV, Settings, load_settings, system_state_dirs  # noqa: E402
from tradingsystem.core.timeutil import MS_PER_DAY, MS_PER_MINUTE, iso, parse_date_spec  # noqa: E402
from tradingsystem.core.timeutil import now_ms as _now_ms  # noqa: E402

TOOLS = Path(__file__).resolve().parent
DEFAULT_OUT = ROOT / "docs" / "runs" / "demo.md"
EXIT_OK, EXIT_ERROR, EXIT_INVALID = 0, 1, 3
SLOT_MS = 15 * MS_PER_MINUTE                 # one decision cycle (the pairs' decision timeframe)
EPISODE_GAP_MS = 45 * MS_PER_MINUTE          # a monitor finding silent this long (3 runs) starts a new episode
GROUP_GAP_MS = 60 * MS_PER_MINUTE            # events of one kind this close are one incident row
MERGE_GAP_MS = 10 * MS_PER_MINUTE            # the same event in several systems (one machine sleep) is one row
MONITOR_LOG_MAX_BYTES = 20_000_000
MT5_LOCK_WAIT_S = 120                        # as the MT5 backfill's history calls (ingest/mt5/backfill.py)
RESTART_MAX_MS = 5 * MS_PER_MINUTE           # a service's exit/kill → its supervisor restarts it (backoff ≤ 120 s)
SESSION_END_CODE = "0x40010004"              # a service's exit code when the console session ended (logoff/shutdown)
SUPERVISOR_GAP_EVENTS = ("system_suspend", "stall", "clock_jump")    # rows only a supervisor that lived on writes
POWER_LOG_TIMEOUT_S = 10                     # the System log query (measured 0.2 s on the production laptop)
POWER_LOG_MAX_EVENTS = 500
POWER_GROUP_MS = 5 * MS_PER_MINUTE           # System log rows of one shutdown / one boot this close are one episode
# the Windows System log's power rows (read-only query): User32 1074 = a shutdown/restart requested (and by whom);
# Kernel-General 13/12 = the OS stopped/started (a full shutdown and a cold boot); Kernel-Boot 27 = the boot type (0
# cold, 1 Fast Startup = a Start-menu "shut down" on this laptop, 2 resume from hibernate); Kernel-Power 42 = entering
# sleep (TargetState 4 sleep, 5 hibernate, 6 the hybrid shutdown of Fast Startup), 107 = resumed, 41 = rebooted
# without a clean shutdown
POWER_QUERY = ("*[System[((Provider[@Name='User32'] and EventID=1074) or "
               "(Provider[@Name='Microsoft-Windows-Kernel-Power'] and (EventID=41 or EventID=42 or EventID=107)) or "
               "(Provider[@Name='Microsoft-Windows-Kernel-General'] and (EventID=12 or EventID=13)) or "
               "(Provider[@Name='Microsoft-Windows-Kernel-Boot'] and EventID=27)) and "
               "TimeCreated[@SystemTime>='{since}' and @SystemTime<'{until}']]]")
POWER_EVENT_RE = re.compile(r"<Event[ >].*?</Event>", re.S)
POWER_COUNTED = ("shutdown", "boot", "sleep")        # what item 11 counts (a resume ends a sleep; 41 marks a boot)
SESSION_LIMIT_RE = re.compile(r"subscription usage limit|session limit|usage limit", re.I)
ONSET_RE = re.compile(r"since (\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z)")
FINDING_RE = re.compile(r"^\[(warn|critical)\] (.+)$", re.S)
RAM_RE = re.compile(r"(\d+) MB available")
_PAIRISH = re.compile(r"^[A-Z0-9]{3,12}$")
# availability: why a market-open 15-min cycle was lost (a cycle may have several; the share counts it once)
LOSS_CLASSES = {
    "stopped": "the system was not running (supervisor shutdown → next start)",
    "system_suspend": "the PC was asleep",
    "killed": "a service was killed or exited (restarted by its supervisor)",
    "data_not_ready": "the decision bar was not stored 90 s after its close",
    "data_stale": "an AI cycle skipped by the data gate (stale analysis price)",
    "ai_not_ready": "the AI provider was not ready (sign-in check, cooldown)",
    "ai_quota": "the daily call cap was used up (0 calls left, or budget_blocked)",
    "session_limit": "the subscription's session/usage limit",
    "interrupted": "an AI call cut off by a suspend",
    "ai_error": "an AI call failed (timeout, deadline, sign-in race)",
    "cycle_error": "the engine cycle raised",
    "no_screen": "no 5-min screen in the engine log (not running, busy or waiting) — only where the log reaches",
}
SAMPLE_SIZE = ("{days:g} days show the absence of catastrophic behaviour and the execution quality, not a statistical "
               "edge: every rate in this report comes with its n, and no n here is large enough to estimate an edge "
               "(D-047). The owner signs the go-live checklist knowing this.")


# an engine 'ai_quota' event whose text says calls are still LEFT is rationing, not exhaustion: the F8 ladder ("5/30
# requests left today — not analysed: …") or the pre-12:00 reserve ("18/30 calls used today — the 12 left are kept for
# …"). Deliberate budget allocation: reported on its own, never counted against the availability threshold.
LEFT_RES = (re.compile(r"(\d+)/\d+ requests left today"), re.compile(r"the (\d+) left are kept"))


def calls_left(text: str) -> int | None:
    """The calls left that an engine quota message states, or None when it states none."""
    for rx in LEFT_RES:
        m = rx.search(text or "")
        if m:
            return int(m.group(1))
    return None


class Invalid(Exception):
    """Bad arguments (exit 3)."""


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise Invalid(f"{self.prog}: {message}")


# --------------------------------------------------------------------------- loading
def _load(name: str, path: Path) -> types.ModuleType:
    """A tool module by path (``tools/`` is not a package) under one sys.modules name, loaded once."""
    mod = sys.modules.get(name)
    if mod is None:
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        try:
            spec.loader.exec_module(mod)
        except BaseException:
            sys.modules.pop(name, None)
            raise
    return mod


def pack_module() -> types.ModuleType:
    """``tools/review_pack.py`` — the same module name the operator session runner uses."""
    return _load("ts_tools_review_pack", TOOLS / "review_pack.py")


def with_root(s: Settings, root: Path | None) -> Settings:
    """``s`` reading another checkout's ``data/`` and ``logs/`` (``--root``); this checkout's config stays."""
    if root is None:
        return s
    return s.model_copy(update={"paths": s.paths.model_copy(update={"data_dir": str(Path(root) / "data"),
                                                                     "logs_dir": str(Path(root) / "logs")})})


def settings_for(base: Settings, pair: str) -> Settings:
    """One pair's system settings (its instance overlay) on ``base``'s data and logs roots."""
    s = load_settings(extra_env={INSTANCE_ENV: pair})
    return s.model_copy(update={"paths": s.paths.model_copy(update={"data_dir": base.paths.data_dir,
                                                                     "logs_dir": base.paths.logs_dir})})


def report_systems(base: Settings, only: str | None = None) -> list[tuple[str, Settings]]:
    """``(pair, its system's settings)``: every configured instance with its own app.db (D-042); none → the
    all-pairs system's enabled pairs. No process scan: the report is about a past window, not what runs now."""
    data = base.paths.data()
    out = [(p, settings_for(base, p)) for p in sorted(base.instances)
           if (data / "instances" / p / "app.db").exists()]
    if not out:
        out = [(p, base) for p in sorted(base.enabled_pairs())]
    if only:
        only = only.upper()
        out = [(p, sp) for p, sp in out if p == only]
        if not out:
            raise Invalid(f"--pair {only}: no system on {data} trades it")
    return out


# --------------------------------------------------------------------------- the window
def demo_window(s: Settings, *, since: int | None = None, until: int | None = None,
                now: int | None = None) -> dict[str, Any]:
    """The evaluated window: ``since``/``until`` or ``evaluation.demo_start_utc`` + ``demo_days``; its end is cut at
    ``now`` while it runs (``complete`` False, ``days_covered`` so far)."""
    now = _now_ms() if now is None else int(now)
    days = s.evaluation.demo_days
    source = "arguments" if since is not None else "config"
    if since is None:
        if not s.evaluation.demo_start_utc:
            raise Invalid("no window: set evaluation.demo_start_utc in the config or pass --since")
        since = parse_date_spec(s.evaluation.demo_start_utc)
    planned = int(until) if until is not None else int(since) + days * MS_PER_DAY
    if planned <= since:
        raise Invalid("--until must be after the window start")
    end = min(planned, now)
    if end <= since:
        raise Invalid(f"the window starts {iso(since)}, after now")
    return {"since_ms": int(since), "until_ms": end, "planned_until_ms": planned, "since": iso(since),
            "until": iso(end), "planned_until": iso(planned), "complete": now >= planned,
            "days": round((planned - since) / MS_PER_DAY, 2), "days_covered": round((end - since) / MS_PER_DAY, 2),
            "source": source}


# --------------------------------------------------------------------------- small helpers
def _ts_ms(text: Any) -> int | None:
    """A log line's ISO timestamp → UTC ms (None when it is not one)."""
    try:
        d = dt.datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        return None
    return int(d.timestamp() * 1000)


def _loads(text: Any, default: Any = None) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def _pct(x: float | None, nd: int = 1) -> str:
    return "-" if x is None else f"{x * 100:.{nd}f} %"


def _usd(x: float | None) -> str:
    return "-" if x is None else f"{x:+.2f}"


def _num(x: Any, nd: int = 2) -> str:
    if x is None:
        return "-"
    if isinstance(x, float):
        return f"{x:.{nd}f}"
    return str(x)


def _hm(ms: int | None) -> str:
    return time.strftime("%m-%d %H:%M", time.gmtime(ms / 1000)) if ms else "-"


def _short(text: Any, n: int) -> str:
    t = " ".join(str(text or "").split())
    return t if len(t) <= n else t[: n - 1] + "…"


def _counts(c: dict[str, int] | collections.Counter, n: int = 12) -> str:
    c = collections.Counter(c)
    return ", ".join(f"{k} {v}" for k, v in c.most_common(n)) or "none"


def _statuses(con: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """The engine / executor heartbeats (``collector_status``) with their details."""
    rp = pack_module()
    if not rp._cols(con, "collector_status"):
        return {}
    out = {}
    for c, st, upd, det in con.execute("SELECT collector, state, updated_ms, detail FROM collector_status "
                                       "WHERE collector IN ('engine', 'executor')"):
        out[c] = {"state": st, "updated_ms": upd, "updated": iso(upd), "detail": _loads(det, {}) or {}}
    return out


# --------------------------------------------------------------------------- the shared ledger
LEDGER_COLS = ("ts", "provider", "model", "role", "purpose", "pair", "input_tokens", "cached_tokens",
               "cache_creation_tokens", "output_tokens", "ok", "error", "api_equivalent_usd")


def ledger_rows(ledger: Path, since: int, until: int) -> tuple[list[dict[str, Any]], str | None]:
    """The ledger rows of the window (read-only), ``role`` falling back to ``purpose`` for pre-Phase-3 rows."""
    rp = pack_module()
    con = rp.ro(ledger)
    if con is None:
        return [], f"{ledger}: no ledger"
    try:
        have = rp._cols(con, "ai_usage")
        rows = rp._rows(con, f"SELECT {rp._select(have, LEDGER_COLS)} FROM ai_usage WHERE ts>=? AND ts<? ORDER BY ts",
                        (since, until))
    except sqlite3.Error as exc:
        return [], f"{ledger}: unreadable: {exc}"[:200]
    finally:
        con.close()
    for r in rows:
        r["role"] = r.get("role") or r.get("purpose") or "-"
    return rows, None


def model_prices(s: Settings) -> dict[str, tuple[float, float]]:
    """``model → (input, output) USD/MTok`` from every provider's ``model_prices`` (the first provider by name wins
    a model priced twice). A model without a price stays unpriced — never a guess."""
    out: dict[str, tuple[float, float]] = {}
    for name in sorted(s.ai.providers):
        for model, (p_in, p_out) in s.ai.providers[name].model_prices.items():
            out.setdefault(model, (float(p_in), float(p_out)))
    return out


def api_usd(r: dict[str, Any], price: tuple[float, float]) -> float:
    """API-equivalent USD of one ledger row: fresh input at the input price, cache reads at 0.1×, cache writes at
    1.25× (Anthropic's rates), output at the output price. ``input_tokens`` already contains the cache reads and
    writes (the claude_code convention)."""
    p_in, p_out = price
    inp = max(int(r.get("input_tokens") or 0), 0)
    cached = min(max(int(r.get("cached_tokens") or 0), 0), inp)
    written = min(max(int(r.get("cache_creation_tokens") or 0), 0), inp - cached)
    out = max(int(r.get("output_tokens") or 0), 0)
    return ((inp - cached - written) * p_in + cached * p_in * 0.1 + written * p_in * 1.25 + out * p_out) / 1e6


def cost_per_day(rows: list[dict[str, Any]], s: Settings) -> dict[str, Any]:
    """Per UTC day: calls (ok, by role), tokens raw (input incl. cache, output), gauge-weighted tokens (the claude_code
    providers' rows, cache reads × ``ai.usage.cache_read_weight`` — ``ai/usage_gauge.py``), the cache-read share,
    API-equivalent USD over the priced rows (their n) and the CLI's own ``api_equivalent_usd``; sessions whose usage
    is unknown (0 tokens recorded) are counted, since their spend is missing, not zero."""
    from tradingsystem.ai.usage_gauge import claude_providers, effective_tokens
    prices = model_prices(s)
    claude = set(claude_providers(s))
    weight = s.ai.usage.cache_read_weight
    days: dict[str, dict[str, Any]] = {}
    for r in rows:
        day = time.strftime("%Y-%m-%d", time.gmtime(r["ts"] / 1000))
        a = days.setdefault(day, {"calls": 0, "ok": 0, "by_role": collections.Counter(), "input": 0, "cached": 0,
                                  "output": 0, "weighted": 0.0, "api_usd": 0.0, "priced_n": 0, "with_tokens_n": 0,
                                  "cli_usd": 0.0, "usage_unknown": 0, "unpriced_models": collections.Counter()})
        a["calls"] += 1
        a["ok"] += int(r.get("ok") or 0)
        a["by_role"][r["role"]] += 1
        inp, cached, out = (int(r.get(k) or 0) for k in ("input_tokens", "cached_tokens", "output_tokens"))
        a["input"] += inp
        a["cached"] += cached
        a["output"] += out
        if r.get("provider") in claude:
            a["weighted"] += effective_tokens({"input": inp, "cached": cached, "output": out}, weight)
        a["cli_usd"] += float(r.get("api_equivalent_usd") or 0.0)
        if str(r.get("error") or "").startswith((USAGE_UNKNOWN_PREFIX, CANCELLED_PREFIX)):
            a["usage_unknown"] += 1
        if inp or out:
            a["with_tokens_n"] += 1
            price = prices.get(str(r.get("model")))
            if price is None:
                a["unpriced_models"][str(r.get("model"))] += 1
            else:
                a["priced_n"] += 1
                a["api_usd"] += api_usd(r, price)
    total = {"calls": 0, "ok": 0, "input": 0, "cached": 0, "output": 0, "weighted": 0.0, "api_usd": 0.0,
             "priced_n": 0, "with_tokens_n": 0, "cli_usd": 0.0, "usage_unknown": 0}
    for a in days.values():
        for k in total:
            total[k] += a[k]
        a["by_role"] = dict(a["by_role"])
        a["unpriced_models"] = dict(a["unpriced_models"])
    for a in (*days.values(), total):
        a["cache_read_share"] = round(a["cached"] / a["input"], 3) if a["input"] else None
        a["weighted"] = int(round(a["weighted"]))
        a["api_usd"] = round(a["api_usd"], 2)
        a["cli_usd"] = round(a["cli_usd"], 2)
    return {"days": dict(sorted(days.items())), "total": total, "cache_read_weight": weight,
            "priced_models": sorted(prices)}


def calls_per_day(rows: list[dict[str, Any]], pair: str, s: Settings) -> dict[str, int]:
    """The pair's calls per UTC day that count against ``ai.daily_calls_per_pair`` (the claude_code providers' rows
    of the pair, operator sessions excluded — as ``budget.UsageStore.count_since``)."""
    from tradingsystem.ai.usage_gauge import claude_providers
    claude = set(claude_providers(s))
    out: collections.Counter = collections.Counter()
    for r in rows:
        if r.get("pair") == pair and r.get("provider") in claude and r["role"] not in ("review", "diagnose"):
            out[time.strftime("%Y-%m-%d", time.gmtime(r["ts"] / 1000))] += 1
    return dict(sorted(out.items()))


def session_limits(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Subscription-limit errors of the window: ledger rows, and events (rows within 10 min are one hit of the one
    shared subscription — both pairs' calls failed at the same second on 2026-09-26)."""
    hits = [r for r in rows if SESSION_LIMIT_RE.search(str(r.get("error") or ""))]
    events: list[dict[str, Any]] = []
    for r in hits:
        if events and r["ts"] - events[-1]["last_ms"] <= MERGE_GAP_MS:
            events[-1]["last_ms"] = r["ts"]
            events[-1]["rows"] += 1
            events[-1]["pairs"] = sorted({*events[-1]["pairs"], str(r.get("pair") or "-")})
        else:
            events.append({"first_ms": r["ts"], "last_ms": r["ts"], "rows": 1, "pairs": [str(r.get("pair") or "-")],
                           "error": _short(r.get("error"), 160)})
    for e in events:
        e["time"] = iso(e["first_ms"])
    return {"rows": len(hits), "events": events}


# --------------------------------------------------------------------------- per pair
def pair_calendar(sp: Settings, pair: str) -> Any:
    """The session calendar of the pair's execution instrument (as the engine and the executor use it)."""
    from tradingsystem.core.instruments import InstrumentRegistry
    from tradingsystem.core.sessions import calendar_for
    reg = InstrumentRegistry.from_settings(sp)
    ex = reg.with_role(pair, "execution") or [reg.primary(pair)]
    return calendar_for(ex[0].venue, ex[0].symbol, sp.pairs[pair].asset_class)


def _slots(since: int, until: int) -> list[int]:
    """The whole 15-min cycles inside ``[since, until)``."""
    first = -(-since // SLOT_MS) * SLOT_MS
    return list(range(first, until - SLOT_MS + 1, SLOT_MS))


def availability(con: sqlite3.Connection, sp: Settings, pair: str, since: int, until: int,
                 ledger: list[dict[str, Any]], screen_slots: set[int] | None, screen_from: int | None) -> dict[str, Any]:
    """The share of the pair's market-open 15-min cycles of ``[since, until)`` not lost to any :data:`LOSS_CLASSES`
    (from ``ingestion_events``, ``ai_decisions``, the ledger's session-limit rows and the engine's screen lines);
    ``lost`` counts the open cycles per class (a cycle may carry several), ``lost_total`` each once. An open cycle
    whose decision bar (the 15 minutes before it) lies in closed time — the first cycle after every reopen, XAU's
    daily and Sunday one — is never lost: the engine waits for its first stored bar by design (no screen line, no
    ``data_not_ready`` before the next close); ``reopen_waits`` counts them."""
    try:
        cal = pair_calendar(sp, pair)
        cal_name = cal.name
    except Exception as exc:  # noqa: BLE001 — every cycle counts as open then, said in the output
        cal, cal_name = None, f"unknown ({type(exc).__name__}: every cycle counted as open)"
    starts = _slots(since, until)
    open_ = [t for t in starts if cal is None or cal.is_open(t)]
    waits = {t for t in open_ if cal is not None and not cal.is_open(t - SLOT_MS)}
    open_set = set(open_) - waits                     # the cycles that can be lost
    lost: dict[str, set[int]] = collections.defaultdict(set)
    rationed: set[int] = set()              # held back with calls left (F8 / the reserve): not lost

    def mark(cls: str, t0: int, t1: int | None = None) -> None:
        """The open cycles overlapping ``[t0, t1]`` (the one holding ``t0`` when ``t1`` is None) → ``cls``."""
        t = t0 // SLOT_MS * SLOT_MS
        hi = t if t1 is None else t1
        while t <= hi:
            if t in open_set:
                lost[cls].add(t)
            t += SLOT_MS

    rp = pack_module()
    notes: list[str] = []
    if rp._cols(con, "ingestion_events"):
        # the system's run state at the window start: the last shutdown / start before it
        prev = con.execute("SELECT ts, event FROM ingestion_events WHERE ts<? AND ((collector='supervisor:all' AND "
                           "event='stopped') OR (collector LIKE 'supervisor:%' AND event='started')) "
                           "ORDER BY ts DESC LIMIT 1", (since,)).fetchone()
        down_since = since if prev and prev[1] == "stopped" else None
        rows = con.execute("SELECT ts, collector, event, detail, duration_ms FROM ingestion_events WHERE ts>=? AND ts<? "
                           "AND event IN ('stopped', 'started', 'system_suspend', 'killed', 'exited', 'data_not_ready', "
                           "'ai_not_ready', 'ai_quota', 'cycle_error') ORDER BY ts",
                           (since, until + MS_PER_DAY)).fetchall()
        for ts, col, ev, det, dur in rows:
            if ev == "system_suspend":
                if ts - int(dur or 0) < until:
                    mark("system_suspend", max(ts - int(dur or 0), since), min(ts, until))
                continue
            if ts >= until:
                continue
            if ev == "stopped" and col == "supervisor:all":
                down_since = ts if down_since is None else down_since
            elif ev == "started" and str(col).startswith("supervisor:"):
                if down_since is not None:
                    mark("stopped", down_since, ts)
                    down_since = None
            elif ev in ("killed", "exited") and str(col).startswith("supervisor:"):
                mark("killed", ts)
            elif ev == "ai_quota" and (calls_left(str(det or "")) or 0) > 0:
                slot = ts - ts % SLOT_MS
                if slot in open_set:
                    rationed.add(slot)
            elif ev in ("data_not_ready", "ai_not_ready", "ai_quota", "cycle_error") and \
                    (col == "engine" or ev != "cycle_error"):
                mark(ev, ts)
        if down_since is not None:
            mark("stopped", down_since, until - 1)
        # B12 / B15 / B8: entry calls the engine deliberately did not make (one event per hour / per gap between
        # windows / per release)
        why = {"skipped: no_fit": " (the minimum lot does not fit the risk / leverage caps; "
                                  "ai.skip_entry_calls_when_no_fit)",
               "skipped: outside_desk_window": " (the desk's call windows, B15)",
               "skipped: news_blackout": " (a scheduled release's news blackout, B8)"}
        for ev, n in con.execute("SELECT event, count(*) FROM ingestion_events WHERE collector='engine' AND ts>=? "
                                 "AND ts<? AND event IN ('skipped: no_fit', 'skipped: outside_desk_window', "
                                 "'skipped: news_blackout') AND detail LIKE ? GROUP BY event",
                                 (since, until, f"{pair}:%")):
            notes.append(f"{n} `{ev}` event(s): entry calls deliberately not made" + why.get(ev, ""))
    else:
        notes.append("no ingestion_events table")
    if rp._cols(con, "ai_decisions"):
        for ts, status, errors in con.execute("SELECT ts, status, errors FROM ai_decisions WHERE pair=? AND ts>=? "
                                              "AND ts<? AND status IN ('error', 'skipped', 'interrupted', "
                                              "'budget_blocked')", (pair, since, until)):
            text = str(errors or "")
            if SESSION_LIMIT_RE.search(text):
                mark("session_limit", ts)
            elif status == "interrupted":
                mark("interrupted", ts)
            elif status == "budget_blocked":
                mark("ai_quota", ts)
            elif status == "skipped":
                if "data gate" in text:
                    mark("data_stale", ts)
            else:
                mark("ai_error", ts)
    for r in ledger:
        if r.get("pair") == pair and SESSION_LIMIT_RE.search(str(r.get("error") or "")):
            mark("session_limit", r["ts"])
    if screen_slots is not None and screen_from is not None:
        for t in open_:
            if t >= screen_from and t not in screen_slots and t in open_set:
                lost["no_screen"].add(t)
    elif screen_slots is not None:
        notes.append("no engine screen line read: the no_screen class is not measured")
    if waits:
        notes.append(f"{len(waits)} first cycle(s) after a reopen not counted as lost: the decision bar lies in "
                     "closed time, the engine waits for its first stored bar")
    rationed -= set().union(*lost.values()) if lost else set()
    if rationed:
        notes.append(f"{len(rationed)} cycle(s) held back with calls left (F8 rationing / the pre-12:00 reserve) — "
                     "deliberate budget allocation, not counted as lost")
    total = set().union(*lost.values()) if lost else set()
    n_open = len(open_)
    return {"calendar": cal_name, "cycles": len(starts), "open_cycles": n_open, "closed_cycles": len(starts) - n_open,
            "lost": {k: len(lost[k]) for k in LOSS_CLASSES if lost.get(k)}, "lost_total": len(total),
            "share": round(1 - len(total) / n_open, 4) if n_open else None, "reopen_waits": len(waits),
            "rationed": len(rationed),
            "screen_log_from": iso(screen_from) if screen_from else None, "notes": notes}


def gate_class(check: str, detail: str) -> str:
    """A failed gate check → its rejection class; ``position_size`` is split by why (the 0.01-lot minimum risking
    more than ``max_risk_per_trade_pct`` is the by-design refusal at $100, D-046 d)."""
    if check == "position_size":
        return "position_size_min_lot" if "minimum lot" in str(detail) else "position_size_other"
    return check


def gate_rejections(con: sqlite3.Connection, pair: str, since: int, until: int) -> dict[str, Any]:
    """Rejected ideas of the window by class: every failed check of the gate list; an order the backend refused after
    a passed gate (the executor stores the gate list, all ok, and ``backend`` with the broker's or the paper book's
    reason — no top-level ``reason``) is ``not_placed: <that reason>``; a rejection without a gate record (no quote,
    pair disabled, an exception) is ``not_gated: <reason>``."""
    by_class: collections.Counter = collections.Counter()
    ideas: collections.Counter = collections.Counter()
    examples: dict[str, str] = {}
    n = expired = 0
    for did, ts, state, det in con.execute(
            "SELECT id, ts, execution_state, execution_detail FROM ai_decisions WHERE pair=? AND ts>=? AND ts<? AND "
            "decision IN ('BUY', 'SELL') AND execution_state IN ('rejected', 'expired')", (pair, since, until)):
        if state == "expired":
            expired += 1
            continue
        n += 1
        d = _loads(det, {}) or {}
        gate = d.get("gate") if isinstance(d, dict) else None
        failed = [(str(g.get("check")), str(g.get("detail") or "")) for g in gate or []
                  if isinstance(g, dict) and not g.get("ok")] if isinstance(gate, list) else []
        backend = d.get("backend") if isinstance(d, dict) else None
        if failed:
            classes = sorted({gate_class(c, x) for c, x in failed})
            for c, x in failed:
                examples.setdefault(gate_class(c, x), f"{str(did)[:8]} {iso(ts)}: {_short(x, 140)}")
        elif isinstance(gate, list) and gate and isinstance(backend, dict):
            why = backend.get("reason") or backend.get("error") or "no reason recorded"
            cls = f"not_placed: {_short(why, 80)}"
            classes = [cls]
            examples.setdefault(cls, f"{str(did)[:8]} {iso(ts)}: gate passed, the order refused: {_short(why, 140)}")
        else:
            reason = _short((d.get("reason") if isinstance(d, dict) else None)
                            or (backend.get("reason") if isinstance(backend, dict) else None) or "no reason recorded", 80)
            cls = f"not_gated: {reason}"
            classes = [cls]
            examples.setdefault(cls, f"{str(did)[:8]} {iso(ts)}")
        by_class.update(classes)
        ideas[" + ".join(classes)] += 1
    return {"rejected": n, "expired": expired, "by_class": dict(by_class), "by_idea": dict(ideas),
            "examples": examples}


def placed_without_sl(con: sqlite3.Connection, pair: str, since: int, until: int) -> dict[str, Any]:
    """Placed ideas of the window whose gate record lacks a passing ``stop_loss_present`` check (every order carries
    its SL from the gate; a record without the check cannot prove it)."""
    placed = unproven = 0
    ids = []
    for did, det in con.execute("SELECT id, execution_detail FROM ai_decisions WHERE pair=? AND ts>=? AND ts<? AND "
                                "execution_state='executed'", (pair, since, until)):
        placed += 1
        gate = (_loads(det, {}) or {}).get("gate")
        ok = isinstance(gate, list) and any(isinstance(g, dict) and g.get("check") == "stop_loss_present"
                                            and g.get("ok") for g in gate)
        if not ok:
            unproven += 1
            ids.append(str(did)[:8])
    return {"placed": placed, "sl_not_proven": unproven, "ids": ids[:10]}


def realised(con: sqlite3.Connection, pair: str, since: int, until: int) -> dict[str, Any]:
    """Outcomes SETTLED in the window (``outcome_ts``), whenever the idea was made: net PnL (profit + commission +
    swap + fee, as the MT5 settlement stores it) and the cost split from ``outcome_detail`` where it is known (an
    outcome settled before Phase 4 has none — unknown, never 0)."""
    rows = con.execute("SELECT id, ts, decision, outcome, outcome_pnl_usd, outcome_ts, outcome_detail FROM ai_decisions "
                       "WHERE pair=? AND outcome IS NOT NULL AND outcome_ts>=? AND outcome_ts<? ORDER BY outcome_ts",
                       (pair, since, until)).fetchall()
    out: dict[str, Any] = {"n": len(rows), "by_outcome": dict(collections.Counter(r[3] for r in rows)),
                           "pnl_usd": round(sum(float(r[4] or 0) for r in rows), 2),
                           "pnl_known_n": sum(1 for r in rows if r[4] is not None),
                           "wins": sum(1 for r in rows if (r[4] or 0) > 0),
                           "losses": sum(1 for r in rows if (r[4] or 0) < 0), "trades": []}
    details = [d for d in (_loads(r[6], {}) for r in rows) if isinstance(d, dict)]
    for k in ("commission", "swap", "fee"):
        vals = [float(d[k]) for d in details if isinstance(d.get(k), (int, float)) and not isinstance(d.get(k), bool)]
        out[k] = round(sum(vals), 4) if vals else None
        out[f"{k}_n"] = len(vals)
    for did, ts, side, oc, pnl, ots, _od in rows:
        out["trades"].append({"id": str(did)[:8], "idea": iso(ts), "settled_ms": ots, "settled": iso(ots),
                              "side": side, "outcome": oc, "pnl_usd": pnl, "pair": pair})
    return out


def hashes_seen(con: sqlite3.Connection, pair: str, since: int, until: int) -> dict[str, list[dict[str, Any]]]:
    """Each hash column of the window's decisions: every value with its count and first/last time."""
    rp = pack_module()
    have = rp._cols(con, "ai_decisions")
    out: dict[str, list[dict[str, Any]]] = {}
    for col in ("prompt_hash", "library_hash", "playbook_hash", "adaptive_hash", "config_hash", "git_sha"):
        if col not in have:
            continue
        rows = con.execute(f"SELECT {col}, count(*), min(ts), max(ts) FROM ai_decisions WHERE pair=? AND ts>=? AND "
                           f"ts<? AND {col} IS NOT NULL GROUP BY {col} ORDER BY min(ts)", (pair, since, until)).fetchall()
        out[col] = [{"value": v, "n": n, "first": iso(a), "last": iso(b)} for v, n, a, b in rows]
    return out


def pair_section(sp: Settings, pair: str, w: dict[str, Any], ledger: list[dict[str, Any]], now: int,
                 cap_default: int) -> dict[str, Any]:
    """Everything of one pair over the window: the pack's per-pair evidence (``review_pack.pair_report`` bounded to
    the window), availability, gate classes, SL proof, realised PnL, hashes, calls per day against the cap."""
    rp = pack_module()
    since, until = w["since_ms"], w["until_ms"]
    by_role: dict[str, dict[str, int]] = {}
    for r in ledger:
        if r.get("pair") == pair:
            a = by_role.setdefault(r["role"], {"calls": 0, "ok": 0, "input": 0, "cached": 0, "output": 0})
            a["calls"] += 1
            a["ok"] += int(r.get("ok") or 0)
            a["input"] += int(r.get("input_tokens") or 0)
            a["cached"] += int(r.get("cached_tokens") or 0)
            a["output"] += int(r.get("output_tokens") or 0)
    rep = rp.pair_report(sp, pair, since, now, by_role, until=until, screen_slot_ms=SLOT_MS)
    ideas = rep.pop("ideas", [])
    rep["placed_ideas"] = [i for i in ideas if i.get("execution") == "executed"]
    sc = rep.get("screens") or {}
    slots = sc.pop("slots", None)
    screen_from = _ts_ms(sc.get("oldest_read")) if sc.get("oldest_read") else None
    db = sp.paths.state() / "app.db"
    con = rp.ro(db)
    if con is None:
        rep["availability"] = {"error": "no app.db"}
        return rep
    try:
        st = _statuses(con)
        rep["status"] = st
        eng = (st.get("engine") or {}).get("detail") or {}
        cap = eng.get("quota_per_day") if isinstance(eng.get("quota_per_day"), int) else None
        rep["cap"] = {"value": cap or cap_default,
                      "source": "the engine status (quota_per_day)" if cap else "ai.daily_calls_per_pair"}
        rep["calls_per_day"] = calls_per_day(ledger, pair, sp)
        rep["availability"] = availability(con, sp, pair, since, until, ledger,
                                           set(slots) if slots is not None else None, screen_from)
        if rp._cols(con, "ai_decisions"):
            rep["gate"] = gate_rejections(con, pair, since, until)
            rep["sl_proof"] = placed_without_sl(con, pair, since, until)
            rep["realised"] = realised(con, pair, since, until)
            rep["hashes_seen"] = hashes_seen(con, pair, since, until)
            if sp.pairs[pair].desk is not None:                # B19: the shadow desk's ideas (no baseline here)
                rep["desk"] = desk_section(con, sp, pair, since, until)
    except sqlite3.Error as exc:
        rep["error"] = f"database unreadable: {exc}"[:200]
    finally:
        con.close()
    return rep


def desk_section(con: sqlite3.Connection, sp: Settings, pair: str, since: int, until: int) -> dict[str, Any]:
    """B19 (D-049): a desk pair's shadow ideas of the window - ``tools/desk_report.py``'s own functions; the
    random-entry baseline and the H32 verdict are that tool's (``python tools/desk_report.py --print``)."""
    try:
        dr = _load("ts_tools_desk_report", TOOLS / "desk_report.py")
        rows = dr.ideas(con, pair, since, until, sp)
        shadow = [r for r in rows if r["shadow"]]
        return {"shadow": dr.summary(shadow), "desk_ok": dr.summary([r for r in shadow if r["desk_ok"]]),
                "calls_per_day": dr.calls_per_day(con, pair, since, until)}
    except Exception as exc:  # noqa: BLE001 - one section, never the whole report
        return {"error": repr(exc)[:200]}


# --------------------------------------------------------------------------- incidents
def _finding_title(rest: str) -> tuple[str, str]:
    """``"BTCUSDT: connection outage: text"`` → (title, text): a finding's title is ``<system>: <what>`` or a plain
    name (tools/monitor.py ``Finding.title``); the log line is ``[level] title: text[ | action][ (already notified)]``."""
    parts = rest.split(": ", 2)
    if len(parts) == 3 and (_PAIRISH.match(parts[0]) or parts[0] in ("all", "all-pairs", "machine")):
        title, text = f"{parts[0]}: {parts[1]}", parts[2]
    elif len(parts) >= 2:
        title, text = parts[0], ": ".join(parts[1:])
    else:
        title, text = rest, ""
    text = text.removesuffix(" (already notified)").split(" | ")[0]
    return title.strip(), text.strip()


def _read_lines(files: Iterable[Path], max_bytes: int) -> list[bytes]:
    """The lines of ``files`` (newest file first), each file's tail once ``max_bytes`` is spent."""
    out: list[bytes] = []
    left = max_bytes
    for f in files:
        if left <= 0:
            break
        try:
            size = f.stat().st_size
            with f.open("rb") as fh:
                if size > left:
                    fh.seek(size - left)
                    fh.readline()
                data = fh.read(left)
        except OSError:
            continue
        left -= len(data)
        out = data.splitlines() + out
    return out


def monitor_log(logs: Path, since: int, until: int) -> dict[str, Any]:
    """``logs/monitor.jsonl`` (+ rotations) over the window: the warn/critical findings grouped into episodes (one
    title silent for :data:`EPISODE_GAP_MS` starts a new one), the monitor runs (count, largest gap) and the lowest
    free RAM a ``Low free RAM`` finding named."""
    rp = pack_module()
    episodes: list[dict[str, Any]] = []
    open_ep: dict[str, dict[str, Any]] = {}
    runs: list[int] = []
    ram_min: int | None = None
    for raw in _read_lines(rp._log_files(logs, "monitor"), MONITOR_LOG_MAX_BYTES):
        if b'"monitor' not in raw:
            continue
        try:
            j = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(j, dict) or j.get("logger") != "monitor":
            continue
        ts = _ts_ms(j.get("ts"))
        if ts is None or not since <= ts < until:
            continue
        msg = str(j.get("msg") or "")
        if msg.startswith("monitor run:"):
            runs.append(ts)
            continue
        m = FINDING_RE.match(msg)
        if not m:
            continue
        level, (title, text) = m.group(1), _finding_title(m.group(2))
        if title == "Low free RAM" and (r := RAM_RE.search(text)):
            ram_min = int(r.group(1)) if ram_min is None else min(ram_min, int(r.group(1)))
        ep = open_ep.get(title)
        if ep is None or ts - ep["last_ms"] > EPISODE_GAP_MS:
            onset = ONSET_RE.search(text)
            ep = {"kind": "monitor", "title": title, "level": level, "first_ms": ts, "last_ms": ts, "n": 0,
                  "text": _short(text, 160), "onset_ms": _ts_ms(onset.group(1)) if onset else None,
                  "source": "monitor.jsonl"}
            open_ep[title] = ep
            episodes.append(ep)
        ep["last_ms"] = ts
        ep["n"] += 1
        if level == "critical":
            ep["level"] = "critical"
    runs.sort()
    gaps = [b - a for a, b in zip(runs, runs[1:])]
    edge = [runs[0] - since, until - runs[-1]] if runs else []
    return {"episodes": episodes, "runs": len(runs), "max_gap_min": round(max(gaps) / MS_PER_MINUTE, 1) if gaps else None,
            "edge_gap_min": [round(x / MS_PER_MINUTE, 1) for x in edge], "ram_min_mb": ram_min}


def monitor_state(s: Settings) -> dict[str, Any]:
    """``data/shared/monitor_state.json`` (read once; unreadable → {})."""
    try:
        doc = json.loads((s.paths.shared() / "monitor_state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def merge_state_alerts(episodes: list[dict[str, Any]], state: dict[str, Any], since: int, until: int) -> None:
    """The monitor state's alerts (``first_ms`` = first detection of the current episode, kept through a cool-down)
    complete the log: an earlier first detection moves an episode's start, an alert the log no longer holds (rotated)
    becomes an episode of its own."""
    alerts = state.get("alerts") if isinstance(state.get("alerts"), dict) else {}
    for key, a in alerts.items():
        if not isinstance(a, dict) or a.get("level") not in ("warn", "critical"):
            continue
        first = a.get("first_ms")
        if not isinstance(first, int) or not since <= first < until:
            continue
        title = str(a.get("title") or key)
        last = min(int(a.get("cleared_ms") or a.get("last_ms") or first), until)
        match = next((e for e in episodes if e["kind"] == "monitor" and e["title"] == title
                      and e["first_ms"] - EPISODE_GAP_MS <= first <= e["last_ms"] + EPISODE_GAP_MS), None)
        if match is not None:
            if first < match["first_ms"]:
                match["first_ms"] = first
            match["source"] = "monitor.jsonl + state"
            if a.get("level") == "critical":
                match["level"] = "critical"
        else:
            episodes.append({"kind": "monitor", "title": title, "level": a["level"], "first_ms": first,
                             "last_ms": last, "n": None, "text": f"alert {key}", "onset_ms": None,
                             "source": "monitor_state.json"})


INCIDENT_EVENTS = ("system_suspend", "stopped", "started", "killed", "exited", "cycle_error", "order_failed")


def system_events(systems: list[tuple[str, Settings]], since: int, until: int) -> list[dict[str, Any]]:
    """Suspends, supervisor stops/starts, kills/exits, cycle errors and failed orders of every system in the window,
    grouped per system and kind (:data:`GROUP_GAP_MS`), then merged across systems (one machine sleep recorded by
    three supervisors is one row)."""
    rp = pack_module()
    dbs: list[tuple[str, Path]] = []
    seen: set[Path] = set()
    for name, sp in systems:
        db = sp.paths.state() / "app.db"
        if db not in seen:
            seen.add(db)
            dbs.append((sp.paths.instance or "all-pairs", db))
    rows: list[dict[str, Any]] = []
    for name, db in dbs:
        con = rp.ro(db)
        if con is None:
            continue
        try:
            evs = con.execute(f"SELECT ts, collector, event, detail, duration_ms FROM ingestion_events WHERE ts>=? AND "
                              f"ts<? AND event IN ({','.join('?' * len(INCIDENT_EVENTS))}) ORDER BY ts",
                              (since, until, *INCIDENT_EVENTS)).fetchall()
        except sqlite3.Error:
            continue
        finally:
            con.close()
        groups: dict[tuple[str, str], dict[str, Any]] = {}
        for ts, col, ev, det, dur in evs:
            if ev == "started":
                col = "supervisor:*"                         # every service of one start is one restart
            key = (str(col), ev)
            g = groups.get(key)
            if g is None or ts - g["last_ms"] > GROUP_GAP_MS:
                g = {"kind": "event", "title": f"{col} {ev}", "level": "event", "first_ms": ts, "last_ms": ts,
                     "n": 0, "text": _short(det, 140), "systems": [name], "onset_ms": None,
                     "source": "ingestion_events", "minutes": None}
                groups[key] = g
                rows.append(g)
            g["last_ms"] = ts
            g["n"] += 1
            if ev == "system_suspend":
                g["first_ms"] = min(g["first_ms"], ts - int(dur or 0))
                g["minutes"] = round((g.get("minutes") or 0) + int(dur or 0) / MS_PER_MINUTE, 1)
    rows.sort(key=lambda r: (r["title"], r["first_ms"]))
    merged: list[dict[str, Any]] = []
    for r in rows:
        prev = merged[-1] if merged else None
        if prev is not None and prev["title"] == r["title"] and abs(r["first_ms"] - prev["first_ms"]) <= MERGE_GAP_MS:
            prev["systems"] = sorted({*prev["systems"], *r["systems"]})
            prev["last_ms"] = max(prev["last_ms"], r["last_ms"])
            prev["n"] += r["n"]
            continue
        merged.append(r)
    for r in merged:
        if r["title"].endswith(" started"):
            r["title"] = "services started (restart)"
        elif r["title"] == "supervisor:all stopped":
            r["title"] = "supervisor shutdown"
        elif r["title"] == "supervisor:all system_suspend":
            r["title"] = f"PC asleep ~{r['minutes']:g} min"
    return sorted(merged, key=lambda r: r["first_ms"])


def unclean_starts(systems: list[tuple[str, Settings]], since: int, until: int) -> list[dict[str, Any]]:
    """The supervisors' starts in the window after a run that ended WITHOUT a stop: the PC shut down or restarted,
    the user logged off, or the supervisor crashed or was killed. A stop (``run --stop`` / STOP_ALL — stop_all,
    restart_all — or Ctrl+C) writes ``supervisor:all stopped``; a supervisor that dies with the machine writes nothing,
    and with Fast Startup on (this laptop) a Start-menu shutdown keeps ``psutil.boot_time()`` — 2026-09-27 09:11 was
    found only this way. A service's ``started`` row is its supervisor's own start unless it follows that service's
    ``exited``/``killed`` within :data:`RESTART_MAX_MS` or across a gap the supervisor lived through (a supervised
    restart; an exit with :data:`SESSION_END_CODE` never is one: the session ended with it); such a start is unclean
    when no ``supervisor:all stopped`` lies between the service's previous row and it (a service without an earlier
    row is not judged). One row per start, merged across services and systems (:data:`MERGE_GAP_MS`: one shutdown
    restarts every system). Rows before the window are read for the context (the cursor is streamed)."""
    rp = pack_module()
    found: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for _, sp in systems:
        db = sp.paths.state() / "app.db"
        if db in seen:
            continue
        seen.add(db)
        con = rp.ro(db)
        if con is None:
            continue
        last: dict[str, tuple[int, str, str]] = {}          # service → its previous (ts, event, detail)
        last_stop = last_gap = None
        try:
            for ts, col, ev, det in con.execute(
                    "SELECT ts, collector, event, detail FROM ingestion_events WHERE ts<? AND collector LIKE "
                    "'supervisor:%' AND event IN ('started', 'exited', 'killed', 'stopped', 'system_suspend', 'stall', "
                    "'clock_jump') ORDER BY ts", (until,)):
                if col == "supervisor:all":
                    if ev == "stopped":
                        last_stop = ts
                    elif ev in SUPERVISOR_GAP_EVENTS:
                        last_gap = ts
                    continue
                svc = str(col).split(":", 1)[1]
                prev = last.get(svc)
                last[svc] = (ts, ev, str(det or ""))
                if ev != "started" or prev is None or ts < since:
                    continue
                pts, pev, pdet = prev
                if pev in ("exited", "killed") and SESSION_END_CODE not in pdet and (
                        ts - pts <= RESTART_MAX_MS or (last_gap is not None and last_gap >= pts)):
                    continue                                    # restarted by its own supervisor
                if last_stop is not None and last_stop >= pts:
                    continue                                    # the start after a stop
                found.append({"ts": ts, "system": sp.paths.instance or "all-pairs", "service": svc,
                              "previous": f"{pev} {iso(pts)}"})
        except sqlite3.Error:
            continue
        finally:
            con.close()
    found.sort(key=lambda r: r["ts"])
    merged: list[dict[str, Any]] = []
    for r in found:
        m = merged[-1] if merged else None
        if m is not None and r["ts"] - m["last_ms"] <= MERGE_GAP_MS:
            m["last_ms"] = r["ts"]
            m["systems"] = sorted({*m["systems"], r["system"]})
            m["services"] += 1
            continue
        merged.append({"first_ms": r["ts"], "last_ms": r["ts"], "first": iso(r["ts"]), "systems": [r["system"]],
                       "services": 1, "previous": f"{r['service']}: {r['previous']}"})
    return merged


# --------------------------------------------------------------------------- account, equity, machine
def account_state(base: Settings, systems: list[tuple[str, Settings]], pairs: dict[str, dict[str, Any]],
                  mon: dict[str, Any]) -> dict[str, Any]:
    """The account as the files show it: the high-water mark(s) (``account_peak.json``, a trip included), the
    newest executor status (equity, balance, drawdown, exposure), the monitor's last equity sample."""
    out: dict[str, Any] = {"peak_file": None, "executor": None, "monitor_equity": None}
    try:
        doc = json.loads((base.paths.shared() / "account_peak.json").read_text(encoding="utf-8"))
        out["peak_file"] = {k: {"peak": v.get("peak"), "peak_at": iso(v.get("peak_ms")) if v.get("peak_ms") else None,
                                "tripped_equity": v.get("tripped_equity"),
                                "tripped_at": iso(v.get("tripped_ms")) if v.get("tripped_ms") else None}
                            for k, v in doc.items() if isinstance(v, dict)} if isinstance(doc, dict) else None
    except (OSError, ValueError) as exc:
        out["peak_file_error"] = f"{type(exc).__name__}: {exc}"[:160]
    newest = None
    for pair, rep in pairs.items():
        ex = ((rep.get("status") or {}).get("executor") or {})
        if ex and (newest is None or (ex.get("updated_ms") or 0) > (newest.get("updated_ms") or 0)):
            newest = {**ex, "system": pair}
    if newest:
        d = newest.get("detail") or {}
        out["executor"] = {"system": newest["system"], "updated": newest.get("updated"), "mode": d.get("mode"),
                           "equity": d.get("equity"), "balance": d.get("balance"), "currency": d.get("currency"),
                           "open_positions": d.get("open_positions"), "open_orders": d.get("open_orders"),
                           "drawdown": d.get("account_drawdown"), "kill_switch": d.get("kill_switch")}
    out["exposure_without_sl"] = sorted(
        f"{pair}:{x.get('decision')}" for pair, rep in pairs.items()
        for x in ((((rep.get("status") or {}).get("executor") or {}).get("detail") or {}).get("exposure") or [])
        if isinstance(x, dict) and x.get("kind") == "position" and (x.get("sl_missing") or not x.get("sl")))
    eq = mon.get("equity") if isinstance(mon.get("equity"), dict) else {}
    out["monitor_equity"] = {k: {"equity": v.get("equity"), "at": iso(v.get("ts")) if v.get("ts") else None}
                             for k, v in eq.items() if isinstance(v, dict)} or None
    return out


def equity_path(pairs: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """The settled outcomes of every pair in time order with the running realised total (USD)."""
    trades = sorted((t for rep in pairs.values() for t in (rep.get("realised") or {}).get("trades", [])),
                    key=lambda t: t["settled_ms"] or 0)
    cum = 0.0
    for t in trades:
        cum += float(t["pnl_usd"] or 0)
        t["cum_usd"] = round(cum, 2)
    return trades


def _xml_attr(ev: str, pattern: str) -> str | None:
    m = re.search(pattern, ev)
    return m.group(1) if m else None


def power_row(ev: str) -> dict[str, Any] | None:
    """One System log ``<Event>`` (wevtutil's XML) → ``{"ms", "kind", "what"}``; kind is shutdown / boot / sleep /
    resume / unclean, None for a row it does not know."""
    prov = (_xml_attr(ev, r"<Provider Name=['\"]([^'\"]+)") or "").removeprefix("Microsoft-Windows-")
    eid = _xml_attr(ev, r"<EventID[^>]*>(\d+)<")
    t = re.search(r"SystemTime=['\"](\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?Z", ev)   # 7 fraction digits
    ms = _ts_ms(f"{t.group(1)}.{(t.group(2) or '0')[:3].ljust(3, '0')}+00:00") if t else None
    if ms is None or eid is None:
        return None
    data = dict(re.findall(r"<Data Name=['\"]([^'\"]+)['\"]>([^<]*)</Data>", ev))
    key = (prov, int(eid))
    if key == ("User32", 1074):
        who = data.get("param1", "").split(" (")[0].replace("\\", "/").rsplit("/", 1)[-1] or "?"
        return {"ms": ms, "kind": "shutdown", "what": f"{data.get('param5') or 'shutdown'} requested by {who} "
                                                    f"({data.get('param3') or '-'})"}
    if key == ("Kernel-General", 13):
        return {"ms": ms, "kind": "shutdown", "what": "the OS shut down"}
    if key == ("Kernel-General", 12):
        return {"ms": ms, "kind": "boot", "what": "the OS started"}
    if key == ("Kernel-Boot", 27):
        bt = data.get("BootType")
        if bt == "2":
            return {"ms": ms, "kind": "resume", "what": "resumed from hibernate"}
        return {"ms": ms, "kind": "boot", "what": {"0": "cold boot", "1": "Fast Startup boot (after a Start-menu shut "
                                                                       "down)"}.get(str(bt), f"boot type {bt}")}
    if key == ("Kernel-Power", 42):
        st = data.get("TargetState")
        if st == "6":
            return {"ms": ms, "kind": "shutdown", "what": "hybrid shutdown (Fast Startup)"}
        return {"ms": ms, "kind": "sleep", "what": {"4": "sleep", "5": "hibernate"}.get(str(st), f"sleep state {st}")}
    if key == ("Kernel-Power", 107):
        return {"ms": ms, "kind": "resume", "what": "resumed"}
    if key == ("Kernel-Power", 41):
        return {"ms": ms, "kind": "unclean", "what": "rebooted without a clean shutdown (Kernel-Power 41)"}
    return None


def power_episodes(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The System log rows in time order → episodes: one shutdown (1074 + 42/6 + 13), one boot (12 + 27, a 41 marks
    it unclean), one sleep (42 → the next resume from hibernate; a resume without its sleep in the window is a sleep
    that began before it). Rows of one kind within :data:`POWER_GROUP_MS` are one episode; 107 is left out (this
    laptop logs it with the sleep's own time, seconds after the 42)."""
    out: list[dict[str, Any]] = []
    open_sleep: dict[str, Any] | None = None
    for r in sorted(rows, key=lambda x: x["ms"]):
        kind = r["kind"]
        if kind == "resume":
            if r["what"] == "resumed":
                continue
            if open_sleep is not None:
                open_sleep["end_ms"] = r["ms"]
                open_sleep = None
            else:
                out.append({"kind": "sleep", "first_ms": r["ms"], "last_ms": r["ms"], "end_ms": r["ms"],
                            "what": [f"{r['what']} (asleep since before the window)"]})
            continue
        if kind == "unclean":
            kind = "boot"                               # logged just after the boot it describes
        prev = next((e for e in reversed(out) if e["kind"] == kind), None)
        if kind != "sleep" and prev is not None and r["ms"] - prev["last_ms"] <= POWER_GROUP_MS:
            prev["last_ms"] = r["ms"]
            if r["what"] not in prev["what"]:
                prev["what"].append(r["what"])
            continue
        e = {"kind": kind, "first_ms": r["ms"], "last_ms": r["ms"], "end_ms": None, "what": [r["what"]]}
        out.append(e)
        if kind == "sleep":
            open_sleep = e
    for e in out:
        e["first"] = iso(e["first_ms"])
        e["what"] = "; ".join(e["what"])
        if e.get("end_ms"):
            e["until"] = iso(e["end_ms"])
    return out


def power_events(since: int, until: int) -> dict[str, Any]:
    """Best effort, read-only: the boots, sleeps and shutdowns the Windows System log holds for the window (one
    ``wevtutil qe`` query, at most :data:`POWER_LOG_TIMEOUT_S`; not Windows, a timeout, an error → ``{"error"}``,
    never raised — the log is evidence beside the supervisors' rows, not a precondition). ``counts`` per
    :data:`POWER_COUNTED` kind."""
    empty = {"episodes": [], "counts": {k: 0 for k in POWER_COUNTED}}
    if os.name != "nt":
        return {**empty, "error": "not read (not Windows)"}
    q = POWER_QUERY.format(since=time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(since / 1000)),
                           until=time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(until / 1000)))
    try:
        r = subprocess.run(["wevtutil", "qe", "System", f"/q:{q}", "/f:xml", "/rd:true",
                            f"/c:{POWER_LOG_MAX_EVENTS}"], capture_output=True, timeout=POWER_LOG_TIMEOUT_S,
                           stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError) as exc:
        return {**empty, "error": f"not read: {type(exc).__name__}: {exc}"[:200]}
    if r.returncode != 0:
        err = (r.stderr or b"").decode("utf-8", "replace").strip()
        return {**empty, "error": f"not read: wevtutil exit {r.returncode}: {err}"[:200]}
    rows = [x for x in (power_row(ev) for ev in POWER_EVENT_RE.findall((r.stdout or b"").decode("utf-8", "replace")))
            if x is not None and since <= x["ms"] < until]
    eps = power_episodes(rows)
    return {"episodes": eps, "counts": {k: sum(1 for e in eps if e["kind"] == k) for k in POWER_COUNTED},
            "rows": len(rows), "error": None}


def machine_now() -> dict[str, Any]:
    """This machine now: free RAM and the last boot time (a boot inside the window = a shutdown or restart in it —
    but not every one: Fast Startup keeps the boot time over a Start-menu shutdown, and only the last boot is seen;
    :func:`unclean_starts` and :func:`power_events` see the others)."""
    try:
        import psutil
        vm = psutil.virtual_memory()
        return {"available_mb": int(vm.available / 2**20), "total_mb": int(vm.total / 2**20),
                "boot_ms": int(psutil.boot_time() * 1000)}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"[:160]}


def mt5_deals(base: Settings, since: int, until: int) -> dict[str, Any]:
    """``--mt5``: the broker's deals of the project's magics in the window, per pair and in time order (the balance
    path), from the execution profile's terminal — only when it runs (never launched), one ``history_deals_get``
    under the machine-wide MT5 history lock, then ``shutdown``. Nothing is sent to the terminal but reads."""
    from tradingsystem.core.filelock import FileLock, locks_dir
    from tradingsystem.ingest.mt5.servertime import ServerTimeModel
    from tradingsystem.ingest.mt5.terminal import AccountMismatch, MT5Terminal, MT5Unavailable, terminal_running
    mode = base.execution.mode if base.execution.mode in base.mt5.execution_profile_by_mode else "demo"
    prof = base.mt5.profiles[base.mt5.execution_profile_by_mode[mode]]
    if not terminal_running(prof.terminal_path):
        return {"error": f"the MT5 terminal {prof.terminal_path} is not running — --mt5 only reads a running one"}
    magic0 = base.execution.magic
    by_magic = {magic0: "all-pairs system"} | {magic0 + i.magic_offset: p for p, i in base.instances.items()}
    model = ServerTimeModel()
    lock = FileLock(locks_dir(base) / "mt5_history.lock")
    try:
        got = lock.acquire(timeout=MT5_LOCK_WAIT_S, poll=0.2)
    except OSError as exc:
        return {"error": f"the MT5 history lock could not be taken: {exc}"[:200]}
    if not got:
        return {"error": f"the MT5 history lock stayed busy for {MT5_LOCK_WAIT_S} s — run again later"}
    term = None
    try:
        term = MT5Terminal(prof)
        acc = term.connect()
        raw = term.mt5.history_deals_get(model.utc_to_server(since - MS_PER_DAY) // 1000,
                                         model.utc_to_server(until) // 1000 + 86_400)
        if raw is None:
            return {"error": f"history_deals_get failed: {term.mt5.last_error()}"}
    except (AccountMismatch, MT5Unavailable, ImportError, OSError, RuntimeError) as exc:
        return {"error": f"{type(exc).__name__}: {exc}"[:200]}
    finally:
        if term is not None:
            term.shutdown()
        lock.release()
    deals = []
    for d in raw:
        if d.magic not in by_magic:
            continue
        t = model.server_to_utc(int(getattr(d, "time_msc", 0) or d.time * 1000), prefer="earlier")
        if not since <= t < until:
            continue
        deals.append({"ms": t, "pair": by_magic[d.magic], "symbol": d.symbol, "profit": float(d.profit),
                      "commission": float(d.commission), "swap": float(d.swap), "fee": float(getattr(d, "fee", 0.0))})
    deals.sort(key=lambda x: x["ms"])
    per: dict[str, dict[str, Any]] = {}
    cum, path = 0.0, []
    for x in deals:
        a = per.setdefault(x["pair"], {"deals": 0, "profit": 0.0, "commission": 0.0, "swap": 0.0, "fee": 0.0})
        a["deals"] += 1
        for k in ("profit", "commission", "swap", "fee"):
            a[k] = round(a[k] + x[k], 4)
        cum += x["profit"] + x["commission"] + x["swap"] + x["fee"]
        path.append({"time": iso(x["ms"]), "pair": x["pair"], "net": round(x["profit"] + x["commission"] + x["swap"]
                                                                          + x["fee"], 2), "cum_usd": round(cum, 2)})
    return {"server": acc.server, "balance_now": acc.balance, "equity_now": acc.equity, "currency": acc.currency,
            "deals": len(deals), "per_pair": per, "net_usd": round(cum, 2), "path": path[-60:]}


# --------------------------------------------------------------------------- the report
def collect(s: Settings, *, since: int | None = None, until: int | None = None, now: int | None = None,
            pair: str | None = None, mt5: bool = False) -> dict[str, Any]:
    """The demo report's data (JSON-ready). ``s``: the base (all-pairs view) settings."""
    now = _now_ms() if now is None else int(now)
    rp = pack_module()
    w = demo_window(s, since=since, until=until, now=now)
    systems = report_systems(s, pair)
    ledger_file = rp.ledger_path(s, [sp for _, sp in systems])
    rows, ledger_err = ledger_rows(ledger_file, w["since_ms"], w["until_ms"])
    pairs = {p: pair_section(sp, p, w, rows, now, s.ai.daily_calls_per_pair) for p, sp in systems}
    mon_state = monitor_state(s)
    mon = monitor_log(s.paths.logs(), w["since_ms"], w["until_ms"])
    merge_state_alerts(mon["episodes"], mon_state, w["since_ms"], w["until_ms"])
    incidents = sorted(mon["episodes"] + system_events(systems, w["since_ms"], w["until_ms"]),
                       key=lambda e: e["first_ms"])
    for e in incidents:
        e["first"], e["last"] = iso(e["first_ms"]), iso(e["last_ms"])
        e["detect_min"] = round((e["first_ms"] - e["onset_ms"]) / MS_PER_MINUTE, 1) if e.get("onset_ms") else None
    account = account_state(s, systems, pairs, mon_state)
    # the mode the systems RUN in (their executor status) — this checkout's config may differ (--root)
    ran = (account.get("executor") or {}).get("mode")
    mode = s.execution.mode if not ran or ran == s.execution.mode else f"{ran} (executor status; config {s.execution.mode})"
    return {
        "kind": "demo", "generated": iso(now), "generated_ms": now, "window": w,
        "pairs": list(pairs), "systems": [sp.paths.instance or "all-pairs" for _, sp in systems],
        "execution_mode": mode, "data_root": str(s.paths.data()), "logs_root": str(s.paths.logs()),
        "config_hash": s.config_hash or None, "git": rp.git_info(ROOT),
        "ledger": {"path": str(ledger_file), "rows": len(rows), "error": ledger_err},
        "per_pair": pairs, "cost": cost_per_day(rows, s), "session_limits": session_limits(rows),
        "incidents": incidents, "monitor": {k: v for k, v in mon.items() if k != "episodes"},
        "account": account, "equity_path": equity_path(pairs),
        "mt5": mt5_deals(s, w["since_ms"], w["until_ms"]) if mt5 else None,
        "unclean_starts": unclean_starts(systems, w["since_ms"], w["until_ms"]),
        "machine": {**machine_now(), "power": power_events(w["since_ms"], w["until_ms"])},
        "sample_size": SAMPLE_SIZE.format(days=w["days"]),
    }


# --------------------------------------------------------------------------- markdown
def render(d: dict[str, Any]) -> str:
    L: list[str] = []
    p = L.append
    w = d["window"]
    so_far = "complete" if w["complete"] else f"so far: {w['days_covered']:g} of {w['days']:g} days"
    p(f"# Demo report — {w['since']} → {w['planned_until']} ({so_far})")
    g = d.get("git") or {}
    p(f"generated {d['generated']} · systems {', '.join(d['systems'])} · execution mode {d['execution_mode']} · "
      f"data root {d['data_root']} · git {g.get('sha', '-')} · config {d['config_hash']}")
    p("")
    p(f"> **Sample size.** {d['sample_size']}")
    p("")
    # ---- summary
    p("## Summary per pair")
    p("| pair | open 15m cycles | availability | calls (ledger) | stored answers | ideas | placed | settled in window "
      "(PnL USD) | virtual resolved (TP1 first) | gate rejections |")
    p("|---|---|---|---|---|---|---|---|---|---|")
    for pair, r in d["per_pair"].items():
        f = r.get("funnel") or {}
        av = r.get("availability") or {}
        rl = r.get("realised") or {}
        calls = sum(x["calls"] for x in (r.get("calls_by_role") or {}).values())
        p(f"| {pair} | {av.get('open_cycles', '-')} | {_pct(av.get('share'))} (lost {av.get('lost_total', '-')}) | "
          f"{calls} | {f.get('calls_stored', '-')} | {f.get('ideas', '-')} | {f.get('placed', '-')} | "
          f"{rl.get('n', '-')} ({_usd(rl.get('pnl_usd'))}) | {f.get('virtual_resolved', '-')} "
          f"({f.get('virtual_tp1_first', '-')}) | {(r.get('gate') or {}).get('rejected', '-')} |")
    p("")
    # ---- funnel per pair
    p("## Funnel and execution quality per pair")
    for pair, r in d["per_pair"].items():
        p(f"### {pair} (system {r.get('system')})")
        if r.get("error"):
            p(f"!! {r['error']}")
        sc = r.get("screens") or {}
        trig = sc.get("triggers_by_strength") or {}
        calls = r.get("calls_by_role") or {}
        p(f"- screens: {sc.get('screens', 0)} closes ({_counts(sc.get('closes_by_tf') or {})}) → triggers "
          f"{sum(trig.values())} ({_counts(trig)})")
        p("- calls by role (shared ledger): " + (", ".join(f"{k} {x['calls']} ({x['ok']} ok)"
                                                          for k, x in sorted(calls.items())) or "none"))
        f = r.get("funnel")
        if f:
            m = f["metrics"]
            p(f"- stored answers {f['calls_stored']}: status {_counts(f['by_status'])}; decisions "
              f"{_counts(f['decisions'])}")
            gt = r.get("gate") or {}
            p(f"- ideas {f['ideas']} → execution {_counts(f['execution'])} → gate rejections {gt.get('rejected', 0)} "
              f"by class: {_counts(gt.get('by_class') or {})}"
              + (f"; expired before execution {gt['expired']}" if gt.get("expired") else "")
              + f" → placed {f['placed']}")
            broker = ", ".join(f"{k} {v['n']} ({v['pnl_usd']:+} USD)" for k, v in f["broker_outcomes"].items())
            p(f"- outcomes of the window's ideas: broker {broker or 'none'} · virtual "
              f"{_counts(f['virtual_outcomes'])} (resolved {f['virtual_resolved']}, TP1 first "
              f"{f['virtual_tp1_first']}, mean R {_num(f['mean_virtual_r'])})")
            dk = r.get("desk")
            if dk and not dk.get("error"):
                sh, ok = dk["shadow"], dk["desk_ok"]
                p(f"- gold desk in shadow (D-049; never sent): shadow ideas {sh['n']}, desk_ok {ok['n']} (resolved "
                  f"{ok['resolved']}, expectancy after the spread {_num(ok['expectancy_r'])} R); calls per UTC day "
                  f"{_counts(dk['calls_per_day'])} — the baseline and the H32 verdict: `tools/desk_report.py --print`")
            elif dk:
                p(f"- gold desk section: {dk['error']}")
            p(f"- decision metrics (n {m['n']}; with excursion n {m['n_with_excursion']}): MFE "
              f"{_num(m['mean_mfe_r'])} R, MAE {_num(m['mean_mae_r'])} R, TP1/2/3 hit {_num(m['tp1_hit_share'])}/"
              f"{_num(m['tp2_hit_share'])}/{_num(m['tp3_hit_share'])}, minutes to resolve "
              f"{_num(m['mean_minutes_to_resolve'], 0)}, exits {_counts(m['exit_reasons'])}, adverse slippage "
              f"{_num(m['mean_slippage_adverse'], 5)} (n {m['slippage_n']}), spread at the gate "
              f"{_num(m['mean_spread_at_gate'], 5)}, commission {_num(m['commission'])} (n {m['commission_n']}), swap "
              f"{_num(m['swap'])} (n {m['swap_n']}), rejected-but-virtual-win {m['rejected_but_virtual_win']}, "
              f"NO_TRADE counterfactual {_num(m['no_trade_mean_counterfactual_atr'])} ATR (n {m['no_trade_n']})")
            p(f"- tokens of the stored calls: in {f['tokens']['input']:,}, out {f['tokens']['output']:,} (n "
              f"{f['calls_stored']}); mean latency {_num(f['mean_latency_s'], 0)} s")
        pa, ru, es = r.get("position_actions") or {}, r.get("rule_executions") or {}, r.get("escalations") or {}
        p(f"- position actions {pa.get('n', 0)} ({_counts(pa.get('by_source_action_status') or {})}); rule executions "
          f"{ru.get('n', 0)} (applied {ru.get('applied', 0)}); escalations {es.get('n', 0)} "
          f"({_counts(es.get('verdicts') or {})})")
        for cls, ex in sorted(((r.get("gate") or {}).get("examples") or {}).items()):
            p(f"  - gate class {cls}: e.g. {ex}")
        placed = r.get("placed_ideas") or []
        if placed:
            p("")
            p("| idea (UTC) | id | side/type | conf | rr | outcome | PnL USD | virtual | MFE/MAE R | exit |")
            p("|---|---|---|---|---|---|---|---|---|---|")
            for i in placed:
                p(f"| {_hm(i['ts'])} | {i['id']} | {i['decision']}/{i['order_type'] or '-'} | {_num(i['confidence'])} | "
                  f"{_num(i['rr'])} | {i['outcome'] or 'open/unsettled'} | {_num(i['pnl_usd'])} | "
                  f"{i['virtual_outcome'] or '-'} | {_num(i['mfe_r'])}/{_num(i['mae_r'])} | {i['exit_reason'] or '-'} |")
        p("")
    # ---- realised + equity
    p("## Realised PnL, costs and the equity path")
    p("Outcomes SETTLED in the window (whenever the idea was made); PnL is net of commission, swap and fee as the MT5 "
      "settlement stores it; a cost is `-` (unknown) where no settlement split was stored (before Phase 4).")
    p("")
    p("| pair | settled | wins / losses | PnL USD | commission (n) | swap (n) | fee (n) |")
    p("|---|---|---|---|---|---|---|")
    tot_n = tot_pnl = 0
    for pair, r in d["per_pair"].items():
        rl = r.get("realised") or {}
        tot_n += rl.get("n", 0)
        tot_pnl += rl.get("pnl_usd", 0.0) or 0.0
        p(f"| {pair} | {rl.get('n', '-')} | {rl.get('wins', '-')} / {rl.get('losses', '-')} | {_usd(rl.get('pnl_usd'))} | "
          f"{_num(rl.get('commission'), 4)} ({rl.get('commission_n', 0)}) | {_num(rl.get('swap'), 4)} "
          f"({rl.get('swap_n', 0)}) | {_num(rl.get('fee'), 4)} ({rl.get('fee_n', 0)}) |")
    p(f"| total | {tot_n} | | {_usd(round(tot_pnl, 2))} | | | |")
    p("")
    path = d.get("equity_path") or []
    if path:
        p("Realised path (settled outcomes in time order, running total USD): " + "; ".join(
            f"{_hm(t['settled_ms'])} {t['pair']} {t['outcome']} {_usd(t['pnl_usd'])} → {t['cum_usd']:+.2f}"
            for t in path[-30:]))
    else:
        p("Realised path: no outcome settled in the window.")
    acc = d.get("account") or {}
    ex = acc.get("executor") or {}
    if ex:
        dd = ex.get("drawdown") or {}
        p(f"Account now (executor status of {ex.get('system')}, {ex.get('updated')}): equity {ex.get('equity')} "
          f"{ex.get('currency') or ''}, balance {ex.get('balance')}, open positions {ex.get('open_positions')}, orders "
          f"{ex.get('open_orders')}, drawdown from the peak {dd.get('drawdown_pct')} % (tripped: {dd.get('tripped')}), "
          f"kill switch {ex.get('kill_switch')}")
    for k, v in (acc.get("peak_file") or {}).items():
        p(f"High-water mark {k}: peak {v.get('peak')} at {v.get('peak_at')}; drawdown stop "
          + (f"TRIPPED at {v['tripped_at']} (equity {v['tripped_equity']})" if v.get("tripped_at") else "not tripped"))
    if acc.get("peak_file_error"):
        p(f"!! account_peak.json unreadable: {acc['peak_file_error']}")
    for k, v in (acc.get("monitor_equity") or {}).items():
        p(f"Monitor's last equity sample {k}: {v.get('equity')} at {v.get('at')}")
    mt = d.get("mt5")
    if mt is None:
        p("MT5 deals history: not read (run with `--mt5` for the broker's own record — read-only, brief).")
    elif mt.get("error"):
        p(f"MT5 deals history: not available — {mt['error']}")
    else:
        p(f"MT5 deals history ({mt['server']}): {mt['deals']} deals of the project's magics, net {mt['net_usd']:+.2f} "
          f"{mt.get('currency') or ''}; balance now {mt['balance_now']}, equity {mt['equity_now']}")
        for k, v in sorted(mt["per_pair"].items()):
            p(f"- {k}: {v['deals']} deals, profit {v['profit']:+.2f}, commission {v['commission']:+.2f}, swap "
              f"{v['swap']:+.2f}, fee {v['fee']:+.2f}")
        if mt.get("path"):
            p("  balance path: " + "; ".join(f"{x['time'][5:16]} {x['pair']} {x['net']:+.2f} → {x['cum_usd']:+.2f}"
                                             for x in mt["path"][-30:]))
    p("")
    # ---- availability
    p("## Availability (15-min cycles, market-open time only)")
    p("A cycle is lost when any of these happened in it (the share counts a cycle once): "
      + "; ".join(f"`{k}` {v}" for k, v in LOSS_CLASSES.items()) + ".")
    p("")
    p("| pair | calendar | cycles (closed) | open | lost | availability | lost by class |")
    p("|---|---|---|---|---|---|---|")
    for pair, r in d["per_pair"].items():
        av = r.get("availability") or {}
        if av.get("error"):
            p(f"| {pair} | - | - | - | - | - | {av['error']} |")
            continue
        p(f"| {pair} | {av['calendar']} | {av['cycles']} ({av['closed_cycles']}) | {av['open_cycles']} | "
          f"{av['lost_total']} | {_pct(av['share'])} (n {av['open_cycles']}) | {_counts(av['lost'])} |")
    for pair, r in d["per_pair"].items():
        av = r.get("availability") or {}
        if av.get("screen_log_from"):
            p(f"- {pair}: the engine log read reaches back to {av['screen_log_from']} (`no_screen` measured from there)")
        for n in av.get("notes") or []:
            p(f"- {pair}: {n}")
    p("")
    # ---- incidents
    p("## Incidents")
    mon = d.get("monitor") or {}
    p(f"Monitor runs in the window: {mon.get('runs', 0)}, largest gap between runs {_num(mon.get('max_gap_min'), 1)} "
      f"min (edges {mon.get('edge_gap_min')}) — the detection time of a finding is bounded by this cadence.")
    inc = d.get("incidents") or []
    if inc:
        p("")
        p("| first detected (UTC) | last seen | level | what | n | systems | onset → detection | detail |")
        p("|---|---|---|---|---|---|---|---|")
        for e in inc:
            det = (f"{_hm(e['onset_ms'])} → {e['detect_min']:g} min" if e.get("detect_min") is not None else "-")
            p(f"| {_hm(e['first_ms'])} | {_hm(e['last_ms'])} | {e['level']} | {_short(e['title'], 60)} | "
              f"{e['n'] if e.get('n') is not None else '-'} | {', '.join(e.get('systems') or []) or '-'} | {det} | "
              f"{_short(e.get('text'), 110).replace('|', '/')} |")
    else:
        p("No incident in the window.")
    p("")
    us = d.get("unclean_starts") or []
    p("Supervisor starts after a run that ended without a stop (a shutdown, restart, logoff, crash or a killed "
      "supervisor — Fast Startup keeps the boot time over a Start-menu shutdown): "
      + ("; ".join(f"{_hm(u['first_ms'])} {', '.join(u['systems'])} ({u['services']} services; the previous row "
                    f"{u['previous']})" for u in us) if us else "none"))
    mach = d.get("machine") or {}
    pw = mach.get("power") or {}
    if pw.get("error"):
        p(f"Windows System log (boots, sleeps, shutdowns): {pw['error']}")
    else:
        p("Windows System log (boots, sleeps, shutdowns): " + ("; ".join(
            f"{_hm(e['first_ms'])} {e['kind']}" + (f" → {_hm(e['end_ms'])}" if e.get("end_ms") else "")
            + f" ({e['what']})" for e in pw.get("episodes") or []) or "none in the window"))
    if mach.get("boot_ms"):
        p(f"Last boot (psutil): {iso(mach['boot_ms'])}")
    p("")
    # ---- cost
    c = d.get("cost") or {}
    led = d.get("ledger") or {}
    p("## AI cost per day (shared ledger)")
    if led.get("error"):
        p(f"!! {led['error']}")
    p(f"Gauge-weighted = the claude_code rows' fresh input + output + cache reads × {c.get('cache_read_weight')} "
      "(ai/usage_gauge.py). API-equivalent USD only for models with a price in `ai.providers.*.model_prices` (n priced "
      "of the rows with tokens); the CLI's own `total_cost_usd` beside it.")
    p("")
    p("| day (UTC) | calls (ok) | by role | input (cache-read share) | output | gauge-weighted | API-equivalent USD "
      "(priced n / n) | CLI USD | usage unknown |")
    p("|---|---|---|---|---|---|---|---|---|")
    for day, a in (c.get("days") or {}).items():
        p(f"| {day} | {a['calls']} ({a['ok']}) | {_counts(a['by_role'])} | {a['input']:,} ({_num(a['cache_read_share'])}) "
          f"| {a['output']:,} | {a['weighted']:,} | {a['api_usd']:.2f} ({a['priced_n']}/{a['with_tokens_n']}) | "
          f"{a['cli_usd']:.2f} | {a['usage_unknown']} |")
    t = c.get("total") or {}
    if t:
        p(f"| total | {t['calls']} ({t['ok']}) | | {t['input']:,} ({_num(t['cache_read_share'])}) | {t['output']:,} | "
          f"{t['weighted']:,} | {t['api_usd']:.2f} ({t['priced_n']}/{t['with_tokens_n']}) | {t['cli_usd']:.2f} | "
          f"{t['usage_unknown']} |")
    sl = d.get("session_limits") or {}
    p("")
    p(f"Subscription-limit errors: {sl.get('rows', 0)} ledger rows in {len(sl.get('events') or [])} event(s)"
      + ("".join(f"; {e['time']} {', '.join(e['pairs'])}" for e in sl.get("events") or [])))
    for pair, r in d["per_pair"].items():
        cap = r.get("cap") or {}
        cpd = r.get("calls_per_day") or {}
        p(f"- {pair} calls per UTC day against the cap {cap.get('value')} ({cap.get('source')}): "
          + (", ".join(f"{k} {v}" for k, v in cpd.items()) or "none"))
    p("")
    # ---- versions + tuning
    p("## Versions seen in the window")
    for pair, r in d["per_pair"].items():
        hs = r.get("hashes_seen") or {}
        parts = []
        for col, vals in hs.items():
            if vals:
                parts.append(f"{col} " + ", ".join(f"{v['value']} (n {v['n']}, {v['first'][5:16]}→{v['last'][5:16]})"
                                                   for v in vals))
        p(f"- {pair}: " + ("; ".join(parts) or "no decision in the window"))
    p("")
    p("## Tuning changes in the window")
    any_tc = False
    for pair, r in d["per_pair"].items():
        for x in r.get("tuning_changes") or []:
            if x.get("in_window"):
                any_tc = True
                p(f"- {pair} {x['time']} {x['key']} {x['old']} → {x['new']} by {x['actor']} ({x['reason']})"
                  + (f", reverted {x['reverted']}" if x.get("reverted") else ""))
    if not any_tc:
        p("none")
    p("")
    p("## Go-live")
    p("`tools/go_live_inputs.py` prints this window's measured values against docs/go_live_checklist.md (pass / FAIL "
      "/ n.a. with the value and its n); the owner ticks the rest and decides.")
    p("")
    p(f"> **Sample size.** {d['sample_size']}")
    return "\n".join(L) + "\n"


def _json_safe(d: dict[str, Any]) -> str:
    return json.dumps(d, ensure_ascii=False, indent=1, default=str)


# --------------------------------------------------------------------------- CLI
def _utc_arg(text: str) -> int:
    try:
        return parse_date_spec(text)
    except (ValueError, TypeError) as exc:
        raise argparse.ArgumentTypeError(f"{text!r}: an ISO-8601 UTC time (…Z) or -12h/-5d") from exc


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = _Parser(prog="demo_report.py", description=__doc__.split("\n\n")[0])
    ap.add_argument("--since", type=_utc_arg, help="window start (default: evaluation.demo_start_utc)")
    ap.add_argument("--until", type=_utc_arg, help="window end (default: start + evaluation.demo_days)")
    ap.add_argument("--pair", help="only this pair")
    ap.add_argument("--out", help=f"markdown file (default {DEFAULT_OUT.relative_to(ROOT)}, refused when this "
                                  "checkout's data root holds a system's app.db and no --root is given — the "
                                  "production checkout: use --print or --out data\\reviews\\demo.md there)")
    ap.add_argument("--json", dest="json_out", help="also write the data as JSON to this file")
    ap.add_argument("--print", dest="print_only", action="store_true", help="print the markdown, write nothing")
    ap.add_argument("--root", help="read another checkout's data/ and logs/ (read-only) with this checkout's config")
    ap.add_argument("--mt5", action="store_true", help="also read the MT5 deals history (running terminal only, "
                                                       "read-only, under the MT5 history lock)")
    try:
        a = ap.parse_args(argv)
        if a.root and not (Path(a.root) / "data").is_dir():
            raise Invalid(f"--root {a.root}: no data/ folder there")
        s = with_root(load_settings(), Path(a.root) if a.root else None)
        if not a.out and not a.print_only and not a.root:
            dbs = [d / "app.db" for d in system_state_dirs(s) if (d / "app.db").exists()]
            if dbs:                     # the production checkout: docs/ would turn dirty and can block the next ff-merge
                raise Invalid(f"this checkout runs a system ({dbs[0]}): the default {DEFAULT_OUT} would make it dirty "
                              "and can block the next git merge --ff-only — use --print or --out data\\reviews\\demo.md")
        t0 = time.monotonic()
        data = collect(s, since=a.since, until=a.until, pair=a.pair, mt5=a.mt5)
        data["build_s"] = round(time.monotonic() - t0, 1)
        md = render(data)
        if a.print_only:
            print(md)
            return EXIT_OK
        rp = pack_module()
        out = Path(a.out) if a.out else DEFAULT_OUT
        out.parent.mkdir(parents=True, exist_ok=True)
        rp._write_text(out, md)
        if a.json_out:
            jp = Path(a.json_out)
            jp.parent.mkdir(parents=True, exist_ok=True)
            rp._write_text(jp, _json_safe(data))
    except Invalid as exc:
        print(f"invalid: {exc}")
        return EXIT_INVALID
    except SystemExit as exc:           # --help
        return int(exc.code or 0)
    except Exception as exc:  # noqa: BLE001 — one clear line
        print(f"error: {type(exc).__name__}: {exc}")
        return EXIT_ERROR
    print(f"demo report {data['window']['since']} → {data['window']['until']}: {out} ({len(md):,} chars, "
          f"{data['build_s']} s)" + (f", {a.json_out}" if a.json_out else ""))
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
