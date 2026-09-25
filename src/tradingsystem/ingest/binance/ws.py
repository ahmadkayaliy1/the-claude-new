"""Binance combined-stream WebSocket connection with reconnect, stall watchdog and planned 23 h rotation (P3.5).

``on_message(stream, data, recv_ms)`` is called for every message; ``on_state(event, detail, outage_ms)``
reports connect/disconnect so the service can log outages and trigger gap-fill.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable

import orjson
import websockets

from ...core.timeutil import now_ms

log = logging.getLogger(__name__)

OnMessage = Callable[[str, dict, int], None]
OnState = Callable[[str, str, int | None], Awaitable[None]]


class StreamConnection:
    def __init__(self, name: str, base_url: str, streams: list[str], on_message: OnMessage, on_state: OnState, *,
                 stall_timeout_s: float = 30.0, max_age_s: float = 23 * 3600) -> None:
        if not streams:
            raise ValueError(f"{name}: no streams")
        self.name = name
        self.url = f"{base_url.rstrip('/')}/stream?streams=" + "/".join(streams)
        self.on_message, self.on_state = on_message, on_state
        self.stall_timeout_s, self.max_age_s = stall_timeout_s, max_age_s
        self.messages = 0
        self.reconnects = 0
        self.last_msg_ms: int | None = None
        self.connected = False

    async def run(self, stop: asyncio.Event) -> None:
        backoff = 1.0
        down_since: int | None = now_ms()
        first = True
        while not stop.is_set():
            try:
                async with websockets.connect(self.url, open_timeout=20, ping_interval=20, ping_timeout=20,
                                              max_size=2**22, close_timeout=5) as ws:
                    self.connected = True
                    outage = None if first else (now_ms() - down_since if down_since else None)
                    await self.on_state("connect", self.url.split("?")[0], outage)
                    first, down_since, backoff = False, None, 1.0
                    opened = time.monotonic()
                    while not stop.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=self.stall_timeout_s)
                        except asyncio.TimeoutError:
                            raise ConnectionError(f"stalled: no message for {self.stall_timeout_s:.0f}s")
                        recv = now_ms()
                        msg = orjson.loads(raw)
                        self.messages += 1
                        self.last_msg_ms = recv
                        try:
                            self.on_message(msg["stream"], msg["data"], recv)
                        except Exception:  # noqa: BLE001 — one bad message must not kill the feed
                            log.exception("%s: handler failed for %s", self.name, msg.get("stream"))
                        if time.monotonic() - opened > self.max_age_s:
                            log.info("%s: planned reconnect (connection age)", self.name)
                            break
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.reconnects += 1
                if down_since is None:
                    down_since = now_ms()
                log.warning("%s disconnected: %r — retry in %.0fs", self.name, exc, backoff)
                await self.on_state("disconnect", repr(exc)[:300], None)
            finally:
                if self.connected:
                    self.connected = False
                    if down_since is None:
                        down_since = now_ms()
            if stop.is_set():
                break
            try:
                await asyncio.wait_for(stop.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, 60)
