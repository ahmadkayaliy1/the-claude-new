"""Google Gemini via the official ``google-genai`` SDK (default provider during testing, D-003)."""
from __future__ import annotations

from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from .base import LLMProvider, LLMResult, ProviderError, Refusal, transport_schema


class GeminiProvider(LLMProvider):
    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        if not self.api_key:
            raise ProviderError(f"{self.name}: missing API key ({self.cfg.api_key_env})", retryable=False)
        self.client = genai.Client(api_key=self.api_key)

    async def _call(self, system: str, user: str, schema: dict | None, schema_name: str,
                    max_output_tokens: int) -> LLMResult:
        cfg = types.GenerateContentConfig(
            system_instruction=system,
            max_output_tokens=max_output_tokens,
            temperature=0.2,
            response_mime_type="application/json" if schema else None,
            response_json_schema=transport_schema(schema, "gemini") if schema else None,
        )
        try:
            resp = await self.client.aio.models.generate_content(model=self.model, contents=user, config=cfg)
        except genai_errors.APIError as exc:
            code = getattr(exc, "code", None) or 0
            raise ProviderError(f"gemini {code}: {exc}", retryable=code in (429, 500, 502, 503, 504),
                                rate_limited=code == 429) from exc
        cand = resp.candidates[0] if resp.candidates else None
        finish = str(cand.finish_reason) if cand is not None and cand.finish_reason is not None else None
        if finish and ("SAFETY" in finish or "PROHIBITED" in finish or "BLOCKLIST" in finish):
            raise Refusal(f"gemini blocked the response ({finish})")
        u = resp.usage_metadata
        text = resp.text or ""
        return LLMResult(
            provider=self.name, model=self.model, text=text, data=None,
            input_tokens=(u.prompt_token_count or 0) if u else 0,
            output_tokens=((u.candidates_token_count or 0) + (u.thoughts_token_count or 0)) if u else 0,
            cached_input_tokens=(u.cached_content_token_count or 0) if u else 0,
            stop_reason=finish, request_id=getattr(resp, "response_id", None),
        )
