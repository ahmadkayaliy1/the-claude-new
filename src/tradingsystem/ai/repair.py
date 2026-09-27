"""Validated generation with a bounded repair loop (P8.3).

call → parse JSON → validate against the pydantic contract (bounds + semantic risk rules) → on failure send
the exact validation errors back and ask for a corrected object (≤ ``max_repairs``). Transient provider errors
back off briefly and retry; rate limits, refusals, budget refusals, non-retryable provider errors and any
unexpected exception end the attempt (the next trigger tries again — no 20–60 s sleeps inside a cycle). Every
call is recorded in ``ai_usage`` (with its role and the images it carried).

Chart images (Phase 3) go with the first attempt and its retries only: a repair re-asks about the model's own
answer, the charts add nothing to that and would cost ≈ 2.3 k input tokens each time. When the provider cannot
take the images at all (``charts_disabled_cli_shape``) the same attempt is re-sent as text — the decision is never
lost because of the charts.
"""
from __future__ import annotations

import asyncio
import functools
import json
import logging
from dataclasses import dataclass, field
from typing import Generic, TypeVar

from pydantic import BaseModel, ValidationError

from .budget import BudgetExceeded, CostGovernor, RateLimiter, UsageStore
from .providers.base import CHARTS_DISABLED, ImageInput, LLMProvider, LLMResult, ProviderError, Refusal

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)
# appended to the user text when the charts could not be sent: the cycle header still says they are attached
NO_CHARTS_NOTE = ("\n\n(Note from the system: the chart images could not be attached to this message — work from "
                  "the payload numbers only.)")
# a repair goes out as text only (a fresh CLI session): tell the model why the charts named above are missing
CHARTS_SEEN_NOTE = ("(The chart images went with the first request only; your previous answer already reflects "
                    "them.)\n\n")


@dataclass
class Generation(Generic[T]):
    ok: bool
    value: T | None
    attempts: list[LLMResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    refused: bool = False
    budget_blocked: bool = False
    provider_error: str | None = None     # the call failed at the provider (→ status 'error', not 'invalid')
    images_sent: int = 0                  # chart images on the first attempt that reached the model
    charts_dropped: str | None = None     # why the charts were not sent after all (the call went out as text)

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
                             est_input_tokens: int = 8000, images: list[ImageInput] | None = None,
                             role: str | None = None) -> Generation[T]:
    """``images`` go with the first attempt (and its retries on transient provider errors) only; ``role`` (decision,
    escalation, …) is written on every ledger row, with the images that row's attempt carried."""
    gen: Generation[T] = Generation(False, None)
    schema = model_cls.model_json_schema()
    prompt = user
    first_text = user                              # the first attempt's text (gains a note if the charts are dropped)
    pending = list(images or [])                   # still to be sent: cleared after the first answer
    if pending and not getattr(provider, "supports_images", False):
        # a text-only provider (e.g. the fallback) must not read "Attached: N charts" without seeing them
        gen.charts_dropped = f"{provider.name} does not take images"
        pending = []
        first_text = prompt = first_text + NO_CHARTS_NOTE
    record = functools.partial(usage.record, provider=provider.name, purpose=purpose, pair=pair, role=role)
    repairs = retries = 0
    while True:
        # what this attempt really sends: a provider without image support drops them (base.usable_images)
        sent = pending if pending and getattr(provider, "supports_images", False) else []
        img_tok = sum(i.token_est for i in sent)
        img = {"images": len(sent), "image_tokens_est": img_tok}
        try:
            governor.check(provider, est_input_tokens + img_tok, provider.cfg.max_output_tokens)
            await limiter.acquire()
        except BudgetExceeded as exc:
            gen.budget_blocked = True
            gen.errors.append(str(exc))
            return gen
        kw = {"images": pending} if pending else {}
        try:
            res = await provider.generate(system=system, user=prompt, schema=schema, schema_name=model_cls.__name__,
                                          **kw)
        except Refusal as exc:
            record(None, model=provider.model, ok=False, error=f"refusal: {exc}", **img)
            gen.refused = True
            gen.errors.append(str(exc))
            return gen
        except ProviderError as exc:
            if sent and CHARTS_DISABLED in str(exc):
                # the CLI refused the image message at its input parser — no API request, so no ledger row (it would
                # eat the pair's daily cap): the same attempt goes out again as text
                log.warning("%s %s: %s — re-sending this attempt without the charts", pair or "*", purpose, exc)
                gen.charts_dropped = str(exc)[:300]
                pending = []
                first_text = prompt = first_text + NO_CHARTS_NOTE
                continue
            record(None, model=provider.model, ok=False, error=str(exc)[:300], **img)
            gen.errors.append(str(exc))
            gen.provider_error = str(exc)[:300]
            if not exc.retryable or exc.rate_limited or retries >= max_retries:
                return gen
            retries += 1
            await asyncio.sleep(2 * 2 ** (retries - 1))
            continue
        except Exception as exc:  # noqa: BLE001 — an SDK/transport surprise must not abort the whole cycle (F4)
            log.exception("%s: unexpected provider failure", provider.name)
            msg = f"{provider.name}: {type(exc).__name__}: {exc}"[:300]
            record(None, model=provider.model, ok=False, error=msg, **img)
            gen.errors.append(msg)
            gen.provider_error = msg
            return gen
        gen.attempts.append(res)
        gen.provider_error = None
        if pending:
            gen.images_sent = len(sent)
            pending = []                           # repairs re-ask about the answer: text only
        try:
            if res.data is None:
                raise ValueError("output is not valid JSON")
            value = model_cls.model_validate(res.data)
        except (ValidationError, ValueError) as exc:
            err = _format_errors(exc) if isinstance(exc, ValidationError) else f"- {exc}"
            record(res, model=res.model, ok=False, error=("invalid: " + err)[:300], **img)
            gen.errors.append(err)
            if repairs >= max_repairs:
                return gen
            repairs += 1
            prev = json.dumps(res.data, ensure_ascii=False)[:6000] if res.data is not None else res.text[:6000]
            seen = CHARTS_SEEN_NOTE if gen.images_sent else ""
            prompt = (f"{first_text}\n\n---\nYour previous answer was rejected by the validator:\n{err}\n\n"
                      f"Previous answer:\n{prev}\n\n{seen}Return ONE corrected JSON object. Fix only what the "
                      "validator reports; do not invent data that is not in the inputs.")
            continue
        record(res, model=res.model, ok=True, **img)
        gen.ok, gen.value = True, value
        return gen
