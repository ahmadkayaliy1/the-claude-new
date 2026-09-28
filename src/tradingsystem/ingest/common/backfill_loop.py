"""Backfill pass bookkeeping shared by the Binance and MT5 workers (BF-01/BF-03/F4/OPS-02).

* A pass returns a :class:`PassResult`. Failed units are classified *transient* (network, terminal link, file
  lock → the pass is retried within minutes, 5 → 60 min backoff) or *permanent* (bad data, 4xx → next daily slot).
  ``done`` is recorded only after a clean pass; otherwise the collector shows the failed units.
* One pass per worker process (Phase 5 A7): the worker runs a pass and exits with a code saying how it went
  (:func:`pass_exit_code`); a crashed pass is a transient result, never a dead worker. An idle worker used to stay
  resident between passes — six of them held ≈ 1.2 GB of private memory on the production laptop (2026-09-28).
* :class:`WorkerKeeper` (in the parent ingester) starts the next pass when it is due — the daily slot after a clean
  pass, the 5 → 60 min backoff after transient failures, the longest backoff when the disk is full — keeps the
  collector's heartbeat fresh while no worker runs (wall clock: laptop sleep/resume safe), and restarts a crashed
  worker with its own backoff.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import multiprocessing as mp
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from ...core.timeutil import iso, now_ms
from .appdb import AppDB

log = logging.getLogger(__name__)

RETRY_BACKOFF_S = (300, 600, 1200, 2400, 3600)   # after a pass with transient failures
# a worker process's exit code after one pass (anything else — 1 for an unhandled error, a kill — is a crash)
EXIT_PASS_DONE = 0            # clean, or permanent failures only: the next pass at the daily slot
EXIT_PASS_TRANSIENT = 75      # transient failures (EX_TEMPFAIL): the next pass after the 5 → 60 min backoff
EXIT_PASS_DISK_FULL = 76      # free disk below storage.min_free_disk_gb: the next pass after the longest backoff


class StepIncomplete(RuntimeError):
    """Some units of a step failed while the others were processed; carries the per-unit errors."""

    def __init__(self, what: str, failures: dict[str, BaseException]) -> None:
        unit, exc = next(iter(failures.items()))
        super().__init__(f"{what}: {len(failures)} unit(s) failed, e.g. {unit}: {exc!r}"[:300])
        self.failures = failures


@dataclass
class PassResult:
    transient: dict[str, str] = field(default_factory=dict)
    permanent: dict[str, str] = field(default_factory=dict)
    disk_full: bool = False
    progress: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not (self.transient or self.permanent or self.disk_full)

    @property
    def state(self) -> str:
        """collector_status state after the pass."""
        if self.ok:
            return "stopped"
        return "error" if self.permanent or self.disk_full else "reconnecting"

    def add(self, unit: str, exc: BaseException, transient: bool) -> None:
        (self.transient if transient else self.permanent)[unit] = repr(exc)[:200]

    def summary(self) -> str:
        units = [*self.transient, *self.permanent]
        return f"{len(units)} unit(s) failed: {', '.join(units)}"[:300]


def finish_pass(appdb: AppDB, collector: str, res: PassResult) -> None:
    """Final status of a pass: 'stopped' + 'done' event (error cleared) only when nothing failed."""
    if res.ok:
        appdb.set_status(collector, "stopped", detail=res.progress, clear_error=True)
        appdb.add_event(collector, "done", iso(now_ms()))
    else:
        appdb.set_status(collector, res.state, error=res.summary(),
                         detail={**res.progress, "failed": {**res.transient, **res.permanent}})
        appdb.add_event(collector, "incomplete", res.summary())


def next_daily_run(hour: int, now_s: float) -> float:
    now = dt.datetime.fromtimestamp(now_s, dt.timezone.utc)
    nxt = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if nxt <= now:
        nxt += dt.timedelta(days=1)
    return nxt.timestamp()


def schedule(res: PassResult, fails: int, daily_hour: int, now_s: float) -> tuple[float, int]:
    """(wake-up epoch s, consecutive transient-failure count) for the next pass."""
    if res.disk_full:                       # retrying fast frees no disk
        return now_s + RETRY_BACKOFF_S[-1], fails
    if res.transient:
        return now_s + RETRY_BACKOFF_S[min(fails, len(RETRY_BACKOFF_S) - 1)], fails + 1
    return next_daily_run(daily_hour, now_s), 0


def _safe(fn: Callable, *args, **kwargs) -> None:
    try:
        fn(*args, **kwargs)
    except Exception:  # noqa: BLE001 — bookkeeping must never kill the worker
        log.exception("status update failed")


def run_worker_pass(run_pass: Callable[[], PassResult], *, collector: str, appdb: AppDB,
                    logger: logging.Logger) -> PassResult:
    """One pass inside a worker process. A crashed pass never kills the bookkeeping (BF-03): it becomes a transient
    result (retried within minutes) with an ``error`` status and event."""
    try:
        res = run_pass()
    except Exception as exc:  # noqa: BLE001
        logger.exception("backfill pass crashed")
        _safe(appdb.set_status, collector, "error", error=f"pass crashed: {exc!r}"[:300])
        _safe(appdb.add_event, collector, "error", f"pass crashed: {exc!r}"[:300])
        res = PassResult(transient={"pass": repr(exc)[:200]})
    logger.info("backfill pass %s — the worker exits; the ingester starts the next pass when it is due",
                "complete" if res.ok else f"incomplete ({res.summary()})")
    return res


def pass_exit_code(res: PassResult) -> int:
    """The worker process's exit code for a finished pass (read back by :func:`pass_result`)."""
    if res.disk_full:
        return EXIT_PASS_DISK_FULL
    return EXIT_PASS_TRANSIENT if res.transient else EXIT_PASS_DONE


def pass_result(code: int | None) -> PassResult | None:
    """What a worker's exit code says about its pass, as far as :func:`schedule` needs it; None when the process did
    not finish a pass (an unhandled error, a kill, still unknown)."""
    if code == EXIT_PASS_DONE:
        return PassResult()
    if code == EXIT_PASS_TRANSIENT:
        return PassResult(transient={"pass": "transient failures (see the worker log)"})
    if code == EXIT_PASS_DISK_FULL:
        return PassResult(disk_full=True)
    return None


def _pass_process(target: Callable[..., int], args: tuple) -> None:
    sys.exit(target(*args))


def spawn_pass(target: Callable[..., int], args: tuple, name: str) -> mp.Process:
    """Start ONE pass in a fresh process (spawn: nothing of the ingester is inherited); ``target(*args)`` returns the
    exit code (:func:`pass_exit_code`) and must be a module-level function."""
    p = mp.get_context("spawn").Process(target=_pass_process, args=(target, args), name=name, daemon=True)
    p.start()
    return p


class WorkerKeeper:
    """Runs the backfill as one worker process per pass (A7) and keeps that going (BF-03).

    * A worker that exits with a pass code (:func:`pass_result`) is gone until its next pass is due — the daily slot
      after a clean pass, 5 → 60 min after transient failures (consecutive ones counted here), an hour when the disk
      is full; meanwhile the keeper refreshes the collector's heartbeat (the row keeps the state and detail the pass
      wrote, plus ``next_run``) and no memory stays resident.
    * Any other exit (an unhandled error, a kill) is reported once (``worker_exit`` event, ``error`` status) and the
      worker is restarted after a backoff of 30 s → 30 min (reset after ``healthy_s`` of running or a finished pass).

    The owning ingester calls :meth:`check` periodically — or :meth:`monitor` for a daemon thread doing it — and
    :meth:`stop` on shutdown (after which nothing is started).
    """

    def __init__(self, start: Callable[[], mp.Process], appdb: AppDB, collector: str, *, daily_hour: int = 3,
                 min_backoff_s: float = 30, max_backoff_s: float = 1800, healthy_s: float = 600,
                 clock: Callable[[], float] = time.time) -> None:
        self._start, self.appdb, self.collector, self.daily_hour = start, appdb, collector, daily_hour
        self.min_backoff_s, self.max_backoff_s, self.healthy_s, self._clock = min_backoff_s, max_backoff_s, healthy_s, clock
        self.backoff = min_backoff_s
        self.restarts = 0
        self.passes = 1
        self.fails = 0                          # consecutive passes with transient failures
        self._lock = threading.Lock()
        self._stopped = False
        self.proc: mp.Process | None = start()
        self.started = clock()
        self.next_try: float | None = None      # restart time after a crash
        self.next_pass: float | None = None     # start time of the next pass while no worker runs

    def check(self) -> bool:
        """True while a worker process runs (a pass is in progress)."""
        with self._lock:
            if self._stopped:
                return False
            now = self._clock()
            if self.proc is not None and self.proc.is_alive():
                if now - self.started > self.healthy_s:
                    self.backoff = self.min_backoff_s
                return True
            if self.proc is not None and self.next_try is None:        # the worker process has just ended
                code = self.proc.exitcode
                res = pass_result(code)
                if res is None:
                    log.error("%s worker exited (code %s) — restart in %.0fs", self.collector, code, self.backoff)
                    _safe(self.appdb.add_event, self.collector, "worker_exit", f"exitcode={code}")
                    _safe(self.appdb.set_status, self.collector, "error", error=f"worker exited (code {code})")
                    self.next_try = now + self.backoff
                    return False
                self.proc, self.backoff = None, self.min_backoff_s
                self.next_pass, self.fails = schedule(res, self.fails, self.daily_hour, now)
                log.info("%s pass %s — worker exited, next pass in %.1f h (%s)", self.collector,
                         "complete" if res.ok else "incomplete", (self.next_pass - now) / 3600,
                         iso(int(self.next_pass * 1000)))
                _safe(self._note_next_run, res)
                return False
            if self.next_pass is not None and self.next_try is None:   # between two passes: no process
                if now < self.next_pass:
                    _safe(self.appdb.touch, self.collector)             # idle heartbeat
                    return False
                return self._launch(now, restart=False)
            if self.next_try is None or now < self.next_try:
                return False
            return self._launch(now, restart=True)

    def _launch(self, now: float, *, restart: bool) -> bool:
        if restart:
            self.restarts += 1
            self.backoff = min(self.backoff * 2, self.max_backoff_s)
        try:
            self.proc = self._start()
        except Exception:  # noqa: BLE001 — try again after the (grown) backoff
            log.exception("%s worker start failed", self.collector)
            self.proc, self.next_pass, self.next_try = None, None, now + self.backoff
            return False
        self.started, self.next_try, self.next_pass = now, None, None
        if restart:
            _safe(self.appdb.add_event, self.collector, "backfill_restart", f"restart #{self.restarts}")
            log.warning("%s worker restarted (#%d)", self.collector, self.restarts)
        else:
            self.passes += 1
            log.info("%s pass #%d started", self.collector, self.passes)
        return True

    def _note_next_run(self, res: PassResult) -> None:
        """``next_run`` in the collector's detail; state and detail otherwise stay as the pass wrote them."""
        row = next((r for r in self.appdb.statuses() if r["collector"] == self.collector), None)
        try:
            detail = json.loads(row["detail"]) if row and row["detail"] else {}
        except ValueError:
            detail = {}
        detail = detail if isinstance(detail, dict) else {}
        state = row["state"] if row else res.state
        self.appdb.set_status(self.collector, state, detail={**detail, "next_run": iso(int(self.next_pass * 1000))})

    def monitor(self, interval_s: float = 60.0) -> threading.Event:
        """Run :meth:`check` every ``interval_s`` in a daemon thread; set the returned event to end it."""
        done = threading.Event()

        def loop() -> None:
            while not done.wait(interval_s):
                try:
                    self.check()
                except Exception:  # noqa: BLE001
                    log.exception("%s keeper check failed", self.collector)

        threading.Thread(target=loop, name=f"{self.collector}-keeper", daemon=True).start()
        return done

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
            if self.proc is not None and self.proc.is_alive():
                self.proc.terminate()
