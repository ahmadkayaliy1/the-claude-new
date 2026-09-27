"""Per-role models and the escalation of strong setups (D-043): a stronger model may confirm a trade unchanged or
downgrade it to NO_TRADE — never change levels, never raise confidence."""
from __future__ import annotations

import asyncio
import copy

import pytest

from tradingsystem.ai.orchestrator import CycleRequest, Orchestrator
from tradingsystem.core.instruments import InstrumentRegistry
from tradingsystem.core.settings import load_settings

from .test_orchestrator import make, rec_for  # noqa: F401 — the fixture


def escalate_on(o: Orchestrator, **kw) -> Orchestrator:
    esc = o.s.ai.escalation.model_copy(update={"enabled": True, **kw})
    o.s = o.s.model_copy(update={"ai": o.s.ai.model_copy(update={"escalation": esc})})
    return o


def run(o, strength="strong"):
    return asyncio.run(o.run_cycle([CycleRequest("XAUUSD", "15m BOS up", strength)], as_of=1790334600000))


def responder(verdict="confirm", *, alter=False, esc_conf=58, bad=False):
    def r(schema, user):
        if schema != "EscalationReview":
            return rec_for()
        if bad:
            return {"nonsense": True}
        final = rec_for() if verdict == "confirm" else rec_for(decision="NO_TRADE")
        if alter:
            final["stop_loss"] = 4280.0
        return {"verdict": verdict, "issues": ["HTF supply above"] if verdict == "downgrade" else [],
                "confidence": esc_conf, "final_recommendation": final}
    return r


def test_no_escalation_unless_enabled_strong_and_confident(make):
    o, prov, _ = make("agent_per_pair", responder())
    [r] = run(o)
    assert r.status == "valid" and [c[0] for c in prov.calls] == ["Recommendation"]
    o, prov, _ = make("agent_per_pair", responder())
    escalate_on(o)
    [r] = run(o, strength="weak")
    assert [c[0] for c in prov.calls] == ["Recommendation"]
    o, prov, _ = make("agent_per_pair", responder())
    escalate_on(o, min_confidence=70)                                  # the trader's confidence is 62
    run(o)
    assert [c[0] for c in prov.calls] == ["Recommendation"]


def test_confirm_keeps_levels_and_never_raises_confidence(make):
    o, prov, _ = make("agent_per_pair", responder("confirm", esc_conf=58))
    escalate_on(o)
    [r] = run(o)
    assert [c[0] for c in prov.calls] == ["Recommendation", "EscalationReview"]
    assert r.status == "valid" and r.recommendation["decision"] == "BUY"
    assert r.recommendation["stop_loss"] == 4284.0 and r.recommendation["confidence"] == 58
    assert [s["role"] for s in r.sub_outputs] == ["trader", "escalation"]
    assert any("escalation: confirmed" in e for e in r.errors)
    assert "The trader's proposal" in prov.calls[1][2] and "senior partner" in prov.calls[1][1]


def test_downgrade_ends_as_no_trade(make):
    o, _, store = make("agent_per_pair", responder("downgrade"))
    escalate_on(o)
    [r] = run(o)
    assert r.status == "valid" and r.recommendation["decision"] == "NO_TRADE" and r.rr_computed is None
    assert any("downgraded" in e for e in r.errors)
    assert store.last_decision("XAUUSD")["recommendation"]["decision"] == "NO_TRADE"


def test_a_confirmation_that_changes_levels_is_withheld(make):
    o, _, _ = make("agent_per_pair", responder("confirm", alter=True))
    escalate_on(o)
    [r] = run(o)
    assert r.status == "invalid" and any("altered levels (stop_loss)" in e for e in r.errors)


@pytest.mark.parametrize("policy,status", [("withhold", "invalid"), ("keep", "valid")])
def test_failed_escalation_follows_the_policy(make, policy, status):
    o, _, _ = make("agent_per_pair", responder(bad=True))
    escalate_on(o, on_failure=policy)
    [r] = run(o)
    assert r.status == status
    if status == "valid":
        assert r.recommendation["decision"] == "BUY"


def test_escalations_have_a_daily_maximum(make):
    o, prov, _ = make("agent_per_pair", responder())
    escalate_on(o, max_per_day_per_pair=1)
    o.usage.record(None, provider="scripted", model="m", purpose="escalation", pair="XAUUSD", ok=True, role="escalation")
    run(o)
    assert [c[0] for c in prov.calls] == ["Recommendation"]


def test_role_models_apply_to_claude_code_only():
    s = load_settings(extra_env={"TS_INSTANCE": "BTCUSDT"})
    o = object.__new__(Orchestrator)
    o.s, o.reg = s, InstrumentRegistry.from_settings(s)
    assert o.role_model("claude_code", "escalation") == ("opus", "high")
    assert o.role_model("claude_code", "decision") == (None, None)       # the provider's own model and effort
    assert o.role_model("gemini", "escalation") == (None, None)
    models = s.ai.models.model_copy(update={"decision": s.ai.models.decision.model_copy(update={"model": "fable",
                                                                                                "effort": "high"})})
    o.s = s.model_copy(update={"ai": s.ai.model_copy(update={"models": models})})
    assert o.role_model("claude_code", "decision") == ("fable", "high")


def test_decision_records_carry_strength_and_library_hash(make):
    o, _, store = make("agent_per_pair", responder())
    [r] = run(o, strength="review")
    assert r.trigger_strength == "review" and r.library_hash == o.library_hash and len(r.library_hash) == 16
    con = store._con
    row = con.execute("SELECT trigger_strength, library_hash FROM ai_decisions WHERE id=?", (r.id,)).fetchone()
    assert row == ("review", o.library_hash)
