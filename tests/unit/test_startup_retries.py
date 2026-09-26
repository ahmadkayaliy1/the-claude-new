"""Start-up while the network or MT5 is down: services wait and retry instead of exiting into a supervisor restart
loop (observed 2026-09-26 04:42 UTC: a restart during a 34-min DNS outage made ingest-binance and the executor exit
every ~70 s). Control flow only."""
import asyncio
from types import SimpleNamespace as NS

import httpx
import pytest

from tradingsystem.execution import executor as ex_mod
from tradingsystem.ingest.binance.service import BinanceLiveService
from tradingsystem.ingest.mt5.terminal import AccountMismatch, MT5Unavailable


class Status:
    def __init__(self):
        self.calls = []

    def set_status(self, name, state, **kw):
        self.calls.append((name, state, kw.get("error")))


def test_binance_clock_sync_retries_until_the_network_is_back():
    svc = object.__new__(BinanceLiveService)
    svc.appdb, fails = Status(), [httpx.ConnectError("[Errno 11001] getaddrinfo failed")] * 2
    calls = []

    async def sync_clock(path):
        calls.append(path)
        if fails:
            raise fails.pop()
        return 12.0

    async def go():
        svc.stop = asyncio.Event()
        await svc._sync_clock_until_ok("binance_spot", NS(sync_clock=sync_clock), first_delay_s=0.01)
    asyncio.run(go())
    assert len(calls) == 3 and [c[1] for c in svc.appdb.calls] == ["reconnecting", "reconnecting"]


def test_binance_clock_sync_gives_way_to_stop():
    svc = object.__new__(BinanceLiveService)
    svc.appdb = Status()

    async def sync_clock(path):
        raise httpx.ConnectError("down")

    async def go():
        svc.stop = asyncio.Event()
        asyncio.get_running_loop().call_later(0.05, svc.stop.set)
        await asyncio.wait_for(svc._sync_clock_until_ok("binance_usdm", NS(sync_clock=sync_clock), 0.01), 2)
    asyncio.run(go())


def test_executor_waits_for_mt5_at_start_up(monkeypatch):
    ex = object.__new__(ex_mod.Executor)
    ex.appdb = Status()
    monkeypatch.setattr(ex_mod.time, "sleep", lambda s: None)
    left = [MT5Unavailable("initialize failed: (-10005, 'IPC timeout')")] * 3
    term = NS(connect=lambda: (_ for _ in ()).throw(left.pop()) if left else "ok")
    ex._connect_mt5(term)
    assert [c[1] for c in ex.appdb.calls] == ["reconnecting"] * 3


def test_executor_account_mismatch_still_raises(monkeypatch):
    ex = object.__new__(ex_mod.Executor)
    ex.appdb = Status()
    monkeypatch.setattr(ex_mod.time, "sleep", lambda s: None)

    def connect():
        raise AccountMismatch("terminal is on another server")
    with pytest.raises(AccountMismatch):
        ex._connect_mt5(NS(connect=connect))
