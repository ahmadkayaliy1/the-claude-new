"""Kill switches (ON only from code; OFF stays a deliberate user action: scripts\\kill_switch_off.bat).

* ``data/KILL_SWITCH`` — every system on this machine stops opening trades.
* ``data/instances/<PAIR>/KILL_SWITCH`` — that pair's system only (its own and the all-pairs system alike).

The executor only tests whether the file exists (:meth:`..execution.executor.Executor.kill_switch`); the content says
who set it and why (read by the dashboard, the monitor and the review pack). Protective actions on open trades keep
running while a switch is on.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .settings import Settings
from .timeutil import iso, now_ms


def kill_switch_path(s: Settings, pair: str | None = None) -> Path:
    """The global switch (``pair`` None) or one pair's switch under the shared data root."""
    data = s.paths.data()
    return data / "KILL_SWITCH" if pair is None else data / "instances" / pair / "KILL_SWITCH"


def is_on(s: Settings, pair: str | None = None) -> bool:
    """Whether the global switch or (with ``pair``) that pair's switch is on."""
    return kill_switch_path(s, None).exists() or (pair is not None and kill_switch_path(s, pair).exists())


def read_reason(path: Path) -> dict:
    """Who set a switch and why ({} for an empty file written by the .bat scripts, or an unreadable one)."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
        return json.loads(text) if text.startswith("{") else ({"reason": text} if text else {})
    except (OSError, ValueError):
        return {}


def set_kill_switch(s: Settings, pair: str | None, *, reason: str, actor: str) -> tuple[Path, bool]:
    """Turn a switch ON (idempotent). Returns (path, created): an existing switch is left as it is — its first
    reason is kept. Never raises on an existing file; raises OSError only when the file cannot be written."""
    path = kill_switch_path(s, pair)
    if path.exists():
        return path, False
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps({"ts": iso(now_ms()), "actor": actor, "reason": reason[:500],
                       "scope": pair or "all"}, ensure_ascii=False)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{int(time.time() * 1000)}.tmp")
    tmp.write_text(body, encoding="utf-8")
    try:
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return path, True


__all__ = ["is_on", "kill_switch_path", "read_reason", "set_kill_switch"]
