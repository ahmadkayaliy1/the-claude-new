"""P1.8 — MT5 polling probe.

For each interval (20/50/100/250 ms) poll ``copy_ticks_from`` for all symbols for N seconds, measuring call
latency, CPU of this process and of terminal64.exe, tick rate and same-millisecond ordering. Then re-fetch the
whole window with ``copy_ticks_range`` and compare sets and order (completeness is independent of the poll
interval if the terminal stores every tick; the interval only changes latency).

Usage: python -m tradingsystem probe mt5_polling [seconds_per_interval]   (default 120)
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import MetaTrader5 as mt5
import psutil

sys.path.insert(0, str(Path(__file__).parent))
from _report import Report, summary  # noqa: E402

PATH = r"C:/Program Files/MetaTrader 5/terminal64.exe"
SYMBOLS = ["XAUUSD@", "BTCUSD@", "ETHUSD@"]
INTERVALS_MS = [20, 50, 100, 250]


def terminal_proc() -> psutil.Process | None:
    for p in psutil.process_iter(["name", "exe"]):
        if (p.info["name"] or "").lower() == "terminal64.exe":
            return p
    return None


def key(t) -> tuple:
    return (int(t["time_msc"]), float(t["bid"]), float(t["ask"]), int(t["flags"]))


def run_interval(interval_ms: int, seconds: float) -> dict:
    me, term = psutil.Process(), terminal_proc()
    me.cpu_percent(None)
    if term:
        term.cpu_percent(None)
    state = {s: (int(mt5.symbol_info_tick(s).time_msc), 0) for s in SYMBOLS}
    start_msc = {s: state[s][0] for s in SYMBOLS}
    got: dict[str, list[tuple]] = {s: [] for s in SYMBOLS}
    call_ms: list[float] = []
    same_ms_groups = 0
    end = time.time() + seconds
    while time.time() < end:
        for s in SYMBOLS:
            last, seen = state[s]
            t0 = time.perf_counter()
            ticks = mt5.copy_ticks_from(s, last // 1000, 100_000, mt5.COPY_TICKS_ALL)
            call_ms.append((time.perf_counter() - t0) * 1000)
            if ticks is None:
                continue
            skip = seen
            new_last, new_seen = last, seen
            for t in ticks:
                tm = int(t["time_msc"])
                if tm < last or (tm == last and skip > 0):
                    if tm == last:
                        skip -= 1
                    continue
                got[s].append(key(t))
                if tm == new_last:
                    new_seen += 1
                    same_ms_groups += 1
                else:
                    new_last, new_seen = tm, 1
            state[s] = (new_last, new_seen)
        time.sleep(interval_ms / 1000)
    cpu_me = me.cpu_percent(None)
    cpu_term = term.cpu_percent(None) if term else float("nan")
    time.sleep(3)  # let the terminal settle, then re-fetch the same window
    mismatch = {}
    for s in SYMBOLS:
        if not got[s]:
            mismatch[s] = "no ticks"
            continue
        lo, hi = start_msc[s], got[s][-1][0]
        ref = mt5.copy_ticks_range(s, lo // 1000, hi // 1000 + 1, mt5.COPY_TICKS_ALL)
        ref_keys = [key(t) for t in ref if lo <= int(t["time_msc"]) <= hi]
        # our first poll included ticks at exactly `lo` → compare on the common range
        ours = [k for k in got[s] if lo <= k[0] <= hi]
        mismatch[s] = "identical (set+order)" if ours == ref_keys else (
            f"DIFF ours={len(ours)} ref={len(ref_keys)} missing={len(set(ref_keys) - set(ours))} "
            f"extra={len(set(ours) - set(ref_keys))}")
    return {"interval_ms": interval_ms, "call": summary(call_ms), "cpu_me": cpu_me, "cpu_term": cpu_term,
            "ticks": {s: len(v) for s, v in got.items()}, "same_ms": same_ms_groups, "check": mismatch,
            "seconds": seconds}


def main() -> None:
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 120
    if not mt5.initialize(path=PATH):
        raise SystemExit(f"MT5 initialize failed: {mt5.last_error()}")
    for s in SYMBOLS:
        mt5.symbol_select(s, True)
    results = [run_interval(i, seconds) for i in INTERVALS_MS]
    mt5.shutdown()
    rep = Report("probe_mt5_polling", "MetaTrader 5 — polling interval measurement (P1.8)")
    rep.p(f"{seconds:g} s per interval, symbols {SYMBOLS}. CPU % is per logical core (4 cores on this PC).")
    rep.table(["interval ms", "calls", "call p50 ms", "call p99 ms", "python CPU %", "terminal CPU %",
               "ticks/s (all)", "same-ms ticks"],
              [[r["interval_ms"], r["call"]["n"], round(r["call"]["p50"], 3), round(r["call"]["p99"], 3),
                round(r["cpu_me"], 1), round(r["cpu_term"], 1),
                round(sum(r["ticks"].values()) / r["seconds"], 2), r["same_ms"]] for r in results])
    rep.h("Completeness check vs copy_ticks_range re-fetch")
    rep.table(["interval ms", *SYMBOLS], [[r["interval_ms"], *[r["check"][s] for s in SYMBOLS]] for r in results])
    rep.raw = {"results": results}
    rep.save()


if __name__ == "__main__":
    main()
