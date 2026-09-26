"""Backfill pass bookkeeping shared by the Binance and MT5 workers (BF-01/BF-03/F4/OPS-02).

* A pass returns a :class:`PassResult`. Failed units are classified *transient* (network, terminal link, file
  lock → the pass is retried within minutes, 5 → 60 min backoff) or *permanent* (bad data, 4xx → next daily slot).
  ``done`` is recorded only after a clean pass; otherwise the collector shows the failed units.
* The worker loop never dies on an exception and sleeps in short wall-clock slices (laptop sleep/resume safe),
  refreshing its ``collector_status`` heartbeat while idle.
* :class:`WorkerKeeper` lets the parent ingester restart a dead worker process with backoff.
"""
from __future__ import annotations

import datetime as dt
import logging
import multiprocessing as mp
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from ...core.timeutil import iso, now_ms
from .appdb import AppDB

log = logging.getLogger(__name__)

RETRY_BACKOFF_S = (300, 600, 1200, 2400, 3600)   # after a pass with transient failures
IDLE_SLICE_S = 60.0                               # max single sleep: wall clock re-checked, heartbeat refreshed


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


def worker_loop(run_pass: Callable[[], PassResult], *, collector: str, appdb: AppDB, daily_hour: int, once: bool,
                logger: logging.Logger, sleep: Callable[[float], None] = time.sleep,
                clock: Callable[[], float] = time.time, max_passes: int | None = None) -> None:
    """Run passes forever: transient failures → retry with backoff; clean/permanent → next daily slot."""
    fails = passes = 0
    while True:
        try:
            res = run_pass()
        except Exception as exc:  # noqa: BLE001 — the worker must never die (BF-03)
            logger.exception("backfill pass crashed")
            res = PassResult(transient={"pass": repr(exc)[:200]})
            _safe(appdb.set_status, collector, "error", error=f"pass crashed: {exc!r}"[:300])
            _safe(appdb.add_event, collector, "error", f"pass crashed: {exc!r}"[:300])
        passes += 1
        if once or (max_passes is not None and passes >= max_passes):
            return
        wake, fails = schedule(res, fails, daily_hour, clock())
        state = res.state
        logger.info("backfill pass %s — next pass in %.1f h (%s)", "complete" if res.ok else "incomplete",
                    (wake - clock()) / 3600, iso(int(wake * 1000)))
        detail = {**res.progress, "next_run": iso(int(wake * 1000))}
        if not res.ok:
            detail["failed"] = {**res.transient, **res.permanent}
        while (left := wake - clock()) > 0:
            _safe(appdb.set_status, collector, state, detail=detail)       # idle heartbeat
            sleep(min(IDLE_SLICE_S, left))


class WorkerKeeper:
    """Keeps a backfill worker process alive: restarts it after an unexpected exit (30 s → 30 min backoff).

    The owning ingester calls :meth:`check` periodically — or :meth:`monitor` for a daemon thread doing it — and
    :meth:`stop` on shutdown (after which nothing is restarted).
    """

    def __init__(self, start: Callable[[], mp.Process], appdb: AppDB, collector: str, *, min_backoff_s: float = 30,
                 max_backoff_s: float = 1800, healthy_s: float = 600, clock: Callable[[], float] = time.time) -> None:
        self._start, self.appdb, self.collector = start, appdb, collector
        self.min_backoff_s, self.max_backoff_s, self.healthy_s, self._clock = min_backoff_s, max_backoff_s, healthy_s, clock
        self.backoff = min_backoff_s
        self.restarts = 0
        self._lock = threading.Lock()
        self._stopped = False
        self.proc: mp.Process | None = start()
        self.started = clock()
        self.next_try: float | None = None

    def check(self) -> bool:
        """True while the worker runs; a dead worker is reported once and restarted after the backoff."""
        with self._lock:
            if self._stopped:
                return False
            now = self._clock()
            if self.proc is not None and self.proc.is_alive():
                if now - self.started > self.healthy_s:
                    self.backoff = self.min_backoff_s
                return True
            if self.next_try is None:
                code = self.proc.exitcode if self.proc is not None else None
                log.error("%s worker exited (code %s) — restart in %.0fs", self.collector, code, self.backoff)
                _safe(self.appdb.add_event, self.collector, "worker_exit", f"exitcode={code}")
                _safe(self.appdb.set_status, self.collector, "error", error=f"worker exited (code {code})")
                self.next_try = now + self.backoff
                return False
            if now < self.next_try:
                return False
            self.restarts += 1
            self.backoff = min(self.backoff * 2, self.max_backoff_s)
            try:
                self.proc = self._start()
            except Exception:  # noqa: BLE001 — try again after the (grown) backoff
                log.exception("%s worker restart failed", self.collector)
                self.proc, self.next_try = None, now + self.backoff
                return False
            self.started, self.next_try = now, None
            _safe(self.appdb.add_event, self.collector, "backfill_restart", f"restart #{self.restarts}")
            log.warning("%s worker restarted (#%d)", self.collector, self.restarts)
            return True

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
