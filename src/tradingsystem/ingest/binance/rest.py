"""Async Binance REST client (spot + USDⓈ-M) with weight budgeting and ban-safe backoff (P3.1).

* Tracks ``X-MBX-USED-WEIGHT-1M`` and pauses before the configured per-minute budget is exceeded.
* 429 → honours ``Retry-After``; 418 (IP ban) → waits the full ``Retry-After`` and logs an error.
* Network errors / 5xx → exponential backoff (bounded). 4xx other than 429/418 → raised (caller bug).
* Keeps a local↔Binance clock offset (NTP-style, lowest-RTT sample).
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from ...core.timeutil import now_ms

log = logging.getLogger(__name__)


class BinanceHTTPError(RuntimeError):
    def __init__(self, status: int, body: str, url: str) -> None:
        super().__init__(f"HTTP {status} {url}: {body[:200]}")
        self.status, self.body = status, body


class RetriesExhausted(RuntimeError):
    """Rate-limit (429/418) retries used up — transient: the caller may retry later."""


class BinanceRest:
    def __init__(self, base_url: str, *, weight_budget_per_min: int = 3000, timeout_s: float = 20.0,
                 max_retries: int = 6) -> None:
        self.base = base_url.rstrip("/")
        self.budget = weight_budget_per_min
        self.max_retries = max_retries
        self._client = httpx.AsyncClient(timeout=timeout_s, headers={"User-Agent": "tradingsystem/0.1"})
        self._used = 0
        self._window = int(time.time() // 60)
        self._lock = asyncio.Lock()
        self.clock_offset_ms: float = 0.0     # Binance − local
        self.requests = 0
        self.errors = 0

    async def close(self) -> None:
        await self._client.aclose()

    async def _respect_budget(self, weight: int) -> None:
        async with self._lock:
            minute = int(time.time() // 60)
            if minute != self._window:
                self._window, self._used = minute, 0
            if self._used + weight > self.budget:
                wait = 60 - (time.time() % 60) + 0.5
                log.info("weight budget %d reached (used %d) — pausing %.1fs", self.budget, self._used, wait)
                await asyncio.sleep(wait)
                self._window, self._used = int(time.time() // 60), 0
            self._used += weight

    async def get(self, path: str, params: dict[str, Any] | None = None, *, weight: int = 1) -> Any:
        url = self.base + path
        delay = 1.0
        for attempt in range(self.max_retries + 1):
            await self._respect_budget(weight)
            try:
                r = await self._client.get(url, params=params)
                self.requests += 1
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                self.errors += 1
                if attempt == self.max_retries:
                    raise
                log.warning("GET %s failed (%r) — retry in %.0fs", path, exc, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            used = r.headers.get("x-mbx-used-weight-1m")
            if used is not None:
                async with self._lock:
                    self._used = max(self._used, int(used))
            if r.status_code == 200:
                return r.json()
            self.errors += 1
            if r.status_code in (429, 418):
                wait = float(r.headers.get("Retry-After", 60))
                (log.error if r.status_code == 418 else log.warning)(
                    "Binance %d on %s — backing off %.0fs", r.status_code, path, wait)
                await asyncio.sleep(wait + 1)
                continue
            if r.status_code >= 500 and attempt < self.max_retries:
                log.warning("GET %s → %d — retry in %.0fs", path, r.status_code, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            raise BinanceHTTPError(r.status_code, r.text, str(r.url))
        raise RetriesExhausted(f"GET {path}: retries exhausted")

    async def sync_clock(self, path: str = "/api/v3/time", samples: int = 3) -> float:
        best: tuple[float, int] | None = None
        for _ in range(samples):
            t0 = now_ms()
            data = await self.get(path)
            t1 = now_ms()
            if best is None or t1 - t0 < best[1]:
                best = (data["serverTime"] - (t0 + t1) / 2, t1 - t0)
        self.clock_offset_ms = best[0]
        return self.clock_offset_ms

    def binance_now(self) -> int:
        return int(now_ms() + self.clock_offset_ms)
