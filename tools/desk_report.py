"""Gold desk scorecard (Phase 5 B19, D-049): the evidence for any future real gold trading (H32).

    python tools/desk_report.py --print                                 → the markdown on stdout, nothing written
    python tools/desk_report.py --root C:\\the_claude_new --print        → production's data (a worktree reading it)
    python tools/desk_report.py --out docs/measurements/gold_desk.md    → the measurement document (a worktree only:
                                                                          refused where a system's app.db lives)
    python tools/desk_report.py … [--pair XAUUSD] [--since ISO] [--until ISO] [--days 90] [--no-baseline] [--json FILE]

What it measures (read-only everywhere: SQLite through ``file:…?mode=ro``, the 1m bars through the storage reader):
* every SHADOW idea of the desk pair (``execution_detail.shadow``): its stop, ``desk_ok`` (every gate check but
  ``position_size`` / ``effective_leverage`` passed), the virtual outcome in R (the executor's virtual walk on real
  bars) and R net of the spread at the gate (the bars are bid prices: one spread per round trip), MFE/MAE and minutes
  to resolve from ``decision_metrics``, by desk window (London / New York) and by session;
* M1 = every stored BUY/SELL of the pair re-gated with the desk rules from its stored gate record (ideas from before
  the desk existed included), with the checks that failed;
* M4 = the pair's calls per UTC day (the stored cycles; ``tools/replay_triggers.py XAUUSD`` replays the windows);
* the RANDOM-ENTRY BASELINE: from every 5-min close inside the desk windows over the last ``--days`` (90) days of the
  pair's 1m bars, a BUY and a SELL at each stop size (the shadow ideas' own, else the D-049 study's 2 / 2.9 / 5 / 9),
  entered at the next bar's open with the bar's real spread (a BUY pays the ask; a SELL's stop and target trigger on
  the ask = bid + spread), target 2R, the stop first when one bar reaches both, flat at the gold day's end (17:00 New
  York) — the R expectancy, the share reaching 2R first, the minutes to resolve and the share of losses inside 15 min;
* the H32 verdict: ≥ 30 ``desk_ok`` ideas resolved, expectancy > 0 after the spread and ≥ the baseline + 0.15 R
  (the other two H32 conditions — equity or a smaller contract, the dedicated machine — are the owner's).

One DB and one bar series at a time; < 60 s and < 200 MB on 90 days (a numpy walk per entry). Exit 0 written/printed,
1 unexpected error, 3 invalid arguments.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import statistics
import sys
import time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tradingsystem.analysis.desk_windows import desk_window_state  # noqa: E402
from tradingsystem.core.instruments import InstrumentRegistry  # noqa: E402
from tradingsystem.core.sessions import calendar_for  # noqa: E402
from tradingsystem.core.settings import INSTANCE_ENV, Settings, load_settings  # noqa: E402
from tradingsystem.core.timeframes import Timeframe  # noqa: E402
from tradingsystem.core.timeutil import MS_PER_DAY, MS_PER_MINUTE, iso, now_ms, parse_date_spec  # noqa: E402
from tradingsystem.storage.reader import InstrumentReader  # noqa: E402
from tradingsystem.storage.tablespec import spec_for  # noqa: E402

WAIVED = ("position_size", "effective_leverage")        # executor.DESK_WAIVED_CHECKS (D-049)
DEFAULT_STOPS = (2.0, 2.9, 5.0, 9.0)                     # the D-049 study's sizes (gold_desk_study.md §4)
TARGET_R = 2.0
EDGE_MARGIN_R = 0.15                                     # H32: >= the baseline + 0.15 R
MIN_IDEAS = 30                                           # H32: >= 30 desk_ok ideas
FAST_LOSS_MIN = 15
NY = ZoneInfo("America/New_York")
DEFAULT_OUT = "docs/measurements/gold_desk.md"


class Invalid(Exception):
    pass


def ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=30)


def _loads(text: Any, default: Any = None) -> Any:
    try:
        return json.loads(text) if text else default
    except (TypeError, ValueError):
        return default


def pair_settings(pair: str, root: Path | None) -> Settings:
    s = load_settings(extra_env={INSTANCE_ENV: pair})
    if root is not None:
        s = s.model_copy(update={"paths": s.paths.model_copy(update={"data_dir": str(Path(root) / "data"),
                                                                     "logs_dir": str(Path(root) / "logs")})})
    return s


def app_db(s: Settings) -> Path:
    return s.paths.state() / "app.db"


# --------------------------------------------------------------------------------------------- the stored ideas
def _levels(rec: dict) -> tuple[float | None, float | None, float | None]:
    """(entry, stop, TP1) of a recommendation — the entry as the virtual walk reads it."""
    try:
        e = rec.get("entry") or {}
        buy = rec.get("decision") == "BUY"
        entry = e.get("price") or ((e["range_max"] if buy else e["range_min"]) if e.get("range_min") is not None
                                   else None)
        return (float(entry) if entry is not None else None, float(rec["stop_loss"]),
                float(rec["take_profits"][0]["price"]))
    except (KeyError, TypeError, ValueError, IndexError):
        return None, None, None


def desk_ok_of(gate: list | None) -> tuple[bool | None, list[str]]:
    """(desk_ok, failed checks) from a stored gate list: every check but the waived ones passed AND the gate reached
    sizing (a record without ``position_size`` stopped early - later checks never ran, which is not a pass)."""
    if not isinstance(gate, list) or not gate:
        return None, []
    names = {str(g.get("check")) for g in gate if isinstance(g, dict)}
    failed = [str(g.get("check")) for g in gate if isinstance(g, dict) and not g.get("ok")
              and str(g.get("check")) not in WAIVED]
    return ("position_size" in names and not failed), failed


def ideas(con: sqlite3.Connection, pair: str, since: int, until: int, s: Settings) -> list[dict[str, Any]]:
    """Every BUY/SELL of the pair in the window with its gate record, virtual outcome and metrics."""
    cols = {r[1] for r in con.execute("PRAGMA table_info(ai_decisions)")}
    session = "session" if "session" in cols else "NULL"
    rows = con.execute(
        f"SELECT id, ts, decision, recommendation, execution_state, execution_detail, virtual_outcome, virtual_r, "
        f"{session} FROM ai_decisions WHERE pair=? AND ts>=? AND ts<? AND status='valid' "
        f"AND decision IN ('BUY','SELL') ORDER BY ts", (pair, since, until)).fetchall()
    has_metrics = bool(con.execute("SELECT name FROM sqlite_master WHERE name='decision_metrics'").fetchone())
    desk = s.pairs[pair].desk
    execu = InstrumentRegistry.from_settings(s).with_role(pair, "execution")[0]
    cal = calendar_for(execu.venue, execu.symbol, s.pairs[pair].asset_class)
    out = []
    for did, ts, dec, rec_t, state, det_t, vo, vr, sess in rows:
        rec, det = _loads(rec_t, {}) or {}, _loads(det_t, {}) or {}
        entry, sl, tp1 = _levels(rec)
        stop = abs(entry - sl) if entry is not None and sl is not None else None
        ok, failed = desk_ok_of(det.get("gate"))
        spread = None
        for g in det.get("gate") or []:
            if isinstance(g, dict) and g.get("check") == "spread_vs_sl":
                try:
                    spread = float(str(g.get("detail")).split("spread ")[1].split(" ")[0])
                except (IndexError, ValueError):
                    spread = None
        spread = det.get("spread_at_gate", spread)
        m = {}
        if has_metrics:
            r = con.execute("SELECT mfe_r, mae_r, minutes_to_resolve, exit_reason FROM decision_metrics "
                            "WHERE decision_id=?", (did,)).fetchone()
            if r:
                m = {"mfe_r": r[0], "mae_r": r[1], "minutes_to_resolve": r[2], "exit_reason": r[3]}
        r_net = None
        if vr is not None and vo in ("tp1_first", "sl_first") and stop:
            r_net = float(vr) - (float(spread) / stop if spread else 0.0)
        window = desk_window_state(desk, int(ts), cal, pair)[1] if desk is not None else None
        out.append({"id": did[:8], "ts": iso(int(ts)), "decision": dec, "stop": round(stop, 2) if stop else None,
                    "shadow": bool(det.get("shadow")), "state": state, "desk_ok": ok, "desk_failed": failed,
                    "stored_desk_ok": det.get("desk_ok"), "virtual_outcome": vo,
                    "r": round(float(vr), 3) if vr is not None else None,
                    "r_net": round(r_net, 3) if r_net is not None else None, "spread": spread,
                    "window": window, "session": sess, **m})
    return out


def calls_per_day(con: sqlite3.Connection, pair: str, since: int, until: int) -> dict[str, int]:
    """M4 as stored: the pair's answered cycles per UTC day (every stored decision row but skipped)."""
    out: dict[str, int] = {}
    for (ts,) in con.execute("SELECT ts FROM ai_decisions WHERE pair=? AND ts>=? AND ts<? AND status!='skipped'",
                             (pair, since, until)):
        day = iso(int(ts))[:10]
        out[day] = out.get(day, 0) + 1
    return dict(sorted(out.items()))


def summary(rows: list[dict[str, Any]], key: str = "r_net") -> dict[str, Any]:
    vals = [r[key] for r in rows if r.get(key) is not None]
    wins = [r for r in rows if r.get("virtual_outcome") == "tp1_first"]
    mins = [r["minutes_to_resolve"] for r in rows if r.get("minutes_to_resolve") is not None]
    return {"n": len(rows), "resolved": len(vals), "expectancy_r": round(statistics.fmean(vals), 3) if vals else None,
            "tp_first_share": round(len(wins) / len(vals), 3) if vals else None,
            "median_minutes": statistics.median(mins) if mins else None,
            "mfe_r_mean": _mean(rows, "mfe_r"), "mae_r_mean": _mean(rows, "mae_r")}


def _mean(rows, key):
    v = [r[key] for r in rows if r.get(key) is not None]
    return round(statistics.fmean(v), 3) if v else None


# --------------------------------------------------------------------------------------------- the baseline
def load_1m(s: Settings, pair: str, since: int, until: int) -> dict[str, np.ndarray]:
    inst = InstrumentRegistry.from_settings(s).primary(pair)
    rd = InstrumentReader(inst, s.paths.data(), cache_mb=4)
    try:
        cols = ["open_time", "open", "high", "low", "close", "spread"]
        c = rd.read_range(spec_for(inst, "candles", Timeframe.M1), since, until, cols)
    finally:
        rd.close()
    point = 10.0 ** -s.pairs[pair].price_decimals
    out = {k: np.asarray(c[k], dtype=float) for k in cols}
    out["open_time"] = np.asarray(c["open_time"], dtype=np.int64)
    sp = out["spread"] * point
    med = float(np.median(sp[sp > 0])) if (sp > 0).any() else 0.0
    out["spread_px"] = np.where(sp > 0, sp, med)
    return out


def _day_end(t: int) -> int:
    """The end of the gold day (17:00 New York) at or after ``t``."""
    local = dt.datetime.fromtimestamp(t / 1000, tz=NY)
    end = local.replace(hour=17, minute=0, second=0, microsecond=0)
    if local >= end:
        end += dt.timedelta(days=1)
    return int(end.timestamp() * 1000)


def walk(b: dict[str, np.ndarray], i: int, stop: float, buy: bool) -> tuple[float, int] | None:
    """(R, minutes) of one random entry at bar ``i``'s open: bid bars, a BUY pays the ask and exits at the bid; a SELL
    sells the bid, its stop and target trigger on the ask. The stop first when one bar reaches both; flat at 17:00 New
    York (marked at the bar's close)."""
    t0 = int(b["open_time"][i])
    end = _day_end(t0)
    j = int(np.searchsorted(b["open_time"], end))
    if j <= i:
        return None
    hi, lo, cl = b["high"][i:j], b["low"][i:j], b["close"][i:j]
    spr = float(b["spread_px"][i])
    if buy:
        entry = float(b["open"][i]) + spr
        hit_sl = lo <= entry - stop
        hit_tp = hi >= entry + TARGET_R * stop
    else:
        entry = float(b["open"][i])
        hit_sl = hi + spr >= entry + stop
        hit_tp = lo + spr <= entry - TARGET_R * stop
    k_sl = int(np.argmax(hit_sl)) if hit_sl.any() else None
    k_tp = int(np.argmax(hit_tp)) if hit_tp.any() else None
    if k_sl is not None and (k_tp is None or k_sl <= k_tp):
        return -1.0, k_sl + 1
    if k_tp is not None:
        return TARGET_R, k_tp + 1
    last = float(cl[-1])
    r = ((last - entry) if buy else (entry - (last + spr))) / stop
    return r, len(cl)


def baseline(s: Settings, pair: str, stops: list[float], days: int, until: int) -> dict[str, Any]:
    desk = s.pairs[pair].desk
    execu = InstrumentRegistry.from_settings(s).with_role(pair, "execution")[0]
    cal = calendar_for(execu.venue, execu.symbol, s.pairs[pair].asset_class)
    since = until - days * MS_PER_DAY
    b = load_1m(s, pair, since, until)
    if len(b["open_time"]) < 1000:
        return {"error": f"only {len(b['open_time'])} 1m bars in {days} days"}
    t = b["open_time"]
    starts = [i for i in np.nonzero(t % (5 * MS_PER_MINUTE) == 0)[0]
              if desk is None or desk_window_state(desk, int(t[i]), cal, pair)[0]]
    by: dict[str, dict[str, Any]] = {}
    for stop in stops:
        for side, buy in (("BUY", True), ("SELL", False)):
            res, win = [], {}
            for i in starts:
                w = walk(b, int(i), stop, buy)
                if w is None:
                    continue
                res.append(w)
                name = desk_window_state(desk, int(t[i]), cal, pair)[1] if desk is not None else "all"
                win.setdefault(name, []).append(w[0])
            if not res:
                continue
            r = np.array([x[0] for x in res])
            m = np.array([x[1] for x in res])
            losses = r == -1.0
            by[f"{stop:g} {side}"] = {
                "stop": stop, "side": side, "n": int(len(r)), "expectancy_r": round(float(r.mean()), 3),
                "tp_first_share": round(float((r == TARGET_R).mean()), 3),
                "median_minutes": float(np.median(m)),
                "losses_within_15min": round(float((m[losses] <= FAST_LOSS_MIN).mean()), 3) if losses.any() else None,
                "by_window": {k: {"n": len(v), "expectancy_r": round(float(np.mean(v)), 3)} for k, v in win.items()}}
    pooled = [v["expectancy_r"] for v in by.values()]
    return {"days": days, "since": iso(since), "until": iso(until), "bars": int(len(t)), "entries": len(starts),
            "spread_median": round(float(np.median(b["spread_px"])), 3), "target_r": TARGET_R, "by_stop_side": by,
            "expectancy_r_mean": round(float(np.mean(pooled)), 3) if pooled else None}


def baseline_for(rows: list[dict[str, Any]], b: dict[str, Any]) -> float | None:
    """The baseline expectancy at the ideas' own stop sizes and sides (nearest computed stop), else the pooled mean."""
    by = b.get("by_stop_side") or {}
    if not by:
        return None
    stops = sorted({v["stop"] for v in by.values()})
    vals = []
    for r in rows:
        if r.get("stop") and r.get("r_net") is not None:
            near = min(stops, key=lambda x: abs(x - r["stop"]))
            v = by.get(f"{near:g} {r['decision']}")
            if v:
                vals.append(v["expectancy_r"])
    return round(statistics.fmean(vals), 3) if vals else b.get("expectancy_r_mean")


# --------------------------------------------------------------------------------------------- collect / render
def collect(pair: str = "XAUUSD", root: Path | None = None, since: int | None = None, until: int | None = None,
            days: int = 90, with_baseline: bool = True, now: int | None = None) -> dict[str, Any]:
    now = now_ms() if now is None else now
    s = pair_settings(pair, root)
    if s.pairs[pair].desk is None:
        raise Invalid(f"{pair} has no desk (pairs.{pair}.desk)")
    db = app_db(s)
    if not db.exists():
        raise Invalid(f"no app.db for {pair} under {s.paths.data()}")
    since = 0 if since is None else since
    until = now if until is None else until
    con = ro(db)
    try:
        rows = ideas(con, pair, since, until, s)
        cpd = calls_per_day(con, pair, max(since, until - 14 * MS_PER_DAY), until)
    finally:
        con.close()
    shadow = [r for r in rows if r["shadow"]]
    ok = [r for r in shadow if r["desk_ok"]]
    stops = sorted({round(r["stop"] * 2) / 2 for r in shadow if r.get("stop")}) or list(DEFAULT_STOPS)
    if len(stops) < 3:
        stops = sorted(set(stops) | set(DEFAULT_STOPS))
    base = baseline(s, pair, stops, days, until) if with_baseline else None
    ref = baseline_for(ok, base) if base else None
    so = summary(ok)
    verdict = {"desk_ok_resolved": so["resolved"], "needed": MIN_IDEAS, "expectancy_r": so["expectancy_r"],
               "baseline_r": ref, "margin_r": EDGE_MARGIN_R,
               "passes": bool(so["resolved"] >= MIN_IDEAS and so["expectancy_r"] is not None and so["expectancy_r"] > 0
                              and ref is not None and so["expectancy_r"] >= ref + EDGE_MARGIN_R)}
    by_window: dict[str, list] = {}
    for r in shadow:
        by_window.setdefault(r.get("window") or "?", []).append(r)
    m1 = [r for r in rows if r["desk_ok"] is not None]
    return {"pair": pair, "generated": iso(now), "window": {"since": iso(since) if since else "all", "until": iso(until)},
            "data_root": str(s.paths.data()), "ideas": rows, "shadow": summary(shadow), "desk_ok": so,
            "not_desk_ok": summary([r for r in shadow if r["desk_ok"] is False]),
            "by_window": {k: summary(v) for k, v in sorted(by_window.items())},
            "m1": {"regated": len(m1), "desk_ok": sum(1 for r in m1 if r["desk_ok"]),
                   "failed_checks": _count([c for r in m1 for c in r["desk_failed"]])},
            "m4_calls_per_day": cpd, "baseline": base, "verdict": verdict}


def _count(xs: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for x in xs:
        out[x] = out.get(x, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def _f(x, nd=2) -> str:
    return "–" if x is None else (f"{x:.{nd}f}" if isinstance(x, float) else str(x))


def render(d: dict[str, Any]) -> str:
    v, so = d["verdict"], d["desk_ok"]
    L = [f"# Gold desk scorecard — {d['pair']} (shadow, D-049)", "",
         f"Generated {d['generated']} by `tools/desk_report.py` from `{d['data_root']}` (read-only). Window: "
         f"{d['window']['since']} → {d['window']['until']}.", "",
         "## Verdict for H32 (the evidence part only)", "",
         f"- `desk_ok` ideas resolved: **{v['desk_ok_resolved']}** of the {v['needed']} needed; expectancy after the "
         f"spread **{_f(v['expectancy_r'], 3)} R** against the random-entry baseline {_f(v['baseline_r'], 3)} R + "
         f"{v['margin_r']} R → **{'PASSES' if v['passes'] else 'not met'}**.",
         "- The other H32 conditions (equity ≥ `equity_for_min_lot` at the median shadow stop, or a ≤ 0.1-oz contract; "
         "production on the dedicated machine) are the owner's; nothing here trades.", "",
         "## Shadow ideas", "",
         "| | n | resolved | expectancy R (net) | TP1 first | median min | MFE R | MAE R |", "|---|---|---|---|---|---|---|---|"]
    for name, x in (("all shadow", d["shadow"]), ("desk_ok", so), ("not desk_ok", d["not_desk_ok"]),
                    *[(f"window {k}", x) for k, x in d["by_window"].items()]):
        L.append(f"| {name} | {x['n']} | {x['resolved']} | {_f(x['expectancy_r'], 3)} | {_f(x['tp_first_share'], 3)} | "
                 f"{_f(x['median_minutes'])} | {_f(x['mfe_r_mean'], 3)} | {_f(x['mae_r_mean'], 3)} |")
    L += ["", "Per idea (R net = the virtual R minus the spread at the gate over the stop):", "",
          "| id | time | side | stop | shadow | desk_ok | failed (not waived) | outcome | R | R net | window |",
          "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in d["ideas"][-60:]:
        L.append(f"| {r['id']} | {r['ts'][:16]} | {r['decision']} | {_f(r['stop'])} | {int(r['shadow'])} | "
                 f"{_f(r['desk_ok'])} | {', '.join(r['desk_failed']) or '–'} | {r['virtual_outcome'] or '–'} | "
                 f"{_f(r['r'], 3)} | {_f(r['r_net'], 3)} | {r['window'] or '–'} |")
    m1 = d["m1"]
    L += ["", f"## M1 — every stored {d['pair']} BUY/SELL re-gated with the desk rules", "",
          f"{m1['regated']} ideas with a gate record; desk_ok {m1['desk_ok']}; failed checks (not waived): "
          f"{json.dumps(m1['failed_checks'])}.", "",
          "## M4 — calls per UTC day (stored cycles, last 14 days)", "",
          " · ".join(f"{k}: {n}" for k, n in d["m4_calls_per_day"].items()) or "none", ""]
    b = d.get("baseline")
    if b and not b.get("error"):
        L += ["## Random-entry baseline", "",
              f"{b['entries']} entries (every 5-min close inside the desk windows, {b['since'][:10]} → "
              f"{b['until'][:10]}, {b['bars']} 1m bars), each a BUY and a SELL per stop size, target {b['target_r']}R, "
              f"real per-bar spread (median {b['spread_median']}), stop first on a shared bar, flat at 17:00 New York.",
              "", "| stop | side | n | expectancy R | 2R first | median min | losses ≤ 15 min | by window |",
              "|---|---|---|---|---|---|---|---|"]
        for x in b["by_stop_side"].values():
            win = "; ".join(f"{k} {w['expectancy_r']:+.3f} (n {w['n']})" for k, w in x["by_window"].items())
            L.append(f"| {x['stop']:g} | {x['side']} | {x['n']} | {x['expectancy_r']:+.3f} | {x['tp_first_share']:.3f} "
                     f"| {x['median_minutes']:.0f} | {_f(x['losses_within_15min'], 3)} | {win} |")
        L += ["", "Break-even at a 2R target is a 2R-first share of 0.333. A baseline is not an edge: it is what a "
                  "random entry at the same stop, hours and costs earned."]
    elif b:
        L += ["## Random-entry baseline", "", f"not computed: {b['error']}"]
    L += ["", "Sample size: a handful of ideas proves nothing; the verdict needs 30 resolved desk_ok ideas."]
    return "\n".join(L) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--pair", default="XAUUSD")
    ap.add_argument("--root")
    ap.add_argument("--since")
    ap.add_argument("--until")
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--no-baseline", action="store_true")
    ap.add_argument("--print", action="store_true")
    ap.add_argument("--out")
    ap.add_argument("--json")
    a = ap.parse_args(argv)
    try:
        if not a.print and not a.out and not a.json:
            raise Invalid("say where: --print, --out FILE or --json FILE")
        root = Path(a.root) if a.root else None
        if a.out and root is None and (ROOT / "data" / "instances").exists():
            raise Invalid("this checkout runs a system: --print, or --out under data\\reviews (a changed doc blocks "
                          "the next ff-merge)")
        t0 = time.time()
        d = collect(a.pair.upper(), root, parse_date_spec(a.since) if a.since else None,
                    parse_date_spec(a.until) if a.until else None, a.days, not a.no_baseline)
        d["seconds"] = round(time.time() - t0, 1)
        text = render(d)
        if a.print:
            print(text)
        if a.out:
            Path(a.out).write_text(text, encoding="utf-8")
        if a.json:
            Path(a.json).write_text(json.dumps(d, indent=1, default=str), encoding="utf-8")
        return 0
    except Invalid as e:
        print(f"desk_report: {e}", file=sys.stderr)
        return 3
    except Exception as e:  # noqa: BLE001
        print(f"desk_report: error {e!r}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
