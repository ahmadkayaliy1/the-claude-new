"""The Claude CLI command line of an operator session (§3.8, docs/operator_sessions.md) — built in ONE place.

    python tools/operator/session_args.py --kind daily|weekly|diagnose      → JSON: args, cwd, env changes

``tools/operator/run_session.py`` imports this module; the script form only prints what a session would run.

The session is a read-only reviewer with a narrow Bash allow-list:

* ``claude -p`` in print mode, ``--output-format json`` (one result document), no session files, no settings/CLAUDE.md
  (``--setting-sources=`` — the equals form: PowerShell 5.1 drops an empty ``""`` argument), no MCP servers;
* ``--permission-mode dontAsk --permission-prompts none``: whatever the allow-list does not name is denied, never
  asked (nobody is there to answer) — the denials come back in the result's ``permission_denials``;
* tools Read, Grep, Glob and Bash; editors and the web disallowed, ``.env`` unreadable; Bash only for the exact
  commands ``.venv/Scripts/python.exe tools/<tool>.py`` of health_report, review_pack, tune, propose and notify, bare or
  followed by a space and arguments (the diagnosis adds ``tools/kill_switch.py --pair``: one pair's switch only — the
  global switch is the monitor's). Every rule is ``Bash(<command> *)`` with a SPACE before the ``*``: Claude Code
  (2.1.282) compiles a pattern whose only ``*`` is a trailing `` *`` to ``<command>( .*)?``, while a glued
  ``tools/tune.py*`` is ``tools/tune[.]py.*`` — it matched ``tools/tune.py/../<any file>`` (Windows collapses the
  ``..``), i.e. any Python file, pip or demo_order_test.py, and python.exe arguments are not path-checked. No
  git rule: the review pack carries the git facts, and ``git diff``/``git log`` take ``--output=<file>`` (a file write
  through a read-only-looking prefix);
* the working directory is the checkout this file lives in (never a hard-coded path), so the relative allow-list paths
  resolve there; the data root is added with ``--add-dir`` only when it lies outside the checkout (a scratch root);
* model and effort per kind from ``ai.models.review`` (daily, weekly) or ``ai.models.monitor`` (diagnose), resolved
  like the orchestrator's roles; turns and time limits from ``operator.*``.

Environment of the CLI (and so of every tool the model runs): the provider's allow-list (``claude_code.ENV_KEEP``:
what Windows and the CLI need) — so the MT5 password, API keys, the dashboard token and the Telegram secrets that
``load_settings`` put into ``os.environ`` never reach the session — plus ``TRADINGSYSTEM_CONFIG`` / ``TS_INSTANCE`` (the
tools must see the same data root as this runner: a scratch live run depends on it), ``TS_NOTIFY_DISABLE``,
``CLAUDE_CODE_GIT_BASH_PATH`` (where the CLI finds Git Bash, when the user set it) and the subscription token when one
is configured (exactly as the provider passes it); never ``CLAUDECODE``, other ``CLAUDE_CODE_*``, ``CLAUDE_EFFORT`` or
``ANTHROPIC_*`` (an inherited API key would bill per token, D-030). Added: ``PYTHONIOENCODING=utf-8``, ``PYTHONUTF8=1``
(tool output carries "≥"/"→", which a cp1252 pipe cannot encode), ``MSYS_NO_PATHCONV=1`` (Git Bash would rewrite
``/nopause``-style arguments into paths) and the session marker ``TS_OPERATOR_SESSION=1``: every command the session
runs inherits it, and the allow-listed tools refuse what only a human may do (tune.py: a playbook FILE or ``-``;
propose.py: ``--body-file`` and a ``--base`` other than main; kill_switch.py: ``--all``).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from tradingsystem.ai.providers import claude_code as cc  # noqa: E402
from tradingsystem.ai.providers.base import secret  # noqa: E402
from tradingsystem.core.settings import Settings, load_settings  # noqa: E402

KINDS = ("daily", "weekly", "diagnose")
PY = ".venv/Scripts/python.exe"                 # relative to the working directory = the checkout
BASE_TOOLS = ("health_report", "review_pack", "tune", "propose", "notify")
DIAGNOSE_TOOLS = ("kill_switch",)
# what may follow ``tools/<tool>.py`` in a rule (default "*": any arguments); the diagnosis may engage one pair's
# switch only — ``--all`` (the global switch) is the monitor's decision (§3.8), and kill_switch.py refuses it in a
# session too
RULE_TAIL = {"kill_switch": " --pair *"}         # every other tool: " *" (never a glued "*", see the docstring)
READ_TOOLS = ("Read", "Grep", "Glob")
TOOLS = "Read,Grep,Glob,Bash"
DISALLOWED = ("Edit", "Write", "NotebookEdit", "WebFetch", "WebSearch", "Read(**/.env)", "Read(.env)",
              "Read(**/.env.*)",
              # the CLI's own sign-in and settings, SSH/cloud credentials: never part of a review
              "Read(~/.claude/**)", "Read(~/.ssh/**)", "Read(~/.aws/**)", "Read(~/.config/**)",
              "Read(**/.credentials.json)")
PROMPTS = Path(__file__).resolve().parent / "prompts"
SYSTEM_PROMPT = PROMPTS / "_system.md"
LEDGER_ROLE = {"daily": "review", "weekly": "review", "diagnose": "diagnose"}     # ai_usage.role of the session row
MODEL_ROLE = {"daily": "review", "weekly": "review", "diagnose": "monitor"}       # ai.models.<role>
DIAGNOSE_PACK_HOURS = 6                          # the monitor's findings are recent: the last hours are the evidence
PACK_MAX_CHARS = {"daily": 40_000, "weekly": 40_000, "diagnose": 16_000}   # diagnosis ≤ ~8 k tokens in all

# environment
PASS_THROUGH = ("TRADINGSYSTEM_CONFIG", "TS_INSTANCE", "TS_NOTIFY_DISABLE", "CLAUDE_CODE_GIT_BASH_PATH")
DROP_PREFIXES = ("CLAUDE_CODE_", "ANTHROPIC_")
DROP_NAMES = ("CLAUDECODE", "CLAUDE_EFFORT")
SESSION_ENV = "TS_OPERATOR_SESSION"        # the session marker the tools check (tune, propose, kill_switch, …)
ADDED_ENV = {"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "MSYS_NO_PATHCONV": "1", SESSION_ENV: "1"}
TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"


class SessionSpec(NamedTuple):       # not a dataclass: tests load this file outside sys.modules
    """Everything a session run needs to know about its CLI call (no state, no side effects)."""
    kind: str
    provider: str                 # the claude_code provider's config name (the ledger row's provider)
    exe: str | None               # None: the CLI was not found
    model: str
    effort: str | None
    max_turns: int
    timeout_min: int
    pack_hours: float
    pack_max_chars: int
    ledger_role: str
    root: Path                    # working directory = the checkout
    data_root: Path
    system_prompt: Path
    kind_prompt: Path
    add_dirs: tuple[Path, ...] = ()
    allowed: tuple[str, ...] = ()
    disallowed: tuple[str, ...] = DISALLOWED


def check_kind(kind: str) -> str:
    if kind not in KINDS:
        raise ValueError(f"kind {kind!r}: one of {', '.join(KINDS)}")
    return kind


def provider_name(s: Settings) -> str:
    """The claude_code provider: the active one when it is claude_code, else the first configured — by kind, not by
    ``active_provider`` alone (``.env ACTIVE_AI_PROVIDER`` may switch the active one to the fallback)."""
    if s.ai.providers.get(s.ai.active_provider) and s.ai.providers[s.ai.active_provider].kind == "claude_code":
        return s.ai.active_provider
    for name, cfg in s.ai.providers.items():
        if cfg.kind == "claude_code":
            return name
    raise ValueError("no provider of kind claude_code is configured (ai.providers)")


def model_effort(s: Settings, kind: str) -> tuple[str, str | None, str]:
    """(model, effort, provider) of a session: ``ai.models.review`` or ``.monitor``, else the provider's own
    (``CLAUDE_CODE_MODEL`` honoured) — the orchestrator's role resolution."""
    name = provider_name(s)
    cfg = s.ai.providers[name]
    rm = getattr(s.ai.models, MODEL_ROLE[check_kind(kind)])
    return rm.model or s.provider_model(name), rm.effort or cfg.effort, name


def allowed_tools(kind: str) -> list[str]:
    """The allow-list: Read/Grep/Glob, and Bash only for the exact tool commands, bare or with arguments after a
    space (the diagnosis adds the kill switch, ``--pair`` only). The rules match the command text literally —
    ``./.venv/…``, backslashes, ``python tools/…`` or ``tools/tune.py/../x`` are denied."""
    tools = BASE_TOOLS + (DIAGNOSE_TOOLS if check_kind(kind) == "diagnose" else ())
    return [*READ_TOOLS, *(f"Bash({PY} tools/{t}.py{RULE_TAIL.get(t, ' *')})" for t in tools)]


def outside(path: Path, root: Path) -> bool:
    p, r = path.resolve(), root.resolve()
    return p != r and r not in p.parents


def spec_for(s: Settings, kind: str, *, root: Path = ROOT, exe: str | None = None) -> SessionSpec:
    model, effort, name = model_effort(s, kind)
    op = s.operator
    data = s.paths.data()
    hours = {"daily": op.daily_pack_hours, "weekly": op.weekly_pack_hours, "diagnose": DIAGNOSE_PACK_HOURS}[kind]
    return SessionSpec(
        kind=kind, provider=name, exe=exe if exe is not None else cc.find_cli(s.ai.providers[name].cli_path),
        model=model, effort=effort, max_turns=int(getattr(op, f"{kind}_max_turns")),
        timeout_min=int(getattr(op, f"{kind}_timeout_min")), pack_hours=float(hours),
        pack_max_chars=PACK_MAX_CHARS[kind], ledger_role=LEDGER_ROLE[kind], root=root, data_root=data,
        system_prompt=SYSTEM_PROMPT, kind_prompt=PROMPTS / f"{kind}.md",
        add_dirs=(data,) if outside(data, root) else (), allowed=tuple(allowed_tools(kind)))


def build_args(spec: SessionSpec) -> list[str]:
    """The CLI arguments (a list for ``subprocess`` — no shell, no PowerShell quoting). The prompt goes on stdin:
    ``--add-dir``/``--tools``/``--disallowedTools``/``--allowedTools`` are variadic and would swallow a positional."""
    if not spec.exe:
        raise ValueError("Claude Code CLI not found (install it or set ai.providers.<claude_code>.cli_path)")
    args = [spec.exe, "-p", "--model", spec.model]
    if spec.effort:
        args += ["--effort", spec.effort]
    args += ["--output-format", "json", "--no-session-persistence", "--setting-sources=", "--strict-mcp-config",
             "--permission-mode", "dontAsk", "--permission-prompts", "none", "--max-turns", str(spec.max_turns),
             "--system-prompt-file", str(spec.system_prompt)]
    for d in spec.add_dirs:
        args += ["--add-dir", str(d)]
    args += ["--tools", TOOLS, "--disallowedTools", *spec.disallowed, "--allowedTools", *spec.allowed]
    return args


def dropped(name: str) -> bool:
    """A name that must never reach the CLI: the calling Claude Code session's variables and any Anthropic API
    setting (``CLAUDE_CODE_GIT_BASH_PATH`` is the one CLAUDE_CODE_* the CLI itself may need)."""
    u = name.upper()
    if u == "CLAUDE_CODE_GIT_BASH_PATH":
        return False
    return u in DROP_NAMES or u.startswith(DROP_PREFIXES)


def child_env(s: Settings | None = None, environ: dict[str, str] | None = None,
              oauth_token: str | None = None) -> dict[str, str]:
    """The session's environment (see the module docstring). ``oauth_token``: default = the provider's configured
    long-lived token (``secret(api_key_env)``), passed exactly as the provider passes it; None/"" = none."""
    src = dict(os.environ if environ is None else environ)
    env = {k: v for k, v in cc.child_env(None, src).items() if not dropped(k)}
    upper = {k.upper(): k for k in src}
    for name in PASS_THROUGH:
        if name in upper and src[upper[name]] != "":
            env[upper[name]] = src[upper[name]]
    if oauth_token is None and s is not None:
        try:
            oauth_token = secret(s.ai.providers[provider_name(s)].api_key_env)
        except ValueError:
            oauth_token = None
    if oauth_token:
        env[TOKEN_ENV] = oauth_token
    env.update(ADDED_ENV)
    return env


def env_diff(parent: dict[str, str], child: dict[str, str]) -> dict[str, object]:
    """What the session's environment differs by — NAMES only for removals, never a value of a secret."""
    added = {}
    for k, v in child.items():
        if parent.get(k) != v:
            added[k] = "(hidden)" if k.upper() == TOKEN_ENV or "TOKEN" in k.upper() else v
    return {"removed": sorted(k for k in parent if k not in child), "added_or_changed": added,
            "kept": sorted(k for k in child if k in parent and parent[k] == child[k])}


def describe(spec: SessionSpec, env: dict[str, str], parent: dict[str, str] | None = None) -> dict[str, object]:
    return {"kind": spec.kind, "cwd": str(spec.root), "args": build_args(spec) if spec.exe else None,
            "cli_found": bool(spec.exe), "model": spec.model, "effort": spec.effort, "max_turns": spec.max_turns,
            "timeout_min": spec.timeout_min, "pack_hours": spec.pack_hours, "add_dirs": [str(d) for d in spec.add_dirs],
            "env": env_diff(dict(os.environ if parent is None else parent), env)}


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(prog="session_args.py", description="print an operator session's CLI command line")
    ap.add_argument("--kind", required=True, choices=KINDS)
    a = ap.parse_args(argv)
    try:
        s = load_settings()
        spec = spec_for(s, a.kind)
    except ValueError as exc:
        print(f"error: {exc}")
        return 1
    print(json.dumps(describe(spec, child_env(s)), indent=1, ensure_ascii=False))
    return 0 if spec.exe else 1


if __name__ == "__main__":
    raise SystemExit(main())
