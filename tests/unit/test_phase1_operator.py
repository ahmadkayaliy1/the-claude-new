"""Phase 1 (docs/handoff_operator_v2.md): the model's compact view, operator memory, performance feedback, gate
reasons in the history, persisted usage extras, and the Claude CLI token-refresh race. The payload is the real
fixture (tests/fixtures/real/payload_xauusd.json); stores run on temporary databases."""
import asyncio
import json
import re
import time
from pathlib import Path

import pytest

from tradingsystem.ai.budget import UsageStore
from tradingsystem.ai.contract import Recommendation
from tradingsystem.ai.model_view import RECENT, VIEW_VERSION, model_view, short_time, view
from tradingsystem.ai.providers import claude_code as cc
from tradingsystem.ai.providers.base import LLMResult
from tradingsystem.ai.store import DecisionRecord, DecisionStore
from tradingsystem.core.settings import AIProviderCfg

from .test_orchestrator import rec_for

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "real" / "payload_xauusd.json"
ISO = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d")


@pytest.fixture
def payload():
    return json.loads(FIX.read_text())


# ------------------------------------------------------------------ model view
def test_view_is_smaller_and_keeps_every_level(payload):
    a = json.dumps(payload, separators=(",", ":"))
    v = model_view(payload)
    b = json.dumps(v, separators=(",", ":"))
    assert len(b) < 0.75 * len(a)
    assert len(ISO.findall(b)) == 1 and v["meta"]["as_of"] == payload["meta"]["as_of"]   # only the cycle time
    assert v["meta"]["view_version"] == VIEW_VERSION
    for tf, t in payload["timeframes"].items():
        w = v["timeframes"][tf]
        if "zones" in t:                 # every zone and every level survives, as a row
            for kind, zones in t["zones"].items():
                assert [r[1:3] for r in w["zones"][kind]] == [[z["top"], z["bottom"]] for z in zones]
        if t.get("structure", {}).get("events"):
            assert [r[3] for r in w["structure"]["events"]] == [e["level"] for e in t["structure"]["events"]]
        if "recent" in t:
            assert w["recent"] == [[short_time(r[0], v["meta"]["as_of"][:4]), *r[1:]]
                                   for r in t["recent"][-RECENT[tf]:]]
        assert w["data"] == "ok" or isinstance(w["data"], dict)


def test_view_leaves_the_stored_payload_untouched(payload):
    before = json.dumps(payload, sort_keys=True)
    view([payload, payload])
    assert json.dumps(payload, sort_keys=True) == before


def test_short_time():
    assert short_time("2026-09-26T08:15:00.000Z", "2026") == "09-26 08:15"
    assert short_time("2025-12-31T23:00:00Z", "2026") == "2025-12-31 23:00"
    assert short_time("mt5:XAUUSD@", "2026") == "mt5:XAUUSD@"


def test_capabilities_grouped(payload):
    caps = model_view(payload)["capabilities"]
    reals = [k for k, c in payload["capabilities"].items() if c["quality"] == "real"]
    assert caps.get("real", []) == reals
    for k, c in payload["capabilities"].items():
        if c["quality"] != "real":
            assert caps[c["quality"]][k] == c["reason"]


# ------------------------------------------------------------------ memory, performance, gate reason
@pytest.fixture
def store(tmp_path):
    s = DecisionStore(tmp_path / "app.db", "cfg")
    yield s
    s.close()


def test_operator_notes_are_part_of_the_contract():
    r = rec_for()
    r["operator_notes"] = "Waiting for a 15m close above 4296 " * 40
    v = Recommendation.model_validate(r)
    assert len(v.operator_notes) == 600                      # trimmed, never a reason to reject
    assert Recommendation.model_validate(rec_for()).operator_notes == ""


def test_memory_returns_the_latest_valid_notes(store):
    assert store.memory("XAUUSD") == {}
    r = rec_for()
    r["operator_notes"] = "Long idea at the 15m OB; cancel if 4284 breaks."
    store.save(DecisionRecord("XAUUSD", "agent_per_pair", "t", "valid", recommendation=r, ts=1_000))
    store.save(DecisionRecord("XAUUSD", "agent_per_pair", "t", "error", ts=2_000))
    m = store.memory("XAUUSD")
    assert m["notes"].startswith("Long idea") and m["decision"] == "BUY" and m["execution_state"] == "not_executed"


def test_history_carries_the_gate_reason(store):
    rec = DecisionRecord("XAUUSD", "agent_per_pair", "t", "valid", recommendation=rec_for())
    store.save(rec)
    store.set_execution_state(rec.id, "rejected", {"gate": [{"check": "spread_vs_sl", "ok": False}],
                                                   "reason": "spread_vs_sl: spread 2.16 > 20% of SL distance 9.69"})
    h = store.recent("XAUUSD")[0]
    assert h["rejected_by"] == "gate" and h["gate_reason"].startswith("spread_vs_sl")
    rec2 = DecisionRecord("XAUUSD", "agent_per_pair", "t", "valid", recommendation=rec_for(), ts=rec.ts + 1)
    store.save(rec2)
    store.set_execution_state(rec2.id, "rejected", {"gate": [], "backend": {"ok": False,
                                                    "reason": "order_send ts:x:1: 10016 Invalid stops"}})
    h = store.recent("XAUUSD")[0]
    assert h["rejected_by"] == "broker" and "10016" in h["reject_reason"] and "gate_reason" not in h


def test_performance_counts(store):
    now = int(time.time() * 1000)
    for dec, vo, vr, ex in (("BUY", "tp1_first", 1.4, "rejected"), ("SELL", "sl_first", -1.0, "executed"),
                            ("NO_TRADE", None, None, "not_executed")):
        r = rec_for(decision=dec if dec != "SELL" else "NO_TRADE")
        r["decision"] = dec
        rec = DecisionRecord("XAUUSD", "agent_per_pair", "t", "valid", recommendation=r, ts=now - 3600_000)
        store.save(rec)
        detail = json.dumps({"gate": [{"check": "rr_after_costs", "ok": False}]}) if ex == "rejected" else None
        store._con.execute("UPDATE ai_decisions SET execution_state=?, execution_detail=?, virtual_outcome=?, "
                           "virtual_r=? WHERE id=?", (ex, detail, vo, vr, rec.id))
    store.save(DecisionRecord("XAUUSD", "agent_per_pair", "t", "skipped", ts=now))
    p = store.performance("XAUUSD", now=now)
    assert p["cycles"] == 3 and p["trade_ideas"] == 2 and p["no_trade"] == 1
    assert p["gate_rejected"] == 1 and p["executed"] == 1 and p["not_placed"] == 0 and p["expired"] == 0
    assert p["virtual"]["tp1_first"] == 1 and p["virtual"]["sl_first"] == 1 and p["virtual"]["mean_r"] == 0.2
    assert store.performance("BTCUSDT", now=now) == {}


# ------------------------------------------------------------------ usage extras
def test_usage_extras_are_persisted(tmp_path):
    u = UsageStore(tmp_path / "app.db")
    UsageStore(tmp_path / "app.db").close()                 # a second opener must not fail on the migration
    res = LLMResult("claude_code", "claude-sonnet-5", "{}", {}, input_tokens=21848, output_tokens=4945,
                    extra={"api_equivalent_usd": 0.137, "num_turns": 1, "cache_creation_input_tokens": 1200})
    u.record(res, provider="claude_code", model="claude-sonnet-5", purpose="agent_per_pair", pair="BTCUSDT", ok=True)
    row = u._con.execute("SELECT api_equivalent_usd, num_turns, cache_creation_tokens FROM ai_usage").fetchone()
    assert row == (0.137, 1, 1200)
    u.close()


# ------------------------------------------------------------------ Claude CLI: refresh race, stagger, schema
@pytest.fixture
def prov(tmp_path, monkeypatch):
    exe = tmp_path / "claude.exe"
    exe.write_bytes(b"")
    monkeypatch.setattr(cc.tempfile, "gettempdir", lambda: str(tmp_path))
    cfg = AIProviderCfg(kind="claude_code", model="sonnet", free_tier=True, cli_path=str(exe),
                        structured_output="prompt")
    return cc.ClaudeCodeProvider("claude_code", cfg, "sonnet", None)


def test_token_refresh_race_is_retried_not_a_sign_out(prov):
    """Review: retryable (the repair loop retries it after a short back-off), NOT rate_limited (that returns at once)."""
    e = prov._error("Failed to refresh OAuth token: another Claude Code process is refreshing it or exited "
                    "mid-refresh.", None)
    assert e.retryable and not e.rate_limited and prov.cooldown_until_ms == 0


def test_auth_refusal_is_retried_and_triggers_a_sign_in_recheck(prov):
    prov._auth_checked = time.monotonic()
    e = prov._error("Failed to authenticate. API Error: 403 Request not allowed", None)
    assert e.retryable and not e.rate_limited and prov._auth_checked == 0.0 and prov.cooldown_until_ms == 0


def test_a_long_lived_token_is_ready_without_a_status_check(tmp_path, monkeypatch):
    exe = tmp_path / "claude.exe"
    exe.write_bytes(b"")
    monkeypatch.setattr(cc.tempfile, "gettempdir", lambda: str(tmp_path))
    cfg = AIProviderCfg(kind="claude_code", model="sonnet", free_tier=True, cli_path=str(exe))
    assert cc.ClaudeCodeProvider("claude_code", cfg, "sonnet", "oat-token").unavailable_reason() is None


def test_memory_keeps_the_latest_notes_when_a_later_decision_has_none(store):
    r = rec_for()
    r["operator_notes"] = "Plan A"
    store.save(DecisionRecord("XAUUSD", "agent_per_pair", "t", "valid", recommendation=r, ts=1_000))
    store.save(DecisionRecord("XAUUSD", "agent_per_pair", "t", "valid", recommendation=rec_for(), ts=2_000))
    assert store.memory("XAUUSD")["notes"] == "Plan A"


def test_view_semantic_nulls_and_gaps(payload):
    import copy
    p = copy.deepcopy(payload)
    p["orderflow"]["divergence_test"] = None
    p["market"]["session"]["killzone"] = None
    tf = next(iter(p["timeframes"]))
    p["timeframes"][tf]["quality"] = {"status": "stale", "coverage": 0.0, "gaps": [[1790334600000, 2]], "stale": True}
    v = model_view(p)
    assert v["market"]["session"]["killzone"] == "none"                  # nothing detected, said explicitly
    assert "divergence_test" not in v["orderflow"]                       # unknown → omitted
    from tradingsystem.core.timeutil import iso
    assert v["timeframes"][tf]["data"] == {"status": "stale", "coverage": 0.0, "stale": True,
                                           "gaps": [[short_time(iso(1790334600000), v["meta"]["as_of"][:4]), 2]]}


def test_analyst_slice_keeps_its_candles(payload):
    tf = next(k for k in ("1h", "4h", "15m") if payload["timeframes"][k].get("recent"))
    t = payload["timeframes"][tf]
    assert len(t["recent"]) > RECENT.get(tf, 0) or tf == "15m"
    v = model_view({"meta": payload["meta"], "timeframe": {tf: t}})
    assert len(v["timeframe"][tf]["recent"]) == len(t["recent"])


def test_cli_starts_are_staggered(prov, monkeypatch):
    monkeypatch.setattr(cc, "START_STAGGER_S", 0.2)
    starts = []

    async def go():
        async def one():
            await prov._staggered_start()
            starts.append(time.monotonic())
        await asyncio.gather(one(), one())
    asyncio.run(go())
    assert abs(starts[1] - starts[0]) >= 0.19


def test_prompt_schema_has_no_titles(prov):
    text = prov.system_text("RULES", Recommendation.model_json_schema())
    assert '"title"' not in text and '"operator_notes"' in text and '"maxLength":600' in text
