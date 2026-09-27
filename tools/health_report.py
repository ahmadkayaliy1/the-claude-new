"""Compact health + performance report (read-only): ``python tools/health_report.py [--hours 6] [--instance PAIR]``.

Services and heartbeats, the MT5 terminal, events and log errors of the window, AI decisions (status, latency,
tokens), executions (placed / gate rejections with reasons), broker outcomes and virtual outcomes, account state.
Built for a periodic check by a person or an agent: short, and every problem line starts with "!!".

Which system (D-042): ``--instance PAIR`` one pair's system; ``--all-pairs-system`` the single all-pairs system;
default: the all-pairs system when it runs; otherwise every running pair plus every pair that should run (its own
``data/instances/<PAIR>/app.db``, not stopped by the user); when nothing applies, the all-pairs system.
"""
from __future__ import annotations

import argparse
import calendar
import collections
import json
import sqlite3
import sys
import time
from pathlib import Path

import psutil

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tradingsystem.core.settings import INSTANCE_ENV, Settings, load_settings  # noqa: E402
from tradingsystem.supervisor import procs  # noqa: E402

STALE_S = {"binance_backfill": 900, "mt5_backfill": 900}


def _snapshot_build(d: dict) -> tuple[str, float | None, str]:
    """The engine's payload build time (follow-up b): (engine-line text, reference ms, which statistic). The median
    of the recent builds when the engine publishes it — one cold build after a restart must not look like a
    lasting problem — else the last build."""
    sb = d.get("snapshot_build_ms") if isinstance(d.get("snapshot_build_ms"), dict) else {}
    use = "median" if isinstance(sb.get("median"), (int, float)) else "last"
    ref = sb.get(use) if isinstance(sb.get(use), (int, float)) else None
    if ref is None:
        return "", None, use
    n = f" of {sb['n']}" if use == "median" and sb.get("n") else ""
    return f" snapshot_build {use}{n} {ref:.0f} ms (last {sb.get('last')}, max {sb.get('max')})", float(ref), use


def _kill_switch_lines(s: Settings) -> list[str]:
    """Every kill switch this system obeys that is ON, with who set it and why (core/killswitch.py)."""
    try:
        from tradingsystem.core.killswitch import kill_switch_path, read_reason
    except ImportError:                                  # before Phase 4
        return []
    paths = {"every system": kill_switch_path(s, None)}
    for pair in s.enabled_pairs():
        paths[pair] = kill_switch_path(s, pair)
    paths["this system"] = s.paths.state() / "KILL_SWITCH"     # = one of the above for today's layouts
    out, done = [], set()
    for scope, path in paths.items():
        if path in done or not path.exists():
            continue
        done.add(path)
        why = read_reason(path)
        told = " ".join(str(why[k]) for k in ("ts", "actor", "reason") if why.get(k)) or "no reason recorded"
        out.append(f"!! kill switch ON ({scope}): {path} — {told}")
    return out


def usage_gauge_line(s: Settings) -> str | None:
    """The usage gauge over the shared AI ledger (Phase 4, ai/usage_gauge.py); None before it exists or when there
    is no ledger yet (a report never creates one)."""
    ledger = s.paths.shared() / "ai_usage.db" if s.paths.instance else s.paths.state() / "app.db"
    if not ledger.exists():
        return None
    try:
        from tradingsystem.ai.budget import UsageStore
        from tradingsystem.ai.usage_gauge import UsageGauge
    except ImportError:
        return None
    store = None
    try:
        store = UsageStore(ledger)
        g = UsageGauge(s, store).state()
    except Exception as exc:  # noqa: BLE001 — the report goes on without the gauge
        return f"!! usage gauge unreadable: {type(exc).__name__}: {exc}"
    finally:
        if store is not None:
            store.close()
    bad = g.level >= 2 or (g.level >= 1 and g.enforce)
    return (f"{'!! ' if bad else '   '}usage gauge: level {g.level} — 7 d {g.week_tokens / 1e6:.2f} M tokens "
            f"({g.week_pct:.0f} % of the weekly budget), 5 h {g.five_h_tokens / 1e6:.2f} M ({g.five_h_pct:.0f} %)"
            + (" — enforced" if g.enforce else " — observe only (ai.usage.enforce off)")
            + (f" — {g.reason}" if g.reason else ""))


def systems(a: argparse.Namespace) -> tuple[list[Settings], list[str]]:
    """(systems to report, problem lines). Default: the all-pairs system when it runs; otherwise every running pair
    plus every pair that should run (own app.db, not stopped by the user) — a pair whose supervisor died is
    reported (stale heartbeats), not silently left out."""
    if a.instance:
        return [load_settings(extra_env={INSTANCE_ENV: a.instance.upper()})], []
    if a.all_pairs_system:
        return [load_settings(extra_env={INSTANCE_ENV: ""})], []
    base = load_settings()
    if base.paths.instance:
        return [base], []
    running = set(procs.running_supervisors().values())
    if None in running:                             # the all-pairs system runs (pairs cannot run next to it)
        return [base], []
    notes = [f"!! supervisor running for {p}, which is not in config 'instances:' — not reported"
             for p in sorted(x for x in running if x not in base.instances)]
    data = base.paths.data()
    expected = {p for p in base.instances if (data / "instances" / p / "app.db").exists()
                and not (data / "instances" / p / "run" / "manual_stop").exists()}
    pairs = sorted((running & set(base.instances)) | expected)
    for p in sorted(expected - running):
        notes.append(f"!! {p}: its system should run (own app.db, not stopped by the user) but no supervisor runs "
                     f"— scripts\\start.bat {p}")
    if pairs:
        return [load_settings(extra_env={INSTANCE_ENV: p}) for p in pairs], notes
    return [base], notes


def machine_report(s: Settings, now: float) -> list[str]:
    """What every system shares: the MT5 terminal, the supervisors, the price recorder, the AI ledger."""
    out: list[str] = []
    p = out.append
    terms = [x for x in psutil.process_iter(["name"]) if (x.info["name"] or "").lower() == "terminal64.exe"]
    p(("   " if terms else "!! ") + f"MT5 terminal running: {bool(terms)}")
    sups = procs.running_supervisors()
    p(("   " if sups else "!! ") + "supervisors: " + (procs.describe(sups) if sups else "none running"))
    # research recorder (P1.12, runs outside the supervisor): its status.json is rewritten every flush (5 min)
    st_file = s.paths.data() / "research" / "price_matching" / "status.json"
    if st_file.exists():
        try:
            st = json.loads(st_file.read_text(encoding="utf-8"))
            upd = calendar.timegm(time.strptime(st["updated"][:19], "%Y-%m-%dT%H:%M:%S"))   # UTC, DST-proof
            age_min = (now / 1000 - upd) / 60
            p(f"{'!! ' if age_min > 12 else '   '}price recorder: last flush {age_min:.0f} min ago"
              + (" — stalled or stopped (restart: scripts\\start_recorder.bat)" if age_min > 12 else ""))
        except (OSError, ValueError, KeyError):
            p("!! price recorder: status.json unreadable")
    ledger = s.paths.shared() / "ai_usage.db"
    if ledger.exists():
        try:
            con = sqlite3.connect(f"file:{ledger.as_posix()}?mode=ro", uri=True, timeout=10)
            day0 = now // 86_400_000 * 86_400_000
            rows = con.execute("SELECT provider, pair, count(*), COALESCE(sum(ok),0) FROM ai_usage WHERE ts>=? "
                               "GROUP BY provider, pair ORDER BY provider, pair", (day0,)).fetchall()
            con.close()
            p("   AI calls today (every system, shared ledger): "
              + (", ".join(f"{pv}/{pr or '-'} {n} ({ok} ok)" for pv, pr, n, ok in rows) or "none"))
        except sqlite3.Error as exc:
            p(f"!! AI usage ledger unreadable: {exc}")
    gauge = usage_gauge_line(s)
    if gauge:
        p(gauge)
    return out


def system_report(s: Settings, hours: float, now: float) -> list[str]:
    state, logs = s.paths.state(), s.paths.logs()
    since = now - hours * 3_600_000
    out: list[str] = []
    p = out.append
    p(f"## {'system ' + s.paths.instance if s.paths.instance else 'all-pairs system'} — mode "
      f"{s.execution.mode}/{s.execution.trigger} — {state}")
    db = state / "app.db"
    if not db.exists():
        p(f"!! {db} does not exist (never started)")
        return out
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=10)

    # ---- heartbeats
    for c, st, upd, err, det in con.execute("SELECT collector, state, updated_ms, last_error, detail FROM collector_status "
                                            "ORDER BY collector"):
        age = (now - upd) / 1000 if upd else None
        bad = st in ("error",) or (age is not None and age > STALE_S.get(c, 120) and st not in ("stopped",))
        extra = ""
        d = json.loads(det) if det else {}
        if c == "executor":
            dd = d.get("account_drawdown") or {}
            extra = (f" equity {d.get('equity')} today {d.get('today_pnl_pct')}% open {d.get('open_positions')} "
                     f"orders {d.get('open_orders')} kill_switch {d.get('kill_switch')}"
                     + (f" drawdown {dd.get('drawdown_pct')}%" if dd else "")
                     + (" DRAWDOWN STOP TRIPPED" if dd.get("tripped") else ""))
            bad = bad or bool(dd.get("tripped"))
        sb_text, sb_ms, sb_use = _snapshot_build(d) if c == "engine" else ("", None, "")
        if c == "engine":
            extra = f" ai_ready {d.get('ai_ready')} provider {d.get('provider')} quota_left {d.get('quota_left_today')}" \
                    + (f" problem: {d.get('ai_problem')}" if d.get("ai_problem") else "") + sb_text
        p(f"{'!! ' if bad else '   '}{c:17s} {st:12s} beat {age:6.0f}s{extra}" + (f" | last_error: {err[:120]}" if err and bad else ""))
        for x in (d.get("exposure") or []) if c == "executor" else []:
            p(f"     {x.get('pair')} {x.get('kind')} {x.get('side')} {x.get('volume')} @ {x.get('price')} sl {x.get('sl')} "
              f"tps {x.get('tps')} profit {x.get('profit_usd')} ({x.get('decision')})")
        if c == "engine":                                  # Phase 4: follow-up (b) + the adaptive overlay in use
            if sb_ms is not None and sb_ms > s.monitor.snapshot_build_warn_ms:
                p(f"!! snapshot build {sb_ms:.0f} ms ({sb_use}) above monitor.snapshot_build_warn_ms "
                  f"{s.monitor.snapshot_build_warn_ms} — the 5-min screening lags (RAM/CPU)")
            for pair, o in sorted((d["adaptive"] if isinstance(d.get("adaptive"), dict) else {}).items()):
                if isinstance(o, dict):
                    p(f"   adaptive overlay {pair}: adaptive {o.get('adaptive_hash') or '-'} playbook "
                      f"{o.get('playbook_hash') or '-'}"
                      + (f" — AI paused until {o['paused_until']}" if o.get("paused_until") else ""))
    out += _kill_switch_lines(s)

    # ---- events
    ev = collections.Counter(r[0] for r in con.execute("SELECT event FROM ingestion_events WHERE ts>=?", (since,)))
    p("events: " + (", ".join(f"{k} {v}" for k, v in ev.most_common(14)) or "none"))
    for ts, col, e, det in con.execute("SELECT ts, collector, event, detail FROM ingestion_events WHERE ts>=? AND event IN "
                                       "('exited','killed','cycle_error','order_failed','worker_exit','ai_not_ready',"
                                       "'data_not_ready','system_suspend','terminal_started') ORDER BY ts DESC LIMIT 8",
                                       (since,)):
        p(f"!! {time.strftime('%H:%M', time.gmtime(ts / 1000))} {col} {e}: {(det or '')[:140]}")

    # ---- AI decisions
    rows = con.execute("SELECT pair, status, decision, latency_ms, input_tokens, output_tokens, errors FROM ai_decisions "
                       "WHERE ts>=?", (since,)).fetchall()
    st = collections.Counter(f"{r[1]}/{r[2] or '-'}" for r in rows)
    lat = [r[3] for r in rows if r[3]]
    p(f"AI decisions: {len(rows)} ({', '.join(f'{k} {v}' for k, v in st.most_common())})"
      + (f" | latency avg {sum(lat) / len(lat) / 1000:.0f}s max {max(lat) / 1000:.0f}s" if lat else "")
      + (f" | tokens in {sum(r[4] or 0 for r in rows)} out {sum(r[5] or 0 for r in rows)}" if rows else ""))
    for r in rows:
        if r[1] in ("error", "invalid", "refused"):
            p(f"!! {r[0]} {r[1]}: {(r[6] or '')[:160]}")

    # ---- execution
    ex = con.execute("SELECT pair, decision, execution_state, execution_detail FROM ai_decisions WHERE ts>=? AND "
                     "decision IN ('BUY','SELL')", (since,)).fetchall()
    p(f"trade ideas: {len(ex)} ({', '.join(f'{k} {v}' for k, v in collections.Counter(r[2] for r in ex).most_common())})")
    reasons = collections.Counter()
    for r in ex:
        if r[2] == "rejected" and r[3]:
            reasons.update(x.split(" ")[0] for x in (json.loads(r[3]).get("reason") or "").split("; ") if x)
    if reasons:
        p("   gate rejections: " + ", ".join(f"{k} {v}" for k, v in reasons.most_common(8)))

    # ---- outcomes (all time) and virtual outcomes
    oc = con.execute("SELECT outcome, count(*), round(sum(outcome_pnl_usd),2) FROM ai_decisions WHERE outcome IS NOT NULL "
                     "GROUP BY outcome").fetchall()
    p("broker/paper outcomes (all time): " + (", ".join(f"{o} {n} ({usd:+} USD)" for o, n, usd in oc) or "none yet"))
    vo = con.execute("SELECT count(*), sum(virtual_outcome='tp1_first'), round(avg(virtual_r),2), "
                     "(SELECT count(*) FROM ai_decisions WHERE virtual_outcome IN ('not_triggered','unresolved_24h')) "
                     "FROM ai_decisions WHERE virtual_outcome IN ('tp1_first','sl_first')").fetchone()
    if vo and (vo[0] or vo[3]):
        p(f"virtual outcomes (every trade idea, on real prices): {vo[0]} resolved — TP1 first {vo[1] or 0}, "
          f"SL first {vo[0] - (vo[1] or 0)}, avg R {vo[2]}; not triggered/unresolved {vo[3]}")
    con.close()

    # ---- log errors
    errs = collections.Counter()
    for f in sorted(logs.glob("*.jsonl")):
        try:
            with f.open("rb") as fh:
                fh.seek(max(0, f.stat().st_size - 2_000_000))
                for line in fh.read().decode("utf-8", "replace").splitlines():
                    if '"level": "ERROR"' in line or '"level": "CRITICAL"' in line:
                        try:
                            j = json.loads(line)
                        except ValueError:
                            continue
                        if j.get("ts", "") >= time.strftime("%Y-%m-%dT%H:%M", time.gmtime(since / 1000)):
                            errs[f"{f.stem}: {j.get('msg', '')[:110]}"] += 1
        except OSError:
            continue
    for k, v in errs.most_common(8):
        p(f"!! log error ×{v} {k}")
    if not errs:
        p("   no ERROR lines in the logs of the window")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=6)
    ap.add_argument("--instance", help="only this pair's system")
    ap.add_argument("--all-pairs-system", action="store_true", help="the single all-pairs system")
    a = ap.parse_args()
    now = time.time() * 1000
    chosen, notes = systems(a)
    s0 = chosen[0]
    out = [f"# report {time.strftime('%Y-%m-%d %H:%M', time.gmtime())} UTC — last {a.hours:g} h — AI {s0.ai.active_provider}"]
    out += machine_report(s0, now) + notes
    for s in chosen:
        out += system_report(s, a.hours, now)
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
