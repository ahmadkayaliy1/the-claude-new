"""MT5 live ingestion service (P4.4/P4.5) — a dedicated process (D-019).

Loop (every ``mt5.poll_interval_ms``, measured 50 ms, D-015):
  ticks: ``copy_ticks_from(cursor)`` → cursor de-dup → UTC rows (keys identical to the backfill's)
  rates: once per second per TF, ``copy_rates_from_pos(0, 3)`` → closed bars upserted, forming bar replaced; when
         bars closed since the newest stored one fell out of that window (outage, stall, session break) the poll
         fetches the range from that bar — a failed fetch is retried on the next poll (F6)
  flush: every 0.5 s, validated, one transaction per instrument
  status/heartbeat: every 2 s (``collector_status.updated_ms``) — the supervisor restarts a silent process
  audit: every 5 min, closed intraday bars missing from the recent window (session-aware grid) are re-fetched
Start-up: pre-live marks → gap-fill rates/ticks from the marks (short gaps; long ones → backfill worker).
Market-closed periods (session calendar) are reported as ``market_closed``, not as errors or gaps.
"""
from __future__ import annotations

import logging
import signal
import time
from collections import defaultdict

import numpy as np

from ...core.instruments import Instrument, InstrumentRegistry
from ...core.logsetup import setup_from_settings
from ...core.sessions import calendar_for
from ...core.settings import PathsCfg, Settings, load_settings
from ...core.timeframes import Timeframe
from ...core.timeutil import MS_PER_DAY, iso, now_ms
from ...storage.parquet_store import ParquetColdStore
from ...storage.retention import rollover
from ...storage.sqlite_store import SQLiteHotStore
from ...storage.tablespec import FORMING, TableSpec, spec_for, system_specs, table_specs
from ...storage.validators import validate_rows
from ..common.appdb import AppDB
from .convert import TickCursor, mt5_candle_gaps, rates_to_rows, ticks_to_rows
from .servertime import MonotonicServerClock, ServerTimeModel
from .terminal import AccountMismatch, MT5Terminal

log = logging.getLogger("ingest-mt5")
COLLECTOR = "mt5"
LIVE_GAPFILL_MAX_MS = 3 * MS_PER_DAY
AUDIT_EVERY_S = 300
AUDIT_WINDOW_MS = 2 * 3_600_000          # at least; 3 bars for H4
RATES_FAIL_EVENT_AFTER = 30              # consecutive failed rate polls (≈ 30 s) while open → one event


class MT5Sink:
    def __init__(self, inst: Instrument, s: Settings, model: ServerTimeModel) -> None:
        self.inst = inst
        self.hot = SQLiteHotStore(inst.hot_db_path(s.paths.data()), cache_mb=s.resource.sqlite_cache_mb)
        self.hot.ensure_tables([*table_specs(inst), *system_specs()])
        self.cal = calendar_for(inst.venue, inst.symbol, s.pairs[inst.pair].asset_class)
        self.clock = MonotonicServerClock(model)
        self.cursor: TickCursor | None = None
        self.buf: dict[TableSpec, list[tuple]] = defaultdict(list)
        self.forming: dict[str, tuple] = {}
        self.last_closed: dict[str, int] = {}      # TF → srv_time of the newest closed bar stored/buffered
        self.rates_fail: dict[str, int] = defaultdict(int)
        self.last_tick_srv: int | None = None
        self.quote: tuple[int, float, float] | None = None
        self.rows_written = self.rows_rejected = 0

    def flush(self) -> list[str]:
        pending, self.buf = self.buf, defaultdict(list)
        batches, problems = [], []
        for spec, rows in pending.items():
            res = validate_rows(spec, rows, venue="mt5")
            if res.rejected:
                self.rows_rejected += res.n_rejected
                problems.append(f"{spec.name}: {res.n_rejected} rejected ({res.rejected[0][1]})")
            if res.good:
                batches.append((spec, res.good))
        if batches:
            self.rows_written += self.hot.upsert_many(batches)["_total"]
        if self.forming:
            self.hot.replace(FORMING, list(self.forming.values()))
        return problems


class MT5LiveService:
    def __init__(self, s: Settings) -> None:
        self.s = s
        self.model = ServerTimeModel()
        self.appdb = AppDB(s.paths.data() / "app.db")
        self.cold = ParquetColdStore(s.paths.data() / "cold")
        reg = InstrumentRegistry.from_settings(s)
        self.instruments = [i for i in reg.all() if i.venue == "mt5"]
        self.term = MT5Terminal(s.mt5_data_profile())
        self.sinks: dict[str, MT5Sink] = {}
        self.stop = False
        self.errors = 0
        self.reconnects = 0
        self._clear_error = False            # first status after a (re)connect clears the old error (BF-14)

    # ------------------------------------------------------------------ helpers
    def _tfc(self, tf: Timeframe) -> int:
        return self.term.timeframe(tf.mt5_attr)

    def _init_cursor(self, sink: MT5Sink) -> None:
        """Resume the tick cursor from the last stored tick (hot, else cold), else from the newest tick."""
        spec = spec_for(sink.inst, "ticks")
        last = sink.hot.read_last(spec, 1, columns=["srv_msc"])["srv_msc"]
        srv = int(last[0]) if len(last) else None
        if srv is None:
            ck = self.cold.last_key(sink.inst, spec)
            if ck is not None:
                srv = self.model.utc_to_server(ck[1])
        now_srv = int(self.term.mt5.symbol_info_tick(sink.inst.symbol).time_msc)
        if srv is None or now_srv - srv > LIVE_GAPFILL_MAX_MS:
            srv = now_srv - 60_000            # long holes are the backfill worker's job
            seen = 0
        else:
            # ticks already stored at that exact server ms (so the key sequence continues correctly)
            at = sink.hot.read_range(spec, None, None, ["srv_msc"])["srv_msc"]
            seen = int((at == srv).sum()) if len(at) else 0
        sink.cursor = TickCursor(sink.inst.symbol, srv, seen)

    def _gapfill_rates(self, sink: MT5Sink) -> None:
        """Bars closed while disconnected. Seeds ``last_closed`` so a failed fetch here is retried by the poll."""
        mt5 = self.term.mt5
        now_srv = int(mt5.symbol_info_tick(sink.inst.symbol).time_msc)
        for tf in sink.inst.timeframes:
            spec = spec_for(sink.inst, "candles", tf)
            last = sink.hot.read_last(spec, 1, columns=["srv_time"])["srv_time"]
            if len(last):
                sink.last_closed[tf.value] = max(sink.last_closed.get(tf.value, 0), int(last[0]))
            lo = int(last[0]) if len(last) else now_srv - 1000 * tf.ms
            lo = max(lo, now_srv - LIVE_GAPFILL_MAX_MS - tf.ms)
            r = mt5.copy_rates_range(sink.inst.symbol, self._tfc(tf), lo // 1000, now_srv // 1000 + 1)
            if r is not None and len(r) > 1:
                rows = rates_to_rows(r[:-1], self.model)                      # last bar is still forming
                sink.buf[spec].extend(rows)
                sink.last_closed[tf.value] = max(sink.last_closed.get(tf.value, 0), rows[-1][1])
            elif r is None:
                log.warning("%s %s: rates gap-fill failed (%s) — retried by the poll", sink.inst.key, tf.value,
                            mt5.last_error())

    def _poll_ticks(self, sink: MT5Sink) -> None:
        mt5, cur = self.term.mt5, sink.cursor
        ticks = mt5.copy_ticks_from(sink.inst.symbol, cur.last_srv_msc // 1000, 100_000, mt5.COPY_TICKS_ALL)
        if ticks is None:
            raise ConnectionError(f"copy_ticks_from({sink.inst.symbol}) failed: {mt5.last_error()}")
        new = cur.take_new(ticks)
        if not len(new):
            return
        prev_ms, seq_start = cur.advance(new)
        cont = int(new[0]["time_msc"]) == prev_ms
        rows = ticks_to_rows(new, sink.clock, seq_start=seq_start if cont else 0,
                             prev_srv_msc=prev_ms if cont else None)
        sink.buf[spec_for(sink.inst, "ticks")].extend(rows)
        last = rows[-1]
        sink.last_tick_srv = last[2]
        sink.quote = (last[1], last[3], last[4])

    def _poll_rates(self, sink: MT5Sink) -> None:
        mt5, sym = self.term.mt5, sink.inst.symbol
        for tf in sink.inst.timeframes:
            tfc = self._tfc(tf)
            r = mt5.copy_rates_from_pos(sym, tfc, 0, 3)
            if r is None or len(r) == 0:
                self._rates_failed(sink, tf, "copy_rates_from_pos")
                continue
            last = sink.last_closed.get(tf.value)
            caught_up = True
            if last is not None and len(r) > 1 and int(r[0]["time"]) * 1000 > last + tf.ms:
                # bars closed after the newest stored one are outside the 3-bar window (outage, terminal re-sync,
                # session break): fetch from that bar; on failure keep the mark → retried next poll (F6)
                hi = int(r[-1]["time"])
                rr = mt5.copy_rates_range(sym, tfc, max(last, hi * 1000 - LIVE_GAPFILL_MAX_MS) // 1000, hi)
                if rr is not None and len(rr) and int(rr[-1]["time"]) == hi:
                    r = rr
                else:
                    caught_up = False
                    self._rates_failed(sink, tf, "copy_rates_range")
            rows = rates_to_rows(r, self.model)
            closed = rows[:-1]
            sink.buf[spec_for(sink.inst, "candles", tf)].extend(closed)
            if caught_up:
                sink.rates_fail[tf.value] = 0
                if closed:
                    sink.last_closed[tf.value] = max(last or 0, closed[-1][1])
            f = rows[-1]
            sink.forming[tf.value] = (tf.value, f[0], f[2], f[3], f[4], f[5], float(f[6]), now_ms())

    def _rates_failed(self, sink: MT5Sink, tf: Timeframe, what: str) -> None:
        n = sink.rates_fail[tf.value] = sink.rates_fail[tf.value] + 1
        if n == RATES_FAIL_EVENT_AFTER and sink.cal.is_open(now_ms()):
            err = self.term.mt5.last_error()
            log.warning("%s %s: %s failing for %d polls (%s)", sink.inst.key, tf.value, what, n, err)
            self.appdb.add_event(COLLECTOR, "rates_poll_failed", f"{sink.inst.key} {tf.value} {what}: {err}"[:300])

    def _audit_rates(self) -> None:
        """Re-fetch closed intraday bars missing from the recent window (session-aware server grid) — heals holes
        left when the terminal's history lagged after a reconnect (F6). Bars the broker lacks are just re-asked."""
        mt5 = self.term.mt5
        for sink in self.sinks.values():
            now_srv = sink.last_tick_srv
            if now_srv is None or not sink.cal.is_open(now_ms()):
                continue
            for tf in sink.inst.timeframes:
                if tf.ms >= MS_PER_DAY:
                    continue
                spec = spec_for(sink.inst, "candles", tf)
                end = (now_srv // tf.ms) * tf.ms - tf.ms                     # last closed bar (server grid)
                lo = end - max(AUDIT_WINDOW_MS, 3 * tf.ms)
                have = sink.hot.read_last(spec, (end - lo) // tf.ms + 5, columns=["srv_time"])["srv_time"]
                for g in mt5_candle_gaps(have, tf, lo, end, sink.cal, self.model)[:10]:
                    r = mt5.copy_rates_range(sink.inst.symbol, self._tfc(tf), g.start // 1000, g.end // 1000)
                    if r is None or not len(r):
                        continue
                    t = r["time"].astype(np.int64) * 1000
                    rows = rates_to_rows(r[(t >= g.start) & (t <= g.end)], self.model)
                    if rows:
                        sink.buf[spec].extend(rows)
                        self.appdb.add_event(COLLECTOR, "audit_fill", f"{spec.name}: {len(rows)} bars from "
                                                                      f"srv {iso(g.start)}"[:300])

    # ------------------------------------------------------------------ lifecycle
    def connect(self) -> None:
        acc = self.term.connect()
        self.term.select_symbols([i.symbol for i in self.instruments])
        log.info("MT5 connected: %s (%s), leverage 1:%d", acc.server, "demo" if acc.trade_mode == 0 else "real",
                 acc.leverage)
        for inst in self.instruments:
            if inst.key not in self.sinks:
                self.sinks[inst.key] = MT5Sink(inst, self.s, self.model)
            sink = self.sinks[inst.key]
            self._init_cursor(sink)
            self._gapfill_rates(sink)
        self.appdb.add_event(COLLECTOR, "connect", acc.server)
        self._clear_error = True

    def run(self) -> None:
        interval = self.s.mt5.poll_interval_ms / 1000
        self.appdb.set_status(COLLECTOR, "starting")
        backoff = 2.0
        down_since: int | None = None
        while not self.stop:
            try:
                self.connect()
                if down_since is not None:
                    self.appdb.add_event(COLLECTOR, "resumed", None, now_ms() - down_since)
                    down_since = None
                backoff = 2.0
                self._loop(interval)
            except AccountMismatch as exc:
                log.error("%s — refusing to ingest", exc)
                self.appdb.set_status(COLLECTOR, "error", error=str(exc))
                self._sleep(60)
            except Exception as exc:  # noqa: BLE001 — terminal gone / IPC error → reconnect
                self.errors += 1
                self.reconnects += 1
                down_since = down_since or now_ms()
                log.warning("MT5 loop error: %r — reconnect in %.0fs", exc, backoff)
                self.appdb.add_event(COLLECTOR, "disconnect", repr(exc)[:300])
                self.appdb.set_status(COLLECTOR, "reconnecting", error=repr(exc)[:300])
                self.term.shutdown()
                self._sleep(backoff)
                backoff = min(backoff * 2, 60)
        for sink in self.sinks.values():
            try:
                sink.flush()
            finally:
                sink.hot.close()
        self.term.shutdown()
        self.appdb.set_status(COLLECTOR, "stopped")

    def _sleep(self, s: float) -> None:
        end = time.time() + s
        while not self.stop and time.time() < end:
            time.sleep(0.2)

    def _loop(self, interval: float) -> None:
        last_rates = last_flush = last_status = 0.0
        last_rollover = time.time()
        last_audit = time.time() - AUDIT_EVERY_S + 30        # first audit shortly after every (re)connect
        while not self.stop:
            t0 = time.time()
            for sink in self.sinks.values():
                self._poll_ticks(sink)
            if t0 - last_rates >= 1.0:
                for sink in self.sinks.values():
                    self._poll_rates(sink)
                last_rates = t0
            if t0 - last_flush >= 0.5:
                for sink in self.sinks.values():
                    for p in sink.flush():
                        log.warning("%s: %s", sink.inst.key, p)
                        self.appdb.add_event(COLLECTOR, "rows_rejected", f"{sink.inst.key} {p}"[:300])
                    if sink.quote:
                        ts, bid, ask = sink.quote
                        self.appdb.upsert_quote(sink.inst.key, ts, bid, ask, None, "mt5")
                last_flush = t0
            if t0 - last_status >= 2.0:
                self._status()
                last_status = t0
            if t0 - last_rollover >= 1800:
                self._rollover()
                last_rollover = t0
            if t0 - last_audit >= AUDIT_EVERY_S:
                try:
                    self._audit_rates()
                except Exception:  # noqa: BLE001 — a failed audit must not look like a lost terminal
                    log.exception("rates audit failed")
                last_audit = t0
            spent = time.time() - t0
            if spent < interval:
                time.sleep(interval - spent)

    def _status(self) -> None:
        now = now_ms()
        open_any = any(s.cal.is_open(now) for s in self.sinks.values())
        last = max((s.quote[0] for s in self.sinks.values() if s.quote), default=None)
        state = "live" if open_any else "market_closed"
        detail = {"rows_written": sum(s.rows_written for s in self.sinks.values()),
                  "rows_rejected": sum(s.rows_rejected for s in self.sinks.values()),
                  "reconnects": self.reconnects,
                  "symbols": {s.inst.symbol: {"open": s.cal.is_open(now), "last_tick": iso(s.quote[0]) if s.quote else None}
                              for s in self.sinks.values()}}
        self.appdb.set_status(COLLECTOR, state, last_data_ms=last, detail=detail, clear_error=self._clear_error)
        self._clear_error = False

    def _rollover(self) -> None:
        for sink in self.sinks.values():
            spec = spec_for(sink.inst, "ticks")
            try:
                rollover(sink.hot, self.cold, sink.inst, spec, hot_days=self.s.storage.hot_days.get("ticks", 3),
                         now_ms=now_ms(), grace_hours=self.s.storage.rollover_grace_hours)
            except Exception as exc:  # noqa: BLE001
                log.exception("tick rollover failed for %s", sink.inst.key)
                self.appdb.add_event(COLLECTOR, "rollover_error", f"{sink.inst.key}: {exc!r}"[:300])


def main(data_dir: str | None = None, backfill: bool = True) -> None:
    s = load_settings()
    if data_dir:
        s = s.model_copy(update={"paths": PathsCfg(data_dir=data_dir, logs_dir=s.paths.logs_dir)})
    setup_from_settings("ingest-mt5", s)
    svc = MT5LiveService(s)

    def _stop(*_: object) -> None:
        svc.stop = True

    for sig in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGBREAK", signal.SIGTERM)):
        try:
            signal.signal(sig, _stop)
        except (ValueError, OSError):
            pass
    keeper = keeper_done = None
    if backfill:                  # restarted with backoff if it ever dies (BF-03)
        from ..common.backfill_loop import WorkerKeeper
        from .backfill import COLLECTOR as BACKFILL_COLLECTOR, start_worker
        keeper = WorkerKeeper(lambda: start_worker(data_dir), svc.appdb, BACKFILL_COLLECTOR)
        keeper_done = keeper.monitor()
    try:
        svc.run()
    except KeyboardInterrupt:
        svc.stop = True
    finally:
        if keeper is not None:
            keeper_done.set()
            keeper.stop()
