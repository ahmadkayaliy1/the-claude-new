"""OpenAI and OpenAI-compatible endpoints (xAI Grok, Groq, OpenRouter, DeepSeek, local Ollama) via the official
``openai`` SDK's Chat Completions API with a configurable ``base_url``.

Structured output support differs per endpoint → ``structured_output`` in config:
``native`` (json_schema, strict), ``json_object`` (JSON mode + schema in the prompt), ``prompt`` (schema in the
prompt only). The contract is validated client-side in every case.
"""
from __future__ import annotations

import json

import openai

from .base import LLMProvider, LLMResult, ProviderError, Refusal, transport_schema


class OpenAIChatProvider(LLMProvider):
    supports_images = False     # endpoints differ in vision support: chart images are dropped (one warning)

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        key = self.api_key or ("ollama" if self.cfg.base_url and "127.0.0.1" in self.cfg.base_url else None)
        if not key:
            raise ProviderError(f"{self.name}: missing API key ({self.cfg.api_key_env})", retryable=False)
        self.client = openai.AsyncOpenAI(api_key=key, base_url=self.cfg.base_url, timeout=self.cfg.timeout_s,
                                         max_retries=2)

    async def _call(self, system: str, user: str, schema: dict | None, schema_name: str,
                    max_output_tokens: int) -> LLMResult:
        mode = self.cfg.structured_output
        kwargs: dict = {}
        if schema and mode == "native":
            kwargs["response_format"] = {"type": "json_schema", "json_schema": {
                "name": schema_name, "strict": True, "schema": transport_schema(schema, "openai_strict")}}
        elif schema:
            if mode == "json_object":
                kwargs["response_format"] = {"type": "json_object"}
            user = (f"{user}\n\nReturn ONLY one JSON object that validates against this JSON Schema:\n"
                    f"{json.dumps(transport_schema(schema, 'plain'), separators=(',', ':'))}")
        try:
            resp = await self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                max_completion_tokens=max_output_tokens,
                **kwargs,
            )
        except openai.RateLimitError as exc:
            raise ProviderError(f"{self.name} rate limited: {exc}", retryable=True, rate_limited=True) from exc
        except openai.APIStatusError as exc:
            raise ProviderError(f"{self.name} {exc.status_code}: {exc}", retryable=exc.status_code >= 500) from exc
        except openai.APIConnectionError as exc:
            raise ProviderError(f"{self.name} connection error: {exc}", retryable=True) from exc
        choice = resp.choices[0]
        refusal = getattr(choice.message, "refusal", None)
        if refusal:
            raise Refusal(f"{self.name} refused: {refusal[:200]}")
        u = resp.usage
        cached = 0
        if u is not None and getattr(u, "prompt_tokens_details", None) is not None:
            cached = getattr(u.prompt_tokens_details, "cached_tokens", 0) or 0
        return LLMResult(
            provider=self.name, model=resp.model or self.model, text=choice.message.content or "", data=None,
            input_tokens=(u.prompt_tokens or 0) if u else 0, output_tokens=(u.completion_tokens or 0) if u else 0,
            cached_input_tokens=cached, stop_reason=choice.finish_reason, request_id=resp.id,
        )
