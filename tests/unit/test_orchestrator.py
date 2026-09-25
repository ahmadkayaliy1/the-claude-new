"""Orchestrator modes with a scripted provider double and a real snapshot payload (control flow only)."""
import asyncio
import copy
import json
from pathlib import Path

import pytest

from tradingsystem.ai import orchestrator as orch_mod
from tradingsystem.ai.budget import CostGovernor, UsageStore
from tradingsystem.ai.orchestrator import CycleRequest, Orchestrator, aggregate_consensus
from tradingsystem.ai.providers.base import LLMProvider, LLMResult
from tradingsystem.ai.store import DecisionStore
from tradingsystem.core.instruments import InstrumentRegistry
from tradingsystem.core.settings import AIBudgetCfg, AIProviderCfg, load_settings

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "real" / "payload_xauusd.json"


def rec_for(pair="XAUUSD", decision="BUY"):
    r = {"pair": pair, "timestamp": "2026-09-25T10:50:00Z", "valid_until": "2026-09-25T11:50:00Z",
         "price_reference": "mt5:XAUUSD@", "market_summary": "Bullish 15m structure after a sell-side sweep.",
         "decision": decision, "confidence": 62, "next_review": {"in_minutes": 60},
         "reasoning_trace": "1h sweep of 4295.76 reclaimed; 15m bullish OB 4289.68-4296.87 untouched."}
    if decision == "BUY":
        r.update(order_type="BUY_LIMIT", entry={"range_min": 4290.0, "range_max": 4296.0}, stop_loss=4284.0,
                 take_profits=[{"price": 4313.0, "close_fraction": 1.0}],
                 risk_management={"risk_percent_suggested": 0.5, "risk_reward_ratio": 1.4,
                                  "invalidation_reason": "15m close below 4284", "sl_basis": "below OB low - 0.8 ATR"})
    return r


class Scripted(LLMProvider):
    def __init__(self, responder):
        super().__init__("scripted", AIProviderCfg(kind="gemini", model="m", free_tier=True), "m", "k")
        self.responder, self.calls = responder, []

    async def _call(self, system, user, schema, schema_name, max_output_tokens):
        self.calls.append((schema_name, system, user))
        return LLMResult(self.name, self.model, text=json.dumps(self.responder(schema_name, user)), data=None,
                         input_tokens=1000, output_tokens=300)


class FakeBuilder:
    def __init__(self):
        self.payload = json.loads(FIX.read_text())

    def build(self, pair, as_of, account=None, history=None, timeframes=None):
        p = copy.deepcopy(self.payload)
        p["meta"]["pair"] = pair
        p["meta"]["payload_hash"] = f"hash-{pair}"
        p["history"] = history or []
        return p


@pytest.fixture
def make(tmp_path, monkeypatch):
    def _make(mode, responder, consensus=None):
        s = load_settings(env_path=Path("nope.env"), extra_env={"AGENT_MODE": mode})
        if consensus:
            s = s.model_copy(update={"ai": s.ai.model_copy(update={"consensus_providers": consensus})})
        prov = Scripted(responder)
        monkeypatch.setattr(orch_mod, "make_provider", lambda settings, name=None, **kw: prov)
        usage = UsageStore(tmp_path / "app.db")
        store = DecisionStore(tmp_path / "app.db", "cfg")
        o = Orchestrator(s, InstrumentRegistry.from_settings(s), FakeBuilder(), store, usage,
                         CostGovernor(AIBudgetCfg(), usage))
        return o, prov, store
    return _make


def run(o, pairs=("XAUUSD",)):
    return asyncio.run(o.run_cycle([CycleRequest(p, "test") for p in pairs], as_of=1790334600000))


def test_agent_per_pair_stores_full_chain(make):
    o, prov, store = make("agent_per_pair", lambda schema, user: rec_for())
    [r] = run(o)
    assert r.status == "valid" and r.recommendation["decision"] == "BUY" and r.rr_computed == pytest.approx(17 / 12)
    assert "Evidence only" in prov.calls[0][1] and '"price_reference":"mt5:XAUUSD@"' in prov.calls[0][2]
    assert store.load_payload("hash-XAUUSD")["meta"]["pair"] == "XAUUSD"
    assert store.last_decision("XAUUSD")["recommendation"]["order_type"] == "BUY_LIMIT"


def test_risk_reviewer_can_veto(make):
    def responder(schema, user):
        if schema == "RiskReview":
            return {"verdict": "reject", "issues": ["SL inside noise"], "final_recommendation": rec_for(decision="NO_TRADE")}
        return rec_for()
    o, prov, store = make("agent_per_pair_with_risk_reviewer", responder)
    [r] = run(o)
    assert r.recommendation["decision"] == "NO_TRADE"
    assert [s["role"] for s in r.sub_outputs] == ["trader", "risk_reviewer"]
    assert any("reject" in e for e in r.errors)


def test_pair_and_timeframe_mode_uses_coordinator(make):
    def responder(schema, user):
        if schema == "TimeframeAssessment":
            return {"pair": "XAUUSD", "timeframe": "1h", "bias": "bullish", "confidence": 60, "structure": "HH/HL",
                    "key_levels": [{"price": 4295.76, "kind": "liquidity_high"}]}
        return rec_for()
    o, prov, store = make("agent_per_pair_and_timeframe", responder)
    [r] = run(o)
    assert r.status == "valid"
    assert sum(1 for c in prov.calls if c[0] == "TimeframeAssessment") == 6
    assert prov.calls[-1][0] == "Recommendation" and "Analyst assessments" in prov.calls[-1][2]
    assert len(r.sub_outputs) == 6


def test_single_agent_global_requires_every_pair(make):
    o, prov, store = make("single_agent_global", lambda schema, user: {"recommendations": [rec_for("XAUUSD")]})
    recs = run(o, ("XAUUSD", "BTCUSDT"))
    by = {r.pair: r for r in recs}
    assert by["XAUUSD"].status == "valid" and by["BTCUSDT"].status == "invalid"


def test_consensus_majority_rule():
    buy, no = rec_for(), rec_for(decision="NO_TRADE")
    assert aggregate_consensus([buy, buy, no], 3, "XAUUSD", 1790334600000)["decision"] == "BUY"
    assert aggregate_consensus([buy, no], 2, "XAUUSD", 1790334600000)["decision"] == "NO_TRADE"   # 1/2 is not a majority
    low = copy.deepcopy(buy)
    low["confidence"] = 40
    assert aggregate_consensus([buy, low, no], 3, "XAUUSD", 1790334600000)["confidence"] == 40


def test_invalid_output_never_released(make):
    bad = rec_for()
    bad["stop_loss"] = 4300.0                   # wrong side on every attempt
    o, prov, store = make("agent_per_pair", lambda schema, user: bad)
    [r] = run(o)
    assert r.status == "invalid" and r.recommendation is None and len(prov.calls) == 3
