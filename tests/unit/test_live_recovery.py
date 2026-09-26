"""Live ingestion after an outage (stall F5 ingest side, F6, BF-14).

* Binance: the outage is measured from the last message, and the reconnect gap-fill asks for every bar that closed
  during it (the paginator rounds a start up, so the start must be the bar containing the outage start).
* MT5: bars that fell out of the 3-bar poll window are fetched from the newest stored bar, a failed fetch is retried
  on the next poll, and the periodic audit re-fetches holes. Terminal answers are real fixture bars.
"""
import asyncio
import dataclasses
import datetime as dt
import types
from collections import defaultdict
from pathlib import Path

import numpy as np
import orjson
import pytest

from tradingsystem.core.instruments import InstrumentRegistry
from tradingsystem.core.sessions import CALENDARS
from tradingsystem.core.settings import load_settings
from tradingsystem.core.timeframes import Timeframe
from tradingsystem.ingest.binance import ws as ws_mod
from tradingsystem.ingest.binance.markets import MARKETS
from tradingsystem.ingest.binance.service import BinanceLiveService, InstrumentSink
from tradingsystem.ingest.common.appdb import AppDB
from tradingsystem.ingest.mt5.convert import rates_to_rows
from tradingsystem.ingest.mt5.servertime import ServerTimeModel
from tradingsystem.ingest.mt5.service import MT5LiveService
from tradingsystem.storage.sqlite_store import SQLiteHotStore
from tradingsystem.storage.tablespec import spec_for, system_specs

UTC = dt.timezone.utc
RATE_DTYPE = [("time", "<i8"), ("open", "<f8"), ("high", "<f8"), ("low", "<f8"), ("close", "<f8"),
              ("tick_volume", "<u8"), ("spread", "<i4"), ("real_volume", "<u8")]


def ms(*a) -> int:
    return int(dt.datetime(*a, tzinfo=UTC).timestamp() * 1000)


@pytest.fixture(scope="module")
def reg():
    return InstrumentRegistry.from_settings(load_settings(env_path=Path("nope.env")))


# ---------------------------------------------------------------------------------------------------- Binance
class FakeWS:
    def __init__(self, script, clock) -> None:
        self.script, self.clock = script, clock

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def recv(self):
        t, item = self.script.pop(0)
        self.clock["t"] = t
        if isinstance(item, Exception):
            raise item
        return item


def test_outage_is_measured_from_the_last_message(monkeypatch):
    clock = {"t": 0}
    conns = [[(1_000, orjson.dumps({"stream": "btcusdt@aggTrade", "data": {}})),
              (50_000, ConnectionError("stalled: no message for 30s"))], []]
    seen = []

    def connect(url, **kw):
        if len(conns) == 1:
            clock["t"] = 60_000                                               # back 10 s after the stall fired
        return FakeWS(conns.pop(0), clock)

    monkeypatch.setattr(ws_mod.websockets, "connect", connect)
    monkeypatch.setattr(ws_mod, "now_ms", lambda: clock["t"])
    stop = asyncio.Event()

    async def on_state(event, detail, outage):
        seen.append((event, outage))
        if event == "connect" and outage is not None:
            stop.set()

    conn = ws_mod.StreamConnection("t", "wss://x", ["btcusdt@aggTrade"], lambda *a: None, on_state)
    asyncio.run(asyncio.wait_for(conn.run(stop), timeout=10))
    assert seen == [("connect", None), ("disconnect", None), ("connect", 59_000)]   # not 10 s


class FakeRest:
    def __init__(self, now: int) -> None:
        self.now, self.requests = now, []

    def binance_now(self) -> int:
        return self.now

    async def get(self, path, params=None, *, weight=1):
        self.requests.append((path, dict(params or {})))
        return []


def test_reconnect_gap_fill_starts_at_the_bar_containing_the_outage(tmp_path, reg, monkeypatch):
    real_sleep = asyncio.sleep

    async def fast_sleep(s, *a, **k):
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)
    inst = next(i for i in reg.all() if i.venue == "binance_usdm" and i.symbol == "XAUUSDT")
    sink = InstrumentSink(inst, tmp_path, 4)
    last_4h, last_1m = ms(2026, 9, 26, 4), ms(2026, 9, 26, 11, 26)
    for tf, t in ((Timeframe.H4, last_4h), (Timeframe.M1, last_1m)):
        sink.hot.upsert(sink.spec("candles", tf), [(t, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1, 0.5, 0.5)])
    svc = object.__new__(BinanceLiveService)
    svc.sinks, svc.appdb = {inst.key: sink}, AppDB(tmp_path / "app.db")
    svc.rest = {"binance_usdm": FakeRest(ms(2026, 9, 26, 12, 16))}
    svc._gapfill_locks, svc._marks = {"binance_usdm": asyncio.Lock()}, {}
    since = ms(2026, 9, 26, 11, 27, 30)                                        # last message − 120 s

    async def go():
        svc._audit_kick = asyncio.Event()
        await svc.gap_fill("binance_usdm", since_ms=since)
        return svc._audit_kick.is_set()

    assert asyncio.run(go())                                                  # the audit is kicked at once
    starts = {}
    for path, p in svc.rest["binance_usdm"].requests:
        if path == MARKETS["binance_usdm"].rest_klines:
            starts.setdefault(p["interval"], p["startTime"])
    assert starts["4h"] == ms(2026, 9, 26, 8)          # the 08:00 bar closed at 12:00, inside the outage
    assert starts["1m"] == ms(2026, 9, 26, 11, 27)     # right after the newest stored bar
    assert starts["1h"] == ms(2026, 9, 26, 11)
    assert starts["1d"] == ms(2026, 9, 26)
    sink.hot.close()


# ---------------------------------------------------------------------------------------------------- MT5
class FakeMT5:
    def __init__(self, bars) -> None:
        self.bars, self.range_fail, self.range_calls = bars, 0, []
        for tf in Timeframe:
            setattr(self, tf.mt5_attr, tf.ms)

    def copy_rates_from_pos(self, sym, tfc, pos, n):
        return self.bars[-n:]

    def copy_rates_range(self, sym, tfc, lo_s, hi_s):
        self.range_calls.append((lo_s, hi_s))
        if self.range_fail:
            self.range_fail -= 1
            return None
        return self.bars[(self.bars["time"] >= lo_s) & (self.bars["time"] <= hi_s)]

    def symbol_info_tick(self, sym):
        return types.SimpleNamespace(time_msc=int(self.bars["time"][-1]) * 1000 + 30_000)

    def last_error(self):
        return (-1, "Terminal: Call failed")


@pytest.fixture()
def mt5svc(tmp_path, reg, real_candles_1m):
    model = ServerTimeModel()
    n = 60
    bars = np.zeros(n, dtype=RATE_DTYPE)
    bars["time"] = [model.utc_to_server(int(t)) // 1000 for t in real_candles_1m["open_time"].to_numpy()[:n]]
    for c in ("open", "high", "low", "close"):
        bars[c] = real_candles_1m[c].to_numpy()[:n]
    bars["tick_volume"] = real_candles_1m["trades"].to_numpy()[:n]
    inst = dataclasses.replace(next(i for i in reg.all() if i.venue == "mt5" and i.symbol.startswith("BTCUSD")),
                               timeframes=(Timeframe.M1,))
    hot = SQLiteHotStore(tmp_path / "btcusd.db")
    spec = spec_for(inst, "candles", Timeframe.M1)
    hot.ensure_tables([spec, *system_specs()])
    fake = FakeMT5(bars)
    svc = object.__new__(MT5LiveService)
    svc.model, svc.appdb = model, AppDB(tmp_path / "app.db")
    svc.term = types.SimpleNamespace(mt5=fake, timeframe=lambda attr: getattr(fake, attr))
    sink = types.SimpleNamespace(inst=inst, hot=hot, buf=defaultdict(list), forming={}, last_closed={},
                                 rates_fail=defaultdict(int), cal=CALENDARS["always_open"], quote=None,
                                 rows_written=0, rows_rejected=0, last_tick_srv=int(bars["time"][-1]) * 1000 + 30_000)
    svc.sinks = {inst.key: sink}
    yield svc, sink, spec, bars
    hot.close()


def srv(bars, i) -> int:
    return int(bars["time"][i]) * 1000


def test_poll_catches_up_from_the_last_stored_bar_and_retries_a_failed_fetch(mt5svc):
    svc, sink, spec, bars = mt5svc
    sink.last_closed["1m"] = srv(bars, 10)                    # terminal was away: bars 11..56 never polled
    svc.term.mt5.range_fail = 1
    svc._poll_rates(sink)
    assert [r[1] for r in sink.buf[spec]] == [srv(bars, 57), srv(bars, 58)]
    assert sink.last_closed["1m"] == srv(bars, 10)            # mark kept → the next poll retries
    sink.buf.clear()
    svc._poll_rates(sink)
    assert [r[1] for r in sink.buf[spec]] == [srv(bars, i) for i in range(10, 59)]
    assert sink.last_closed["1m"] == srv(bars, 58)
    assert sink.forming["1m"][1] == rates_to_rows(bars[-1:], svc.model)[0][0]
    calls = len(svc.term.mt5.range_calls)
    svc._poll_rates(sink)                                     # caught up: the cheap 3-bar poll only
    assert len(svc.term.mt5.range_calls) == calls


def test_gapfill_seeds_the_mark_even_when_its_fetch_fails(mt5svc):
    svc, sink, spec, bars = mt5svc
    sink.hot.upsert(spec, rates_to_rows(bars[:21], svc.model))
    svc.term.mt5.range_fail = 1
    svc._gapfill_rates(sink)
    assert sink.last_closed["1m"] == srv(bars, 20) and not sink.buf[spec]
    svc._poll_rates(sink)                                     # the poll finishes the job
    assert [r[1] for r in sink.buf[spec]] == [srv(bars, i) for i in range(20, 59)]


def test_audit_refetches_a_hole(mt5svc):
    svc, sink, spec, bars = mt5svc
    keep = np.ones(len(bars), dtype=bool)
    keep[30:35] = False
    sink.hot.upsert(spec, rates_to_rows(bars[keep], svc.model))
    svc._audit_rates()
    assert sorted(r[1] for r in sink.buf[spec]) == [srv(bars, i) for i in range(30, 35)]
    with svc.appdb._lock:
        ev = [r[0] for r in svc.appdb._con.execute("SELECT event FROM ingestion_events")]
    assert ev == ["audit_fill"]


def test_status_clears_the_old_error_once_after_a_reconnect(mt5svc):
    svc, sink, spec, bars = mt5svc
    svc.appdb.set_status("mt5", "reconnecting", error="IPC timeout")
    svc.reconnects = 1
    svc._clear_error = True                                   # set by connect()
    svc._status()
    row = next(r for r in svc.appdb.statuses() if r["collector"] == "mt5")
    assert row["last_error"] is None and svc._clear_error is False
    svc.appdb.set_status("mt5", "live", error="rows rejected")
    svc._status()
    assert next(r for r in svc.appdb.statuses() if r["collector"] == "mt5")["last_error"] == "rows rejected"
