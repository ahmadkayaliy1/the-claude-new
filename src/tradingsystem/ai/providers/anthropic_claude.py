"""Anthropic Claude via the official ``anthropic`` SDK (Messages API).

* Structured output: ``output_config.format`` (JSON schema; the SDK/API does not enforce numeric or length
  constraints, so those are stripped here and enforced client-side by the contract).
* The long, static system prompt is marked for prompt caching (``cache_control: ephemeral``) so repeated
  analysis cycles pay ~10 % for it.
* Adaptive thinking is left at the model default; depth is set with ``output_config.effort``.
* Refusals: server-side fallbacks (``fallbacks: "default"``, beta ``server-side-fallback-2026-07-01``) are
  enabled by default; a final ``stop_reason == "refusal"`` is surfaced as :class:`Refusal`.
* Chart images (Phase 3): sent as base64 ``image`` content blocks, each after its caption text block.
"""
from __future__ import annotations

import anthropic

from .base import ImageInput, LLMProvider, LLMResult, ProviderError, Refusal, image_content_blocks, transport_schema

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicProvider(LLMProvider):
    supports_images = True

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        if not self.api_key:
            raise ProviderError(f"{self.name}: missing API key ({self.cfg.api_key_env})", retryable=False)
        self.client = anthropic.AsyncAnthropic(api_key=self.api_key, timeout=self.cfg.timeout_s, max_retries=2)
        self.effort = self.cfg.effort or "high"

    def cost_of(self, res: LLMResult) -> float:
        # cache writes are billed at 1.25x input, reads at 0.1x (reads handled by the base)
        base = super().cost_of(res)
        written = int(res.extra.get("cache_creation_input_tokens", 0))
        return base + written * self.prices()[0] * 0.25 / 1e6

    async def _call(self, system: str, user: str, schema: dict | None, schema_name: str,
                    max_output_tokens: int, *, images: list[ImageInput] | None = None) -> LLMResult:
        output_config: dict = {"effort": self.effort}
        content: str | list[dict] = image_content_blocks(user, images) if images else user
        if schema:
            output_config["format"] = {"type": "json_schema", "schema": transport_schema(schema, "anthropic")}
        try:
            resp = await self.client.beta.messages.create(
                model=self.model,
                max_tokens=max(max_output_tokens, 16000),
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": content}],
                output_config=output_config,
                betas=[FALLBACK_BETA],
                fallbacks="default",
            )
        except anthropic.RateLimitError as exc:
            raise ProviderError(f"anthropic rate limited: {exc.message}", retryable=True, rate_limited=True) from exc
        except anthropic.APIStatusError as exc:
            raise ProviderError(f"anthropic {exc.status_code}: {exc.message}", retryable=exc.status_code >= 500) from exc
        except anthropic.APIConnectionError as exc:
            raise ProviderError(f"anthropic connection error: {exc}", retryable=True) from exc
        if resp.stop_reason == "refusal":
            cat = getattr(resp.stop_details, "category", None) if getattr(resp, "stop_details", None) else None
            raise Refusal(f"claude declined (category={cat})")
        text = next((b.text for b in resp.content if b.type == "text"), "")
        u = resp.usage
        cached = (getattr(u, "cache_read_input_tokens", 0) or 0)
        written = (getattr(u, "cache_creation_input_tokens", 0) or 0)
        return LLMResult(
            provider=self.name, model=resp.model, text=text, data=None,
            input_tokens=(u.input_tokens or 0) + cached + written, output_tokens=u.output_tokens or 0,
            cached_input_tokens=cached, stop_reason=resp.stop_reason, request_id=getattr(resp, "_request_id", None),
            extra={"cache_creation_input_tokens": written},
        )
