"""AI path hardening (ai_path F1, F4, F6/AI-01, F7, F8, F12, F13): orchestrator isolation and deadline,
system-owned recommendation fields, trigger floors, store bookkeeping, repair-loop error handling.
Control-flow tests with a scripted provider double and the real snapshot payload fixture."""
import asyncio
import copy
import json
from pathlib import Path

import pytest

from tradingsystem.ai import orchestrator as orch_mod
from tradingsystem.ai.budget import CostGovernor, RateLimiter, UsageStore
from tradingsystem.ai.contract import Recommendation
from tradingsystem.ai.orchestrator import CycleRequest, Orchestrator
from tradingsystem.ai.prompts import PromptError, _fill, render
from tradingsystem.ai.providers.base import LLMProvider, LLMResult, ProviderError, quota_day_end, quota_day_start
from tradingsystem.ai.repair import generate_validated
from tradingsystem.ai.store import DecisionRecord, DecisionStore
from tradingsystem.ai.triggers import decide, review_due
from tradingsystem.core.instruments import InstrumentRegistry
from tradingsystem.core.settings import AIBudgetCfg, AIProviderCfg, load_settings
from tradingsystem.core.timeutil import iso, parse_date_spec

from .test_contract import BASE
from .test_orchestrator import FakeBuilder, rec_for

AS_OF = 1790334600000                      # 2026-09-25T11:10:00Z
MIN = 60_000


class Double(LLMProvider):
    """Replays canned outputs; ``act(schema, user)`` may return a dict, raise, or be a coroutine."""

    def __init__(self, act):
        super().__init__("scripted", AIProviderCfg(kind="gemini", model="m", free_tier=True), "m", "k")
        self.act, self.calls = act, []

    async def _call(self, system, user, schema, schema_name, max_output_tokens):
        self.calls.append(user)
        out = self.act(schema_name, user)
        if asyncio.iscoroutine(out):
            out = await out
        return LLMResult(self.name, self.model, text=json.dumps(out), data=None, input_tokens=10, output_tokens=5)


@pytest.fixture
def orch(tmp_path, monkeypatch):
    made = []

    def _make(act, mode="agent_per_pair"):
        s = load_settings(env_path=Path("nope.env"), extra_env={"AGENT_MODE": mode})
        prov = Double(act)
        monkeypatch.setattr(orch_mod, "make_provider", lambda settings, name=None, **kw: prov)
        usage = UsageStore(tmp_path / "app.db")
        store = DecisionStore(tmp_path / "app.db", "cfg")
        made.append((usage, store))
        return Orchestrator(s, InstrumentRegistry.from_settings(s), FakeBuilder(), store, usage,
                            CostGovernor(AIBudgetCfg(), usage)), prov, store
    yield _make
    for usage, store in made:
        usage.close()
        store.close()


def cycle(o, pairs=("XAUUSD",), **kw):
    return asyncio.run(o.run_cycle([CycleRequest(p, "test") for p in pairs], as_of=AS_OF, **kw))


# ------------------------------------------------------------------ F6 / AI-01: system-owned fields
def test_timestamp_and_validity_are_set_by_the_system(orch):
    def act(schema, user):
        r = rec_for()
        r.update(timestamp="2024-01-01T00:00:00Z", valid_until="2030-01-01T00:00:00Z", pair="GOLD")
        return r
    o, _, store = orch(act)
    [r] = cycle(o)
    assert r.status == "valid" and r.recommendation["pair"] == "XAUUSD"
    assert parse_date_spec(r.recommendation["timestamp"]) == AS_OF
    assert parse_date_spec(r.recommendation["valid_until"]) == AS_OF + 4 * 15 * MIN        # clamped to 4 bars
    assert any("clamped" in e for e in r.errors)
    assert store.last_decision("XAUUSD")["recommendation"]["timestamp"] == r.recommendation["timestamp"]


def test_expired_trade_is_withheld(orch):
    def act(schema, user):
        r = rec_for()
        r.update(timestamp="2026-09-25T09:00:00Z", valid_until="2026-09-25T10:00:00Z")   # before the cycle time
        return r
    o, _, _ = orch(act)
    [r] = cycle(o)
    assert r.status == "invalid" and r.recommendation is None and "not after the cycle time" in r.errors[-1]


def test_price_reference_is_the_analysis_instrument(orch):
    def act(schema, user):
        r = rec_for("BTCUSDT")
        r["price_reference"] = "mt5:BTCUSD@" if "BTC" in user.split("Trigger")[0] else "BTCUSDT"
        return r
    o, _, _ = orch(act)
    [bad] = cycle(o, ("BTCUSDT",))
    assert bad.status == "invalid" and "execution instrument" in bad.errors[-1]
    ok = asyncio.run(o.run_cycle([CycleRequest("ETHUSDT", "t")], as_of=AS_OF))[0]
    assert ok.status == "valid" and ok.recommendation["price_reference"] == "binance_spot:ETHUSDT"


def test_review_plan_floor_and_conditions_already_met(orch):
    # fixture: analysis mid 4305.86, last 15m close 4305.36
    def act(schema, user):
        r = rec_for(decision="NO_TRADE")
        r["next_review"] = {"in_minutes": 1, "conditions": [
            {"kind": "price_above", "value": 4300.0}, {"kind": "price_below", "value": 4200.0},
            {"kind": "minutes_elapsed", "value": 2}, {"kind": "candle_close_above", "value": 4300.0, "timeframe": "15m"}]}
        return r
    o, _, _ = orch(act)
    [r] = cycle(o)
    nr = r.recommendation["next_review"]
    assert nr["in_minutes"] == 15                                       # time-based: never before normal spacing
    assert [(c["kind"], c["value"]) for c in nr["conditions"]] == [("price_below", 4200.0), ("minutes_elapsed", 15.0)]


# ------------------------------------------------------------------ F4 / F2: isolation and deadline
def test_one_failing_pair_does_not_take_the_cycle_down(orch):
    def act(schema, user):
        if "**BTCUSDT**" in user:
            raise RuntimeError("socket closed")               # an SDK surprise, not a ProviderError
        return rec_for()
    o, _, store = orch(act)
    by = {r.pair: r for r in cycle(o, ("XAUUSD", "BTCUSDT"))}
    assert by["XAUUSD"].status == "valid"
    assert by["BTCUSDT"].status == "error" and "RuntimeError" in by["BTCUSDT"].errors[0]
    assert store.last_attempt_ts("BTCUSDT") is not None


def test_unit_exception_is_stored_as_error(orch, monkeypatch):
    o, _, store = orch(lambda schema, user: rec_for())

    async def boom(*a, **kw):
        raise KeyError("x")
    monkeypatch.setattr(o, "_per_pair", boom)
    [r] = cycle(o)
    assert r.status == "error" and "KeyError" in r.errors[0]


def test_deadline_cancels_slow_units_and_keeps_finished_ones(orch):
    async def slow():
        await asyncio.sleep(30)
        return rec_for()

    def act(schema, user):
        return slow() if "**BTCUSDT**" in user else rec_for()
    o, _, store = orch(act)
    by = {r.pair: r for r in cycle(o, ("XAUUSD", "BTCUSDT"), deadline_s=0.5)}
    assert by["XAUUSD"].status == "valid"
    assert by["BTCUSDT"].status == "error" and "deadline" in by["BTCUSDT"].errors[0]
    assert store.last_attempt_ts("BTCUSDT") and store.last_attempt_ts("XAUUSD")


def test_provider_failure_is_error_not_invalid(orch):
    def act(schema, user):
        raise ProviderError("gemini 404 NOT_FOUND: model not found", retryable=False)
    o, prov, _ = orch(act)
    [r] = cycle(o)
    assert r.status == "error" and len(prov.calls) == 1


def test_dollar_words_from_the_model_do_not_break_prompts(orch):
    o, prov, store = orch(lambda schema, user: rec_for())
    store.save(DecisionRecord("XAUUSD", "m", "t", "valid", recommendation={
        **rec_for(), "market_summary": "Price swept $PDH and rejected; $BTC liquidity above."}, ts=AS_OF - 30 * MIN))
    [r] = cycle(o)
    assert r.status == "valid" and "$PDH" in prov.calls[0]


# ------------------------------------------------------------------ F7: templates, not values, are checked
def test_fill_checks_the_template_only():
    assert _fill("a $x b", {"x": "$PDH"}, "t") == "a $PDH b"
    with pytest.raises(PromptError, match="unfilled"):
        _fill("a $x $y", {"x": 1}, "t")
    with pytest.raises(PromptError, match="invalid"):
        _fill("costs $5", {}, "t")
    p = render("risk_reviewer", dict(pair="XAUUSD", pair_list="XAUUSD", decision_tf="15m", sl_min_atr=0.5,
                                     min_rr=1.5, max_risk_pct=1.0, account_equity=100, account_currency="USD",
                                     price_reference="mt5:XAUUSD@", output_language="English"),
               dict(now_utc="x", trigger_reason="r", payload="{}", max_valid_until="y",
                    proposal='{"reasoning_trace":"target $BTC liquidity"}'))
    assert "$BTC" in p.user


# ------------------------------------------------------------------ F1: triggers
def test_review_floor_and_backoff():
    kw = dict(policy="hybrid", payload=None, now=100 * MIN, min_spacing_min=15, max_idle_min=120, at_close=False)
    assert not decide(last_call_ms=97 * MIN, review_reasons=["r"], **kw).fire           # 3 min < floor 5
    assert decide(last_call_ms=94 * MIN, review_reasons=["r"], **kw).fire               # 6 min ≥ floor
    assert not decide(last_call_ms=60 * MIN, review_reasons=["r"], backoff_ms=60 * MIN, **kw).fire
    assert decide(last_call_ms=39 * MIN, review_reasons=["r"], backoff_ms=60 * MIN, **kw).fire


def test_review_due_counts_from_the_stored_row():
    rec = copy.deepcopy(BASE)
    rec["timestamp"] = "2020-01-01T00:00:00Z"                  # a model-written time is ignored
    last = {"id": "a", "ts": AS_OF, "recommendation": rec}
    assert review_due(last, AS_OF + 59 * MIN, None, {}) == []
    assert review_due(last, AS_OF + 61 * MIN, None, {})


# ------------------------------------------------------------------ store / governor input (F1, F13)
def test_store_attempts_realised_and_history(tmp_path):
    st = DecisionStore(tmp_path / "app.db", "cfg")
    try:
        assert st.last_attempt_ts("XAUUSD") is None
        a = DecisionRecord("XAUUSD", "m", "t", "valid", recommendation=rec_for(), ts=AS_OF)
        b = DecisionRecord("XAUUSD", "m", "t", "skipped", errors=["data gate"], ts=AS_OF + MIN)
        c = DecisionRecord("XAUUSD", "m", "t", "valid", recommendation=rec_for(), ts=AS_OF + 2 * MIN)
        for r in (a, b, c):
            st.save(r)
        assert st.last_attempt_ts("XAUUSD") == AS_OF + 2 * MIN
        assert [h["status"] for h in st.recent("XAUUSD")] == ["valid", "valid"]
        st.set_outcome(a.id, "closed_profit", 4.0, 4.0, 40.0)
        st.set_outcome(c.id, "not_filled", 0.0, 0.0, 0.0)
        assert st.realised_since(0) == (4.0, 1)
    finally:
        st.close()


# ------------------------------------------------------------------ repair loop (F2, F8, F12)
class Raising(LLMProvider):
    def __init__(self, exc):
        super().__init__("r", AIProviderCfg(kind="gemini", model="x", free_tier=True), "x", "k")
        self.exc, self.n = exc, 0

    async def _call(self, *a):
        self.n += 1
        raise self.exc


def _gen(prov, usage):
    return asyncio.run(generate_validated(prov, Recommendation, system="s", user="u",
                                          limiter=RateLimiter(prov.name, prov.cfg, usage),
                                          governor=CostGovernor(AIBudgetCfg(), usage), usage=usage, purpose="t"))


def test_rate_limit_is_not_retried_inside_a_cycle(tmp_path):
    usage = UsageStore(tmp_path / "app.db")
    try:
        p = Raising(ProviderError("429", retryable=True, rate_limited=True))
        g = _gen(p, usage)
        assert p.n == 1 and not g.ok and g.provider_error
        q = Raising(ValueError("weird SDK state"))
        g2 = _gen(q, usage)
        assert q.n == 1 and "ValueError" in g2.provider_error
    finally:
        usage.close()


# ------------------------------------------------------------------ F8: quota day
def test_quota_day_follows_pacific_midnight():
    t = parse_date_spec("2026-09-25T06:00:00Z")                # 23:00 PDT on the 24th
    assert iso(quota_day_start(t, "America/Los_Angeles")) == "2026-09-24T07:00:00.000Z"
    assert iso(quota_day_end(t, "America/Los_Angeles")) == "2026-09-25T07:00:00.000Z"
    assert iso(quota_day_start(t)) == "2026-09-25T00:00:00.000Z"
    w = parse_date_spec("2026-11-01T12:00:00Z")                # DST ends that day (25 h)
    assert quota_day_end(w, "America/Los_Angeles") - quota_day_start(w, "America/Los_Angeles") == 25 * 3_600_000
