"""Go-live inputs (Phase 5 A8 = §3.9 item 13): the measured go-live table against docs/go_live_checklist.md.

    python tools/go_live_inputs.py                          → the table over the demo window (so far)
    python tools/go_live_inputs.py --json                   → the same as JSON
    python tools/go_live_inputs.py --since … --until … [--pair ETHUSDT] [--root C:\\the_claude_new]

It collects ``tools/demo_report.py``'s data over ``evaluation.demo_start_utc`` + ``demo_days`` (cut at now) and judges
every checklist item: ``pass`` / ``FAIL`` / ``n.a.``, each with the measured value and its n. What the files cannot
prove — the monitor drill, a restored backup, P7.2, the tests on the live commit, the live-refusal rehearsal, the
signed sample-size statement, the go-live scope, the decision role separated from development — is ``n.a.`` with
what the owner does, plus whatever evidence exists.
The thresholds are this file's constants and docs/go_live_checklist.md's table (D-046, D-047); change both together.

Read-only (as the demo report). Exit 0 always — it informs, the owner decides; an error is one line (``"error"`` in
the JSON).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
import time
import types
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tradingsystem.core.settings import Settings, load_settings  # noqa: E402
from tradingsystem.core.timeutil import iso  # noqa: E402

TOOLS = Path(__file__).resolve().parent
PASS, FAIL, NA = "pass", "FAIL", "n.a."
# ---- thresholds (docs/go_live_checklist.md; D-046, D-047)
DEMO_DAYS_MIN = 5                   # the demo window on Phase 4+ code
AVAILABILITY_MIN = 0.95             # of the market-open 15-min cycles, every pair
SESSION_LIMIT_EVENTS_MAX = 1        # subscription-limit hits in the window
MTTD_MAX_MIN = 15.0                 # monitor: onset → first detection (proven by one drill)
SNAPSHOT_MEDIAN_MAX_MS = 3000       # the engine's payload build, median
FREE_RAM_MIN_MB = 1024
RISK_LIMITS = {"risk_per_trade_pct": 1.0, "max_risk_per_trade_pct": 3.0, "max_daily_loss_pct": 10.0,
               "max_correlated_risk_pct": 4.0, "max_open_positions": 3}      # D-036, unchanged at go-live (D-046 d)
# gate rejections that are the gate doing its job at ≈ $100 (docs/go_live_checklist.md lists why); any other class —
# stop_loss_present, sl_side, is_trade, quote_fresh, basis, daily_loss_limit, account_drawdown, position_size for
# another reason than the minimum lot, a rejection without a gate record (not_gated: …), an order the broker refused
# after a passed gate (not_placed: …) — is a defect or a trip to explain first
EXPECTED_GATE_CLASSES = {
    "position_size_min_lot": "the 0.01-lot minimum risks more than max_risk_per_trade_pct at ≈ $100 (D-046 d)",
    "rr_after_costs": "reward-to-risk after spread and commission below risk.min_rr",
    "sl_min_distance": "stop closer than the ATR / broker minimum",
    "sl_max_distance": "stop farther than risk.sl_atr_max_mult ATR",
    "correlated_exposure": "the BTC+ETH group would exceed risk.max_correlated_risk_pct",
    "spread_vs_sl": "the spread is too large a share of the stop distance",
    "confidence": "below the confidence floor (risk.min_confidence / the adaptive floor)",
    "kill_switch": "a kill switch was on (the owner or the monitor)",
    "max_open_positions": "risk.max_open_positions reached",
    "no_same_direction": "a same-direction position or order of the pair is already live",
    "market_open": "the execution market was closed at the gate",
    "not_expired": "the idea's valid_until passed before the gate",
    "recommendation_age": "the idea was older than risk.max_recommendation_age_s at the gate",
    "market_in_zone": "the market left the entry zone",
    "pending_price_valid": "the pending price is on the wrong side of the market",
    "effective_leverage": "the size would exceed risk.max_effective_leverage",
    "daily_loss_worst_case": "realised + open SL risk + this trade would pass the daily limit (protective refusal)",
    "news_blackout": "inside a scheduled high-impact release's blackout window (B8; XAUUSD only)",
}
TRIP_GATE_CLASSES = ("daily_loss_limit", "account_drawdown")
TRIP_TITLES = re.compile(r"daily loss limit|drawdown stop tripped", re.I)


def _load(name: str, path: Path) -> types.ModuleType:
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


def demo_module() -> types.ModuleType:
    """``tools/demo_report.py`` (imported: the data is collected once, the same way)."""
    return _load("ts_tools_demo_report", TOOLS / "demo_report.py")


def _item(key: str, item: str, threshold: str, how: str, status: str, measured: str,
          n: int | None = None) -> dict[str, Any]:
    return {"id": key, "item": item, "threshold": threshold, "how": how, "status": status, "measured": measured,
            "n": n}


def _pct(x: float | None) -> str:
    return "-" if x is None else f"{x * 100:.1f} %"


# --------------------------------------------------------------------------- the checks
def check_window(d: dict[str, Any]) -> dict[str, Any]:
    w = d["window"]
    ok = w["complete"] and w["days"] >= DEMO_DAYS_MIN
    shas = sorted({v["value"] for r in d["per_pair"].values() for v in (r.get("hashes_seen") or {}).get("git_sha", [])})
    return _item("demo_days", f"the {DEMO_DAYS_MIN} demo days on Phase 4+ code complete", f"≥ {DEMO_DAYS_MIN} days, "
                 "window ended", "auto", PASS if ok else FAIL,
                 f"{w['days_covered']:g} of {w['days']:g} days ({w['since']} → {w['planned_until']}); git of the "
                 f"window's decisions: {', '.join(shas) or '-'}")


def check_outcomes(d: dict[str, Any]) -> dict[str, Any]:
    parts, n = [], 0
    for pair, r in d["per_pair"].items():
        rl, f = r.get("realised") or {}, r.get("funnel") or {}
        n += rl.get("n", 0)
        parts.append(f"{pair}: settled {rl.get('n', 0)} ({rl.get('wins', 0)} won, {rl.get('losses', 0)} lost, "
                     f"{(rl.get('pnl_usd') or 0):+.2f} USD), virtual resolved {f.get('virtual_resolved', 0)} "
                     f"(TP1 first {f.get('virtual_tp1_first', 0)})")
    return _item("outcomes", "resolved outcomes per pair", "REPORTED, not required (5 days cannot reach 30)",
                 "auto (reported)", NA, "; ".join(parts) or "no pair", n)


def check_availability(d: dict[str, Any]) -> dict[str, Any]:
    parts, n, ok, known = [], 0, True, False
    for pair, r in d["per_pair"].items():
        av = r.get("availability") or {}
        share = av.get("share")
        n += av.get("open_cycles") or 0
        if share is None:
            parts.append(f"{pair} - ({av.get('error') or 'no open cycle'})")
            ok = False
            continue
        known = True
        ok = ok and share >= AVAILABILITY_MIN
        lost = ", ".join(f"{k} {v}" for k, v in (av.get("lost") or {}).items())
        parts.append(f"{pair} {_pct(share)} (n {av.get('open_cycles')}; lost {av.get('lost_total')}"
                     + (f": {lost}" if lost else "") + ")")
    return _item("availability", "availability of the 15-min cycles (market-open time)",
                 f"≥ {AVAILABILITY_MIN * 100:.0f} % every pair", "auto", (PASS if ok else FAIL) if known else NA,
                 "; ".join(parts) or "no pair", n)


def check_sl(d: dict[str, Any]) -> dict[str, Any]:
    eps = [e for e in d["incidents"] if e.get("kind") == "monitor" and "position without SL" in e.get("title", "")]
    unproven = sum((r.get("sl_proof") or {}).get("sl_not_proven", 0) for r in d["per_pair"].values())
    placed = sum((r.get("sl_proof") or {}).get("placed", 0) for r in d["per_pair"].values())
    now = (d.get("account") or {}).get("exposure_without_sl") or []
    bad = len(eps) + unproven + len(now)
    return _item("no_position_without_sl", "positions ever without SL", "0", "auto", PASS if bad == 0 else FAIL,
                 f"monitor 'position without SL' findings {len(eps)}; placed ideas without a passing "
                 f"stop_loss_present gate check {unproven} of {placed}; open now without SL {len(now)}"
                 + (f" ({', '.join(now)})" if now else ""), placed)


def check_trips(d: dict[str, Any]) -> dict[str, Any]:
    acc = d.get("account") or {}
    tripped = [f"{k} at {v['tripped_at']}" for k, v in (acc.get("peak_file") or {}).items() if v.get("tripped_at")]
    ex_dd = ((acc.get("executor") or {}).get("drawdown") or {}).get("tripped")
    eps = [e for e in d["incidents"] if e.get("kind") == "monitor" and (
        TRIP_TITLES.search(e.get("title", "")) or (e.get("title") == "Equity drop" and e.get("level") == "critical"))]
    gate = {c: sum(((r.get("gate") or {}).get("by_class") or {}).get(c, 0) for r in d["per_pair"].values())
            for c in TRIP_GATE_CLASSES}
    bad = bool(tripped) or bool(ex_dd) or bool(eps) or any(gate.values())
    return _item("no_loss_trip", "no daily-loss or drawdown trip", "none", "auto", FAIL if bad else PASS,
                 f"drawdown stop tripped: {', '.join(tripped) or 'no'} (executor: {ex_dd or 'no'}); monitor daily-loss / "
                 f"drawdown / critical equity-drop findings {len(eps)}; gate refusals "
                 + ", ".join(f"{k} {v}" for k, v in gate.items()), len(eps))


def check_gate(d: dict[str, Any]) -> dict[str, Any]:
    by: dict[str, int] = {}
    n = 0
    for r in d["per_pair"].values():
        g = r.get("gate") or {}
        n += g.get("rejected", 0)
        for k, v in (g.get("by_class") or {}).items():
            by[k] = by.get(k, 0) + v
    bad = {k: v for k, v in by.items() if k not in EXPECTED_GATE_CLASSES}
    listed = ", ".join(f"{k} {v}" for k, v in sorted(by.items(), key=lambda kv: -kv[1])) or "none"
    return _item("gate_classes", "every gate rejection in an expected class",
                 "no class outside docs/go_live_checklist.md's list", "auto", FAIL if bad else PASS,
                 f"{n} rejections: {listed}; unexpected: "
                 + (", ".join(f"{k} {v}" for k, v in bad.items()) or "none"), n)


def check_calls(d: dict[str, Any]) -> dict[str, Any]:
    parts, ok, days = [], True, 0
    for pair, r in d["per_pair"].items():
        cap = (r.get("cap") or {}).get("value")
        cpd = r.get("calls_per_day") or {}
        days += len(cpd)
        top = max(cpd.values()) if cpd else 0
        over = [k for k, v in cpd.items() if cap is not None and v > cap]
        ok = ok and not over
        parts.append(f"{pair} max {top}/{cap} a day over {len(cpd)} day(s)" + (f", OVER on {', '.join(over)}"
                                                                               if over else ""))
    sl = d.get("session_limits") or {}
    ev = len(sl.get("events") or [])
    ok = ok and ev <= SESSION_LIMIT_EVENTS_MAX
    return _item("calls_cap", "calls/day ≤ cap and subscription-limit errors",
                 f"every pair ≤ its cap every UTC day; ≤ {SESSION_LIMIT_EVENTS_MAX} limit event", "auto",
                 PASS if ok else FAIL, "; ".join(parts) + f"; subscription-limit errors {sl.get('rows', 0)} row(s) in "
                 f"{ev} event(s) (the window's part of its first and last UTC day)", days)


def check_mttd(d: dict[str, Any]) -> dict[str, Any]:
    mon = d.get("monitor") or {}
    delays = [e["detect_min"] for e in d["incidents"] if e.get("detect_min") is not None]
    ev = (f"; findings that name their onset: n {len(delays)}, detection {min(delays):g}–{max(delays):g} min"
          if delays else "; no finding named its onset")
    return _item("monitor_mttd", "monitor MTTD proven by one drill", f"≤ {MTTD_MAX_MIN:g} min", "owner (drill)", NA,
                 f"owner: run the drill in docs/go_live_checklist.md and tick. Evidence: {mon.get('runs', 0)} monitor "
                 f"runs, largest gap {mon.get('max_gap_min')} min{ev}", len(delays))


def check_snapshot(d: dict[str, Any]) -> dict[str, Any]:
    parts, ok, known, n = [], True, False, 0
    for pair, r in d["per_pair"].items():
        sb = ((((r.get("status") or {}).get("engine") or {}).get("detail") or {}).get("snapshot_build_ms") or {})
        med = sb.get("median")
        if not isinstance(med, (int, float)):
            parts.append(f"{pair} -")
            continue
        known = True
        n += int(sb.get("n") or 0)
        ok = ok and med <= SNAPSHOT_MEDIAN_MAX_MS
        parts.append(f"{pair} {med:.0f} ms (n {sb.get('n')}, max {sb.get('max')})")
    slow = [e for e in d["incidents"] if e.get("kind") == "monitor" and "slow snapshot build" in e.get("title", "")]
    ok = ok and not slow
    return _item("snapshot_build", "median snapshot_build_ms", f"≤ {SNAPSHOT_MEDIAN_MAX_MS / 1000:g} s",
                 "auto (now)", (PASS if ok else FAIL) if known else NA,
                 "engine status now (its last builds): " + ", ".join(parts)
                 + f"; monitor 'slow snapshot build' findings in the window {len(slow)}", n)


def check_ram(d: dict[str, Any]) -> dict[str, Any]:
    m = d.get("machine") or {}
    avail = m.get("available_mb")
    low = [e for e in d["incidents"] if e.get("kind") == "monitor" and e.get("title") == "Low free RAM"]
    ram_min = (d.get("monitor") or {}).get("ram_min_mb")
    if avail is None:
        return _item("free_ram", "free RAM", f"≥ {FREE_RAM_MIN_MB} MB", "auto", NA, m.get("error") or "unknown")
    ok = avail >= FREE_RAM_MIN_MB and not low
    return _item("free_ram", "free RAM", f"≥ {FREE_RAM_MIN_MB} MB (now, and no low-RAM finding in the window)",
                 "auto", PASS if ok else FAIL,
                 f"now {avail} MB of {m.get('total_mb')} MB; monitor 'Low free RAM' episodes {len(low)}"
                 + (f", lowest seen {ram_min} MB" if ram_min is not None else ""), len(low))


def check_sleep(d: dict[str, Any]) -> dict[str, Any]:
    """Four sources, any one fails the item: the supervisors' ``system_suspend`` rows; their starts after a run that
    ended without a stop (a shutdown, restart, logoff or crash — the only trace of a Start-menu shutdown under Fast
    Startup, which keeps the boot time); every boot, sleep and shutdown of the Windows System log (best effort: an
    unread log is said, not counted); the last boot time. A stop_all/restart_all (``supervisor shutdown``) is listed,
    not counted."""
    w = d["window"]
    m = d.get("machine") or {}
    sleeps = [e for e in d["incidents"] if e.get("kind") == "event" and str(e.get("title", "")).startswith("PC asleep")]
    unclean = d.get("unclean_starts") or []
    power = m.get("power") or {}
    counted = [e for e in power.get("episodes") or [] if e.get("kind") in ("shutdown", "boot", "sleep")]
    boot = m.get("boot_ms")
    booted_in = boot is not None and w["since_ms"] <= boot < w["until_ms"]
    stops = [e for e in d["incidents"] if e.get("title") == "supervisor shutdown"]
    ok = not sleeps and not unclean and not counted and not booted_in
    if power.get("error"):
        log_part = f"; System log: {power['error']}"
    elif "episodes" in power:
        log_part = "; System log: " + ", ".join(f"{k}s {sum(1 for e in counted if e['kind'] == k)}"
                                                for k in ("shutdown", "boot", "sleep"))
        log_part += (" (" + ", ".join(f"{e['first'][5:16]} {e['kind']}: {e['what']}" for e in counted) + ")"
                     if counted else "")
    else:
        log_part = "; System log: not read"
    return _item("no_sleep_shutdown", "sleep / shutdown events in the window", "0", "auto", PASS if ok else FAIL,
                 f"suspends {len(sleeps)}" + (" (" + ", ".join(f"{e['first'][5:16]} {e['title']}" for e in sleeps)
                                              + ")" if sleeps else "")
                 + f"; supervisor starts without a stop (shutdown / restart / logoff / crash) {len(unclean)}"
                 + (" (" + ", ".join(f"{u['first'][5:16]} {', '.join(u['systems'])}" for u in unclean) + ")"
                    if unclean else "")
                 + log_part
                 + f"; last boot {iso(boot) if boot else 'unknown'}" + (" — INSIDE the window (a shutdown or restart)"
                                                                         if booted_in else "")
                 + f"; supervisor shutdowns {len(stops)} (planned restarts; not counted)",
                 len(sleeps) + len(unclean) + len(counted) + int(booted_in))


def check_backup(s: Settings, root: Path) -> dict[str, Any]:
    p = Path(s.backup.dir)
    bdir = p if p.is_absolute() else root / p
    zips = sorted(x.name for x in bdir.glob("*.zip") if ".incomplete" not in x.name) if bdir.is_dir() else []
    restored: list[str] = []
    log = s.paths.logs() / "backup.jsonl"
    try:
        for line in log.read_text(encoding="utf-8", errors="replace").split("\n"):
            if '"restored ' in line:
                try:
                    j = json.loads(line)
                except ValueError:
                    continue
                if str(j.get("msg", "")).startswith("restored "):
                    restored.append(f"{str(j.get('ts', ''))[:16]} {str(j.get('msg'))[:120]}")
    except OSError:
        pass
    return _item("backup_restored", "a backup restored once", "one restore (docs/ops_windows.md §9)",
                 "owner (evidence: logs/backup.jsonl)", PASS if restored else NA,
                 f"backups in {bdir}: {len(zips)}" + (f" (newest {zips[-1]})" if zips else "") + "; restores logged: "
                 + (f"{len(restored)}, last {restored[-1]}" if restored else
                    "none here (a rehearsal on a scratch root from a worktree logs there — owner ticks)"),
                 len(restored))


def check_risk(s: Settings, d: dict[str, Any]) -> dict[str, Any]:
    r = s.risk
    diff = {k: (getattr(r, k), v) for k, v in RISK_LIMITS.items() if float(getattr(r, k)) != float(v)}
    eq = ((d.get("account") or {}).get("executor") or {}).get("equity")
    return _item("risk_limits", "risk limits unchanged (1 % / 3 % / 10 % daily / 4 % correlated / 3 open)",
                 "as D-036", "auto (config)", FAIL if diff else PASS,
                 ("differs: " + ", ".join(f"{k} {a} (want {b})" for k, (a, b) in diff.items()) if diff else
                  "as D-036") + f"; demo equity now {eq}")


def owner_items(d: dict[str, Any]) -> list[dict[str, Any]]:
    g = d.get("git") or {}
    return [
        _item("p7_2", "P7.2 decided or explicitly deferred", "a D entry", "owner", NA,
              "owner: PROJECT_STATUS P7.2 (de facto venue: Windsor MT5)"),
        _item("tests_green", "tests green on the live commit", "the full unit suite", "owner", NA,
              "owner/lead: .venv\\Scripts\\python.exe -m pytest tests/unit -q -p no:cacheprovider on the exact commit "
              "that goes live, from a worktree or with production stopped (or the lead's recorded green run of that "
              f"sha) — never beside the running systems (RAM) (this checkout: {g.get('sha', '-')}"
              + (", DIRTY" if g.get("dirty") else "") + ")"),
        _item("live_refusal", "the live-refusal rehearsal", "fails with 'account mismatch'", "owner", NA,
              "owner: docs/go_live_checklist.md 'Live-refusal rehearsal' on a scratch root"),
        _item("sample_size", "the sample-size statement signed", "signed knowingly", "owner", NA, d["sample_size"]),
        _item("go_live_scope", "go-live = ONE pair (ETH) at the minimum lot, capital ≈ $100", "the owner's setup",
              "owner", NA, "owner: live account ≈ $100, only ETHUSDT switched to live, everything else stays demo"),
        _item("decision_role", "the decision role separated from development",
              "a second Claude account or an API key with hard USD caps (D-046 e), or a D entry deferring it", "owner",
              NA, "owner: a D entry. Also reconcile in a D entry (not a gate): D-046 moves production off the "
                  "development laptop 'before go-live', handoff §3.9.1's sequence puts it under 'Later'"),
    ]


def measure(d: dict[str, Any], s: Settings, root: Path = ROOT) -> list[dict[str, Any]]:
    """The checklist rows in docs/go_live_checklist.md's order."""
    items = [check_window(d), check_outcomes(d), check_availability(d), check_sl(d), check_trips(d), check_gate(d),
             check_calls(d), check_mttd(d), check_snapshot(d), check_ram(d), check_sleep(d), check_backup(s, root),
             check_risk(s, d)]
    return items + owner_items(d)


def evidence(s: Settings, *, now: int | None = None, since: int | None = None, until: int | None = None,
             pair: str | None = None, root: Path = ROOT) -> dict[str, Any]:
    """Collect over the window and judge: ``{"window", "items", "summary"}`` or ``{"error"}`` (never raises — the
    weekly review pack carries it)."""
    try:
        dr = demo_module()
        d = dr.collect(s, since=since, until=until, now=now, pair=pair)
        items = measure(d, s, root)
    except Exception as exc:  # noqa: BLE001 — informs; never fails its caller
        return {"error": f"{type(exc).__name__}: {exc}"[:300]}
    summary = {st: sum(1 for i in items if i["status"] == st) for st in (PASS, FAIL, NA)}
    return {"generated": d["generated"], "window": d["window"], "items": items, "summary": summary,
            "sample_size": d["sample_size"]}


def render(ev: dict[str, Any]) -> str:
    if ev.get("error"):
        return f"go-live inputs: error: {ev['error']}\n"
    w = ev["window"]
    so_far = "complete" if w["complete"] else f"so far {w['days_covered']:g} of {w['days']:g} days"
    out = [f"# Go-live inputs — demo window {w['since']} → {w['planned_until']} ({so_far}) — generated "
           f"{ev['generated']}", "",
           f"pass {ev['summary'][PASS]} · FAIL {ev['summary'][FAIL]} · n.a. {ev['summary'][NA]} "
           "(thresholds: docs/go_live_checklist.md; the owner decides)", "",
           "| # | item | threshold | status | measured | n | how |", "|---|---|---|---|---|---|---|"]
    for k, i in enumerate(ev["items"], 1):
        out.append(f"| {k} | {i['item']} | {i['threshold']} | **{i['status']}** | "
                   f"{str(i['measured']).replace('|', '/')} | {'-' if i['n'] is None else i['n']} | {i['how']} |")
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    args = sys.argv[1:] if argv is None else list(argv)
    as_json = "--json" in args
    try:
        dr = demo_module()
        ap = argparse.ArgumentParser(prog="go_live_inputs.py", description=__doc__.split("\n\n")[0])
        ap.add_argument("--since", type=dr._utc_arg, help="window start (default: evaluation.demo_start_utc)")
        ap.add_argument("--until", type=dr._utc_arg, help="window end (default: start + evaluation.demo_days)")
        ap.add_argument("--pair", help="only this pair")
        ap.add_argument("--root", help="another checkout's data/ and logs/ (read-only), this checkout's config")
        ap.add_argument("--json", action="store_true", help="JSON instead of the table")
        try:
            a = ap.parse_args(args)
        except SystemExit:                  # --help, or bad arguments (argparse printed why): exit 0 always
            return 0
        root = Path(a.root) if a.root else ROOT
        s = dr.with_root(load_settings(), Path(a.root) if a.root else None)
        t0 = time.monotonic()
        ev = evidence(s, since=a.since, until=a.until, pair=a.pair, root=root)
        ev["build_s"] = round(time.monotonic() - t0, 1)
    except Exception as exc:  # noqa: BLE001 — exit 0 always; the line says what failed
        ev = {"error": f"{type(exc).__name__}: {exc}"[:300]}
    print(json.dumps(ev, ensure_ascii=False, indent=1, default=str) if as_json else render(ev))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
