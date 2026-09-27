"""Google Gemini via the official ``google-genai`` SDK (``ai.fallback_provider`` since D-030).

* Thinking depth comes from ``effort`` (Gemini 3 thinking level); thinking tokens share ``max_output_tokens``.
* Every request is bounded (``timeout_s``); network/timeout failures become retryable ``ProviderError``s.
* A truncated answer (MAX_TOKENS) or a blocked prompt is never re-sent. Auth / model / region errors and an
  exhausted daily quota put the provider on cooldown, so the orchestrator routes to the fallback and the engine
  reports the reason instead of storing one failed call per trigger (F5, F8, F12).
"""
from __future__ import annotations

import asyncio
import json

import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from ...core.timeutil import now_ms
from .base import LLMProvider, LLMResult, ProviderError, Refusal, quota_day_end, transport_schema

try:                                    # the SDK may use either HTTP stack
    import httpx2
    _TRANSPORT: tuple[type[BaseException], ...] = (httpx.RequestError, httpx2.RequestError)
except ImportError:                     # pragma: no cover
    _TRANSPORT = (httpx.RequestError,)

FATAL_COOLDOWN_MS = 60 * 60_000         # bad key / unknown model / region or billing precondition
RATE_COOLDOWN_MS = 60_000               # per-minute quota: skip this cycle, the next trigger tries again
_BLOCKED = ("SAFETY", "PROHIBITED", "BLOCKLIST", "RECITATION", "SPII", "LANGUAGE", "OTHER")


class GeminiProvider(LLMProvider):
    supports_images = False     # fallback provider: chart images are dropped (one warning), the call goes out as text

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        if not self.api_key:
            raise ProviderError(f"{self.name}: missing API key ({self.cfg.api_key_env})", retryable=False)
        lvl = (self.cfg.effort or "low").upper()
        if lvl not in types.ThinkingLevel.__members__ or lvl == "THINKING_LEVEL_UNSPECIFIED":
            raise ProviderError(f"{self.name}: unsupported effort {self.cfg.effort!r} (use low | medium | high)",
                                retryable=False)
        self.thinking = types.ThinkingConfig(thinking_level=types.ThinkingLevel[lvl])
        self.client = genai.Client(api_key=self.api_key,
                                   http_options=types.HttpOptions(timeout=int(self.cfg.timeout_s * 1000)))

    async def _call(self, system: str, user: str, schema: dict | None, schema_name: str,
                    max_output_tokens: int) -> LLMResult:
        cfg = types.GenerateContentConfig(
            system_instruction=system,
            max_output_tokens=max_output_tokens,
            temperature=self.cfg.temperature,
            thinking_config=self.thinking,
            response_mime_type="application/json" if schema else None,
            response_json_schema=transport_schema(schema, "gemini") if schema else None,
        )
        try:
            resp = await asyncio.wait_for(
                self.client.aio.models.generate_content(model=self.model, contents=user, config=cfg),
                self.cfg.timeout_s + 10)
        except genai_errors.APIError as exc:
            raise self.api_error(exc) from exc
        except (*_TRANSPORT, genai_errors.UnknownApiResponseError, TimeoutError) as exc:
            raise ProviderError(f"gemini transport: {exc!r}"[:300], retryable=True) from exc
        return self.result(resp, max_output_tokens)

    def result(self, resp: types.GenerateContentResponse, max_output_tokens: int) -> LLMResult:
        cand = resp.candidates[0] if resp.candidates else None
        fb = resp.prompt_feedback
        if cand is None and fb is not None and fb.block_reason:
            raise Refusal(f"gemini blocked the prompt ({fb.block_reason})")
        finish = str(cand.finish_reason) if cand is not None and cand.finish_reason is not None else None
        u = resp.usage_metadata
        if finish and any(b in finish for b in _BLOCKED):
            raise Refusal(f"gemini blocked the response ({finish})")
        if finish and "MAX_TOKENS" in finish:
            # re-sending the same request would be truncated again — never spend a repair call on it
            raise ProviderError(f"{self.name}: answer truncated at max_output_tokens={max_output_tokens} "
                                f"(thinking tokens: {(u.thoughts_token_count if u else None) or 0})", retryable=False)
        text = resp.text or ""
        return LLMResult(
            provider=self.name, model=resp.model_version or self.model, text=text, data=None,
            input_tokens=(u.prompt_token_count or 0) if u else 0,
            output_tokens=((u.candidates_token_count or 0) + (u.thoughts_token_count or 0)) if u else 0,
            cached_input_tokens=(u.cached_content_token_count or 0) if u else 0,
            stop_reason=finish, request_id=getattr(resp, "response_id", None),
        )

    def api_error(self, exc: genai_errors.APIError) -> ProviderError:
        """Classify an API error; errors that would repeat on every call put the provider on cooldown."""
        code = getattr(exc, "code", None) or 0
        status = getattr(exc, "status", None) or ""
        detail = json.dumps(getattr(exc, "details", None), default=str)
        msg = f"gemini {code} {status}: {getattr(exc, 'message', None) or exc}"[:300]
        now = now_ms()
        if code == 429:
            if "PerDay" in detail:
                self.cool_down(quota_day_end(now, self.cfg.quota_reset_tz), f"{self.name}: daily quota used up")
                return ProviderError(f"{self.cooldown_reason} ({msg[:160]})", retryable=False, rate_limited=True)
            self.cool_down(now + RATE_COOLDOWN_MS, f"{self.name}: rate limited")
            return ProviderError(msg, retryable=False, rate_limited=True)
        if code in (500, 502, 503, 504):
            return ProviderError(msg, retryable=True)
        if code in (400, 401, 403, 404):         # key invalid, permission, unknown model, precondition (region)
            self.cool_down(now + FATAL_COOLDOWN_MS, f"{self.name}: {msg[:200]}")
            return ProviderError(msg, retryable=False)
        return ProviderError(msg, retryable=False)
