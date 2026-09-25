"""Cold Parquet store, rollover and unified reader — real aggTrades fixture."""
from pathlib import Path

import numpy as np
import pyarrow as pa
import pytest

from tradingsystem.core.instruments import InstrumentRegistry
from tradingsystem.core.settings import load_settings
from tradingsystem.core.timeutil import MS_PER_DAY
from tradingsystem.storage.parquet_store import ParquetColdStore, day_of
from tradingsystem.storage.reader import InstrumentReader
from tradingsystem.storage.retention import columns_to_arrow, rollover
from tradingsystem.storage.sqlite_store import SQLiteHotStore
from tradingsystem.storage.tablespec import spec_for


@pytest.fixture(scope="module")
def btc():
    return InstrumentRegistry.from_settings(load_settings(env_path=Path("nope.env"))).primary("BTCUSDT")


def shifted_rows(tbl, day_shift: int):
    """Real rows, re-dated by whole days so they straddle several UTC days (keys shifted to stay unique)."""
    ts = [t + day_shift * MS_PER_DAY for t in tbl["ts"].to_pylist()]
    ids = [a + day_shift * 10_000_000 for a in tbl["agg_id"].to_pylist()]
    return list(zip(ids, ts, tbl["price"].to_pylist(), tbl["qty"].to_pylist(), tbl["first_id"].to_pylist(),
                    tbl["last_id"].to_pylist(), [int(x) for x in tbl["is_buyer_maker"].to_pylist()]))


def test_write_day_merges_and_dedupes(tmp_path, btc, real_aggtrades):
    spec = spec_for(btc, "agg_trades")
    cold = ParquetColdStore(tmp_path / "cold")
    tbl = real_aggtrades.select(list(spec.column_names)).cast(
        pa.schema([(c.name, pa.int64() if c.sql_type == "INTEGER" else pa.float64()) for c in spec.columns]))
    day = day_of(tbl["ts"][0].as_py())
    same_day = tbl.filter(pa.compute.less(tbl["ts"], (day.toordinal() - 719162) * MS_PER_DAY + MS_PER_DAY))
    first, second = same_day.slice(0, 2000), same_day.slice(1500)
    assert cold.write_day(btc, spec, day, first) == first.num_rows
    assert cold.write_day(btc, spec, day, second) == same_day.num_rows      # overlap 500 removed
    back = cold.read_range(btc, spec)
    assert back.num_rows == same_day.num_rows
    assert np.all(np.diff(back["agg_id"].to_numpy()) > 0)
    with pytest.raises(ValueError):
        cold.write_day(btc, spec, day.fromordinal(day.toordinal() + 5), first)   # wrong day


def test_rollover_moves_old_days_and_reader_spans_both(tmp_path, btc, real_aggtrades):
    spec = spec_for(btc, "agg_trades")
    data = tmp_path / "data"
    hot = SQLiteHotStore(btc.hot_db_path(data))
    hot.ensure_tables([spec])
    rows = []
    for shift in (0, 1, 2, 3, 4):
        rows += shifted_rows(real_aggtrades, shift)
    hot.upsert(spec, rows)
    total = hot.count(spec)
    last_ts = max(r[1] for r in rows)
    cold = ParquetColdStore(data / "cold")
    stats = rollover(hot, cold, btc, spec, hot_days=1, now_ms=last_ts + 3 * 3_600_000, grace_hours=2)
    assert stats["rows"] > 0 and stats["days"] >= 2
    assert hot.count(spec) + sum(cold.day_rows(btc, spec, d) for d in cold.days(btc, spec)) == total
    again = rollover(hot, cold, btc, spec, hot_days=1, now_ms=last_ts + 3 * 3_600_000, grace_hours=2)
    assert again["rows"] == 0                                   # idempotent
    reader = InstrumentReader(btc, data)
    allc = reader.read_range(spec)
    assert len(allc["agg_id"]) == total
    assert np.all(np.diff(allc["agg_id"]) > 0)
    mid = rows[len(rows) // 2][1]
    part = reader.read_range(spec, mid - MS_PER_DAY, mid + MS_PER_DAY, columns=["ts", "price"])
    assert part["ts"].min() >= mid - MS_PER_DAY and part["ts"].max() < mid + MS_PER_DAY
    reader.close()
    hot.close()


def test_boundary_duplicates_are_removed(tmp_path, btc, real_aggtrades):
    """A crash after writing cold but before deleting hot leaves duplicates — the reader must hide them."""
    spec = spec_for(btc, "agg_trades")
    data = tmp_path / "data"
    hot = SQLiteHotStore(btc.hot_db_path(data))
    hot.ensure_tables([spec])
    rows = shifted_rows(real_aggtrades, 0)
    hot.upsert(spec, rows)
    cold = ParquetColdStore(data / "cold")
    cols = hot.read_range(spec)
    day = day_of(int(cols["ts"][0]))
    in_day = cols["ts"] < (day.toordinal() - 719162 + 1) * MS_PER_DAY
    cold.write_day(btc, spec, day, columns_to_arrow(spec, {k: v[in_day] for k, v in cols.items()}))
    reader = InstrumentReader(btc, data)
    assert len(reader.read_range(spec)["agg_id"]) == len(rows)
    reader.close()
    hot.close()
