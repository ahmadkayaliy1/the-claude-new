"""Versioned prompt library (P8.4).

Templates are Markdown files using ``$name`` placeholders (``string.Template`` — JSON braces stay literal).
System prompts are assembled from the shared persona and the shared non-negotiable rules, so every mode
carries the same risk / anti-fabrication / data-quality clauses. ``prompt_hash`` identifies the exact text
used for each decision (stored with the decision, P8.7).
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from string import Template

DIR = Path(__file__).parent
log = logging.getLogger(__name__)
_HEADER = re.compile(r"^<!-- prompt: .*? -->\s*", re.M)

ROLE_FILES = {
    "agent_per_pair": ("agent_per_pair/system.md", "agent_per_pair/instructions.md"),
    "single_agent_global": ("single_agent_global/system.md", "single_agent_global/instructions.md"),
    "timeframe_analyst": ("timeframe_analyst/system.md", "timeframe_analyst/instructions.md"),
    "coordinator": ("coordinator/system.md", "coordinator/instructions.md"),
    "risk_reviewer": ("risk_reviewer/system.md", "risk_reviewer/instructions.md"),
    "escalation": ("escalation/system.md", "escalation/instructions.md"),
}


class PromptError(ValueError):
    pass


def _read(rel: str) -> str:
    return _read_versioned(rel)[0]


def _read_versioned(rel: str) -> tuple[str, str, int | None]:
    """(text without its header, library name ``shared/core_rules``, version from the header or None) — one read,
    so the version recorded is the version of the text actually used."""
    raw = (DIR / rel).read_text(encoding="utf-8")
    m = _VERSION.search(raw)
    name = m.group(1) if m else rel.removesuffix(".md")
    return _HEADER.sub("", raw).strip(), name, int(m.group(2)) if m else None


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
    # Phase 4: ``{"shared/core_rules": 6, "agent_per_pair/instructions": 5, …}`` of the files this render used
    versions: dict[str, int] = field(default_factory=dict, compare=False)

    @property
    def prompt_hash(self) -> str:
        """Hash of the system prompt only (stable across cycles → cache-friendly, identifies the version)."""
        return hashlib.sha256(self.system.encode()).hexdigest()[:16]


_APPENDIX = re.compile(r"^desks/[a-z0-9_]+$")


def render(role: str, system_vars: dict[str, object], user_vars: dict[str, object],
           appendix: str | tuple[str, ...] | list[str] | None = None) -> RenderedPrompt:
    """Render a role. ``system_vars`` must be stable across cycles (keeps the system prompt cacheable);
    per-cycle values (time, payload) belong in ``user_vars``. ``appendix`` (Phase 5 B18, D-049) names versioned
    library files (``desks/xau``, ``desks/xauusd_fields``) appended verbatim, in order, after the system prompt -
    constant per pair, no placeholder, their versions recorded like the others; None or empty leaves the system
    prompt exactly as before."""
    if role not in ROLE_FILES:
        raise PromptError(f"unknown prompt role {role!r}")
    sys_file, user_file = ROLE_FILES[role]
    base, used = dict(system_vars), {}

    def text(rel: str, record: bool = True) -> str:
        body, name, version = _read_versioned(rel)
        if record and version is not None:
            used[name] = version
        return body

    for var, rel in (("persona", "shared/trader_persona.md"), ("core_rules", "shared/core_rules.md"),
                     ("payload_legend", "shared/payload_legend.md")):
        own = var in base                          # a caller-supplied block replaces the shared file (still checked)
        base.setdefault(var, _fill(text(rel, record=not own), system_vars, var))
    system = _fill(text(sys_file), base, sys_file)
    for name in ((appendix,) if isinstance(appendix, str) else tuple(appendix or ())):
        if not isinstance(name, str) or not _APPENDIX.match(name) or not (DIR / f"{name}.md").is_file():
            raise PromptError(f"unknown prompt appendix {name!r} (a desks/<name> file of the prompt library)")
        system = system + "\n\n" + text(f"{name}.md")      # verbatim: a desk brief has no placeholders
    user = _fill(text(user_file), {**system_vars, **user_vars}, user_file)
    return RenderedPrompt(role, system, user, used)


def register_versions(store, rendered: RenderedPrompt, role: str, *, lib_hash: str | None = None) -> None:
    """Record ``rendered``'s prompt hash and the library hash with the versions of the files it used in
    ``prompt_versions`` (first sighting, :meth:`..store.DecisionStore.register_prompt`). The system prompt's hash alone
    does not identify a version — an edit of the user template (``instructions.md``) leaves it unchanged — so the
    registry is keyed on both. One database write per new (system prompt, library) per process; never raises — a
    registry problem must not fail a cycle. ``lib_hash`` defaults to the library as it is now."""
    try:
        if store is None:
            return
        lib = lib_hash or library_hash()
        if store.prompt_known(rendered.prompt_hash, lib):
            return
        store.register_prompt(rendered.prompt_hash, role, lib, dict(rendered.versions))
    except Exception:  # noqa: BLE001
        log.warning("prompt versions of %s (%s) not registered", role, rendered.prompt_hash, exc_info=True)


_VERSION = re.compile(r"^<!-- prompt: (\S+) · version (\d+) -->", re.M)


def versions() -> dict[str, int]:
    """``{"shared/core_rules": 6, …}`` from the files' version headers (recorded with the library hash)."""
    out = {}
    for p in sorted(DIR.rglob("*.md")):
        m = _VERSION.search(p.read_text(encoding="utf-8"))
        if m:
            out[m.group(1)] = int(m.group(2))
    return out


def library_hash() -> str:
    h = hashlib.sha256()
    for p in sorted(DIR.rglob("*.md")):
        h.update(p.relative_to(DIR).as_posix().encode())
        h.update(p.read_bytes())
    return h.hexdigest()[:16]
