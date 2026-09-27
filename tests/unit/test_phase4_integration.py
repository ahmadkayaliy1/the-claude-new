"""Phase 4 integration by the lead: the adaptive overlay reaches every consumer (engine spacing/thresholds/pause,
executor confidence floor, orchestrator playbook/hint/hashes/attribution), the usage gauge only rations when enforced,
operator sessions do not use the pairs' request quota, critical notifications are never rate-limited, an alias bomb in
adaptive.yaml is refused cheaply, and the executor announces orders, outcomes and switch transitions."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from tradingsystem.ai.budget import UsageStore
from tradingsystem.core import adaptive as ad
from tradingsystem.core.settings import PathsCfg, load_settings
from tradingsystem.core.tunables import Tunables, config_tunables, tunables_of

from .test_orchestrator import make  # noqa: F401 — the orchestrator fixture (scripted provider)
from .test_position_actions import live  # noqa: F401 — the paper executor fixture


@pytest.fixture
def s(tmp_path):
    base = load_settings(env_path=Path("nope.env"), extra_env={"TS_INSTANCE": "BTCUSDT"})
    return base.model_copy(update={"paths": PathsCfg(data_dir=str(tmp_path / "data"), logs_dir=str(tmp_path / "logs"),
                                                     instance="BTCUSDT")})


def test_without_an_overlay_every_consumer_gets_the_configured_values(s):
    t = Tunables(s)
    got, want = t.get("BTCUSDT"), config_tunables(s)
    for k in ("min_confidence", "min_minutes_between_calls", "max_idle_minutes", "review_floor_minutes", "weak_min",
              "liquidity_atr"):
        assert getattr(got, k) == getattr(want, k), k
    assert got.adaptive_hash is None and got.playbook == "" and got.ai_paused_until_ms is None
    assert tunables_of(NS(s=s), "BTCUSDT") == want                     # objects built bare in tests
    off = s.model_copy(update={"adaptive": s.adaptive.model_copy(update={"enabled": False})})
    assert Tunables(off).get("BTCUSDT") == config_tunables(off)


def test_a_broken_overlay_module_never_stops_a_service(s, monkeypatch):
    class Boom:
        def __init__(self, *a, **k):
            raise RuntimeError("disk gone")
    monkeypatch.setattr(ad, "AdaptiveStore", Boom)
    assert Tunables(s).get("BTCUSDT") == config_tunables(s)


def test_an_alias_bomb_in_adaptive_yaml_is_refused_without_expanding_it():
    bomb = "a: &a [x,x,x,x,x,x,x,x,x]\nb: &b [*a,*a,*a,*a,*a,*a,*a,*a,*a]\nc: &c [*b,*b,*b,*b,*b,*b,*b,*b,*b]\n" \
           "d: &d [*c,*c,*c,*c,*c,*c,*c,*c,*c]\ne: &e [*d,*d,*d,*d,*d,*d,*d,*d,*d]\nf: [*e,*e,*e,*e,*e,*e,*e,*e,*e]\n"
    t0 = time.perf_counter()
    with pytest.raises(ValueError, match="aliases"):
        ad.load_cfg_text(bomb, max_expiry_days=14)
    assert time.perf_counter() - t0 < 1.0
    with pytest.raises(ValueError, match="larger"):
        ad.load_cfg_text("a: 1\n" * 60_000, max_expiry_days=14)


def test_operator_sessions_do_not_use_the_pairs_request_quota(tmp_path):
    u = UsageStore(tmp_path / "ai_usage.db")
    try:
        for role in ("decision", "escalation", "review", "diagnose"):
            u.record(None, provider="claude_code", model="m", purpose="x", pair="BTCUSDT", ok=True, role=role)
        assert u.count_since("claude_code", 0) == 2
        assert u.count_role_since("review", 0) == 1                       # still in the ledger (usage gauge)
    finally:
        u.close()


def test_a_critical_notification_is_never_rate_limited(s, monkeypatch):
    from tradingsystem.core import notify as nt
    monkeypatch.delenv("TS_NOTIFY_DISABLE", raising=False)
    cfg = s.notify.model_copy(update={"rate_per_hour": 1, "dedupe_minutes": 0, "telegram": False})
    s2 = s.model_copy(update={"notify": cfg})
    n = nt.Notifier() if hasattr(nt, "Notifier") else None
    if n is None:
        pytest.skip("notifier class not exposed")
    shown = []
    monkeypatch.setattr(n, "_toast_wanted", lambda item: (True, ""), raising=False)
    monkeypatch.setattr(n, "_toast", lambda item: shown.append(item.title) or "shown", raising=False)
    results = [n.process(nt._Item(s2, lvl, f"t{i}", "x", None, None, int(time.time() * 1000), {}))
               for i, lvl in enumerate(("info", "info", "critical"))]
    assert [bool(r.get("rate_limited")) for r in results] == [False, True, False]


# ------------------------------------------------------------------ the overlay reaches the consumers
def overlay(s, **kw):
    from dataclasses import replace
    return replace(config_tunables(s), **kw)


class FixedTunables:
    def __init__(self, t):
        self.t = t

    def get(self, pair, now_ms=None):
        return self.t


def test_the_engine_uses_the_pairs_overlay_for_spacing_thresholds_and_pause(tmp_path, monkeypatch):
    from tradingsystem.analysis import engine as eng_mod
    from tradingsystem.analysis.engine import Engine
    base = load_settings(env_path=Path("nope.env"))
    s = base.model_copy(update={"paths": base.paths.model_copy(update={"data_dir": str(tmp_path)})})
    e = Engine(s)
    try:
        e.tunables = FixedTunables(overlay(s, min_minutes_between_calls=45, max_idle_minutes=200,
                                           review_floor_minutes=20, weak_min=3, liquidity_atr=0.2))
        seen = {}
        from tradingsystem.ai.triggers import TriggerDecision
        monkeypatch.setattr(eng_mod, "decide", lambda *a, **k: seen.update(k) or TriggerDecision(False, [], "none"))
        e.evaluate("XAUUSD", 1790334600000, False, None, "hybrid")
        assert (seen["min_spacing_min"], seen["max_idle_min"], seen["review_floor_min"], seen["weak_min"],
                seen["liquidity_atr"]) == (45, 200, 20, 3, 0.2)
        e.fails["XAUUSD"] = 1
        assert e._backoff_ms("XAUUSD") == 90 * 60_000                  # 45 × 2^1 (the overlay's spacing)
        now = 1790334600000
        e.tunables = FixedTunables(overlay(s, ai_paused_until_ms=now + 3_600_000))
        assert e._unpaused([("XAUUSD", ["x"], "strong", None)], now) == []
        assert e._unpaused([("XAUUSD", ["x"], "strong", None)], now + 3_600_001)
    finally:
        e.close()


def test_the_gauge_rations_only_when_enforced(tmp_path, monkeypatch):
    from tradingsystem.analysis.engine import Engine
    base = load_settings(env_path=Path("nope.env"))
    s = base.model_copy(update={"paths": base.paths.model_copy(update={"data_dir": str(tmp_path)})})
    e = Engine(s)
    try:
        monkeypatch.setattr(e.orch, "quota", lambda: (None, None))
        fired = [("a", [], "strong", None), ("b", [], "review", None), ("c", [], "event", None)]
        monkeypatch.setattr(e, "_gauge_state", lambda: NS(enforce=False, level=2, reason="95 %"))
        assert e._ration(fired, 0) == fired                            # observe only (the default)
        monkeypatch.setattr(e, "_gauge_state", lambda: NS(enforce=True, level=1, reason="75 %"))
        assert [f[0] for f in e._ration(fired, 0)] == ["b", "c"]
        monkeypatch.setattr(e, "_gauge_state", lambda: NS(enforce=True, level=2, reason="95 %"))
        assert [f[0] for f in e._ration(fired, 0)] == ["c"]
        monkeypatch.setattr(e, "_gauge_state", lambda: None)            # unreadable gauge: never a reason to stop
        assert e._ration(fired, 0) == fired
    finally:
        e.close()


def test_the_orchestrator_shows_the_playbook_and_records_hashes_attribution_and_versions(tmp_path, monkeypatch):
    import asyncio
    from .test_orchestrator import FakeBuilder, Scripted, rec_for
    from tradingsystem.ai import orchestrator as orch_mod
    from tradingsystem.ai.budget import AIBudgetCfg, CostGovernor
    from tradingsystem.ai.orchestrator import CycleRequest, Orchestrator
    from tradingsystem.ai.store import DecisionStore
    from tradingsystem.core.instruments import InstrumentRegistry
    s = load_settings(env_path=Path("nope.env"), extra_env={"AGENT_MODE": "agent_per_pair"})
    prov = Scripted(lambda schema, user: rec_for())
    monkeypatch.setattr(orch_mod, "make_provider", lambda settings, name=None, **kw: prov)
    usage, store = UsageStore(tmp_path / "app.db"), DecisionStore(tmp_path / "app.db", "cfg")
    o = Orchestrator(s, InstrumentRegistry.from_settings(s), FakeBuilder(), store, usage, CostGovernor(AIBudgetCfg(), usage))
    o.tunables = FixedTunables(overlay(s, playbook="- fade the Asia high only after a 15m close back inside",
                                       tp_hint="TP1 at the nearest 1h liquidity", playbook_hash="p" * 16,
                                       adaptive_hash="a" * 16, min_confidence=70))
    [r] = asyncio.run(o.run_cycle([CycleRequest("XAUUSD", "t", "strong")], as_of=1790334600000))
    system, user = prov.calls[0][1], prov.calls[0][2]
    assert "fade the Asia high" in user and "TP1 at the nearest 1h liquidity" in user
    assert "70" in system                                              # the confidence floor in force
    row = store._con.execute("SELECT playbook_hash, adaptive_hash, session, htf_bias, regime, setup_kinds, "
                             "data_warnings FROM ai_decisions WHERE id=?", (r.id,)).fetchone()
    assert row[0] == "p" * 16 and row[1] == "a" * 16
    assert row[3] in ("bullish", "bearish", "mixed") and json.loads(row[5]) is not None and json.loads(row[6]) is not None
    assert store._con.execute("SELECT count(*) FROM prompt_versions WHERE role='agent_per_pair'").fetchone()[0] == 1


class SwitchingTunables:
    """The overlay as ``tools/tune.py`` (or an expiring entry) may change it while a model call runs."""

    def __init__(self, t):
        self.now = t

    def get(self, pair, now_ms=None):
        return self.now


ASSESSMENT = {"pair": "XAUUSD", "timeframe": "1h", "bias": "bullish", "confidence": 60, "structure": "HH/HL",
              "key_levels": [{"price": 4295.76, "kind": "liquidity_high"}]}


@pytest.mark.parametrize("mode", ["agent_per_pair", "agent_per_pair_with_risk_reviewer", "agent_per_pair_and_timeframe",
                                  "agent_per_timeframe", "single_agent_global", "multi_provider_consensus"])
def test_a_decision_is_attributed_to_the_overlay_its_prompts_were_rendered_with(make, monkeypatch, mode):
    """A new playbook lands during the first model call: every prompt of the unit (escalation, risk review,
    coordinator) still shows the overlay the unit started with, and the record keeps that overlay's hashes (None
    included — no playbook yet) and its liquidity_atr for the setup kinds."""
    import asyncio
    from tradingsystem.ai import triggers
    from tradingsystem.ai.orchestrator import CycleRequest
    from .test_orchestrator import rec_for

    def answer(schema, user):
        tn.now = after                                                 # the overlay changed during the call
        return {"RiskReview": {"verdict": "approve", "issues": [], "final_recommendation": rec_for()},
                "EscalationReview": {"verdict": "confirm", "issues": [], "confidence": 60,
                                     "final_recommendation": rec_for()},
                "TimeframeAssessment": ASSESSMENT, "AssessmentSet": {"assessments": [ASSESSMENT]},
                "RecommendationSet": {"recommendations": [rec_for()]}}.get(schema) or rec_for()

    o, prov, store = make(mode, answer, consensus=["claude_code", "claude_code"])
    esc = o.s.ai.escalation.model_copy(update={"enabled": True})
    o.s = o.s.model_copy(update={"ai": o.s.ai.model_copy(update={"escalation": esc})})
    before = overlay(o.s, min_confidence=71, liquidity_atr=0.31, adaptive_hash="a" * 16)
    after = overlay(o.s, playbook="- only fade the Asia high", playbook_hash="b" * 16, adaptive_hash="c" * 16,
                    min_confidence=77, liquidity_atr=0.99)
    tn = o.tunables = SwitchingTunables(before)
    atr = []
    orig = triggers.scan_setups
    monkeypatch.setattr(triggers, "scan_setups", lambda *a, **k: (atr.append(k.get("liquidity_atr")), orig(*a, **k))[1])
    [r] = asyncio.run(o.run_cycle([CycleRequest("XAUUSD", "t", "strong")], as_of=1790334600000))
    assert prov.calls and tn.now is after
    for schema, system, user in prov.calls:
        assert "nothing below 77" not in system and "only fade the Asia high" not in user, schema
    # a prompt rendered after the change (the escalation, the coordinator after its analysts) shows the snapshot
    late = {"agent_per_pair_and_timeframe": "Recommendation", "agent_per_timeframe": None,
            "single_agent_global": None}.get(mode, "EscalationReview")
    if late:
        shown = [c[1] for c in prov.calls[1:] if c[0] == late]
        assert shown and all("nothing below 71" in text for text in shown)
    rows = store._con.execute("SELECT playbook_hash, adaptive_hash FROM ai_decisions").fetchall()
    assert rows == [(None, "a" * 16)] and (r.playbook_hash, r.adaptive_hash) == (None, "a" * 16)
    assert atr == [0.31]


def test_a_record_without_a_rendered_prompt_is_attributed_as_of_now(make):
    """A paused cycle (or a failed snapshot, or a cancelled unit) rendered nothing: the overlay in force now."""
    import asyncio
    from tradingsystem.ai.orchestrator import CycleRequest
    o, prov, store = make("agent_per_pair", lambda schema, user: {})
    o.tunables = FixedTunables(overlay(o.s, playbook_hash="p" * 16, adaptive_hash="a" * 16))
    o.effective_mode = lambda: ("paused", "Cost Governor paused AI calls")
    [r] = asyncio.run(o.run_cycle([CycleRequest("XAUUSD", "t", "strong")], as_of=1790334600000))
    assert r.status == "budget_blocked" and not prov.calls and r.tunables is None
    assert (r.playbook_hash, r.adaptive_hash) == ("p" * 16, "a" * 16)


def test_the_executor_gate_uses_the_overlay_confidence_floor(live, monkeypatch):
    from tradingsystem.execution import executor as ex_mod
    ex, ticks = live
    ex.tunables = FixedTunables(overlay(ex.s, min_confidence=72))
    seen = {}

    def gate(rec, pair, ctx, risk, groups, *, min_confidence):
        seen["floor"] = min_confidence
        raise RuntimeError("stop after the gate call")
    monkeypatch.setattr(ex_mod, "evaluate", gate)
    monkeypatch.setattr(ex, "atr", lambda pair, as_of=None: 5.0)        # the fixture holds ticks, not candles
    q = ticks[-1]
    from tradingsystem.core.timeutil import iso, now_ms
    rec = {"decision": "BUY", "order_type": "MARKET", "stop_loss": round(q.bid - 20, 2), "confidence": 71,
           "valid_until": iso(now_ms() + 3_600_000), "take_profits": [{"price": round(q.ask + 40, 2), "close_fraction": 1.0}],
           "entry": {"price": None}, "timestamp": iso(now_ms()), "pair": "XAUUSD", "price_reference": "mt5:XAUUSD@"}
    with pytest.raises(RuntimeError, match="stop after the gate"):
        ex._handle({"pair": "XAUUSD", "rec": rec, "id": "g" * 32})
    assert seen["floor"] == 72


def test_the_config_command_prints_the_phase4_switches_and_never_the_telegram_values(s, monkeypatch, tmp_path):
    from tradingsystem import cli
    from tradingsystem.ai.providers import base
    monkeypatch.setattr(base, "ENV_FILE", tmp_path / "none.env")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    line = cli._phase4_line(s)
    assert line.startswith("phase 4: adaptive=on ") and "telegram=not configured" in line
    assert "gauge=observe only" in line and "sessions=on" in line
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456789:" + "A" * 35)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "987654321")
    s.paths.data().mkdir(parents=True, exist_ok=True)
    (s.paths.data() / "TUNING_FREEZE").write_text("x", encoding="utf-8")
    off = s.model_copy(update={"adaptive": s.adaptive.model_copy(update={"enabled": False})})
    line = cli._phase4_line(off)
    assert "adaptive=OFF (TUNING_FREEZE)" in line and "telegram=configured" in line
    assert "123456789" not in line and "987654321" not in line
