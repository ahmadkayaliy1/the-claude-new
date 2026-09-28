"""Phase 5 A6/A7, ingest and storage parts, on real data:

* the 5-min metrics table completes a stored live row when a later poll brings the column that was missing (the taker
  ratio is published a bucket after the other ratios — the cause of the null ``taker_buy_sell_vol_ratio`` in the
  production payloads): real stored rows + a real REST poll (tests/fixtures/real, PROVENANCE.md);
* the live writers' hot → cold rollover waits while the free disk is below ``storage.cold_archive_min_free_gb`` (the
  hot store keeps the rows; a later rollover moves them), with one warning event per UTC day (real aggTrades);
* the shared disk floor used by both backfills.
"""
import asyncio
import csv
import gzip
import json
import sqlite3
import types
from pathlib import Path

import pytest

from tradingsystem.core.instruments import InstrumentRegistry
from tradingsystem.core.settings import PathsCfg, load_settings
from tradingsystem.core.timeutil import MS_PER_DAY
from tradingsystem.ingest.binance import fetch
from tradingsystem.ingest.binance.service import BinanceLiveService, InstrumentSink
from tradingsystem.ingest.common.appdb import AppDB
from tradingsystem.ingest.mt5 import service as mt5svc
from tradingsystem.storage import disk
from tradingsystem.storage.parquet_store import ParquetColdStore
from tradingsystem.storage.retention import ColdArchiveGuard
from tradingsystem.storage.sqlite_store import SQLiteHotStore
from tradingsystem.storage.tablespec import spec_for

REAL = Path(__file__).resolve().parents[1] / "fixtures" / "real"
MIN = 60_000
T0700 = 1790578800000                     # 2026-09-28 07:00 UTC


@pytest.fixture(scope="module")
def settings():
    return load_settings(env_path=Path("nope.env"))


def stored_metrics(since: int) -> list[tuple]:
    with gzip.open(REAL / "ethusdt_metrics_30d.csv.gz", "rt", newline="") as f:
        r = csv.reader(f)
        next(r)
        rows = [tuple(int(v) if i == 0 else (float(v) if v != "" else None) for i, v in enumerate(row)) for row in r]
    return [x for x in rows if x[0] >= since]


class PollRest:
    """The five ratio endpoints answering with the real poll of 2026-09-28 07:41 UTC."""

    def __init__(self) -> None:
        doc = json.loads((REAL / "ethusdt_metrics_rest_poll.json").read_text())
        self.params = doc["params"]
        self.by_path = {v["path"]: v["body"] for v in doc["responses"].values()}
        self.seen: list[dict] = []

    async def get(self, path, params=None, weight=1):
        self.seen.append(dict(params))
        return self.by_path[path]


# ------------------------------------------------------------------------------------------------ metrics fill
def test_a_later_poll_fills_the_taker_ratio_the_first_poll_stored_as_null(tmp_path, settings):
    inst = InstrumentRegistry.from_settings(settings).get("binance_usdm:ETHUSDT")
    sink = InstrumentSink(inst, tmp_path / "data", 4)                      # the live writer (upsert_many per flush)
    spec = sink.spec("metrics")
    before = {r[0]: r for r in stored_metrics(T0700 - 60 * MIN)}
    sink.hot.upsert(spec, list(before.values()))
    assert all(before[T0700 + i * 5 * MIN][6] is None for i in range(-1, 5))  # 06:55..07:20 stored without it
    rest = PollRest()
    rows = asyncio.run(fetch.metrics_5m(rest, "ETHUSDT", T0700, T0700 + 25 * MIN))
    assert rest.seen[0] == rest.params                                       # the request the fixture answers
    taker = {int(d["timestamp"]): float(d["buySellRatio"]) for d in rest.by_path["/futures/data/takerlongshortRatio"]}
    assert sorted(taker) == [T0700 + i * 5 * MIN for i in range(-1, 4)]    # the taker stamps lag one bucket
    sink.add_many(spec, rows)
    n, problems = sink.flush()
    con = sqlite3.connect(inst.hot_db_path(tmp_path / "data"))             # NULL stays None (read_range gives NaN)
    try:
        after = {r[0]: r for r in con.execute(f"SELECT * FROM {spec.name} WHERE ts >= ?", (T0700 - 60 * MIN,))}
    finally:
        con.close()
    assert n == 5 and not problems                                           # 06:55 .. 07:15 completed
    for t in taker:
        assert after[t][6] == taker[t] and after[t][1:6] == before[t][1:6]  # the other columns untouched
    assert after[T0700 + 20 * MIN][6] is None                                # 07:20: its bucket was still open
    assert all(after[t] == before[t] for t in before if t < T0700 - 5 * MIN)
    sink.add_many(spec, rows)
    assert sink.flush()[0] == 0                                              # nothing left to fill: no writes
    sink.hot.close()


def test_the_fill_never_overwrites_and_other_tables_keep_do_nothing(tmp_path, settings):
    reg = InstrumentRegistry.from_settings(settings)
    inst = reg.get("binance_usdm:ETHUSDT")
    spec = spec_for(inst, "metrics")
    row = stored_metrics(T0700)[0]                                           # real 07:00 row (taker null)
    with SQLiteHotStore(tmp_path / "m.db") as st:
        st.ensure_tables([spec])
        st.upsert(spec, [row])
        other = (row[0], row[1] * 2, None, None, None, None, None)
        assert st.upsert(spec, [other]) == 0                                 # different OI, nothing null to fill
        got = st.read_range(spec)
        assert got["sum_open_interest"][0] == row[1]
    kl = spec_for(inst, "candles", inst.timeframes[0])
    assert SQLiteHotStore._upsert_sql(kl).endswith("ON CONFLICT DO NOTHING")
    assert "coalesce" in SQLiteHotStore._upsert_sql(spec)


# ------------------------------------------------------------------------------------------------ cold archive guard
def test_cold_archive_guard_skips_below_the_floor_with_one_warning_per_utc_day():
    free, warned, now = [1.5], [], [10 * MS_PER_DAY + 5 * MIN]
    g = ColdArchiveGuard(Path("x"), 2.0, warned.append, free=lambda p: free[0], clock=lambda: now[0])
    assert not g.allowed() and not g.allowed() and len(warned) == 1
    assert "1.5 GB < 2 GB" in warned[0] and "rows stay in the hot store" in warned[0]
    free[0] = 2.0
    assert g.allowed()
    free[0] = 1.0
    assert not g.allowed() and len(warned) == 1                              # same UTC day: no second event
    now[0] += MS_PER_DAY
    assert not g.allowed() and len(warned) == 2 and g.skips == 4


def test_cold_archive_guard_off_or_unreadable_lets_the_rollover_run():
    def boom(p):
        raise OSError("device not ready")

    assert ColdArchiveGuard(Path("x"), 0, pytest.fail, free=boom).allowed()      # 0 = off: disk never read
    assert ColdArchiveGuard(Path("x"), 2.0, pytest.fail, free=boom).allowed()    # unknown: the old behaviour


def test_binance_rollover_waits_below_the_cold_archive_floor_and_catches_up(tmp_path, settings, real_aggtrades,
                                                                              monkeypatch):
    """Real aggTrades of 2026-09-18 in the hot store are due for the cold archive; below the floor they stay hot (one
    ``rollover_skipped`` event), and the first pass with room moves them."""
    s = settings.model_copy(update={"paths": PathsCfg(data_dir=str(tmp_path / "data"), logs_dir=str(tmp_path / "l"))})
    inst = InstrumentRegistry.from_settings(s).primary("BTCUSDT")
    svc = object.__new__(BinanceLiveService)
    svc.s, svc.appdb = s, AppDB(tmp_path / "app.db")
    svc.cold = ParquetColdStore(s.paths.data() / "cold")
    sink = InstrumentSink(inst, s.paths.data(), 4)
    svc.sinks = {inst.key: sink}
    spec = sink.spec("agg_trades")
    t = real_aggtrades
    sink.hot.upsert(spec, list(zip(t["agg_id"].to_pylist(), t["ts"].to_pylist(), t["price"].to_pylist(),
                                   t["qty"].to_pylist(), t["first_id"].to_pylist(), t["last_id"].to_pylist(),
                                   [int(x) for x in t["is_buyer_maker"].to_pylist()])))
    free = [1.0]
    monkeypatch.setattr(disk, "free_gb", lambda p: free[0])
    guards = svc._archive_guards()
    assert set(guards) == {inst.venue}
    for _ in range(2):
        asyncio.run(svc._rollover_pass(guards))
    assert sink.hot.count(spec) == t.num_rows and not svc.cold.days(inst, spec)
    with svc.appdb._lock:
        ev = svc.appdb._con.execute("SELECT collector, event, detail FROM ingestion_events").fetchall()
    assert [(c, e) for c, e, _ in ev] == [(inst.venue, "rollover_skipped")]
    assert "cold_archive_min_free_gb" in ev[0][2]
    free[0] = 50.0
    asyncio.run(svc._rollover_pass(guards))
    assert sink.hot.count(spec) == 0
    assert sum(svc.cold.day_rows(inst, spec, d) for d in svc.cold.days(inst, spec)) == t.num_rows
    sink.hot.close()


def test_mt5_tick_rollover_waits_below_the_cold_archive_floor(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(mt5svc, "rollover", lambda *a, **k: calls.append(a[2]))
    svc = object.__new__(mt5svc.MT5LiveService)
    svc.appdb = AppDB(tmp_path / "app.db")
    svc.s = types.SimpleNamespace(storage=types.SimpleNamespace(hot_days={"ticks": 3}, rollover_grace_hours=2))
    svc.cold = ParquetColdStore(tmp_path / "cold")
    inst = InstrumentRegistry.from_settings(load_settings(env_path=Path("nope.env"))).get("mt5:XAUUSD@")
    svc.sinks = {inst.key: types.SimpleNamespace(inst=inst, hot=None)}
    free = [1.0]
    svc.archive_guard = ColdArchiveGuard(svc.cold.root, 2.0,
                                         lambda m: svc.appdb.add_event("mt5", "rollover_skipped", m),
                                         free=lambda p: free[0])
    svc._rollover()
    assert calls == []
    free[0] = 3.0
    svc._rollover()
    assert calls == [inst]
    with svc.appdb._lock:
        assert svc.appdb._con.execute("SELECT count(*) FROM ingestion_events WHERE event='rollover_skipped'"
                                      ).fetchone()[0] == 1


# ------------------------------------------------------------------------------------------------ disk floor
def test_require_free_raises_below_the_floor_and_zero_disables(monkeypatch):
    monkeypatch.setattr(disk, "free_gb", lambda p: 9.5)
    with pytest.raises(disk.DiskFullError, match="MT5 backfill paused"):
        disk.require_free(Path("x"), 10.0, what="MT5 backfill")
    assert disk.require_free(Path("x"), 9.0) == 9.5
    with pytest.raises(disk.DiskFullError):
        disk.require_free(Path("x"), 9.0, need_bytes=2 * 2**30)             # the next write would cross the floor
    assert disk.require_free(Path("x"), 0) == 9.5


def test_free_disk_is_read_on_the_nearest_existing_folder(tmp_path):
    assert disk.usage_path(tmp_path / "not" / "yet" / "there") == tmp_path
    assert disk.free_gb(tmp_path / "missing") > 0


def test_the_vision_backfill_and_the_mt5_backfill_share_one_disk_full_error():
    from tradingsystem.ingest.binance import vision
    assert vision.DiskFullError is disk.DiskFullError
