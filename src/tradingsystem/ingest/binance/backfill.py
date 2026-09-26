"""Binance historical backfill worker (P3.2/P3.3) — separate OS process so CPU-heavy CSV parsing never stalls
the live WebSocket loop. Safe to run alongside live capture (idempotent writes; SQLite serialises writers;
cold day files are locked).

Order (most useful first): candles (all TFs) → funding → metrics → aggTrades newest→oldest.
Gaps the source cannot fill are recorded in ``known_gaps`` (never synthesised).

Failure handling (BF-01..03, F4): every (step, instrument), timeframe and Vision file is isolated; a pass returns
a :class:`PassResult` and the worker retries transient failures within minutes (5 → 60 min) instead of waiting
for the next daily slot. ``done`` is recorded only after a clean pass. When a unit fails on a network error and
neither Binance REST nor Vision answers, the pass is aborted at once (``NetworkDown``) instead of spending every
remaining unit's retry budget.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import multiprocessing as mp
import sqlite3

import httpx
import numpy as np

from ...core.instruments import Instrument, InstrumentRegistry
from ...core.logsetup import setup_from_settings
from ...core.sessions import CALENDARS
from ...core.settings import Settings, load_settings
from ...core.timeframes import Timeframe
from ...core.timeutil import iso, now_ms
from ...storage.gaps import KNOWN_GAPS, candle_gaps, id_gaps
from ...storage.parquet_store import ParquetColdStore, day_start_ms
from ...storage.sqlite_store import SQLiteHotStore
from ...storage.tablespec import TableSpec, spec_for, system_specs, table_specs
from ..common.appdb import AppDB
from ..common.backfill_loop import PassResult, StepIncomplete, finish_pass, worker_loop
from . import fetch
from .markets import MARKETS
from .rest import BinanceHTTPError, BinanceRest, RetriesExhausted
from .vision import DiskFullError, VisionClient, VisionIngestor, VisionTransientError, choose_kline_source

log = logging.getLogger("backfill")
COLLECTOR = "binance_backfill"
MAX_REST_BRIDGE = {"binance_spot": 2_000_000, "binance_usdm": 300_000}   # ids; spot weight 4/req, futures 20/req
DAILY_RUN_UTC_HOUR = 3        # Vision publishes the previous day's files overnight (lag ≈ 1 day, P1.4)
CSV_BYTES_PER_ROW = 70        # block size = resources.parse_chunk_rows × this
REST_RETRY_DEADLINE_S = 900   # backfill REST calls ride out a post-wake outage (live keeps its short retries)


class NetworkDown(RuntimeError):
    """A unit failed on a network error and nothing at Binance answers — abort the pass, retry it soon."""


def is_transient(exc: BaseException) -> bool:
    """Network / server / lock errors a later retry can fix (never request or data errors)."""
    if isinstance(exc, StepIncomplete):
        return any(is_transient(e) for e in exc.failures.values())
    if isinstance(exc, (httpx.TransportError, VisionTransientError, RetriesExhausted, NetworkDown, TimeoutError,
                        PermissionError)):
        return True
    if isinstance(exc, BinanceHTTPError):
        return exc.status >= 500 or exc.status in (408, 418, 429)
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500 or exc.response.status_code in (408, 429)
    if isinstance(exc, sqlite3.OperationalError):
        return "locked" in str(exc) or "busy" in str(exc)
    return False


def _utc_today() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()


class BinanceBackfill:
    def __init__(self, s: Settings) -> None:
        self.s = s
        self.data = s.paths.data()
        self.appdb = AppDB(s.paths.state() / "app.db")
        self.cold = ParquetColdStore(self.data / "cold")
        self.vision = VisionClient(s.binance.vision, self.data / "vision_cache", min_free_gb=s.storage.min_free_disk_gb)
        reg = InstrumentRegistry.from_settings(s)
        self.instruments = [i for i in reg.all() if i.venue in MARKETS]
        b = s.binance
        # leave most of the weight budget to the live service
        # Binance IP limits: spot 6000/min, USDⓈ-M 2400/min — the live service keeps most of its own share
        self.rest = {"binance_spot": BinanceRest(b.spot_rest, weight_budget_per_min=min(2000, b.rest_weight_budget_per_min * 2 // 3),
                                                 retry_deadline_s=REST_RETRY_DEADLINE_S),
                     "binance_usdm": BinanceRest(b.usdm_rest, weight_budget_per_min=min(1200, b.rest_weight_budget_per_min * 2 // 5),
                                                 retry_deadline_s=REST_RETRY_DEADLINE_S)}
        self.progress: dict[str, str] = {}
        self._first_bar: dict[tuple[str, str], int] = {}
        self.vision.beat = self._vision_beat

    def status(self, state: str = "backfilling", error: str | None = None) -> None:
        self.progress["downloaded_mb"] = f"{self.vision.bytes_downloaded / 2**20:.0f}"
        self.appdb.set_status(COLLECTOR, state, error=error, detail=self.progress)

    def _vision_beat(self, msg: str) -> None:
        """Heartbeat while one Vision download / retry wait runs for minutes (worker thread; AppDB is locked)."""
        self.progress["vision"] = msg[:120]
        self.status()

    async def _network_up(self) -> bool:
        """Any HTTP answer from Binance REST or Vision means the network works (a failed unit is then its own
        problem, not an outage)."""
        for url in (self.rest["binance_spot"].base + "/api/v3/ping", self.vision.base + "/"):
            try:
                await asyncio.to_thread(self.vision.http.head, url, timeout=10)
                return True
            except httpx.HTTPError:
                continue
        return False

    async def _unit_failed(self, what: str, exc: BaseException) -> None:
        """After one TF/file failed: a network outage aborts the pass instead of trying every remaining unit."""
        if is_transient(exc) and not isinstance(exc, NetworkDown) and not await self._network_up():
            raise NetworkDown(f"network down ({what}: {exc!r})"[:300]) from exc

    @staticmethod
    def _pk(inst: Instrument, spec: TableSpec) -> str:
        """Progress key — spot and USDⓈ-M share table names (separate DB files)."""
        return f"{inst.key}/{spec.name}"

    def _ingestor(self, hot: SQLiteHotStore) -> VisionIngestor:
        r = self.s.resource
        return VisionIngestor(self.vision, hot, self.cold, block_bytes=max(2**20, r.parse_chunk_rows * CSV_BYTES_PER_ROW),
                              use_threads=self.s.profile != "low")

    # ------------------------------------------------------------------ candles
    async def candles(self, inst: Instrument, hot: SQLiteHotStore) -> None:
        start = inst.start_ms("candles")
        if start is None:
            return
        ingestor = self._ingestor(hot)
        failed: dict[str, BaseException] = {}
        for tf in inst.timeframes:            # one TF's failure never skips the others (BF-02)
            spec = spec_for(inst, "candles", tf)
            try:
                await self._candles_tf(inst, hot, ingestor, spec, tf, start)
            except (DiskFullError, NetworkDown):
                raise
            except Exception as exc:  # noqa: BLE001
                log.exception("candles %s failed", self._pk(inst, spec))
                failed[tf.value] = exc
                self.progress[self._pk(inst, spec)] = f"failed: {exc!r}"[:120]
                await self._unit_failed(self._pk(inst, spec), exc)
        if failed:
            raise StepIncomplete(f"candles {inst.key}", failed)

    async def _candles_tf(self, inst: Instrument, hot: SQLiteHotStore, ingestor: VisionIngestor, spec: TableSpec,
                          tf: Timeframe, start: int) -> None:
        m, rest = MARKETS[inst.venue], self.rest[inst.venue]
        cal, key = CALENDARS["always_open"], self._pk(inst, spec)
        end = tf.floor(rest.binance_now()) - tf.ms            # last closed bar open
        known = self._known_gap_starts(hot, spec.name)
        for attempt in range(2):
            have = hot.read_range(spec, max(start, 0), None, columns=["open_time"])["open_time"]
            first_listed = int(have.min()) if len(have) else None
            lo = max(start, self._listing_floor(inst, tf, first_listed))
            gaps = [g for g in candle_gaps(have, tf, lo, end, cal) if g.start not in known]
            if not gaps:
                break
            self.progress[key] = f"{sum(g.count for g in gaps)} bars missing in {len(gaps)} gaps"
            self.status()
            for g in gaps:
                g_end = g.end + tf.ms
                if choose_kline_source(tf, g.start, g_end, m.kline_limit) == "rest" or attempt == 1:
                    async for rows in fetch.klines(rest, m, inst.symbol, tf, g.start, g_end):
                        hot.upsert(spec, rows)
                else:
                    await self._vision_klines(ingestor, inst, spec, tf, g.start, g_end)
        # every REST request of the last attempt succeeded: what is still missing is absent at the source
        have = hot.read_range(spec, max(start, 0), None, columns=["open_time"])["open_time"]
        if len(have):
            rest_gaps = candle_gaps(have, tf, int(have.min()), end, cal)
            rows = [(spec.name, g.start, g.end, "source_no_data", now_ms(),
                     f"{g.count} bars absent in Binance REST+Vision") for g in rest_gaps if g.start not in known]
            if rows:
                hot.upsert(KNOWN_GAPS, rows)
                log.info("%s: %d source gaps recorded (e.g. %s ×%d)", key, len(rows), iso(rows[0][1]),
                         rest_gaps[0].count)
        self.progress[key] = "done"
        self.status()

    def _listing_floor(self, inst: Instrument, tf: Timeframe, first_stored: int | None) -> int:
        # never ask for bars before the symbol existed: the earliest bar the exchange returns is the floor
        return self._first_bar.get((inst.key, tf.value), first_stored or 0)

    async def _vision_klines(self, ing: VisionIngestor, inst: Instrument, spec, tf: Timeframe, lo: int, hi: int) -> None:
        """Vision files for [lo, hi). A failed file is left to the REST attempt that follows (never fatal)."""
        m, key = MARKETS[inst.venue], self._pk(inst, spec)
        d0 = dt.datetime.fromtimestamp(lo / 1000, dt.timezone.utc).date()
        d1 = dt.datetime.fromtimestamp((hi - 1) / 1000, dt.timezone.utc).date() + dt.timedelta(days=1)
        try:
            files = await asyncio.to_thread(self.vision.plan, m, "klines", inst.symbol, tf.binance_interval, d0, d1)
        except DiskFullError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("%s: Vision listing failed (%r) — REST fallback", key, exc)
            return
        for f in files:
            try:
                n = await asyncio.to_thread(ing.ingest_klines, inst, m, spec, f)
            except DiskFullError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("%s: Vision %s failed (%r) — REST fallback", key, f.period, exc)
                continue
            self.progress[key] = f"vision {f.period}: {n} rows"
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
        self.progress[self._pk(inst, spec)] = "done"

    async def metrics(self, inst: Instrument, hot: SQLiteHotStore) -> None:
        start = inst.start_ms("metrics")
        if start is None or "metrics" not in inst.datatypes:
            return
        spec = spec_for(inst, "metrics")
        key = self._pk(inst, spec)
        ing = self._ingestor(hot)
        done = ing.done_periods(spec)
        d0 = dt.datetime.fromtimestamp(start / 1000, dt.timezone.utc).date()
        files = [f for f in await asyncio.to_thread(self.vision.plan, MARKETS[inst.venue], "metrics", inst.symbol,
                                                    None, d0, _utc_today(), done) if f.period not in done]
        failed: dict[str, BaseException] = {}
        for i, f in enumerate(files):
            try:
                await asyncio.to_thread(ing.ingest_metrics, inst, spec, f)
            except DiskFullError:
                raise
            except Exception as exc:  # noqa: BLE001 — next file (BF-02)
                log.warning("%s %s failed: %r", key, f.period, exc)
                failed[f.period] = exc
                await self._unit_failed(f"{key} {f.period}", exc)
            if i % 20 == 0:
                self.progress[key] = f"{i + 1}/{len(files)} days"
                self.status()
        if failed:
            self.progress[key] = f"{len(files) - len(failed)}/{len(files)} days, {len(failed)} failed"
            raise StepIncomplete(f"metrics {inst.key}", failed)
        self.progress[key] = "done"

    # ------------------------------------------------------------------ aggTrades
    async def agg_trades(self, inst: Instrument, hot: SQLiteHotStore) -> None:
        start = inst.start_ms("agg_trades")
        if start is None:
            return
        spec = spec_for(inst, "agg_trades")
        m, key = MARKETS[inst.venue], self._pk(inst, spec)
        ing = self._ingestor(hot)
        failed: dict[str, BaseException] = {}
        holes = 0
        try:
            done = ing.done_periods(spec)
            d0 = max(dt.datetime.fromtimestamp(start / 1000, dt.timezone.utc).date(),
                     dt.datetime.fromtimestamp(self._first_bar.get((inst.key, "1d"), start) / 1000,
                                               dt.timezone.utc).date())
            files = await asyncio.to_thread(self.vision.plan, m, "aggTrades", inst.symbol, None, d0, _utc_today(), done)
            todo = [f for f in files if f.period not in done and not self._covered(f, done)]
            todo.sort(key=lambda f: f.first_day, reverse=True)      # newest first
            for i, f in enumerate(todo):
                try:
                    n = await asyncio.to_thread(ing.ingest_agg_trades, inst, m, spec, f)
                except DiskFullError:
                    raise
                except Exception as exc:  # noqa: BLE001 — next file (BF-02)
                    log.warning("%s %s failed: %r", key, f.period, exc)
                    failed[f.period] = exc
                    await self._unit_failed(f"{key} {f.period}", exc)
                    continue
                self.progress[key] = f"{i + 1}/{len(todo)} files (last {f.period}: {n:,} rows)"
                self.status()
                if i == 0 and not f.monthly:     # bridge newest Vision day → live before the long backlog (BF-11)
                    await self._try_bridge(inst, hot, spec, failed)
        except (DiskFullError, NetworkDown):
            raise
        except Exception as exc:  # noqa: BLE001 — listing failed: still bridge what REST can reach
            log.warning("%s: Vision planning failed: %r", key, exc)
            failed["plan"] = exc
        holes = await self._try_bridge(inst, hot, spec, failed)
        if failed:
            self.progress[key] = f"{len(failed)} unit(s) failed: {', '.join(failed)}"[:120]
            raise StepIncomplete(f"agg_trades {inst.key}", failed)
        self.progress[key] = "done" if not holes else f"done ({holes} hole(s) left for Vision)"

    @staticmethod
    def _covered(f, done: set[str]) -> bool:
        # daily file: its month was ingested from the monthly file; monthly file: every day ingested (BF-10)
        if not f.monthly:
            return f.period[:7] in done
        return all(d in done for d in f.days())

    async def _try_bridge(self, inst: Instrument, hot: SQLiteHotStore, spec, failed: dict[str, BaseException]) -> int:
        """_bridge_agg that records its failure instead of masking the step's own error."""
        try:
            return await self._bridge_agg(inst, hot, spec)
        except DiskFullError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("%s: aggTrades bridge failed: %r", inst.key, exc)
            failed["bridge"] = exc
            return 0

    async def _bridge_agg(self, inst: Instrument, hot: SQLiteHotStore, spec) -> int:
        """REST-fill every aggTrade id hole in the recent window (newest cold day + hot store).

        Holes appear between the last Vision day and live capture, or inside the hot store after an
        interrupted bridge/outage. Holes outside the REST reach or larger than the per-market cap are
        left for the next Vision daily file (next daily pass). Returns the number of holes left.
        """
        m, rest = MARKETS[inst.venue], self.rest[inst.venue]
        key = self._pk(inst, spec)
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
            return 0
        order = np.argsort(all_ids)
        all_ids, all_ts = all_ids[order], all_ts[order]
        left = 0
        for g in id_gaps(all_ids):
            before_ts = int(all_ts[np.searchsorted(all_ids, g.start) - 1])
            if m.agg_rest_window_ms is not None and rest.binance_now() - before_ts > m.agg_rest_window_ms:
                log.warning("%s: aggTrades hole %d..%d (%s) outside the REST window — left for Vision",
                            inst.key, g.start, g.end, iso(before_ts))
                left += 1
                continue
            if g.count > MAX_REST_BRIDGE[inst.venue]:
                self.progress[key] = f"hole of {g.count:,} ids left for the next Vision daily file"
                log.info("%s: %s", inst.key, self.progress[key])
                left += 1
                continue
            n = 0
            async for rows in fetch.agg_trades_from_id(rest, m, inst.symbol, g.start, g.end + 1, max_requests=20_000):
                n += await asyncio.to_thread(hot.upsert, spec, rows)
                self.progress[key] = f"bridging {g.start}..{g.end}: {n:,}/{g.count:,}"
                self.status()
            log.info("%s: bridged %d aggTrades (%d..%d)", inst.key, n, g.start, g.end)
        return left

    # ------------------------------------------------------------------ run
    async def run(self) -> PassResult:
        res = PassResult(progress=self.progress)
        self._first_bar = {}
        hots: dict[str, SQLiteHotStore] = {}
        try:
            try:        # preparation: a network error here fails the pass (retried soon), never the worker (BF-03)
                self.status("backfilling")
                for r, path in ((self.rest["binance_spot"], "/api/v3/time"), (self.rest["binance_usdm"], "/fapi/v1/time")):
                    await r.sync_clock(path)
                for inst in self.instruments:
                    hot = SQLiteHotStore(inst.hot_db_path(self.data), cache_mb=self.s.resource.sqlite_cache_mb)
                    hots[inst.key] = hot
                    hot.ensure_tables([*table_specs(inst), *system_specs()])
                    await self.discover_listing(inst)
            except Exception as exc:  # noqa: BLE001
                log.exception("backfill preparation failed")
                self.appdb.add_event(COLLECTOR, "error", f"prepare: {exc!r}"[:300])
                res.add("prepare", exc, is_transient(exc))
                finish_pass(self.appdb, COLLECTOR, res)
                return res
            units = [(f"{st.__name__} {i.key}", st, i) for st in (self.candles, self.funding, self.metrics,
                                                                   self.agg_trades) for i in self.instruments]
            for unit, step, inst in units:
                try:
                    await step(inst, hots[inst.key])
                except DiskFullError as exc:
                    log.error("%s", exc)
                    res.disk_full = True
                    res.add(unit, exc, transient=False)
                    break
                except Exception as exc:  # noqa: BLE001 — continue with the next instrument
                    log.exception("backfill step %s failed for %s", step.__name__, inst.key)
                    self.appdb.add_event(COLLECTOR, "error", f"{unit}: {exc!r}"[:300])
                    res.add(unit, exc, is_transient(exc))
                    if not isinstance(exc, NetworkDown):
                        try:
                            await self._unit_failed(unit, exc)
                        except NetworkDown as down:
                            exc = down
                    if isinstance(exc, NetworkDown):
                        log.warning("network down — backfill pass aborted, retried soon")
                        res.add("network", exc, transient=True)
                        break
            self.progress.pop("vision", None)
            self.progress["downloaded_mb"] = f"{self.vision.bytes_downloaded / 2**20:.0f}"
            finish_pass(self.appdb, COLLECTOR, res)
            return res
        finally:
            for h in hots.values():
                h.close()
            for r in self.rest.values():
                await r.close()
            self.vision.close()
            self.appdb.close()


def worker_main(data_dir: str | None, once: bool = False) -> None:
    """Run the backfill now, then again every day after Vision publishes (catch-up + hole bridging);
    a pass with transient failures is retried within minutes."""
    s = load_settings()
    if data_dir:
        s = s.model_copy(update={"paths": s.paths.model_copy(update={"data_dir": data_dir})})   # keeps the instance
    setup_from_settings("backfill-binance", s)
    appdb = AppDB(s.paths.state() / "app.db")
    worker_loop(lambda: asyncio.run(BinanceBackfill(s).run()), collector=COLLECTOR, appdb=appdb,
                daily_hour=DAILY_RUN_UTC_HOUR, once=once, logger=log)


def start_worker(data_dir: str | None) -> mp.Process:
    p = mp.get_context("spawn").Process(target=worker_main, args=(data_dir, False), name="backfill-binance", daemon=True)
    p.start()
    return p
