"""Phase 4 review fixes of the review pack and the operator-session runner: ``--out`` confined to data/reviews, the
adaptive section read through the services' own validator (never an invalid, expired or unreferenced value shown
as in force; alias bombs and deep nesting guarded), sessions without a result document recorded as usage unknown,
and a config error that leaves a trace (log line, toast, exit 3). The real ``claude`` CLI is never started."""
from __future__ import annotations

import base64
import importlib.util
import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tradingsystem.ai.budget import UsageStore
from tradingsystem.ai.providers.base import LLMResult
from tradingsystem.core.adaptive import text_hash
from tradingsystem.core.settings import INSTANCE_ENV, load_settings
from tradingsystem.core.timeutil import MS_PER_DAY, MS_PER_HOUR, now_ms
from tradingsystem.supervisor import procs

ROOT = Path(__file__).resolve().parents[2]
OPS = ROOT / "tools" / "operator"
PAIR = "BTCUSDT"
FAKE_EXE = "C:/fake/claude.exe"
PLAYBOOK = "- London open sweeps of the Asia range reverse more often than they run\n"


def load(name: str, path: Path, *, register: bool = False):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    if register:
        sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


def relocate(s, tmp_path: Path, **adaptive):
    s = s.model_copy(update={"paths": s.paths.model_copy(update={"data_dir": str(tmp_path / "data"),
                                                                    "logs_dir": str(tmp_path / "logs")})})
    if adaptive:
        s = s.model_copy(update={"adaptive": s.adaptive.model_copy(update=adaptive)})
    return s


def settings_at(tmp_path: Path, **adaptive):
    return relocate(load_settings(extra_env={INSTANCE_ENV: PAIR}), tmp_path, **adaptive)


@pytest.fixture()
def rp(monkeypatch):
    m = load("test_fixes_review_pack", ROOT / "tools" / "review_pack.py")
    monkeypatch.setattr(m, "git_info", lambda root: {"sha": "abc1234", "branch": "main", "last_commits": [],
                                                     "dirty": False, "dirty_files": 0, "dirty_list": []})
    monkeypatch.setattr(procs, "running_supervisors", lambda older_s=None: {})     # never the production pids
    return m


def entry(value, now: int, *, set_h: float = -30, exp_h: float = 240, reason: str = "test") -> str:
    return (f"{{value: {value}, set_ms: {now + int(set_h * MS_PER_HOUR)}, expires_ms: {now + int(exp_h * MS_PER_HOUR)},"
            f" reason: {reason}, window_hours: 168}}")


def overlay(s, text: str, playbook: str | None = PLAYBOOK) -> Path:
    d = s.paths.data() / "adaptive" / PAIR
    d.mkdir(parents=True, exist_ok=True)
    (d / "adaptive.yaml").write_text(text, encoding="utf-8")
    if playbook is not None:
        (d / "playbook.md").write_text(playbook, encoding="utf-8")
    return d


def pack_md(rp, s, now: int) -> str:
    return rp.build_pack(s, hours=24, kind="adhoc", systems=[s], notes=[], now=now, write=False).md


# ------------------------------------------------------------------ --out (session-safety-5, engine-orch-gauge-pack-3)
@pytest.fixture()
def cli(rp, tmp_path, monkeypatch):
    s = settings_at(tmp_path)
    monkeypatch.setattr(rp, "load_settings", lambda: s)
    monkeypatch.setattr(rp, "choose_systems", lambda pair: ([s], []))
    return s


def test_out_outside_the_reviews_dir_is_refused_and_nothing_is_written(rp, cli, tmp_path, capsys):
    outside = tmp_path / "elsewhere"
    assert rp.main(["--out", str(outside)]) == rp.EXIT_INVALID
    assert rp.main(["--out", str(cli.paths.data() / "reviews" / ".." / ".." / "escape")]) == rp.EXIT_INVALID
    assert rp.main(["--out", str(cli.paths.data())]) == rp.EXIT_INVALID            # the data root itself: no
    assert not outside.exists() and not (tmp_path / "escape").exists()
    assert not (cli.paths.data() / "reviews").exists()
    assert "must be" in capsys.readouterr().out


def test_out_unc_and_double_slash_paths_are_refused_before_any_access(rp, cli, monkeypatch):
    def untouched(*a, **k):
        raise AssertionError("the path was examined")

    monkeypatch.setattr(rp, "reviews_dir", untouched)
    monkeypatch.setattr(rp, "build_pack", untouched)
    for raw in ("\\\\evil-host\\share\\x", "//evil-host/share/x", "\\/evil-host/share", "/\\evil-host\\share",
                "\\\\?\\C:\\temp", "\\\\.\\pipe\\x", "  //evil-host/share"):
        assert rp.main(["--out", raw, "--print"]) == rp.EXIT_INVALID, raw


def test_out_under_the_reviews_dir_is_accepted(rp, cli):
    sub = cli.paths.data() / "reviews" / "manual"
    assert rp.main(["--out", str(sub), "--hours", "24"]) == rp.EXIT_OK
    assert sorted(p.suffix for p in sub.iterdir()) == [".json", ".md"]
    assert rp.main(["--out", str(cli.paths.data() / "reviews")]) == rp.EXIT_OK


def test_out_through_a_junction_that_points_outside_is_refused(rp, cli, tmp_path):
    winapi = pytest.importorskip("_winapi")
    target = tmp_path / "outside"
    target.mkdir()
    reviews = cli.paths.data() / "reviews"
    reviews.mkdir(parents=True)
    try:
        winapi.CreateJunction(str(target), str(reviews / "link"))
    except OSError as exc:
        pytest.skip(f"no junction: {exc}")
    assert rp.main(["--out", str(reviews / "link")]) == rp.EXIT_INVALID
    assert list(target.iterdir()) == []


# ------------------------------------------------------------------ adaptive section (engine-orch-gauge-pack-1)
def test_an_invalid_overlay_is_reported_and_nothing_is_shown_as_in_force(rp, tmp_path):
    s = settings_at(tmp_path)
    now = now_ms()
    overlay(s, f"version: 1\nmin_confidence_floor: {entry(90, now)}\nplaybook: {entry(text_hash(PLAYBOOK), now)}\n")
    info = rp.adaptive_info(s, PAIR, now)
    assert info["error"].startswith(rp.ADAPTIVE_INVALID) and "min_confidence_floor" in info["error"]
    assert [(e["key"], e["value"], e["in_force"], e["state"]) for e in info["entries"]] == [
        ("min_confidence_floor", 90, False, "not_applied"), ("playbook", text_hash(PLAYBOOK), False, "not_applied")]
    assert info["effective"] is None and info["playbook"]["in_force"] is False
    assert info["playbook"]["state"] == rp.PLAYBOOK_NOT_IN_FORCE
    md = pack_md(rp, s, now)
    assert "!! adaptive: adaptive.yaml invalid (services keep the last good values; config defaults after a restart)" \
        in md
    assert "min_confidence_floor=90 (NOT APPLIED: the file is invalid" in md and "(until" not in md
    assert f"playbook.md {rp.PLAYBOOK_NOT_IN_FORCE}" in md and "London open sweeps" not in md


def test_a_lowered_max_expiry_days_makes_the_overlay_invalid_in_the_pack_too(rp, tmp_path):
    s = settings_at(tmp_path, max_expiry_days=3)            # the entry spans 11 days: the services refuse it now
    now = now_ms()
    overlay(s, f"version: 1\nmin_confidence_floor: {entry(60, now)}\n", playbook=None)
    info = rp.adaptive_info(s, PAIR, now)
    assert info["error"].startswith(rp.ADAPTIVE_INVALID) and "3 days" in info["error"]
    assert [e["in_force"] for e in info["entries"]] == [False]


def test_an_expired_playbook_entry_leaves_the_file_on_disk_not_in_force(rp, tmp_path):
    s = settings_at(tmp_path)
    now = now_ms()
    overlay(s, f"version: 1\nmin_confidence_floor: {entry(60, now)}\n"
               f"playbook: {entry(text_hash(PLAYBOOK), now, set_h=-100, exp_h=-2)}\n")
    info = rp.adaptive_info(s, PAIR, now)
    assert "error" not in info
    assert [(e["key"], e["in_force"], e["state"]) for e in info["entries"]] == [
        ("min_confidence_floor", True, "in_force"), ("playbook", False, "expired")]
    assert info["playbook"]["in_force"] is False and info["effective"]["playbook_chars"] == 0
    md = pack_md(rp, s, now)
    assert "playbook=" in md and "EXPIRED" in md and f"playbook.md {rp.PLAYBOOK_NOT_IN_FORCE}" in md
    assert "playbook in force" not in md and "London open sweeps" not in md


def test_an_unreferenced_playbook_is_not_in_force(rp, tmp_path):
    s = settings_at(tmp_path)
    now = now_ms()
    overlay(s, f"version: 1\nmin_confidence_floor: {entry(60, now)}\n")
    info = rp.adaptive_info(s, PAIR, now)
    assert "error" not in info and info["playbook"]["in_force"] is False
    assert info["playbook"]["hash"] == text_hash(PLAYBOOK) and "London open sweeps" in info["playbook"]["text"]
    assert "London open sweeps" not in pack_md(rp, s, now)


def test_a_playbook_that_does_not_match_its_hash_is_not_in_force(rp, tmp_path):
    s = settings_at(tmp_path)
    now = now_ms()
    overlay(s, f"version: 1\nplaybook: {entry(text_hash(PLAYBOOK), now)}\n",
            playbook=PLAYBOOK + "- a line added by hand\n")
    info = rp.adaptive_info(s, PAIR, now)
    assert info["error"].startswith(rp.ADAPTIVE_INVALID) and "does not match" in info["error"]
    assert info["playbook"]["in_force"] is False and [e["in_force"] for e in info["entries"]] == [False]


def test_adaptive_disabled_puts_nothing_in_force(rp, tmp_path):
    s = settings_at(tmp_path, enabled=False)
    now = now_ms()
    overlay(s, f"version: 1\nmin_confidence_floor: {entry(70, now)}\nplaybook: {entry(text_hash(PLAYBOOK), now)}\n"
               f"trigger:\n  weak_min: {entry(3, now, set_h=-100, exp_h=-1)}\n")
    info = rp.adaptive_info(s, PAIR, now)
    assert "error" not in info and not any(e["in_force"] for e in info["entries"])
    assert [e["state"] for e in info["entries"]] == ["disabled", "disabled", "expired"]
    assert info["effective"]["min_confidence"] == s.risk.min_confidence and info["playbook"]["in_force"] is False
    md = pack_md(rp, s, now)
    assert "min_confidence_floor=70 (NOT APPLIED: adaptive.enabled is false" in md and "London open sweeps" not in md


def test_valid_entries_show_the_effective_values_of_the_services(rp, tmp_path):
    s = settings_at(tmp_path)
    now = now_ms()
    overlay(s, f"version: 1\nmin_confidence_floor: {entry(70, now)}\nplaybook: {entry(text_hash(PLAYBOOK), now)}\n")
    info = rp.adaptive_info(s, PAIR, now)
    assert info["effective"]["min_confidence"] == max(70, s.risk.min_confidence)
    assert info["playbook"] == {"in_force": True, "hash": text_hash(PLAYBOOK), "chars": len(PLAYBOOK.strip()),
                                "text": PLAYBOOK.strip()}
    md = pack_md(rp, s, now)
    assert f"min_confidence {max(70, s.risk.min_confidence)}" in md and "playbook in force (" in md


def _merge_bomb(levels: int) -> str:
    lines = ["l0: &l0 {" + ", ".join(f"k{i}: {i}" for i in range(10)) + "}"]
    for n in range(1, levels + 1):
        lines.append(f"l{n}: &l{n} {{<<: [" + ", ".join([f"*l{n - 1}"] * 10) + "]}")
    return "\n".join(lines) + "\n"


def test_a_merge_key_bomb_neither_stalls_nor_crashes_the_pack(rp, tmp_path):
    s = settings_at(tmp_path)
    now = now_ms()
    overlay(s, _merge_bomb(7), playbook=None)               # ~ minutes and GBs if it were ever constructed
    t0 = time.monotonic()
    info = rp.adaptive_info(s, PAIR, now)
    md = pack_md(rp, s, now)
    assert time.monotonic() - t0 < 10
    assert info["error"].startswith(rp.ADAPTIVE_INVALID) and "alias" in info["error"] and info["entries"] == []
    assert "nothing listed (see the line above)" in md


def test_deep_nesting_and_an_oversized_file_are_reported_not_raised(rp, tmp_path):
    s = settings_at(tmp_path)
    now = now_ms()
    d = overlay(s, "a: " + "[" * 3000 + "]" * 3000 + "\n", playbook=None)
    info = rp.adaptive_info(s, PAIR, now)
    assert info["error"].startswith(rp.ADAPTIVE_INVALID) and info["entries"] == []
    (d / "adaptive.yaml").write_bytes(b"# " + b"x" * (5 * 256 * 1024) + b"\n")
    info = rp.adaptive_info(s, PAIR, now)
    assert info["error"].startswith(rp.ADAPTIVE_INVALID) and "larger than" in info["error"]
    assert info["file_hash"] and info["entries"] == []


# ------------------------------------------------------------------ unknown usage (session-safety-4)
def test_the_usage_section_counts_sessions_with_unknown_usage(rp, tmp_path):
    s = settings_at(tmp_path)
    ledger = s.paths.shared() / "ai_usage.db"
    u = UsageStore(ledger)
    try:
        for error in (f"{rp.USAGE_UNKNOWN_PREFIX}no result document after 2280 s (timeout): no answer",
                      f"{rp.USAGE_UNKNOWN_PREFIX}no result document after 3 s (error): crash", "usage_limit",
                      f"{rp.USAGE_UNKNOWN_PREFIX}old"):
            u.record(LLMResult("claude_code", "opus", "", None), provider="claude_code", model="opus",
                     purpose="operator_daily", pair=None, ok=False, error=error, role="review")
    finally:
        u.close()
    con = sqlite3.connect(ledger)
    con.execute("UPDATE ai_usage SET ts=? WHERE error=?", (now_ms() - 3 * MS_PER_DAY, f"{rp.USAGE_UNKNOWN_PREFIX}old"))
    con.commit()
    con.close()
    now = now_ms()
    assert rp.usage_section(ledger, now - 24 * MS_PER_HOUR)["usage_unknown_sessions"] == 2
    md = pack_md(rp, s, now)
    assert "!! 2 operator session(s) with unknown usage (timed out or crashed)" in md


# ------------------------------------------------------------------ the runner
@pytest.fixture()
def rs(monkeypatch, tmp_path):
    m = load("test_fixes_run_session", OPS / "run_session.py", register=True)
    monkeypatch.setattr(m.sa.cc, "find_cli", lambda configured: FAKE_EXE)
    monkeypatch.setattr(procs, "running_supervisors", lambda older_s=None: {})
    rp_ = m.pack_module()
    hr = rp_.health_module()                 # the systems the pack picks live under tmp_path, never the checkout's
    monkeypatch.setattr(hr, "load_settings", lambda **kw: relocate(load_settings(**kw), tmp_path))
    monkeypatch.setattr(rp_, "git_info", lambda root: {"sha": "abc1234", "branch": "main", "last_commits": [],
                                                       "dirty": False, "dirty_files": 0})
    monkeypatch.setattr(m, "setup_log", lambda s: None)
    monkeypatch.setattr(m, "CONFIG_ERROR_LOG", tmp_path / "logs" / "operator-session-config-error.log")
    yield m
    sys.modules.pop("test_fixes_run_session", None)


class FakeCli:
    def __init__(self, doc: dict | None, *, timed_out: bool = False) -> None:
        self.doc, self.timed_out, self.calls = doc, timed_out, []

    def __call__(self, args, *, cwd, env, stdin_path, stdout_path, stderr_path, timeout_s):
        self.calls.append(args)
        Path(stdout_path).write_text(json.dumps(self.doc) if self.doc else "", encoding="utf-8")
        Path(stderr_path).write_text("" if self.timed_out else "Error: the CLI crashed", encoding="utf-8")
        return (None if self.timed_out else 3), self.timed_out, 2280.0


def checkout(tmp_path: Path) -> Path:
    root = tmp_path / "checkout"
    (root / ".venv" / "Scripts").mkdir(parents=True)
    (root / ".venv" / "Scripts" / "python.exe").write_bytes(b"")
    return root


@pytest.mark.parametrize("timed_out", [True, False], ids=["timeout", "crash"])
def test_a_session_without_a_result_document_is_recorded_as_usage_unknown(rs, tmp_path, monkeypatch, timed_out):
    sent: list[tuple] = []
    monkeypatch.setattr(rs, "send", lambda s, level, title, text, key=None: sent.append((level, title, text)))
    monkeypatch.setattr(rs, "wait_for_start", lambda workdir, max_wait_s=0: True)
    monkeypatch.setattr(rs, "check_auth", lambda exe, env, workdir: None)
    monkeypatch.setattr(rs, "git_snapshot", lambda root: {})
    monkeypatch.setattr(rs, "spawn_cli", FakeCli(None, timed_out=timed_out))
    s = relocate(load_settings(), tmp_path)
    assert rs.run("weekly", s=s, root=checkout(tmp_path), out=lambda *a: None) == rs.EXIT_FAILED
    rec = json.loads(next((tmp_path / "data" / "reviews").glob("*.session.json")).read_text(encoding="utf-8"))
    assert rec["status"] == ("timeout" if timed_out else "error") and rec["result"]["usage_unknown"] is True
    assert rec["ledger"]["recorded"] is True and rec["ledger"]["usage_unknown"] is True
    con = sqlite3.connect(rec["ledger"]["path"])
    rows = con.execute("SELECT input_tokens, output_tokens, ok, role, error FROM ai_usage").fetchall()
    con.close()
    assert len(rows) == 1 and rows[0][:4] == (0, 0, 0, "review")
    rp_ = rs.pack_module()
    assert rows[0][4].startswith(rp_.USAGE_UNKNOWN_PREFIX + "no result document after 2280 s (")
    assert "usage unknown" in sent[-1][2] and "0 tokens" not in sent[-1][2]
    assert rp_.usage_section(Path(rec["ledger"]["path"]), 0)["usage_unknown_sessions"] == 1


def test_a_result_document_is_never_marked_usage_unknown(rs):
    doc = {"type": "result", "subtype": "success", "is_error": False, "num_turns": 3, "result": "ok",
           "usage": {"input_tokens": 10, "output_tokens": 5}}
    assert "usage_unknown" not in rs.parse_result(json.dumps(doc), "", 0)
    assert rs.parse_result("", "Error: boom", 3)["usage_unknown"] is True


# ------------------------------------------------------------------ config error (config-ops-rollback-2)
def _broken_settings(**kw):
    raise ValueError("1 validation error for Settings\nmonitor.equity_drop_warn_pct\n  must be below the kill level")


def test_a_config_error_is_logged_toasted_and_exits_3(rs, tmp_path, monkeypatch, capsys):
    from tradingsystem.core import notify as nt
    calls: list[tuple] = []
    monkeypatch.setattr(rs, "load_settings", _broken_settings)
    monkeypatch.setattr(rs, "run", lambda *a, **k: pytest.fail("a session ran without a config"))
    monkeypatch.setattr(nt, "disabled", lambda: False)
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: calls.append((cmd, kw)))
    assert rs.main(["--kind", "daily"]) == rs.EXIT_CONFIG == 3
    line = rs.CONFIG_ERROR_LOG.read_text(encoding="utf-8").splitlines()
    assert len(line) == 1 and line[0].endswith("Z operator session (daily) cannot read its config: ValueError: 1 "
                                               "validation error for Settings monitor.equity_drop_warn_pct must be "
                                               "below the kill level")
    assert len(calls) == 1 and calls[0][1]["timeout"] == 15.0
    cmd = calls[0][0]
    assert cmd[cmd.index("-Level") + 1] == "critical"
    assert base64.b64decode(cmd[cmd.index("-TitleB64") + 1]).decode("utf-8") == \
        "Operator session cannot read its config"
    assert "equity_drop_warn_pct" in base64.b64decode(cmd[cmd.index("-TextB64") + 1]).decode("utf-8")
    assert "cannot read its config" in capsys.readouterr().err


def test_a_config_error_never_raises_and_respects_the_notify_switch(rs, tmp_path, monkeypatch):
    from tradingsystem.core import notify as nt
    monkeypatch.setattr(rs, "load_settings", _broken_settings)
    (tmp_path / "blocker").write_text("a file where the log directory should be", encoding="utf-8")
    monkeypatch.setattr(rs, "CONFIG_ERROR_LOG", tmp_path / "blocker" / "x.log")

    def timeout(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))

    monkeypatch.setattr(subprocess, "run", timeout)
    monkeypatch.setattr(nt, "disabled", lambda: False)
    assert rs.main(["--kind", "weekly"]) == 3                       # unwritable log, toast timed out: still exit 3
    monkeypatch.setattr(nt, "disabled", lambda: True)                # TS_NOTIFY_DISABLE (the unit suite): no toast
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: pytest.fail("toast under TS_NOTIFY_DISABLE"))
    assert rs.main(["--kind", "diagnose"]) == 3
