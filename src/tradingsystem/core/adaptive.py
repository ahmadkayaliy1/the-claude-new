"""The per-pair adaptive overlay (Phase 4, §3.8 component 2): bounded, expiring, logged values and a playbook that a
review session may change without a restart and without touching the reviewed config.

Files (``data/adaptive/<PAIR>/``, shared by every system of the data root; nothing in git):

* ``adaptive.yaml`` — the overlay (:class:`AdaptiveCfg`): per key an entry ``{value, set_ms, expires_ms, reason,
  evidence, window_hours, review_id}``; ``expires_ms ≤ set_ms + adaptive.max_expiry_days``.
* ``playbook.md`` — the pair's playbook; its entry in ``adaptive.yaml`` carries the text's hash, so a hand edit (or a
  torn write) is detected instead of reaching the model unlinted.
* ``changes.jsonl`` — the append-only history (set / revert by ``tools/tune.py``, ``expired`` by the services).

``tools/tune.py`` is the only writer of the first two (under ``FileLock(data/shared/locks/adaptive_<PAIR>.lock)``,
atomic replace) and of ``tuning_changes`` in the pair's app.db. The services read through :class:`AdaptiveStore`:
re-read on a file change (checked at most every ``adaptive.reload_check_s``), validated; an invalid file keeps the last
good values; an expired entry is simply absent (the config default applies again).

The overlay can only make the system more selective and slower (the direction column of §3.8), and every consumer
applies that direction again (:class:`Effective`: ``max``/``min`` against the config), so a hand-edited file cannot
loosen anything either. There are no risk or execution keys: sizing, stops, leverage, the gate's other checks and the
kill switch stay in ``config.yaml``.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, ValidationInfo, field_validator, model_validator

from .filelock import FileLock, locks_dir
from .playbook import MAX_HINT_CHARS, lint, lint_hint
from .settings import Settings, _UniqueKeyLoader
from . import timeutil
from .timeutil import MS_PER_DAY, iso

log = logging.getLogger("adaptive")

YAML_FILE, PLAYBOOK_FILE, CHANGES_FILE = "adaptive.yaml", "playbook.md", "changes.jsonl"
FREEZE_FILE = "TUNING_FREEZE"                 # data/TUNING_FREEZE: the user stops every tuning change
HARD_MAX_EXPIRY_DAYS = 30                     # the settings bound of adaptive.max_expiry_days
MAX_PAUSE_DAYS = 7                            # pair.ai_paused_until ≤ set time + 7 d
MAX_REASON_CHARS = 500
MAX_EVIDENCE_CHARS = 4000
MAX_EVIDENCE_DEPTH = 8                        # nested lists/objects inside one entry's evidence
MAX_YAML_DEPTH = 32                           # adaptive.yaml: root → group → entry → evidence (≤ 8) stays far below
READ_LOCK_WAIT_S = 0.25                       # a reader never waits longer (engine loop, executor placement lock)
REPLACE_RETRIES = 20                          # os.replace onto a file a reader holds open (Windows) — retry briefly
REPLACE_RETRY_S = 0.05

TUNING_DDL = (
    """CREATE TABLE IF NOT EXISTS tuning_changes (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL,
        pair TEXT NOT NULL, key TEXT NOT NULL, old_value TEXT, new_value TEXT, reason TEXT, evidence TEXT,
        window_hours INTEGER, expires_ms INTEGER, review_id TEXT, actor TEXT, reverted_ms INTEGER)""",
    "CREATE INDEX IF NOT EXISTS tuning_changes_pair_ts ON tuning_changes(pair, ts)",
)


# --------------------------------------------------------------------------- keys
@dataclass(frozen=True)
class KeySpec:
    """One tunable. ``direction``: the only way an autonomous change may move the *effective* value ("up" = raise
    only, "down" = lower only, None = free within the bounds). ``group``: which minimum sample count a change needs
    (``strategy`` = ``adaptive.min_samples_strategy``, ``activity`` = ``adaptive.min_samples_activity``)."""
    key: str
    kind: Literal["int", "float", "until", "text", "playbook"]
    lo: float | None
    hi: float | None
    direction: Literal["up", "down"] | None
    group: Literal["strategy", "activity"]
    consumer: str

    def default(self, s: Settings) -> Any:
        """The value in force when the overlay has no (unexpired) entry for this key."""
        return {
            "min_confidence_floor": lambda: s.risk.min_confidence,
            "min_minutes_between_calls": lambda: s.ai.min_minutes_between_calls,
            "max_idle_minutes": lambda: s.ai.max_idle_minutes,
            "review_floor_minutes": lambda: s.ai.review_floor_minutes,
            "trigger.weak_min": lambda: s.ai.weak_min,
            "trigger.liquidity_atr": lambda: s.ai.liquidity_atr,
            "pair.ai_paused_until": lambda: None,
            "tp_hint": lambda: "",
            "playbook": lambda: "",
        }[self.key]()

    def combine(self, config: Any, overlay: Any) -> Any:
        """The effective value: the direction rule applied against the config (a hand edit cannot loosen)."""
        if overlay is None:
            return config
        if self.direction == "up":
            return max(config, overlay)
        if self.direction == "down":
            return min(config, overlay)
        return overlay


KEYS: dict[str, KeySpec] = {k.key: k for k in (
    KeySpec("min_confidence_floor", "int", 55, 80, "up", "strategy", "executor gate: max(risk.min_confidence, floor)"),
    KeySpec("min_minutes_between_calls", "int", 15, 60, "up", "activity", "engine spacing"),
    KeySpec("max_idle_minutes", "int", 60, 240, "up", "activity", "engine idle review"),
    KeySpec("review_floor_minutes", "int", 5, 30, "up", "activity", "engine + next_review floor"),
    KeySpec("trigger.weak_min", "int", 2, 3, "up", "strategy", "setup screen: weak reasons needed"),
    KeySpec("trigger.liquidity_atr", "float", 0.2, 0.5, "down", "strategy", "setup screen: near-liquidity distance"),
    KeySpec("pair.ai_paused_until", "until", None, None, None, "activity", "engine skips dispatch (ai_paused)"),
    KeySpec("tp_hint", "text", None, MAX_HINT_CHARS, None, "strategy", "user prompt $tp_hint"),
    KeySpec("playbook", "playbook", None, None, None, "strategy", "user prompt $playbook"),
)}


# --------------------------------------------------------------------------- adaptive.yaml schema
class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Entry(_Strict):
    """One tuned value. Validate with ``context={"max_expiry_days": s.adaptive.max_expiry_days}`` (without it the hard
    maximum of 30 days applies)."""
    value: Any
    set_ms: int = Field(ge=0, strict=True)
    expires_ms: int = Field(ge=0, strict=True)
    reason: str = Field(min_length=1, max_length=MAX_REASON_CHARS)
    evidence: Any = None
    window_hours: int = Field(ge=1, le=720, strict=True)
    review_id: str | None = Field(None, max_length=120)

    @model_validator(mode="after")
    def _expiry(self, info: ValidationInfo) -> "Entry":
        days = int((info.context or {}).get("max_expiry_days", HARD_MAX_EXPIRY_DAYS))
        if self.expires_ms <= self.set_ms:
            raise ValueError("expires_ms must be after set_ms")
        if self.expires_ms > self.set_ms + days * MS_PER_DAY:
            raise ValueError(f"expires_ms more than {days} days after set_ms")
        if len(json.dumps(self.evidence, default=str)) > MAX_EVIDENCE_CHARS:
            raise ValueError(f"evidence longer than {MAX_EVIDENCE_CHARS} characters")
        if json_depth(self.evidence) > MAX_EVIDENCE_DEPTH:
            raise ValueError(f"evidence nested deeper than {MAX_EVIDENCE_DEPTH} levels")
        return self


def json_depth(v: Any) -> int:
    """Nesting depth of lists/dicts (a scalar is 0). Iterative: evidence text may nest deeper than the recursion limit
    before it is refused."""
    deepest, stack = 0, [(v, 1)]
    while stack:
        x, d = stack.pop()
        if isinstance(x, dict):
            x = list(x.values())
        if isinstance(x, (list, tuple)):
            deepest = max(deepest, d)
            stack.extend((y, d + 1) for y in x)
    return deepest


class FloorEntry(Entry):
    value: int = Field(ge=55, le=80, strict=True)


class SpacingEntry(Entry):
    value: int = Field(ge=15, le=60, strict=True)


class IdleEntry(Entry):
    value: int = Field(ge=60, le=240, strict=True)


class ReviewFloorEntry(Entry):
    value: int = Field(ge=5, le=30, strict=True)


class WeakMinEntry(Entry):
    value: int = Field(ge=2, le=3, strict=True)


class LiquidityEntry(Entry):
    value: float = Field(ge=0.2, le=0.5, allow_inf_nan=False)

    @field_validator("value", mode="before")
    @classmethod
    def _number(cls, v: Any) -> Any:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError("must be a number")
        return v


class PauseEntry(Entry):
    value: int = Field(ge=0, strict=True)            # UTC ms until which the engine dispatches no AI call

    @model_validator(mode="after")
    def _pause(self) -> "PauseEntry":
        if not self.set_ms < self.value <= self.set_ms + MAX_PAUSE_DAYS * MS_PER_DAY:
            raise ValueError(f"pair.ai_paused_until must be after set_ms and at most {MAX_PAUSE_DAYS} days later")
        return self


class HintEntry(Entry):
    value: str

    @field_validator("value")
    @classmethod
    def _lint(cls, v: str) -> str:
        problems = lint_hint(v)
        if problems:
            raise ValueError("; ".join(problems))
        return v


class PlaybookEntry(Entry):
    value: str = Field(pattern=r"^[0-9a-f]{16}$")   # text_hash() of playbook.md


class TriggerOverlay(_Strict):
    weak_min: WeakMinEntry | None = None
    liquidity_atr: LiquidityEntry | None = None


class PairOverlay(_Strict):
    ai_paused_until: PauseEntry | None = None


ENTRY_TYPES: dict[str, type[Entry]] = {
    "min_confidence_floor": FloorEntry, "min_minutes_between_calls": SpacingEntry, "max_idle_minutes": IdleEntry,
    "review_floor_minutes": ReviewFloorEntry, "trigger.weak_min": WeakMinEntry,
    "trigger.liquidity_atr": LiquidityEntry, "pair.ai_paused_until": PauseEntry, "tp_hint": HintEntry,
    "playbook": PlaybookEntry,
}


class AdaptiveCfg(_Strict):
    """The ``adaptive.yaml`` schema. ``extra="forbid"`` everywhere and deliberately no ``risk`` / ``execution``
    block: an unknown key makes the whole file invalid (the services keep the last good values)."""
    version: Literal[1] = 1
    min_confidence_floor: FloorEntry | None = None
    min_minutes_between_calls: SpacingEntry | None = None
    max_idle_minutes: IdleEntry | None = None
    review_floor_minutes: ReviewFloorEntry | None = None
    trigger: TriggerOverlay = TriggerOverlay()
    pair: PairOverlay = PairOverlay()
    tp_hint: HintEntry | None = None
    playbook: PlaybookEntry | None = None

    def entry(self, key: str) -> Entry | None:
        if key not in KEYS:
            raise KeyError(key)
        node: Any = self
        for part in key.split("."):
            node = getattr(node, part)
        return node

    def entries(self) -> dict[str, Entry]:
        """Every entry present (expired ones included), keyed by the dotted key."""
        return {k: e for k in KEYS if (e := self.entry(k)) is not None}

    def dump(self) -> dict[str, Any]:
        """The YAML document: absent entries, empty groups and unset entry fields left out (evidence kept as is)."""
        doc: dict[str, Any] = {"version": self.version}
        for key, e in self.entries().items():
            node = doc
            parts = key.split(".")
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = {k: v for k, v in e.model_dump().items() if v is not None}
        return doc

    def with_entry(self, key: str, entry: dict[str, Any] | Entry | None, *, max_expiry_days: int) -> "AdaptiveCfg":
        """A new overlay with ``key`` set (or removed with ``None``), validated."""
        if key not in KEYS:
            raise KeyError(key)
        doc = self.dump()
        parts = key.split(".")
        node = doc
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        if entry is None:
            node.pop(parts[-1], None)
        else:
            node[parts[-1]] = entry.model_dump() if isinstance(entry, Entry) else dict(entry)
        return parse_cfg(doc, max_expiry_days=max_expiry_days)


def parse_cfg(doc: Any, *, max_expiry_days: int) -> AdaptiveCfg:
    """Validate a loaded YAML document (None / empty = no overlay). Raises ValueError (pydantic's ValidationError)."""
    if doc is None:
        doc = {}
    if not isinstance(doc, dict):
        raise ValueError("adaptive.yaml must be a mapping")
    return AdaptiveCfg.model_validate(doc, context={"max_expiry_days": max_expiry_days})


MAX_YAML_CHARS = 256 * 1024              # tune.py writes a few kB; anything larger is not ours


def has_alias(text: str) -> bool:
    """Whether a YAML text uses an alias (``*name``), found on the event stream — nothing is constructed, so an
    alias bomb ("billion laughs") costs nothing to detect. Raises ValueError for a document nested deeper than
    ``MAX_YAML_DEPTH``: PyYAML's scanner slows down quadratically on deep flow nesting (one line of 256 K ``[`` would
    stall the engine loop for minutes), so the walk stops at the first level beyond the limit."""
    depth = 0
    try:
        for ev in yaml.parse(text, Loader=yaml.SafeLoader):
            if isinstance(ev, yaml.AliasEvent):
                return True
            if isinstance(ev, (yaml.SequenceStartEvent, yaml.MappingStartEvent)):
                depth += 1
                if depth > MAX_YAML_DEPTH:
                    raise ValueError("adaptive.yaml is nested too deeply")
            elif isinstance(ev, (yaml.SequenceEndEvent, yaml.MappingEndEvent)):
                depth -= 1
    except yaml.YAMLError:
        return False                           # not YAML at all: the loader refuses it quickly
    return False


def load_cfg_text(text: str | None, *, max_expiry_days: int) -> AdaptiveCfg:
    """Parse ``adaptive.yaml`` text (duplicate keys, aliases and oversized files refused — the engine reads it on
    its loop: a hand-made alias bomb must never stall a tick). Raises ValueError or yaml.YAMLError — also for a
    document nested so deeply that the (recursive) YAML composer hits the recursion limit."""
    if text is None or not text.strip():
        return AdaptiveCfg()
    if len(text) > MAX_YAML_CHARS:
        raise ValueError(f"adaptive.yaml is larger than {MAX_YAML_CHARS} characters")
    if has_alias(text):
        raise ValueError("adaptive.yaml must not use YAML aliases (*name)")
    try:
        return parse_cfg(yaml.load(text, Loader=_UniqueKeyLoader), max_expiry_days=max_expiry_days)
    except RecursionError:
        raise ValueError("adaptive.yaml is nested too deeply") from None


def dump_cfg(cfg: AdaptiveCfg) -> str:
    head = ("# Adaptive overlay of one pair (docs/learning_loop.md). Written only by tools/tune.py; every entry\n"
            "# expires. A hand edit is validated like any change: an invalid file keeps the last good values.\n")
    return head + yaml.safe_dump(cfg.dump(), sort_keys=False, allow_unicode=True, width=120)


# --------------------------------------------------------------------------- paths and small helpers
def adaptive_dir(s: Settings, pair: str) -> Path:
    """``data/adaptive/<PAIR>`` — under the shared data root, the same for the pair's own system and the all-pairs
    system."""
    return s.paths.data() / "adaptive" / pair


def lock_path(s: Settings, pair: str) -> Path:
    return locks_dir(s) / f"adaptive_{pair}.lock"


def freeze_path(s: Settings) -> Path:
    return s.paths.data() / FREEZE_FILE


def normalize_text(text: str) -> str:
    """Playbook / hint text as stored and hashed: LF line ends, no trailing blanks at the end."""
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def text_hash(text: str) -> str:
    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()[:16]


def atomic_write(path: Path, text: str) -> None:
    """Write-then-replace (fsync first); retried while another process holds the target open (Windows)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    try:
        for attempt in range(REPLACE_RETRIES):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == REPLACE_RETRIES - 1:
                    raise
                time.sleep(REPLACE_RETRY_S)
    finally:
        tmp.unlink(missing_ok=True)


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    """Append one line; a torn last line (crash mid-write) is closed first so the new line stays readable. The line
    is ASCII (non-ASCII characters escaped as ``\\uXXXX``): a raw U+2028 / U+2029 / U+0085 would be a line break to
    ``str.splitlines()`` in some reader and split the record."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=True, default=str, separators=(",", ":")) + "\n"
    with open(path, "a+b") as fh:
        fh.seek(0, os.SEEK_END)
        if fh.tell() > 0:
            fh.seek(-1, os.SEEK_END)
            if fh.read(1) != b"\n":
                line = "\n" + line
        fh.seek(0, os.SEEK_END)
        fh.write(line.encode("utf-8"))
        fh.flush()
        os.fsync(fh.fileno())


def read_changes(s: Settings, pair: str, limit: int | None = None) -> list[dict[str, Any]]:
    """The pair's ``changes.jsonl`` (oldest first, the last ``limit``); unreadable lines are skipped. Records are
    split on ``\\n`` only (not ``splitlines()``: a raw U+2028 in an older line must not cut it in two)."""
    try:
        text = (adaptive_dir(s, pair) / CHANGES_FILE).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in text.split("\n"):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out[-limit:] if limit else out


def ensure_tuning_table(con: sqlite3.Connection) -> None:
    for stmt in TUNING_DDL:
        con.execute(stmt)


# --------------------------------------------------------------------------- effective values
@dataclass(frozen=True)
class Effective:
    """What the consumers use. Defaults come from Settings when a key is absent or expired; the direction rule is
    applied against the config (``max`` / ``min``)."""
    min_confidence: int                # max(risk.min_confidence, floor) — the executor gate
    min_minutes_between_calls: int     # max
    max_idle_minutes: int              # max
    review_floor_minutes: int          # max
    weak_min: int                      # max
    liquidity_atr: float               # min
    ai_paused_until_ms: int | None     # set only while in the future
    tp_hint: str                       # "" when none
    playbook: str                      # "" when none (the orchestrator shows its own "(no playbook yet)")
    adaptive_hash: str | None          # 16 hex of the effective overlay values, None when nothing is set
    playbook_hash: str | None
    expires: tuple[tuple[str, int], ...] = ()   # (key, expires_ms) of every entry in force

    def as_detail(self) -> dict[str, Any]:
        """For status rows / the API (no texts: their length and hash)."""
        return {"min_confidence": self.min_confidence, "min_minutes_between_calls": self.min_minutes_between_calls,
                "max_idle_minutes": self.max_idle_minutes, "review_floor_minutes": self.review_floor_minutes,
                "weak_min": self.weak_min, "liquidity_atr": self.liquidity_atr,
                "ai_paused_until": iso(self.ai_paused_until_ms), "tp_hint": self.tp_hint or None,
                "playbook_chars": len(self.playbook), "adaptive_hash": self.adaptive_hash,
                "playbook_hash": self.playbook_hash, "expires": {k: iso(v) for k, v in self.expires}}


def defaults(s: Settings) -> Effective:
    """The effective values without any overlay."""
    return Effective(
        min_confidence=s.risk.min_confidence, min_minutes_between_calls=s.ai.min_minutes_between_calls,
        max_idle_minutes=s.ai.max_idle_minutes, review_floor_minutes=s.ai.review_floor_minutes,
        weak_min=s.ai.weak_min, liquidity_atr=s.ai.liquidity_atr, ai_paused_until_ms=None, tp_hint="", playbook="",
        adaptive_hash=None, playbook_hash=None)


def active_entries(cfg: AdaptiveCfg, now: int) -> tuple[dict[str, Entry], dict[str, Entry]]:
    """(entries in force, expired entries) at ``now``."""
    live, expired = {}, {}
    for key, e in cfg.entries().items():
        (expired if e.expires_ms <= now else live)[key] = e
    return live, expired


def compute_effective(s: Settings, cfg: AdaptiveCfg, playbook: str, now: int) -> Effective:
    """Pure: the effective values of ``cfg`` (+ the playbook text it references) at ``now``."""
    if not s.adaptive.enabled:
        return defaults(s)
    live, _ = active_entries(cfg, now)

    def val(key: str) -> Any:
        spec = KEYS[key]
        e = live.get(key)
        return spec.combine(spec.default(s), e.value if e is not None else None)

    pause = live.get("pair.ai_paused_until")
    paused = pause.value if pause is not None and pause.value > now else None
    values = {k: e.value for k, e in live.items() if k not in ("playbook", "pair.ai_paused_until")}
    if paused is not None:
        values["pair.ai_paused_until"] = paused
    pb = playbook if "playbook" in live else ""
    return Effective(
        min_confidence=val("min_confidence_floor"), min_minutes_between_calls=val("min_minutes_between_calls"),
        max_idle_minutes=val("max_idle_minutes"), review_floor_minutes=val("review_floor_minutes"),
        weak_min=val("trigger.weak_min"), liquidity_atr=val("trigger.liquidity_atr"), ai_paused_until_ms=paused,
        tp_hint=live["tp_hint"].value if "tp_hint" in live else "", playbook=pb,
        adaptive_hash=(hashlib.sha256(json.dumps(values, sort_keys=True, separators=(",", ":")).encode())
                       .hexdigest()[:16] if values else None),
        playbook_hash=text_hash(pb) if pb else None,
        expires=tuple(sorted((k, e.expires_ms) for k, e in live.items())))


# --------------------------------------------------------------------------- the services' reader
class AdaptiveStore:
    """One pair's overlay as the services see it. Cheap to call on every tick: the files are stat'ed at most every
    ``adaptive.reload_check_s`` and re-read only when they changed (mtime_ns + size). Never raises.

    ``app_db``: the pair's app.db — given by the process that owns the expiry bookkeeping (``tuning_changes
    .reverted_ms``); any store may write the single ``expired`` line (de-duplicated through ``changes.jsonl`` under
    the pair's lock, so the engine and the executor of one pair write it once). ``emit(kind, payload)``: an event
    sink such as ``Executor._emit`` (kinds ``adaptive_invalid`` / ``adaptive_expired``; payload has ``pair`` and
    ``text``)."""

    def __init__(self, s: Settings, pair: str, *, app_db: Path | None = None,
                 emit: Callable[[str, dict], None] | None = None, clock: Callable[[], float] = time.monotonic) -> None:
        self.s, self.pair = s, pair
        self.dir = adaptive_dir(s, pair)
        self.app_db = Path(app_db) if app_db is not None else None
        self._emit_fn, self._clock = emit, clock
        self._lock = threading.Lock()
        self._cfg = AdaptiveCfg()
        self._playbook = ""
        self._sig: tuple | None = None
        self._seen = False                   # _sig describes the files as last processed (valid or not)
        self._loaded = False                 # _cfg came from a successful read (not the start-up placeholder)
        self._checked: float | None = None
        self._invalid_sig: tuple | None = None
        self._expired_done: set[tuple[str, int]] = set()
        self._expire_retry_at = float("-inf")
        self._failed_once = False

    # ------------------------------------------------------------------ public
    def effective(self, now_ms: int | None = None) -> Effective:
        try:
            now = int(now_ms) if now_ms is not None else timeutil.now_ms()
            if not self.s.adaptive.enabled:
                return defaults(self.s)
            with self._lock:
                self._maybe_reload()
                cfg, pb = self._cfg, self._playbook
                _, expired = active_entries(cfg, now)
                todo = [(k, e) for k, e in expired.items() if (k, e.set_ms) not in self._expired_done]
                if todo and self._clock() >= self._expire_retry_at:
                    for key, e in todo:
                        if not self._expire(key, e, now):     # lock or database busy: not on every tick
                            self._expire_retry_at = self._clock() + self.s.adaptive.reload_check_s
                            break
                return compute_effective(self.s, cfg, pb, now)
        except Exception:  # noqa: BLE001 — a consumer (engine tick, executor gate) must never fail on the overlay
            if not self._failed_once:
                self._failed_once = True
                log.exception("%s: adaptive overlay unavailable - config defaults in force", self.pair)
            return defaults(self.s)

    @property
    def cfg(self) -> AdaptiveCfg:
        """The last good overlay (for display; consumers use :meth:`effective`)."""
        return self._cfg

    # ------------------------------------------------------------------ internals
    def _emit(self, kind: str, payload: dict) -> None:
        if self._emit_fn is None:
            return
        try:
            self._emit_fn(kind, payload)
        except Exception:  # noqa: BLE001
            log.warning("%s: %s event could not be recorded", self.pair, kind, exc_info=True)

    def _signature(self) -> tuple:
        out = []
        for name in (YAML_FILE, PLAYBOOK_FILE):
            try:
                st = (self.dir / name).stat()
                out.append((st.st_mtime_ns, st.st_size))
            except OSError:
                out.append(None)
        return tuple(out)

    def _maybe_reload(self) -> None:
        t = self._clock()
        if self._checked is not None and t - self._checked < self.s.adaptive.reload_check_s:
            return
        sig = self._signature()
        if self._seen and sig == self._sig:
            self._checked = t
            return
        if sig == (None, None):              # no overlay at all: nothing to read, no lock needed
            self._cfg, self._playbook, self._sig, self._checked = AdaptiveCfg(), "", sig, t
            self._seen, self._loaded, self._invalid_sig = True, True, None
            return
        with FileLock(lock_path(self.s, self.pair)).hold(timeout=READ_LOCK_WAIT_S, poll=0.02) as got:
            if not got:                      # tune.py is writing: keep the last good values, look again at the
                if self._loaded:             # next check (the signature still differs, so it is re-read then)
                    self._checked = t        # — but a store that has none yet (a service just started, config
                return                       # values as placeholder) tries again on its next call
            sig = self._signature()
            try:
                cfg, pb = self._read()
            except OSError as exc:           # transient (a replace in progress, access denied): next check
                log.debug("%s: adaptive files not readable yet: %s", self.pair, exc)
                self._checked = t
                return
            except (ValueError, yaml.YAMLError, RecursionError) as exc:
                self._sig, self._checked, self._seen = sig, t, True
                if sig != self._invalid_sig:
                    self._invalid_sig = sig
                    text = (f"{self.pair}: adaptive overlay invalid - keeping the last good values: "
                            f"{_short(exc)}")
                    log.warning(text)
                    self._emit("adaptive_invalid", {"pair": self.pair, "text": text})
                return
        self._cfg, self._playbook, self._sig, self._checked = cfg, pb, sig, t
        self._seen, self._loaded, self._invalid_sig = True, True, None

    def _read(self) -> tuple[AdaptiveCfg, str]:
        return read_files(self.s, self.pair, self.dir)


    def _expire(self, key: str, e: Entry, now: int) -> bool:
        """Once per entry across processes: tuning_changes.reverted_ms (with app_db), one ``expired`` line, one
        event. False when the lock or the database was busy (retried on a later call; the line is not repeated)."""
        wrote = db_ok = False
        with FileLock(lock_path(self.s, self.pair)).hold(timeout=READ_LOCK_WAIT_S, poll=0.02) as got:
            if not got:
                return False
            db_ok = self._mark_reverted(key, e, now)
            if not any(r.get("action") == "expired" and r.get("key") == key and r.get("set_ms") == e.set_ms
                       for r in read_changes(self.s, self.pair)):
                append_jsonl(self.dir / CHANGES_FILE, {
                    "ts": now, "iso": iso(now), "pair": self.pair, "action": "expired", "key": key,
                    "value": _display(key, e.value), "set_ms": e.set_ms, "expires_ms": e.expires_ms,
                    "actor": "system"})
                wrote = True
        if db_ok:
            self._expired_done.add((key, e.set_ms))
        if wrote:
            text = (f"{self.pair}: adaptive {key} = {_display(key, e.value)} expired (set {iso(e.set_ms)}) - "
                    f"back to the config value")
            log.info(text)
            self._emit("adaptive_expired", {"pair": self.pair, "key": key, "set_ms": e.set_ms,
                                            "expires_ms": e.expires_ms, "text": text})
        return db_ok

    def _mark_reverted(self, key: str, e: Entry, now: int) -> bool:
        if self.app_db is None or not self.app_db.exists():
            return True
        try:
            con = sqlite3.connect(f"file:{self.app_db.as_posix()}?mode=rw", uri=True, timeout=2.0)
            try:
                ensure_tuning_table(con)
                con.execute("UPDATE tuning_changes SET reverted_ms=? WHERE pair=? AND key=? AND ts=? "
                            "AND reverted_ms IS NULL AND new_value IS NOT NULL", (now, self.pair, key, e.set_ms))
                con.commit()
            finally:
                con.close()
            return True
        except sqlite3.Error as exc:
            log.warning("%s: tuning_changes not updated for the expired %s (retried): %s", self.pair, key, exc)
            return False


def read_files(s: Settings, pair: str, d: Path | None = None) -> tuple[AdaptiveCfg, str]:
    """(overlay, playbook text) of ``pair`` as on disk, side-effect free (the dashboard, the review pack). Raises
    ValueError / yaml.YAMLError for an invalid overlay or a playbook that does not match its hash."""
    d = d or adaptive_dir(s, pair)
    try:
        text = (d / YAML_FILE).read_text(encoding="utf-8")
    except FileNotFoundError:
        text = None
    cfg = load_cfg_text(text, max_expiry_days=s.adaptive.max_expiry_days)
    if cfg.playbook is None:
        return cfg, ""
    try:
        pb = normalize_text((d / PLAYBOOK_FILE).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ValueError("playbook.md is missing but adaptive.yaml references it") from None
    if text_hash(pb) != cfg.playbook.value:
        raise ValueError("playbook.md does not match the hash in adaptive.yaml (edited by hand?)")
    problems = lint(pb)
    if problems:
        raise ValueError("playbook.md: " + "; ".join(problems))
    return cfg, pb


def _short(exc: BaseException) -> str:
    if isinstance(exc, ValidationError):
        errs = exc.errors()
        return "; ".join(f"{'.'.join(str(p) for p in er.get('loc', ()))}: {er.get('msg')}" for er in errs[:3])[:400]
    return " ".join(str(exc).split())[:400]


def _display(key: str, value: Any) -> Any:
    """Short, readable form of a value for lines and messages."""
    if key == "pair.ai_paused_until" and isinstance(value, int):
        return iso(value)
    if isinstance(value, str) and len(value) > 80:
        return value[:77] + "..."
    return value


__all__ = ["AdaptiveCfg", "AdaptiveStore", "Effective", "Entry", "KEYS", "KeySpec", "active_entries", "adaptive_dir",
           "append_jsonl", "atomic_write", "compute_effective", "defaults", "dump_cfg", "ensure_tuning_table",
           "freeze_path", "load_cfg_text", "lock_path", "normalize_text", "parse_cfg", "read_changes", "text_hash"]
