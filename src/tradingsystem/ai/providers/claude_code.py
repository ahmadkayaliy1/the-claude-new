"""Claude through the local Claude Code CLI on the user's own Claude subscription (D-030) — no API key, no
per-token bill.

Each call spawns ``claude -p`` (print mode):

* our versioned system prompt replaces Claude Code's own (``--system-prompt-file``); the snapshot goes on stdin;
* output format: with ``structured_output: prompt`` (default in config) the JSON Schema is appended to the system
  prompt and the model answers in ONE turn; ``native`` uses ``--json-schema``, which makes the CLI run a tool
  round-trip (measured 2026-09-26: ~4-5x the input tokens and minutes of latency per call). Either way the contract
  is validated client-side afterwards;
* pure analysis: ``--tools ""`` + ``--strict-mcp-config`` + ``--setting-sources ""`` in an empty working
  directory — the model cannot read files, run commands or reach MCP servers, and no CLAUDE.md, hooks or
  settings are loaded. Data collection, the risk gate and execution stay in our own code;
* a minimal child environment: our secrets (MT5 password, Gemini key, dashboard token) are not passed on, and
  ``ANTHROPIC_API_KEY`` / ``ANTHROPIC_BASE_URL`` are dropped so a call can only run on the subscription login
  (``claude auth login`` once, or a long-lived ``claude setup-token`` given as ``CLAUDE_CODE_OAUTH_TOKEN``).

Usage counts against the plan's shared 5-hour / weekly limits (the same pool as interactive Claude use). When
the CLI reports a usage limit the provider cools down until the reset (or ``DEFAULT_COOLDOWN_MS``) and
``unavailable_reason()`` lets the orchestrator route to ``ai.fallback_provider``. At most ``max_concurrency`` CLI
processes run at once (≈170 MB each — keep 1 on 4 GB machines) and a call that times out or is cancelled (cycle deadline, shutdown)
kills its CLI process, so no orphan keeps spending the plan's limits.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from ...core.filelock import FileLock
from ...core.timeutil import now_ms
from .base import LLMProvider, LLMResult, ProviderError, secret, transport_schema

# environment passed to the CLI: what Windows and the CLI need to run and find its login — nothing else
ENV_KEEP = {"SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "PATH", "PATHEXT", "COMSPEC", "USERPROFILE", "HOMEDRIVE",
            "HOMEPATH", "HOME", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)",
            "TEMP", "TMP", "USERNAME", "USERDOMAIN", "COMPUTERNAME", "NUMBER_OF_PROCESSORS",
            "PROCESSOR_ARCHITECTURE", "OS", "LANG", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY"}
AUTH_CHECK_TTL_S = 300
AUTH_RECHECK_S = 60              # after the status command itself failed
AUTH_CHECK_TIMEOUT_S = 90        # a cold CLI start on a busy machine took 55 s (2026-09-26)
DEFAULT_COOLDOWN_MS = 30 * 60_000
LOGIN_COOLDOWN_MS = 5 * 60_000
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_LOGIN_RE = re.compile(r"not logged in|please run /login|invalid (api key|bearer token)|oauth token (has )?"
                       r"(expired|revoked)|authentication_error", re.I)
_LIMIT_RE = re.compile(r"usage limit|hit your limit|limit reached|limit will reset|out of (extra )?usage|"
                       r"rate[ _-]?limit", re.I)
_EPOCH_RE = re.compile(r"\|(\d{10})\b")
# two CLI processes refreshing the OAuth token at the same moment (observed 2026-09-26 after a reboot: one lost the
# race, the next call got "403 Request not allowed"): transient — retried after a pause, never a sign-out
_REFRESH_RACE_RE = re.compile(r"another claude code process is refreshing", re.I)
# may follow a lost race, or be a real sign-in problem: retried once, and the sign-in is re-checked in the background
_AUTH_TRANSIENT_RE = re.compile(r"403 request not allowed|failed to refresh oauth token", re.I)
START_STAGGER_S = 15.0           # minimum gap between two CLI starts on this machine (token refresh happens at start)
START_STAMP = "last_start.txt"   # in the CLI work folder, shared by every system of this Windows user (D-042)



def claim_start(workdir: Path, gap_s: float | None = None) -> float:
    """Machine-wide spacing of CLI starts: 0.0 when this process may start a CLI now (the start is recorded), else
    the seconds to wait. Every system on the machine (one per pair, D-042) and every sign-in check shares the work
    folder, so two CLIs never refresh the OAuth token at the same moment."""
    gap = START_STAGGER_S if gap_s is None else gap_s
    if gap <= 0:
        return 0.0
    with FileLock(workdir / "start.lock").hold(timeout=5) as got:
        if not got:
            return 1.0
        stamp = workdir / START_STAMP
        try:
            last = float(stamp.read_text(encoding="ascii").strip() or 0)
        except (OSError, ValueError):
            last = 0.0
        now = time.time()
        if last > now + 5:                  # the wall clock was stepped back: the stamp says nothing
            last = 0.0
        wait = last + gap - now
        if wait > 0:
            return wait
        stamp.write_text(f"{now:.3f}", encoding="ascii")
        return 0.0


class AuthCheckFailed(RuntimeError):
    """``claude auth status`` itself failed (timeout, broken start) — not evidence of a sign-out."""

def find_cli(configured: str | None) -> str | None:
    """The Claude Code executable: ``cli_path`` if set, else PATH, else the native installer's location."""
    if configured:
        return configured if Path(configured).is_file() else None
    found = shutil.which("claude")
    if found:
        return found
    default = Path.home() / ".local" / "bin" / ("claude.exe" if os.name == "nt" else "claude")
    return str(default) if default.is_file() else None


def child_env(oauth_token: str | None, environ: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in (os.environ if environ is None else environ).items() if k.upper() in ENV_KEEP}
    if oauth_token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = oauth_token
    return env


def limit_reset_ms(message: str, now: int) -> int:
    """When a usage limit resets: the epoch the CLI embeds (``…|1767225600``) if plausible, else a short
    cooldown after which the provider is simply tried again."""
    m = _EPOCH_RE.search(message)
    if m:
        t = int(m.group(1)) * 1000
        if now < t <= now + 8 * 86_400_000:
            return t
    return now + DEFAULT_COOLDOWN_MS



def _without_titles(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: _without_titles(v) for k, v in node.items() if k != "title" or not isinstance(v, str)}
    if isinstance(node, list):
        return [_without_titles(v) for v in node]
    return node

def auth_problem(name: str, status: dict[str, Any]) -> str | None:
    """Interpret ``claude auth status --json``: only a subscription sign-in is accepted — any sign of an API key
    (``authMethod`` naming a key, or an ``apiKeySource`` other than none) is refused."""
    if not status.get("loggedIn"):
        return f"{name}: Claude Code is not signed in — run `claude auth login` once (H11)"
    method = str(status.get("authMethod", ""))
    key_source = str(status.get("apiKeySource") or "none")
    if "key" in method.lower() or key_source.lower() not in ("none", "null", ""):
        return (f"{name}: Claude Code is signed in with an API key ({method or key_source}), not the subscription "
                "— refusing so nothing is billed per token (D-030)")
    return None


def _main_model(model_usage: dict[str, Any], default: str) -> str:
    """The model that produced the answer (Claude Code may also use a small model for housekeeping)."""
    if not model_usage:
        return default
    return max(model_usage, key=lambda m: (model_usage[m] or {}).get("outputTokens", 0) or 0)


class ClaudeCodeProvider(LLMProvider):
    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self.exe = find_cli(self.cfg.cli_path)
        if not self.exe:
            raise ProviderError(f"{self.name}: Claude Code CLI not found (install it or set cli_path)", retryable=False)
        self.workdir = Path(tempfile.gettempdir()) / "tradingsystem-claude-code"
        self.workdir.mkdir(parents=True, exist_ok=True)
        self._auth_checked = float("-inf")
        self._auth_problem: str | None = f"{self.name}: checking the Claude Code sign-in"
        self._auth_ok_once = False
        self._auth_refreshing = False
        self._slots: asyncio.Semaphore | None = None
        self._start_lock: asyncio.Lock | None = None
        self._last_start = float("-inf")
        if self.api_key:                    # a long-lived token is trusted until a call fails: nothing to check
            self._auth_problem, self._auth_ok_once, self._auth_checked = None, True, time.monotonic()

    # ------------------------------------------------------------------ availability
    def unavailable_reason(self, refresh: bool = True) -> str | None:
        """Cooldown, else the cached login check. Every check runs in a background thread — ``claude auth status``
        can take ~1 min on a cold, busy machine (measured 55 s) and the engine's event loop (heartbeat) must never
        wait on it; until the first answer the provider reports "checking". ``refresh=False`` (inside a call) never
        starts a check, so a status command and a model call are not started together (OAuth refresh race)."""
        why = super().unavailable_reason()
        if why:
            return why
        if refresh and time.monotonic() - self._auth_checked > AUTH_CHECK_TTL_S and not self._auth_refreshing \
                and time.monotonic() - self._last_start >= START_STAGGER_S:
            self._auth_refreshing = True
            threading.Thread(target=self._refresh_auth, name="claude-auth-status", daemon=True).start()
        return self._auth_problem

    def _refresh_auth(self) -> None:
        now = time.monotonic()
        try:
            try:
                if not self.api_key and claim_start(self.workdir) > 0:
                    return                              # another system just started a CLI: ask again on the next look
            except OSError as exc:                      # the shared stamp/lock file (antivirus, temp cleanup)
                raise AuthCheckFailed(f"{self.name}: cannot space the CLI start ({exc})") from exc
            self._last_start = now                      # the next model call waits START_STAGGER_S after this CLI start
            problem = self.check_auth()
        except AuthCheckFailed as exc:
            # the check itself failed (slow or broken CLI start), which says nothing about the sign-in: keep a sign-in
            # verified before, and ask again in a minute rather than after the full TTL
            if not self._auth_ok_once:
                self._auth_problem = str(exc)
            self._auth_checked = now - AUTH_CHECK_TTL_S + AUTH_RECHECK_S
        else:
            self._auth_problem = problem
            self._auth_ok_once = self._auth_ok_once or problem is None
            self._auth_checked = now
        finally:
            self._auth_refreshing = False

    def check_auth(self) -> str | None:
        """``claude auth status`` (no model call, no usage): None when signed in with the subscription, else the
        problem; raises :class:`AuthCheckFailed` when the command itself fails. A long-lived token is trusted until a
        call fails; one added to ``.env`` later is picked up here (OPS-07)."""
        self.api_key = self.api_key or secret(self.cfg.api_key_env)
        if self.api_key:
            return None
        try:
            p = subprocess.run([self.exe, "auth", "status", "--json"], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", env=child_env(None), cwd=self.workdir,
                               timeout=AUTH_CHECK_TIMEOUT_S, creationflags=_NO_WINDOW)
            st = json.loads(p.stdout or "{}")
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            raise AuthCheckFailed(f"{self.name}: cannot read the Claude Code login status ({exc})") from exc
        return auth_problem(self.name, st)

    # ------------------------------------------------------------------ call
    def build_args(self, system_file: str, schema: dict | None) -> list[str]:
        args = [self.exe, "-p", "--output-format", "json", "--model", self.model,
                "--system-prompt-file", system_file, "--tools", "", "--strict-mcp-config",
                "--setting-sources", "", "--no-session-persistence"]
        if self.cfg.effort:
            args += ["--effort", self.cfg.effort]
        if self.cfg.fallback_model:
            args += ["--fallback-model", self.cfg.fallback_model]
        if schema and self.cfg.structured_output == "native":
            args += ["--json-schema", json.dumps(transport_schema(schema, "anthropic"), separators=(",", ":"))]
        return args

    def system_text(self, system: str, schema: dict | None) -> str:
        """The system prompt; in prompt mode followed by the output schema (static per role → cached by the CLI).
        Schema ``title`` keys only repeat the field names and are left out."""
        if not schema or self.cfg.structured_output == "native":
            return system
        return (f"{system}\n\n# Output format\nReply with ONLY one JSON object - no prose before or after it, no code "
                "fences - that validates against this JSON Schema (respect every enum, maxLength and numeric bound):\n"
                + json.dumps(_without_titles(schema), separators=(",", ":"), ensure_ascii=False))

    async def _staggered_start(self) -> None:
        """Keep CLI starts at least ``START_STAGGER_S`` apart — within this process and across every system on the
        machine (the OAuth refresh race)."""
        if self._start_lock is None:
            self._start_lock = asyncio.Lock()
        async with self._start_lock:
            wait = self._last_start + START_STAGGER_S - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            while (wait := await asyncio.to_thread(claim_start, self.workdir)) > 0:
                await asyncio.sleep(min(wait, START_STAGGER_S))
            self._last_start = time.monotonic()

    async def _call(self, system: str, user: str, schema: dict | None, schema_name: str,
                    max_output_tokens: int) -> LLMResult:
        why = self.unavailable_reason(refresh=False)
        if why:
            raise ProviderError(why, retryable=False, rate_limited=now_ms() < self.cooldown_until_ms)
        if self._slots is None:
            self._slots = asyncio.Semaphore(max(1, self.cfg.max_concurrency))
        async with self._slots:
            why = self.unavailable_reason(refresh=False)   # another call may have hit the limit while we waited
            if why:
                raise ProviderError(why, retryable=False, rate_limited=now_ms() < self.cooldown_until_ms)
            await self._staggered_start()
            fd, system_file = tempfile.mkstemp(prefix="system_", suffix=".md", dir=self.workdir)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(self.system_text(system, schema))
            try:
                proc = await asyncio.create_subprocess_exec(
                    *self.build_args(system_file, schema), stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=self.workdir,
                    env=child_env(self.api_key), creationflags=_NO_WINDOW)
                try:
                    out, err = await asyncio.wait_for(proc.communicate(user.encode("utf-8")), self.cfg.timeout_s)
                except asyncio.TimeoutError:
                    # not retried: the timed-out call has most likely used the plan's limits already
                    raise ProviderError(f"{self.name}: no answer within {self.cfg.timeout_s:.0f}s",
                                        retryable=False) from None
                finally:
                    if proc.returncode is None:        # timed out or cancelled: never leave a CLI running
                        _kill(proc)
                        try:
                            await asyncio.wait_for(proc.wait(), 5)
                        except (asyncio.TimeoutError, OSError):
                            pass
            except OSError as exc:
                raise ProviderError(f"{self.name}: cannot start the Claude Code CLI ({exc})", retryable=False) from exc
            finally:
                try:
                    os.unlink(system_file)
                except OSError:
                    pass
        return self.parse(out.decode("utf-8", "replace"), err.decode("utf-8", "replace"), proc.returncode)

    # ------------------------------------------------------------------ output
    def parse(self, stdout: str, stderr: str, returncode: int | None) -> LLMResult:
        doc = None
        for chunk in (stdout, *reversed(stdout.strip().splitlines())):
            try:
                doc = json.loads(chunk)
                break
            except ValueError:
                continue
        if not isinstance(doc, dict) or doc.get("type") != "result":
            raise self._error((stderr or stdout).strip()[:300] or f"CLI exited with code {returncode}", None)
        if doc.get("is_error") or doc.get("subtype") != "success":
            raise self._error(str(doc.get("result") or doc.get("subtype") or "error")[:300],
                              doc.get("api_error_status"))
        data = doc.get("structured_output")
        text = doc.get("result")
        if data is None and doc.get("stop_reason") == "max_tokens":
            # a truncated answer would be truncated again — never spend a repair call on it
            raise ProviderError(f"{self.name}: answer truncated (max_tokens)", retryable=False)
        if not isinstance(text, str) or not text.strip():
            text = json.dumps(data, ensure_ascii=False) if data is not None else ""
        u = doc.get("usage") or {}
        cached = int(u.get("cache_read_input_tokens") or 0)
        written = int(u.get("cache_creation_input_tokens") or 0)
        return LLMResult(
            provider=self.name, model=_main_model(doc.get("modelUsage") or {}, self.model), text=text, data=data,
            input_tokens=int(u.get("input_tokens") or 0) + cached + written,
            output_tokens=int(u.get("output_tokens") or 0), cached_input_tokens=cached,
            stop_reason=doc.get("stop_reason"), request_id=doc.get("session_id"),
            extra={"api_equivalent_usd": float(doc.get("total_cost_usd") or 0.0), "num_turns": doc.get("num_turns"),
                   "cache_creation_input_tokens": written},
        )

    def _error(self, message: str, status: Any) -> ProviderError:
        now = now_ms()
        if _REFRESH_RACE_RE.search(message):
            return ProviderError(f"{self.name}: sign-in token refresh race ({message[:120]})", retryable=True)
        if _AUTH_TRANSIENT_RE.search(message):
            self._auth_checked = 0.0                      # a real sign-out shows up in the background re-check
            return ProviderError(f"{self.name}: sign-in refused ({message[:120]})", retryable=True)
        if _LOGIN_RE.search(message):
            self.cool_down(now + LOGIN_COOLDOWN_MS, f"{self.name}: not signed in ({message[:120]})")
            self._auth_checked = 0.0                      # re-check (in the background) after the cooldown
            return ProviderError(self.cooldown_reason, retryable=False)
        if status == 429 or _LIMIT_RE.search(message):
            self.cool_down(limit_reset_ms(message, now), f"{self.name}: subscription usage limit ({message[:120]})")
            return ProviderError(self.cooldown_reason, retryable=False, rate_limited=True)
        retryable = status in (500, 502, 503, 504, 529) or "overloaded" in message.lower()
        return ProviderError(f"{self.name}: {message}", retryable=retryable)


def _kill(proc: asyncio.subprocess.Process) -> None:
    try:
        proc.kill()
    except (ProcessLookupError, OSError):
        pass
