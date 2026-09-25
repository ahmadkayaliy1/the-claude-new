"""Provider abstraction (spec §4.1): every provider takes (system, user, JSON schema) and returns an ``LLMResult``.

Structured output is requested natively where the provider supports it, with a provider-specific *transport*
schema (unsupported keywords stripped); the full contract — including numeric bounds and the semantic risk
rules — is always validated client-side afterwards (``ai/repair.py``). Nothing a model returns is trusted
until it passes that validation.
"""
from __future__ import annotations

import copy
import json
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal

from ...core.settings import AIProviderCfg

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


class ProviderError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool, rate_limited: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.rate_limited = rate_limited


class Refusal(ProviderError):
    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=False)


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

    @abstractmethod
    async def _call(self, system: str, user: str, schema: dict | None, schema_name: str,
                    max_output_tokens: int) -> LLMResult: ...

    def unavailable_reason(self) -> str | None:
        """Why the provider cannot take calls right now (e.g. not signed in, usage limit), or None."""
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
