"""Versioned prompt library (P8.4).

Templates are Markdown files using ``$name`` placeholders (``string.Template`` — JSON braces stay literal).
System prompts are assembled from the shared persona and the shared non-negotiable rules, so every mode
carries the same risk / anti-fabrication / data-quality clauses. ``prompt_hash`` identifies the exact text
used for each decision (stored with the decision, P8.7).
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from string import Template

DIR = Path(__file__).parent
_HEADER = re.compile(r"^<!-- prompt: .*? -->\s*", re.M)

ROLE_FILES = {
    "agent_per_pair": ("agent_per_pair/system.md", "agent_per_pair/instructions.md"),
    "single_agent_global": ("single_agent_global/system.md", "single_agent_global/instructions.md"),
    "timeframe_analyst": ("timeframe_analyst/system.md", "timeframe_analyst/instructions.md"),
    "coordinator": ("coordinator/system.md", "coordinator/instructions.md"),
    "risk_reviewer": ("risk_reviewer/system.md", "risk_reviewer/instructions.md"),
}


class PromptError(ValueError):
    pass


def _read(rel: str) -> str:
    return _HEADER.sub("", (DIR / rel).read_text(encoding="utf-8")).strip()


def _fill(text: str, values: dict[str, object], where: str) -> str:
    """Placeholders are checked on the *template*: model-authored values (history, proposals, assessments)
    may contain ``$PDH`` or ``$BTC`` and are inserted verbatim, never re-parsed."""
    t = Template(text)
    if not t.is_valid():
        raise PromptError(f"{where}: invalid '$' in template (write $$ for a literal dollar sign)")
    missing = sorted(set(t.get_identifiers()) - set(values))
    if missing:
        raise PromptError(f"{where}: unfilled placeholders {missing}")
    return t.substitute({k: str(v) for k, v in values.items()})


@dataclass(frozen=True)
class RenderedPrompt:
    role: str
    system: str
    user: str

    @property
    def prompt_hash(self) -> str:
        """Hash of the system prompt only (stable across cycles → cache-friendly, identifies the version)."""
        return hashlib.sha256(self.system.encode()).hexdigest()[:16]


def render(role: str, system_vars: dict[str, object], user_vars: dict[str, object]) -> RenderedPrompt:
    """Render a role. ``system_vars`` must be stable across cycles (keeps the system prompt cacheable);
    per-cycle values (time, payload) belong in ``user_vars``."""
    if role not in ROLE_FILES:
        raise PromptError(f"unknown prompt role {role!r}")
    sys_file, user_file = ROLE_FILES[role]
    base = dict(system_vars)
    base.setdefault("persona", _fill(_read("shared/trader_persona.md"), system_vars, "persona"))
    base.setdefault("core_rules", _fill(_read("shared/core_rules.md"), system_vars, "core_rules"))
    system = _fill(_read(sys_file), base, sys_file)
    user = _fill(_read(user_file), {**system_vars, **user_vars}, user_file)
    return RenderedPrompt(role, system, user)


def library_hash() -> str:
    h = hashlib.sha256()
    for p in sorted(DIR.rglob("*.md")):
        h.update(p.relative_to(DIR).as_posix().encode())
        h.update(p.read_bytes())
    return h.hexdigest()[:16]
