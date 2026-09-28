"""tools/recorder_keepalive.py (Phase 5 A3): the STOP file, no MT5 terminal, never two recorders, the crash-loop
guard, a hung recorder left alone (the advice: taskkill), the detached start with the P1.12 arguments, the task wiring,
and scripts/start_recorder.bat going through the same check (static: never run here).
status.json is the real file the production recorder wrote (tests/fixtures/real/recorder_status.json). Nothing here
starts the recorder or MT5: the spawn is replaced, except for one harmless stand-in script that proves the process scan."""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tradingsystem.core.timeutil import MS_PER_MINUTE, to_ms

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "real"
STATUS = json.loads((FIXTURES / "recorder_status.json").read_text(encoding="utf-8"))
UPDATED_MS = to_ms(dt.datetime.fromisoformat(STATUS["updated"].replace("Z", "+00:00")))


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


class FakeProc:
    def __init__(self, pid: int = 5555, code: int | None = None) -> None:
        self.pid, self.code = pid, code

    def poll(self):
        return self.code


@pytest.fixture()
def ka(monkeypatch, tmp_path):
    m = load("test_ts_recorder_keepalive", ROOT / "tools" / "recorder_keepalive.py")
    out = tmp_path / "price_matching"
    out.mkdir()
    shutil.copyfile(FIXTURES / "recorder_status.json", out / "status.json")
    monkeypatch.setattr(m, "OUT", out)
    monkeypatch.setattr(m, "setup_logging", lambda *a, **k: None)
    monkeypatch.setattr(m, "mt5_running", lambda path: True)
    monkeypatch.setattr(m, "recorder_pids", lambda status_pid=None: [])
    monkeypatch.setattr(m, "START_CHECK_S", 0.2)
    m.spawned = []

    def spawn(cmd):
        m.spawned.append(cmd)
        return FakeProc()

    monkeypatch.setattr(m, "spawn", spawn)
    return m


def test_keepalive_does_nothing_while_the_stop_file_exists(ka):
    (ka.OUT / "STOP").write_text("", encoding="utf-8")
    assert ka.check(now=UPDATED_MS + 60 * MS_PER_MINUTE)[0] == "stopped"
    assert ka.main(["--quiet"]) == 0 and ka.spawned == [] and (ka.OUT / "STOP").exists()   # never removes it


def test_keepalive_never_starts_the_recorder_without_the_mt5_terminal(ka, monkeypatch):
    seen = []
    monkeypatch.setattr(ka, "mt5_running", lambda path: seen.append(path) or False)
    assert ka.check(now=UPDATED_MS + 60 * MS_PER_MINUTE)[0] == "no_mt5"
    assert ka.spawned == [] and seen == [ka.recorder_constant("MT5_PATH", "?")]
    assert seen[0] == "C:/Program Files/MetaTrader 5/terminal64.exe"      # the terminal the recorder attaches to


def test_keepalive_never_starts_a_second_recorder(ka, monkeypatch):
    monkeypatch.setattr(ka, "recorder_pids", lambda status_pid=None: [7777])     # e.g. a fresh one, no flush yet
    outcome, detail = ka.check(now=UPDATED_MS + 60 * MS_PER_MINUTE)
    assert outcome == "alive" and "7777" in detail and ka.spawned == []


def test_keepalive_leaves_a_hung_recorder_alone_and_warns_once(ka, monkeypatch, caplog):
    monkeypatch.setattr(ka, "recorder_pids", lambda status_pid=None: [STATUS["pid"]])
    monkeypatch.setattr(ka, "now_ms", lambda: UPDATED_MS + 20 * MS_PER_MINUTE)
    outcome, detail = ka.check(now=UPDATED_MS + 20 * MS_PER_MINUTE)
    assert outcome == "stale" and "last flush 20 min ago" in detail and ka.spawned == []
    # a hung recorder reads STOP only after a completed flush (recorder.py flusher): the advice ends the process
    assert f"end it with 'taskkill /PID {STATUS['pid']} /T /F', then the TradingSystemOps-Recorder task starts a " \
           "fresh one within 5 min (by hand: scripts\\start_recorder.bat)" in detail
    assert "STOP" not in detail and detail.isascii()                  # printed on a cp1252 console by the .bat
    with caplog.at_level("WARNING", logger="recorder-keepalive"):
        assert ka.main(["--quiet"]) == 0
        assert ka.main(["--quiet"]) == 0
    assert len([r for r in caplog.records if "stale" in r.getMessage()]) == 1    # once per change, not every 5 min
    # a live recorder that flushed recently is simply alive
    assert ka.check(now=UPDATED_MS + 4 * MS_PER_MINUTE)[0] == "alive"


def test_keepalive_starts_the_recorder_detached_with_the_launcher_arguments(ka, monkeypatch):
    monkeypatch.setattr(ka, "now_ms", lambda: UPDATED_MS + 60 * MS_PER_MINUTE)
    assert ka.main(["--quiet"]) == 0
    assert len(ka.spawned) == 1
    cmd = ka.spawned[0]
    assert Path(cmd[1]) == ROOT / "research" / "price_matching" / "recorder.py"
    assert cmd[2:] == ["--flush-s", "300", "--mt5-interval-ms", "50"]
    assert Path(cmd[0]).name.lower() in ("pythonw.exe", Path(sys.executable).name.lower())
    state = json.loads((ka.OUT / ka.STATE_FILE).read_text(encoding="utf-8"))
    assert state["last_outcome"] == "started" and state["starts"] == [UPDATED_MS + 60 * MS_PER_MINUTE]


def test_keepalive_spawns_outside_the_task_job_without_a_console(monkeypatch):
    m = load("test_ts_recorder_keepalive_spawn", ROOT / "tools" / "recorder_keepalive.py")
    seen = {}
    monkeypatch.setattr(m.winops, "spawn_outside", lambda cmd, **kw: seen.update(cmd=cmd, **kw) or FakeProc())
    m.spawn(["pythonw.exe", "recorder.py"])
    assert seen == {"cmd": ["pythonw.exe", "recorder.py"], "cwd": m.ROOT, "console": False}


def test_keepalive_reports_a_recorder_that_exits_at_once(ka, monkeypatch):
    monkeypatch.setattr(ka, "spawn", lambda cmd: FakeProc(code=1))
    monkeypatch.setattr(ka, "now_ms", lambda: UPDATED_MS + 60 * MS_PER_MINUTE)
    assert ka.main(["--quiet"]) == ka.EXIT_FAILED
    state = json.loads((ka.OUT / ka.STATE_FILE).read_text(encoding="utf-8"))
    assert state["last_outcome"] == "start_failed" and "code 1" in state["last_detail"] and len(state["starts"]) == 1


def test_keepalive_crash_loop_guard_stops_after_three_starts_without_a_flush(ka, monkeypatch):
    t0 = UPDATED_MS + 60 * MS_PER_MINUTE                       # the last flush is older than every start below
    (ka.OUT / ka.STATE_FILE).write_text(json.dumps({"starts": [t0, t0 + 5 * MS_PER_MINUTE, t0 + 10 * MS_PER_MINUTE]}),
                                        encoding="utf-8")
    assert ka.check(now=t0 + 15 * MS_PER_MINUTE)[0] == "crash_loop" and ka.spawned == []
    assert ka.check(now=t0 + 61 * MS_PER_MINUTE)[0] == "started"             # the window moved on: one more try
    # a flush after the first start = the recorder did run: no crash loop
    (ka.OUT / ka.STATE_FILE).write_text(json.dumps({"starts": [UPDATED_MS - 10 * MS_PER_MINUTE,
                                                               UPDATED_MS - 5 * MS_PER_MINUTE, UPDATED_MS + 1]}),
                                        encoding="utf-8")
    assert ka.check(now=UPDATED_MS + 20 * MS_PER_MINUTE)[0] == "started"


def test_keepalive_crash_loop_is_logged_once_not_every_run(ka, monkeypatch, caplog):
    t0 = UPDATED_MS + 60 * MS_PER_MINUTE
    (ka.OUT / ka.STATE_FILE).write_text(json.dumps({"starts": [t0, t0 + 5 * MS_PER_MINUTE, t0 + 10 * MS_PER_MINUTE]}),
                                        encoding="utf-8")
    monkeypatch.setattr(ka, "now_ms", lambda: t0 + 15 * MS_PER_MINUTE)
    with caplog.at_level("INFO", logger="recorder-keepalive"):
        assert ka.main(["--quiet"]) == 0 and ka.main(["--quiet"]) == 0
    assert [r.levelname for r in caplog.records if "crash_loop" in r.getMessage()] == ["ERROR"] and ka.spawned == []
    assert len(json.loads((ka.OUT / ka.STATE_FILE).read_text(encoding="utf-8"))["starts"]) == 3   # kept, not grown


def test_keepalive_dry_run_starts_and_writes_nothing(ka, capsys):
    assert ka.main(["--dry-run"]) == 0
    assert ka.spawned == [] and not (ka.OUT / ka.STATE_FILE).exists()
    assert "would_start" in capsys.readouterr().out


def test_is_recorder_cmd_matches_the_launcher_forms_only():
    m = load("test_ts_recorder_keepalive_cmd", ROOT / "tools" / "recorder_keepalive.py")
    assert m.is_recorder_cmd([".venv\\Scripts\\python.exe", "research\\price_matching\\recorder.py", "--flush-s", "300"])
    assert m.is_recorder_cmd(["C:\\x\\pythonw.exe", "C:\\the_claude_new\\research\\price_matching\\recorder.py"])
    assert m.is_recorder_cmd(["python", "research/price_matching/recorder.py"])
    assert not m.is_recorder_cmd(["python", "tools/recorder_keepalive.py"])
    assert not m.is_recorder_cmd(["python", "-m", "tradingsystem", "run", "all"])
    assert not m.is_recorder_cmd(["research/price_matching/recorder.py"])              # the interpreter is cmd[0]


def test_recorder_pids_finds_a_running_recorder_process(tmp_path):
    """The real process scan: a stand-in script at .../price_matching/recorder.py that only sleeps."""
    m = load("test_ts_recorder_keepalive_scan", ROOT / "tools" / "recorder_keepalive.py")
    script = tmp_path / "research" / "price_matching" / "recorder.py"
    script.parent.mkdir(parents=True)
    script.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
    p = subprocess.Popen([sys.executable, str(script)])
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and p.pid not in m.recorder_pids():
            time.sleep(0.3)
        assert p.pid in m.recorder_pids()
        assert m.recorder_pids(status_pid=p.pid)[0] == p.pid           # the status file's pid is checked first
    finally:
        p.kill()
        p.wait(10)
    assert p.pid not in m.recorder_pids(status_pid=p.pid)


def test_keepalive_matches_the_recorder():
    m = load("test_ts_recorder_keepalive_pins", ROOT / "tools" / "recorder_keepalive.py")
    rec = (ROOT / "research" / "price_matching" / "recorder.py").read_text(encoding="utf-8")
    assert 'OUT = PROJECT_ROOT / "data" / "research" / "price_matching"' in rec
    assert m.OUT == ROOT / "data" / "research" / "price_matching"
    assert m.recorder_constant("MT5_PATH", "?") == m.DEFAULT_MT5_PATH
    assert m.RECORDER_ARGS == ["--flush-s", "300", "--mt5-interval-ms", "50"]
    assert '(OUT / "STOP")' in rec and '"pid": os.getpid()' in rec and '"updated": iso(now_ms())' in rec
    # STOP is read only in the flusher, after the flush and the status write: a hung recorder never sees it
    flusher = rec.split("async def flusher", 1)[1]
    assert flusher.index("buf.flush") < flusher.index('(OUT / "STOP").exists()')


def test_start_recorder_bat_goes_through_the_keepalive_check_never_straight_to_recorder_py():
    """start_recorder.bat never starts a second recorder: after the MT5 wait it removes STOP and runs the keep-alive
    once (not --quiet: its answer is shown), whose check starts nothing while a recorder.py process runs. It says the
    STOP file is the only way to stop the recorder (the task restarts one closed any other way)."""
    raw = (ROOT / "scripts" / "start_recorder.bat").read_bytes()
    assert raw.isascii() and b"\n" not in raw.replace(b"\r\n", b"")        # cmd.exe: ASCII + CRLF
    text = raw.decode("ascii")
    code = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().lower().startswith("rem ")]
    runs = [ln for ln in code if "%PY%" in ln and not ln.lower().startswith(("set ", "if not exist", "echo "))]
    assert runs == ['"%PY%" tools\\recorder_keepalive.py']                  # the only start, without --quiet
    assert not [ln for ln in code if ln.lower().startswith("start ") or "price_matching\\recorder.py" in ln]
    wait, delete, keep = (code.index(':run'), code.index('del /q "data\\research\\price_matching\\STOP" 2>nul'),
                          code.index(runs[0]))
    assert code.index("tasklist /FI \"IMAGENAME eq terminal64.exe\" 2>nul | find /i \"terminal64.exe\" >nul "
                      "&& goto run") < wait < delete < keep                # the MT5 wait is kept, STOP removed first
    header = " ".join(ln[4:].strip() for ln in text.splitlines()[1:8] if ln.lower().startswith("rem "))
    assert "Stop it only by creating data\\research\\price_matching\\STOP" in header
    assert "restarts (hidden) a recorder that was closed or ended any other way" in header
    assert "closing its window)" not in text


def test_the_start_by_hand_adds_no_recorder_while_one_runs(ka, monkeypatch, capsys):
    """What start_recorder.bat does: STOP removed, then ``recorder_keepalive.py`` without --quiet — a running
    recorder (for example the task's hidden one after a resume) is reported, none is added."""
    assert not (ka.OUT / "STOP").exists()                                   # the .bat has removed it
    monkeypatch.setattr(ka, "recorder_pids", lambda status_pid=None: [7777])
    monkeypatch.setattr(ka, "now_ms", lambda: UPDATED_MS + 3 * MS_PER_MINUTE)
    assert ka.main([]) == ka.EXIT_OK and ka.spawned == []
    assert capsys.readouterr().out.strip() == "recorder: alive - pid 7777, last flush 3 min ago"
    monkeypatch.setattr(ka, "recorder_pids", lambda status_pid=None: [])  # none runs: it starts one
    assert ka.main([]) == ka.EXIT_OK and len(ka.spawned) == 1
    assert capsys.readouterr().out.startswith("recorder: started - pid 5555: ")


def test_recorder_task_runs_every_5_minutes_from_the_operator_tasks_not_autostart():
    ops = (ROOT / "scripts" / "install_operator_tasks.ps1").read_text(encoding="ascii")
    assert re.search(r"\[int\]\$RecorderMinutes = 5\b", ops) and re.search(r"\[int\]\$RecorderLimitMinutes = 4\b", ops)
    assert '$recorderPy = Join-Path $root "tools\\recorder_keepalive.py"' in ops
    assert "-Execute $pyw -Argument \"`\"$recorderPy`\" --quiet\"" in ops
    assert "-RepetitionInterval (New-TimeSpan -Minutes $RecorderMinutes)" in ops
    assert "(New-OpsSettings $RecorderLimitMinutes)" in ops
    auto = (ROOT / "scripts" / "install_autostart.ps1").read_text(encoding="utf-8")
    assert "Recorder" not in auto and "recorder_keepalive" not in auto      # it would delete an unknown pair task
