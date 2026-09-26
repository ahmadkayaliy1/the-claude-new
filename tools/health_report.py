"""Compact health + performance report (read-only): ``python tools/health_report.py [--hours 6]``.

Services and heartbeats, the MT5 terminal, events and log errors of the window, AI decisions (status, latency,
tokens), executions (placed / gate rejections with reasons), broker outcomes and virtual outcomes, account state.
Built for a periodic check by a person or an agent: short, and every problem line starts with "!!".
"""
from __future__ import annotations

import argparse
import collections
import json
import sqlite3
import time
from pathlib import Path

import psutil

from tradingsystem.core.settings import load_settings

STALE_S = {"binance_backfill": 900, "mt5_backfill": 900}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=6)
    a = ap.parse_args()
    s = load_settings()
    data, logs = s.paths.data(), s.paths.logs()
    now = time.time() * 1000
    since = now - a.hours * 3_600_000
    con = sqlite3.connect(f"file:{(data / 'app.db').as_posix()}?mode=ro", uri=True, timeout=10)
    out: list[str] = []
    p = out.append
    p(f"# report {time.strftime('%Y-%m-%d %H:%M', time.gmtime())} UTC — last {a.hours:g} h — "
      f"mode {s.execution.mode}/{s.execution.trigger}, AI {s.ai.active_provider}")

    # ---- processes and heartbeats
    terms = [x for x in psutil.process_iter(["name"]) if (x.info["name"] or "").lower() == "terminal64.exe"]
    p(("   " if terms else "!! ") + f"MT5 terminal running: {bool(terms)}")
    for c, st, upd, err, det in con.execute("SELECT collector, state, updated_ms, last_error, detail FROM collector_status "
                                            "ORDER BY collector"):
        age = (now - upd) / 1000 if upd else None
        bad = st in ("error",) or (age is not None and age > STALE_S.get(c, 120) and st not in ("stopped",))
        extra = ""
        d = json.loads(det) if det else {}
        if c == "executor":
            extra = f" equity {d.get('equity')} open {d.get('open_positions')} orders {d.get('open_orders')} " \
                    f"kill_switch {d.get('kill_switch')}"
        if c == "engine":
            extra = f" ai_ready {d.get('ai_ready')} provider {d.get('provider')} quota_left {d.get('quota_left_today')}" \
                    + (f" problem: {d.get('ai_problem')}" if d.get("ai_problem") else "")
        p(f"{'!! ' if bad else '   '}{c:17s} {st:12s} beat {age:6.0f}s{extra}" + (f" | last_error: {err[:120]}" if err and bad else ""))

    # ---- research recorder (P1.12, runs outside the supervisor): its status.json is rewritten every flush (5 min)
    st_file = data / "research" / "price_matching" / "status.json"
    if st_file.exists():
        try:
            st = json.loads(st_file.read_text(encoding="utf-8"))
            upd = time.mktime(time.strptime(st["updated"][:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone
            age_min = (now / 1000 - upd) / 60
            p(f"{'!! ' if age_min > 12 else '   '}price recorder: last flush {age_min:.0f} min ago"
              + (" — stalled or stopped (restart: scripts\\start_recorder.bat)" if age_min > 12 else ""))
        except (OSError, ValueError, KeyError):
            p("!! price recorder: status.json unreadable")

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
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
