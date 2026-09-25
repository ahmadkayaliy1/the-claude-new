"""Windows process / power / clock helpers for the supervisor (portable fallbacks elsewhere).

* :func:`sample_clock` / :func:`time_gap` — tell a PC sleep or a wall-clock step from a hung child: the
  unbiased interrupt time stops while the machine sleeps, the tick count and the wall clock do not (F2/OPS-05).
* :func:`keep_awake` — block *idle* sleep while the supervisor runs (lid/buttons stay a Windows setting).
* :func:`acquire_instance` — named mutex, one supervisor per data dir (OPS-03).
* :func:`spawn_outside` — start a process outside our job(s) (MT5 terminal, detached supervisor; OPS-04).
* :class:`JobObject` — kill-on-close job for the children (no orphans).
"""
from __future__ import annotations

import ctypes
import hashlib
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO

WIN = os.name == "nt"
ERROR_ACCESS_DENIED, ERROR_ALREADY_EXISTS = 5, 183
WAIT_OBJECT_0, WAIT_ABANDONED = 0x0, 0x80
SYNCHRONIZE = 0x00100000
PROCESS_QUERY_LIMITED_INFORMATION, PROCESS_SET_QUOTA, PROCESS_TERMINATE = 0x1000, 0x0100, 0x0001
ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
DETACHED_PROCESS, CREATE_NEW_PROCESS_GROUP, NORMAL_PRIORITY_CLASS = 0x8, 0x200, 0x20
CREATE_BREAKAWAY_FROM_JOB, CREATE_NO_WINDOW = 0x01000000, 0x08000000
JOB_KILL_ON_CLOSE = 0x2000

if WIN:
    from ctypes import wintypes

    # own WinDLL instance: typed prototypes without touching ctypes.windll.kernel32 used elsewhere
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _H, _D, _B = wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL
    for _name, _res, _args in (
        ("CreateMutexW", _H, [ctypes.c_void_p, _B, wintypes.LPCWSTR]),
        ("OpenMutexW", _H, [_D, _B, wintypes.LPCWSTR]),
        ("ReleaseMutex", _B, [_H]),
        ("WaitForSingleObject", _D, [_H, _D]),
        ("CloseHandle", _B, [_H]),
        ("OpenProcess", _H, [_D, _B, _D]),
        ("IsProcessInJob", _B, [_H, _H, ctypes.POINTER(_B)]),
        ("QueryUnbiasedInterruptTime", _B, [ctypes.POINTER(ctypes.c_ulonglong)]),
        ("GetTickCount64", ctypes.c_ulonglong, []),
        ("SetThreadExecutionState", _D, [_D]),
        ("CreateJobObjectW", _H, [ctypes.c_void_p, wintypes.LPCWSTR]),
        ("SetInformationJobObject", _B, [_H, ctypes.c_int, ctypes.c_void_p, _D]),
        ("QueryInformationJobObject", _B, [_H, ctypes.c_int, ctypes.c_void_p, _D, ctypes.c_void_p]),
        ("AssignProcessToJobObject", _B, [_H, _H]),
    ):
        _f = getattr(k32, _name)
        _f.restype, _f.argtypes = _res, _args


# --------------------------------------------------------------------------- clocks
@dataclass(frozen=True)
class ClockSample:
    wall: float     # time.time()
    tick: float     # s since boot INCLUDING time asleep (GetTickCount64 / CLOCK_BOOTTIME)
    awake: float    # s since boot EXCLUDING time asleep (QueryUnbiasedInterruptTime / CLOCK_MONOTONIC); system-wide


def sample_clock() -> ClockSample:
    wall = time.time()
    if WIN:
        u = ctypes.c_ulonglong()
        k32.QueryUnbiasedInterruptTime(ctypes.byref(u))
        return ClockSample(wall, k32.GetTickCount64() / 1000.0, u.value / 1e7)
    mono = time.monotonic()
    boot = time.clock_gettime(time.CLOCK_BOOTTIME) if hasattr(time, "CLOCK_BOOTTIME") else mono
    return ClockSample(wall, boot, mono)


def time_gap(a: ClockSample, b: ClockSample, expected_s: float, *, suspend_s: float = 5.0, jump_s: float = 10.0,
             stall_s: float = 15.0) -> tuple[str, float] | None:
    """Classify an interval that should have lasted ``expected_s``.

    ``("suspend", s asleep)`` | ``("clock_jump", signed wall step s)`` | ``("stall", awake s)`` | None (normal).
    """
    tick, awake, wall = b.tick - a.tick, b.awake - a.awake, b.wall - a.wall
    if tick - awake > suspend_s:
        return "suspend", tick - awake
    if abs(wall - tick) > jump_s:
        return "clock_jump", wall - tick
    if awake - expected_s > stall_s:
        return "stall", awake
    return None


# --------------------------------------------------------------------------- power
def keep_awake(on: bool) -> bool:
    """ES_SYSTEM_REQUIRED on the calling thread: blocks idle sleep only (not lid / power button / critical battery)."""
    if not WIN:
        return False
    return bool(k32.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0)))


# --------------------------------------------------------------------------- single instance
_HELD: dict[str, object] = {}


def instance_name(kind: str, data_dir: Path | str) -> str:
    key = hashlib.sha1(os.path.normcase(str(Path(data_dir).resolve())).encode()).hexdigest()[:12]
    return f"TradingSystem.{kind}.{key}"


def acquire_instance(name: str, wait_s: float = 0.0) -> bool:
    """Hold a machine-wide lock for the life of this process; False if another process holds it.

    Windows: named mutex (released by the OS when the holder dies → ``WAIT_ABANDONED`` for the next one).
    ``wait_s`` lets a restart wait for a predecessor that is still shutting down.
    """
    if name in _HELD:
        return True
    if WIN:
        h = k32.CreateMutexW(None, True, "Global\\" + name)
        err = ctypes.get_last_error()
        if not h:                       # ERROR_ACCESS_DENIED: held by another user / elevation level
            return False
        if err == ERROR_ALREADY_EXISTS and k32.WaitForSingleObject(h, int(wait_s * 1000)) not in (
                WAIT_OBJECT_0, WAIT_ABANDONED):
            k32.CloseHandle(h)
            return False
        _HELD[name] = h
        return True
    import fcntl
    import tempfile

    fh = open(Path(tempfile.gettempdir()) / f"{name}.lock", "a+")  # noqa: SIM115 (held for the process life)
    deadline = time.monotonic() + wait_s
    while True:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            _HELD[name] = fh
            return True
        except OSError:
            if time.monotonic() >= deadline:
                fh.close()
                return False
            time.sleep(0.2)


def release_instance(name: str) -> None:
    h = _HELD.pop(name, None)
    if h is None:
        return
    if WIN:
        k32.ReleaseMutex(h)
        k32.CloseHandle(h)
    else:
        h.close()


def instance_running(name: str) -> bool:
    """True while some process holds (or has open) the lock ``name`` — including this one."""
    if name in _HELD:
        return True
    if WIN:
        h = k32.OpenMutexW(SYNCHRONIZE, False, "Global\\" + name)
        if h:
            k32.CloseHandle(h)
            return True
        return ctypes.get_last_error() == ERROR_ACCESS_DENIED
    if acquire_instance(name):
        release_instance(name)
        return False
    return True


# --------------------------------------------------------------------------- processes
def spawn_outside(cmd: list[str], *, cwd: str | Path, console: bool, env: dict[str, str] | None = None,
                  stdout: IO[bytes] | None = None) -> subprocess.Popen:
    """Start ``cmd`` outside every job we are in (where the job allows breakaway) and outside our console.

    ``console=True`` gives a console app its own *windowless* console (CTRL_BREAK still works inside it);
    ``console=False`` is for GUI apps (MT5 terminal). Never assigned to the supervisor's job.
    """
    kw: dict = dict(cwd=str(cwd), env=env, stdin=subprocess.DEVNULL, stdout=stdout or subprocess.DEVNULL,
                    stderr=subprocess.STDOUT if stdout else subprocess.DEVNULL, close_fds=True)
    if not WIN:
        return subprocess.Popen(cmd, start_new_session=True, **kw)
    flags = CREATE_NEW_PROCESS_GROUP | NORMAL_PRIORITY_CLASS | (CREATE_NO_WINDOW if console else DETACHED_PROCESS)
    try:
        return subprocess.Popen(cmd, creationflags=flags | CREATE_BREAKAWAY_FROM_JOB, **kw)
    except OSError:                     # our job forbids breakaway (ERROR_ACCESS_DENIED) — still leave our console
        return subprocess.Popen(cmd, creationflags=flags, **kw)


def in_job(pid: int, job: int | None = None) -> bool | None:
    """Is ``pid`` in ``job`` (None = in any job)? None when it cannot be checked."""
    if not WIN:
        return None
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        res = wintypes.BOOL()
        return bool(res.value) if k32.IsProcessInJob(h, job, ctypes.byref(res)) else None
    finally:
        k32.CloseHandle(h)


class JobObject:
    """Windows job object: children die with the supervisor (no orphans)."""

    def __init__(self) -> None:
        self.handle = None
        if not WIN:
            return
        self.handle = k32.CreateJobObjectW(None, None)
        self._set_flags(JOB_KILL_ON_CLOSE)

    def _set_flags(self, flags: int) -> bool:
        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in ("r", "w", "o", "rb", "wb", "ob")]

        class BASIC(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", ctypes.c_uint32), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", ctypes.c_uint32),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", ctypes.c_uint32),
                        ("SchedulingClass", ctypes.c_uint32)]

        class EXTENDED(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO_COUNTERS),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        info = EXTENDED()
        info.BasicLimitInformation.LimitFlags = flags
        return bool(k32.SetInformationJobObject(self.handle, 9, ctypes.byref(info), ctypes.sizeof(info)))

    def add(self, pid: int) -> None:
        if self.handle is None:
            return
        h = k32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
        if h:
            k32.AssignProcessToJobObject(self.handle, h)
            k32.CloseHandle(h)

    def pids(self) -> list[int]:
        """Processes currently in the job (JobObjectBasicProcessIdList)."""
        if self.handle is None:
            return []

        class PIDS(ctypes.Structure):
            _fields_ = [("assigned", ctypes.c_uint32), ("listed", ctypes.c_uint32), ("ids", ctypes.c_size_t * 1024)]

        buf = PIDS()
        if not k32.QueryInformationJobObject(self.handle, 3, ctypes.byref(buf), ctypes.sizeof(buf), None):
            return []
        return [int(buf.ids[i]) for i in range(buf.listed)]

    def keep_members_on_close(self) -> bool:
        """Drop KILL_ON_JOB_CLOSE (used only after every other member was killed, to spare an MT5 terminal)."""
        return self.handle is not None and self._set_flags(0)
