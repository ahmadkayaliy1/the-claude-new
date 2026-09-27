"""Usage gauge (Phase 4, §3.8 component 6): how much of the Claude subscription the system has used lately.

The subscription's real limits are not published, so the gauge is ledger-based: rolling 7-day and 5-hour sums of the
shared AI ledger (``ai_usage``: every decision, repair, escalation and operator session, of every instance) against
``ai.usage.weekly_token_budget`` and ``five_hour_token_budget`` — both calibration guesses until a week of data exists
(H21). Only the rows of the ``claude_code`` providers count (operator sessions are recorded under that provider's
name); a fallback provider's calls (e.g. Gemini while Claude is at its limit) do not use the subscription. A cache
read costs the subscription far less than fresh input, so it counts ``cache_read_weight`` of a token:

    effective = (input - cached) + cached * cache_read_weight + output         (input already contains cache reads)

Level 0 below ``level1_pct`` of both budgets, 1 (reviews and events only) at or above it in either window, 2 (events
only) at or above ``level2_pct``. A level is raised at its threshold but lowered only once the worse window is
``HYSTERESIS_PCT`` points below it (the gauge instance remembers its last level), so usage hovering at a threshold does
not flip the level — and its notifications — on every re-read. The engine rations by the level only while
``ai.usage.enforce`` is true; otherwise the gauge is only published (engine status, health report, review pack). The
CLI's own usage-limit cooldown stays the hard stop either way.

The engine asks every 10 s on its event loop, so the sums are cached for ``ai.usage.cache_s``; the gauge never raises
(an unreadable ledger is level 0 with the reason and ``error`` set — a gauge problem is never a reason to stop
analysing; it leaves the remembered level unchanged).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

from ..core.settings import Settings
from ..core.timeutil import MS_PER_DAY, MS_PER_HOUR, now_ms
from .budget import UsageStore

log = logging.getLogger(__name__)

WEEK_MS = 7 * MS_PER_DAY
FIVE_HOURS_MS = 5 * MS_PER_HOUR
LEVEL_TEXT = {0: "all triggers", 1: "reviews and events only", 2: "events only"}
HYSTERESIS_PCT = 5.0          # a level steps down only this many points below its threshold


@dataclass
class GaugeState:
    level: int            # 0 ok · 1 (≥ level1_pct: reviews/events only) · 2 (≥ level2_pct: events only); down with
    #                       hysteresis (HYSTERESIS_PCT)
    week_tokens: float
    week_pct: float
    five_h_tokens: float
    five_h_pct: float
    enforce: bool         # s.ai.usage.enforce — the engine rations only when True
    reason: str
    week_budget: int = 0
    five_h_budget: int = 0
    week_calls: int = 0
    five_h_calls: int = 0
    as_of_ms: int = 0
    error: str | None = None

    def as_detail(self) -> dict[str, Any]:
        """Flat and JSON-ready (engine status ``usage_gauge``, review pack, dashboard)."""
        return {"level": self.level, "enforce": self.enforce, "reason": self.reason,
                "week_tokens": int(round(self.week_tokens)), "week_pct": round(self.week_pct, 1),
                "five_h_tokens": int(round(self.five_h_tokens)), "five_h_pct": round(self.five_h_pct, 1),
                "week_budget": self.week_budget, "five_h_budget": self.five_h_budget,
                "week_calls": self.week_calls, "five_h_calls": self.five_h_calls,
                "as_of_ms": self.as_of_ms, **({"error": self.error} if self.error else {})}


def effective_tokens(sums: dict[str, int], cache_read_weight: float) -> float:
    """``tokens_since`` sums → weighted tokens (cache reads count ``cache_read_weight``)."""
    inp, cached, out = (max(int(sums.get(k) or 0), 0) for k in ("input", "cached", "output"))
    cached = min(cached, inp)                      # input contains the cache reads; never count them twice
    return (inp - cached) + cached * cache_read_weight + out


def _millions(n: float) -> str:
    return f"{n / 1e6:.2f} M"


def claude_providers(s: Settings) -> list[str]:
    """Names of the configured providers of kind ``claude_code``: the subscription's calls (the operator sessions'
    ledger rows carry that provider's name too — ``tools/operator/session_args.provider_name``)."""
    return [name for name, cfg in s.ai.providers.items() if cfg.kind == "claude_code"]


class UsageGauge:
    def __init__(self, s: Settings, usage: UsageStore, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.s, self.usage, self.clock = s, usage, clock
        self._cached: tuple[float, int | None, GaugeState] | None = None     # (clock, requested now_ms, state)
        self._last_error: str | None = None
        self._level: int | None = None        # the last level of a successful read (hysteresis; errors keep it)

    def state(self, now_ms: int | None = None) -> GaugeState:
        """The gauge now (or as of ``now_ms``), re-read at most every ``ai.usage.cache_s`` (an explicit ``now_ms``
        other than the cached one is always re-read). Never raises."""
        try:
            ttl = float(self.s.ai.usage.cache_s)
        except Exception:  # noqa: BLE001
            ttl = 60.0
        t = self.clock()
        c = self._cached
        if c is not None and t - c[0] < ttl and (now_ms is None or now_ms == c[1]):
            return c[2]
        st = self._compute(now_ms)
        self._cached = (t, now_ms, st)
        return st

    def _compute(self, as_of: int | None) -> GaugeState:
        now = as_of if as_of is not None else now_ms()
        enforce = False
        try:
            u = self.s.ai.usage
            enforce = bool(u.enforce)
            week_budget, five_budget = max(int(u.weekly_token_budget), 1), max(int(u.five_hour_token_budget), 1)
            providers = claude_providers(self.s)
            wk = self.usage.tokens_since(now - WEEK_MS, providers=providers)
            fh = self.usage.tokens_since(now - FIVE_HOURS_MS, providers=providers)
            week, five = effective_tokens(wk, u.cache_read_weight), effective_tokens(fh, u.cache_read_weight)
            week_pct, five_pct = 100.0 * week / week_budget, 100.0 * five / five_budget
            worst = max(week_pct, five_pct)
            limits = {1: float(u.level1_pct), 2: float(u.level2_pct)}
            raw = 2 if worst >= limits[2] else 1 if worst >= limits[1] else 0
            level = self._settle(raw, worst, limits)
            windows = (("7 d", week, week_pct, week_budget), ("5 h", five, five_pct, five_budget))
            if level == 0:
                reason = "within budget"
            elif level > raw:                                      # held by the hysteresis
                limit = limits[level]
                name, v, pct, b = max(windows, key=lambda w: w[2])
                reason = (f"{name} {_millions(v)} tokens = {pct:.0f} % of {_millions(b)} (level {level} held until "
                          f"≤ {limit - HYSTERESIS_PCT:g} % → {LEVEL_TEXT[level]})")
            else:
                limit = limits[level]
                over = [f"{name} {_millions(v)} tokens = {pct:.0f} % of {_millions(b)}"
                        for name, v, pct, b in windows if pct >= limit]
                reason = f"{'; '.join(over)} (≥ {limit:g} % → {LEVEL_TEXT[level]})"
            self._last_error, self._level = None, level
            return GaugeState(level, week, week_pct, five, five_pct, enforce, reason, week_budget=week_budget,
                              five_h_budget=five_budget, week_calls=int(wk.get("calls") or 0),
                              five_h_calls=int(fh.get("calls") or 0), as_of_ms=now)
        except Exception as exc:  # noqa: BLE001 — a gauge problem never stops the engine
            err = f"{type(exc).__name__}: {exc}"[:300]
            if err != self._last_error:
                log.warning("usage gauge unavailable: %s", err)
                self._last_error = err
            return GaugeState(0, 0.0, 0.0, 0.0, 0.0, enforce, f"gauge unavailable ({err})", as_of_ms=now, error=err)

    def _settle(self, raw: int, worst: float, limits: dict[int, float]) -> int:
        """The level with hysteresis: up as soon as ``raw`` (the thresholds alone) is higher than the last level; down
        one level at a time, each only while ``worst`` is at least ``HYSTERESIS_PCT`` points below that level's
        threshold (from level 2 at 92 % → 60 % the level goes straight to 0; at 87 % it stays 2, at 85 % it is 1)."""
        level = self._level
        if level is None or raw >= level:
            return raw
        while level > raw and worst <= limits[level] - HYSTERESIS_PCT:
            level -= 1
        return level


__all__ = ["GaugeState", "UsageGauge", "claude_providers", "effective_tokens", "HYSTERESIS_PCT", "LEVEL_TEXT"]
