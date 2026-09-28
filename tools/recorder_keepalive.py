"""Keep the P1.12 price recorder running (Phase 5 A3): ``pythonw tools/recorder_keepalive.py [--quiet] [--dry-run]``.

Task Scheduler runs it every 5 min (``TradingSystemOps-Recorder``, ``scripts\\install_operator_tasks.ps1`` — not an
``install_autostart.ps1`` task: that script deletes every ``TradingSystem-*`` task it does not know as a pair). The
recorder (``research/price_matching/recorder.py``, started by hand with ``scripts\\start_recorder.bat``) dies at every
suspend and shutdown and has no autostart of its own. Each run, in this order:

1. ``data/research/price_matching/STOP`` exists → nothing: the owner stopped it (``start_recorder.bat`` removes the
   file and starts it again). This tool never removes the file.
2. The MT5 terminal the recorder attaches to (its ``MT5_PATH``) is not running → nothing: the recorder's MT5 poller
   would start the terminal as its own child through ``mt5.initialize(path)``, and our processes never start MT5
   (docs/ops_windows.md §5) — the ``TradingSystem-MT5`` task or a supervisor does.
3. A recorder runs → nothing (never two recorders). Alive = a process whose command line runs
   ``research/price_matching/recorder.py`` (any checkout, relative or absolute path). ``status.json`` alone cannot
   tell: a new recorder writes it only at its first flush (300 s), so the pid in it is checked first and then every
   python process. A live recorder whose last flush is older than 15 min is logged as a warning, not replaced.
4. Crash-loop guard: this tool started it 3 times within 60 min and no flush happened since the first of them → no
   start (logged once as an error; the guard lifts as the window moves on — at most 3 attempts an hour).
5. Otherwise it starts ``pythonw research\\price_matching\\recorder.py --flush-s 300 --mt5-interval-ms 50`` (the
   arguments of ``start_recorder.bat``) in the project folder, detached and outside the task's job
   (``winops.spawn_outside``, as the monitor starts its diagnosis sessions), and checks that it still runs a few
   seconds later.

State: ``data/research/price_matching/keepalive.json`` (last check, its outcome, the recent starts). Log:
``logs/recorder-keepalive.jsonl`` (starts, problems and changes of the outcome; a run that finds the recorder alive
again writes no line). ``--dry-run`` says what it would do and writes nothing. Exit code: 0 running / started /
nothing to do (also stopped, no MT5, stale, crash loop); 1 a start failed or the check itself failed (the task's
"Last Result").
"""
# No ``from __future__ import annotations``: tools are loaded by file path without a sys.modules entry (tests).
import argparse
import ast
import datetime as dt
import json
import logging
import os
import sys
import time
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tradingsystem.core.filelock import FileLock  # noqa: E402
from tradingsystem.core.logsetup import setup_logging  # noqa: E402
from tradingsystem.core.timeutil import MS_PER_MINUTE, iso, now_ms, to_ms  # noqa: E402
from tradingsystem.supervisor import procs, winops  # noqa: E402

log = logging.getLogger("recorder-keepalive")

RECORDER = ROOT / "research" / "price_matching" / "recorder.py"
OUT = ROOT / "data" / "research" / "price_matching"          # = recorder.OUT (pinned by a test)
RECORDER_ARGS = ["--flush-s", "300", "--mt5-interval-ms", "50"]  # = scripts\start_recorder.bat (pinned by a test)
DEFAULT_MT5_PATH = r"C:/Program Files/MetaTrader 5/terminal64.exe"
STATE_FILE = "keepalive.json"
LOCK_FILE = "keepalive.lock"
STALE_MS = 15 * MS_PER_MINUTE               # a live recorder without a flush for this long → warning
LOOP_STARTS, LOOP_WINDOW_MS = 3, 60 * MS_PER_MINUTE
START_CHECK_S = 8.0                         # a recorder that exits within this is a failed start
EXIT_OK, EXIT_FAILED = 0, 1


def recorder_constant(name: str, default: str) -> str:
    """A string constant of recorder.py read from its source (importing it would load pyarrow for nothing)."""
    try:
        tree = ast.parse(RECORDER.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError):
        return default
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets) \
                and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            return node.value.value
    return default


def mt5_running(path: str) -> bool:
    return bool(procs.find_terminals([path]).get(path))


def is_recorder_cmd(cmd: list[str]) -> bool:
    """A command line that runs the recorder script (``research\\price_matching\\recorder.py``, relative or absolute)."""
    return any(a.replace("\\", "/").lower().endswith("price_matching/recorder.py") for a in cmd[1:])


def recorder_pids(status_pid: int | None = None) -> list[int]:
    """Running recorders (any checkout), the status file's pid first. A venv's python.exe/pythonw.exe is a launcher
    that runs the real interpreter as its child with the same command line: both count (one recorder)."""
    found: list[int] = []
    if status_pid:
        try:
            p = psutil.Process(int(status_pid))
            if (p.name() or "").lower().startswith("python"):
                try:
                    if is_recorder_cmd(p.cmdline()):
                        return [p.pid]
                except psutil.AccessDenied:
                    return [p.pid]          # a python process we may not inspect: assume it is the recorder
        except (psutil.Error, ValueError, TypeError):
            pass
    me = os.getpid()
    for p in psutil.process_iter(["name"]):
        if p.pid == me or not (p.info.get("name") or "").lower().startswith("python"):
            continue
        try:
            if is_recorder_cmd(p.cmdline()):
                found.append(p.pid)
        except (psutil.Error, OSError):
            continue
    return found


def read_json(path: Path) -> dict:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def write_state(path: Path, doc: dict) -> None:
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(doc, indent=1, sort_keys=True), encoding="utf-8")
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:             # a reader holds it open (Windows) — retry briefly
                if attempt == 4:
                    raise
                time.sleep(0.1)
    except OSError as exc:
        log.warning("could not write %s: %s", path, exc)
        tmp.unlink(missing_ok=True)


def _ms(value) -> int | None:
    """``status.json``'s ``updated`` (``…Z``, written by ``timeutil.iso``) as UTC ms; None when absent or naive."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return to_ms(dt.datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def pythonw() -> str:
    """The windowless interpreter of this checkout's venv (the task's own), else this one's sibling."""
    venv = ROOT / ".venv" / "Scripts" / "pythonw.exe"
    if venv.exists():
        return str(venv)
    alt = Path(sys.executable).with_name("pythonw.exe")
    return str(alt) if alt.exists() else sys.executable


def spawn(cmd: list[str]):
    """Detached, outside Task Scheduler's job (breakaway, with a fallback) — the job ends with this run. pythonw is a
    GUI-subsystem program: no console of its own (DETACHED_PROCESS)."""
    return winops.spawn_outside(cmd, cwd=ROOT, console=False)


def check(*, dry_run: bool = False, now: int | None = None) -> tuple[str, str]:
    """One keep-alive decision (and the start). Returns ``(outcome, detail)``; outcome in stopped | no_mt5 | alive |
    stale | crash_loop | started | start_failed | would_start (``--dry-run``)."""
    now = now_ms() if now is None else now
    status = read_json(OUT / "status.json")
    updated = _ms(status.get("updated"))
    if (OUT / "STOP").exists():
        return "stopped", f"{OUT / 'STOP'} exists (scripts\\start_recorder.bat removes it and starts the recorder)"
    mt5_path = recorder_constant("MT5_PATH", DEFAULT_MT5_PATH)
    if not mt5_running(mt5_path):
        return "no_mt5", f"the MT5 terminal is not running ({mt5_path}) - the recorder is not started without it"
    pids = recorder_pids(status.get("pid"))
    if pids:
        age_ms = None if updated is None else now - updated
        detail = f"pid {', '.join(map(str, pids))}" + ("" if age_ms is None else
                                                        f", last flush {age_ms / MS_PER_MINUTE:.0f} min ago")
        if age_ms is not None and age_ms > STALE_MS and status.get("pid") in pids:
            # the recorder that wrote the status is still there but no longer flushes: hung — never a second one
            return "stale", (detail + " - alive but not flushing; not replaced (never two recorders): create "
                             f"{OUT / 'STOP'}, wait for it to exit, then scripts\\start_recorder.bat")
        return "alive", detail
    state = read_json(OUT / STATE_FILE)
    starts = [int(t) for t in state.get("starts") or [] if isinstance(t, (int, float)) and now - t < LOOP_WINDOW_MS]
    if len(starts) >= LOOP_STARTS and (updated is None or updated < min(starts)):
        return "crash_loop", (f"{len(starts)} starts in {LOOP_WINDOW_MS // MS_PER_MINUTE} min without a flush - not "
                              "starting again (see logs\\recorder.jsonl and logs\\recorder-mt5.jsonl)")
    cmd = [pythonw(), str(RECORDER), *RECORDER_ARGS]
    if dry_run:
        return "would_start", " ".join(cmd)
    try:
        p = spawn(cmd)
    except OSError as exc:
        return "start_failed", f"could not start {cmd[0]}: {exc}"
    deadline = time.monotonic() + START_CHECK_S
    while time.monotonic() < deadline:
        code = p.poll()
        if code is not None:
            return "start_failed", f"the recorder exited at once (code {code}) - see logs\\recorder.jsonl"
        time.sleep(0.5)
    return "started", f"pid {p.pid}: {' '.join(cmd)}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Start the P1.12 price recorder when it should run and does not "
                                             "(Task Scheduler, every 5 min).")
    ap.add_argument("--quiet", action="store_true", help="no console output (the scheduled task, pythonw)")
    ap.add_argument("--dry-run", action="store_true", help="say what would be done; start nothing, write nothing")
    a = ap.parse_args(argv)
    say = (lambda *_a, **_k: None) if a.quiet else print
    if a.dry_run:
        outcome, detail = check(dry_run=True)
        say(f"recorder: {outcome} - {detail}")
        return EXIT_OK
    setup_logging("recorder-keepalive", logs_dir=ROOT / "logs", console=False)
    OUT.mkdir(parents=True, exist_ok=True)
    lock = FileLock(OUT / LOCK_FILE)
    if not lock.acquire(0):
        say("another keep-alive run is checking the recorder - nothing done")
        return EXIT_OK
    try:
        now = now_ms()
        state = read_json(OUT / STATE_FILE)
        try:
            outcome, detail = check(now=now)
        except Exception as exc:  # noqa: BLE001 — a scheduled task: log it, exit 1, never a traceback on no console
            log.exception("keep-alive check failed")
            outcome, detail = "error", f"{type(exc).__name__}: {exc}"
        say(f"recorder: {outcome} - {detail}")
        starts = [int(t) for t in state.get("starts") or []
                  if isinstance(t, (int, float)) and now - t < LOOP_WINDOW_MS]
        if outcome in ("started", "start_failed"):
            starts.append(now)                               # every attempt counts for the crash-loop guard
        if outcome == "started":
            log.info("recorder started: %s", detail)
        elif outcome in ("start_failed", "error"):
            log.error("recorder %s: %s", outcome, detail)
        elif outcome != state.get("last_outcome"):  # a state that persists (stale, crash loop, stopped): once per change
            level = {"crash_loop": logging.ERROR, "stale": logging.WARNING}.get(outcome, logging.INFO)
            log.log(level, "recorder %s: %s", outcome, detail)
        write_state(OUT / STATE_FILE, {"last_check": iso(now), "last_outcome": outcome, "last_detail": detail,
                                       "starts": starts[-10:]})
        return EXIT_FAILED if outcome in ("start_failed", "error") else EXIT_OK
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
