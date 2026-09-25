"""P1.9 — MT5 multi-client probe.

Spawns 3 worker processes that attach to the same terminal (``initialize(path=...)`` without credentials)
and read ticks for ~25 s. Worker 0 calls ``shutdown()`` after 8 s. Checks: every worker attaches; the others
keep working after worker 0's shutdown; the logged-in account never changes.

Optional manual part (H3): run with ``--watch 180`` and close/reopen the terminal during the window; the report
shows how long reads failed and whether the workers recovered by re-initialising.
"""
from __future__ import annotations

import multiprocessing as mp
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _report import Report  # noqa: E402

PATH = r"C:/Program Files/MetaTrader 5/terminal64.exe"


def worker(idx: int, seconds: float, shutdown_after: float | None, q: mp.Queue) -> None:
    import MetaTrader5 as mt5

    events = []
    ok = mt5.initialize(path=PATH)
    acc = mt5.account_info()
    events.append(("init", ok, acc.login if acc else None, acc.server if acc else None))
    reads = fails = recov = 0
    connected = ok
    t0 = time.time()
    did_shutdown = False
    while time.time() - t0 < seconds:
        if shutdown_after is not None and not did_shutdown and time.time() - t0 > shutdown_after:
            mt5.shutdown()
            did_shutdown = True
            events.append(("shutdown", time.time() - t0))
            break
        tick = mt5.symbol_info_tick("BTCUSD@") if connected else None
        if tick is None:
            fails += 1
            connected = False
            if mt5.initialize(path=PATH):
                connected = True
                recov += 1
                events.append(("reinit", round(time.time() - t0, 1)))
        else:
            reads += 1
        time.sleep(0.05)
    acc2 = mt5.account_info() if not did_shutdown else None
    events.append(("end", acc2.login if acc2 else None))
    q.put({"idx": idx, "reads": reads, "fails": fails, "recoveries": recov, "events": events})
    if not did_shutdown:
        mt5.shutdown()


def main() -> None:
    watch = float(sys.argv[sys.argv.index("--watch") + 1]) if "--watch" in sys.argv else None
    seconds = watch or 25
    q: mp.Queue = mp.Queue()
    procs = [mp.Process(target=worker, args=(i, seconds, 8 if (i == 0 and not watch) else None, q)) for i in range(3)]
    for p in procs:
        p.start()
    results = [q.get(timeout=seconds + 60) for _ in procs]
    for p in procs:
        p.join()
    results.sort(key=lambda r: r["idx"])
    rep = Report("probe_mt5_multiclient", "MT5 — several processes on one terminal (P1.9)")
    rep.p(f"Mode: {'manual terminal restart window ' + str(watch) + ' s' if watch else 'automatic (worker 0 shuts down at 8 s)'}.")
    rep.table(["worker", "successful reads", "failed reads", "re-inits", "events"],
              [[r["idx"], r["reads"], r["fails"], r["recoveries"], "; ".join(map(str, r["events"]))] for r in results])
    logins = {e[2] for r in results for e in r["events"] if e[0] == "init"}
    rep.p(f"Distinct logins seen at attach: {logins} — attaching without credentials must never switch the account.")
    rep.raw = {"results": results}
    rep.save()


if __name__ == "__main__":
    main()
