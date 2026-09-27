"""One Claude operator session (§3.8, docs/operator_sessions.md): daily review, weekly review or diagnosis.

    python tools/operator/run_session.py --kind daily|weekly|diagnose [--dry-run] [--reason TEXT]
    Task Scheduler: powershell -File tools\\operator\\run_session.ps1 -Kind daily      (a thin launcher of this file)
    tools/monitor.py: [python, tools/operator/run_session.py, "--kind", "diagnose"]   (detached)

Steps (Python, not PowerShell: PS 5.1 drops empty arguments, pipes stdin as ASCII and decodes native stdout with the
OEM code page):

1. one session at a time on the machine (``data/shared/locks/operator_session.lock``; a review waits up to 5 min for a
   running diagnosis, a diagnosis never waits); ``operator.enabled: false`` → nothing runs;
2. the review pack (``tools/review_pack.py``, imported) → ``data/reviews/<ts>_<kind>.md|.json``; with the usage gauge
   enforcing and at level 2 a daily/weekly review is skipped (a diagnosis still runs: it is event-like);
3. the prompt = the kind's checklist (``prompts/<kind>.md``) + the pack, written to ``<ts>_<kind>.prompt.md`` and fed
   to the CLI's stdin from that file; the command line and environment come from ``session_args.py``;
4. the machine-wide CLI start stagger (``claude_code.claim_start``: the engines' OAuth refresh race) before
   ``claude auth status`` (only a subscription sign-in is accepted, D-030) and again before ``claude -p``;
5. ``claude -p`` with the working directory = this checkout, stdout/stderr to files under ``data/reviews/`` (never a
   pipe nobody reads), a time limit that ends the whole run inside ``operator.<kind>_timeout_min`` (the process tree is
   killed: the model's tool children too);
6. the result document: ``success`` and ``error_max_turns`` are both a finished session (the second has no summary);
   usage, turns, API-equivalent cost and permission denials are kept; one ledger row (provider claude_code, role
   review|diagnose, pair NULL, cost 0 — the API-equivalent cost goes to ``api_equivalent_usd``) when
   ``operator.record_usage``. Without a result document (the time limit killed the CLI, which prints it only at the
   end, or the CLI crashed) the spend is unknown, not zero: the row's ``error`` starts with ``usage_unknown: `` (with
   the elapsed seconds) and the review pack counts such sessions;
7. the diff guard: ``git status --porcelain`` (+ content hashes of the files already dirty) before and after —
   any difference notifies ``review_touched_checkout``; nothing is ever reverted;
8. ``<ts>_<kind>.session.json`` (written at the start as ``running`` and at the end), and the final summary (the
   prompt asks the model to end with it; ≤ ``operator.summary_max_chars``) sent through ``core.notify``.

``--dry-run``: builds the pack and the prompt, prints the exact command, working directory and environment changes;
no sign-in check, no CLI, no ledger row, no notification, no session file.
Exit codes: 0 finished (summary or turn limit) or dry run, 1 failed (CLI missing, not signed in, error, timeout),
2 not run (disabled, another session running, usage gauge), 3 the config cannot be read (``load_settings`` failed,
e.g. a bad ``config.local.yaml``): there are no Settings, so no normal log, notifier or data root — one timestamped
line goes to ``logs/operator-session-config-error.log`` of this checkout, a best-effort critical toast is shown
(``notify.ps1``, 15 s at most; skipped under ``TS_NOTIFY_DISABLE``) and the error is printed to stderr; nothing runs.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import logging
import os
import re
import string
import subprocess
import sys
import tempfile
import time
import types
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tradingsystem.ai.providers import claude_code as cc  # noqa: E402
from tradingsystem.core.filelock import FileLock, locks_dir  # noqa: E402
from tradingsystem.core.logsetup import get_redactor, setup_logging  # noqa: E402
from tradingsystem.core.settings import Settings, load_settings  # noqa: E402
from tradingsystem.core.timeutil import iso  # noqa: E402
from tradingsystem.core.timeutil import now_ms as _now_ms  # noqa: E402

log = logging.getLogger("operator")

EXIT_OK, EXIT_FAILED, EXIT_SKIPPED, EXIT_CONFIG = 0, 1, 2, 3
CONFIG_ERROR_LOG = ROOT / "logs" / "operator-session-config-error.log"   # fixed: without Settings, no logs dir
CONFIG_ERROR_LOG_MAX = 1_000_000               # bytes; then it is rotated once (.1)
CONFIG_TOAST_TIMEOUT_S = 15.0
FINISHED = ("ok", "max_turns")                  # both are a normal end of a session
SKIPPED = ("disabled", "busy", "gauge_paused")
KILL_MARGIN_S = 90.0             # the run ends this long before operator.<kind>_timeout_min — the governing deadline;
                                 # the task's ExecutionTimeLimit (130/190 min) is only a backstop beyond every config value
MIN_CLI_S = 120.0                # less time than this left for the CLI → do not start it
BUSY_WAIT_S = {"daily": 300.0, "weekly": 300.0, "diagnose": 0.0}
STAGGER_MAX_WAIT_S = 180.0
MAX_REASON_CHARS = 2000
MAX_RESULT_CHARS = 20_000
HASH_MAX_BYTES = 5_000_000
LOCK_NAME = "operator_session.lock"
TITLES = {"daily": "Daily review", "weekly": "Weekly review", "diagnose": "Diagnosis"}
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
SUMMARY_HEAD = re.compile(r"^[ \t]*#{1,6}[ \t]*SUMMARY[ \t]*:?[ \t]*$", re.I | re.M)
LEVEL_LINE = re.compile(r"^\s*[*_]*level[*_]*\s*:\s*[*_]*\s*(info|warn|warning|critical)\b", re.I)


# --------------------------------------------------------------------------- sibling modules (loaded by path)
def _load(name: str, path: Path) -> types.ModuleType:
    mod = sys.modules.get(name)
    if mod is None:
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return mod


sa = _load("ts_operator_session_args", HERE / "session_args.py")


def pack_module() -> types.ModuleType:
    return _load("ts_tools_review_pack", ROOT / "tools" / "review_pack.py")


# --------------------------------------------------------------------------- process helpers (patched in tests)
def setup_log(s: Settings) -> None:
    setup_logging("operator-session", logs_dir=s.paths.logs(), console=sys.stderr is not None,
                  secret_env_names=s.secret_env_names())


def wait_for_start(workdir: Path, max_wait_s: float = STAGGER_MAX_WAIT_S) -> bool:
    """``claim_start`` until granted: at least 15 s between two CLI starts on this machine (every engine shares the
    stamp — two CLIs refreshing the OAuth token together lose the sign-in race)."""
    workdir.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + max_wait_s
    while True:
        try:
            wait = cc.claim_start(workdir)
        except OSError:
            wait = 1.0
        if wait <= 0:
            return True
        if time.monotonic() + min(wait, cc.START_STAGGER_S) > deadline:
            return False
        time.sleep(min(wait, cc.START_STAGGER_S))


def check_auth(exe: str, env: dict[str, str], workdir: Path) -> str | None:
    """None when the CLI is signed in with the subscription, else the problem (an API-key sign-in is refused:
    D-030). Raises ``claude_code.AuthCheckFailed`` when the status command itself fails."""
    try:
        p = subprocess.run([exe, "auth", "status", "--json"], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=env, cwd=str(workdir), timeout=cc.AUTH_CHECK_TIMEOUT_S,
                           creationflags=_NO_WINDOW)
        st = json.loads(p.stdout or "{}")
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise cc.AuthCheckFailed(f"cannot read the Claude Code login status ({exc})") from exc
    return cc.auth_problem("operator session", st if isinstance(st, dict) else {})


def spawn_cli(args: list[str], *, cwd: Path, env: dict[str, str], stdin_path: Path, stdout_path: Path,
              stderr_path: Path, timeout_s: float) -> tuple[int | None, bool, float]:
    """Run the CLI with stdin/stdout/stderr on files. Returns (exit code, timed out, seconds). A timeout — or any
    interruption of this runner — kills the whole process tree (the model's Bash children included)."""
    from tradingsystem.supervisor.procs import kill_tree
    t0 = time.monotonic()
    with open(stdin_path, "rb") as fin, open(stdout_path, "wb") as fout, open(stderr_path, "wb") as ferr:
        proc = subprocess.Popen(args, stdin=fin, stdout=fout, stderr=ferr, cwd=str(cwd), env=env,
                                creationflags=_NO_WINDOW)
        try:
            rc = proc.wait(timeout=max(1.0, timeout_s))
            return rc, False, time.monotonic() - t0
        except subprocess.TimeoutExpired:
            kill_tree(proc.pid)
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                pass
            return proc.returncode, True, time.monotonic() - t0
        except BaseException:
            kill_tree(proc.pid)
            raise


def git_snapshot(root: Path) -> dict[str, str] | None:
    """``git status --porcelain`` of the checkout, each line with a hash of its file's content — a file that was
    already modified before the session and is modified again keeps the same status line. No optional locks (a
    concurrent ``git merge`` by the user never meets our index.lock). None when git fails."""
    try:
        p = subprocess.run(["git", "-C", str(root), "status", "--porcelain", "--untracked-files=all"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
                           env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"}, creationflags=_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return None
    if p.returncode != 0:
        return None
    out: dict[str, str] = {}
    for line in p.stdout.splitlines():
        if not line.strip():
            continue
        rel = line[3:].split(" -> ")[-1].strip().strip('"')
        out[line] = _file_hash(root / rel)
    return out


def _file_hash(path: Path) -> str:
    try:
        if not path.is_file():
            return "-"
        st = path.stat()
        if st.st_size > HASH_MAX_BYTES:
            return f"size {st.st_size} mtime {st.st_mtime_ns}"
        return hashlib.sha1(path.read_bytes()).hexdigest()[:16]
    except OSError:
        return "?"


def diff_guard(before: dict[str, str] | None, after: dict[str, str] | None) -> dict[str, Any]:
    """What the session changed in the checkout (status lines added/removed, dirty files whose content changed)."""
    if before is None or after is None:
        return {"clean": None, "error": "git status failed"}
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(k for k in set(before) & set(after) if before[k] != after[k])
    return {"clean": not (added or removed or changed), "added": added[:50], "removed": removed[:50],
            "content_changed": changed[:50], "before": len(before), "after": len(after)}


def send(s: Settings, level: str, title: str, text: str, *, key: str | None = None) -> None:
    """Through ``core.notify`` (log line + toast + Telegram); waits for the sinks before this process exits."""
    try:
        from tradingsystem.core import notify as nt
    except Exception:  # noqa: BLE001 — without the notifier the log line is all there is
        log.warning("notify unavailable — %s: %s", title, text[:300])
        return
    try:
        nt.notify(s, level, title, text, key=key)
        nt.flush(15.0)
    except Exception:  # noqa: BLE001
        log.exception("notify failed: %s", title)


# --------------------------------------------------------------------------- result document
def _classify(text: str) -> str:
    if cc._REFRESH_RACE_RE.search(text) or cc._AUTH_TRANSIENT_RE.search(text):
        return "sign_in_transient"
    if cc._LOGIN_RE.search(text):
        return "not_signed_in"
    if cc._LIMIT_RE.search(text):
        return "usage_limit"
    return "other"


def parse_result(stdout: str, stderr: str, rc: int | None, *, timed_out: bool = False,
                 default_model: str = "") -> dict[str, Any]:
    """The session's end from ``--output-format json``. ``success`` → ok; ``error_max_turns`` (``errors[]``, no
    ``result``) → max_turns — both normal ends; anything else → error (classified only from the error text and
    stderr, never from a successful answer that merely mentions a "usage limit"). Without a result document the
    token counts are 0 placeholders and ``usage_unknown`` is True (the CLI prints its usage only at the end)."""
    doc = cc.result_doc(stdout)
    out: dict[str, Any] = {"status": "error", "rc": rc, "timed_out": timed_out}
    if doc is None:
        out["status"] = "timeout" if timed_out else "error"
        out["error"] = ("no answer before the time limit" if timed_out
                        else cc.failure_text(stdout, stderr, rc))
        out["error_kind"] = "timeout" if timed_out else _classify(f"{out['error']}\n{stderr}")
        out["usage"] = {"input_tokens": 0, "cache_read": 0, "cache_creation": 0, "output_tokens": 0}
        out["ledger_tokens"] = {"input": 0, "cached": 0, "output": 0}
        out["usage_unknown"] = True
        out["model"] = default_model
        return out
    subtype, is_error = doc.get("subtype"), bool(doc.get("is_error"))
    errors = [str(e)[:300] for e in (doc.get("errors") or [])]
    if subtype == "success" and not is_error:
        out["status"] = "ok"
    elif subtype == "error_max_turns":
        out["status"] = "max_turns"
    else:
        out["error"] = str(doc.get("result") or "; ".join(errors) or subtype or "error")[:300]
        out["error_kind"] = _classify(f"{out['error']}\n{stderr}")
    u = doc.get("usage") or {}
    usage = {"input_tokens": int(u.get("input_tokens") or 0), "cache_read": int(u.get("cache_read_input_tokens") or 0),
             "cache_creation": int(u.get("cache_creation_input_tokens") or 0),
             "output_tokens": int(u.get("output_tokens") or 0)}
    mu = doc.get("modelUsage") or {}
    mu_tot = {"input": 0, "cached": 0, "output": 0}
    for m in mu.values():
        m = m or {}
        mu_tot["input"] += int(m.get("inputTokens") or 0) + int(m.get("cacheReadInputTokens") or 0) \
            + int(m.get("cacheCreationInputTokens") or 0)
        mu_tot["cached"] += int(m.get("cacheReadInputTokens") or 0)
        mu_tot["output"] += int(m.get("outputTokens") or 0)
    from_usage = usage["input_tokens"] + usage["cache_read"] + usage["cache_creation"]
    # the ledger convention (claude_code.parse): input = input + cache reads + cache writes; a multi-turn session's
    # `usage` and the per-model sums may differ (housekeeping calls) — the larger one is recorded
    out["ledger_tokens"] = {"input": max(from_usage, mu_tot["input"]),
                            "cached": max(usage["cache_read"], mu_tot["cached"]),
                            "output": max(usage["output_tokens"], mu_tot["output"])}
    denials = []
    for d in (doc.get("permission_denials") or [])[:20]:
        if isinstance(d, dict):
            denials.append({"tool": d.get("tool_name"),
                            "input": json.dumps(d.get("tool_input"), ensure_ascii=False, default=str)[:300]})
    out.update({
        "subtype": subtype, "is_error": is_error, "errors": errors, "num_turns": doc.get("num_turns"),
        "duration_ms": doc.get("duration_ms"), "total_cost_usd": doc.get("total_cost_usd"),
        "session_id": doc.get("session_id"), "stop_reason": doc.get("stop_reason"),
        "terminal_reason": doc.get("terminal_reason"), "permission_denials": denials, "usage": usage,
        "model_usage_totals": mu_tot, "model": cc._main_model(mu, default_model),
        "text": doc.get("result") if isinstance(doc.get("result"), str) else None,
    })
    return out


def extract_summary(text: str | None, max_chars: int) -> tuple[str | None, str]:
    """(summary, level) from the model's final message: the text after the LAST ``## SUMMARY`` heading, its first
    line ``level: info|warn|critical`` optional. Without the heading the end of the message stands in (level info)."""
    if not text or not text.strip():
        return None, "warn"
    heads = list(SUMMARY_HEAD.finditer(text))
    body = text[heads[-1].end():] if heads else text
    lines = body.strip().splitlines()
    level = "info"
    if lines:
        m = LEVEL_LINE.match(lines[0])
        if m:
            level = {"warning": "warn"}.get(m.group(1).lower(), m.group(1).lower())
            lines = lines[1:]
    summary = "\n".join(lines).strip()
    if not heads and len(summary) > max_chars:
        summary = "…" + summary[-(max_chars - 1):]
    if len(summary) > max_chars:
        summary = summary[:max_chars - 1] + "…"
    return summary or None, level


def record_usage(ledger: Path, spec: Any, parsed: dict[str, Any], *, purpose: str, elapsed_s: float) -> str | None:
    """One ledger row for the whole session (None = recorded, else why not). Provider = the claude_code provider:
    the subscription's shared limits count every call; ``cost_usd`` 0 (the plan, not a bill) — the API-equivalent
    price goes to ``api_equivalent_usd`` like every other claude_code row. A session without a result document
    (``usage_unknown``: time limit, crash) is not a cheap one: its ``error`` starts with the review pack's
    ``USAGE_UNKNOWN_PREFIX`` and gives the elapsed seconds, so its 0 tokens are never read as the real spend."""
    try:
        from tradingsystem.ai.budget import UsageStore
        from tradingsystem.ai.providers.base import LLMResult
        error = parsed.get("error")
        if parsed.get("usage_unknown"):
            error = (f"{pack_module().USAGE_UNKNOWN_PREFIX}no result document after {elapsed_s:.0f} s "
                     f"({parsed.get('status')}): {error or '-'}")
        t = parsed.get("ledger_tokens") or {}
        res = LLMResult(provider=spec.provider, model=parsed.get("model") or spec.model, text="", data=None,
                        input_tokens=int(t.get("input") or 0), output_tokens=int(t.get("output") or 0),
                        cached_input_tokens=int(t.get("cached") or 0), cost_usd=0.0,
                        latency_ms=int(parsed.get("duration_ms") or elapsed_s * 1000),
                        stop_reason=parsed.get("stop_reason"), request_id=parsed.get("session_id"),
                        extra={"api_equivalent_usd": float(parsed.get("total_cost_usd") or 0.0),
                               "num_turns": parsed.get("num_turns"),
                               "cache_creation_input_tokens": (parsed.get("usage") or {}).get("cache_creation")})
        ledger.parent.mkdir(parents=True, exist_ok=True)
        store = UsageStore(ledger)
        try:
            store.record(res, provider=spec.provider, model=res.model, purpose=purpose, pair=None,
                         ok=parsed.get("status") in FINISHED, error=_redact(error)[:600] if error else None,
                         role=spec.ledger_role)
        finally:
            store.close()
        return None
    except Exception as exc:  # noqa: BLE001 — the session happened; a ledger problem is reported, not fatal
        return f"{type(exc).__name__}: {exc}"[:200]


# --------------------------------------------------------------------------- prompt + files
def compose_prompt(spec: Any, *, review_id: str, pack: Any, summary_max_chars: int, now: int,
                   reason: str | None) -> str:
    """The kind's checklist with its placeholders filled (``$`` in the pack or the reason is never re-parsed) and the
    pack markdown appended — the pack is in the first message, no Read turn is spent on it."""
    kind_text = spec.kind_prompt.read_text(encoding="utf-8")
    vals = {"kind": spec.kind, "review_id": review_id, "hours": f"{spec.pack_hours:g}",
            "pairs": ", ".join(pack.data.get("pairs") or []) or "-", "pack_md": str(pack.md_path),
            "pack_json": str(pack.json_path), "summary_max_chars": str(summary_max_chars),
            "max_turns": str(spec.max_turns), "now_utc": iso(now), "data_root": str(spec.data_root),
            "reason": (reason or "(none given)")[:MAX_REASON_CHARS]}
    head = string.Template(kind_text).safe_substitute(vals)
    return f"{head.rstrip()}\n\n---\n\n{pack.md}"


def _write(path: Path, text: str) -> None:
    pack_module()._write_text(path, text)


def _redact(text: Any) -> Any:
    return get_redactor()(text) if isinstance(text, str) else text


# --------------------------------------------------------------------------- the run
def run(kind: str, *, s: Settings, dry_run: bool = False, reason: str | None = None, now: int | None = None,
        root: Path | None = None, out=print) -> int:
    """One session (see the module docstring). ``root``: the checkout (default: the one this file lives in)."""
    t_start = time.monotonic()
    kind = sa.check_kind(kind)
    spec = sa.spec_for(s, kind, root=root or sa.ROOT)
    now = _now_ms() if now is None else int(now)
    rp = pack_module()
    review_id = f"{rp.pack_stamp(now)}_{kind}"
    rdir = rp.reviews_dir(s)
    files = {"prompt": rdir / f"{review_id}.prompt.md", "stdout": rdir / f"{review_id}.cli.stdout.json",
             "stderr": rdir / f"{review_id}.cli.stderr.txt"}
    session_path = rdir / f"{review_id}.session.json"
    rec: dict[str, Any] = {"review_id": review_id, "kind": kind, "status": "running", "started": iso(now),
                           "provider": spec.provider, "model": spec.model, "effort": spec.effort,
                           "max_turns": spec.max_turns, "timeout_min": spec.timeout_min, "cwd": str(spec.root),
                           "reason": (reason or None) and reason[:MAX_REASON_CHARS],
                           "files": {k: str(v) for k, v in files.items()}}

    def finish(status: str, code: int, *, level: str | None = None, title: str | None = None,
               text: str | None = None) -> int:
        rec["status"] = status
        rec["ended"] = iso(_now_ms())
        rec["elapsed_s"] = round(time.monotonic() - t_start, 1)
        if not dry_run:
            try:
                rdir.mkdir(parents=True, exist_ok=True)
                _write(session_path, json.dumps(rec, ensure_ascii=False, indent=1, default=str))
            except OSError as exc:
                log.error("cannot write %s: %s", session_path, exc)
            if level and title:
                send(s, level, title, text or status, key=None)
        log.info("operator session %s: %s (%.0f s)", review_id, status, rec["elapsed_s"])
        out(f"{review_id}: {status}" + (f" — {rec.get('error')}" if rec.get("error") else ""))
        return code

    if not dry_run and not s.operator.enabled:
        return finish("disabled", EXIT_SKIPPED)
    lock = FileLock(locks_dir(s) / LOCK_NAME)
    if not dry_run and not lock.acquire(timeout=BUSY_WAIT_S[kind]):
        rec["error"] = "another operator session is running"
        return finish("busy", EXIT_SKIPPED, **({} if kind == "diagnose" else
                                               {"level": "warn", "title": f"{TITLES[kind]} skipped",
                                                "text": "another operator session was still running after 5 min"}))
    try:
        return _run_locked(kind, s=s, spec=spec, rp=rp, rec=rec, files=files, review_id=review_id, now=now,
                           dry_run=dry_run, reason=reason, finish=finish, t_start=t_start, out=out)
    finally:
        if lock.held:
            lock.release()


def _run_locked(kind: str, *, s: Settings, spec: Any, rp: types.ModuleType, rec: dict[str, Any],
                files: dict[str, Path], review_id: str, now: int, dry_run: bool, reason: str | None, finish,
                t_start: float, out) -> int:
    title = TITLES[kind]
    deadline = t_start + spec.timeout_min * 60 - KILL_MARGIN_S
    # ---- the pack
    try:
        pack = rp.build_pack(s, hours=spec.pack_hours, kind=kind, now=now, max_chars=spec.pack_max_chars)
    except Exception as exc:  # noqa: BLE001
        log.exception("review pack failed")
        rec["error"] = f"review pack failed: {type(exc).__name__}: {exc}"[:300]
        return finish("pack_failed", EXIT_FAILED, level="warn", title=f"{title} not run", text=rec["error"])
    rec["pack"] = {"md": str(pack.md_path), "json": str(pack.json_path), "chars": len(pack.md),
                   "health_problems": len(pack.problems), "ledger": str(pack.ledger)}
    gauge = (pack.data.get("usage") or {}).get("gauge") or {}
    rec["gauge"] = {k: v for k, v in gauge.items() if not isinstance(v, (dict, list))}
    if kind != "diagnose" and gauge.get("enforce") and int(gauge.get("level") or 0) >= 2:
        rec["error"] = "usage gauge at level 2 (events only)"
        return finish("gauge_paused", EXIT_SKIPPED, level="info", title=f"{title} skipped",
                      text=f"{rec['error']} — {gauge.get('reason') or ''}".strip(" —"))
    # ---- prompt, command line, environment
    prompt = compose_prompt(spec, review_id=review_id, pack=pack, summary_max_chars=s.operator.summary_max_chars,
                            now=now, reason=reason)
    files["prompt"].parent.mkdir(parents=True, exist_ok=True)
    _write(files["prompt"], prompt)
    env = sa.child_env(s)
    problems = []
    if not spec.exe:
        problems.append("Claude Code CLI not found (install it or set ai.providers.<claude_code>.cli_path)")
    venv_py = spec.root / ".venv" / "Scripts" / "python.exe"
    if not venv_py.exists():
        problems.append(f"{venv_py} missing — the allow-listed commands start with .venv/Scripts/python.exe")
    args = sa.build_args(spec) if spec.exe else None
    rec["args"] = args
    rec["env"] = sa.env_diff(dict(os.environ), env)
    if dry_run:
        before = git_snapshot(spec.root)
        out(json.dumps({"dry_run": True, "review_id": review_id, "cwd": str(spec.root), "args": args,
                        "stdin": str(files["prompt"]), "prompt_chars": len(prompt),
                        "pack": rec["pack"], "timeout_s": round(deadline - time.monotonic()),
                        "env": rec["env"], "problems": problems,
                        "diff_guard_baseline": None if before is None else len(before)},
                       indent=1, ensure_ascii=False))
        return EXIT_OK
    if problems:
        rec["error"] = "; ".join(problems)
        return finish("cli_missing" if not spec.exe else "venv_missing", EXIT_FAILED, level="warn",
                      title=f"{title} not run", text=rec["error"])
    _write(rp.reviews_dir(s) / f"{review_id}.session.json", json.dumps(rec, ensure_ascii=False, indent=1, default=str))
    before = git_snapshot(spec.root)
    # ---- sign-in (a start of its own: staggered like every CLI start)
    workdir = Path(tempfile.gettempdir()) / "tradingsystem-claude-code"
    if sa.TOKEN_ENV not in env:
        if not wait_for_start(workdir):
            rec["error"] = "the machine-wide CLI start stagger did not clear"
            return finish("busy", EXIT_SKIPPED)
        try:
            problem = check_auth(spec.exe, env, workdir)
        except cc.AuthCheckFailed as exc:
            problem = str(exc)
        if problem:
            rec["error"] = problem[:300]
            return finish("not_signed_in", EXIT_FAILED, level="warn", title=f"{title} not run", text=rec["error"])
    if not wait_for_start(workdir):
        rec["error"] = "the machine-wide CLI start stagger did not clear"
        return finish("busy", EXIT_SKIPPED)
    left = deadline - time.monotonic()
    if left < MIN_CLI_S:
        rec["error"] = f"only {left:.0f} s left of operator.{kind}_timeout_min — not started"
        return finish("no_time", EXIT_FAILED, level="warn", title=f"{title} not run", text=rec["error"])
    # ---- the session
    log.info("operator session %s: %s %s, max %d turns, %.0f s", review_id, spec.model, spec.effort or "-",
             spec.max_turns, left)
    rc, timed_out, elapsed = spawn_cli(args, cwd=spec.root, env=env, stdin_path=files["prompt"],
                                       stdout_path=files["stdout"], stderr_path=files["stderr"], timeout_s=left)
    stdout = files["stdout"].read_text(encoding="utf-8", errors="replace") if files["stdout"].exists() else ""
    stderr = files["stderr"].read_text(encoding="utf-8", errors="replace") if files["stderr"].exists() else ""
    parsed = parse_result(stdout, stderr, rc, timed_out=timed_out, default_model=spec.model)
    status = parsed["status"]
    text = _redact(parsed.pop("text", None))
    rec["cli"] = {"rc": rc, "timed_out": timed_out, "elapsed_s": round(elapsed, 1)}
    rec["result"] = {k: _redact(v) if isinstance(v, str) else v for k, v in parsed.items()
                     if k not in ("status", "error", "error_kind")}
    rec["result_text"] = text[:MAX_RESULT_CHARS] if text else None
    if parsed.get("error"):
        rec["error"], rec["error_kind"] = _redact(parsed["error"]), parsed.get("error_kind")
    # ---- ledger
    if s.operator.record_usage:
        why = record_usage(pack.ledger, spec, parsed, purpose=f"operator_{kind}", elapsed_s=elapsed)
        rec["ledger"] = {"path": str(pack.ledger), "recorded": why is None, **({"error": why} if why else {}),
                         **({"usage_unknown": True} if parsed.get("usage_unknown") else {})}
    # ---- diff guard
    guard = diff_guard(before, git_snapshot(spec.root))
    rec["diff_guard"] = guard
    if guard.get("clean") is False:
        send(s, "warn", "review_touched_checkout",
             f"{review_id} changed the checkout {spec.root} (not reverted): "
             + "; ".join((guard["added"] + guard["content_changed"] + guard["removed"])[:8])[:900],
             key="review_touched_checkout")
    # ---- summary
    usage_line = (f"({elapsed / 60:.1f} min, usage unknown: no result document)" if parsed.get("usage_unknown")
                  else f"({parsed.get('num_turns')} turns, in {parsed['ledger_tokens']['input']:,} / out "
                       f"{parsed['ledger_tokens']['output']:,} tokens, {elapsed / 60:.1f} min)")
    if status == "ok":
        summary, level = extract_summary(text, s.operator.summary_max_chars)
        rec["summary"], rec["summary_level"] = summary, level
        return finish("ok", EXIT_OK, level=level, title=title,
                      text=f"{summary or '(the session ended without a summary)'}\n{usage_line}")
    if status == "max_turns":
        rec["summary"] = None
        return finish("max_turns", EXIT_OK, level="warn", title=f"{title}: turn limit",
                      text=f"ended at the {spec.max_turns}-turn limit without a summary {usage_line} — "
                           f"{review_id}.session.json")
    return finish(status, EXIT_FAILED, level="warn", title=f"{title} failed",
                  text=f"{rec.get('error_kind') or status}: {(rec.get('error') or '')[:400]} {usage_line}")


# --------------------------------------------------------------------------- CLI
def config_error(kind: str, exc: Exception) -> int:
    """``load_settings()`` failed (e.g. a bad ``config.local.yaml``): there are no Settings, so no normal log, no
    notifier and no data root. One timestamped line goes to ``CONFIG_ERROR_LOG``, a best-effort critical toast is
    shown (skipped under ``TS_NOTIFY_DISABLE``), the error is printed to stderr; exit ``EXIT_CONFIG``. Never raises:
    under Task Scheduler this is the only trace the session leaves."""
    short = " ".join(f"{type(exc).__name__}: {exc}".split())
    try:
        short = get_redactor()(short)
    except Exception:  # noqa: BLE001
        pass
    line = f"{iso(_now_ms())} operator session ({kind}) cannot read its config: {short[:4000]}"
    try:
        CONFIG_ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
        if CONFIG_ERROR_LOG.exists() and CONFIG_ERROR_LOG.stat().st_size > CONFIG_ERROR_LOG_MAX:
            CONFIG_ERROR_LOG.replace(CONFIG_ERROR_LOG.with_name(CONFIG_ERROR_LOG.name + ".1"))
        with open(CONFIG_ERROR_LOG, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass
    try:
        from tradingsystem.core import notify as nt
        if not nt.disabled():
            subprocess.run(nt.toast_command("critical", "Operator session cannot read its config",
                                            f"{kind}: {short[:300]} — no review runs until it is fixed "
                                            f"({CONFIG_ERROR_LOG})"),
                           capture_output=True, timeout=CONFIG_TOAST_TIMEOUT_S, creationflags=_NO_WINDOW)
    except Exception:  # noqa: BLE001 — best effort (no powershell, a timeout, …)
        pass
    if sys.stderr is not None:
        try:
            print(f"!! {line}", file=sys.stderr)
        except (OSError, ValueError):
            pass
    return EXIT_CONFIG


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(prog="run_session.py", description="one Claude operator session")
    ap.add_argument("--kind", required=True, choices=sa.KINDS)
    ap.add_argument("--dry-run", action="store_true", help="build the pack, print the command; no CLI call")
    ap.add_argument("--reason", help="why this session runs (the monitor's findings for a diagnosis)")
    a = ap.parse_args(argv)
    try:
        s = load_settings()
    except Exception as exc:  # noqa: BLE001 — a config error must leave a trace (exit 3), never a lost traceback
        return config_error(a.kind, exc)
    try:
        setup_log(s)
    except OSError:
        pass
    try:
        return run(a.kind, s=s, dry_run=a.dry_run, reason=a.reason)
    except Exception:  # noqa: BLE001 — Task Scheduler shows only the exit code; the log has the traceback
        log.exception("operator session crashed")
        print("error: the operator session crashed (logs/operator-session.jsonl)")
        return EXIT_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
