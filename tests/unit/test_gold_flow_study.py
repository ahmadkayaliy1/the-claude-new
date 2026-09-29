"""Phase 5 B9: research/gold_flow/study.py on tiny day files (BUILT here: the study's own Parquet layout with a known
relationship, so the verdict logic can be checked; the real run is over production's cold store, idle time)."""
import datetime as dt
import importlib.util
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("gold_flow_study", ROOT / "research" / "gold_flow" / "study.py")
st = importlib.util.module_from_spec(spec)
spec.loader.exec_module(st)


def write_day(cold: Path, day: dt.date, coupling: float, rng) -> None:
    """Built: per minute one perp trade whose signed size drives the broker's mid move with ``coupling``."""
    start = int(dt.datetime(day.year, day.month, day.day, tzinfo=dt.timezone.utc).timestamp() * 1000)
    n = 1440
    ts = start + np.arange(n) * 60_000 + 10_000
    signed = rng.normal(size=n)
    noise = rng.normal(size=n)
    move = coupling * signed + np.sqrt(max(0.0, 1 - coupling ** 2)) * noise
    mid = 2000 * np.exp(np.cumsum(move * 1e-4))
    p = cold / "binance_usdm" / "XAUUSDT" / "xauusdt_agg_trades" / str(day.year)
    p.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table({"agg_id": np.arange(n), "ts": ts, "price": mid, "qty": np.abs(signed),
                             "first_id": np.arange(n), "last_id": np.arange(n),
                             "is_buyer_maker": (signed < 0).astype(np.int64)}), p / f"{day.isoformat()}.parquet")
    b = cold / "mt5" / "XAUUSD" / "xauusd_ticks" / str(day.year)
    b.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table({"key": np.arange(n), "time_msc": ts + 20_000, "srv_msc": ts, "bid": mid - 0.1,
                             "ask": mid + 0.1, "last": np.zeros(n), "volume_real": np.zeros(n),
                             "flags": np.zeros(n, dtype=np.int64)}), b / f"{day.isoformat()}.parquet")


def days(n):
    d0 = dt.date(2026, 6, 1)                            # a Monday
    return [d0 + dt.timedelta(days=i) for i in range(n) if (d0 + dt.timedelta(days=i)).weekday() < 5]


def test_a_coupled_series_passes_and_the_lag_is_found(tmp_path):
    rng = np.random.default_rng(7)
    for d in days(35):
        write_day(tmp_path / "data" / "cold", d, 0.8, rng)
    r = st.run(tmp_path, None, None, progress=lambda m: None)
    assert r["days_used"] == 25 and r["corr_1m"][0] > 0.6 and abs(r["corr_1m"][3]) < 0.1
    assert r["rolling_same_sign"] and r["passes"] is True
    assert "PASSES" in st.render(r)


def test_an_uncoupled_series_fails(tmp_path):
    rng = np.random.default_rng(11)
    for d in days(35):
        write_day(tmp_path / "data" / "cold", d, 0.0, rng)
    r = st.run(tmp_path, None, None, progress=lambda m: None)
    assert abs(r["corr_1m"][0]) < 0.1 and r["passes"] is False and "FAILS" in st.render(r)


def test_closed_minutes_are_excluded():
    m = st.open_mask(dt.date(2026, 6, 5))                # a Friday: the market closes at 21:00 UTC (EDT)
    assert m[20 * 60] and not m[21 * 60 + 30]
    assert not st.open_mask(dt.date(2026, 6, 6)).any()   # Saturday
