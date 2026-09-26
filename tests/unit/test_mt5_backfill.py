"""MT5 backfill failure semantics (BF-06/BF-07/OPS-02): a ``None`` from the terminal is never "no data".

The terminal is replaced by a fake module whose answers are real bars/ticks from the fixtures (converted to server
time exactly as the terminal serves them), a ``None`` with an error code, or a link failure.
"""
import datetime as dt
import types
from pathlib import Path

import numpy as np
import pytest

from tradingsystem.core.filelock import FileLock
from tradingsystem.core.instruments import InstrumentRegistry
from tradingsystem.core.sessions import CALENDARS
from tradingsystem.core.settings import load_settings
from tradingsystem.core.timeframes import Timeframe
from tradingsystem.ingest.common.appdb import AppDB
from tradingsystem.ingest.common.backfill_loop import PassResult
from tradingsystem.ingest.mt5 import backfill as mt5bf
from tradingsystem.ingest.mt5.backfill import MT5Backfill, MT5CallError, is_transient
from tradingsystem.ingest.mt5.servertime import ServerTimeModel
from tradingsystem.ingest.mt5.terminal import AccountMismatch, MT5Unavailable
from tradingsystem.storage.gaps import KNOWN_GAPS
from tradingsystem.storage.parquet_store import ParquetColdStore
from tradingsystem.storage.sqlite_store import SQLiteHotStore
from tradingsystem.storage.tablespec import VISION_DONE, spec_for, system_specs, table_specs

RATE_DTYPE = [("time", "<i8"), ("open", "<f8"), ("high", "<f8"), ("low", "<f8"), ("close", "<f8"),
              ("tick_volume", "<u8"), ("spread", "<i4"), ("real_volume", "<u8")]
TICK_DTYPE = [("time", "<i8"), ("bid", "<f8"), ("ask", "<f8"), ("last", "<f8"), ("volume", "<u8"),
              ("time_msc", "<i8"), ("flags", "<u4"), ("volume_real", "<f8")]
OK, CALL_FAILED, NO_IPC = (1, "Success"), (-1, "Terminal: Call failed"), (-10004, "No IPC connection")


class FakeMT5:
    """Just enough of the MetaTrader5 module; ``rates``/``ticks`` decide each answer."""
    COPY_TICKS_ALL = -1

    def __init__(self) -> None:
        self.connected, self.err, self.calls = True, OK, []
        self.rates = lambda lo_s, hi_s: None
        self.ticks = lambda a_s, b_s: None
        for tf in Timeframe:
            setattr(self, tf.mt5_attr, tf.ms)

    def copy_rates_range(self, sym, tfc, lo_s, hi_s):
        self.calls.append(("rates", lo_s, hi_s))
        return self.rates(lo_s, hi_s)

    def copy_ticks_range(self, sym, a_s, b_s, flags):
        self.calls.append(("ticks", a_s, b_s))
        return self.ticks(a_s, b_s)

    def last_error(self):
        return self.err

    def terminal_info(self):
        return types.SimpleNamespace(connected=self.connected)


class FakeTerm:
    def __init__(self) -> None:
        self.mt5, self.connected = FakeMT5(), True
        self.profile = types.SimpleNamespace(server="WindsorBrokers1-Demo")
        self.connect_errors: list[Exception] = []
        self.connects = 0

    def healthy(self) -> bool:
        return self.mt5.connected

    def connect(self):
        self.connects += 1
        if self.connect_errors:
            raise self.connect_errors.pop(0)
        self.connected = self.mt5.connected = True

    def shutdown(self) -> None:
        self.connected = False

    def select_symbols(self, names):
        return {}

    def timeframe(self, attr: str) -> int:
        return getattr(self.mt5, attr)


@pytest.fixture(scope="module")
def settings():
    return load_settings(env_path=Path("nope.env"))


@pytest.fixture()
def bf(tmp_path, settings, monkeypatch):
    clock = types.SimpleNamespace(t=1_000.0)
    fake_time = types.SimpleNamespace(time=lambda: clock.t, sleep=lambda s: setattr(clock, "t", clock.t + s))
    monkeypatch.setattr(mt5bf, "time", fake_time)
    b = object.__new__(MT5Backfill)
    b.s, b.data = settings, tmp_path
    b.appdb = AppDB(tmp_path / "app.db")
    b.cold = ParquetColdStore(tmp_path / "cold")
    b.model = ServerTimeModel()
    b.term = FakeTerm()
    b.instruments = [i for i in InstrumentRegistry.from_settings(settings).all() if i.venue == "mt5"]
    b.progress = {}
    b.sibling_dbs, b.heavy_lock, b._lock_warned = [], FileLock(tmp_path / "history.lock"), False
    b.clock = clock
    return b


@pytest.fixture(scope="module")
def srv_bars(real_candles_1m):
    """60 real BTC 1m bars as the terminal returns them (server-time epoch seconds)."""
    model = ServerTimeModel()
    n = 60
    arr = np.zeros(n, dtype=RATE_DTYPE)
    arr["time"] = [model.utc_to_server(int(t)) // 1000 for t in real_candles_1m["open_time"].to_numpy()[:n]]
    for c in ("open", "high", "low", "close"):
        arr[c] = real_candles_1m[c].to_numpy()[:n]
    arr["tick_volume"] = real_candles_1m["trades"].to_numpy()[:n]
    return arr


def in_range(arr, lo_s, hi_s, drop=()):
    keep = (arr["time"] >= lo_s) & (arr["time"] <= hi_s)
    for i in drop:
        keep[i] = False
    return arr[keep]


def seed(bf, srv_bars, hole=range(20, 30), cache_earliest=True):
    inst = next(i for i in bf.instruments if i.symbol.startswith("BTCUSD"))
    hot = SQLiteHotStore(inst.hot_db_path(bf.data))
    hot.ensure_tables([*table_specs(inst), *system_specs()])
    spec = spec_for(inst, "candles", Timeframe.M1)
    keep = np.ones(len(srv_bars), dtype=bool)
    keep[list(hole)] = False
    from tradingsystem.ingest.mt5.convert import rates_to_rows
    hot.upsert(spec, rates_to_rows(srv_bars[keep], bf.model))
    if cache_earliest:
        hot.replace(VISION_DONE, [(spec.name, "earliest_srv_ms", int(srv_bars["time"][0]) * 1000, 1)])
    now_srv = int(srv_bars["time"][-1]) * 1000 + 60_000 + 1
    return inst, hot, spec, now_srv


def run_tf(bf, inst, hot, spec, now_srv):
    bf._yield_to_live = lambda: None
    bf._candles_tf(inst, hot, spec, Timeframe.M1, 0, CALENDARS["always_open"], now_srv)


def known(hot, spec):
    g = hot.read_range(KNOWN_GAPS)
    return [(s, e) for t, s, e in zip(g["table_name"], g["start"], g["end"]) if t == spec.name]


def test_call_tells_link_errors_from_unverified_answers(bf):
    m = bf.term.mt5
    m.err = NO_IPC
    with pytest.raises(MT5CallError):
        bf._call("copy_rates_range", lambda: None)
    m.err = CALL_FAILED
    assert bf._call("copy_rates_range", lambda: None) is None                 # unverified, not "empty"
    m.connected = False
    with pytest.raises(MT5CallError):
        bf._call("copy_rates_range", lambda: None)                           # broker link down
    assert is_transient(MT5CallError("x")) and is_transient(MT5Unavailable("x"))
    assert not is_transient(AccountMismatch("x")) and not is_transient(ValueError("x"))


def test_failed_chunk_never_becomes_a_source_gap(bf, srv_bars):
    inst, hot, spec, now_srv = seed(bf, srv_bars)
    bf.term.mt5.err = CALL_FAILED                                            # every request: None, terminal ok
    run_tf(bf, inst, hot, spec, now_srv)
    assert known(hot, spec) == [] and "unverified" in bf.progress[spec.name]


def test_link_failure_aborts_the_timeframe_without_recording(bf, srv_bars):
    inst, hot, spec, now_srv = seed(bf, srv_bars)
    bf.term.mt5.err = NO_IPC
    with pytest.raises(MT5CallError):
        run_tf(bf, inst, hot, spec, now_srv)
    assert known(hot, spec) == []


def test_empty_while_disconnected_is_an_error(bf, srv_bars):
    inst, hot, spec, now_srv = seed(bf, srv_bars)
    bf.term.mt5.rates = lambda lo, hi: in_range(srv_bars, lo, hi, drop=range(20, 30))
    bf.term.mt5.connected = False
    with pytest.raises(MT5CallError):
        run_tf(bf, inst, hot, spec, now_srv)
    assert known(hot, spec) == []


def test_verified_empty_gap_is_recorded_after_its_own_request(bf, srv_bars):
    inst, hot, spec, now_srv = seed(bf, srv_bars)
    bf.term.mt5.rates = lambda lo, hi: in_range(srv_bars, lo, hi, drop=range(20, 30))
    run_tf(bf, inst, hot, spec, now_srv)
    hole = (int(srv_bars["time"][20]) * 1000, int(srv_bars["time"][29]) * 1000)
    assert known(hot, spec) == [hole]
    assert ("rates", hole[0] // 1000, hole[1] // 1000) in bf.term.mt5.calls      # the exact gap was asked


def test_late_history_is_filled_by_the_verification_request(bf, srv_bars):
    inst, hot, spec, now_srv = seed(bf, srv_bars)
    hole_lo = int(srv_bars["time"][20])
    # the chunk comes back while the terminal is still downloading (partial); the gap request gets everything
    bf.term.mt5.rates = lambda lo, hi: (in_range(srv_bars, lo, hi) if lo == hole_lo
                                        else in_range(srv_bars, lo, hi, drop=range(20, 30)))
    run_tf(bf, inst, hot, spec, now_srv)
    assert known(hot, spec) == [] and hot.count(spec) == len(srv_bars)


def test_unverified_probe_never_caches_the_earliest_bar(bf, srv_bars):
    inst, hot, spec, now_srv = seed(bf, srv_bars, cache_earliest=False)
    first_s = int(srv_bars["time"][0])
    glitch = {"left": 2}

    def rates(lo, hi):          # the first probe of an older month fails on both of its attempts
        if hi < first_s and glitch["left"]:
            glitch["left"] -= 1
            return None
        return in_range(srv_bars, lo, hi)

    bf.term.mt5.rates, bf.term.mt5.err = rates, CALL_FAILED
    run_tf(bf, inst, hot, spec, now_srv)
    cached = hot.read_range(VISION_DONE)
    assert "earliest_srv_ms" not in set(cached["period"])
    bf.term.mt5.rates = lambda lo, hi: in_range(srv_bars, lo, hi)
    run_tf(bf, inst, hot, spec, now_srv)
    assert bf._earliest_cached(hot, spec.name) == first_s * 1000


@pytest.fixture(scope="module")
def raw_ticks(real_xau_ticks):
    model = ServerTimeModel()
    srv = np.array([model.utc_to_server(int(t)) for t in real_xau_ticks["time_msc"].to_numpy()], dtype=np.int64)
    arr = np.zeros(len(srv), dtype=TICK_DTYPE)
    arr["time_msc"], arr["time"] = srv, srv // 1000
    arr["bid"], arr["ask"] = real_xau_ticks["bid"].to_numpy(), real_xau_ticks["ask"].to_numpy()
    arr["flags"] = real_xau_ticks["flags"].to_numpy()
    return arr


def tick_run(bf, raw_ticks, fail_hour_s=None, err=CALL_FAILED):
    inst = next(i for i in bf.instruments if i.symbol.startswith("XAUUSD"))
    hot = SQLiteHotStore(inst.hot_db_path(bf.data))
    hot.ensure_tables([*table_specs(inst), *system_specs()])
    bf._yield_to_live = lambda: None
    bf.term.mt5.err = err

    def ticks(a_s, b_s):
        if fail_hour_s is not None and a_s <= fail_hour_s < b_s:
            return None
        t = raw_ticks["time_msc"] // 1000
        return raw_ticks[(t >= a_s) & (t <= b_s)]

    bf.term.mt5.ticks = ticks
    return inst, hot, spec_for(inst, "ticks")


def done_days(hot, spec):
    d = hot.read_range(VISION_DONE)
    return {p for t, p in zip(d["table_name"], d["period"]) if t == spec.name}


def test_tick_day_is_marked_done_only_when_every_hour_answered(bf, raw_ticks):
    inst, hot, spec = tick_run(bf, raw_ticks, fail_hour_s=int(raw_ticks["time_msc"][0]) // 1000)
    utc_day = dt.date(2026, 9, 21)
    bf.ticks(inst, hot, today=utc_day + dt.timedelta(days=1))
    assert utc_day.isoformat() not in done_days(hot, spec) and "unverified" in bf.progress[spec.name]
    bf.progress.clear()
    tick_run(bf, raw_ticks)
    bf.ticks(inst, hot, today=utc_day + dt.timedelta(days=1))
    assert utc_day.isoformat() in done_days(hot, spec)
    assert bf.cold.day_rows(inst, spec, utc_day) == len(raw_ticks)


def test_tick_link_failure_raises_and_marks_nothing(bf, raw_ticks):
    inst, hot, spec = tick_run(bf, raw_ticks, fail_hour_s=int(raw_ticks["time_msc"][0]) // 1000, err=NO_IPC)
    with pytest.raises(MT5CallError):
        bf.ticks(inst, hot, today=dt.date(2026, 9, 22))
    assert done_days(hot, spec) == set()


def test_connect_retries_then_succeeds(bf):
    bf.term.connect_errors = [MT5Unavailable("initialize failed: (-10005, 'IPC timeout')")] * 2
    res = PassResult()
    assert bf._connect(res) and res.ok and bf.term.connects == 3
    row = next(r for r in bf.appdb.statuses() if r["collector"] == mt5bf.COLLECTOR)
    assert row["state"] == "reconnecting" and "IPC timeout" in row["last_error"]


def test_connect_gives_up_after_the_window_and_account_mismatch_is_permanent(bf):
    bf.term.connect_errors = [MT5Unavailable("down")] * 1000
    res = PassResult()
    assert not bf._connect(res) and "connect" in res.transient
    assert bf.clock.t - 1_000.0 <= mt5bf.CONNECT_RETRY_S
    bf.term.connect_errors = [AccountMismatch("terminal is on 'Other-Live'")]
    res = PassResult()
    assert not bf._connect(res) and "connect" in res.permanent and not res.transient


def test_pass_reconnects_between_units_and_ends_early_when_the_terminal_stays_away(bf):
    calls = []

    def candles(inst, hot, tfs=None):
        calls.append(inst.key)
        bf.term.mt5.connected = False                                        # link lost mid-step
        bf.term.connect_errors = [MT5Unavailable("initialize failed")] * 1000
        raise MT5CallError("copy_rates_range failed: (-10004, 'No IPC connection')")

    def ticks(inst, hot):
        calls.append(("ticks", inst.key))

    bf.candles, bf.ticks = candles, ticks
    res = bf.run()
    assert calls == [bf.instruments[0].key]                                   # no futile attempts on a dead link
    assert not res.ok and f"candles {bf.instruments[0].key} 1h-1w" in res.transient and "connect" in res.transient
    db = AppDB(bf.data / "app.db")
    row = next(r for r in db.statuses() if r["collector"] == mt5bf.COLLECTOR)
    assert row["state"] == "reconnecting"
    with db._lock:
        ev = [r[0] for r in db._con.execute("SELECT event FROM ingestion_events WHERE collector=?",
                                           (mt5bf.COLLECTOR,))]
    assert "done" not in ev and "incomplete" in ev


def test_clean_pass_records_done(bf):
    bf.candles = lambda inst, hot, tfs=None: None
    bf.ticks = lambda inst, hot: None
    res = bf.run()
    assert res.ok
    db = AppDB(bf.data / "app.db")
    row = next(r for r in db.statuses() if r["collector"] == mt5bf.COLLECTOR)
    assert row["state"] == "stopped" and row["last_error"] is None


def test_higher_timeframes_of_every_instrument_come_first(bf):
    """1h-1w candles of all instruments (what the engine's data gate needs), then 1m-15m, then ticks: a multi-year
    1m history or tick archive of one symbol never starves another symbol's 4h/1d bars."""
    order = []
    bf.candles = lambda inst, hot, tfs=None: order.append(("c", inst.key, min(tf.ms for tf in tfs)))
    bf.ticks = lambda inst, hot: order.append(("t", inst.key, 0))
    assert bf.run().ok
    n = len(bf.instruments)
    assert all(kind == "c" and low >= 3_600_000 for kind, _, low in order[:n])
    assert all(kind == "c" and low < 3_600_000 for kind, _, low in order[n:2 * n])
    assert [k for k, _, _ in order[2 * n:]] == ["t"] * n
