"""Hot store: idempotent upserts, config-driven tables, reads, crash consistency (real data fixtures)."""
import pickle
import subprocess
import sys
import textwrap
from pathlib import Path

import numpy as np
import pytest

from tradingsystem.core.instruments import InstrumentRegistry
from tradingsystem.core.settings import load_settings
from tradingsystem.core.timeframes import Timeframe
from tradingsystem.storage.sqlite_store import SQLiteHotStore
from tradingsystem.storage.tablespec import spec_for, table_specs


@pytest.fixture(scope="module")
def registry():
    return InstrumentRegistry.from_settings(load_settings(env_path=Path("nope.env")))


def agg_rows(tbl):
    return list(zip(tbl["agg_id"].to_pylist(), tbl["ts"].to_pylist(), tbl["price"].to_pylist(),
                    tbl["qty"].to_pylist(), tbl["first_id"].to_pylist(), tbl["last_id"].to_pylist(),
                    [int(x) for x in tbl["is_buyer_maker"].to_pylist()]))


def test_tables_generated_from_config(tmp_path, registry):
    btc = registry.primary("BTCUSDT")
    names = [s.name for s in table_specs(btc)]
    assert "btcusdt_candles_1m" in names and "btcusdt_candles_1w" in names
    assert {"btcusdt_agg_trades", "btcusdt_book_ticker", "btcusdt_depth"} <= set(names)
    with SQLiteHotStore(btc.hot_db_path(tmp_path)) as st:
        st.ensure_tables(table_specs(btc))
        st.ensure_tables(table_specs(btc))  # idempotent DDL
    gold = registry.primary("XAUUSD")
    assert [s.name for s in table_specs(gold)][:2] == ["xauusd_candles_1m", "xauusd_candles_5m"]
    assert "srv_time" in spec_for(gold, "candles", Timeframe.H1).column_names


def test_upsert_is_idempotent(tmp_path, registry, real_aggtrades):
    btc = registry.primary("BTCUSDT")
    spec = spec_for(btc, "agg_trades")
    rows = agg_rows(real_aggtrades)
    with SQLiteHotStore(tmp_path / "a.db") as st:
        st.ensure_tables([spec])
        assert st.upsert(spec, rows) == len(rows)
        assert st.upsert(spec, rows) == 0                    # exact duplicates ignored
        assert st.upsert(spec, rows[-500:] + rows[:10]) == 0
        assert st.count(spec) == len(rows)
        assert st.last_key(spec) == (rows[-1][0],)
        assert st.last_time(spec) == max(r[1] for r in rows)


def test_reads_return_numpy_columns(tmp_path, registry, real_aggtrades):
    spec = spec_for(registry.primary("BTCUSDT"), "agg_trades")
    rows = agg_rows(real_aggtrades)
    with SQLiteHotStore(tmp_path / "a.db") as st:
        st.ensure_tables([spec])
        st.upsert(spec, rows)
        t0, t1 = rows[100][1], rows[200][1]
        cols = st.read_range(spec, t0, t1, columns=["agg_id", "price", "ts"])
        assert cols["agg_id"].dtype == np.int64 and cols["price"].dtype == np.float64
        assert np.all(np.diff(cols["agg_id"]) > 0)
        assert cols["ts"].min() >= t0 and cols["ts"].max() < t1
        last = st.read_last(spec, 50)
        assert list(last["agg_id"]) == [r[0] for r in rows[-50:]]


def test_reader_sees_writer_commits(tmp_path, registry, real_candles_1m):
    spec = spec_for(registry.primary("BTCUSDT"), "candles", Timeframe.M1)
    tbl = real_candles_1m
    rows = list(zip(*(tbl[c].to_pylist() for c in spec.column_names)))
    path = tmp_path / "k.db"
    with SQLiteHotStore(path) as w:
        w.ensure_tables([spec])
        w.upsert(spec, rows[:1000])
        with SQLiteHotStore(path, readonly=True) as r:
            assert r.count(spec) == 1000
            w.upsert(spec, rows[1000:])
            assert r.count(spec) == len(rows)
            with pytest.raises(PermissionError):
                r.upsert(spec, rows[:1])


def test_crash_mid_transaction_leaves_db_consistent(tmp_path, registry, real_aggtrades):
    spec = spec_for(registry.primary("BTCUSDT"), "agg_trades")
    path = tmp_path / "crash.db"
    rows = agg_rows(real_aggtrades)
    with SQLiteHotStore(path) as st:
        st.ensure_tables([spec])
        st.upsert(spec, rows[:1000])
    # a child process opens a transaction, inserts the rest, and is killed before COMMIT
    (tmp_path / "rows.pkl").write_bytes(pickle.dumps(rows[1000:]))
    script = tmp_path / "child.py"
    script.write_text(textwrap.dedent(f"""
        import sqlite3, os, pickle
        rows = pickle.loads(open(r"{tmp_path / 'rows.pkl'}", "rb").read())
        con = sqlite3.connect(r"{path}", isolation_level=None)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("BEGIN")
        con.executemany("INSERT INTO {spec.name} VALUES (?,?,?,?,?,?,?)", rows)
        print("inserted", flush=True)
        os._exit(9)
    """))
    out = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=60)
    assert "inserted" in out.stdout
    with SQLiteHotStore(path) as st:
        assert st.count(spec) == 1000                  # uncommitted rows never appear
        assert st.upsert(spec, rows) == len(rows) - 1000
