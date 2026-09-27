"""Prompt library lint: every trading role carries the non-negotiable rules; placeholders are all filled;
the system prompt is stable across cycles (cacheable)."""
import pytest

from tradingsystem.ai.prompts import PromptError, ROLE_FILES, library_hash, render

SYS = dict(pair="XAUUSD", pair_list="BTCUSDT, ETHUSDT, XAUUSD", decision_tf="15m", sl_min_atr=0.5, min_rr=1.5,
           sl_max_atr=5.0, max_spread_pct=20, min_confidence=55, max_rec_age_min=5,
           max_risk_pct=1.0, account_equity=158, account_currency="USD", price_reference="mt5:XAUUSD@",
           output_language="English", timeframe="1h", scope_text=" for XAUUSD")
USER = dict(now_utc="2026-09-25T10:00:00Z", trigger_reason="15m close", payload="{}", max_valid_until="x",
            assessments="[]", proposal="{}", charts_note="No charts this cycle.", playbook="(no playbook yet)")
TRADING_ROLES = ["agent_per_pair", "single_agent_global", "coordinator"]


@pytest.mark.parametrize("role", list(ROLE_FILES))
def test_all_roles_render_without_leftovers(role):
    p = render(role, SYS, USER)
    assert "$" not in p.system.replace("$158", "") or "$" not in p.system  # no unfilled $placeholders
    assert p.user and p.system


@pytest.mark.parametrize("role", TRADING_ROLES)
def test_trading_roles_carry_core_rules(role):
    s = render(role, SYS, USER).system
    for must in ("Evidence only", "No trade without a structural stop", "NO_TRADE is a professional decision",
                 "Data quality is part of the analysis", "Calibrated confidence", "Execution realism"):
        assert must in s, f"{role} is missing rule: {must}"


def test_reviewer_and_analyst_forbid_fabrication():
    assert "never invent" in render("risk_reviewer", SYS, USER).system.lower()
    assert "never invent" in render("timeframe_analyst", SYS, USER).system.lower()


def test_system_prompt_stable_across_cycles():
    a = render("agent_per_pair", SYS, USER)
    b = render("agent_per_pair", SYS, {**USER, "now_utc": "2026-09-25T10:15:00Z", "payload": '{"x": 1}'})
    assert a.system == b.system and a.prompt_hash == b.prompt_hash and a.user != b.user


def test_missing_variable_raises():
    bad = dict(SYS)
    bad.pop("min_rr")
    with pytest.raises(PromptError, match="min_rr"):
        render("agent_per_pair", bad, USER)


def test_library_hash_is_stable():
    assert library_hash() == library_hash() and len(library_hash()) == 16


@pytest.mark.parametrize("role", list(ROLE_FILES))
def test_every_role_explains_the_compact_payload(role):
    """Phase 1: the model view's column tables and short times are explained once, in the cached system prompt."""
    s = render(role, SYS, USER).system
    assert "Reading the payload" in s and "[dir, top, bottom, formed" in s and "MM-DD HH:MM" in s


def test_trading_roles_know_the_gate_limits_and_the_memory():
    s = render("agent_per_pair", SYS, USER).system
    for must in ("min_stop_distance", "max_stop_distance", "20 %", "executes nothing below 55", "operator_notes",
                 "within 5 minutes"):
        assert must in s, must
