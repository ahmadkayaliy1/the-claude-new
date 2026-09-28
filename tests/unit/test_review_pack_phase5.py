"""Review pack, Phase 5 (A7/A8): one supervisor scan per build, the per-pair data bounded by ``until`` (the demo
report's window), and the weekly pack's go-live evidence for the weekly review's paragraph."""
from __future__ import annotations

import importlib.util
import json
import re
import sqlite3
import string
from pathlib import Path

import pytest

from tradingsystem.ai.budget import UsageStore
from tradingsystem.ai.providers.base import LLMResult
from tradingsystem.ai.store import DecisionRecord, DecisionStore
from tradingsystem.core.settings import INSTANCE_ENV, load_settings
from tradingsystem.core.timeutil import MS_PER_HOUR, MS_PER_MINUTE, iso, now_ms, parse_date_spec
from tradingsystem.execution import management
from tradingsystem.ingest.common.appdb import AppDB
from tradingsystem.supervisor import procs

ROOT = Path(__file__).resolve().parents[2]
PAIR = "BTCUSDT"


def tool(name: str):
    spec = importlib.util.spec_from_file_location(f"{name}_p5_under_test", ROOT / "tools" / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture()
def rp(monkeypatch):
    m = tool("review_pack")
    monkeypatch.setattr(m, "git_info", lambda root: {"sha": "abc1234", "branch": "main", "last_commits": [],
                                                     "dirty": False, "dirty_files": 0, "dirty_list": []})
    monkeypatch.setattr(procs, "running_supervisors", lambda older_s=None: {})     # never the production pids
    return m


def settings_at(tmp_path: Path, instance: str | None = PAIR):
    s = load_settings(extra_env={INSTANCE_ENV: instance or ""})
    return s.model_copy(update={"paths": s.paths.model_copy(update={"data_dir": str(tmp_path / "data"),
                                                                     "logs_dir": str(tmp_path / "logs")})})


def test_a_pack_build_scans_the_supervisors_once_and_restores_the_scanner(rp, monkeypatch):
    """A7: the health report asks ``systems`` and ``machine_report`` — one scan answers both; an ``older_s`` ask
    (a start race) always scans; the original function is back after the build, also after an exception."""
    asked: list = []

    def scan(older_s=None):
        asked.append(older_s)
        return {4242: PAIR}
    monkeypatch.setattr(procs, "running_supervisors", scan)
    hr = rp.health_module()
    with rp.one_supervisor_scan():
        with rp.one_supervisor_scan():                                  # nested: the outer cache stays
            assert procs.running_supervisors() == {4242: PAIR}
        assert hr.procs is procs                                         # the health report asks the same module
        assert "pid 4242 (BTCUSDT)" in hr.procs.describe(hr.procs.running_supervisors())
        got = procs.running_supervisors()
        got[1] = "mutated"                                               # a caller's copy, not the cache
        assert procs.running_supervisors() == {4242: PAIR}
        procs.running_supervisors(older_s=5.0)
    assert asked == [None, 5.0] and procs.running_supervisors is scan
    with pytest.raises(RuntimeError):
        with rp.one_supervisor_scan():
            raise RuntimeError("boom")
    assert procs.running_supervisors is scan


def test_build_pack_asks_the_machine_once(rp, tmp_path, monkeypatch):
    asked: list = []
    monkeypatch.setattr(procs, "running_supervisors", lambda older_s=None: asked.append(older_s) or {})
    hr = rp.health_module()
    s = settings_at(tmp_path)
    real_machine = hr.machine_report

    def machine_twice(s0, now):                                          # the health report asking twice
        hr.procs.running_supervisors()
        return real_machine(s0, now)
    monkeypatch.setattr(hr, "machine_report", machine_twice)
    rp.build_pack(s, hours=6, kind="adhoc", systems=[s], notes=[], write=False)
    assert asked == [None]


def seed(s, now: int) -> Path:
    db = s.paths.state() / "app.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    AppDB(db).close()
    store = DecisionStore(db, config_hash="c" * 16)
    ids = []
    for h in (5, 3, 1):                                                  # 5 h, 3 h, 1 h ago
        ids.append(store.save(DecisionRecord(
            pair=PAIR, mode="agent_per_pair", trigger="setup", status="valid", ts=now - h * MS_PER_HOUR,
            prompt_hash=f"p{h}" * 8, recommendation={"decision": "BUY", "order_type": "LIMIT", "confidence": 60,
                                                    "stop_loss": 1.0, "entry": {"price": 2.0},
                                                    "take_profits": [{"price": 3.0, "close_fraction": 1.0}],
                                                    "operator_notes": f"note {h}"})))
    store.close()
    con = sqlite3.connect(db)
    for stmt in management._DDL:
        con.execute(stmt)
    for k, did in enumerate(ids):
        ts = now - (5 - 2 * k) * MS_PER_HOUR + MS_PER_MINUTE
        con.execute("INSERT INTO position_actions (ts, pair, source, source_decision, seq, target_decision, leg, "
                    "action, requested, status, detail) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (ts, PAIR, "rule", did, 0, did, "1", "set_sl", None, "applied", None))
        con.execute("INSERT INTO tuning_changes (ts, pair, key, old_value, new_value, reason, actor, reverted_ms) "
                    "VALUES (?,?,?,?,?,?,?,?)", (ts, PAIR, f"key{k}", "1", "2", "r", "operator",
                                                 now - 30 * MS_PER_MINUTE if k == 0 else None))
    con.commit()
    con.close()
    s.paths.shared().mkdir(parents=True, exist_ok=True)
    u = UsageStore(s.paths.shared() / "ai_usage.db")
    for _ in range(3):
        u.record(LLMResult("claude_code", "sonnet", "", None, input_tokens=100, output_tokens=10), provider="claude_code",
                 model="sonnet", purpose="agent_per_pair", pair=PAIR, ok=True, role="decision")
    u.close()
    con = sqlite3.connect(s.paths.shared() / "ai_usage.db")
    for i, h in enumerate((5, 3, 1)):
        con.execute("UPDATE ai_usage SET ts=? WHERE id=?", (now - h * MS_PER_HOUR, i + 1))
    con.commit()
    con.close()
    logs = s.paths.logs()
    logs.mkdir(parents=True)
    lines = [json.dumps({"ts": iso(now - h * MS_PER_HOUR).replace("Z", "+00:00"), "level": "INFO", "logger": "engine",
                         "msg": f"{PAIR} 5m close {iso(now)}: trigger={'True' if h == 1 else 'False'} (strong) x"})
             for h in (5, 3, 1)]
    (logs / "engine.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return db


def test_until_bounds_every_per_pair_section_and_the_ledger(rp, tmp_path):
    s = settings_at(tmp_path)
    now = now_ms()
    seed(s, now)
    since, until = now - 6 * MS_PER_HOUR, now - 2 * MS_PER_HOUR                # the 5 h and 3 h rows only
    rep = rp.pair_report(s, PAIR, since, now, {}, until=until, screen_slot_ms=15 * MS_PER_MINUTE)
    assert rep["funnel"]["calls_stored"] == 2 and rep["funnel"]["ideas"] == 2
    assert rep["position_actions"]["n"] == 2
    assert rep["last_valid"]["prompt_hash"] == "p3" * 8 and rep["last_valid"]["operator_notes"] == "note 3"
    assert [t["key"] for t in rep["tuning_changes"]] == ["key1", "key0"]          # key2 was made after 'until'
    assert rep["tuning_changes"][1]["reverted"] is None                            # reverted after 'until'
    sc = rep["screens"]
    assert sc["screens"] == 2 and sc["triggers_by_strength"] == {}                 # the fired one is after 'until'
    assert len(sc["slots"]) == 2 and all(t % (15 * MS_PER_MINUTE) == 0 for t in sc["slots"])
    assert sc["oldest_read"] == iso(now - 5 * MS_PER_HOUR).replace("Z", "+00:00")
    u = rp.usage_section(s.paths.shared() / "ai_usage.db", since, until)
    assert u["total"]["calls"] == 2
    # without 'until' (the pack): everything up to now, and no slot list
    full = rp.pair_report(s, PAIR, since, now, {})
    assert full["funnel"]["calls_stored"] == 3 and "slots" not in full["screens"]
    assert full["tuning_changes"][2]["reverted"] is not None
    assert rp.usage_section(s.paths.shared() / "ai_usage.db", since)["total"]["calls"] == 3


def test_the_weekly_pack_carries_the_go_live_evidence_and_the_daily_does_not(rp, tmp_path):
    base = settings_at(tmp_path, instance=None)
    s = settings_at(tmp_path)
    now = now_ms()
    assert now > parse_date_spec(base.evaluation.demo_start_utc)                 # the config's window has started
    seed(s, now)
    weekly = rp.build_pack(base, hours=168, kind="weekly", systems=[s], notes=[], now=now, write=False)
    gl = weekly.data["go_live"]
    assert "error" not in gl, gl
    assert len(gl["items"]) == 19 and set(gl["summary"]) == {"pass", "FAIL", "n.a."}
    assert "## Go-live evidence so far (tools/go_live_inputs.py; thresholds: docs/go_live_checklist.md)" in weekly.md
    assert re.search(r"^- \[(pass|FAIL|n\.a\.)\] the 5 demo days on Phase 4\+ code complete", weekly.md, re.M)
    daily = rp.build_pack(base, hours=24, kind="daily", systems=[s], notes=[], now=now, write=False)
    assert "go_live" not in daily.data and "Go-live evidence" not in daily.md
    unset = base.model_copy(update={"evaluation": base.evaluation.model_copy(update={"demo_start_utc": None})})
    weekly = rp.build_pack(unset, hours=168, kind="weekly", systems=[s], notes=[], now=now, write=False)
    assert "no window" in weekly.data["go_live"]["error"]
    assert "go-live evidence unavailable: Invalid: no window" in weekly.md


def test_the_weekly_prompt_asks_for_the_go_live_paragraph_with_placeholders_the_runner_fills():
    text = (ROOT / "tools" / "operator" / "prompts" / "weekly.md").read_text(encoding="utf-8")
    assert 'one paragraph that starts with\n   "Go-live evidence so far:"' in text
    assert "Go-live evidence so far" in text and "docs/go_live_checklist.md" in text
    # every $placeholder is one tools/operator/run_session.py compose_prompt fills
    filled = {"kind", "review_id", "hours", "pairs", "pack_md", "pack_json", "summary_max_chars", "max_turns",
              "now_utc", "data_root", "reason"}
    used = {m.group("named") or m.group("braced") for m in string.Template.pattern.finditer(text)
            if m.group("named") or m.group("braced")}
    assert used <= filled, used - filled
    runner = (ROOT / "tools" / "operator" / "run_session.py").read_text(encoding="utf-8")
    body = runner.split("def compose_prompt", 1)[1].split("head = string.Template", 1)[0]
    for k in filled:
        assert f'"{k}"' in body, k
