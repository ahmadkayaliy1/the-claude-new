"""Review pack (§3.8 component 3): a seeded pair system → a bounded markdown + JSON pack, read-only."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
import sys
import types
from pathlib import Path

import pytest

from tradingsystem.ai.budget import UsageStore
from tradingsystem.ai.providers.base import LLMResult
from tradingsystem.ai.store import DecisionRecord, DecisionStore
from tradingsystem.core.settings import INSTANCE_ENV, load_settings
from tradingsystem.core.timeutil import MS_PER_HOUR, MS_PER_MINUTE, iso, now_ms
from tradingsystem.execution import management
from tradingsystem.ingest.common.appdb import AppDB
from tradingsystem.supervisor import procs

ROOT = Path(__file__).resolve().parents[2]
PAIR = "BTCUSDT"


def tool(name: str):
    spec = importlib.util.spec_from_file_location(f"test_{name}", ROOT / "tools" / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture()
def rp(monkeypatch):
    m = tool("review_pack")
    monkeypatch.setattr(m, "git_info", lambda root: {"sha": "abc1234", "branch": "main", "last_commits":
                                                     ["abc1234 2026-09-27 test commit"], "dirty": False,
                                                     "dirty_files": 0, "dirty_list": []})
    monkeypatch.setattr(procs, "running_supervisors", lambda older_s=None: {})     # never the production pids
    return m


def settings_at(tmp_path: Path, instance: str | None = PAIR):
    s = load_settings(extra_env={INSTANCE_ENV: instance or ""})
    return s.model_copy(update={"paths": s.paths.model_copy(update={"data_dir": str(tmp_path / "data"),
                                                                     "logs_dir": str(tmp_path / "logs")})})


def _rec(ts: int, i: int, **kw) -> DecisionRecord:
    side = "BUY" if i % 2 else "SELL"
    r = {"decision": side, "order_type": "LIMIT", "confidence": 60 + i % 20, "stop_loss": 60_000 + i,
         "entry": {"price": 61_000 + i}, "take_profits": [{"price": 62_000 + i, "close_fraction": 1.0}],
         "market_summary": f"idea {i}: " + "range rotation into the prior day high with absorption " * 6,
         "operator_notes": "watch the Asia range", "valid_until": iso(ts + 3_600_000)}
    return DecisionRecord(pair=PAIR, mode="agent_per_pair", trigger="setup", status="valid", recommendation=r,
                          ts=ts, trigger_strength="strong" if i % 3 else "weak", input_tokens=25_000,
                          output_tokens=4_000, latency_ms=60_000, prompt_hash="p" * 16, library_hash="l" * 16,
                          setup_kinds=["structure", "location"], session="london" if i % 2 else "asia",
                          regime="trend", htf_bias="bullish", playbook_hash="b" * 16, adaptive_hash="a" * 16,
                          **kw)


def seed(s, now: int, n_ideas: int = 30) -> dict:
    """A pair system with decisions (ideas, NO_TRADE, errors), gate rejections, outcomes, metrics, position actions,
    an escalation, a tuning row, heartbeats, the shared ledger, the adaptive files and the engine log."""
    db = s.paths.state() / "app.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    app = AppDB(db)
    app.set_status("engine", "live", detail={"ai_ready": True, "provider": "claude_code", "quota_left_today": 30})
    app.set_status("executor", "live", detail={"equity": 103.85, "today_pnl_pct": 1.2, "exposure": [],
                                               "kill_switch": False})
    app.add_event("executor", "order", f"paper {PAIR} 1234abcd: placed")
    app.add_event("supervisor:engine", "exited", "code 1 after 10s")
    app.close()
    store = DecisionStore(db, config_hash="c" * 16)
    ids = []
    for i in range(n_ideas):
        rec = _rec(now - (n_ideas - i) * 20 * MS_PER_MINUTE, i)
        ids.append(store.save(rec))
    for i in range(5):
        store.save(DecisionRecord(pair=PAIR, mode="agent_per_pair", trigger="idle", status="valid",
                                  recommendation={"decision": "NO_TRADE", "confidence": 40, "market_summary": "nothing",
                                                  "operator_notes": "watch the Asia range"}, ts=now - i * MS_PER_HOUR))
    store.save(DecisionRecord(pair=PAIR, mode="agent_per_pair", trigger="setup", status="error",
                              errors=["claude_code: timeout"], ts=now - 2 * MS_PER_HOUR))
    old = store.save(_rec(now - 72 * MS_PER_HOUR, 99))                 # outside a 24-h window
    con = sqlite3.connect(db)
    gate = {"gate": [{"check": "rr_after_costs", "ok": False, "detail": "1.2 < 1.5"},
                     {"check": "position_size", "ok": True, "detail": "0.01 lots"}], "reason": "rr_after_costs: 1.2"}
    for k, did in enumerate(ids):
        if k % 3 == 0:
            con.execute("UPDATE ai_decisions SET execution_state='rejected', execution_detail=? WHERE id=?",
                        (json.dumps(gate), did))
        elif k % 3 == 1:
            con.execute("UPDATE ai_decisions SET execution_state='executed', execution_detail=?, outcome='tp', "
                        "outcome_pnl_usd=2.5 WHERE id=?", (json.dumps({"mode": "paper"}), did))
        con.execute("UPDATE ai_decisions SET virtual_outcome=?, virtual_r=? WHERE id=?",
                    ("tp1_first" if k % 2 else "sl_first", 1.5 if k % 2 else -1.0, did))
        con.execute("INSERT INTO decision_metrics (decision_id, computed_ms, mfe_r, mae_r, tp1_hit, minutes_to_resolve, "
                    "exit_reason, spread_at_gate, rejected_but_virtual_win) VALUES (?,?,?,?,?,?,?,?,?)",
                    (did, now, 1.4, -0.6, k % 2, 95, "tp" if k % 2 else "sl", 0.8, int(k % 3 == 0 and k % 2 == 1)))
    for stmt in management._DDL:
        con.execute(stmt)
    one, esc = ids[min(1, len(ids) - 1)], ids[min(4, len(ids) - 1)]
    con.execute("INSERT INTO position_actions (ts, pair, source, source_decision, seq, target_decision, leg, action, "
                "requested, status, detail) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (now - MS_PER_HOUR, PAIR, "model", ids[-1], 0, one, "123", "modify_sl", '{"value": 61000}',
                 "applied", json.dumps({"reason": "tighten to breakeven"})))
    con.execute("INSERT INTO position_actions (ts, pair, source, source_decision, seq, target_decision, leg, action, "
                "requested, status, detail) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (now - MS_PER_HOUR, PAIR, "rule", one, 1, one, "123", "set_sl", None, "rejected",
                 json.dumps({"reason": "would loosen the stop"})))
    con.execute("INSERT INTO management_state VALUES (?,?,?,?,?,?,?)", (one, 0, "123", "applied", now, now, None))
    con.execute("INSERT INTO ai_sub_outputs (decision_id, role, label, ok, output) VALUES (?,?,?,?,?)",
                (esc, "escalation", "strong setup", 1, json.dumps({"verdict": "downgrade", "issues": ["late entry"],
                                                                      "final_recommendation": {"decision": "NO_TRADE"}})))
    con.execute("INSERT INTO tuning_changes (ts, pair, key, old_value, new_value, reason, window_hours, expires_ms, "
                "review_id, actor) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (now - 30 * MS_PER_HOUR, PAIR, "min_confidence_floor", "55", "60", "low-confidence ideas lose", 168,
                 now + 10 * 24 * MS_PER_HOUR, "20260926T043000Z_daily", "operator"))
    con.commit()
    con.close()
    store.close()
    # the shared ledger: decisions, an escalation and an earlier review session
    ledger = s.paths.shared() / "ai_usage.db"
    u = UsageStore(ledger)
    for role, n, pair in (("decision", 6, PAIR), ("escalation", 1, PAIR), ("review", 1, None)):
        for _ in range(n):
            u.record(LLMResult("claude_code", "sonnet", "", None, input_tokens=25_000, output_tokens=4_000,
                               cached_input_tokens=15_000), provider="claude_code", model="sonnet",
                     purpose="agent_per_pair", pair=pair, ok=True, role=role)
    u.close()
    # adaptive files: one entry in force, one expired, a playbook, changes with a torn last line
    ad = s.paths.data() / "adaptive" / PAIR
    ad.mkdir(parents=True)
    (ad / "adaptive.yaml").write_text(
        "version: 1\n"
        f"min_confidence_floor: {{value: 60, set_ms: {now - 30 * MS_PER_HOUR}, expires_ms: {now + 10 * 24 * MS_PER_HOUR},"
        " reason: low-confidence ideas lose, window_hours: 168}\n"
        f"trigger:\n  weak_min: {{value: 3, set_ms: {now - 20 * 24 * MS_PER_HOUR}, expires_ms: {now - 6 * 24 * MS_PER_HOUR},"
        " reason: too many weak calls, window_hours: 168}\n", encoding="utf-8")
    (ad / "playbook.md").write_text("- London open sweeps of the Asia range reverse more often than they run\n"
                                    "- Skip the first 15 min after 13:30 UTC data\n", encoding="utf-8")
    (ad / "changes.jsonl").write_text(json.dumps({"ts": now - 30 * MS_PER_HOUR, "op": "set", "key": "min_confidence_floor",
                                                  "new": 60}) + "\n{\"ts\": 1, \"op\": \"se", encoding="utf-8")
    # the engine's screen log + one ERROR line
    logs = s.paths.logs()
    logs.mkdir(parents=True)
    lines = []
    for k, (fire, strength) in enumerate((("False", "none"), ("True", "strong"), ("False", "none"), ("True", "weak"))):
        ts = iso(now - (4 - k) * 5 * MS_PER_MINUTE).replace("Z", "+00:00")
        lines.append(json.dumps({"ts": ts, "level": "INFO", "proc": "engine", "logger": "engine",
                                 "msg": f"{PAIR} 5m close {iso(now)}: trigger={fire} ({strength}) new structure"}))
    lines.append(json.dumps({"ts": iso(now - 26 * MS_PER_HOUR).replace("Z", "+00:00"), "level": "INFO", "proc": "engine",
                             "logger": "engine", "msg": f"{PAIR} 15m close x: trigger=True (strong) old"}))
    lines.append(json.dumps({"ts": iso(now - MS_PER_HOUR).replace("Z", "+00:00"), "level": "ERROR", "proc": "engine",
                             "logger": "engine", "msg": "cycle failed"}))
    (logs / "engine.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (s.paths.shared() / "proposals.jsonl").write_text(
        json.dumps({"id": "2026-09-26-xau-sessions", "title": "XAU session filter", "status": "awaiting_user"})
        + "\n{\"id\": \"torn", encoding="utf-8")
    return {"db": db, "ids": ids, "old": old, "ledger": ledger}


def _digest(paths) -> dict:
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def test_seeded_pair_gives_a_bounded_pack_with_every_section(rp, tmp_path):
    s = settings_at(tmp_path)
    now = now_ms()
    seeded = seed(s, now)
    watched = [seeded["db"], seeded["ledger"], *(s.paths.data() / "adaptive" / PAIR).iterdir()]
    before = _digest(watched)
    pack = rp.build_pack(s, hours=24, kind="daily", systems=[s], notes=[], now=now, out_dir=tmp_path / "reviews")
    md = pack.md
    assert len(md) <= rp.MAX_MD_CHARS
    assert pack.md_path == tmp_path / "reviews" / f"{pack.pack_id}.md" and pack.md_path.read_text(encoding="utf-8") == md
    assert rp.NAME_RE.fullmatch(pack.pack_id) and pack.pack_id.endswith("_daily")
    data = json.loads(pack.json_path.read_text(encoding="utf-8"))
    f = data["per_pair"][PAIR]["funnel"]
    assert f["ideas"] == 30 and f["placed"] == 10                                    # the 72-h-old idea is out
    assert f["gate_failures_by_check"] == {"rr_after_costs": 10}
    assert f["by_status"] == {"valid": 35, "error": 1}
    assert f["broker_outcomes"] == {"tp": {"n": 10, "pnl_usd": 25.0}}
    assert f["virtual_resolved"] == 30 and f["virtual_tp1_first"] == 15
    assert f["metrics"]["n"] == 30 and f["metrics"]["mean_mfe_r"] == 1.4 and f["metrics"]["exit_reasons"] == {"tp": 15, "sl": 15}
    sc = data["per_pair"][PAIR]["screens"]
    assert sc["closes_by_tf"] == {"5m": 4} and sc["triggers_by_strength"] == {"strong": 1, "weak": 1}
    calls = data["per_pair"][PAIR]["calls_by_role"]
    assert calls["decision"]["calls"] == 6 and calls["escalation"]["calls"] == 1
    assert data["usage"]["by_role"]["review"]["calls"] == 1 and data["usage"]["total"]["cache_read_share"] == 0.6
    pa = data["per_pair"][PAIR]["position_actions"]
    assert pa["n"] == 2 and pa["by_source_action_status"] == {"model/modify_sl/applied": 1, "rule/set_sl/rejected": 1}
    assert data["per_pair"][PAIR]["rule_executions"]["applied"] == 1
    assert data["per_pair"][PAIR]["escalations"]["verdicts"] == {"downgrade": 1}
    ad = data["per_pair"][PAIR]["adaptive"]
    assert [(e["key"], e["in_force"]) for e in ad["entries"]] == [("min_confidence_floor", True), ("trigger.weak_min", False)]
    assert ad["playbook"]["chars"] > 0 and len(ad["changes"]) == 1                   # the torn line is skipped
    assert data["per_pair"][PAIR]["tuning_changes"][0]["key"] == "min_confidence_floor"
    assert data["log_errors"] == {f"{PAIR}/engine.jsonl": 1}
    assert data["hashes"]["config"] == s.config_hash and data["hashes"]["git"] == "abc1234"
    assert data["hashes"]["per_pair"][PAIR]["prompt"] == "p" * 16
    assert data["operator"]["proposals"][0]["id"] == "2026-09-26-xau-sessions"
    assert len(data["ideas"]) == rp.IDEAS and data["ideas_in_window"] == 30
    assert data["ideas"][0]["ts"] >= data["ideas"][-1]["ts"]                       # latest first
    for text in ("## Versions and hashes", "## AI usage", "## Health", f"## {PAIR}", "gate failures by check: "
                 "rr_after_costs 10", "min_confidence_floor=60 (until", "trigger.weak_min=3 (EXPIRED", "London open sweeps",
                 "## Operator context", "## Last 25 trade ideas", "git abc1234", "watch the Asia range"):
        assert text in md, text
    assert md.count(f"| {PAIR} |") == rp.IDEAS
    assert _digest(watched) == before                                                # read-only


def test_the_pack_is_cut_to_its_size_limit_and_says_so(rp, tmp_path):
    s = settings_at(tmp_path)
    now = now_ms()
    seed(s, now, n_ideas=60)
    full = rp.build_pack(s, hours=24, kind="weekly", systems=[s], notes=[], now=now, write=False)
    assert len(full.md) <= rp.MAX_MD_CHARS and full.md.count(f"| {PAIR} |") == rp.IDEAS
    limit = len(full.md) - 1
    small = rp.build_pack(s, hours=24, kind="weekly", systems=[s], notes=[], now=now, write=False, max_chars=limit)
    assert len(small.md) <= limit and small.md_path is None
    assert small.md.count(f"| {PAIR} |") < rp.IDEAS and "size limit" not in small.md    # fewer ideas first
    tiny = rp.build_pack(s, hours=24, kind="weekly", systems=[s], notes=[], now=now, write=False, max_chars=2000)
    assert len(tiny.md) <= 2000 and "size limit" in tiny.md


def test_kill_switches_and_freeze_are_reported(rp, tmp_path):
    s = settings_at(tmp_path)
    now = now_ms()
    seed(s, now, n_ideas=3)
    from tradingsystem.core.killswitch import set_kill_switch
    set_kill_switch(s, PAIR, reason="order burst", actor="monitor")
    (s.paths.data() / "TUNING_FREEZE").write_text("x", encoding="utf-8")
    pack = rp.build_pack(s, hours=24, kind="diagnose", systems=[s], notes=[], now=now, write=False)
    assert pack.data["per_pair"][PAIR]["kill_switch"]["on"] is True
    assert pack.data["per_pair"][PAIR]["kill_switch"]["reason"]["reason"] == "order burst"
    assert pack.data["kill_switch_global"]["on"] is False and pack.data["tuning_freeze"] is True
    assert "pair kill switch: ON" in pack.md and "TUNING_FREEZE: present" in pack.md


def test_a_system_without_a_database_is_a_problem_line_not_a_failure(rp, tmp_path):
    s = settings_at(tmp_path)
    pack = rp.build_pack(s, hours=24, kind="daily", systems=[s], notes=["!! ETHUSDT: no supervisor runs"],
                         now=now_ms(), write=False)
    assert pack.data["per_pair"][PAIR]["error"].startswith("no app.db")
    assert any("does not exist" in p for p in pack.problems) and "!! ETHUSDT: no supervisor runs" in pack.problems
    assert pack.data["usage"]["error"] == "no ledger yet" and not (s.paths.shared() / "ai_usage.db").exists()


def test_ledger_path_follows_the_layout(rp, tmp_path):
    inst = settings_at(tmp_path)
    legacy = settings_at(tmp_path, instance=None)
    assert rp.ledger_path(legacy, [inst]) == legacy.paths.shared() / "ai_usage.db"      # per-pair systems share one
    assert rp.ledger_path(legacy, [legacy]) == legacy.paths.data() / "app.db"          # the all-pairs system: its own


def test_cli_print_writes_nothing_and_bad_args_exit_3(rp, tmp_path, monkeypatch, capsys):
    s = settings_at(tmp_path)
    now = now_ms()
    seed(s, now, n_ideas=2)
    monkeypatch.setattr(rp, "load_settings", lambda: s)
    monkeypatch.setattr(rp, "choose_systems", lambda pair: ([s], []))
    assert rp.main(["--hours", "24", "--print"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("# Review pack — adhoc — last 24 h")
    assert not (s.paths.data() / "reviews").exists()
    assert rp.main(["--hours", "24", "--kind", "daily"]) == 0
    files = sorted(p.name for p in (s.paths.data() / "reviews").iterdir())
    assert len(files) == 2 and files[0].endswith("_daily.json") and files[1].endswith("_daily.md")
    assert rp.main(["--hours", "0"]) == 3
    assert rp.main(["--kind", "monthly"]) == 3


def test_the_tool_imports_its_own_checkout(rp):
    assert Path(sys.path[0]).resolve() == (ROOT / "src").resolve() or str(ROOT / "src") in sys.path
    assert isinstance(rp.health_module(), types.ModuleType)
    assert hasattr(rp.health_module(), "system_report")


def test_the_usage_gauge_is_read_when_present_and_never_fatal(rp, tmp_path, monkeypatch):
    s = settings_at(tmp_path)
    seed(s, now_ms(), n_ideas=2)

    class State:
        level, enforce, reason, week_pct = 1, False, "week 72 %", 72.0

        def as_detail(self):
            return {"level": self.level, "week_pct": self.week_pct}

    class Gauge:
        def __init__(self, s, usage, clock=None):
            assert usage.__class__.__name__ == "UsageStore"

        def state(self, now_ms=None):
            return State()

    monkeypatch.setitem(sys.modules, "tradingsystem.ai.usage_gauge", types.SimpleNamespace(UsageGauge=Gauge))
    g = rp.gauge_state(s, s.paths.shared() / "ai_usage.db", now_ms())
    assert g == {"level": 1, "week_pct": 72.0, "enforce": False, "reason": "week 72 %"}

    class Broken(Gauge):
        def state(self, now_ms=None):
            raise RuntimeError("boom")

    monkeypatch.setitem(sys.modules, "tradingsystem.ai.usage_gauge", types.SimpleNamespace(UsageGauge=Broken))
    assert rp.gauge_state(s, s.paths.shared() / "ai_usage.db", now_ms())["error"].startswith("usage gauge failed")
    assert rp.gauge_state(s, tmp_path / "nope.db", now_ms()) == {"error": "no ledger yet"}


def test_screens_follow_the_log_rotation_back_to_the_window_start(rp, tmp_path):
    now = now_ms()
    logs = tmp_path / "logs"
    logs.mkdir()

    def line(minutes_ago: int, fire: str, strength: str, pair: str = PAIR) -> str:
        ts = iso(now - minutes_ago * MS_PER_MINUTE).replace("Z", "+00:00")
        return json.dumps({"ts": ts, "level": "INFO", "msg": f"{pair} 5m close x: trigger={fire} ({strength}) r"})

    (logs / "engine.jsonl").write_text("\n".join([line(30, "True", "strong"), line(25, "False", "none"),
                                                  line(20, "True", "strong", "ETHUSDT")]) + "\n", encoding="utf-8")
    (logs / "engine.jsonl.1").write_text("\n".join([line(200, "True", "weak"), line(90, "True", "review")]) + "\n",
                                         encoding="utf-8")
    (logs / "engine.jsonl.2").write_text(line(100, "True", "idle") + "\n", encoding="utf-8")     # never reached
    sc = rp.screens(logs, PAIR, now - 120 * MS_PER_MINUTE)
    assert sc["closes_by_tf"] == {"5m": 3} and sc["triggers_by_strength"] == {"strong": 1, "review": 1}
