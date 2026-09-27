"""A pair's effective tunables: the adaptive overlay (Phase 4, D-039, :mod:`.adaptive`) over the configuration.

Every consumer (engine spacing/idle/trigger thresholds, the executor's confidence floor, the orchestrator's playbook,
take-profit hint and hashes) asks :class:`Tunables` instead of reading ``Settings`` directly. Without an overlay —
``adaptive.enabled: false``, no files, an unreadable file, the module failing to load — the values are exactly the
configured ones, so a missing or broken overlay can never stop a service or loosen a limit.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .settings import Settings

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ConfigTunables:
    """The configured values (the shape of :class:`.adaptive.Effective`)."""
    min_confidence: int
    min_minutes_between_calls: int
    max_idle_minutes: int
    review_floor_minutes: int
    weak_min: int
    liquidity_atr: float
    ai_paused_until_ms: int | None = None
    tp_hint: str = ""
    playbook: str = ""
    adaptive_hash: str | None = None
    playbook_hash: str | None = None


def config_tunables(s: Settings) -> ConfigTunables:
    return ConfigTunables(min_confidence=s.risk.min_confidence,
                          min_minutes_between_calls=s.ai.min_minutes_between_calls,
                          max_idle_minutes=s.ai.max_idle_minutes, review_floor_minutes=s.ai.review_floor_minutes,
                          weak_min=s.ai.weak_min, liquidity_atr=s.ai.liquidity_atr)


class Tunables:
    """One :class:`.adaptive.AdaptiveStore` per pair, created on first use (per process)."""

    def __init__(self, s: Settings, *, app_db: Path | None = None,
                 emit: Callable[[str, dict], None] | None = None) -> None:
        self.s, self.app_db, self.emit = s, app_db, emit
        self._stores: dict[str, Any] = {}
        self._warned: set[str] = set()

    def _store(self, pair: str):
        if pair not in self._stores:
            store = None
            if self.s.adaptive.enabled:
                try:
                    from .adaptive import AdaptiveStore
                    store = AdaptiveStore(self.s, pair, app_db=self.app_db, emit=self.emit)
                except Exception:  # noqa: BLE001 — no overlay is the configured behaviour, never an outage
                    log.exception("adaptive overlay of %s unavailable — configured values used", pair)
            self._stores[pair] = store
        return self._stores[pair]

    def get(self, pair: str | None, now_ms: int | None = None):
        """The pair's effective values (the configured ones for ``None``, without an overlay, or on any error)."""
        if pair is None:
            return config_tunables(self.s)
        store = self._store(pair)
        if store is not None:
            try:
                return store.effective(now_ms)
            except Exception:  # noqa: BLE001
                if pair not in self._warned:
                    self._warned.add(pair)
                    log.exception("adaptive overlay of %s failed — configured values used", pair)
        return config_tunables(self.s)


def tunables_of(owner: Any, pair: str | None, now_ms: int | None = None):
    """``owner.tunables.get(pair)`` when the owner has one (engine, executor, orchestrator), else the configuration
    (objects built bare in tests have no ``tunables``)."""
    t = getattr(owner, "tunables", None)
    return t.get(pair, now_ms) if t is not None else config_tunables(owner.s)


__all__ = ["ConfigTunables", "Tunables", "config_tunables", "tunables_of"]
