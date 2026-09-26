"""MT5 historical backfill worker (P4.3) — its own OS process (heavy history requests stall the terminal and,
through the GIL, the calling process; the live poller must not share it).

* Rates per TF: gap-driven on the server-time grid (session-aware), fetched in month chunks with
  ``copy_rates_range`` (server-scale epoch seconds — never naive datetimes).
* Ticks: complete UTC days (newest first) → cold Parquet day files; progress in ``vision_done`` (used as a
  generic "backfill done" registry with period = date).
* Gaps the broker cannot fill (holidays) → ``known_gaps``. Re-runs daily.

Failure handling (BF-06/BF-07/OPS-02): connecting is retried with backoff (an IPC timeout right after a resume is
normal) and repeated before a step whenever the link dropped; a ``None`` from a ``copy_*`` call is an error, never
"no data" — link failures abort the step (the pass is retried within minutes) and other ``None`` results leave the
range unverified: no ``known_gaps`` row, no cached earliest bar, no tick day marked done. A gap is recorded as
``source_no_data`` only after its own range request came back verified-empty.
"""
from __future__ import annotations

import datetime as dt
import itertools
import logging
import multiprocessing as mp
import sqlite3
import time
from typing import Any, Callable

import numpy as np
import pyarrow as pa

from ...core.instruments import Instrument, InstrumentRegistry
from ...core.logsetup import setup_from_settings
from ...core.sessions import calendar_for
from ...core.settings import PathsCfg, Settings, load_settings
from ...core.timeutil import MS_PER_DAY, now_ms
from ...storage.gaps import KNOWN_GAPS
from ...storage.parquet_store import ParquetColdStore, arrow_schema, day_start_ms
from ...storage.sqlite_store import SQLiteHotStore
from ...storage.tablespec import VISION_DONE, spec_for, system_specs, table_specs
from ...storage.validators import validate_rows
from ..common.appdb import AppDB
from ..common.backfill_loop import PassResult, StepIncomplete, finish_pass, worker_loop
from .convert import mt5_candle_gaps, rates_to_rows, ticks_to_rows
from .servertime import MonotonicServerClock, ServerTimeModel
from .terminal import AccountMismatch, MT5Terminal, MT5Unavailable

log = logging.getLogger("backfill-mt5")
COLLECTOR = "mt5_backfill"
DAILY_RUN_UTC_HOUR = 4
MONTH_MS = 31 * MS_PER_DAY
HOUR_MS = 3_600_000
LIVE_COLLECTOR = "mt5"
LIVE_STALE_MS = 5_000        # live heartbeat older than this → the terminal is busy: back off
LIVE_WAIT_BEFORE_CONNECT_S = 600
LIVE_WAIT_PER_CHUNK_S = 120
CONNECT_RETRY_S = 600        # keep retrying initialize() this long before failing the pass (retried later)
CONNECT_DELAYS_S = (2, 4, 8, 16, 32, 60)
IPC_ERRORS = frozenset(range(-10005, -9999))   # -10000..-10005: IPC / terminal-link failures
MAX_GAP_VERIFY = 200         # per TF and pass; further candidate source gaps wait for the next pass


class MT5CallError(RuntimeError):
    """A ``copy_*`` call failed on the IPC/terminal link — transient, never treated as absence of data."""


class _Unverified(RuntimeError):
    """A probe came back ``None`` with a non-link error: the answer proves nothing either way."""


def is_transient(exc: BaseException) -> bool:
    """Terminal-link / lock errors a later retry can fix (an account mismatch or bad data is permanent)."""
    if isinstance(exc, StepIncomplete):
        return any(is_transient(e) for e in exc.failures.values())
    if isinstance(exc, AccountMismatch):
        return False
    if isinstance(exc, (MT5Unavailable, MT5CallError, ConnectionError, TimeoutError, PermissionError)):
        return True
    if isinstance(exc, sqlite3.OperationalError):
        return "locked" in str(exc) or "busy" in str(exc)
    return False


class MT5Backfill:
    def __init__(self, s: Settings) -> None:
        self.s = s
        self.data = s.paths.data()
        self.appdb = AppDB(self.data / "app.db")
        self.cold = ParquetColdStore(self.data / "cold")
        self.model = ServerTimeModel()
        self.term = MT5Terminal(s.mt5_data_profile())
        reg = InstrumentRegistry.from_settings(s)
        self.instruments = [i for i in reg.all() if i.venue == "mt5"]
        self.progress: dict[str, str] = {}

    def status(self, state: str = "backfilling", error: str | None = None) -> None:
        self.appdb.set_status(COLLECTOR, state, error=error, detail=self.progress)

    # ------------------------------------------------------------------ terminal etiquette
    def _live_healthy(self) -> bool:
        live = next((r for r in self.appdb.statuses() if r["collector"] == LIVE_COLLECTOR), None)
        if live is None or live["state"] in ("stopped", "error"):
            return True             # no live poller to protect (an account problem shows in our own connect)
        return live["state"] in ("live", "market_closed") and now_ms() - int(live["updated_ms"]) <= LIVE_STALE_MS

    def _wait_for_live(self, max_s: float) -> None:
        """Wait while the live poller is starting/reconnecting or its heartbeat is stale (BF-08) — the terminal
        serves clients one at a time and the live feed comes first (D-024). Proceeds after ``max_s``."""
        deadline = time.time() + max_s
        while not self._live_healthy():
            if time.time() >= deadline:
                log.warning("live MT5 collector not healthy after %.0fs — proceeding", max_s)
                return
            time.sleep(2)

    def _yield_to_live(self) -> None:
        time.sleep(0.2)
        self._wait_for_live(LIVE_WAIT_PER_CHUNK_S)

    def _connect(self, res: PassResult) -> bool:
        """Attach with backoff (BF-06): an IPC timeout while the terminal re-syncs after a resume is normal."""
        deadline = time.time() + CONNECT_RETRY_S
        for i in itertools.count():
            self._wait_for_live(LIVE_WAIT_BEFORE_CONNECT_S)
            try:
                self.term.shutdown()
                self.term.connect()
                self.term.select_symbols([inst.symbol for inst in self.instruments])
                return True
            except AccountMismatch as exc:
                log.error("MT5 backfill refuses the terminal account: %s", exc)
                res.add("connect", exc, transient=False)
                return False
            except Exception as exc:  # noqa: BLE001
                delay = CONNECT_DELAYS_S[min(i, len(CONNECT_DELAYS_S) - 1)]
                if time.time() + delay > deadline:
                    log.error("MT5 backfill cannot connect after %d attempts: %r", i + 1, exc)
                    res.add("connect", exc, transient=True)
                    return False
                log.warning("MT5 backfill cannot connect (%r) — retry in %ds", exc, delay)
                self.status("reconnecting", error=f"connect: {exc!r}"[:300])
                time.sleep(delay)
        return False

    # ------------------------------------------------------------------ MT5 calls
    def _call(self, what: str, fn: Callable[..., Any], *args: Any) -> np.ndarray | None:
        """ndarray (possibly empty) on success; None when the terminal answered with a non-link error (unverified —
        never proof of absence); raises MT5CallError when the IPC/terminal link failed."""
        r = fn(*args)
        if r is not None:
            return r
        code, msg = self.term.mt5.last_error()
        if code in IPC_ERRORS or not self.term.healthy():
            raise MT5CallError(f"{what} failed: ({code}, {msg!r})")
        log.warning("%s returned None (%s, %r) — range left unverified", what, code, msg)
        return None

    def _now_srv(self, sym: str) -> int:
        t = self.term.mt5.symbol_info_tick(sym)
        if t is None:
            code, msg = self.term.mt5.last_error()
            raise MT5CallError(f"symbol_info_tick({sym}) failed: ({code}, {msg!r})")
        return int(t.time_msc)

    def _earliest_cached(self, hot: SQLiteHotStore, table: str) -> int | None:
        rows = hot.read_range(VISION_DONE, columns=["table_name", "period", "rows"])
        for t, p, r in zip(rows["table_name"], rows["period"], rows["rows"]):
            if t == table and p == "earliest_srv_ms":
                return int(r)
        return None

    def _rates(self, sym: str, tf_const: int, lo_srv_ms: int, hi_srv_ms: int, retries: int = 3) -> np.ndarray | None:
        """Bars in the range: non-empty array, verified-empty array, or None (unverified). Link errors raise."""
        mt5 = self.term.mt5
        r = None
        for i in range(retries):
            if i:
                time.sleep(1.0 * i)         # the terminal may still be downloading history
            r = self._call("copy_rates_range", mt5.copy_rates_range, sym, tf_const, int(lo_srv_ms // 1000),
                           int(hi_srv_ms // 1000))
            if r is not None and len(r):
                return r
        if r is not None and not self.term.healthy():
            raise MT5CallError("copy_rates_range: empty while the terminal is disconnected from the broker")
        return r

    # ------------------------------------------------------------------ rates
    def candles(self, inst: Instrument, hot: SQLiteHotStore) -> None:
        start = inst.start_ms("candles")
        if start is None:
            return
        cal = calendar_for(inst.venue, inst.symbol, self.s.pairs[inst.pair].asset_class)
        now_srv = self._now_srv(inst.symbol)
        failed: dict[str, BaseException] = {}
        for tf in inst.timeframes:
            spec = spec_for(inst, "candles", tf)
            try:
                self._candles_tf(inst, hot, spec, tf, start, cal, now_srv)
            except (MT5CallError, MT5Unavailable, AccountMismatch):
                raise                                   # the link is down: the other TFs would fail the same way
            except Exception as exc:  # noqa: BLE001
                log.exception("mt5 backfill candles %s failed", spec.name)
                failed[tf.value] = exc
                self.progress[spec.name] = f"failed: {exc!r}"[:120]
        if failed:
            raise StepIncomplete(f"candles {inst.key}", failed)

    def _candles_tf(self, inst: Instrument, hot: SQLiteHotStore, spec, tf, start: int, cal, now_srv: int) -> None:
        tfc = self.term.timeframe(tf.mt5_attr)
        # earliest bar the broker serves (probe from the oldest month forwards, coarse)
        have = hot.read_range(spec, columns=["srv_time"])["srv_time"]
        first = self._earliest_cached(hot, spec.name)
        if first is None:
            try:
                first = self._earliest_bar(inst.symbol, tfc, max(start, 0), now_srv)  # link errors raise: no cache
            except _Unverified as exc:
                log.warning("%s: earliest bar unverified (%s) — not cached", spec.name, exc)
                if len(have) == 0:
                    self.progress[spec.name] = "earliest bar unverified (retried next pass)"
                    return
            else:
                if first is not None and self.term.healthy():
                    hot.replace(VISION_DONE, [(spec.name, "earliest_srv_ms", first, now_ms())])
        if first is None and len(have) == 0:
            self.progress[spec.name] = "no history on server"
            return
        lo = min(x for x in (first, int(have.min()) if len(have) else None) if x is not None)
        if start > 0:
            lo = max(lo, self.model.utc_to_server(start))
        end = (now_srv // tf.ms) * tf.ms - tf.ms          # last closed bar (server grid, approx for W1)
        known = self._known(hot, spec.name)
        gaps = [g for g in mt5_candle_gaps(have, tf, lo, end, cal, self.model) if g.start not in known]
        self.progress[spec.name] = f"{sum(g.count for g in gaps):,} bars to fetch in {len(gaps)} gaps"
        self.status()
        chunk = max(tf.ms * 10_000, MS_PER_DAY)          # ≈ 7 days of M1 per request
        unverified: list[tuple[int, int]] = []
        for g in gaps:
            for c0 in range(g.start, g.end + tf.ms, chunk):
                c1 = min(c0 + chunk, g.end + tf.ms)
                self._yield_to_live()
                r = self._rates(inst.symbol, tfc, c0, c1)
                if r is None:
                    unverified.append((c0, c1))
                    continue
                if not len(r):
                    continue
                rows = rates_to_rows(r, self.model)
                res = validate_rows(spec, rows, venue="mt5")
                if res.rejected:
                    log.warning("%s: %d bars rejected (%s)", spec.name, res.n_rejected, res.rejected[0][1])
                hot.upsert(spec, res.good)
        have = hot.read_range(spec, columns=["srv_time"])["srv_time"]
        left = [g for g in mt5_candle_gaps(have, tf, lo, end, cal, self.model) if g.start not in known]
        # only a verified-empty answer proves absence (BF-07): never record ranges whose request failed, and ask
        # once more for exactly the gap (a chunk answered while history was still downloading may be partial)
        candidates = [g for g in left if not any(a <= g.end and g.start < b for a, b in unverified)]
        confirmed = []
        for g in candidates[:MAX_GAP_VERIFY]:
            self._yield_to_live()
            r = self._rates(inst.symbol, tfc, g.start, g.end, retries=2)     # copy_rates_range: both ends inclusive
            if r is None:
                unverified.append((g.start, g.end + tf.ms))
                continue
            t = r["time"].astype(np.int64) * 1000
            inside = r[(t >= g.start) & (t <= g.end)]
            if len(inside):
                rows = validate_rows(spec, rates_to_rows(inside, self.model), venue="mt5").good
                hot.upsert(spec, rows)          # late history: filled now, any rest is re-checked next pass
            else:
                confirmed.append(g)
        if confirmed:
            hot.upsert(KNOWN_GAPS, [(spec.name, g.start, g.end, "source_no_data", now_ms(),
                                      f"{g.count} bars absent on {self.term.profile.server} (server-time keys)")
                                     for g in confirmed])
        note = f", {len(unverified)} range(s) unverified" if unverified else ""
        if len(candidates) > MAX_GAP_VERIFY:
            note += f", {len(candidates) - MAX_GAP_VERIFY} gap(s) left for the next pass"
        self.progress[spec.name] = f"done ({len(have):,} bars, {len(confirmed)} source gaps{note})"
        self.status()

    def _probe(self, sym: str, tfc: int, lo: int, hi: int, retries: int = 3) -> np.ndarray:
        """Bars in [lo, hi] as proof (possibly empty); raises ``_Unverified`` on a non-link ``None``."""
        r = self._rates(sym, tfc, lo, hi, retries)
        if r is None:
            raise _Unverified(f"copy_rates_range {sym} {lo}..{hi}")
        return r

    def _earliest_bar(self, sym: str, tfc: int, start_ms: int, now_srv: int) -> int | None:
        """Earliest bar the broker serves (month binary search). Every probe must be verified, else the search
        would land on a later month and cache a wrong floor (BF-07)."""
        lo = max(start_ms, self.model.utc_to_server(1_546_300_800_000) if start_ms == 0 else start_ms)  # ≥ 2019
        months = list(range(lo, now_srv, MONTH_MS))
        if not months:
            return None
        a, b = 0, len(months) - 1
        if not len(self._probe(sym, tfc, months[b], now_srv)):
            return None
        while a < b:
            mid = (a + b) // 2
            if len(self._probe(sym, tfc, months[mid], months[mid] + MONTH_MS, retries=2)):
                b = mid
            else:
                a = mid + 1
        r = self._probe(sym, tfc, months[a], months[a] + MONTH_MS)
        return int(r["time"][0]) * 1000 if len(r) else None

    @staticmethod
    def _known(hot: SQLiteHotStore, table: str) -> set[int]:
        g = hot.read_range(KNOWN_GAPS, columns=["table_name", "start"])
        return {int(s) for t, s in zip(g["table_name"], g["start"]) if t == table}

    # ------------------------------------------------------------------ ticks
    def ticks(self, inst: Instrument, hot: SQLiteHotStore, today: dt.date | None = None) -> None:
        start = inst.start_ms("ticks")
        if start is None or "ticks" not in inst.datatypes:
            return
        spec = spec_for(inst, "ticks")
        schema = arrow_schema(spec)
        mt5 = self.term.mt5
        done_rows = hot.read_range(VISION_DONE, columns=["table_name", "period"])
        done = {p for t, p in zip(done_rows["table_name"], done_rows["period"]) if t == spec.name}
        today = today or dt.datetime.now(dt.timezone.utc).date()
        first_day = dt.date(2019, 1, 1) if start == 0 else dt.datetime.fromtimestamp(start / 1000, dt.timezone.utc).date()
        cal = calendar_for(inst.venue, inst.symbol, self.s.pairs[inst.pair].asset_class)
        day = today - dt.timedelta(days=1)
        empty_streak = n_days = n_unverified = 0
        while day >= first_day:
            period = day.isoformat()
            lo_utc = day_start_ms(day)
            if period in done:
                day -= dt.timedelta(days=1)
                continue
            open_any = any(cal.is_open(lo_utc + h * 3_600_000) for h in range(24))
            parts, unverified = [], False
            for h in range(24):
                a = self.model.utc_to_server(lo_utc + h * HOUR_MS)
                b = self.model.utc_to_server(lo_utc + (h + 1) * HOUR_MS)
                self._yield_to_live()
                chunk = self._call("copy_ticks_range", mt5.copy_ticks_range, inst.symbol, int(a // 1000),
                                   int(b // 1000) + 1, mt5.COPY_TICKS_ALL)
                if chunk is None:
                    unverified = True
                    continue
                if len(chunk):
                    tm = chunk["time_msc"].astype(np.int64)
                    chunk = chunk[(tm >= a) & (tm < b)]     # exact [a, b) → hour chunks never overlap
                    if len(chunk):
                        parts.append(chunk)
            t = np.concatenate(parts) if parts else None
            if t is None or len(t) == 0:
                if unverified:
                    n_unverified += 1           # neither done nor evidence of the start of tick history
                elif open_any:
                    if not self.term.healthy():
                        raise MT5CallError("copy_ticks_range: empty day while the terminal is disconnected")
                    empty_streak += 1
                    if empty_streak >= 10:        # ten verified-empty trading days → start of tick history
                        self.progress[spec.name] = f"tick history starts after {day + dt.timedelta(days=10)}"
                        break
                day -= dt.timedelta(days=1)
                continue
            empty_streak = 0
            rows = ticks_to_rows(t, MonotonicServerClock(self.model))
            rows = [r for r in rows if lo_utc <= r[1] < lo_utc + MS_PER_DAY]
            res = validate_rows(spec, rows, venue="mt5")
            if res.good:
                cols = list(zip(*res.good))
                tbl = pa.Table.from_arrays([pa.array(c, type=f.type) for c, f in zip(cols, schema)], schema=schema)
                self.cold.write_day(inst, spec, day, tbl, merge=True)
            if unverified:
                n_unverified += 1               # partial day kept (idempotent merge) but retried next pass
            else:
                hot.replace(VISION_DONE, [(spec.name, period, len(res.good), now_ms())])
            n_days += 1
            if n_days % 5 == 0:
                self.progress[spec.name] = f"{n_days} days (at {period})"
                self.status()
            day -= dt.timedelta(days=1)
        if n_unverified:
            self.progress[spec.name] = f"{n_days} days, {n_unverified} unverified (retried next pass)"
        self.progress.setdefault(spec.name, "done")

    # ------------------------------------------------------------------ run
    def run(self) -> PassResult:
        res = PassResult(progress=self.progress)
        hots: dict[str, SQLiteHotStore] = {}
        try:
            self.status("backfilling")
            if not self._connect(res):
                finish_pass(self.appdb, COLLECTOR, res)
                return res
            for inst in self.instruments:
                hot = SQLiteHotStore(inst.hot_db_path(self.data), cache_mb=self.s.resource.sqlite_cache_mb)
                hots[inst.key] = hot
                hot.ensure_tables([*table_specs(inst), *system_specs()])
            for unit, step, inst in [(f"{st.__name__} {i.key}", st, i) for st in (self.candles, self.ticks)
                                     for i in self.instruments]:
                # the link dropped (previous unit failed, terminal re-syncing): reconnect with backoff, or end the
                # pass (retried within minutes) instead of failing every remaining unit on a dead link
                if not (self.term.connected and self.term.healthy()) and not self._connect(res):
                    break
                self.status()
                try:
                    step(inst, hots[inst.key])
                except AccountMismatch as exc:
                    log.error("%s: %s", unit, exc)
                    res.add(unit, exc, transient=False)
                    break
                except Exception as exc:  # noqa: BLE001
                    log.exception("mt5 backfill %s failed for %s", step.__name__, inst.key)
                    self.appdb.add_event(COLLECTOR, "error", f"{unit}: {exc!r}"[:300])
                    res.add(unit, exc, is_transient(exc))
            finish_pass(self.appdb, COLLECTOR, res)
            return res
        finally:
            for h in hots.values():
                h.close()
            self.term.shutdown()
            self.appdb.close()


def worker_main(data_dir: str | None, once: bool = False) -> None:
    s = load_settings()
    if data_dir:
        s = s.model_copy(update={"paths": PathsCfg(data_dir=data_dir, logs_dir=s.paths.logs_dir)})
    setup_from_settings("backfill-mt5", s)
    appdb = AppDB(s.paths.data() / "app.db")
    worker_loop(lambda: MT5Backfill(s).run(), collector=COLLECTOR, appdb=appdb, daily_hour=DAILY_RUN_UTC_HOUR,
                once=once, logger=log)


def start_worker(data_dir: str | None) -> mp.Process:
    p = mp.get_context("spawn").Process(target=worker_main, args=(data_dir, False), name="backfill-mt5", daemon=True)
    p.start()
    return p
