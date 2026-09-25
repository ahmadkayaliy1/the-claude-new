"""Validated generation with a bounded repair loop (P8.3).

call → parse JSON → validate against the pydantic contract (bounds + semantic risk rules) → on failure send
the exact validation errors back and ask for a corrected object (≤ ``max_repairs``). Provider errors that are
retryable back off; refusals and budget refusals end the attempt. Every call is recorded in ``ai_usage``.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Generic, TypeVar

from pydantic import BaseModel, ValidationError

from .budget import BudgetExceeded, CostGovernor, RateLimiter, UsageStore
from .providers.base import LLMProvider, LLMResult, ProviderError, Refusal

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)


@dataclass
class Generation(Generic[T]):
    ok: bool
    value: T | None
    attempts: list[LLMResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    refused: bool = False
    budget_blocked: bool = False

    @property
    def cost_usd(self) -> float:
        return sum(a.cost_usd for a in self.attempts)

    @property
    def raw_text(self) -> str:
        return self.attempts[-1].text if self.attempts else ""


def _format_errors(exc: ValidationError) -> str:
    lines = []
    for e in exc.errors()[:15]:
        loc = ".".join(str(x) for x in e.get("loc", ())) or "(root)"
        lines.append(f"- {loc}: {e.get('msg')}")
    return "\n".join(lines)


async def generate_validated(provider: LLMProvider, model_cls: type[T], *, system: str, user: str,
                             limiter: RateLimiter, governor: CostGovernor, usage: UsageStore, purpose: str,
                             pair: str | None = None, max_repairs: int = 2, max_retries: int = 3,
                             est_input_tokens: int = 8000) -> Generation[T]:
    gen: Generation[T] = Generation(False, None)
    schema = model_cls.model_json_schema()
    prompt = user
    repairs = retries = 0
    while True:
        try:
            governor.check(provider, est_input_tokens, provider.cfg.max_output_tokens)
            await limiter.acquire()
        except BudgetExceeded as exc:
            gen.budget_blocked = True
            gen.errors.append(str(exc))
            return gen
        try:
            res = await provider.generate(system=system, user=prompt, schema=schema, schema_name=model_cls.__name__)
        except Refusal as exc:
            usage.record(None, provider=provider.name, model=provider.model, purpose=purpose, pair=pair, ok=False,
                         error=f"refusal: {exc}")
            gen.refused = True
            gen.errors.append(str(exc))
            return gen
        except ProviderError as exc:
            usage.record(None, provider=provider.name, model=provider.model, purpose=purpose, pair=pair, ok=False,
                         error=str(exc)[:300])
            gen.errors.append(str(exc))
            if not exc.retryable or retries >= max_retries:
                return gen
            retries += 1
            await asyncio.sleep(min(60, (20 if exc.rate_limited else 2) * 2 ** (retries - 1)))
            continue
        gen.attempts.append(res)
        try:
            if res.data is None:
                raise ValueError("output is not valid JSON")
            value = model_cls.model_validate(res.data)
        except (ValidationError, ValueError) as exc:
            err = _format_errors(exc) if isinstance(exc, ValidationError) else f"- {exc}"
            usage.record(res, provider=provider.name, model=res.model, purpose=purpose, pair=pair, ok=False,
                         error=("invalid: " + err)[:300])
            gen.errors.append(err)
            if repairs >= max_repairs:
                return gen
            repairs += 1
            prev = json.dumps(res.data, ensure_ascii=False)[:6000] if res.data is not None else res.text[:6000]
            prompt = (f"{user}\n\n---\nYour previous answer was rejected by the validator:\n{err}\n\n"
                      f"Previous answer:\n{prev}\n\nReturn ONE corrected JSON object. Fix only what the validator "
                      "reports; do not invent data that is not in the inputs.")
            continue
        usage.record(res, provider=provider.name, model=res.model, purpose=purpose, pair=pair, ok=True)
        gen.ok, gen.value = True, value
        return gen
