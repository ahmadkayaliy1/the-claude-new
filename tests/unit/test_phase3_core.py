"""Phase 3 core (lead's parts): prompt cache fix, prompt versions, daily cap, single-leg gate, price translation of
the management plan and position actions, position-action target validation."""
from __future__ import annotations

import copy
import datetime as dt
from pathlib import Path

import pytest

from tradingsystem.ai import budget
from tradingsystem.ai.orchestrator import Orchestrator
from tradingsystem.ai.prompts import library_hash, render, versions
from tradingsystem.core.instruments import InstrumentRegistry
from tradingsystem.core.settings import INSTANCE_ENV, AIProviderCfg, RiskCfg, load_settings
from tradingsystem.core.timeutil import to_ms
from tradingsystem.execution.price_mapping import translate
from tradingsystem.execution.risk_gate import evaluate
from tradingsystem.execution.sizing import single_leg_index, split_volume

from .test_risk_gate import ctx, rec


def orch_for(pair: str = "BTCUSDT") -> Orchestrator:
    s = load_settings(extra_env={INSTANCE_ENV: pair})
    o = object.__new__(Orchestrator)
    o.s, o.reg = s, InstrumentRegistry.from_settings(s)
    return o


# ------------------------------------------------------------------ prompt cache (3.1)
def test_system_prompt_does_not_change_with_the_equity():
    o = orch_for()
    sv99, sv158 = o._system_vars("BTCUSDT", {"equity": 99.24}), o._system_vars("BTCUSDT", {"equity": 158.0})
    assert sv99 == sv158 and "account_equity" not in sv99
    u1 = o._user_vars("BTCUSDT", 1_790_000_000_000, "t", "{}", account={"equity": 99.24, "currency": "USD"})
    u2 = o._user_vars("BTCUSDT", 1_790_000_000_000, "t", "{}", account={"equity": 158.0, "currency": "USD"})
    a, b = render("agent_per_pair", sv99, u1), render("agent_per_pair", sv158, u2)
    assert a.system == b.system and a.prompt_hash == b.prompt_hash
    assert "99.24 USD" in a.user and "158.00 USD" in b.user
    assert "No charts this cycle." in a.user and "(no playbook yet)" in a.user


def test_prompt_versions_are_registered():
    v = versions()
    assert v["shared/core_rules"] == 6 and v["shared/trader_persona"] == 2 and v["shared/payload_legend"] == 4
    assert v["agent_per_pair/instructions"] == 4 and v["escalation/system"] == 1
    assert len(library_hash()) == 16


def test_core_rules_v6_speak_about_position_actions_charts_single_leg_and_playbook():
    o = orch_for()
    s = render("agent_per_pair", o._system_vars("BTCUSDT", {}),
               o._user_vars("BTCUSDT", 1_790_000_000_000, "t", "{}")).system
    for must in ("position_actions", "never widen or remove a stop", "Charts.", "Take-profits at the minimum lot",
                 "Playbook.", "Manage what is open first", "never in anticipation"):
        assert must in s, must
    assert "cannot be changed or cancelled by you" not in s          # the old rule 13 is gone


def test_escalation_role_renders():
    o = orch_for()
    p = render("escalation", o._system_vars("BTCUSDT", {}),
               {**o._user_vars("BTCUSDT", 1_790_000_000_000, "t", "{}"), "proposal": "{}"})
    assert "senior partner" in p.system and "confirm" in p.system and "downgrade" in p.system
    assert "The trader's proposal" in p.user


# ------------------------------------------------------------------ 40/day cap (3.4)
def test_daily_cap_counts_every_row_of_the_pair(tmp_path):
    usage = budget.UsageStore(tmp_path / "ai_usage.db")
    cfg = AIProviderCfg(kind="claude_code", model="sonnet", rpd=120)
    for role in ["decision"] * 30 + ["repair"] * 6 + ["escalation"] * 3:
        usage.record(None, provider="claude_code", model="sonnet", purpose=role, pair="BTCUSDT", ok=True, role=role)
    lim = budget.RateLimiter("claude_code", cfg, usage, instance="BTCUSDT", daily_cap=40)
    assert lim.cap() == 40 and lim.remaining_today() == 1
    assert usage.count_role_since("escalation", 0, "BTCUSDT") == 3
    alone = budget.RateLimiter("claude_code", cfg, usage)
    assert alone.cap() == 120 and alone.remaining_today() == 81
    usage.close()


def test_settings_carry_the_phase3_keys():
    s = load_settings(extra_env={INSTANCE_ENV: "BTCUSDT"})
    assert s.ai.daily_calls_per_pair == 40 and s.ai.screen_timeframe == "5m" and s.ai.weak_min == 2
    assert s.ai.charts.enabled and s.ai.charts.width == 720 and s.ai.charts.timeframes[0] == "1w"
    assert s.ai.escalation.enabled is False and s.ai.models.escalation.model == "opus"
    assert s.execution.management.enabled and s.execution.position_actions.max_per_pair_per_day == 12
    assert not hasattr(s.ai, "instance_max_rpd_share")


# ------------------------------------------------------------------ single leg at the minimum lot (3.9)
def test_split_volume_and_single_leg_rule():
    assert split_volume(0.01, [0.4, 0.6], 0.01, 0.01) is None
    assert split_volume(0.04, [0.4, 0.6], 0.01, 0.01) == [0.02, 0.02]
    assert single_leg_index([0.4, 0.6]) == 1 and single_leg_index([0.5, 0.5]) == 0 and single_leg_index([0.6, 0.4]) == 0


def test_gate_measures_rr_on_the_leg_actually_placed():
    risk = RiskCfg(risk_per_trade_pct=1.0, max_risk_per_trade_pct=3.0)
    # 0.01 lot of gold (min lot) cannot be split: the one position goes to the largest fraction (TP1 at 4310, 0.6)
    r = rec(take_profits=[{"price": 4310.0, "close_fraction": 0.6}, {"price": 4330.0, "close_fraction": 0.4}])
    g = evaluate(r, "XAUUSD", ctx(equity=1000.0), risk, [])
    assert g.size.lots == pytest.approx(0.01) and g.single_leg_tp == 0
    rr = [c for c in g.checks if c[0] == "rr_after_costs"][0]
    assert not rr[1] and "single leg at TP1" in rr[2]                  # 14/12 = 1.17 < 1.5 (blended 1.83 would pass)
    # the same trade with the larger fraction on the far target passes on that leg
    r2 = rec(take_profits=[{"price": 4310.0, "close_fraction": 0.4}, {"price": 4330.0, "close_fraction": 0.6}])
    g2 = evaluate(r2, "XAUUSD", ctx(equity=1000.0), risk, [])
    assert g2.single_leg_tp == 1 and [c for c in g2.checks if c[0] == "rr_after_costs"][0][1]
    # enough size to split: unchanged blended RR
    g3 = evaluate(r, "XAUUSD", ctx(equity=10_000.0), risk, [])
    assert g3.single_leg_tp is None


# ------------------------------------------------------------------ translation of plan and actions
def test_translate_shifts_management_prices_and_actions_but_not_indices():
    r = rec()
    r["management"] = [{"action": "move_sl_to_breakeven", "trigger": "tp_hit", "value": 1, "params": {}},
                       {"action": "close_all", "trigger": "price_reached", "value": 4320.0, "params": {}},
                       {"action": "trail_atr", "trigger": "r_multiple", "value": 1.5, "params": {"atr_mult": 1.5}}]
    r["position_actions"] = [{"target": {"decision": "abcd1234", "kind": "position"}, "action": "modify_sl",
                              "value": 4295.0, "reason": "tighten"},
                             {"target": {"decision": "abcd1234", "kind": "position"}, "action": "close",
                              "fraction": 0.5, "reason": "take profit"}]
    x = translate(r, 10.0, 0.01)
    assert [m["value"] for m in x["management"]] == [1, 4330.0, 1.5]
    assert x["position_actions"][0]["value"] == 4305.0 and x["position_actions"][1].get("value") is None
    assert r["management"][1]["value"] == 4320.0                        # the source is not modified


# ------------------------------------------------------------------ position-action targets (3.7 _finalize)
def test_actions_on_trades_the_model_did_not_see_are_dropped():
    payload = {"account": {"open_positions": [{"decision": "abcd1234"}], "pending_orders": [{"decision": "ef012345"}]}}
    acts = [{"target": {"decision": "abcd1234", "kind": "position"}, "action": "modify_sl", "value": 1.0, "reason": "x"},
            {"target": {"decision": "ef012345", "kind": "order"}, "action": "cancel_order", "reason": "x"},
            {"target": {"decision": "abcd1234", "kind": "order"}, "action": "cancel_order", "reason": "x"},
            {"target": {"decision": "99999999", "kind": "position"}, "action": "close", "reason": "x"}]
    notes: list[str] = []
    kept = Orchestrator._action_targets(copy.deepcopy(acts), payload, notes)
    assert [(a["action"], a["target"]["decision"]) for a in kept] == [("modify_sl", "abcd1234"),
                                                                      ("cancel_order", "ef012345")]
    assert len(notes) == 2 and all("dropped" in n for n in notes)


# ------------------------------------------------------------------ review fixes (Phase 3 adversarial review)
def test_a_repeated_section_in_the_local_config_is_refused(tmp_path):
    import yaml
    from tradingsystem.core.settings import DEFAULT_CONFIG
    local = tmp_path / "config.local.yaml"
    local.write_text("execution: {management: {dry_run: true}}\nexecution: {position_actions: {enabled: false}}\n",
                     encoding="utf-8")
    with pytest.raises(yaml.constructor.ConstructorError, match="duplicate key 'execution'"):
        load_settings(DEFAULT_CONFIG, local_path=local, env_path=Path("nope.env"))
    local.write_text("execution:\n  management: {dry_run: true}\n  position_actions: {enabled: false}\n",
                     encoding="utf-8")
    s = load_settings(DEFAULT_CONFIG, local_path=local, env_path=Path("nope.env"))
    assert s.execution.management.dry_run and not s.execution.position_actions.enabled


def test_one_system_can_turn_its_charts_off(tmp_path):
    from tradingsystem.core.settings import DEFAULT_CONFIG
    local = tmp_path / "config.local.yaml"
    local.write_text("instances:\n  XAUUSD: {api_port: 8768, magic_offset: 3, overrides: {ai: {charts: {enabled: false}}}}\n",
                     encoding="utf-8")
    xau = load_settings(DEFAULT_CONFIG, local_path=local, env_path=Path("nope.env"), extra_env={"TS_INSTANCE": "XAUUSD"})
    btc = load_settings(DEFAULT_CONFIG, local_path=local, env_path=Path("nope.env"), extra_env={"TS_INSTANCE": "BTCUSDT"})
    assert not xau.ai.charts.enabled and btc.ai.charts.enabled
    local.write_text("instances:\n  XAUUSD: {api_port: 8768, magic_offset: 3, overrides: {paths: {data_dir: x}}}\n",
                     encoding="utf-8")
    with pytest.raises(ValueError, match="may not set"):
        load_settings(DEFAULT_CONFIG, local_path=local, env_path=Path("nope.env"), extra_env={"TS_INSTANCE": "XAUUSD"})


def test_holdings_without_a_basis_are_not_drawn_in_the_wrong_prices():
    from tradingsystem.analysis.charts import overlay_spec
    p = {"meta": {"price_reference": "binance_spot:BTCUSDT"}, "market": {},
         "account": {"holdings_price_space": "mt5:BTCUSD@",
                     "open_positions": [{"decision": "abcdef12", "price": 84343.0, "sl": 84085.6, "tps": [84595.15]}]}}
    assert overlay_spec(p, "15m", ["holdings"])["holdings"] == []
    p["market"]["basis_exec_minus_analysis"] = 43.0
    [h] = overlay_spec(p, "15m", ["holdings"])["holdings"]
    assert h["entry"] == pytest.approx(84300.0)
    same = {"meta": {"price_reference": "mt5:XAUUSD@"}, "market": {},
            "account": {"holdings_price_space": "mt5:XAUUSD@", "open_positions": [{"decision": "x", "price": 4300.0}]}}
    assert overlay_spec(same, "15m", ["holdings"])["holdings"][0]["entry"] == 4300.0


def test_the_action_limits_in_rule_13_are_the_configured_ones():
    from tradingsystem.ai.prompts import render
    s = load_settings(extra_env={"TS_INSTANCE": "BTCUSDT"})
    o = object.__new__(Orchestrator)
    o.s, o.reg = s, InstrumentRegistry.from_settings(s)
    pr = render("agent_per_pair", o._system_vars("BTCUSDT", {}), o._user_vars("BTCUSDT", 1790334600000, "t", "{}"))
    pa = s.execution.position_actions
    assert f"every {pa.min_minutes_between_sl_changes} minutes and {pa.max_per_pair_per_day} applied actions" in pr.system
    assert "turns it into `price_reached`" in pr.system and "re-enter on a later cycle" in pr.system
