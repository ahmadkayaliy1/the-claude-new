"""Provider abstraction (spec §4.1): every provider takes (system, user, JSON schema) and returns an ``LLMResult``.

Structured output is requested natively where the provider supports it, with a provider-specific *transport*
schema (unsupported keywords stripped); the full contract — including numeric bounds and the semantic risk
rules — is always validated client-side afterwards (``ai/repair.py``). Nothing a model returns is trusted
until it passes that validation.
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import os
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal
from zoneinfo import ZoneInfo

from dotenv import dotenv_values

from ...core.settings import DEFAULT_ENV, AIProviderCfg
from ...core.timeutil import MS_PER_DAY, MS_PER_HOUR, iso, now_ms

SchemaFlavor = Literal["anthropic", "gemini", "openai_strict", "plain"]
_DROP_ALWAYS = {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf", "minLength", "maxLength",
                "pattern", "minItems", "maxItems", "default", "title"}


@dataclass
class LLMResult:
    provider: str
    model: str
    text: str
    data: Any | None
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    stop_reason: str | None = None
    request_id: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ImageInput:
    """One image sent with the user message (Phase 3 charts). ``label`` is the caption text sent before it;
    ``token_est`` the estimated input tokens (≈ width × height / 750)."""
    label: str
    data: bytes
    media_type: str = "image/png"
    token_est: int = 0


class ProviderError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool, rate_limited: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.rate_limited = rate_limited


class Refusal(ProviderError):
    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=False)


ENV_FILE = DEFAULT_ENV
DOTENV_TTL_S = 60.0
_dotenv: tuple[float, dict[str, str]] = (float("-inf"), {})


def secret(name: str | None) -> str | None:
    """A secret from the process environment, else from ``.env`` (re-read at most once a minute), so a key added
    to ``.env`` is picked up by running services without a restart (OPS-07). Values are never logged."""
    global _dotenv
    if not name:
        return None
    v = os.environ.get(name, "").strip()
    if v:
        return v
    t, vals = _dotenv
    if time.monotonic() - t > DOTENV_TTL_S:
        try:
            vals = {k: (x or "") for k, x in dotenv_values(ENV_FILE).items()} if ENV_FILE.exists() else {}
        except (OSError, ValueError):
            vals = {}
        _dotenv = (time.monotonic(), vals)
    return vals.get(name, "").strip() or None


def quota_day_start(ms: int, tz: str | None = None) -> int:
    """Start of the provider's quota day containing ``ms``: UTC midnight, or midnight in ``tz`` (Gemini resets
    its free-tier daily quota at midnight Pacific time)."""
    if not tz:
        return ms // MS_PER_DAY * MS_PER_DAY
    z = ZoneInfo(tz)
    d = dt.datetime.fromtimestamp(ms / 1000, z)
    return int(dt.datetime(d.year, d.month, d.day, tzinfo=z).timestamp() * 1000)


def quota_day_end(ms: int, tz: str | None = None) -> int:
    """When the quota day containing ``ms`` resets (DST-safe: days of 23–25 h)."""
    return quota_day_start(quota_day_start(ms, tz) + MS_PER_DAY + 3 * MS_PER_HOUR, tz)


def transport_schema(schema: dict, flavor: SchemaFlavor) -> dict:
    """Schema to send to the provider (constraints the provider cannot enforce are removed)."""
    s = copy.deepcopy(schema)

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            for k in list(node):
                if k in _DROP_ALWAYS:
                    node.pop(k)
            for k, v in list(node.items()):
                node[k] = walk(v)
            if node.get("type") == "object" or "properties" in node:
                node["additionalProperties"] = False
                if flavor == "openai_strict":
                    node["required"] = list(node.get("properties", {}))
            return node
        if isinstance(node, list):
            return [walk(x) for x in node]
        return node

    return walk(s)


_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.S)


def extract_json(text: str) -> Any:
    """Parse JSON from a model reply (tolerates code fences / leading prose); raises ValueError."""
    t = text.strip()
    m = _FENCE.match(t)
    if m:
        t = m.group(1)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        start, end = t.find("{"), t.rfind("}")
        if start >= 0 and end > start:
            return json.loads(t[start:end + 1])
        raise ValueError("no JSON object in model output") from None


class LLMProvider(ABC):
    def __init__(self, name: str, cfg: AIProviderCfg, model: str, api_key: str | None) -> None:
        self.name, self.cfg, self.model, self.api_key = name, cfg, model, api_key
        self.cooldown_until_ms = 0
        self.cooldown_reason = ""

    @abstractmethod
    async def _call(self, system: str, user: str, schema: dict | None, schema_name: str,
                    max_output_tokens: int) -> LLMResult: ...

    def cool_down(self, until_ms: int, reason: str) -> None:
        """Take the provider out of rotation until ``until_ms`` (auth/model errors, quota or usage limits): the
        orchestrator then routes to ``ai.fallback_provider`` and the engine reports the reason (F12)."""
        self.cooldown_until_ms, self.cooldown_reason = until_ms, reason

    def unavailable_reason(self) -> str | None:
        """Why the provider cannot take calls right now (e.g. not signed in, usage limit), or None."""
        if now_ms() < self.cooldown_until_ms:
            return f"{self.cooldown_reason} — retry after {iso(self.cooldown_until_ms)}"
        return None

    async def generate(self, *, system: str, user: str, schema: dict | None = None, schema_name: str = "output",
                       max_output_tokens: int | None = None) -> LLMResult:
        t0 = time.perf_counter()
        res = await self._call(system, user, schema, schema_name, max_output_tokens or self.cfg.max_output_tokens)
        res.latency_ms = int((time.perf_counter() - t0) * 1000)
        if res.data is None and res.text:
            try:
                res.data = extract_json(res.text)
            except ValueError:
                res.data = None
        res.cost_usd = self.cost_of(res)
        return res

    def prices(self) -> tuple[float, float]:
        """(input, output) USD per million tokens for the active model (0, 0 when unknown)."""
        if self.model in self.cfg.model_prices:
            p_in, p_out = self.cfg.model_prices[self.model]
            return float(p_in), float(p_out)
        return self.cfg.usd_per_mtok_in, self.cfg.usd_per_mtok_out

    def cost_of(self, res: LLMResult) -> float:
        if self.cfg.free_tier:
            return 0.0
        p_in, p_out = self.prices()
        uncached = max(res.input_tokens - res.cached_input_tokens, 0)
        return (uncached * p_in + res.cached_input_tokens * p_in * 0.1 + res.output_tokens * p_out) / 1e6

    @property
    def priced(self) -> bool:
        """True when the cost of a call is known (free tier or configured prices for the active model)."""
        p_in, p_out = self.prices()
        return self.cfg.free_tier or (p_in > 0 and p_out > 0)
