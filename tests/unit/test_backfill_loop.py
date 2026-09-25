"""Backfill worker bookkeeping (BF-01/BF-03/BF-14/F4/OPS-02): pass results, retry scheduling, a worker that never
dies, status hygiene and the parent's restart keeper. Pure logic — no market data."""
import datetime as dt
import json

import pytest

from tradingsystem.ingest.common import backfill_loop as bl
from tradingsystem.ingest.common.appdb import AppDB
from tradingsystem.ingest.common.backfill_loop import (PassResult, StepIncomplete, WorkerKeeper, finish_pass,
                                                       next_daily_run, schedule, worker_loop)

C = "binance_backfill"


class Clock:
    def __init__(self, t: float) -> None:
        self.t, self.sleeps = t, []

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


def row(db: AppDB, collector: str = C) -> dict:
    return next(r for r in db.statuses() if r["collector"] == collector)


def events(db: AppDB) -> list[str]:
    with db._lock:
        return [r[0] for r in db._con.execute("SELECT event FROM ingestion_events ORDER BY id")]


def test_pass_result_states():
    assert PassResult().ok and PassResult().state == "stopped"
    r = PassResult()
    r.add("candles x", TimeoutError("lock"), transient=True)
    assert not r.ok and r.state == "reconnecting" and "candles x" in r.summary()
    r.add("metrics y", ValueError("bad row"), transient=False)
    assert r.state == "error"
    inc = StepIncomplete("agg_trades z", {"2026-01": ValueError("x"), "2026-02": TimeoutError("y")})
    assert inc.failures and "2 unit(s) failed" in str(inc)


def test_schedule_backs_off_on_transient_and_waits_for_the_daily_slot_otherwise():
    now = dt.datetime(2026, 9, 26, 12, 10, tzinfo=dt.timezone.utc).timestamp()
    t = PassResult(transient={"u": "x"})
    waits, fails = [], 0
    for _ in range(7):
        wake, fails = schedule(t, fails, 3, now)
        waits.append(wake - now)
    assert waits == [300, 600, 1200, 2400, 3600, 3600, 3600]
    wake, fails = schedule(PassResult(), fails, 3, now)                     # clean pass: reset, next 03:00 UTC
    assert fails == 0 and dt.datetime.fromtimestamp(wake, dt.timezone.utc) == dt.datetime(2026, 9, 27, 3,
                                                                                         tzinfo=dt.timezone.utc)
    assert schedule(PassResult(permanent={"u": "x"}), 2, 3, now)[0] == next_daily_run(3, now)
    assert schedule(PassResult(disk_full=True), 0, 3, now)[0] == now + 3600


def test_finish_pass_records_done_only_when_clean_and_clears_old_errors(tmp_path):
    db = AppDB(tmp_path / "app.db")
    db.set_status(C, "error", error="old failure")
    bad = PassResult(transient={"candles spot:ETHUSDT": "ConnectError()"}, progress={"k": "v"})
    finish_pass(db, C, bad)
    r = row(db)
    assert r["state"] == "reconnecting" and "candles spot:ETHUSDT" in r["last_error"]
    assert json.loads(r["detail"])["failed"] == bad.transient
    assert "done" not in events(db) and events(db)[-1] == "incomplete"
    finish_pass(db, C, PassResult(progress={"k": "done"}))
    r = row(db)
    assert r["state"] == "stopped" and r["last_error"] is None and r["last_error_ms"] is None
    assert events(db)[-1] == "done"


def test_set_status_keeps_errors_unless_cleared(tmp_path):
    db = AppDB(tmp_path / "app.db")
    db.set_status("mt5", "reconnecting", error="IPC timeout")
    db.set_status("mt5", "live")
    assert row(db, "mt5")["last_error"] == "IPC timeout"                   # history kept while running
    db.set_status("mt5", "live", clear_error=True)
    assert row(db, "mt5")["last_error"] is None


def test_worker_survives_crashes_and_retries_soon(tmp_path):
    """F4/BF-03: a crashed or failed pass is retried within minutes (not at the next daily slot), a clean pass
    waits for the daily slot, and every idle minute refreshes the heartbeat."""
    db = AppDB(tmp_path / "app.db")
    start = dt.datetime(2026, 9, 26, 12, 10, tzinfo=dt.timezone.utc).timestamp()
    clock = Clock(start)
    outcomes = iter([OSError("getaddrinfo failed"), PassResult(transient={"metrics": "x"}), PassResult()])
    starts = []

    def run_pass() -> PassResult:
        starts.append(clock())
        out = next(outcomes)
        if isinstance(out, Exception):
            raise out
        return out

    worker_loop(run_pass, collector=C, appdb=db, daily_hour=3, once=False, logger=bl.log, sleep=clock.sleep,
                clock=clock, max_passes=3)
    assert [s - start for s in starts] == [0, 300, 900]
    assert max(clock.sleeps) <= bl.IDLE_SLICE_S
    assert events(db)[0] == "error" and row(db)["state"] == "reconnecting"
    assert "next_run" in json.loads(row(db)["detail"])


class FakeProc:
    def __init__(self) -> None:
        self.alive, self.exitcode, self.terminated = True, None, False

    def is_alive(self) -> bool:
        return self.alive

    def terminate(self) -> None:
        self.terminated, self.alive = True, False


def test_keeper_restarts_a_dead_worker_with_backoff_and_not_after_stop(tmp_path):
    db = AppDB(tmp_path / "app.db")
    clock = Clock(1000.0)
    procs: list[FakeProc] = []

    def start() -> FakeProc:
        procs.append(FakeProc())
        return procs[-1]

    k = WorkerKeeper(start, db, C, min_backoff_s=30, max_backoff_s=120, healthy_s=600, clock=clock)
    assert k.check() and len(procs) == 1
    procs[0].alive, procs[0].exitcode = False, 1                           # crashed (e.g. unhandled exception)
    assert not k.check() and row(db)["state"] == "error" and "worker_exit" in events(db)
    clock.t += 29
    assert not k.check() and len(procs) == 1                                # still backing off
    clock.t += 2
    assert k.check() and len(procs) == 2 and "backfill_restart" in events(db)
    procs[1].alive = False
    k.check()
    clock.t += 59
    assert not k.check()
    clock.t += 2
    assert k.check() and len(procs) == 3                                    # backoff doubled to 60 s
    clock.t += 601
    assert k.check() and k.backoff == 30                                    # healthy for 10 min → reset
    k.stop()
    assert procs[2].terminated and not k.check() and len(procs) == 3


def test_keeper_monitor_thread_stops(tmp_path):
    db = AppDB(tmp_path / "app.db")
    k = WorkerKeeper(FakeProc, db, C)
    done = k.monitor(interval_s=0.01)
    done.set()
    k.stop()
    assert k.proc.terminated


@pytest.mark.parametrize("hour", [0, 3, 23])
def test_next_daily_run_is_in_the_future(hour):
    now = dt.datetime(2026, 9, 26, 3, 0, tzinfo=dt.timezone.utc).timestamp()
    nxt = next_daily_run(hour, now)
    assert now < nxt <= now + 86_400
