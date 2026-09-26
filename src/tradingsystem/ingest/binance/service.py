"""Binance live ingestion service (P3.3–P3.8) — one asyncio process for all Binance instruments.

Order of operations (D-009 live-first): open stores → connect WebSockets → REST gap-fill from the last
stored row to now (overlap with live rows is harmless: idempotent upserts) → steady state.
After a reconnect, candles are re-fetched from the bar containing the outage start (or the bar after the newest stored
one, whichever is older) and the self-healing audit runs at once (F5).
Deep history (Vision) is handled by the separate backfill worker (``backfill.py``).
"""
from __future__ import annotations

import asyncio
import logging
import threading
from collections import defaultdict
from pathlib import Path

import httpx

from ...core.instruments import Instrument, InstrumentRegistry
from ...core.settings import Settings
from ...core.timeframes import Timeframe
from ...core.timeutil import MS_PER_DAY, MS_PER_MINUTE, iso, now_ms
from ...storage.parquet_store import ParquetColdStore
from ...storage.retention import rollover
from ...storage.sqlite_store import SQLiteHotStore
from ...storage.tablespec import FORMING, TableSpec, system_specs, table_specs
from ...storage.validators import validate_rows
from ..common.appdb import AppDB
from . import fetch, parsers
from .markets import DEPTH_BANDS_PCT, MARKETS
from .rest import BinanceHTTPError, BinanceRest, RetriesExhausted
from .ws import StreamConnection

log = logging.getLogger(__name__)

LIVE_GAPFILL_MAX_BARS = 20_000          # older holes are the backfill worker's job
LIVE_GAPFILL_MAX_AGG_MS = 2 * MS_PER_DAY


class InstrumentSink:
    """Buffers + hot writer for one instrument (this process is the file's live writer)."""

    def __init__(self, inst: Instrument, data_dir: Path, cache_mb: int) -> None:
        self.inst = inst
        self.hot = SQLiteHotStore(inst.hot_db_path(data_dir), cache_mb=cache_mb)
        self.specs: dict[tuple[str, Timeframe | None], TableSpec] = {(s.datatype, s.timeframe): s for s in table_specs(inst)}
        self.hot.ensure_tables([*self.specs.values(), *system_specs()])
        self._lock = threading.Lock()
        self._buf: dict[TableSpec, list[tuple]] = defaultdict(list)
        self.forming: dict[str, tuple] = {}
        self.first_live_agg_id: int | None = None     # first live id after (re)connect
        self.last_seen_agg_id: int | None = None      # newest id received live
        self.agg_before_outage: int | None = None     # last live id when the connection dropped
        self.agg_mark: tuple[int, int] | None = None  # (last stored id, its time) before live started
        self.last_px: tuple[str, str] | None = None
        self.quote: tuple[int, float, float] | None = None
        self.mark: tuple[int, tuple] | None = None
        self.liq_seq: dict[int, int] = {}
        self.rows_written = 0
        self.rows_rejected = 0

    def wants(self, datatype: str) -> bool:
        return datatype in self.inst.datatypes

    def spec(self, datatype: str, tf: Timeframe | None = None) -> TableSpec:
        return self.specs[(datatype, tf)]

    def add(self, datatype: str, row: tuple, tf: Timeframe | None = None) -> None:
        spec = self.specs.get((datatype, tf))
        if spec is not None:
            with self._lock:
                self._buf[spec].append(row)

    def add_many(self, spec: TableSpec, rows: list[tuple]) -> None:
        with self._lock:
            self._buf[spec].extend(rows)

    def flush(self) -> tuple[int, list[str]]:
        """Validate + write everything buffered in one transaction. Runs in a worker thread."""
        with self._lock:
            pending, self._buf = self._buf, defaultdict(list)
            forming = list(self.forming.values())
        batches, problems = [], []
        for spec, rows in pending.items():
            res = validate_rows(spec, rows, venue=self.inst.venue)
            if res.rejected:
                self.rows_rejected += res.n_rejected
                problems.append(f"{spec.name}: {res.n_rejected} rejected ({res.rejected[0][1]})")
            if res.good:
                batches.append((spec, res.good))
        n = 0
        if batches:
            n = self.hot.upsert_many(batches)["_total"]
            self.rows_written += n
        if forming:
            self.hot.replace(FORMING, forming)
        return n, problems


class BinanceLiveService:
    def __init__(self, settings: Settings, registry: InstrumentRegistry, appdb: AppDB) -> None:
        self.s = settings
        self.appdb = appdb
        self.data_dir = settings.paths.data()
        self.cold = ParquetColdStore(self.data_dir / "cold")
        self.instruments = [i for i in registry.all() if i.venue in MARKETS]
        cache = settings.resource.sqlite_cache_mb
        self.sinks = {i.key: InstrumentSink(i, self.data_dir, cache) for i in self.instruments}
        b = settings.binance
        self.rest = {
            "binance_spot": BinanceRest(b.spot_rest, weight_budget_per_min=b.rest_weight_budget_per_min),
            "binance_usdm": BinanceRest(b.usdm_rest, weight_budget_per_min=min(b.rest_weight_budget_per_min, 2000)),
        }
        self.conns: list[StreamConnection] = []
        self._by_stream: dict[tuple[str, str], InstrumentSink] = {}
        self._gapfill_locks = {v: asyncio.Lock() for v in MARKETS}
        self._marks: dict[tuple[str, str], int | None] = {}
        self._initial_done: set[str] = set()
        self._audit_kick = asyncio.Event()
        self._venue_ok: dict[str, bool] = {}
        self.stop = asyncio.Event()

    # ------------------------------------------------------------------ streams
    def _build_connections(self) -> None:
        groups: dict[tuple[str, str], list[str]] = defaultdict(list)
        for sink in self.sinks.values():
            inst, sym = sink.inst, sink.inst.symbol.lower()
            self._by_stream[(inst.venue, sym)] = sink
            market_streams, book_streams = [], []
            if sink.wants("candles"):
                market_streams += [f"{sym}@kline_{tf.binance_interval}" for tf in inst.timeframes]
            if sink.wants("agg_trades"):
                market_streams.append(f"{sym}@aggTrade")
            if sink.wants("mark_price"):
                market_streams.append(f"{sym}@markPrice@1s")
            if sink.wants("liquidations"):
                market_streams.append(f"{sym}@forceOrder")
            if sink.wants("book_ticker"):
                book_streams.append(f"{sym}@bookTicker")
            if inst.venue == "binance_spot":
                groups[("binance_spot", self.s.binance.spot_ws)] += market_streams + book_streams
            else:  # USDⓈ-M routing split (D-016)
                groups[("binance_usdm", self.s.binance.usdm_ws + "/market")] += market_streams
                groups[("binance_usdm", self.s.binance.usdm_ws + "/public")] += book_streams
        for (venue, base), streams in groups.items():
            if not streams:
                continue
            name = f"{venue}:{base.rsplit('/', 1)[-1] if venue == 'binance_usdm' else 'spot'}"
            self.conns.append(StreamConnection(
                name, base, streams, self._handler(venue), self._state_handler(venue, name),
                stall_timeout_s=30 if "public" in base or venue == "binance_spot" else 60))

    def _handler(self, venue: str):
        rest = self.rest[venue]

        def on_message(stream: str, d: dict, recv: int) -> None:
            sym, kind = stream.split("@", 1)
            sink = self._by_stream.get((venue, sym))
            if sink is None:
                return
            if kind.startswith("kline_"):
                k = d["k"]
                tf = Timeframe.parse(k["i"])
                sink.forming[tf.value] = (tf.value, int(k["t"]), float(k["o"]), float(k["h"]), float(k["l"]),
                                          float(k["c"]), float(k["v"]), recv)
                if k["x"]:
                    sink.add("candles", parsers.kline_ws(k), tf)
            elif kind == "aggTrade":
                row = parsers.agg_trade_ws(d)
                sink.add("agg_trades", row)
                if sink.first_live_agg_id is None:
                    sink.first_live_agg_id = row[0]
                if sink.last_seen_agg_id is None or row[0] > sink.last_seen_agg_id:
                    sink.last_seen_agg_id = row[0]
            elif kind == "bookTicker":
                px = (d["b"], d["a"])
                ts = int(d.get("E") or rest.binance_now())
                sink.quote = (ts, float(d["b"]), float(d["a"]))
                if px != sink.last_px:                      # store price changes only (conflation)
                    sink.last_px = px
                    sink.add("book_ticker", parsers.book_ticker(d, ts))
            elif kind.startswith("markPrice"):
                minute = int(d["E"]) // MS_PER_MINUTE * MS_PER_MINUTE
                if sink.mark is not None and minute != sink.mark[0]:
                    sink.add("mark_price", sink.mark[1])
                sink.mark = (minute, parsers.mark_price_ws(d, minute))
            elif kind == "forceOrder":
                o = d["o"]
                t = int(o["T"])
                seq = sink.liq_seq.get(t, 0)
                sink.liq_seq = {t: seq + 1}
                sink.add("liquidations", parsers.liquidation_ws(o, seq))
        return on_message

    def _state_handler(self, venue: str, name: str):
        async def on_state(event: str, detail: str, outage_ms: int | None) -> None:
            self.appdb.add_event(name, event, detail, outage_ms)
            if event == "disconnect":
                for sink in self.sinks.values():
                    if sink.inst.venue == venue:
                        sink.first_live_agg_id = None      # next live id marks the end of the hole
                        if sink.agg_before_outage is None:  # keep the id seen before the FIRST drop
                            sink.agg_before_outage = sink.last_seen_agg_id
            elif event == "connect":
                if outage_ms is None:
                    if venue in self._initial_done:
                        return                             # second connection of the same venue
                    self._initial_done.add(venue)
                    asyncio.create_task(self.gap_fill(venue, since_ms=None))
                else:
                    log.info("%s reconnected after %.1fs outage → gap-fill", name, outage_ms / 1000)
                    asyncio.create_task(self.gap_fill(venue, since_ms=now_ms() - outage_ms - 120_000))
        return on_state

    # ------------------------------------------------------------------ gap-fill
    async def _pre_live_marks(self) -> None:
        """Last stored time/id per table BEFORE live capture starts (initial gap-fill starts here)."""
        for sink in self.sinks.values():
            for spec in sink.specs.values():
                if spec.datatype in ("candles", "metrics", "funding"):
                    self._marks[(sink.inst.key, spec.name)] = await asyncio.to_thread(sink.hot.last_time, spec)
            if sink.wants("agg_trades"):
                spec = sink.spec("agg_trades")
                key = await asyncio.to_thread(sink.hot.last_key, spec)
                ts = await asyncio.to_thread(sink.hot.last_time, spec)
                if key is None:
                    ck = await asyncio.to_thread(self.cold.last_key, sink.inst, spec)
                    key, ts = ((ck[0],), ck[1]) if ck else (None, None)
                sink.agg_mark = (key[0], ts) if key else None

    def _start_for(self, sink: InstrumentSink, spec: TableSpec, since_ms: int | None, default_start: int) -> int:
        if since_ms is not None:                  # reconnect: from just before the outage
            return since_ms
        mark = self._marks.get((sink.inst.key, spec.name))
        if mark is None:
            return default_start
        step = spec.timeframe.ms if spec.timeframe else 1
        return mark + step

    async def gap_fill(self, venue: str, since_ms: int | None) -> None:
        async with self._gapfill_locks[venue]:
            await asyncio.sleep(3)          # let live rows start arriving → first live ids known
            rest, m = self.rest[venue], MARKETS[venue]
            now = rest.binance_now()
            for sink in self.sinks.values():
                inst = sink.inst
                if inst.venue != venue:
                    continue
                try:
                    if sink.wants("candles"):
                        for tf in inst.timeframes:
                            spec = sink.spec("candles", tf)
                            start = self._start_for(sink, spec, since_ms, now - 1000 * tf.ms)
                            if since_ms is not None:
                                # every bar that closed during the outage: from the bar containing its start (the
                                # paginator rounds a start up) or after the newest stored bar, if older (F5)
                                last = await asyncio.to_thread(sink.hot.last_time, spec)
                                start = tf.floor(min(since_ms, last + tf.ms) if last is not None else since_ms)
                            start = max(start, now - LIVE_GAPFILL_MAX_BARS * tf.ms)
                            async for rows in fetch.klines(rest, m, inst.symbol, tf, start, now):
                                sink.add_many(spec, rows)
                    if sink.wants("agg_trades"):
                        await self._gap_fill_agg(sink, rest, m, now, since_ms)
                    if sink.wants("metrics"):
                        spec = sink.spec("metrics")
                        start = max(self._start_for(sink, spec, since_ms, 0), now - 29 * MS_PER_DAY)
                        for off in range(start, now, 500 * 300_000):
                            sink.add_many(spec, await fetch.metrics_5m(rest, inst.symbol, off, min(now, off + 500 * 300_000)))
                    if sink.wants("funding"):
                        spec = sink.spec("funding")
                        start = self._start_for(sink, spec, since_ms, now - 30 * MS_PER_DAY)
                        async for rows in fetch.funding(rest, inst.symbol, start):
                            sink.add_many(spec, rows)
                except Exception as exc:  # noqa: BLE001
                    log.exception("gap-fill failed for %s", inst.key)
                    self.appdb.add_event(venue, "gapfill_error", f"{inst.key}: {exc!r}"[:300])
            self.appdb.add_event(venue, "gapfill_done", f"{'initial' if since_ms is None else 'after outage'} "
                                                         f"until {iso(now)}")
            if since_ms is not None:
                self._audit_kick.set()          # verify recent candles / aggTrade ids now, not in ≤ 5 min

    async def _gap_fill_agg(self, sink: InstrumentSink, rest: BinanceRest, m, now: int, since_ms: int | None) -> None:
        """Fill aggTrade ids between the last stored id (before live / before the outage) and the first live id."""
        spec = sink.spec("agg_trades")
        if since_ms is None:
            mark = sink.agg_mark
        else:
            before, sink.agg_before_outage = sink.agg_before_outage, None
            mark = (before, since_ms) if before is not None else None
        if mark is None:
            return  # no history yet → the backfill worker bridges history → live
        last_id, last_ts = mark
        if now - last_ts > LIVE_GAPFILL_MAX_AGG_MS:
            log.info("%s: aggTrades gap %.1f h exceeds live gap-fill window → left to backfill worker",
                     sink.inst.key, (now - last_ts) / 3_600_000)
            return
        until = sink.first_live_agg_id
        if until is not None and until <= last_id + 1:
            return
        async for rows in fetch.agg_trades_from_id(rest, m, sink.inst.symbol, last_id + 1, until):
            sink.add_many(spec, rows)

    # ------------------------------------------------------------------ self-healing audit
    async def _audit_loop(self) -> None:
        """Every 5 min: find id holes in recent aggTrades and missing recent candles, re-fetch them from REST.

        Catches anything a reconnect gap-fill missed (network flaps, killed process) — spec §9 no silent gaps.
        """
        from ...storage.gaps import candle_gaps, id_gaps
        try:
            await asyncio.wait_for(self._audit_kick.wait(), timeout=120)
        except asyncio.TimeoutError:
            pass
        while not self.stop.is_set():
            if self._audit_kick.is_set():
                self._audit_kick.clear()
                await asyncio.sleep(5)          # let the gap-fill rows reach the hot store first
            for sink in self.sinks.values():
                inst, rest, m = sink.inst, self.rest[sink.inst.venue], MARKETS[sink.inst.venue]
                now = rest.binance_now()
                try:
                    if sink.wants("agg_trades"):
                        spec = sink.spec("agg_trades")
                        c = await asyncio.to_thread(sink.hot.read_range, spec, now - 2 * 3_600_000, None, ["agg_id"])
                        for g in id_gaps(c["agg_id"])[:10]:
                            if g.count > 200_000:
                                continue
                            n = 0
                            async for rows in fetch.agg_trades_from_id(rest, m, inst.symbol, g.start, g.end + 1):
                                sink.add_many(spec, rows)
                                n += len(rows)
                            self.appdb.add_event(inst.venue, "audit_fill", f"{spec.name}: {n} ids {g.start}..{g.end}")
                    if sink.wants("candles"):
                        for tf in inst.timeframes:      # every TF: a missed 4h/1d close is as bad as a 1m one (F5)
                            spec = sink.spec("candles", tf)
                            lo = tf.floor(now - max(2 * 3_600_000, 3 * tf.ms))
                            hi = tf.floor(now) - tf.ms                      # last closed bar open
                            c = await asyncio.to_thread(sink.hot.read_range, spec, lo, None, ["open_time"])
                            for g in candle_gaps(c["open_time"], tf, lo, hi):
                                async for rows in fetch.klines(rest, m, inst.symbol, tf, g.start, g.end + tf.ms):
                                    sink.add_many(spec, rows)
                                self.appdb.add_event(inst.venue, "audit_fill", f"{spec.name}: {g.count} bars from {iso(g.start)}")
                except Exception as exc:  # noqa: BLE001
                    log.warning("audit failed for %s: %r", inst.key, exc)
            try:
                await asyncio.wait_for(self._audit_kick.wait(), timeout=300)
            except asyncio.TimeoutError:
                pass

    # ------------------------------------------------------------------ periodic jobs
    async def _flush_loop(self) -> None:
        while not self.stop.is_set():
            await asyncio.sleep(0.5)
            for sink in self.sinks.values():
                try:
                    n, problems = await asyncio.to_thread(sink.flush)
                    for p in problems:
                        log.warning("%s: %s", sink.inst.key, p)
                        self.appdb.add_event(sink.inst.venue, "rows_rejected", f"{sink.inst.key} {p}"[:300])
                    if sink.quote:
                        ts, bid, ask = sink.quote
                        await asyncio.to_thread(self.appdb.upsert_quote, sink.inst.key, ts, bid, ask, None, sink.inst.venue)
                except Exception as exc:  # noqa: BLE001
                    log.exception("flush failed for %s", sink.inst.key)
                    self.appdb.set_status(sink.inst.venue, "error", error=f"flush {sink.inst.key}: {exc!r}"[:300])

    async def _poll_loop(self) -> None:
        tick = 0
        while not self.stop.is_set():
            for sink in self.sinks.values():
                inst, rest, m = sink.inst, self.rest[sink.inst.venue], MARKETS[sink.inst.venue]
                try:
                    if sink.wants("depth") and tick % 60 == 0:
                        d = await rest.get(m.rest_depth, {"symbol": inst.symbol, "limit": m.depth_limit},
                                           weight=m.depth_weight)
                        ts = rest.binance_now()
                        sink.add_many(sink.spec("depth"), parsers.depth_bands(d["bids"], d["asks"], ts, DEPTH_BANDS_PCT))
                    if sink.wants("open_interest") and tick % 60 == 0:
                        d = await rest.get("/fapi/v1/openInterest", {"symbol": inst.symbol})
                        sink.add("open_interest", parsers.open_interest_rest(d, int(d["time"])))
                    if sink.wants("metrics") and tick % 300 == 30:
                        now = rest.binance_now()
                        sink.add_many(sink.spec("metrics"), await fetch.metrics_5m(rest, inst.symbol, now - 1_200_000, now))
                    if sink.wants("funding") and tick % 1800 == 45:
                        last = await asyncio.to_thread(sink.hot.last_time, sink.spec("funding"))
                        async for rows in fetch.funding(rest, inst.symbol, (last or rest.binance_now() - MS_PER_DAY) + 1):
                            sink.add_many(sink.spec("funding"), rows)
                except Exception as exc:  # noqa: BLE001
                    log.warning("poll failed for %s: %r", inst.key, exc)
            if tick % 600 == 0:
                for venue, rest in self.rest.items():
                    try:
                        await rest.sync_clock(MARKETS[venue].rest_time)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("clock sync failed (%s): %r", venue, exc)
            tick += 1
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=1)
            except asyncio.TimeoutError:
                pass

    async def _rollover_loop(self) -> None:
        await asyncio.sleep(60)
        while not self.stop.is_set():
            for sink in self.sinks.values():
                for dtype, days in self.s.storage.hot_days.items():
                    if not sink.wants(dtype):
                        continue
                    spec = sink.spec(dtype)
                    try:
                        if len(spec.key) == 1:
                            await asyncio.to_thread(rollover, sink.hot, self.cold, sink.inst, spec, hot_days=days,
                                                    now_ms=now_ms(), grace_hours=self.s.storage.rollover_grace_hours)
                        else:
                            await asyncio.to_thread(sink.hot.delete_before, spec, now_ms() - (days + 1) * MS_PER_DAY)
                    except Exception as exc:  # noqa: BLE001
                        log.exception("rollover failed for %s", spec.name)
                        self.appdb.add_event(sink.inst.venue, "rollover_error", f"{spec.name}: {exc!r}"[:300])
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=1800)
            except asyncio.TimeoutError:
                pass

    async def _status_loop(self) -> None:
        while not self.stop.is_set():
            for venue in MARKETS:
                conns = [c for c in self.conns if c.name.startswith(venue)]
                if not conns:
                    continue
                last = max((c.last_msg_ms or 0) for c in conns) or None
                ok = all(c.connected for c in conns) and last is not None and now_ms() - last < 60_000
                sinks = [s for s in self.sinks.values() if s.inst.venue == venue]
                recovered = ok and not self._venue_ok.get(venue, False)     # clear the old error once (BF-14)
                self._venue_ok[venue] = ok
                self.appdb.set_status(venue, "live" if ok else "reconnecting", last_data_ms=last, detail={
                    "messages": sum(c.messages for c in conns), "reconnects": sum(c.reconnects for c in conns),
                    "rows_written": sum(s.rows_written for s in sinks),
                    "rows_rejected": sum(s.rows_rejected for s in sinks),
                    "rest_requests": self.rest[venue].requests, "rest_errors": self.rest[venue].errors,
                    "clock_offset_ms": round(self.rest[venue].clock_offset_ms)}, clear_error=recovered)
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass

    # ------------------------------------------------------------------ lifecycle
    async def _sync_clock_until_ok(self, venue: str, rest: BinanceRest, first_delay_s: float = 5.0) -> None:
        """The network may be down at start-up (Wi-Fi reset, DNS): wait and retry with backoff, reporting
        'reconnecting', instead of exiting — the supervisor would restart the whole service every minute."""
        delay = first_delay_s
        while not self.stop.is_set():
            try:
                await rest.sync_clock(MARKETS[venue].rest_time)
                return
            except (httpx.TransportError, RetriesExhausted, BinanceHTTPError) as exc:
                self.appdb.set_status(venue, "reconnecting", error=f"clock sync at start-up: {exc!r}"[:200])
                log.warning("%s: clock sync failed at start-up (%r) — retry in %.0fs", venue, exc, delay)
                try:
                    await asyncio.wait_for(self.stop.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
                delay = min(delay * 2, 60.0)

    async def run(self) -> None:
        for venue in MARKETS:
            self.appdb.set_status(venue, "starting")
        for venue, rest in self.rest.items():
            await self._sync_clock_until_ok(venue, rest)
        if self.stop.is_set():
            return
        await self._pre_live_marks()
        self._build_connections()
        tasks = [asyncio.create_task(c.run(self.stop), name=c.name) for c in self.conns]
        tasks += [asyncio.create_task(f(), name=f.__name__) for f in
                  (self._flush_loop, self._poll_loop, self._rollover_loop, self._status_loop, self._audit_loop)]
        log.info("binance live service started: %d instruments, %d connections",
                 len(self.sinks), len(self.conns))
        try:
            await self.stop.wait()
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for sink in self.sinks.values():
                try:
                    sink.flush()
                finally:
                    sink.hot.close()
            for r in self.rest.values():
                await r.close()
            for venue in MARKETS:
                self.appdb.set_status(venue, "stopped")
