"""Binance historical backfill worker (P3.2/P3.3) — separate OS process so CPU-heavy CSV parsing never stalls
the live WebSocket loop. Safe to run alongside live capture (idempotent writes; SQLite serialises writers;
cold day files are locked).

Order (most useful first): candles (all TFs) → funding → metrics → aggTrades newest→oldest.
Gaps the source cannot fill are recorded in ``known_gaps`` (never synthesised).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import multiprocessing as mp
from pathlib import Path

import numpy as np

from ...core.instruments import Instrument, InstrumentRegistry
from ...core.logsetup import setup_from_settings
from ...core.sessions import CALENDARS
from ...core.settings import PathsCfg, Settings, load_settings
from ...core.timeframes import Timeframe
from ...core.timeutil import MS_PER_DAY, iso, now_ms
from ...storage.gaps import KNOWN_GAPS, candle_gaps, id_gaps
from ...storage.parquet_store import ParquetColdStore, day_start_ms
from ...storage.sqlite_store import SQLiteHotStore
from ...storage.tablespec import spec_for, system_specs, table_specs
from ..common.appdb import AppDB
from . import fetch
from .markets import MARKETS
from .rest import BinanceRest
from .vision import DiskFullError, VisionClient, VisionIngestor, choose_kline_source

log = logging.getLogger("backfill")
COLLECTOR = "binance_backfill"
MAX_REST_BRIDGE = {"binance_spot": 2_000_000, "binance_usdm": 300_000}   # ids; spot weight 4/req, futures 20/req
DAILY_RUN_UTC_HOUR = 3        # Vision publishes the previous day's files overnight (lag ≈ 1 day, P1.4)


class BinanceBackfill:
    def __init__(self, s: Settings) -> None:
        self.s = s
        self.data = s.paths.data()
        self.appdb = AppDB(self.data / "app.db")
        self.cold = ParquetColdStore(self.data / "cold")
        self.vision = VisionClient(s.binance.vision, self.data / "vision_cache", min_free_gb=s.storage.min_free_disk_gb)
        reg = InstrumentRegistry.from_settings(s)
        self.instruments = [i for i in reg.all() if i.venue in MARKETS]
        b = s.binance
        # leave most of the weight budget to the live service
        # Binance IP limits: spot 6000/min, USDⓈ-M 2400/min — the live service keeps most of its own share
        self.rest = {"binance_spot": BinanceRest(b.spot_rest, weight_budget_per_min=2000),
                     "binance_usdm": BinanceRest(b.usdm_rest, weight_budget_per_min=1200)}
        self.progress: dict[str, str] = {}

    def status(self, state: str = "backfilling", error: str | None = None) -> None:
        self.progress["downloaded_mb"] = f"{self.vision.bytes_downloaded / 2**20:.0f}"
        self.appdb.set_status(COLLECTOR, state, error=error, detail=self.progress)

    # ------------------------------------------------------------------ candles
    async def candles(self, inst: Instrument, hot: SQLiteHotStore) -> None:
        m, rest = MARKETS[inst.venue], self.rest[inst.venue]
        start = inst.start_ms("candles")
        if start is None:
            return
        cal = CALENDARS["always_open"]
        ingestor = VisionIngestor(self.vision, hot, self.cold)
        for tf in inst.timeframes:
            spec = spec_for(inst, "candles", tf)
            end = tf.floor(rest.binance_now()) - tf.ms            # last closed bar open
            known = self._known_gap_starts(hot, spec.name)
            for attempt in range(2):
                have = hot.read_range(spec, max(start, 0), None, columns=["open_time"])["open_time"]
                first_listed = int(have.min()) if len(have) else None
                lo = max(start, self._listing_floor(inst, tf, first_listed))
                gaps = [g for g in candle_gaps(have, tf, lo, end, cal) if g.start not in known]
                if not gaps:
                    break
                self.progress[spec.name] = f"{sum(g.count for g in gaps)} bars missing in {len(gaps)} gaps"
                self.status()
                for g in gaps:
                    g_end = g.end + tf.ms
                    if choose_kline_source(tf, g.start, g_end, m.kline_limit) == "rest" or attempt == 1:
                        async for rows in fetch.klines(rest, m, inst.symbol, tf, g.start, g_end):
                            hot.upsert(spec, rows)
                    else:
                        await self._vision_klines(ingestor, inst, spec, tf, g.start, g_end)
            # anything still missing is confirmed absent at the source → record it
            have = hot.read_range(spec, max(start, 0), None, columns=["open_time"])["open_time"]
            if len(have):
                rest_gaps = candle_gaps(have, tf, int(have.min()), end, cal)
                rows = [(spec.name, g.start, g.end, "source_no_data", now_ms(),
                         f"{g.count} bars absent in Binance REST+Vision") for g in rest_gaps if g.start not in known]
                if rows:
                    hot.upsert(KNOWN_GAPS, rows)
                    log.info("%s: %d source gaps recorded (e.g. %s ×%d)", spec.name, len(rows), iso(rows[0][1]),
                             rest_gaps[0].count)
            self.progress[spec.name] = "done"
            self.status()

    def _listing_floor(self, inst: Instrument, tf: Timeframe, first_stored: int | None) -> int:
        # never ask for bars before the symbol existed: the earliest bar the exchange returns is the floor
        return self._first_bar.get((inst.key, tf.value), first_stored or 0)

    async def _vision_klines(self, ing: VisionIngestor, inst: Instrument, spec, tf: Timeframe, lo: int, hi: int) -> None:
        m = MARKETS[inst.venue]
        d0 = dt.datetime.fromtimestamp(lo / 1000, dt.timezone.utc).date()
        d1 = dt.datetime.fromtimestamp((hi - 1) / 1000, dt.timezone.utc).date() + dt.timedelta(days=1)
        files = await asyncio.to_thread(self.vision.plan, m, "klines", inst.symbol, tf.binance_interval, d0, d1)
        for f in files:
            n = await asyncio.to_thread(ing.ingest_klines, inst, m, spec, f)
            self.progress[spec.name] = f"vision {f.period}: {n} rows"
            self.status()

    def _known_gap_starts(self, hot: SQLiteHotStore, table: str) -> set[int]:
        g = hot.read_range(KNOWN_GAPS, columns=["table_name", "start"])
        return {int(s) for t, s in zip(g["table_name"], g["start"]) if t == table}

    async def discover_listing(self, inst: Instrument) -> None:
        m, rest = MARKETS[inst.venue], self.rest[inst.venue]
        for tf in inst.timeframes:
            data = await rest.get(m.rest_klines, {"symbol": inst.symbol, "interval": tf.binance_interval,
                                                  "startTime": 0, "limit": 1})
            if data:
                self._first_bar[(inst.key, tf.value)] = int(data[0][0])

    # ------------------------------------------------------------------ funding / metrics
    async def funding(self, inst: Instrument, hot: SQLiteHotStore) -> None:
        start = inst.start_ms("funding")
        if start is None or "funding" not in inst.datatypes:
            return
        spec = spec_for(inst, "funding")
        first = hot.first_time(spec)
        if first is not None and first <= start + 8 * 3_600_000:
            return
        async for rows in fetch.funding(self.rest[inst.venue], inst.symbol, start):
            hot.upsert(spec, rows)
        self.progress[spec.name] = "done"

    async def metrics(self, inst: Instrument, hot: SQLiteHotStore) -> None:
        start = inst.start_ms("metrics")
        if start is None or "metrics" not in inst.datatypes:
            return
        spec = spec_for(inst, "metrics")
        ing = VisionIngestor(self.vision, hot, self.cold)
        done = ing.done_periods(spec)
        d0 = dt.datetime.fromtimestamp(start / 1000, dt.timezone.utc).date()
        d1 = dt.date.today()
        files = [f for f in await asyncio.to_thread(self.vision.plan, MARKETS[inst.venue], "metrics", inst.symbol,
                                                    None, d0, d1) if f.period not in done]
        for i, f in enumerate(files):
            await asyncio.to_thread(ing.ingest_metrics, inst, spec, f)
            if i % 20 == 0:
                self.progress[spec.name] = f"{i + 1}/{len(files)} days"
                self.status()
        self.progress[spec.name] = "done"

    # ------------------------------------------------------------------ aggTrades
    async def agg_trades(self, inst: Instrument, hot: SQLiteHotStore) -> None:
        start = inst.start_ms("agg_trades")
        if start is None:
            return
        spec = spec_for(inst, "agg_trades")
        m = MARKETS[inst.venue]
        ing = VisionIngestor(self.vision, hot, self.cold)
        done = ing.done_periods(spec)
        d0 = max(dt.datetime.fromtimestamp(start / 1000, dt.timezone.utc).date(),
                 dt.datetime.fromtimestamp(self._first_bar.get((inst.key, "1d"), start) / 1000, dt.timezone.utc).date())
        d1 = dt.date.today()
        files = await asyncio.to_thread(self.vision.plan, m, "aggTrades", inst.symbol, None, d0, d1)
        todo = [f for f in files if f.period not in done and not self._covered(f, done)]
        todo.sort(key=lambda f: f.first_day, reverse=True)      # newest first
        for i, f in enumerate(todo):
            n = await asyncio.to_thread(ing.ingest_agg_trades, inst, m, spec, f)
            self.progress[spec.name] = f"{i + 1}/{len(todo)} files (last {f.period}: {n:,} rows)"
            self.status()
        await self._bridge_agg(inst, hot, spec)
        self.progress[spec.name] = "done"

    @staticmethod
    def _covered(f, done: set[str]) -> bool:
        # a daily file is covered when its month was ingested from the monthly file
        return (not f.monthly) and f.period[:7] in done

    async def _bridge_agg(self, inst: Instrument, hot: SQLiteHotStore, spec) -> None:
        """REST-fill every aggTrade id hole in the recent window (newest cold day + hot store).

        Holes appear between the last Vision day and live capture, or inside the hot store after an
        interrupted bridge/outage. Holes outside the REST reach or larger than the per-market cap are
        left for the next Vision daily file (next daily pass).
        """
        m, rest = MARKETS[inst.venue], self.rest[inst.venue]
        ids, times = [], []
        days = self.cold.days(inst, spec)
        if days:
            t = await asyncio.to_thread(self.cold.read_range, inst, spec, day_start_ms(days[-1]), None, ["agg_id", "ts"])
            ids.append(t["agg_id"].to_numpy())
            times.append(t["ts"].to_numpy())
        h = await asyncio.to_thread(hot.read_range, spec, None, None, ["agg_id", "ts"])
        ids.append(h["agg_id"])
        times.append(h["ts"])
        all_ids = np.concatenate(ids) if ids else np.empty(0, dtype=np.int64)
        all_ts = np.concatenate(times) if times else np.empty(0, dtype=np.int64)
        if len(all_ids) < 2:
            return
        order = np.argsort(all_ids)
        all_ids, all_ts = all_ids[order], all_ts[order]
        for g in id_gaps(all_ids):
            before_ts = int(all_ts[np.searchsorted(all_ids, g.start) - 1])
            if m.agg_rest_window_ms is not None and rest.binance_now() - before_ts > m.agg_rest_window_ms:
                log.warning("%s: aggTrades hole %d..%d (%s) outside the REST window — left for Vision",
                            inst.key, g.start, g.end, iso(before_ts))
                continue
            if g.count > MAX_REST_BRIDGE[inst.venue]:
                self.progress[spec.name] = f"hole of {g.count:,} ids left for the next Vision daily file"
                log.info("%s: %s", inst.key, self.progress[spec.name])
                continue
            n = 0
            async for rows in fetch.agg_trades_from_id(rest, m, inst.symbol, g.start, g.end + 1, max_requests=20_000):
                n += await asyncio.to_thread(hot.upsert, spec, rows)
                self.progress[spec.name] = f"bridging {g.start}..{g.end}: {n:,}/{g.count:,}"
                self.status()
            log.info("%s: bridged %d aggTrades (%d..%d)", inst.key, n, g.start, g.end)

    # ------------------------------------------------------------------ run
    async def run(self) -> None:
        self._first_bar: dict[tuple[str, str], int] = {}
        self.status("backfilling")
        for r, market in ((self.rest["binance_spot"], "/api/v3/time"), (self.rest["binance_usdm"], "/fapi/v1/time")):
            await r.sync_clock(market)
        hots = {}
        try:
            for inst in self.instruments:
                hot = SQLiteHotStore(inst.hot_db_path(self.data), cache_mb=self.s.resource.sqlite_cache_mb)
                hot.ensure_tables([*table_specs(inst), *system_specs()])
                hots[inst.key] = hot
                await self.discover_listing(inst)
            for step in (self.candles, self.funding, self.metrics, self.agg_trades):
                for inst in self.instruments:
                    try:
                        await step(inst, hots[inst.key])
                    except DiskFullError as exc:
                        self.status("error", error=str(exc))
                        log.error("%s", exc)
                        return
                    except Exception as exc:  # noqa: BLE001 — continue with the next instrument
                        log.exception("backfill step %s failed for %s", step.__name__, inst.key)
                        self.appdb.add_event(COLLECTOR, "error", f"{step.__name__} {inst.key}: {exc!r}"[:300])
            self.status("stopped")
            self.appdb.add_event(COLLECTOR, "done", iso(now_ms()))
        finally:
            for h in hots.values():
                h.close()
            for r in self.rest.values():
                await r.close()
            self.vision.close()


def _seconds_until_next_run() -> float:
    now = dt.datetime.now(dt.timezone.utc)
    nxt = now.replace(hour=DAILY_RUN_UTC_HOUR, minute=0, second=0, microsecond=0)
    if nxt <= now:
        nxt += dt.timedelta(days=1)
    return (nxt - now).total_seconds()


def worker_main(data_dir: str | None, once: bool = False) -> None:
    """Run the backfill now, then again every day after Vision publishes (catch-up + hole bridging)."""
    import time

    s = load_settings()
    if data_dir:
        s = s.model_copy(update={"paths": PathsCfg(data_dir=data_dir, logs_dir=s.paths.logs_dir)})
    setup_from_settings("backfill-binance", s)
    while True:
        asyncio.run(BinanceBackfill(s).run())
        if once:
            return
        wait = _seconds_until_next_run()
        log.info("backfill pass complete — next pass in %.1f h", wait / 3600)
        time.sleep(wait)


def start_worker(data_dir: str | None) -> mp.Process:
    p = mp.get_context("spawn").Process(target=worker_main, args=(data_dir, False), name="backfill-binance", daemon=True)
    p.start()
    return p
