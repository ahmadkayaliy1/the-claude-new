"""Binance Vision client (BF-02/04/05/09/10): retries, listing validation, streamed + resumed + verified downloads,
done-aware planning, and file-based ingestion — real Vision files served through ``httpx.MockTransport``."""
import datetime as dt
import hashlib
import io
import zipfile
from pathlib import Path

import httpx
import pyarrow.parquet as pq
import pytest

from tradingsystem.core.instruments import InstrumentRegistry
from tradingsystem.core.settings import load_settings
from tradingsystem.ingest.binance.backfill import BinanceBackfill
from tradingsystem.ingest.binance.markets import USDM
from tradingsystem.ingest.binance.vision import (VisionClient, VisionFile, VisionIngestor, VisionMissing,
                                                 VisionTransientError)
from tradingsystem.storage.parquet_store import ParquetColdStore
from tradingsystem.storage.sqlite_store import SQLiteHotStore
from tradingsystem.storage.tablespec import VISION_DONE, spec_for, system_specs

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "real"
BASE = "https://data.binance.vision"
ZIP_KEY = "data/futures/um/monthly/klines/XAUUSDT/1d/XAUUSDT-1d-2025-12.zip"
LIST_PREFIX = "data/futures/um/monthly/klines/XAUUSDT/1d/"
NS = "http://s3.amazonaws.com/doc/2006-03-01/"


class FakeClock:
    """Deterministic time for retry budgets: ``sleep`` advances ``now``."""

    def __init__(self) -> None:
        self.now, self.sleeps = 0.0, []

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.now += s

    def __call__(self) -> float:
        return self.now


class BrokenStream(httpx.SyncByteStream):
    """Body that drops the connection after ``data`` (a Wi-Fi reset mid-download)."""

    def __init__(self, data: bytes) -> None:
        self.data = data

    def __iter__(self):
        yield self.data
        raise httpx.ReadError("connection reset")


@pytest.fixture(scope="module")
def xau():
    reg = InstrumentRegistry.from_settings(load_settings(env_path=Path("nope.env")))
    return next(i for i in reg.all() if i.venue == "binance_usdm" and i.symbol == "XAUUSDT")


def client(tmp_path, handler, clock=None, **kw) -> VisionClient:
    clock = clock or FakeClock()
    return VisionClient(BASE, tmp_path / "cache", min_free_gb=0, transport=httpx.MockTransport(handler),
                        sleep=clock.sleep, clock=clock, **kw)


def listing(prefix: str, keys: list[str]) -> bytes:
    body = "".join(f"<Contents><Key>{k}</Key></Contents>" for k in keys)
    return (f'<?xml version="1.0" encoding="UTF-8"?><ListBucketResult xmlns="{NS}"><Prefix>{prefix}</Prefix>'
            f"<IsTruncated>false</IsTruncated>{body}</ListBucketResult>").encode()


def test_listing_retries_network_and_captive_portal_then_parses_real_page(tmp_path):
    real = (FIX / "vision_listing_xauusdt_1d.xml").read_bytes()
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        if len(calls) == 1:
            raise httpx.ConnectError("getaddrinfo failed")
        if len(calls) == 2:
            return httpx.Response(200, text="<html>hotel wifi login</html>")    # not a listing → retried
        assert req.url.params["prefix"] == LIST_PREFIX
        return httpx.Response(200, content=real)

    clock = FakeClock()
    c = client(tmp_path, handler, clock)
    keys = c.list_zips(LIST_PREFIX)
    assert len(keys) == 9 and all(k.endswith(".zip") for k in keys) and ZIP_KEY in keys
    assert len(clock.sleeps) == 2
    assert c.list_zips(LIST_PREFIX) == keys and len(calls) == 3          # cached for the pass


def test_listing_for_another_prefix_is_rejected(tmp_path):
    real = (FIX / "vision_listing_xauusdt_1d.xml").read_bytes()
    c = client(tmp_path, lambda req: httpx.Response(200, content=real), retry_deadline_s=20)
    with pytest.raises(VisionTransientError):
        c.list_zips("data/futures/um/monthly/klines/XAUUSDT/1h/")


def test_retry_budget_is_bounded_and_honours_retry_after(tmp_path):
    clock = FakeClock()
    c = client(tmp_path, lambda req: httpx.Response(503, headers={"Retry-After": "7"}), clock, retry_deadline_s=30)
    with pytest.raises(VisionTransientError):
        c.list_zips(LIST_PREFIX)
    assert clock.sleeps and set(clock.sleeps) == {7.0} and clock.now <= 30


def test_client_errors_are_not_retried(tmp_path):
    clock = FakeClock()
    c = client(tmp_path, lambda req: httpx.Response(404), clock)
    with pytest.raises(VisionMissing):
        c.list_zips(LIST_PREFIX)
    assert clock.sleeps == []


def vision_server(zip_bytes: bytes, checksum: str, log: list, *, drop_after: int | None = None):
    def handler(req: httpx.Request) -> httpx.Response:
        log.append((req.url.path, req.headers.get("range")))
        if req.url.path.endswith(".CHECKSUM"):
            return httpx.Response(200, text=f"{checksum}  {Path(req.url.path).name[:-9]}\n")
        rng = req.headers.get("range")
        if rng:
            start = int(rng.split("=")[1].rstrip("-"))
            return httpx.Response(206, content=zip_bytes[start:], headers={
                "content-range": f"bytes {start}-{len(zip_bytes) - 1}/{len(zip_bytes)}", "etag": '"e1"'})
        if drop_after is not None and sum(1 for p, r in log if not p.endswith(".CHECKSUM")) == 1:
            return httpx.Response(200, stream=BrokenStream(zip_bytes[:drop_after]),
                                  headers={"content-length": str(len(zip_bytes)), "etag": '"e1"'})
        return httpx.Response(200, content=zip_bytes, headers={"etag": '"e1"'})
    return handler


def test_download_checksum_first_resumes_after_drop_then_ingests_from_file(tmp_path, xau):
    zip_bytes = (FIX / "XAUUSDT-1d-2025-12.zip").read_bytes()
    checksum = (FIX / "XAUUSDT-1d-2025-12.zip.CHECKSUM").read_text().split()[0]
    assert hashlib.sha256(zip_bytes).hexdigest() == checksum
    log: list = []
    c = client(tmp_path, vision_server(zip_bytes, checksum, log, drop_after=500))
    hot = SQLiteHotStore(tmp_path / "hot.db")
    spec = spec_for(xau, "candles", xau.timeframes[5])                         # 1d
    hot.ensure_tables([spec, *system_specs()])
    ing = VisionIngestor(c, hot, ParquetColdStore(tmp_path / "cold"), block_bytes=1 << 20, use_threads=False)
    f = VisionFile(ZIP_KEY, "2025-12", True)
    n = ing.ingest_klines(xau, USDM, spec, f)
    assert log[0][0].endswith(".CHECKSUM")                                     # hash known before the body
    assert [r for _, r in log[1:]] == [None, "bytes=500-"]                     # resumed, not restarted
    assert n == 21 and hot.count(spec) == 21                                   # 2025-12-11 .. 2025-12-31
    assert ing.done_periods(spec) == {"2025-12"}
    assert c.bytes_downloaded == len(zip_bytes)
    assert not any((tmp_path / "cache").rglob("*.zip*"))                        # zip + .part dropped once done


def test_checksum_mismatch_twice_is_transient_and_leaves_nothing(tmp_path):
    zip_bytes = (FIX / "XAUUSDT-1d-2025-12.zip").read_bytes()
    log: list = []
    c = client(tmp_path, vision_server(zip_bytes, "0" * 64, log))
    with pytest.raises(VisionTransientError):
        c.download(VisionFile(ZIP_KEY, "2025-12", True))
    assert sum(1 for p, _ in log if p.endswith(".zip")) == 2                   # one refetch, then give up
    assert not any((tmp_path / "cache").rglob("*.zip")) and not any((tmp_path / "cache").rglob("*.part"))


def test_checksum_mismatch_in_every_pass_becomes_permanent(tmp_path):
    """Review fix: an object that never matches its CHECKSUM stops being retried every few minutes."""
    from tradingsystem.ingest.binance.backfill import is_transient
    from tradingsystem.ingest.binance.vision import MISMATCH_PASSES, VisionCorrupt
    zip_bytes = (FIX / "XAUUSDT-1d-2025-12.zip").read_bytes()
    c = client(tmp_path, vision_server(zip_bytes, "0" * 64, []))
    for _ in range(MISMATCH_PASSES - 1):
        with pytest.raises(VisionTransientError):
            c.download(VisionFile(ZIP_KEY, "2025-12", True))
    with pytest.raises(VisionCorrupt) as e:
        c.download(VisionFile(ZIP_KEY, "2025-12", True))
    assert not is_transient(e.value)


def test_missing_object_is_not_marked_done(tmp_path, xau):
    def handler(req):
        return httpx.Response(200, text="a" * 64) if req.url.path.endswith(".CHECKSUM") else httpx.Response(404)
    c = client(tmp_path, handler)
    hot = SQLiteHotStore(tmp_path / "hot.db")
    spec = spec_for(xau, "candles", xau.timeframes[5])
    hot.ensure_tables([spec, *system_specs()])
    ing = VisionIngestor(c, hot, ParquetColdStore(tmp_path / "cold"))
    with pytest.raises(VisionMissing):
        ing.ingest_klines(xau, USDM, spec, VisionFile(ZIP_KEY, "2025-12", True))
    assert hot.count(VISION_DONE) == 0


def test_plan_finishes_a_month_started_from_daily_files(tmp_path):
    """BF-10: a month partly ingested day by day is completed with daily files, not re-downloaded monthly."""
    mon = "data/futures/um/monthly/aggTrades/XAUUSDT/"
    day = "data/futures/um/daily/aggTrades/XAUUSDT/"
    months = [f"{mon}XAUUSDT-aggTrades-{m}.zip" for m in ("2026-01", "2026-02")]
    days = [f"{day}XAUUSDT-aggTrades-{d}.zip" for d in
            [(dt.date(2026, 2, 1) + dt.timedelta(i)).isoformat() for i in range(28)]]

    def handler(req):
        p = req.url.params["prefix"]
        return httpx.Response(200, content=listing(p, months if p == mon else days))

    c = client(tmp_path, handler)
    start, end = dt.date(2026, 1, 1), dt.date(2026, 3, 1)
    assert [f.period for f in c.plan(USDM, "aggTrades", "XAUUSDT", None, start, end)] == ["2026-01", "2026-02"]
    started = {"2026-02-01", "2026-02-02"}
    plan = [f.period for f in c.plan(USDM, "aggTrades", "XAUUSDT", None, start, end, started)]
    assert plan[0] == "2026-01" and "2026-02" not in plan and plan[1:] == [d[-14:-4] for d in days]
    full = {(dt.date(2026, 2, 1) + dt.timedelta(i)).isoformat() for i in range(28)}
    assert [f.period for f in c.plan(USDM, "aggTrades", "XAUUSDT", None, start, end, full)] == ["2026-01"]
    # the backfill's own filter agrees: a monthly file whose days are all done is covered
    assert BinanceBackfill._covered(VisionFile(months[1], "2026-02", True), full)
    assert not BinanceBackfill._covered(VisionFile(months[1], "2026-02", True), started)
    assert BinanceBackfill._covered(VisionFile(days[0], "2026-02-01", False), {"2026-02"})


def test_agg_trades_stream_into_day_files_and_rerun_is_a_no_op(tmp_path, real_aggtrades):
    """Real aggTrades re-packed as a Vision futures zip (header row); tiny blocks → many batches per day."""
    reg = InstrumentRegistry.from_settings(load_settings(env_path=Path("nope.env")))
    btc = reg.primary("BTCUSDT")
    spec = spec_for(btc, "agg_trades")
    lines = ["agg_trade_id,price,quantity,first_trade_id,last_trade_id,transact_time,is_buyer_maker"]
    cols = [real_aggtrades[c].to_pylist() for c in ("agg_id", "price", "qty", "first_id", "last_id", "ts",
                                                   "is_buyer_maker")]
    lines += [",".join(str(v).lower() if isinstance(v, bool) else repr(v) if isinstance(v, float) else str(v)
                       for v in row) for row in zip(*cols)]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("BTCUSDT-aggTrades-2026-09-18.csv", "\n".join(lines) + "\n")
    zip_bytes = buf.getvalue()
    log: list = []
    c = client(tmp_path, vision_server(zip_bytes, hashlib.sha256(zip_bytes).hexdigest(), log))
    hot = SQLiteHotStore(tmp_path / "hot.db")
    hot.ensure_tables([spec, *system_specs()])
    cold = ParquetColdStore(tmp_path / "cold")
    ing = VisionIngestor(c, hot, cold, block_bytes=16 * 1024, use_threads=False)
    key = "data/futures/um/daily/aggTrades/BTCUSDT/BTCUSDT-aggTrades-2026-09-18.zip"
    f = VisionFile(key, "2026-09-18", False)
    total = ing.ingest_agg_trades(btc, USDM, spec, f)
    days = cold.days(btc, spec)
    assert total == real_aggtrades.num_rows == sum(cold.day_rows(btc, spec, d) for d in days)
    back = cold.read_range(btc, spec)
    assert back["agg_id"].to_pylist() == real_aggtrades["agg_id"].to_pylist()
    assert back["ts"].to_pylist() == real_aggtrades["ts"].to_pylist()
    before = {d: cold.day_path(btc, spec, d).stat().st_mtime_ns for d in days}
    hot.delete_before(VISION_DONE, 2**62)                                      # crash before vision_done → rerun
    assert ing.ingest_agg_trades(btc, USDM, spec, f) == total
    assert {d: cold.day_path(btc, spec, d).stat().st_mtime_ns for d in days} == before   # superset kept, no rewrite
    assert all(pq.read_metadata(cold.day_path(btc, spec, d)).num_rows for d in days)
