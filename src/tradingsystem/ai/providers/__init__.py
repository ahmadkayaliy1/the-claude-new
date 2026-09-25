"""Provider factory: ``make_provider(settings, name)`` - switching providers is a config change only."""
from __future__ import annotations

import os

from ...core.settings import Settings
from .base import LLMProvider, LLMResult, ProviderError, Refusal, extract_json, transport_schema

__all__ = ["LLMProvider", "LLMResult", "ProviderError", "Refusal", "extract_json", "transport_schema", "make_provider"]


def make_provider(settings: Settings, name: str | None = None, *, model: str | None = None) -> LLMProvider:
    name = name or settings.ai.active_provider
    cfg = settings.ai.providers[name]
    key = (os.environ.get(cfg.api_key_env, "").strip() or None) if cfg.api_key_env else None
    mdl = model or settings.provider_model(name)
    if cfg.kind == "gemini":
        from .gemini import GeminiProvider
        return GeminiProvider(name, cfg, mdl, key)
    if cfg.kind == "anthropic":
        from .anthropic_claude import AnthropicProvider
        return AnthropicProvider(name, cfg, mdl, key)
    if cfg.kind == "claude_code":
        from .claude_code import ClaudeCodeProvider
        return ClaudeCodeProvider(name, cfg, mdl, key)
    from .openai_chat import OpenAIChatProvider
    return OpenAIChatProvider(name, cfg, mdl, key)
