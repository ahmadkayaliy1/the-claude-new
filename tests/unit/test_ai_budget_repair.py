"""Repair loop, rate limit and Cost Governor — control-flow tests with a scripted provider double
(no market data involved; the double only replays canned model outputs)."""
import asyncio
import copy

import pytest

from tradingsystem.ai.budget import BudgetExceeded, CostGovernor, RateLimiter, UsageStore
from tradingsystem.ai.contract import Recommendation
from tradingsystem.ai.providers.base import LLMProvider, LLMResult, ProviderError
from tradingsystem.ai.repair import generate_validated
from tradingsystem.core.settings import AIBudgetCfg, AIProviderCfg

from .test_contract import BASE


class Scripted(LLMProvider):
    def __init__(self, outputs, cfg=None):
        super().__init__("scripted", cfg or AIProviderCfg(kind="gemini", model="x", free_tier=True), "x", "k")
        self.outputs = list(outputs)
        self.calls = []

    async def _call(self, system, user, schema, schema_name, max_output_tokens):
        self.calls.append(user)
        out = self.outputs.pop(0)
        if isinstance(out, Exception):
            raise out
        return LLMResult(self.name, self.model, text=out if isinstance(out, str) else "", data=None if isinstance(out, str) else out,
                         input_tokens=1000, output_tokens=500)


@pytest.fixture
def env(tmp_path):
    usage = UsageStore(tmp_path / "app.db")
    yield usage
    usage.close()


def run(provider, usage, budget=None, **kw):
    limiter = RateLimiter(provider.name, provider.cfg, usage)
    gov = CostGovernor(budget or AIBudgetCfg(), usage)
    return asyncio.run(generate_validated(provider, Recommendation, system="s", user="u", limiter=limiter,
                                          governor=gov, usage=usage, purpose="test", **kw))


def test_valid_first_try(env):
    g = run(Scripted([copy.deepcopy(BASE)]), env)
    assert g.ok and g.value.decision.value == "BUY" and len(g.attempts) == 1


def test_repair_after_missing_stop_loss(env):
    bad = copy.deepcopy(BASE)
    bad["stop_loss"] = None
    p = Scripted([bad, copy.deepcopy(BASE)])
    g = run(p, env)
    assert g.ok and len(g.attempts) == 2
    assert "without stop_loss" in p.calls[1] and "rejected by the validator" in p.calls[1]


def test_gives_up_after_max_repairs_and_never_returns_invalid_trade(env):
    bad = copy.deepcopy(BASE)
    bad["stop_loss"] = 4300.0                     # wrong side, every time
    g = run(Scripted([bad, bad, bad]), env, max_repairs=2)
    assert not g.ok and g.value is None and len(g.attempts) == 3


def test_non_json_output_is_repaired(env):
    import json
    g = run(Scripted(["I think gold goes up", "```json\n" + json.dumps(BASE) + "\n```"]), env)
    assert g.ok


def test_retryable_provider_error_then_success(env, monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", lambda *_: asyncio.sleep(0) if False else _noop())
    g = run(Scripted([ProviderError("503", retryable=True), copy.deepcopy(BASE)]), env)
    assert g.ok


async def _noop():
    return None


def test_daily_quota_blocks(env):
    cfg = AIProviderCfg(kind="gemini", model="x", free_tier=True, rpd=2)
    p = Scripted([copy.deepcopy(BASE)] * 3, cfg)
    assert run(p, env).ok and run(p, env).ok
    g = run(p, env)
    assert g.budget_blocked and "quota" in g.errors[0]


def test_paid_provider_blocked_by_zero_cap_and_unknown_price(env):
    paid = AIProviderCfg(kind="anthropic", model="m", model_prices={"m": (5.0, 25.0)})
    p = Scripted([copy.deepcopy(BASE)], paid)
    p.model = "m"
    g = run(p, env, budget=AIBudgetCfg(daily_usd_cap=0.0))
    assert g.budget_blocked and "daily AI cap" in g.errors[0]
    unknown = Scripted([copy.deepcopy(BASE)], AIProviderCfg(kind="openai", model="z"))
    g2 = run(unknown, env, budget=AIBudgetCfg(daily_usd_cap=10.0))
    assert g2.budget_blocked and "price unknown" in g2.errors[0]


def test_governor_degrades_on_poor_roi(env):
    paid = AIProviderCfg(kind="anthropic", model="m", model_prices={"m": (5.0, 25.0)})
    p = Scripted([copy.deepcopy(BASE)] * 3, paid)
    p.model = "m"
    budget = AIBudgetCfg(daily_usd_cap=5.0, max_ai_cost_to_profit_ratio=0.2, min_trades_for_ratio=5)
    for _ in range(3):
        assert run(p, env, budget=budget).ok     # 3 × (1000×5 + 500×25)/1e6 = $0.0525
    gov = CostGovernor(budget, env, profit_fn=lambda since: (0.10, 20))
    st = gov.state()
    assert st.level == 2 and "cost/profit" in st.reason
    assert CostGovernor(budget, env, profit_fn=lambda since: (10.0, 20)).state().level == 0
    assert CostGovernor(budget, env, profit_fn=lambda since: (-3.0, 20)).state().level == 2
