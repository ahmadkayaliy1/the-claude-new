"""Backfill worker bookkeeping (BF-01/BF-03/BF-14/F4/OPS-02): pass results, retry scheduling, a pass that never
kills the bookkeeping, status hygiene, and the parent's keeper — one worker process per pass (Phase 5 A7: the worker
exits when its pass is done and is started again when the next pass is due). Pure logic — no market data, no
network; one test spawns a real (tiny) worker process."""
import datetime as dt
import json

import pytest

from tradingsystem.ingest.common import backfill_loop as bl
from tradingsystem.ingest.common.appdb import AppDB
from tradingsystem.ingest.common.backfill_loop import (EXIT_PASS_DISK_FULL, EXIT_PASS_DONE, EXIT_PASS_TRANSIENT,
                                                       PassResult, StepIncomplete, WorkerKeeper, finish_pass,
                                                       next_daily_run, pass_exit_code, pass_result, run_worker_pass,
                                                       schedule, spawn_pass)

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


class FakeProc:
    def __init__(self) -> None:
        self.alive, self.exitcode, self.terminated = True, None, False

    def is_alive(self) -> bool:
        return self.alive

    def terminate(self) -> None:
        self.terminated, self.alive = True, False


def test_passes_are_retried_soon_after_a_crash_or_transient_failure_and_daily_after_a_clean_one(tmp_path):
    """F4/BF-03 with one worker process per pass (A7): the crashed pass and the transient one are retried within
    minutes (5, then 10 min: the keeper counts consecutive transient passes across processes), a clean pass waits for
    the daily slot. The keeper notices an exit at its next minute check."""
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
        finish_pass(db, C, out)
        return out

    class PassProc(FakeProc):
        """A worker process that ran its pass and exited with the pass's code (as ``worker_main`` does)."""

        def __init__(self) -> None:
            super().__init__()
            self.alive, self.exitcode = False, pass_exit_code(run_worker_pass(run_pass, collector=C, appdb=db,
                                                                              logger=bl.log))

    k = WorkerKeeper(PassProc, db, C, daily_hour=3, clock=clock)
    while len(starts) < 3 or k.proc is not None:
        k.check()
        clock.t += 60
    assert [s - start for s in starts] == [0, 300, 960]
    assert dt.datetime.fromtimestamp(k.next_pass, dt.timezone.utc) == dt.datetime(2026, 9, 27, 3, tzinfo=dt.timezone.utc)
    assert k.fails == 0 and k.passes == 3 and k.restarts == 0
    assert events(db)[0] == "error" and "worker_exit" not in events(db) and "backfill_restart" not in events(db)
    assert row(db)["state"] == "stopped" and json.loads(row(db)["detail"])["next_run"] == "2026-09-27T03:00:00.000Z"


def test_a_finished_pass_leaves_no_process_and_keeps_the_heartbeat_fresh(tmp_path):
    """Between two passes no worker process exists (A7: six idle workers held ≈ 1.2 GB); the keeper refreshes the
    collector's heartbeat and keeps the state and detail the pass wrote."""
    db = AppDB(tmp_path / "app.db")
    clock = Clock(dt.datetime(2026, 9, 26, 12, 10, tzinfo=dt.timezone.utc).timestamp())
    procs: list[FakeProc] = []

    def start() -> FakeProc:
        procs.append(FakeProc())
        return procs[-1]

    k = WorkerKeeper(start, db, C, daily_hour=3, clock=clock)
    finish_pass(db, C, PassResult(progress={"spot:BTCUSDT/btcusdt_candles_1m": "done"}))
    procs[0].alive, procs[0].exitcode = False, EXIT_PASS_DONE
    assert not k.check() and k.proc is None and k.backoff == k.min_backoff_s
    with db._lock:
        db._con.execute("UPDATE collector_status SET updated_ms=0 WHERE collector=?", (C,))
    clock.t += 60
    assert not k.check() and len(procs) == 1
    r = row(db)
    assert r["updated_ms"] > 0 and r["state"] == "stopped" and r["last_error"] is None        # beat, same state
    assert json.loads(r["detail"])["spot:BTCUSDT/btcusdt_candles_1m"] == "done"
    clock.t = k.next_pass
    assert k.check() and len(procs) == 2 and k.passes == 2
    assert "worker_exit" not in events(db) and "backfill_restart" not in events(db)


def test_a_disk_full_pass_waits_the_longest_backoff(tmp_path):
    db = AppDB(tmp_path / "app.db")
    clock = Clock(1_000_000.0)
    p = FakeProc()
    k = WorkerKeeper(lambda: p, db, C, clock=clock)
    p.alive, p.exitcode = False, EXIT_PASS_DISK_FULL
    k.check()
    assert k.next_pass == clock.t + bl.RETRY_BACKOFF_S[-1] and k.proc is None


def test_pass_exit_codes_say_how_the_pass_went_and_anything_else_is_a_crash():
    assert pass_exit_code(PassResult()) == EXIT_PASS_DONE
    assert pass_exit_code(PassResult(permanent={"u": "bad row"})) == EXIT_PASS_DONE       # next daily slot
    assert pass_exit_code(PassResult(transient={"u": "x"})) == EXIT_PASS_TRANSIENT
    assert pass_exit_code(PassResult(transient={"u": "x"}, disk_full=True)) == EXIT_PASS_DISK_FULL
    assert pass_result(EXIT_PASS_DONE).ok and pass_result(EXIT_PASS_TRANSIENT).transient
    assert pass_result(EXIT_PASS_DISK_FULL).disk_full
    assert all(pass_result(c) is None for c in (None, 1, -15, 65536))     # unhandled error, kills: restart backoff


def test_a_crashed_pass_is_a_transient_result_with_an_error_event(tmp_path):
    db = AppDB(tmp_path / "app.db")

    def boom() -> PassResult:
        raise OSError("getaddrinfo failed")

    res = run_worker_pass(boom, collector=C, appdb=db, logger=bl.log)
    assert res.transient and pass_exit_code(res) == EXIT_PASS_TRANSIENT
    assert row(db)["state"] == "error" and "getaddrinfo" in row(db)["last_error"] and events(db) == ["error"]


def _one_pass_worker(db_path: str, outcome: str) -> int:
    """A worker process's body as ``worker_main`` has it, with a stand-in pass (no network, no terminal)."""
    from pathlib import Path
    db = AppDB(Path(db_path))
    try:
        def run_pass() -> PassResult:
            res = PassResult(transient={"metrics usdm:ETHUSDT": "ConnectError()"}) if outcome == "transient" \
                else PassResult()
            finish_pass(db, C, res)
            return res
        res = run_worker_pass(run_pass, collector=C, appdb=db, logger=bl.log)
    finally:
        db.close()
    return pass_exit_code(res)


def test_a_real_worker_process_exits_after_its_pass_and_is_respawned_when_the_next_is_due(tmp_path):
    """The lifecycle with real processes: the spawned worker ends with the pass's exit code (nothing stays resident),
    the keeper starts a new process only when the retry is due."""
    db_path = tmp_path / "app.db"
    db = AppDB(db_path)
    clock = Clock(dt.datetime(2026, 9, 26, 12, 10, tzinfo=dt.timezone.utc).timestamp())
    spawned = []

    def start():
        spawned.append(spawn_pass(_one_pass_worker, (str(db_path), "transient"), "backfill-test"))
        return spawned[-1]

    k = WorkerKeeper(start, db, C, clock=clock)
    try:
        spawned[0].join(120)
        assert spawned[0].exitcode == EXIT_PASS_TRANSIENT and not spawned[0].is_alive()
        assert not k.check() and k.proc is None and k.next_pass == clock.t + 300
        clock.t += 299
        assert not k.check() and len(spawned) == 1                          # nothing runs before the retry is due
        clock.t += 1
        assert k.check() and len(spawned) == 2
        spawned[1].join(120)
        assert not k.check() and k.next_pass == clock.t + 600 and k.fails == 2
        assert row(db)["state"] == "reconnecting" and "worker_exit" not in events(db)
    finally:
        k.stop()
        for p in spawned:
            p.join(10)


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
