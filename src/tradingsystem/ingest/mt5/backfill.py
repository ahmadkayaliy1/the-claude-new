"""MT5 historical backfill worker (P4.3) — its own OS process (heavy history requests stall the terminal and,
through the GIL, the calling process; the live poller must not share it).

* Rates per TF: gap-driven on the server-time grid (session-aware), fetched in month chunks with
  ``copy_rates_range`` (server-scale epoch seconds — never naive datetimes).
* Ticks: complete UTC days (newest first) → cold Parquet day files; progress in ``vision_done`` (used as a
  generic "backfill done" registry with period = date).
* Gaps the broker cannot fill (holidays) → ``known_gaps``. Re-runs daily.
"""
from __future__ import annotations

import datetime as dt
import logging
import multiprocessing as mp
import time

import numpy as np
import pyarrow as pa

from ...core.instruments import Instrument, InstrumentRegistry
from ...core.logsetup import setup_from_settings
from ...core.sessions import calendar_for
from ...core.settings import PathsCfg, Settings, load_settings
from ...core.timeutil import MS_PER_DAY, iso, now_ms
from ...storage.gaps import KNOWN_GAPS
from ...storage.parquet_store import ParquetColdStore, arrow_schema, day_start_ms
from ...storage.sqlite_store import SQLiteHotStore
from ...storage.tablespec import VISION_DONE, spec_for, system_specs, table_specs
from ...storage.validators import validate_rows
from ..common.appdb import AppDB
from .convert import mt5_candle_gaps, rates_to_rows, ticks_to_rows
from .servertime import MonotonicServerClock, ServerTimeModel
from .terminal import MT5Terminal

log = logging.getLogger("backfill-mt5")
COLLECTOR = "mt5_backfill"
DAILY_RUN_UTC_HOUR = 4
MONTH_MS = 31 * MS_PER_DAY
HOUR_MS = 3_600_000
LIVE_COLLECTOR = "mt5"
LIVE_STALE_MS = 5_000        # live heartbeat older than this → the terminal is busy: back off


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

    def _yield_to_live(self) -> None:
        """The terminal serves clients one at a time: never starve the live poller (D-024)."""
        time.sleep(0.2)
        for _ in range(24):
            live = next((r for r in self.appdb.statuses() if r["collector"] == LIVE_COLLECTOR), None)
            if live is None or live["state"] not in ("live",) or now_ms() - int(live["updated_ms"]) <= LIVE_STALE_MS:
                return
            time.sleep(5)

    def _earliest_cached(self, hot: SQLiteHotStore, table: str) -> int | None:
        rows = hot.read_range(VISION_DONE, columns=["table_name", "period", "rows"])
        for t, p, r in zip(rows["table_name"], rows["period"], rows["rows"]):
            if t == table and p == "earliest_srv_ms":
                return int(r)
        return None

    def _rates(self, sym: str, tf_const: int, lo_srv_ms: int, hi_srv_ms: int, retries: int = 3) -> np.ndarray | None:
        mt5 = self.term.mt5
        for i in range(retries):
            r = mt5.copy_rates_range(sym, tf_const, int(lo_srv_ms // 1000), int(hi_srv_ms // 1000))
            if r is not None and len(r):
                return r
            time.sleep(1.0 * (i + 1))       # the terminal may still be downloading history
        return None

    # ------------------------------------------------------------------ rates
    def candles(self, inst: Instrument, hot: SQLiteHotStore) -> None:
        start = inst.start_ms("candles")
        if start is None:
            return
        cal = calendar_for(inst.venue, inst.symbol, self.s.pairs[inst.pair].asset_class)
        mt5 = self.term.mt5
        now_srv = int(mt5.symbol_info_tick(inst.symbol).time_msc)
        for tf in inst.timeframes:
            spec = spec_for(inst, "candles", tf)
            tfc = self.term.timeframe(tf.mt5_attr)
            # earliest bar the broker serves (probe from the oldest month forwards, coarse)
            have = hot.read_range(spec, columns=["srv_time"])["srv_time"]
            first = self._earliest_cached(hot, spec.name)
            if first is None:
                first = self._earliest_bar(inst.symbol, tfc, max(start, 0), now_srv)
                if first is not None:
                    hot.replace(VISION_DONE, [(spec.name, "earliest_srv_ms", first, now_ms())])
            if first is None and len(have) == 0:
                self.progress[spec.name] = "no history on server"
                continue
            lo = min(x for x in (first, int(have.min()) if len(have) else None) if x is not None)
            if start > 0:
                lo = max(lo, self.model.utc_to_server(start))
            end = (now_srv // tf.ms) * tf.ms - tf.ms          # last closed bar (server grid, approx for W1)
            known = self._known(hot, spec.name)
            gaps = [g for g in mt5_candle_gaps(have, tf, lo, end, cal, self.model) if g.start not in known]
            self.progress[spec.name] = f"{sum(g.count for g in gaps):,} bars to fetch in {len(gaps)} gaps"
            self.status()
            chunk = max(tf.ms * 10_000, MS_PER_DAY)          # ≈ 7 days of M1 per request
            for g in gaps:
                for c0 in range(g.start, g.end + tf.ms, chunk):
                    c1 = min(c0 + chunk, g.end + tf.ms)
                    self._yield_to_live()
                    r = self._rates(inst.symbol, tfc, c0, c1)
                    if r is None:
                        continue
                    rows = rates_to_rows(r, self.model)
                    res = validate_rows(spec, rows, venue="mt5")
                    if res.rejected:
                        log.warning("%s: %d bars rejected (%s)", spec.name, res.n_rejected, res.rejected[0][1])
                    hot.upsert(spec, res.good)
            have = hot.read_range(spec, columns=["srv_time"])["srv_time"]
            left = [g for g in mt5_candle_gaps(have, tf, lo, end, cal, self.model) if g.start not in known]
            if left:
                hot.upsert(KNOWN_GAPS, [(spec.name, g.start, g.end, "source_no_data", now_ms(),
                                          f"{g.count} bars absent on {self.term.profile.server} (server-time keys)")
                                         for g in left])
            self.progress[spec.name] = f"done ({len(have):,} bars, {len(left)} source gaps)"
            self.status()

    def _earliest_bar(self, sym: str, tfc: int, start_ms: int, now_srv: int) -> int | None:
        lo = max(start_ms, self.model.utc_to_server(1_546_300_800_000) if start_ms == 0 else start_ms)  # ≥ 2019
        months = list(range(lo, now_srv, MONTH_MS))
        if not months:
            return None
        a, b = 0, len(months) - 1
        if self._rates(sym, tfc, months[b], now_srv) is None:
            return None
        while a < b:
            mid = (a + b) // 2
            if self._rates(sym, tfc, months[mid], months[mid] + MONTH_MS, retries=2) is not None:
                b = mid
            else:
                a = mid + 1
        r = self._rates(sym, tfc, months[a], months[a] + MONTH_MS)
        return int(r["time"][0]) * 1000 if r is not None else None

    @staticmethod
    def _known(hot: SQLiteHotStore, table: str) -> set[int]:
        g = hot.read_range(KNOWN_GAPS, columns=["table_name", "start"])
        return {int(s) for t, s in zip(g["table_name"], g["start"]) if t == table}

    # ------------------------------------------------------------------ ticks
    def ticks(self, inst: Instrument, hot: SQLiteHotStore) -> None:
        start = inst.start_ms("ticks")
        if start is None or "ticks" not in inst.datatypes:
            return
        spec = spec_for(inst, "ticks")
        schema = arrow_schema(spec)
        done_rows = hot.read_range(VISION_DONE, columns=["table_name", "period"])
        done = {p for t, p in zip(done_rows["table_name"], done_rows["period"]) if t == spec.name}
        today = dt.datetime.now(dt.timezone.utc).date()
        first_day = dt.date(2019, 1, 1) if start == 0 else dt.datetime.fromtimestamp(start / 1000, dt.timezone.utc).date()
        cal = calendar_for(inst.venue, inst.symbol, self.s.pairs[inst.pair].asset_class)
        day = today - dt.timedelta(days=1)
        empty_streak = 0
        n_days = 0
        while day >= first_day:
            period = day.isoformat()
            lo_utc = day_start_ms(day)
            if period in done:
                day -= dt.timedelta(days=1)
                continue
            open_any = any(cal.is_open(lo_utc + h * 3_600_000) for h in range(24))
            parts = []
            for h in range(24):
                a = self.model.utc_to_server(lo_utc + h * HOUR_MS)
                b = self.model.utc_to_server(lo_utc + (h + 1) * HOUR_MS)
                self._yield_to_live()
                chunk = self.term.mt5.copy_ticks_range(inst.symbol, int(a // 1000), int(b // 1000) + 1,
                                                       self.term.mt5.COPY_TICKS_ALL)
                if chunk is not None and len(chunk):
                    tm = chunk["time_msc"].astype(np.int64)
                    chunk = chunk[(tm >= a) & (tm < b)]     # exact [a, b) → hour chunks never overlap
                    if len(chunk):
                        parts.append(chunk)
            t = np.concatenate(parts) if parts else None
            if t is None or len(t) == 0:
                if open_any:
                    empty_streak += 1
                    if empty_streak >= 10:        # ten trading days without ticks → start of tick history
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
            hot.replace(VISION_DONE, [(spec.name, period, len(res.good), now_ms())])
            n_days += 1
            if n_days % 5 == 0:
                self.progress[spec.name] = f"{n_days} days (at {period})"
                self.status()
            day -= dt.timedelta(days=1)
        self.progress.setdefault(spec.name, "done")

    # ------------------------------------------------------------------ run
    def run(self) -> None:
        self.status("backfilling")
        try:
            self.term.connect()
            self.term.select_symbols([i.symbol for i in self.instruments])
        except Exception as exc:  # noqa: BLE001
            self.status("error", error=f"connect: {exc!r}")
            log.exception("MT5 backfill cannot connect")
            return
        hots = {}
        try:
            for inst in self.instruments:
                hot = SQLiteHotStore(inst.hot_db_path(self.data), cache_mb=self.s.resource.sqlite_cache_mb)
                hot.ensure_tables([*table_specs(inst), *system_specs()])
                hots[inst.key] = hot
            for step in (self.candles, self.ticks):
                for inst in self.instruments:
                    try:
                        step(inst, hots[inst.key])
                    except Exception as exc:  # noqa: BLE001
                        log.exception("mt5 backfill %s failed for %s", step.__name__, inst.key)
                        self.appdb.add_event(COLLECTOR, "error", f"{step.__name__} {inst.key}: {exc!r}"[:300])
            self.status("stopped")
            self.appdb.add_event(COLLECTOR, "done", iso(now_ms()))
        finally:
            for h in hots.values():
                h.close()
            self.term.shutdown()


def worker_main(data_dir: str | None, once: bool = False) -> None:
    s = load_settings()
    if data_dir:
        s = s.model_copy(update={"paths": PathsCfg(data_dir=data_dir, logs_dir=s.paths.logs_dir)})
    setup_from_settings("backfill-mt5", s)
    while True:
        MT5Backfill(s).run()
        if once:
            return
        now = dt.datetime.now(dt.timezone.utc)
        nxt = now.replace(hour=DAILY_RUN_UTC_HOUR, minute=0, second=0, microsecond=0)
        if nxt <= now:
            nxt += dt.timedelta(days=1)
        time.sleep((nxt - now).total_seconds())


def start_worker(data_dir: str | None) -> mp.Process:
    p = mp.get_context("spawn").Process(target=worker_main, args=(data_dir, False), name="backfill-mt5", daemon=True)
    p.start()
    return p
