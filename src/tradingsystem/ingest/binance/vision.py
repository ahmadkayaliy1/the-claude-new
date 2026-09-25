"""Binance Vision historical backfill (P3.2) — runs in the backfill worker process (CPU isolated from WS).

* Plans monthly files for complete months and daily files for the rest (accepting only expected names —
  the listing contains stray objects, D-017).
* Downloads resumably to ``data/vision_cache`` (``.part`` → rename), verifies the SHA-256 CHECKSUM,
  parses with pyarrow in bounded blocks, normalises µs→ms per value, validates every row.
* aggTrades → cold Parquet day files (merged); klines / metrics → hot SQLite.
* Progress is recorded in the ``vision_done`` table, so reruns skip finished periods.
* Stops (without touching live capture) when free disk < ``storage.min_free_disk_gb``.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import io
import logging
import re
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import httpx
import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.csv as pacsv

from ...core.instruments import Instrument
from ...core.timeframes import Timeframe
from ...core.timeutil import MS_PER_DAY, now_ms
from ...storage.parquet_store import ParquetColdStore, day_of, day_start_ms
from ...storage.sqlite_store import SQLiteHotStore
from ...storage.tablespec import VISION_DONE, TableSpec
from ...storage.validators import validate_rows
from .markets import Market
from .parsers import metrics_vision

log = logging.getLogger(__name__)

LIST_URL = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"

KLINE_COLS = ["open_time", "open", "high", "low", "close", "volume", "close_time", "quote_volume", "trades",
              "taker_buy_base", "taker_buy_quote", "ignore"]
AGG_COLS_SPOT = ["agg_id", "price", "qty", "first_id", "last_id", "ts", "is_buyer_maker", "is_best_match"]
AGG_COLS_FUT = ["agg_id", "price", "qty", "first_id", "last_id", "ts", "is_buyer_maker"]


class DiskFullError(RuntimeError):
    pass


@dataclass(frozen=True)
class VisionFile:
    key: str            # object key under data.binance.vision
    period: str         # "2026-08" (monthly) or "2026-09-24" (daily)
    monthly: bool

    @property
    def first_day(self) -> dt.date:
        return dt.date.fromisoformat(self.period + "-01") if self.monthly else dt.date.fromisoformat(self.period)

    @property
    def end_day(self) -> dt.date:  # exclusive
        d = self.first_day
        if not self.monthly:
            return d + dt.timedelta(days=1)
        return (d.replace(day=28) + dt.timedelta(days=4)).replace(day=1)


class VisionClient:
    def __init__(self, base_url: str, cache_dir: Path, *, min_free_gb: float, keep_zips: bool = False) -> None:
        self.base = base_url.rstrip("/")
        self.cache = cache_dir
        self.min_free_gb = min_free_gb
        self.keep_zips = keep_zips
        self.http = httpx.Client(timeout=180, follow_redirects=True)
        self.bytes_downloaded = 0

    def close(self) -> None:
        self.http.close()

    # ------------------------------------------------------------------ listing
    def list_zips(self, prefix: str) -> list[str]:
        keys, marker = [], ""
        while True:
            r = self.http.get(LIST_URL, params={"delimiter": "/", "prefix": prefix, "marker": marker})
            r.raise_for_status()
            found = re.findall(r"<Key>([^<]+)</Key>", r.text)
            keys += [k for k in found if k.endswith(".zip")]
            if "<IsTruncated>true</IsTruncated>" not in r.text or not found:
                return keys
            marker = found[-1]

    def plan(self, market: Market, dtype: str, symbol: str, interval: str | None, start: dt.date,
             end: dt.date) -> list[VisionFile]:
        """Files covering [start, end) — monthly where the whole month is inside, daily elsewhere."""
        sub = f"{dtype}/{symbol}/" + (f"{interval}/" if interval else "")
        stem = f"{symbol}-{interval}" if interval else f"{symbol}-{dtype}"
        mpat = re.compile(rf"/{re.escape(stem)}-(\d{{4}}-\d{{2}})\.zip$")
        dpat = re.compile(rf"/{re.escape(stem)}-(\d{{4}}-\d{{2}}-\d{{2}})\.zip$")
        monthly = {m.group(1): k for k in self.list_zips(f"data/{market.vision_prefix}/monthly/{sub}")
                   if (m := mpat.search(k))}
        daily = {m.group(1): k for k in self.list_zips(f"data/{market.vision_prefix}/daily/{sub}")
                 if (m := dpat.search(k))}
        out: list[VisionFile] = []
        d = start
        while d < end:
            month = f"{d:%Y-%m}"
            mf = VisionFile(monthly.get(month, ""), month, True)
            if d.day == 1 and month in monthly and mf.end_day <= end:
                out.append(mf)
                d = mf.end_day
                continue
            ds = d.isoformat()
            if ds in daily:
                out.append(VisionFile(daily[ds], ds, False))
            d += dt.timedelta(days=1)
        return out

    # ------------------------------------------------------------------ download
    def _check_disk(self) -> None:
        free_gb = shutil.disk_usage(self.cache.anchor if self.cache.anchor else self.cache).free / 2**30
        if free_gb < self.min_free_gb:
            raise DiskFullError(f"free disk {free_gb:.1f} GB < {self.min_free_gb} GB — backfill paused")

    def fetch(self, f: VisionFile) -> bytes:
        self._check_disk()
        path = self.cache / f.key
        if path.exists():
            body = path.read_bytes()
        else:
            url = f"{self.base}/{f.key}"
            body = self.http.get(url).raise_for_status().content
            self.bytes_downloaded += len(body)
            if self.keep_zips:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(".part")
                tmp.write_bytes(body)
                tmp.replace(path)
        expected = self.http.get(f"{self.base}/{f.key}.CHECKSUM").raise_for_status().text.split()[0]
        if hashlib.sha256(body).hexdigest() != expected:
            path.unlink(missing_ok=True)
            raise ValueError(f"CHECKSUM mismatch for {f.key}")
        return body


# ---------------------------------------------------------------------- parsing
def _csv_batches(body: bytes, names: list[str], header: bool, block_mb: int) -> Iterator[pa.RecordBatch]:
    zf = zipfile.ZipFile(io.BytesIO(body))
    member = zf.open(zf.namelist()[0])
    # detect a header row regardless of what the market is documented to do
    first = member.peek(64)[:64] if hasattr(member, "peek") else b""
    has_header = header or (first[:1].isalpha())
    member.close()
    stream = zf.open(zf.namelist()[0])
    reader = pacsv.open_csv(
        stream,
        read_options=pacsv.ReadOptions(column_names=names, skip_rows=1 if has_header else 0,
                                       block_size=block_mb * 2**20),
        convert_options=pacsv.ConvertOptions(column_types={n: pa.string() for n in ("is_buyer_maker", "is_best_match")}),
    )
    for batch in reader:
        yield batch
    stream.close()


def _norm_ms(arr: pa.Array) -> np.ndarray:
    a = arr.to_numpy(zero_copy_only=False).astype(np.int64)
    return np.where(a > 10**14, a // 1000, a)


def _bool01(arr: pa.Array) -> np.ndarray:
    s = pc.utf8_lower(arr.cast(pa.string()))
    return pc.equal(s, "true").to_numpy(zero_copy_only=False).astype(np.int64)


def klines_rows(batch: pa.RecordBatch) -> list[tuple]:
    ot = _norm_ms(batch.column("open_time"))
    cols = [batch.column(n).to_numpy(zero_copy_only=False).astype(np.float64)
            for n in ("open", "high", "low", "close", "volume", "quote_volume")]
    trades = batch.column("trades").to_numpy(zero_copy_only=False).astype(np.int64)
    tb = batch.column("taker_buy_base").to_numpy(zero_copy_only=False).astype(np.float64)
    tq = batch.column("taker_buy_quote").to_numpy(zero_copy_only=False).astype(np.float64)
    return list(zip(ot.tolist(), *(c.tolist() for c in cols), trades.tolist(), tb.tolist(), tq.tolist()))


def agg_table(batch: pa.RecordBatch) -> pa.Table:
    return pa.table({
        "agg_id": batch.column("agg_id").cast(pa.int64()),
        "ts": pa.array(_norm_ms(batch.column("ts"))),
        "price": batch.column("price").cast(pa.float64()),
        "qty": batch.column("qty").cast(pa.float64()),
        "first_id": batch.column("first_id").cast(pa.int64()),
        "last_id": batch.column("last_id").cast(pa.int64()),
        "is_buyer_maker": pa.array(_bool01(batch.column("is_buyer_maker"))),
    })


# ---------------------------------------------------------------------- ingest one file
class VisionIngestor:
    def __init__(self, client: VisionClient, hot: SQLiteHotStore, cold: ParquetColdStore, *, block_mb: int = 32) -> None:
        self.client, self.hot, self.cold, self.block_mb = client, hot, cold, block_mb

    def done_periods(self, spec: TableSpec) -> set[str]:
        cols = self.hot.read_range(VISION_DONE, columns=["table_name", "period"])
        return {p for t, p in zip(cols["table_name"], cols["period"]) if t == spec.name}

    def _mark_done(self, spec: TableSpec, f: VisionFile, rows: int) -> None:
        self.hot.replace(VISION_DONE, [(spec.name, f.period, rows, now_ms())])

    def ingest_klines(self, inst: Instrument, market: Market, spec: TableSpec, f: VisionFile) -> int:
        body = self.client.fetch(f)
        n = rejected = 0
        for batch in _csv_batches(body, KLINE_COLS, market.vision_header, self.block_mb):
            rows = klines_rows(batch)
            res = validate_rows(spec, rows, venue=inst.venue)
            if res.rejected:
                rejected += res.n_rejected
                log.warning("%s %s: %d rows rejected, e.g. %s", spec.name, f.period, res.n_rejected, res.rejected[:2])
            n += self.hot.upsert(spec, res.good)
        self._mark_done(spec, f, n)
        return n

    def ingest_agg_trades(self, inst: Instrument, market: Market, spec: TableSpec, f: VisionFile) -> int:
        body = self.client.fetch(f)
        names = AGG_COLS_FUT if market.vision_header else AGG_COLS_SPOT
        pending: dict[dt.date, list[pa.Table]] = {}
        total = 0

        def flush(day: dt.date) -> int:
            parts = pending.pop(day)
            return self.cold.write_day(inst, spec, day, pa.concat_tables(parts), merge=True)

        for batch in _csv_batches(body, names, market.vision_header, self.block_mb):
            tbl = agg_table(batch)
            # validation (vectorised): price/qty > 0, first ≤ last, time range
            ok = pc.and_(pc.and_(pc.greater(tbl["price"], 0), pc.greater(tbl["qty"], 0)),
                         pc.less_equal(tbl["first_id"], tbl["last_id"]))
            bad = tbl.num_rows - pc.sum(ok.cast(pa.int64())).as_py()
            if bad:
                log.warning("%s %s: %d invalid aggTrades dropped (logged)", spec.name, f.period, bad)
                tbl = tbl.filter(ok)
            days = (tbl["ts"].to_numpy() // MS_PER_DAY)
            for dnum in np.unique(days):
                day = dt.date(1970, 1, 1) + dt.timedelta(days=int(dnum))
                pending.setdefault(day, []).append(tbl.filter(pa.array(days == dnum)))
            # days strictly before the newest day in this batch are complete (file is time-ordered)
            newest = dt.date(1970, 1, 1) + dt.timedelta(days=int(days.max())) if len(days) else None
            for day in sorted(d for d in pending if newest is not None and d < newest):
                total += flush(day)
        for day in sorted(pending):
            total += flush(day)
        self._mark_done(spec, f, total)
        return total

    def ingest_metrics(self, inst: Instrument, spec: TableSpec, f: VisionFile) -> int:
        body = self.client.fetch(f)
        with zipfile.ZipFile(io.BytesIO(body)) as z, z.open(z.namelist()[0]) as fh:
            import csv
            rows = [metrics_vision(r) for r in csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8"))]
        res = validate_rows(spec, rows, venue=inst.venue)
        n = self.hot.upsert(spec, res.good)
        self._mark_done(spec, f, n)
        return n


def choose_kline_source(tf: Timeframe, start_ms: int, end_ms: int, limit: int) -> str:
    """REST when it needs few requests (higher TFs), Vision otherwise (1m/5m/15m history)."""
    return "rest" if (end_ms - start_ms) / tf.ms / limit <= 200 else "vision"
