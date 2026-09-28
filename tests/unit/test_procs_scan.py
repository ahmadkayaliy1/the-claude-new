"""supervisor/procs.running_supervisors: only the process name is prefetched for every process (asking psutil for every
ppid made the scan take ≈ 3.6 s on Windows); the parent is read for supervisor matches only, and a venv launcher with
its interpreter child still counts as one supervisor (Phase 5 A7)."""
from __future__ import annotations

from tradingsystem.supervisor import procs


class FakeProc:
    def __init__(self, pid, name, cmd, ppid, created=1.0):
        self.pid, self.info, self._cmd, self._ppid, self._created = pid, {"name": name}, cmd, ppid, created
        self.ppid_calls = 0

    def cmdline(self):
        return self._cmd

    def ppid(self):
        self.ppid_calls += 1
        return self._ppid

    def create_time(self):
        return self._created


def test_the_scan_prefetches_names_only_and_reads_the_parent_of_supervisors_only(monkeypatch):
    sup = ["C:/x/.venv/Scripts/python.exe", "-m", "tradingsystem", "--instance", "BTCUSDT", "run", "all"]
    launcher = FakeProc(100, "python.exe", sup, 1)
    child = FakeProc(101, "python.exe", sup, 100)             # the venv launcher's real interpreter
    other = FakeProc(200, "python.exe", ["python", "tools/monitor.py"], 1)
    chrome = FakeProc(300, "chrome.exe", ["chrome"], 1)
    asked = []

    def process_iter(attrs=None):
        asked.append(attrs)
        return iter([launcher, child, other, chrome])

    monkeypatch.setattr(procs.psutil, "process_iter", process_iter)
    monkeypatch.setattr(procs, "_proc_instance", lambda p, cmd: "BTCUSDT")
    found = procs.running_supervisors()
    assert asked == [["name"]]                                # never "ppid" for every process
    assert found == {100: "BTCUSDT"}                          # the outermost of launcher + child
    assert launcher.ppid_calls == 1 and child.ppid_calls == 1
    assert other.ppid_calls == 0 and chrome.ppid_calls == 0
