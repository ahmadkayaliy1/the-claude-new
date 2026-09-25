"""Gemini adapter (ai_path F2, F4, F5, F8, F12): request bounds, thinking config, truncation / blocking, error
classification and cooldowns. Offline: responses are SDK objects built from the documented wire shape; no call
leaves the machine."""
import asyncio

import httpx
import pytest
from google.genai import errors as genai_errors
from google.genai import types

from tradingsystem.ai.providers import gemini as gm
from tradingsystem.ai.providers.base import ProviderError, Refusal, quota_day_end
from tradingsystem.core.settings import AIProviderCfg
from tradingsystem.core.timeutil import now_ms


def make(**cfg):
    c = AIProviderCfg(kind="gemini", model="gemini-3.8-flash", free_tier=True, effort="low", timeout_s=30,
                      quota_reset_tz="America/Los_Angeles", **cfg)
    return gm.GeminiProvider("gemini", c, "gemini-3.8-flash", "k")


def resp(**kw):
    return types.GenerateContentResponse.model_validate(kw)


def test_client_is_bounded_and_thinking_level_set():
    p = make()
    assert p.thinking.thinking_level == types.ThinkingLevel.LOW
    with pytest.raises(ProviderError, match="unsupported effort"):
        gm.GeminiProvider("gemini", AIProviderCfg(kind="gemini", model="m", effort="huge"), "m", "k")


def test_truncated_answer_is_not_resent():
    p = make()
    r = resp(candidates=[{"finish_reason": "MAX_TOKENS", "content": {"parts": [{"text": '{"pair": "XAU'}]}}],
             usage_metadata={"prompt_token_count": 9000, "thoughts_token_count": 4000, "candidates_token_count": 96})
    with pytest.raises(ProviderError) as e:
        p.result(r, 4096)
    assert not e.value.retryable and "truncated" in str(e.value) and "4000" in str(e.value)


def test_blocked_prompt_and_response_are_refusals():
    p = make()
    with pytest.raises(Refusal, match="prompt"):
        p.result(resp(prompt_feedback={"block_reason": "SAFETY"}), 4096)
    with pytest.raises(Refusal):
        p.result(resp(candidates=[{"finish_reason": "RECITATION", "content": {"parts": [{"text": "x"}]}}]), 4096)


def test_success_records_model_version_and_thinking_tokens():
    r = resp(candidates=[{"finish_reason": "STOP", "content": {"parts": [{"text": '{"x": 1}'}]}}],
             model_version="gemini-3.8-flash-001",
             usage_metadata={"prompt_token_count": 100, "thoughts_token_count": 40, "candidates_token_count": 10})
    out = make().result(r, 4096)
    assert out.model == "gemini-3.8-flash-001" and out.text == '{"x": 1}' and out.output_tokens == 50


def api_error(code, status, details=None):
    return genai_errors.APIError(code, {"error": {"code": code, "status": status, "message": "m",
                                                  "details": details or []}})


def test_daily_quota_cools_down_until_the_pacific_reset():
    p = make()
    e = p.api_error(api_error(429, "RESOURCE_EXHAUSTED", [{
        "@type": "type.googleapis.com/google.rpc.QuotaFailure",
        "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}]))
    assert not e.retryable and e.rate_limited
    assert p.cooldown_until_ms == quota_day_end(now_ms(), "America/Los_Angeles")
    assert "daily quota" in p.unavailable_reason()


def test_error_classes():
    p = make()
    assert p.api_error(api_error(503, "UNAVAILABLE")).retryable and p.unavailable_reason() is None
    e = p.api_error(api_error(429, "RESOURCE_EXHAUSTED"))           # per-minute: skip, short cooldown
    assert not e.retryable and 0 < p.cooldown_until_ms - now_ms() <= gm.RATE_COOLDOWN_MS
    q = make()
    assert not q.api_error(api_error(404, "NOT_FOUND")).retryable
    assert q.cooldown_until_ms - now_ms() > 30 * 60_000 and "404" in q.unavailable_reason()


def test_transport_errors_become_retryable_provider_errors():
    p = make()

    class Models:
        async def generate_content(self, **kw):
            raise httpx.ConnectError("connection refused")

    p.client = type("C", (), {"aio": type("A", (), {"models": Models()})()})()
    with pytest.raises(ProviderError) as e:
        asyncio.run(p._call("s", "u", None, "x", 100))
    assert e.value.retryable and "transport" in str(e.value)
