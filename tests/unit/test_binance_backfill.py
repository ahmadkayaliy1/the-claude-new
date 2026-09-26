"""Binance backfill pass control (BF-01/02/03/12, F4): a pass never kills the worker, a network outage ends it at
once (retried within minutes), failures are classified, and backfill REST calls ride out long outages.
Pure logic — requests are answered by ``httpx.MockTransport`` / fakes, no market data."""
import asyncio
import json
import sqlite3
import types
from pathlib import Path

import httpx
import pytest

from tradingsystem.core.instruments import InstrumentRegistry
from tradingsystem.core.settings import load_settings
from tradingsystem.ingest.binance import rest as rest_mod
from tradingsystem.ingest.binance.backfill import COLLECTOR, BinanceBackfill, NetworkDown, is_transient
from tradingsystem.ingest.binance.rest import BinanceHTTPError, BinanceRest, RetriesExhausted
from tradingsystem.ingest.binance.vision import VisionClient, VisionMissing, VisionTransientError
from tradingsystem.ingest.common.appdb import AppDB
from tradingsystem.ingest.common.backfill_loop import StepIncomplete
from tradingsystem.storage.parquet_store import ParquetColdStore


class FakeRest:
    def __init__(self, fail: Exception | None = None) -> None:
        self.fail, self.closed, self.base = fail, False, "https://api.binance.com"

    async def sync_clock(self, path: str) -> float:
        if self.fail:
            raise self.fail
        return 0.0

    async def close(self) -> None:
        self.closed = True


def make(tmp_path, *, rest_fail=None, network=True, instruments=()) -> BinanceBackfill:
    s = load_settings(env_path=Path("nope.env"))

    def handler(req: httpx.Request) -> httpx.Response:
        if not network:
            raise httpx.ConnectError("getaddrinfo failed")
        return httpx.Response(200)

    b = object.__new__(BinanceBackfill)
    b.s, b.data = s, tmp_path
    b.appdb = AppDB(tmp_path / "app.db")
    b.cold = ParquetColdStore(tmp_path / "cold")
    b.vision = VisionClient("https://data.binance.vision", tmp_path / "vc", min_free_gb=0,
                            transport=httpx.MockTransport(handler))
    b.instruments = list(instruments)
    b.rest = {"binance_spot": FakeRest(rest_fail), "binance_usdm": FakeRest(rest_fail)}
    b.progress, b._first_bar = {}, {}
    b.vision.beat = b._vision_beat

    async def no_listing(inst):
        return None

    b.discover_listing = no_listing
    return b


def status(tmp_path) -> tuple[dict, list[str]]:
    db = AppDB(tmp_path / "app.db")
    row = next(r for r in db.statuses() if r["collector"] == COLLECTOR)
    with db._lock:
        ev = [r[0] for r in db._con.execute("SELECT event FROM ingestion_events WHERE collector=?", (COLLECTOR,))]
    return row, ev


def test_classification():
    for exc in (httpx.ConnectError("x"), httpx.RemoteProtocolError("x"), VisionTransientError("x"),
                RetriesExhausted("x"), TimeoutError("lock busy"), NetworkDown("x"),
                BinanceHTTPError(503, "", "u"), BinanceHTTPError(429, "", "u"),
                sqlite3.OperationalError("database is locked")):
        assert is_transient(exc), exc
    for exc in (BinanceHTTPError(400, "", "u"), VisionMissing("404"), ValueError("bad row"),
                sqlite3.OperationalError("no such column")):
        assert not is_transient(exc), exc
    assert is_transient(StepIncomplete("x", {"a": ValueError(), "b": httpx.ReadTimeout("t")}))
    assert not is_transient(StepIncomplete("x", {"a": ValueError()}))


def test_network_error_while_preparing_fails_the_pass_not_the_worker(tmp_path):
    """BF-03: sync_clock at pass start used to raise out of the worker process."""
    b = make(tmp_path, rest_fail=httpx.ConnectError("getaddrinfo failed"))
    res = asyncio.run(b.run())
    assert "prepare" in res.transient and not res.ok
    row, ev = status(tmp_path)
    assert row["state"] == "reconnecting" and "done" not in ev
    assert all(r.closed for r in b.rest.values())


def test_network_outage_aborts_the_pass_after_the_first_failed_unit(tmp_path):
    insts = [i for i in InstrumentRegistry.from_settings(load_settings(env_path=Path("nope.env"))).all()
             if i.venue.startswith("binance")]
    b = make(tmp_path, network=False, instruments=insts)
    calls = []

    async def candles(inst, hot):
        calls.append(inst.key)
        raise httpx.ConnectError("getaddrinfo failed")

    async def other(inst, hot):
        calls.append(("other", inst.key))

    b.candles, b.funding, b.metrics, b.agg_trades = candles, other, other, other
    res = asyncio.run(b.run())
    assert calls == [insts[0].key]                                            # nothing else burned its budget
    assert "network" in res.transient and f"candles {insts[0].key}" in res.transient
    row, ev = status(tmp_path)
    assert row["state"] == "reconnecting" and "done" not in ev
    assert json.loads(row["detail"])["failed"]


def test_one_bad_unit_does_not_stop_the_pass_when_the_network_is_up(tmp_path):
    insts = [i for i in InstrumentRegistry.from_settings(load_settings(env_path=Path("nope.env"))).all()
             if i.venue.startswith("binance")]
    b = make(tmp_path, network=True, instruments=insts)
    calls = []

    async def candles(inst, hot):
        calls.append(inst.key)
        if inst is insts[0]:
            raise httpx.ReadTimeout("slow file")

    async def other(inst, hot):
        return None

    b.candles, b.funding, b.metrics, b.agg_trades = candles, other, other, other
    res = asyncio.run(b.run())
    assert calls == [i.key for i in insts] and list(res.transient) == [f"candles {insts[0].key}"]


def test_clean_pass_is_done_and_clears_the_error(tmp_path):
    b = make(tmp_path)
    b.appdb.set_status(COLLECTOR, "error", error="old")
    res = asyncio.run(b.run())
    assert res.ok
    row, ev = status(tmp_path)
    assert row["state"] == "stopped" and row["last_error"] is None and ev[-1] == "done"


def test_rest_retry_deadline_outlasts_the_default_budget(monkeypatch):
    """BF-12: live keeps 7 attempts; the backfill client retries a dead network until its deadline."""
    clock = types.SimpleNamespace(t=0.0)

    async def fake_sleep(s):
        clock.t += s

    attempts = {"n": 0}

    def handler(req):
        attempts["n"] += 1
        raise httpx.ConnectError("getaddrinfo failed")

    async def go(r: BinanceRest):
        await r._client.aclose()
        r._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            await r.get("/api/v3/ping")
        finally:
            await r._client.aclose()

    live, bf = BinanceRest("https://x"), BinanceRest("https://x", retry_deadline_s=900)
    monkeypatch.setattr(rest_mod, "asyncio", types.SimpleNamespace(sleep=fake_sleep, Lock=asyncio.Lock))
    monkeypatch.setattr(rest_mod, "time", types.SimpleNamespace(monotonic=lambda: clock.t, time=lambda: 0.0))
    with pytest.raises(httpx.ConnectError):
        asyncio.run(go(live))
    assert attempts["n"] == 7 and clock.t == 63
    attempts["n"], clock.t = 0, 0.0
    with pytest.raises(httpx.ConnectError):
        asyncio.run(go(bf))
    assert attempts["n"] > 7 and 800 < clock.t <= 900
