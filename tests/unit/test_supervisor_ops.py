"""Supervisor ops (F2/OPS-05 suspend, F7 fail-open, F8/OPS-06 child output, OPS-03 single instance, OPS-04 MT5).

Pure process-control logic: a fake clock / fake Popen drive the real watchdog; no service, no MT5, no market data.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
import sys
import threading
import types
import uuid
from pathlib import Path

import pytest

from tradingsystem.core.logsetup import get_redactor, setup_from_settings, setup_logging
from tradingsystem.core.settings import PROJECT_ROOT, load_settings
from tradingsystem.supervisor import control, procs, winops
from tradingsystem.supervisor import supervisor as sv
from tradingsystem.supervisor.winops import ClockSample, time_gap

WIN = os.name == "nt"


# --------------------------------------------------------------------------- clock classification
def _cs(wall: float, tick: float, awake: float) -> ClockSample:
    return ClockSample(wall, tick, awake)


def test_time_gap_classifies_normal_suspend_jump_stall():
    a = _cs(1000.0, 500.0, 400.0)
    assert time_gap(a, _cs(1005.01, 505.0, 405.0), 5) is None
    # S3 sleep of 41 min (2025-09-25 incident): wall and tick run on, the unbiased (awake) clock stops
    kind, s = time_gap(a, _cs(1005 + 2457, 505 + 2457, 405.0), 5)
    assert kind == "suspend" and s == pytest.approx(2457)
    assert time_gap(a, _cs(1005 + 3600, 505.0, 405.0), 5) == ("clock_jump", pytest.approx(3600))
    assert time_gap(a, _cs(1005 - 7200, 505.0, 405.0), 5) == ("clock_jump", pytest.approx(-7200))
    assert time_gap(a, _cs(1030.0, 530.0, 430.0), 5) == ("stall", pytest.approx(30))


def test_sample_clock_is_consistent():
    a = winops.sample_clock()
    b = winops.sample_clock()
    assert b.awake >= a.awake and b.tick >= a.tick and a.tick >= a.awake - 1
    assert time_gap(a, b, 0) is None


# --------------------------------------------------------------------------- watchdog harness
class FakeClock:
    def __init__(self) -> None:
        self.wall, self.tick, self.awake = 1_790_000_000.0, 10_000.0, 9_000.0

    def __call__(self) -> ClockSample:
        return ClockSample(self.wall, self.tick, self.awake)

    def run(self, s: float) -> None:            # machine awake
        self.wall += s
        self.tick += s
        self.awake += s

    def suspend(self, s: float) -> None:        # S3: every process frozen, the unbiased clock stops
        self.wall += s
        self.tick += s


class FakeProc:
    def __init__(self, pid: int) -> None:
        self.pid, self.code, self.signals = pid, None, []

    def poll(self):
        return self.code

    def wait(self, timeout=None):
        return self.code

    def send_signal(self, sig) -> None:
        self.signals.append(sig)
        self.code = 0


class Harness:
    """Real Supervisor on a tmp data/logs dir; Popen, tree kills, keep-awake and the clock are fakes."""

    def __init__(self, tmp_path: Path, monkeypatch, services=("engine",)) -> None:
        self.clock = FakeClock()
        self.db = tmp_path / "data" / "app.db"
        self.db.parent.mkdir(parents=True)
        con = sqlite3.connect(self.db)
        con.execute(sv._STATUS_DDL)
        con.commit()
        con.close()
        self.spawned: list[dict] = []
        self.killed: list[int] = []
        self.output = b""
        monkeypatch.setattr(sv.subprocess, "Popen", self._popen)
        monkeypatch.setattr(sv, "keep_awake", lambda on: False)
        s = sv.Supervisor(list(services), str(self.db.parent))
        s.s = s.s.model_copy(update={"paths": s.s.paths.model_copy(update={"logs_dir": str(tmp_path / "logs")})})
        s.term_for = {}
        s.clock = self.clock
        s.kill_tree = self.killed.append
        # fake pids must never reach OpenProcess/AssignProcessToJobObject (a real process could own that pid)
        s.job = types.SimpleNamespace(handle=None, add=lambda pid: None, pids=lambda: [],
                                      keep_members_on_close=lambda: False)
        self.sup = s

    def _popen(self, cmd, **kw):
        p = FakeProc(4000 + len(self.spawned))
        self.spawned.append({"cmd": cmd, **kw, "proc": p})
        if self.output:
            kw["stdout"].write(self.output)
        return p

    def beat(self, collector: str, ms: int | None = None) -> None:
        ms = int(self.clock.wall * 1000) if ms is None else ms
        con = sqlite3.connect(self.db)
        con.execute("INSERT INTO collector_status(collector, state, updated_ms) VALUES (?,?,?) ON CONFLICT(collector) "
                    "DO UPDATE SET updated_ms=excluded.updated_ms", (collector, "live", ms))
        con.commit()
        con.close()

    def events(self, event: str | None = None) -> list[tuple]:
        con = sqlite3.connect(self.db)
        rows = con.execute("SELECT collector, event, detail, duration_ms FROM ingestion_events ORDER BY id").fetchall()
        con.close()
        return [r for r in rows if event is None or r[1] == event]

    def loop(self, seconds: float, *, beating: bool = True, step: float = sv.LOOP_S) -> None:
        """Watch passes every ``step`` awake seconds, the children beating (or frozen) in between."""
        t = 0.0
        while t < seconds:
            if beating:
                for c in self.sup.children.values():
                    for b in c.beats:
                        self.beat(b)
            self.sup.watch()
            self.clock.run(step)
            t += step


@pytest.fixture
def h(tmp_path, monkeypatch) -> Harness:
    return Harness(tmp_path, monkeypatch)


def test_hung_child_is_killed_after_stale_awake_time(h):
    h.loop(1)                                           # starts engine
    c = h.sup.children["engine"]
    assert c.proc is not None and h.spawned
    h.beat("engine")
    h.loop(sv.STARTUP_GRACE_S - 10, beating=False)      # heartbeat frozen, still inside the start-up grace
    assert h.killed == []
    h.loop(20, beating=False)
    assert h.killed == [4000]
    (_, _, detail, _), = h.events("killed")
    assert "unchanged for 180s of awake time" in detail


def test_healthy_child_is_never_killed(h):
    h.loop(1200)
    assert h.killed == [] and len(h.spawned) == 1


def test_suspend_does_not_kill_healthy_children(h, monkeypatch):
    """2026-09-25: 41 min of S3 sleep → the old wall-clock watchdog killed ingest-mt5 and engine 3.5 s after resume."""
    h.loop(sv.STARTUP_GRACE_S + 60)                     # past the start-up grace, beating
    ticks = iter([2457.0, 0.0, 0.0])

    def fake_sleep(s):
        h.clock.run(s)
        n = next(ticks, None)
        if n:
            h.clock.suspend(n)                          # PC sleeps during the supervisor's own sleep
        if n is None:
            h.sup.stop = True

    h.sup.sleep = fake_sleep
    h.sup.run()
    assert h.events("killed") == []                     # (shutdown's tree kills are not watchdog kills)
    ev = h.events("system_suspend")
    assert len(ev) == 1 and ev[0][3] == pytest.approx(2457_000, abs=100)
    st = control.read_state(h.db.parent)
    assert st["last_gap"]["kind"] == "suspend" and st["stopping"] is True


def test_resume_grace_covers_slow_recovery_then_kills_a_still_hung_child(h):
    h.loop(sv.STARTUP_GRACE_S + 60)
    h.sup.on_gap("suspend", 3600)
    h.loop(sv.RESUME_GRACE_S - 10, beating=False)       # network / MT5 still reconnecting: no heartbeat yet
    assert h.killed == []
    h.loop(20, beating=False)                           # still frozen after the grace → it really is hung
    assert h.killed == [4000]


def test_wall_clock_steps_never_count(h):
    h.loop(sv.STARTUP_GRACE_S + 60)
    for jump in (3 * 3600, -3 * 3600):
        h.clock.wall += jump                            # NTP step / manual clock change
        h.loop(sv.LOOP_S, beating=False)                # read before the children beat again: wall age = 3 h
        h.loop(300)
    assert h.killed == []


def test_unreadable_heartbeat_fails_open_but_is_reported(h, caplog):
    h.loop(1)
    con = sqlite3.connect(h.db)
    con.execute("DELETE FROM collector_status")
    con.commit()
    con.close()
    with caplog.at_level(logging.WARNING, logger="supervisor"):
        h.loop(sv.STARTUP_GRACE_S + 60, beating=False)
    assert h.killed == []
    assert h.sup.children["engine"].unreadable >= sv.UNREADABLE_ALERT
    assert len(h.events("heartbeat_unreadable")) == 1
    assert "no collector_status row for engine" in caplog.text


def test_exit_is_logged_with_redacted_output_tail_and_restarted(h, monkeypatch):
    secret = "tok-" + uuid.uuid4().hex
    monkeypatch.setenv("TS_TEST_SECRET_TOKEN", secret)
    get_redactor().refresh(["TS_TEST_SECRET_TOKEN"])
    try:
        h.output = f"Traceback (most recent call last):\nRuntimeError: boom {secret}\n".encode()
        h.loop(1)
        call = h.spawned[0]
        assert call["stdout"] is not subprocess.DEVNULL and call["stderr"] == subprocess.STDOUT
        assert call["env"]["TS_LOG_CONSOLE"] == "0" and call["env"]["PYTHONFAULTHANDLER"] == "1"
        log_file = Path(call["stdout"].name)
        assert log_file.name == "engine.stderr.log" and log_file.parent.name == "logs"
        call["proc"].code = 0xC0000005                  # access violation (native crash)
        h.loop(sv.LOOP_S)
        (_, _, detail, _), = h.events("exited")
        assert "0xC0000005" in detail and "RuntimeError: boom" in detail
        assert secret not in detail
        h.loop(10)
        assert len(h.spawned) == 2                      # restarted after the back-off
    finally:
        get_redactor().refresh([])


def test_one_failing_child_does_not_stop_the_others(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, services=("engine", "executor"))
    real_start = h.sup.start

    def start(c):
        if c.name == "engine":
            raise OSError("spawn failed")
        real_start(c)

    h.sup.start = start
    h.loop(10)
    assert h.sup.children["executor"].proc is not None


def test_mt5_terminal_is_started_by_us_before_the_mt5_child(tmp_path, monkeypatch):
    """OPS-04: a child's ``mt5.initialize`` would start the terminal inside our job/tree — so we start it first."""
    h = Harness(tmp_path, monkeypatch, services=("ingest-mt5",))
    term = tmp_path / "MetaTrader 5" / "terminal64.exe"
    term.parent.mkdir()
    term.write_bytes(b"")
    h.sup.term_for = {"ingest-mt5": str(term)}
    alive: list[int] = []
    launched: list[tuple] = []
    order: list[str] = []

    def launch(path, task=None):
        launched.append((path, task))
        order.append("terminal")
        alive[:] = [99]
        return "task x" if task else "cmd start"

    monkeypatch.setattr(procs, "find_terminals", lambda paths: {p: list(alive) for p in paths})
    monkeypatch.setattr(procs, "launch_terminal", launch)
    real_popen = h._popen
    monkeypatch.setattr(sv.subprocess, "Popen", lambda cmd, **kw: (order.append("child"), real_popen(cmd, **kw))[1])
    h.loop(1)
    assert order == ["terminal", "child"]
    assert launched == [(str(term), h.sup.cfg.mt5_task)]
    assert h.sup.term_info[str(term)] == {"pids": [99], "in_supervisor_job": False}
    alive.clear()                                       # the terminal was closed
    h.loop(sv.TERMINAL_CHECK_S * 2)                     # missing again, but relaunches are rate limited
    assert len(launched) == 1
    h.loop(sv.TERMINAL_RELAUNCH_S)
    assert len(launched) == 2 and launched[1][1] is None     # the task did not keep it up → start it directly
    assert [e[1] for e in h.events() if e[0] == "supervisor:mt5"] == ["terminal_started"] * 2


def test_launch_terminal_never_makes_it_our_child(monkeypatch):
    calls = []
    monkeypatch.setattr(procs, "task_exists", lambda task: False)
    monkeypatch.setattr(procs, "spawn_outside", lambda cmd, **kw: calls.append((cmd, kw)) or types.SimpleNamespace(pid=7))
    procs.launch_terminal("C:/Program Files/MetaTrader 5/terminal64.exe", "TradingSystem-MT5")
    cmd, kw = calls[0]
    if WIN:
        assert cmd[:4] == ["cmd.exe", "/d", "/c", "start"] and cmd[-1].endswith("terminal64.exe")
    else:
        assert cmd == ["C:/Program Files/MetaTrader 5/terminal64.exe"]


def test_kill_tree_spares_mt5_terminals(monkeypatch):
    class P:
        def __init__(self, pid, name, kids=()):
            self.pid, self._name, self.kids, self.dead = pid, name, list(kids), False

        def name(self):
            return self._name

        def children(self, recursive=False):
            out = []
            for k in self.kids:
                out += [k] + (k.children(True) if recursive else [])
            return out

        def kill(self):
            self.dead = True

    helper = P(4, "terminal-helper.exe")
    term = P(3, "terminal64.exe", [helper])
    interp = P(2, "python.exe", [term])
    root = P(1, "python.exe", [interp])
    table = {p.pid: p for p in (root, interp, term, helper)}
    fake = types.SimpleNamespace(Process=lambda pid: table[pid], Error=Exception, wait_procs=lambda ps, timeout: None)
    monkeypatch.setattr(procs, "psutil", fake)
    procs.kill_tree(1)
    assert root.dead and interp.dead and not term.dead and not helper.dead


@pytest.mark.parametrize("cmd, expected", [
    (["C:/x/.venv/Scripts/python.exe", "-m", "tradingsystem", "run", "all"], True),
    (["python.exe", "-m", "tradingsystem", "run"], True),
    (["pythonw.exe", "-m", "tradingsystem", "run", "all", "--detach", "--auto"], False),
    (["python.exe", "-m", "tradingsystem", "run", "--status"], False),
    (["python.exe", "-m", "tradingsystem", "ingest", "--source", "mt5"], False),
    (["python.exe", "tradingsystem", "run"], False),
    ([], False),
])
def test_is_supervisor_cmd(cmd, expected):
    assert procs.is_supervisor_cmd(cmd) is expected


# --------------------------------------------------------------------------- child log files
def test_open_rotating_rolls_and_appends(tmp_path):
    p = tmp_path / "logs" / "svc.stderr.log"
    with procs.open_rotating(p, 10, 2) as f:
        f.write(b"first run output\n")
    with procs.open_rotating(p, 10, 2) as f:           # over max → rolled to .1 at the next start
        assert f.tell() == 0
        f.write(b"second\n")
    with procs.open_rotating(p, 1000, 2) as f:          # under max → appended, positioned at the end
        off = f.tell()
        f.write(b"third a\nthird b\n")
    assert (tmp_path / "logs" / "svc.stderr.log.1").read_bytes() == b"first run output\n"
    assert procs.tail(p, off) == "third a | third b"
    assert procs.tail(p, 0, lines=1) == "third b"
    assert procs.tail(None) == "" and procs.tail(tmp_path / "missing.log") == ""


# --------------------------------------------------------------------------- single instance (OPS-03)
@pytest.mark.skipif(not WIN, reason="named mutex")
def test_single_instance_lock_is_machine_wide():
    name = "TradingSystem.test." + uuid.uuid4().hex[:12]
    probe = ("import sys; from tradingsystem.supervisor.winops import acquire_instance, instance_running; "
             f"print(instance_running({name!r}), acquire_instance({name!r}, wait_s=0.2))")
    env = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}

    def other() -> str:
        return subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, env=env,
                              timeout=60).stdout.strip()

    assert winops.acquire_instance(name)
    try:
        assert winops.instance_running(name)
        assert other() == "True False"                  # a second supervisor must not start
    finally:
        winops.release_instance(name)
    assert not winops.instance_running(name)
    assert other() == "False True"


def test_instance_name_is_per_data_dir(tmp_path):
    a, b = winops.instance_name("supervisor", tmp_path / "a"), winops.instance_name("supervisor", tmp_path / "b")
    assert a != b and a == winops.instance_name("supervisor", tmp_path / "x" / ".." / "a")


def test_main_refuses_to_run_next_to_an_older_unlocked_supervisor(tmp_path, monkeypatch):
    monkeypatch.setattr(sv, "setup_from_settings", lambda *a, **k: None)
    monkeypatch.setattr(sv, "acquire_instance", lambda name, wait_s=0: True)
    monkeypatch.setattr(procs, "running_supervisors", lambda older_s=None: {1234: None})
    monkeypatch.setattr(sv, "Supervisor", lambda *a, **k: pytest.fail("must not start"))
    assert sv.main(["all", "--data-dir", str(tmp_path)]) == 3
    assert sv.main(["nope"]) == 2


# --------------------------------------------------------------------------- control: detach / stop / status
def test_heartbeat_age_uses_awake_time():
    assert control.heartbeat_age(None) is None
    assert control.heartbeat_age({"awake_s": 100.0}, awake_now=130.0) == pytest.approx(30.0)
    assert control.heartbeat_age({"awake_s": 5000.0}, awake_now=10.0) == float("inf")   # written before a reboot


def test_state_roundtrip(tmp_path):
    control.write_state(tmp_path, {"pid": 1, "awake_s": 2.5})
    assert control.read_state(tmp_path) == {"pid": 1, "awake_s": 2.5}
    (control.run_dir(tmp_path) / control.STATE).write_text("{broken", encoding="utf-8")
    assert control.read_state(tmp_path) is None


def test_stop_when_nothing_runs_pauses_autostart(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(control, "instance_running", lambda name: False)
    monkeypatch.setattr(procs, "running_supervisors", lambda older_s=None: {})
    assert control.stop(tmp_path, timeout=0) == 0
    assert (control.run_dir(tmp_path) / control.HOLD).exists() and (tmp_path / "STOP_ALL").exists()
    assert "not running" in capsys.readouterr().out


def test_autostart_respects_a_manual_stop_and_a_running_supervisor(tmp_path, monkeypatch):
    s = load_settings()
    monkeypatch.setattr(control, "spawn_outside", lambda *a, **k: pytest.fail("must not start"))
    control.run_dir(tmp_path).mkdir(parents=True)
    (control.run_dir(tmp_path) / control.HOLD).write_text("1", encoding="utf-8")
    assert control.detach(["all"], tmp_path, s, auto=True) == 0          # held by the user
    (control.run_dir(tmp_path) / control.HOLD).unlink()
    monkeypatch.setattr(control, "instance_running", lambda name: True)
    control.write_state(tmp_path, {"pid": os.getpid(), "awake_s": winops.sample_clock().awake})
    assert control.detach(["all"], tmp_path, s, auto=True) == 0          # healthy one already running
    monkeypatch.setattr(control, "instance_running", lambda name: False)
    monkeypatch.setattr(procs, "running_supervisors", lambda older_s=None: {99: None})
    assert control.detach(["all"], tmp_path, s, auto=False) == 1         # older unlocked build still running


def test_console_python_prefers_console_interpreter(monkeypatch, tmp_path):
    (tmp_path / "python.exe").write_bytes(b"")
    monkeypatch.setattr(sys, "executable", str(tmp_path / "pythonw.exe"))
    assert control.console_python() == str(tmp_path / "python.exe")


# --------------------------------------------------------------------------- logging of crashes (F8/OPS-06)
def test_uncaught_exceptions_are_logged_redacted_and_noted_on_stderr(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    secret = "sk-ant-" + "x" * 24
    log = setup_logging("crashy", logs_dir=tmp_path, console=False)
    try:
        raise RuntimeError(f"bad key {secret}")
    except RuntimeError:
        sys.excepthook(*sys.exc_info())
    t = threading.Thread(target=lambda: 1 / 0, name="worker")
    t.start()
    t.join()
    for hd in logging.getLogger().handlers:
        hd.flush()
    lines = [json.loads(x) for x in (tmp_path / "crashy.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [x["msg"] for x in lines] == ["uncaught RuntimeError", "uncaught ZeroDivisionError in thread worker"]
    assert "RuntimeError" in lines[0]["exc"] and secret not in json.dumps(lines)
    err = capsys.readouterr().err
    assert "uncaught RuntimeError: bad key" in err and "in thread worker" in err and secret not in err
    assert log.name == "crashy"


def test_supervised_children_do_not_log_to_the_console(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    s = load_settings()
    s = s.model_copy(update={"paths": s.paths.model_copy(update={"logs_dir": str(tmp_path)}),
                             "logging": s.logging.model_copy(update={"console": True})})
    monkeypatch.setenv("TS_LOG_CONSOLE", "0")
    setup_from_settings("child", s)
    assert not [hd for hd in logging.getLogger().handlers if type(hd) is logging.StreamHandler]
    monkeypatch.delenv("TS_LOG_CONSOLE")
    setup_from_settings("child", s)
    assert [hd for hd in logging.getLogger().handlers if type(hd) is logging.StreamHandler]
