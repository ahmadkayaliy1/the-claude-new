"""Streaming cold day writer (BF-04) and chunked hot-store reads — real aggTrades / candles fixtures."""
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pytest

from tradingsystem.core.instruments import InstrumentRegistry
from tradingsystem.core.settings import load_settings
from tradingsystem.core.timeutil import MS_PER_DAY
from tradingsystem.storage import sqlite_store
from tradingsystem.storage.parquet_store import ParquetColdStore, arrow_schema, day_of
from tradingsystem.storage.sqlite_store import SQLiteHotStore
from tradingsystem.storage.tablespec import spec_for


@pytest.fixture(scope="module")
def btc():
    return InstrumentRegistry.from_settings(load_settings(env_path=Path("nope.env"))).primary("BTCUSDT")


@pytest.fixture()
def day_table(btc, real_aggtrades):
    """The fixture's rows of its first UTC day, in the cold schema (key-sorted, consecutive ids)."""
    spec = spec_for(btc, "agg_trades")
    tbl = real_aggtrades.select(list(spec.column_names)).cast(arrow_schema(spec))
    day = day_of(tbl["ts"][0].as_py())
    end = (day.toordinal() - 719162) * MS_PER_DAY + MS_PER_DAY
    return spec, day, tbl.filter(pc.less(tbl["ts"], end))


def write_stream(cold, btc, spec, day, parts) -> int:
    w = cold.day_writer(btc, spec, day, dense_key=True)
    for p in parts:
        w.write(p)
    return w.commit()


def test_streamed_day_equals_one_shot_write(tmp_path, btc, day_table):
    spec, day, tbl = day_table
    a, b = ParquetColdStore(tmp_path / "a"), ParquetColdStore(tmp_path / "b")
    n = write_stream(a, btc, spec, day, [tbl.slice(i, 250) for i in range(0, tbl.num_rows, 250)])
    assert n == b.write_day(btc, spec, day, tbl) == tbl.num_rows
    assert a.read_range(btc, spec).equals(b.read_range(btc, spec))
    assert not list(a.table_dir(btc, spec).rglob("*.stmp*"))


def test_unsorted_or_duplicate_batches_are_sorted_and_deduped(tmp_path, btc, day_table):
    spec, day, tbl = day_table
    cold = ParquetColdStore(tmp_path)
    parts = [tbl.slice(1000), tbl.slice(0, 1200)]                   # out of order + 200 duplicates
    assert write_stream(cold, btc, spec, day, parts) == tbl.num_rows
    back = cold.read_range(btc, spec)["agg_id"].to_numpy()
    assert np.array_equal(back, tbl["agg_id"].to_numpy())


def test_dense_superset_replaces_a_holey_day_and_subset_keeps_it(tmp_path, btc, day_table):
    spec, day, tbl = day_table
    cold = ParquetColdStore(tmp_path)
    holey = pa.concat_tables([tbl.slice(0, 100), tbl.slice(300)])   # live capture with an outage hole
    cold.write_day(btc, spec, day, holey)
    assert write_stream(cold, btc, spec, day, [tbl]) == tbl.num_rows  # complete Vision day wins, no merge needed
    assert cold.read_range(btc, spec)["agg_id"].to_pylist() == tbl["agg_id"].to_pylist()
    path = cold.day_path(btc, spec, day)
    before = path.stat().st_mtime_ns
    assert write_stream(cold, btc, spec, day, [tbl.slice(10, 50)]) == tbl.num_rows   # subset: existing file kept
    assert path.stat().st_mtime_ns == before


def test_partial_overlap_merges(tmp_path, btc, day_table):
    spec, day, tbl = day_table
    cold = ParquetColdStore(tmp_path)
    cold.write_day(btc, spec, day, tbl.slice(0, 600))
    assert write_stream(cold, btc, spec, day, [tbl.slice(400)]) == tbl.num_rows
    assert cold.read_range(btc, spec)["agg_id"].to_pylist() == tbl["agg_id"].to_pylist()


def test_abort_and_wrong_day(tmp_path, btc, day_table):
    spec, day, tbl = day_table
    cold = ParquetColdStore(tmp_path)
    w = cold.day_writer(btc, spec, day)
    w.write(tbl.slice(0, 10))
    w.abort()
    assert not cold.days(btc, spec) and not list(tmp_path.rglob("*.stmp*"))
    w = cold.day_writer(btc, spec, day.fromordinal(day.toordinal() + 1))
    with pytest.raises(ValueError):
        w.write(tbl.slice(0, 10))
    w.abort()


def test_chunked_reads_match_and_keep_nulls(tmp_path, real_candles_1m, monkeypatch):
    """MT5 candle rows (real BTC bars; ``real_volume`` is NULL when the broker reports 0) read in 7-row chunks."""
    monkeypatch.setattr(sqlite_store, "_FETCH_CHUNK", 7)
    reg = InstrumentRegistry.from_settings(load_settings(env_path=Path("nope.env")))
    spec = spec_for(next(i for i in reg.all() if i.venue == "mt5"), "candles", reg.primary("BTCUSDT").timeframes[0])
    hot = SQLiteHotStore(tmp_path / "h.db")
    hot.ensure_tables([spec])
    c = {k: real_candles_1m[k].to_pylist()[:50] for k in ("open_time", "open", "high", "low", "close", "volume",
                                                         "trades")}
    rows = [(t, t, o, h, lo, cl, n, 0, None if i == 20 else v) for i, (t, o, h, lo, cl, v, n) in
            enumerate(zip(*c.values()))]
    hot.upsert(spec, rows)
    got = hot.read_range(spec)
    assert got["open_time"].dtype == np.int64 and got["open_time"].tolist() == c["open_time"]
    rv = got["real_volume"]
    assert rv.dtype == np.float64 and np.isnan(rv[20]) and np.count_nonzero(np.isnan(rv)) == 1
    assert rv[21] == c["volume"][21]
    assert hot.read_last(spec, 3)["open_time"].tolist() == c["open_time"][-3:]
    assert all(len(v) == 0 for v in hot.read_range(spec, 0, 1).values())
