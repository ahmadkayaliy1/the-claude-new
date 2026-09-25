"""P2.1 — Prepare the real benchmark data set (cached as Parquet under data/research/bench/).

* BTCUSDT spot aggTrades: 7 daily Binance Vision files (checksum-verified, µs→ms normalised)
* XAUUSD@ ticks: 7 days from MT5 (server time → UTC via ServerTimeModel, synthetic key time_msc*1000+seq)
* BTCUSDT spot 1m klines: 12 monthly Vision files
"""
from __future__ import annotations

import datetime as dt
import hashlib
import io
import sys
import time
import zipfile
from pathlib import Path

import httpx
import numpy as np
import pyarrow as pa
import pyarrow.csv as pacsv
import pyarrow.parquet as pq

from tradingsystem.core.settings import PROJECT_ROOT
from tradingsystem.ingest.mt5.servertime import ServerTimeModel

OUT = PROJECT_ROOT / "data" / "research" / "bench"
VISION = "https://data.binance.vision/data/spot"
AGG_COLS = ["agg_id", "price", "qty", "first_id", "last_id", "ts", "is_buyer_maker", "is_best_match"]
KLINE_COLS = ["open_time", "open", "high", "low", "close", "volume", "close_time", "quote_volume", "trades",
              "taker_buy_base", "taker_buy_quote", "ignore"]


def fetch(c: httpx.Client, url: str) -> bytes:
    body = c.get(url).raise_for_status().content
    chk = c.get(url + ".CHECKSUM").raise_for_status().text.split()[0]
    if hashlib.sha256(body).hexdigest() != chk:
        raise ValueError(f"checksum mismatch: {url}")
    return body


def read_zip_csv(body: bytes, names: list[str]) -> pa.Table:
    with zipfile.ZipFile(io.BytesIO(body)) as z, z.open(z.namelist()[0]) as f:
        return pacsv.read_csv(f, read_options=pacsv.ReadOptions(column_names=names))


def to_ms(col: pa.ChunkedArray) -> np.ndarray:
    a = col.to_numpy().astype(np.int64)
    return np.where(a > 10**14, a // 1000, a)  # µs (spot since 2025) → ms


def agg_trades(c: httpx.Client, days: int) -> None:
    path = OUT / "btcusdt_agg_trades.parquet"
    if path.exists():
        print("cached", path)
        return
    end = dt.date.today() - dt.timedelta(days=2)
    tables, t0, nbytes = [], time.time(), 0
    for i in range(days):
        d = end - dt.timedelta(days=i)
        body = fetch(c, f"{VISION}/daily/aggTrades/BTCUSDT/BTCUSDT-aggTrades-{d}.zip")
        nbytes += len(body)
        t = read_zip_csv(body, AGG_COLS)
        tables.append(pa.table({
            "agg_id": t["agg_id"].cast(pa.int64()), "price": t["price"].cast(pa.float64()),
            "qty": t["qty"].cast(pa.float64()), "first_id": t["first_id"].cast(pa.int64()),
            "last_id": t["last_id"].cast(pa.int64()), "ts": pa.array(to_ms(t["ts"])),
            "is_buyer_maker": t["is_buyer_maker"].cast(pa.bool_()),
        }))
    tbl = pa.concat_tables(tables).sort_by("agg_id")
    OUT.mkdir(parents=True, exist_ok=True)
    pq.write_table(tbl, path, compression="zstd")
    secs = time.time() - t0
    print(f"aggTrades: {tbl.num_rows:,} rows, {nbytes / 2**20:.0f} MB zip in {secs:.0f}s "
          f"({nbytes / 2**20 / secs:.1f} MB/s download+parse)")


def klines(c: httpx.Client, months: int) -> None:
    path = OUT / "btcusdt_candles_1m.parquet"
    if path.exists():
        print("cached", path)
        return
    today = dt.date.today().replace(day=1)
    tables = []
    for i in range(1, months + 1):
        m = (today - dt.timedelta(days=1)).replace(day=1) if i == 1 else m_prev
        m_prev = (m - dt.timedelta(days=1)).replace(day=1)
        t = read_zip_csv(fetch(c, f"{VISION}/monthly/klines/BTCUSDT/1m/BTCUSDT-1m-{m:%Y-%m}.zip"), KLINE_COLS)
        tables.append(pa.table({
            "open_time": pa.array(to_ms(t["open_time"])),
            **{k: t[k].cast(pa.float64()) for k in ("open", "high", "low", "close", "volume", "quote_volume",
                                                    "taker_buy_base", "taker_buy_quote")},
            "trades": t["trades"].cast(pa.int64()),
        }))
    tbl = pa.concat_tables(tables).sort_by("open_time")
    pq.write_table(tbl, path, compression="zstd")
    print(f"klines: {tbl.num_rows:,} rows")


def xau_ticks(days: int) -> None:
    import MetaTrader5 as mt5

    path = OUT / "xauusd_ticks.parquet"
    if path.exists():
        print("cached", path)
        return
    if not mt5.initialize(path=r"C:/Program Files/MetaTrader 5/terminal64.exe"):
        raise SystemExit(mt5.last_error())
    model = ServerTimeModel()
    now_srv = int(mt5.symbol_info_tick("XAUUSD@").time)
    ticks = mt5.copy_ticks_range("XAUUSD@", now_srv - days * 86400, now_srv, mt5.COPY_TICKS_ALL)
    mt5.shutdown()
    tm = ticks["time_msc"].astype(np.int64)
    seq = np.zeros(len(tm), dtype=np.int64)
    same = np.concatenate([[False], tm[1:] == tm[:-1]])
    for i in np.nonzero(same)[0]:
        seq[i] = seq[i - 1] + 1
    utc = np.array([model.server_to_utc(int(x), prefer="earlier") for x in tm], dtype=np.int64)
    tbl = pa.table({"key": utc * 1000 + seq, "time_msc": utc, "bid": ticks["bid"], "ask": ticks["ask"],
                    "flags": ticks["flags"].astype(np.int32)})
    pq.write_table(tbl, path, compression="zstd")
    print(f"XAU ticks: {tbl.num_rows:,} rows over {days} days")


def main() -> None:
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 7
    OUT.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=120, follow_redirects=True) as c:
        agg_trades(c, days)
        klines(c, 12)
    xau_ticks(days)


if __name__ == "__main__":
    main()
