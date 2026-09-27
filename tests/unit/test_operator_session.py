"""Operator sessions (§3.8 component 3): CLI arguments and allow-list, environment scrub, result parsing, the diff
guard, dry run and a whole run with a FAKE CLI — the real `claude` is never started here."""
from __future__ import annotations

import fnmatch
import importlib.util
import json
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tradingsystem.core.filelock import FileLock, locks_dir
from tradingsystem.core.settings import load_settings
from tradingsystem.core.timeutil import now_ms
from tradingsystem.ingest.common.appdb import AppDB
from tradingsystem.supervisor import procs

ROOT = Path(__file__).resolve().parents[2]
OPS = ROOT / "tools" / "operator"
FIXTURES = ROOT / "tests" / "fixtures" / "real"
FAKE_EXE = "C:/fake/claude.exe"


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


@pytest.fixture(scope="module")
def sa():
    return load("test_ts_session_args", OPS / "session_args.py")


@pytest.fixture()
def rs(monkeypatch, tmp_path):
    m = load("test_ts_run_session", OPS / "run_session.py")
    monkeypatch.setattr(m.sa.cc, "find_cli", lambda configured: FAKE_EXE)
    monkeypatch.setattr(procs, "running_supervisors", lambda older_s=None: {})
    rp = m.pack_module()
    hr = rp.health_module()                  # the systems the pack picks live under tmp_path, never the checkout's data
    monkeypatch.setattr(hr, "load_settings", lambda **kw: relocate(load_settings(**kw), tmp_path))
    monkeypatch.setattr(rp, "git_info", lambda root: {"sha": "abc1234", "branch": "main", "last_commits": [],
                                                      "dirty": False, "dirty_files": 0})
    monkeypatch.setattr(m, "setup_log", lambda s: None)
    return m


def relocate(s, tmp_path: Path):
    return s.model_copy(update={"paths": s.paths.model_copy(update={"data_dir": str(tmp_path / "data"),
                                                                    "logs_dir": str(tmp_path / "logs")})})


def settings_at(tmp_path: Path, **operator):
    s = relocate(load_settings(), tmp_path)
    if operator:
        s = s.model_copy(update={"operator": s.operator.model_copy(update=operator)})
    return s


def checkout(tmp_path: Path) -> Path:
    root = tmp_path / "checkout"
    (root / ".venv" / "Scripts").mkdir(parents=True)
    (root / ".venv" / "Scripts" / "python.exe").write_bytes(b"")
    return root


def result_doc(**over) -> dict:
    doc = {"type": "result", "subtype": "success", "is_error": False, "num_turns": 7, "duration_ms": 95_000,
           "result": "Checked everything.\n\n## SUMMARY\nlevel: warn\nBTCUSDT: 3 ideas, gate rr 2. tune.py refused "
                     "(cooldown). Owner: nothing.", "session_id": "sess-1", "total_cost_usd": 0.91,
           "usage": {"input_tokens": 1_200, "cache_read_input_tokens": 20_000, "cache_creation_input_tokens": 6_000,
                     "output_tokens": 2_500},
           "modelUsage": {"claude-opus-x": {"inputTokens": 1_200, "cacheReadInputTokens": 20_000,
                                            "cacheCreationInputTokens": 6_000, "outputTokens": 2_500}},
           "permission_denials": [{"tool_name": "Bash", "tool_use_id": "t1", "tool_input": {"command": "git log"}}]}
    doc.update(over)
    return doc


# ------------------------------------------------------------------ arguments and allow-list
def test_daily_args_are_the_read_only_allow_list(sa, tmp_path):
    s = settings_at(tmp_path)
    spec = sa.spec_for(s, "daily", root=tmp_path / "data" / "..", exe=FAKE_EXE)     # data root inside the checkout
    args = sa.build_args(spec)
    assert args[:4] == [FAKE_EXE, "-p", "--model", "opus"] and args[args.index("--effort") + 1] == "high"
    for pair in (["--output-format", "json"], ["--permission-mode", "dontAsk"], ["--permission-prompts", "none"],
                 ["--max-turns", str(s.operator.daily_max_turns)], ["--tools", "Read,Grep,Glob,Bash"],
                 ["--system-prompt-file", str(sa.SYSTEM_PROMPT)]):
        i = args.index(pair[0])
        assert args[i:i + 2] == pair
    assert "--setting-sources=" in args and "--setting-sources" not in args     # one token: PS 5.1 drops ""
    assert "--no-session-persistence" in args and "--strict-mcp-config" in args and "--add-dir" not in args
    dis = args[args.index("--disallowedTools") + 1:args.index("--allowedTools")]
    assert dis == ["Edit", "Write", "NotebookEdit", "WebFetch", "WebSearch", "Read(**/.env)", "Read(.env)",
                   "Read(**/.env.*)", "Read(~/.claude/**)", "Read(~/.ssh/**)", "Read(~/.aws/**)", "Read(~/.config/**)",
                   "Read(**/.credentials.json)"]
    allowed = args[args.index("--allowedTools") + 1:]
    assert allowed == ["Read", "Grep", "Glob"] + [f"Bash(.venv/Scripts/python.exe tools/{t}.py*)" for t in
                                                  ("health_report", "review_pack", "tune", "propose", "notify")]
    assert not any("git" in a.lower() for a in allowed) and not any(a.startswith("-") for a in allowed)
    assert Path(args[args.index("--system-prompt-file") + 1]).resolve().is_relative_to(ROOT.resolve())  # this checkout


def test_diagnose_adds_the_kill_switch_and_uses_the_monitor_model(sa, tmp_path):
    s = settings_at(tmp_path)
    spec = sa.spec_for(s, "diagnose", root=ROOT, exe=FAKE_EXE)
    args = sa.build_args(spec)
    assert args[args.index("--model") + 1] == s.ai.models.monitor.model == "sonnet"
    assert args[args.index("--effort") + 1] == "low"
    assert args[args.index("--max-turns") + 1] == str(s.operator.diagnose_max_turns)
    assert args[-1] == "Bash(.venv/Scripts/python.exe tools/kill_switch.py*)"
    assert "Bash(.venv/Scripts/python.exe tools/kill_switch.py*)" not in sa.allowed_tools("daily")
    # a data root outside the checkout (a scratch live run) is added; one inside is not
    assert args[args.index("--add-dir") + 1] == str(s.paths.data())
    assert spec.ledger_role == "diagnose" and sa.spec_for(s, "weekly", exe=FAKE_EXE).ledger_role == "review"
    with pytest.raises(ValueError):
        sa.spec_for(s, "hourly")


def test_the_system_prompt_names_every_allowed_command_exactly(sa):
    text = sa.SYSTEM_PROMPT.read_text(encoding="utf-8")
    for tool in sa.BASE_TOOLS + sa.DIAGNOSE_TOOLS:
        assert f"{sa.PY} tools/{tool}.py" in text
    assert "## SUMMARY" in text and "level: info|warn|critical" in text and ".env" in text
    for kind in sa.KINDS:
        body = (sa.PROMPTS / f"{kind}.md").read_text(encoding="utf-8")
        assert "$review_id" in body and "$summary_max_chars" in body and "## SUMMARY" in body


# ------------------------------------------------------------------ environment
def test_child_env_scrubs_the_calling_session_and_secrets_but_keeps_the_data_root(sa):
    parent = {"PATH": r"C:\Windows", "SYSTEMROOT": r"C:\Windows", "USERPROFILE": r"C:\Users\x",
              "TRADINGSYSTEM_CONFIG": r"C:\scratch\config.yaml", "TS_NOTIFY_DISABLE": "1",
              "CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "abc", "CLAUDE_CODE_ENTRYPOINT": "cli", "CLAUDE_EFFORT": "max",
              "ANTHROPIC_API_KEY": "sk-ant-123", "ANTHROPIC_BASE_URL": "http://x", "MT5_PASSWORD": "pw",
              "GOOGLE_API_KEY": "g", "TELEGRAM_BOT_TOKEN": "1:abc", "DASHBOARD_TOKEN": "d",
              "CLAUDE_CODE_GIT_BASH_PATH": r"C:\Git\bin\bash.exe", "PYTHONIOENCODING": "cp1252"}
    env = sa.child_env(None, parent, oauth_token="")
    for k in ("PATH", "SYSTEMROOT", "USERPROFILE", "TRADINGSYSTEM_CONFIG", "TS_NOTIFY_DISABLE", "CLAUDE_CODE_GIT_BASH_PATH"):
        assert env[k] == parent[k]
    for k in ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_EFFORT", "ANTHROPIC_API_KEY",
              "ANTHROPIC_BASE_URL", "MT5_PASSWORD", "GOOGLE_API_KEY", "TELEGRAM_BOT_TOKEN", "DASHBOARD_TOKEN",
              "CLAUDE_CODE_OAUTH_TOKEN"):
        assert k not in env
    assert env["PYTHONIOENCODING"] == "utf-8" and env["PYTHONUTF8"] == "1" and env["MSYS_NO_PATHCONV"] == "1"
    tok = sa.child_env(None, parent, oauth_token="sk-ant-oat-secret")
    assert tok["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat-secret"                # exactly as the provider passes it
    diff = sa.env_diff(parent, tok)
    assert "sk-ant-oat-secret" not in json.dumps(diff) and diff["added_or_changed"]["CLAUDE_CODE_OAUTH_TOKEN"] == "(hidden)"
    assert "MT5_PASSWORD" in diff["removed"] and "pw" not in json.dumps(diff)


# ------------------------------------------------------------------ result parsing
def test_success_and_turn_limit_are_both_finished_sessions(rs):
    ok = rs.parse_result(json.dumps(result_doc()), "", 0, default_model="opus")
    assert ok["status"] == "ok" and ok["num_turns"] == 7 and ok["model"] == "claude-opus-x"
    assert ok["ledger_tokens"] == {"input": 27_200, "cached": 20_000, "output": 2_500}
    assert ok["permission_denials"] == [{"tool": "Bash", "input": '{"command": "git log"}'}]
    mt = rs.parse_result(json.dumps(result_doc(subtype="error_max_turns", is_error=True, result=None,
                                               errors=["Reached maximum number of turns (30)"])), "", 1)
    assert mt["status"] == "max_turns" and mt["text"] is None and "error" not in mt
    assert mt["ledger_tokens"]["input"] == 27_200


def test_errors_are_classified_from_the_error_only(rs):
    real = (FIXTURES / "claude_code_not_logged_in.json").read_text(encoding="utf-8")
    nl = rs.parse_result(real, "", 1)
    assert nl["status"] == "error" and nl["error_kind"] == "not_signed_in"
    lim = rs.parse_result(json.dumps(result_doc(subtype="error_during_execution", is_error=True, result=None,
                                                errors=["You've hit your limit · resets 3pm"])), "", 1)
    assert lim["error_kind"] == "usage_limit"
    # a successful review that merely talks about a usage limit is a success
    talk = rs.parse_result(json.dumps(result_doc(result="the usage limit gauge is at level 1\n## SUMMARY\nfine")), "", 0)
    assert talk["status"] == "ok" and "error_kind" not in talk
    to = rs.parse_result("", "", None, timed_out=True, default_model="opus")
    assert to["status"] == "timeout" and to["ledger_tokens"]["input"] == 0 and to["model"] == "opus"
    crash = rs.parse_result("", "Error: something broke", 3)
    assert crash["status"] == "error" and "something broke" in crash["error"]


def test_summary_extraction(rs):
    text = "work…\n## SUMMARY\nlevel: critical\nETH: position without SL.\nOwner: check MT5 now."
    assert rs.extract_summary(text, 1500) == ("ETH: position without SL.\nOwner: check MT5 now.", "critical")
    assert rs.extract_summary("x\n### Summary:\n**level:** warning\nshort", 100) == ("short", "warn")
    s, lvl = rs.extract_summary("no heading here " * 50, 100)
    assert lvl == "info" and len(s) == 100 and s.startswith("…")               # the end of the message stands in
    s, _ = rs.extract_summary("## SUMMARY\nlevel: info\n" + "y" * 500, 100)
    assert len(s) == 100 and s.endswith("…")
    assert rs.extract_summary(None, 100) == (None, "warn")


# ------------------------------------------------------------------ diff guard
def test_diff_guard_sees_new_removed_and_rewritten_files(rs):
    before = {" M src/a.py": "h1", "?? notes.txt": "h2"}
    assert rs.diff_guard(before, dict(before))["clean"] is True
    g = rs.diff_guard(before, {" M src/a.py": "h9", "?? new.py": "h3"})
    assert g["clean"] is False and g["added"] == ["?? new.py"] and g["removed"] == ["?? notes.txt"]
    assert g["content_changed"] == [" M src/a.py"]
    assert rs.diff_guard(None, before)["clean"] is None


def test_git_snapshot_hashes_dirty_files_read_only(rs, tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("x", encoding="utf-8")
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(cmd=cmd, env=kw.get("env"))
        return subprocess.CompletedProcess(cmd, 0, " M a.py\n?? gone.txt\n", "")

    monkeypatch.setattr(rs.subprocess, "run", fake_run)
    snap = rs.git_snapshot(tmp_path)
    assert seen["cmd"][:4] == ["git", "-C", str(tmp_path), "status"] and seen["env"]["GIT_OPTIONAL_LOCKS"] == "0"
    assert snap[" M a.py"] != "-" and snap["?? gone.txt"] == "-"


# ------------------------------------------------------------------ whole runs with a fake CLI
class FakeCli:
    def __init__(self, doc: dict | None, *, timed_out: bool = False) -> None:
        self.doc, self.timed_out, self.calls = doc, timed_out, []

    def __call__(self, args, *, cwd, env, stdin_path, stdout_path, stderr_path, timeout_s):
        self.calls.append({"args": args, "cwd": cwd, "env": env, "stdin": Path(stdin_path).read_text(encoding="utf-8"),
                           "timeout_s": timeout_s})
        Path(stdout_path).write_text(json.dumps(self.doc) if self.doc else "", encoding="utf-8")
        Path(stderr_path).write_text("", encoding="utf-8")
        return (None if self.timed_out else 0), self.timed_out, 12.0


@pytest.fixture()
def wired(rs, tmp_path, monkeypatch):
    sent: list[tuple] = []
    monkeypatch.setattr(rs, "send", lambda s, level, title, text, key=None: sent.append((level, title, text, key)))
    monkeypatch.setattr(rs, "wait_for_start", lambda workdir, max_wait_s=0: True)
    monkeypatch.setattr(rs, "check_auth", lambda exe, env, workdir: None)
    snaps = iter([{" M src/x.py": "h"}, {" M src/x.py": "h"}])
    monkeypatch.setattr(rs, "git_snapshot", lambda root: next(snaps))
    return sent


def _session(tmp_path) -> dict:
    files = list((tmp_path / "data" / "reviews").glob("*.session.json"))
    assert len(files) == 1
    return json.loads(files[0].read_text(encoding="utf-8"))


def test_a_daily_run_records_usage_notifies_the_summary_and_keeps_a_clean_guard(rs, wired, tmp_path, monkeypatch):
    s = settings_at(tmp_path)
    for pair in ("BTCUSDT", "ETHUSDT"):                 # the production layout: one system per pair
        AppDB(tmp_path / "data" / "instances" / pair / "app.db").close()
    cli = FakeCli(result_doc())
    monkeypatch.setattr(rs, "spawn_cli", cli)
    root = checkout(tmp_path)
    assert rs.run("daily", s=s, root=root, out=lambda *a: None) == 0
    call = cli.calls[0]
    assert call["cwd"] == root and call["args"][0] == FAKE_EXE and "--setting-sources=" in call["args"]
    assert call["stdin"].startswith("# Daily review ") and "# Review pack — daily" in call["stdin"]
    assert "$review_id" not in call["stdin"] and call["timeout_s"] <= s.operator.daily_timeout_min * 60
    assert "CLAUDECODE" not in call["env"] and call["env"]["PYTHONUTF8"] == "1"
    rec = _session(tmp_path)
    assert rec["status"] == "ok" and rec["summary_level"] == "warn" and rec["summary"].startswith("BTCUSDT: 3 ideas")
    assert rec["diff_guard"]["clean"] is True and rec["ledger"]["recorded"] is True
    assert rec["result"]["permission_denials"][0]["tool"] == "Bash"
    assert [x[:2] for x in wired] == [("warn", "Daily review")] and "7 turns" in wired[0][2]
    con = sqlite3.connect(tmp_path / "data" / "shared" / "ai_usage.db")    # production layout: per-pair → shared
    row = con.execute("SELECT provider, model, purpose, pair, input_tokens, cached_tokens, output_tokens, cost_usd, "
                      "ok, role, num_turns, api_equivalent_usd FROM ai_usage").fetchall()
    con.close()
    assert row == [("claude_code", "claude-opus-x", "operator_daily", None, 27_200, 20_000, 2_500, 0.0, 1, "review", 7, 0.91)]


def test_a_touched_checkout_is_reported_never_reverted(rs, wired, tmp_path, monkeypatch):
    s = settings_at(tmp_path)
    monkeypatch.setattr(rs, "spawn_cli", FakeCli(result_doc()))
    snaps = iter([{}, {"?? tools/evil.py": "h"}])
    monkeypatch.setattr(rs, "git_snapshot", lambda root: next(snaps))
    assert rs.run("weekly", s=s, root=checkout(tmp_path), out=lambda *a: None) == 0
    assert any(t == "review_touched_checkout" and k == "review_touched_checkout" and "tools/evil.py" in x
               for _, t, x, k in wired)
    assert _session(tmp_path)["diff_guard"]["added"] == ["?? tools/evil.py"]


def test_turn_limit_error_and_timeout_endings(rs, wired, tmp_path, monkeypatch):
    s = settings_at(tmp_path)
    monkeypatch.setattr(rs, "spawn_cli", FakeCli(result_doc(subtype="error_max_turns", is_error=True, result=None,
                                                            errors=["Reached maximum number of turns (12)"])))
    assert rs.run("diagnose", s=s, root=checkout(tmp_path), out=lambda *a: None) == 0
    rec = _session(tmp_path)
    assert rec["status"] == "max_turns" and rec["summary"] is None and wired[-1][0] == "warn"
    for f in (tmp_path / "data" / "reviews").glob("*.session.json"):
        f.unlink()
    monkeypatch.setattr(rs, "git_snapshot", lambda root: {})
    monkeypatch.setattr(rs, "spawn_cli", FakeCli(None, timed_out=True))
    assert rs.run("daily", s=s, root=checkout(tmp_path / "b"), out=lambda *a: None, now=now_ms() + 2000) == 1
    assert _session(tmp_path)["status"] == "timeout"


def test_not_signed_in_never_starts_the_session(rs, wired, tmp_path, monkeypatch):
    s = settings_at(tmp_path)
    cli = FakeCli(result_doc())
    monkeypatch.setattr(rs, "spawn_cli", cli)
    monkeypatch.setattr(rs, "check_auth", lambda exe, env, workdir: "operator session: signed in with an API key")
    assert rs.run("daily", s=s, root=checkout(tmp_path), out=lambda *a: None) == 1
    assert not cli.calls and _session(tmp_path)["status"] == "not_signed_in"
    assert not (tmp_path / "data" / "shared" / "ai_usage.db").exists()


def test_disabled_busy_and_gauge_skip(rs, wired, tmp_path, monkeypatch):
    cli = FakeCli(result_doc())
    monkeypatch.setattr(rs, "spawn_cli", cli)
    off = settings_at(tmp_path, enabled=False)
    assert rs.run("daily", s=off, root=checkout(tmp_path), out=lambda *a: None) == 2
    assert _session(tmp_path)["status"] == "disabled"
    for f in (tmp_path / "data" / "reviews").glob("*"):
        f.unlink()
    s = settings_at(tmp_path)
    held = FileLock(locks_dir(s) / rs.LOCK_NAME)
    assert held.acquire()
    try:
        t0 = time.monotonic()
        assert rs.run("diagnose", s=s, root=checkout(tmp_path / "b"), out=lambda *a: None) == 2
        assert time.monotonic() - t0 < 5 and _session(tmp_path)["status"] == "busy"        # a diagnosis never waits
    finally:
        held.release()
    for f in (tmp_path / "data" / "reviews").glob("*"):
        f.unlink()
    rp = rs.pack_module()
    monkeypatch.setattr(rp, "gauge_state", lambda s, ledger, now: {"level": 2, "enforce": True, "reason": "week 93 %"})
    assert rs.run("daily", s=s, root=checkout(tmp_path / "c"), out=lambda *a: None) == 2
    assert _session(tmp_path)["status"] == "gauge_paused" and not cli.calls
    for f in (tmp_path / "data" / "reviews").glob("*.session.json"):
        f.unlink()
    assert rs.run("diagnose", s=s, root=checkout(tmp_path / "d"), out=lambda *a: None) == 0   # event-like: runs
    assert len(cli.calls) == 1


def test_dry_run_builds_the_pack_and_prints_the_command_only(rs, tmp_path, monkeypatch):
    s = settings_at(tmp_path)
    monkeypatch.setattr(rs, "spawn_cli", lambda *a, **k: pytest.fail("the CLI must not run in a dry run"))
    monkeypatch.setattr(rs, "check_auth", lambda *a, **k: pytest.fail("no sign-in check in a dry run"))
    monkeypatch.setattr(rs, "send", lambda *a, **k: pytest.fail("no notification in a dry run"))
    monkeypatch.setattr(rs, "git_snapshot", lambda root: {})
    monkeypatch.setenv("CLAUDECODE", "1")
    printed: list[str] = []
    assert rs.run("daily", s=s, dry_run=True, root=checkout(tmp_path), out=printed.append,
                  reason="$not_a_placeholder") == 0
    doc = json.loads(printed[-1])
    assert doc["dry_run"] is True and doc["args"][0] == FAKE_EXE and doc["problems"] == []
    assert "CLAUDECODE" in doc["env"]["removed"] and doc["env"]["added_or_changed"]["MSYS_NO_PATHCONV"] == "1"
    reviews = tmp_path / "data" / "reviews"
    assert (reviews / f"{doc['review_id']}.md").exists() and (reviews / f"{doc['review_id']}.prompt.md").exists()
    assert not list(reviews.glob("*.session.json"))
    # a checkout without the venv: reported (a real run refuses — the allow-listed commands would all fail)
    bare = tmp_path / "bare"
    bare.mkdir()
    printed.clear()
    assert rs.run("daily", s=s, dry_run=True, root=bare, out=printed.append) == 0
    assert any(".venv" in p for p in json.loads(printed[-1])["problems"])


def test_spawn_cli_feeds_stdin_from_a_file_and_kills_on_timeout(rs, tmp_path):
    stdin, out, err = tmp_path / "in.txt", tmp_path / "out.txt", tmp_path / "err.txt"
    stdin.write_text("héllo ≥", encoding="utf-8")
    code = "import sys; d = sys.stdin.buffer.read(); sys.stdout.buffer.write(d[::-1])"
    rc, timed_out, _ = rs.spawn_cli([sys.executable, "-c", code], cwd=tmp_path, env=None, stdin_path=stdin,
                                    stdout_path=out, stderr_path=err, timeout_s=60)
    assert rc == 0 and not timed_out and out.read_bytes() == "héllo ≥".encode("utf-8")[::-1]
    t0 = time.monotonic()
    rc, timed_out, _ = rs.spawn_cli([sys.executable, "-c", "import time; time.sleep(60)"], cwd=tmp_path, env=None,
                                    stdin_path=stdin, stdout_path=out, stderr_path=err, timeout_s=1)
    assert timed_out and time.monotonic() - t0 < 30


# ------------------------------------------------------------------ Task Scheduler scripts
def _ps_like(pattern: str) -> re.Pattern:
    return re.compile(fnmatch.translate(pattern), re.I)          # PowerShell -like: * and ? wildcards, no case


def test_install_autostart_never_matches_the_operator_task_names():
    """install_autostart.ps1 deletes every task its Get-TsTasks filter matches when the layout changes: the operator
    tasks must stay outside it (TradingSystemOps-*, not TradingSystem-*)."""
    text = (ROOT / "scripts" / "install_autostart.ps1").read_text(encoding="utf-8")
    fn = text[text.index("function Get-TsTasks"):]
    fn = fn[:fn.index("return $names")]
    eq = re.findall(r'\$n -eq "([^"]+)"', fn)
    like = re.findall(r'\$n -like "([^"]+)"', fn)
    assert eq == ["TradingSystem"] and like == ["TradingSystem-*"]

    def matched(name: str) -> bool:
        return name in eq or any(_ps_like(p).match(name) for p in like)

    ops = ROOT / "scripts" / "install_operator_tasks.ps1"
    names = re.findall(r'"(TradingSystemOps-[A-Za-z]+)"', ops.read_text(encoding="ascii"))
    assert sorted(set(names)) == ["TradingSystemOps-Monitor", "TradingSystemOps-ReviewDaily", "TradingSystemOps-ReviewWeekly"]
    assert not any(matched(n) for n in names)
    assert matched("TradingSystem-Review-Daily") and matched("TradingSystem-BTCUSDT")   # the filter itself works


def test_task_scripts_are_ascii_and_derive_the_root():
    for f in (ROOT / "scripts" / "install_operator_tasks.ps1", ROOT / "scripts" / "install_operator_tasks.bat",
              OPS / "run_session.ps1"):
        raw = f.read_bytes()
        assert all(b < 128 for b in raw), f"{f.name} must be pure ASCII (PowerShell 5.1)"
        assert not re.search(rb'\$root\s*=\s*"', raw)                          # the root is derived, never written in
        assert b"2>&1" not in raw
    ops = (ROOT / "scripts" / "install_operator_tasks.ps1").read_text(encoding="ascii")
    assert "StartBoundary" in ops and "'Z'" in ops and "-LogonType Interactive" in ops and "-DryRun" in ops
    assert "New-TimeSpan -Minutes 15" not in ops or "$MonitorMinutes" in ops
    assert "[int]$DailyLimitMinutes = 20" in ops and "[int]$WeeklyLimitMinutes = 40" in ops
    assert '$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path' in ops
    launcher = (OPS / "run_session.ps1").read_text(encoding="ascii")
    assert 'Join-Path $PSScriptRoot "..\\.."' in launcher and "run_session.py" in launcher


# ------------------------------------------------------------------ tools/kill_switch.py (the diagnosis' only switch)
def test_kill_switch_tool_is_on_only_and_stays_in_the_loaded_data_root(tmp_path, monkeypatch, capsys):
    ks = load("test_ts_kill_switch", ROOT / "tools" / "kill_switch.py")
    sent: list[tuple] = []
    monkeypatch.setattr(ks, "notify", lambda s, level, title, text, key=None: sent.append((level, title, key)))
    monkeypatch.setattr(ks, "setup_log", lambda s: None)
    s = settings_at(tmp_path)
    pair_file = tmp_path / "data" / "instances" / "BTCUSDT" / "KILL_SWITCH"
    assert ks.main(["--pair", "btcusdt", "--reason", "order burst: 5 in 40 min"], settings=s) == 0
    assert pair_file.exists() and not (tmp_path / "data" / "KILL_SWITCH").exists()
    assert json.loads(pair_file.read_text(encoding="utf-8"))["reason"] == "order burst: 5 in 40 min"
    assert sent == [("critical", "kill switch ON: BTCUSDT", "kill_switch_BTCUSDT")]
    assert ks.main(["--pair", "BTCUSDT", "--reason", "again"], settings=s) == 0          # already on: kept as it was
    assert json.loads(pair_file.read_text(encoding="utf-8"))["reason"] == "order burst: 5 in 40 min" and len(sent) == 1
    assert ks.main(["--reason", "no scope"], settings=s) == 3                           # the global one needs --all
    assert ks.main(["--pair", "DOGEUSDT", "--reason", "x"], settings=s) == 3
    assert ks.main(["--pair", "ETHUSDT"], settings=s) == 3                              # a reason is required
    assert ks.main(["--pair", "ETHUSDT", "--reason", "x", "--off"], settings=s) == 3    # there is no OFF
    assert ks.main(["--all", "--reason", "equity -12 %"], settings=s) == 0
    assert (tmp_path / "data" / "KILL_SWITCH").exists()
    capsys.readouterr()
    assert ks.main(["--status"], settings=s) == 0
    rows = {r["scope"]: r["on"] for r in json.loads(capsys.readouterr().out)}
    assert rows["all"] is True and rows["BTCUSDT"] is True and rows["ETHUSDT"] is False


def test_the_prompt_fills_the_checklist_and_never_reparses_the_reason(rs, tmp_path):
    s = settings_at(tmp_path)
    spec = rs.sa.spec_for(s, "diagnose", exe=FAKE_EXE)

    class P:
        data = {"pairs": ["BTCUSDT"]}
        md = "# Review pack — diagnose\nprice $5 and $pair stay literal"
        md_path, json_path = tmp_path / "x.md", tmp_path / "x.json"

    text = rs.compose_prompt(spec, review_id="20260927T120000Z_diagnose", pack=P, summary_max_chars=900,
                             now=now_ms(), reason="order burst $pair (4 orders)")
    assert text.startswith("# Diagnosis 20260927T120000Z_diagnose")
    assert "order burst $pair (4 orders)" in text and "at most 900 characters" in text
    assert text.endswith("price $5 and $pair stay literal") and "$review_id" not in text


def test_the_real_session_result_is_parsed(rs):
    """The Phase 4 live daily review (Opus, 10 turns): a finished session with the ledger's token convention."""
    raw = (Path(__file__).resolve().parents[1] / "fixtures" / "real" / "claude_code_session_result.json").read_text(
        encoding="utf-8")
    out = rs.parse_result(raw, "", 0)
    assert out["status"] == "ok" and not out.get("error")
    assert out["ledger_tokens"] == {"input": 18 + 176_610 + 27_314, "cached": 176_610, "output": 7_415}
