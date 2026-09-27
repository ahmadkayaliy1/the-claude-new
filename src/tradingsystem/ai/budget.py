"""AI usage accounting, rate limiting and the ROI-based Cost Governor (P8.3, D-004).

* Every call is recorded in ``app.db:ai_usage`` (tokens, cost, latency, outcome) — survives restarts, so the
  free-tier daily quota (RPD) and USD caps are enforced across process restarts.
* ``RateLimiter``: requests-per-minute spacing + requests-per-day from the persisted log (the day follows the
  provider's own reset: ``quota_reset_tz``, e.g. midnight Pacific for Gemini).
* ``CostGovernor``: blocks calls whose cost is unknown or would exceed the daily/monthly USD caps, and
  degrades the AI workload when AI cost exceeds ``max_ai_cost_to_profit_ratio`` of realised profit over the
  rolling window: level 0 configured mode → 1 ``agent_per_pair`` → 2 setup-event triggers only → 3 paused
  (cycles return NO_TRADE with the reason).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..core.settings import AIBudgetCfg, AIProviderCfg
from ..core.timeutil import MS_PER_DAY, now_ms
from ..storage.sqlite_store import connect
from .providers.base import LLMProvider, LLMResult, quota_day_start

log = logging.getLogger(__name__)

_DDL = [
    """CREATE TABLE IF NOT EXISTS ai_usage (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL,
        purpose TEXT, pair TEXT, input_tokens INTEGER, output_tokens INTEGER, cached_tokens INTEGER,
        cost_usd REAL, latency_ms INTEGER, ok INTEGER NOT NULL, error TEXT, request_id TEXT)""",
    "CREATE INDEX IF NOT EXISTS ai_usage_ts ON ai_usage(ts)",
]


class BudgetExceeded(RuntimeError):
    """A call was refused by the rate limiter or the Cost Governor (the cycle must fall back to NO_TRADE)."""


def usage_db(settings) -> Path:
    """Where AI usage is recorded: one ledger shared by every instance (the subscription's limits and the daily
    request cap are per account, not per pair — D-042), or the single system's app.db."""
    if settings.paths.instance:
        settings.paths.shared().mkdir(parents=True, exist_ok=True)
        return settings.paths.shared() / "ai_usage.db"
    return settings.paths.state() / "app.db"


EXTRA_COLUMNS = (("api_equivalent_usd", "REAL"), ("num_turns", "INTEGER"), ("cache_creation_tokens", "INTEGER"),
                 # Phase 3: which role made the call, and the chart images it carried (estimated tokens)
                 ("role", "TEXT"), ("images", "INTEGER"), ("image_tokens_est", "INTEGER"))


class UsageStore:
    def __init__(self, app_db: Path) -> None:
        self._con = connect(app_db, cache_mb=2)
        self._lock = threading.Lock()
        with self._lock:
            for s in _DDL:
                self._con.execute(s)
            have = {r[1] for r in self._con.execute("PRAGMA table_info(ai_usage)")}
            for col, typ in EXTRA_COLUMNS:      # added in Phase 1; several services open the store concurrently
                if col not in have:
                    try:
                        self._con.execute(f"ALTER TABLE ai_usage ADD COLUMN {col} {typ}")
                    except sqlite3.OperationalError as exc:
                        if "duplicate column" not in str(exc):
                            raise

    def record(self, res: LLMResult | None, *, provider: str, model: str, purpose: str, pair: str | None,
               ok: bool, error: str | None = None, role: str | None = None, images: int = 0,
               image_tokens_est: int = 0) -> None:
        x = (res.extra if res else None) or {}
        with self._lock:
            self._con.execute(
                "INSERT INTO ai_usage(ts, provider, model, purpose, pair, input_tokens, output_tokens, cached_tokens, "
                "cost_usd, latency_ms, ok, error, request_id, api_equivalent_usd, num_turns, cache_creation_tokens, "
                "role, images, image_tokens_est) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (now_ms(), provider, model, purpose, pair, res.input_tokens if res else 0, res.output_tokens if res else 0,
                 res.cached_input_tokens if res else 0, res.cost_usd if res else 0.0, res.latency_ms if res else 0,
                 int(ok), error, res.request_id if res else None, x.get("api_equivalent_usd"), x.get("num_turns"),
                 x.get("cache_creation_input_tokens"), role, int(images), int(image_tokens_est)))

    def count_since(self, provider: str, since_ms: int, pair: str | None = None) -> int:
        """Requests of ``provider`` since ``since_ms`` (the quota ladder). Operator sessions (roles review /
        diagnose, Phase 4) are recorded for the usage gauge but do not use the pairs' request quota."""
        sql, args = ("SELECT count(*) FROM ai_usage WHERE provider=? AND ts>=? "
                     "AND COALESCE(role, '') NOT IN ('review', 'diagnose')", [provider, since_ms])
        if pair:
            sql, args = sql + " AND pair=?", args + [pair]
        with self._lock:
            return int(self._con.execute(sql, args).fetchone()[0])

    def count_role_since(self, role: str, since_ms: int, pair: str | None = None) -> int:
        """Ledger rows of one role (e.g. 'escalation') since ``since_ms`` — for per-role daily limits."""
        sql, args = "SELECT count(*) FROM ai_usage WHERE role=? AND ts>=?", [role, since_ms]
        if pair:
            sql, args = sql + " AND pair=?", args + [pair]
        with self._lock:
            return int(self._con.execute(sql, args).fetchone()[0])

    def cost_since(self, since_ms: int, pair: str | None = None) -> float:
        sql, args = "SELECT COALESCE(sum(cost_usd),0) FROM ai_usage WHERE ts>=?", [since_ms]
        if pair:
            sql, args = sql + " AND pair=?", args + [pair]
        with self._lock:
            return float(self._con.execute(sql, args).fetchone()[0])

    def tokens_since(self, since_ms: int, pair: str | None = None) -> dict[str, int]:
        """Token sums of every ledger row since ``since_ms`` (failed calls too: they spent the subscription) — the
        usage gauge (Phase 4). ``input`` already contains cache reads and writes, ``cached`` is the cache-read part."""
        sql = ("SELECT COALESCE(sum(COALESCE(input_tokens,0)),0), COALESCE(sum(COALESCE(cached_tokens,0)),0), "
               "COALESCE(sum(COALESCE(output_tokens,0)),0), count(*) FROM ai_usage WHERE ts>=?")
        args: list = [since_ms]
        if pair:
            sql, args = sql + " AND pair=?", args + [pair]
        with self._lock:
            inp, cached, out, calls = self._con.execute(sql, args).fetchone()
        return {"input": int(inp), "cached": int(cached), "output": int(out), "calls": int(calls)}

    def close(self) -> None:
        with self._lock:
            self._con.close()


def utc_midnight(ms: int) -> int:
    return ms // MS_PER_DAY * MS_PER_DAY


def month_start(ms: int) -> int:
    d = dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc)
    return int(dt.datetime(d.year, d.month, 1, tzinfo=dt.timezone.utc).timestamp() * 1000)


class RateLimiter:
    """Per-provider RPM spacing (in-process) + RPD (persisted). ``instance`` (one system per pair, D-042/D-043): the
    ledger is shared, ``rpd`` counts every instance's calls, and this pair may make at most ``daily_cap`` calls a
    day — every ledger row counts (first attempts, repairs, escalations: each one spends the subscription)."""

    def __init__(self, name: str, cfg: AIProviderCfg, usage: UsageStore, *, instance: str | None = None,
                 daily_cap: int | None = None) -> None:
        self.name, self.cfg, self.usage = name, cfg, usage
        self.instance, self.daily_cap = instance, daily_cap
        self._last: list[float] = []
        self._lock = asyncio.Lock()

    def cap(self) -> int | None:
        """This system's daily cap: ``rpd`` for the all-pairs system; for one pair ``min(rpd, daily_cap)``."""
        caps = [c for c in (self.cfg.rpd, self.daily_cap if self.instance else None) if c]
        return min(caps) if caps else None

    def remaining_today(self) -> int | None:
        if self.cap() is None:
            return None
        day0 = quota_day_start(now_ms(), self.cfg.quota_reset_tz)
        lefts = []
        if self.cfg.rpd:
            lefts.append(max(self.cfg.rpd - self.usage.count_since(self.name, day0), 0))
        if self.instance and self.daily_cap:
            lefts.append(max(self.daily_cap - self.usage.count_since(self.name, day0, pair=self.instance), 0))
        return min(lefts) if lefts else None

    async def acquire(self) -> None:
        left = self.remaining_today()
        if left is not None and left <= 0:
            mine = f", {self.instance}: {self.daily_cap}/day" if self.instance and self.daily_cap else ""
            raise BudgetExceeded(f"{self.name}: daily request quota ({self.cfg.rpd}{mine}) used up")
        if not self.cfg.rpm:
            return
        async with self._lock:
            now = time.monotonic()
            self._last = [t for t in self._last if now - t < 60]
            if len(self._last) >= self.cfg.rpm:
                wait = 60 - (now - self._last[0]) + 0.1
                log.info("%s: RPM limit %d — waiting %.1fs", self.name, self.cfg.rpm, wait)
                await asyncio.sleep(wait)
            self._last.append(time.monotonic())


@dataclass
class GovernorState:
    level: int
    reason: str
    spend_today: float
    spend_month: float
    window_cost: float
    window_profit: float | None
    window_trades: int


LEVELS = {0: "configured mode", 1: "agent_per_pair", 2: "setup-event triggers only", 3: "paused (NO_TRADE)"}


class CostGovernor:
    """``profit_fn(since_ms) -> (realised_profit_usd, n_closed_trades)`` comes from the execution layer
    (paper/demo outcomes during testing, D-004)."""

    def __init__(self, cfg: AIBudgetCfg, usage: UsageStore,
                 profit_fn: Callable[[int], tuple[float, int]] | None = None, instance: str | None = None) -> None:
        """``instance`` (one system per pair, D-042): the cost-to-profit ratio compares this pair's AI cost with this
        pair's profit; the daily / monthly USD caps stay account-wide (the shared ledger)."""
        self.cfg, self.usage, self.profit_fn, self.instance = cfg, usage, profit_fn, instance

    def state(self) -> GovernorState:
        now = now_ms()
        today = self.usage.cost_since(utc_midnight(now))
        month = self.usage.cost_since(month_start(now))
        since = now - self.cfg.rolling_window_days * MS_PER_DAY
        wcost = self.usage.cost_since(since, pair=self.instance)
        profit, trades = self.profit_fn(since) if self.profit_fn else (None, 0)
        level, reason = 0, "within budget"
        if trades >= self.cfg.min_trades_for_ratio and wcost > 0:
            if profit is None or profit <= 0:
                level, reason = 2, f"AI cost ${wcost:.2f} with no realised profit over {self.cfg.rolling_window_days} d"
            else:
                ratio = wcost / profit
                if ratio > 2 * self.cfg.max_ai_cost_to_profit_ratio:
                    level, reason = 2, f"AI cost/profit {ratio:.2f} > 2× limit {self.cfg.max_ai_cost_to_profit_ratio}"
                elif ratio > self.cfg.max_ai_cost_to_profit_ratio:
                    level, reason = 1, f"AI cost/profit {ratio:.2f} > limit {self.cfg.max_ai_cost_to_profit_ratio}"
        if self.cfg.monthly_usd_cap > 0 and month >= self.cfg.monthly_usd_cap:
            level, reason = 3, f"monthly AI cap ${self.cfg.monthly_usd_cap:.2f} reached"
        return GovernorState(level, reason, today, month, wcost, profit, trades)

    def check(self, provider: LLMProvider, est_input_tokens: int, est_output_tokens: int) -> None:
        """Raise BudgetExceeded if this call may not be made."""
        if provider.cfg.free_tier:
            return
        if not provider.priced:
            raise BudgetExceeded(f"{provider.name}/{provider.model}: price unknown — configure model_prices first")
        p_in, p_out = provider.prices()
        est = (est_input_tokens * p_in + est_output_tokens * p_out) / 1e6
        now = now_ms()
        today = self.usage.cost_since(utc_midnight(now))
        if today + est > self.cfg.daily_usd_cap:
            raise BudgetExceeded(f"daily AI cap ${self.cfg.daily_usd_cap:.2f} would be exceeded "
                                 f"(spent ${today:.4f}, next call ≈ ${est:.4f})")
        month = self.usage.cost_since(month_start(now))
        if self.cfg.monthly_usd_cap > 0 and month + est > self.cfg.monthly_usd_cap:
            raise BudgetExceeded(f"monthly AI cap ${self.cfg.monthly_usd_cap:.2f} would be exceeded")
        if self.state().level >= 3:
            raise BudgetExceeded("Cost Governor paused AI calls")


def is_budget_error(exc: BaseException) -> bool:
    return isinstance(exc, BudgetExceeded)


__all__ = ["BudgetExceeded", "UsageStore", "RateLimiter", "CostGovernor", "GovernorState", "LEVELS",
           "is_budget_error", "sqlite3"]
