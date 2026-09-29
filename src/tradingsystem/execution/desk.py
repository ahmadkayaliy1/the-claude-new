"""Shadow desk (D-049 / B14): a pair with ``pairs.<PAIR>.desk.mode == "shadow"`` is analysed, gated and scored but never
traded. The executor checks the pair before any backend call (``Executor.handle``, ``_handle``, the management and
model-action steps); this module is the second, independent layer: :class:`ShadowGuard` wraps the execution backend
the executor uses and raises :class:`ShadowViolation` when a mutating call (place / modify / close / cancel) resolves
to a shadow pair, so a coding slip anywhere else still cannot reach the account.

Reads (account, quotes, specs, legs) pass through untouched — the shadow path needs the live account to gate an idea
in full, and reading changes nothing.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from ..core.settings import Settings

log = logging.getLogger("executor")

# methods that change the account: ``place`` adds risk (an unresolvable pair is refused); the others only ever reduce
# or tighten what exists (an unresolvable target passes — a shadow pair never has anything to reduce)
ADDING = frozenset({"place"})
REDUCING = frozenset({"cancel_decision", "modify_leg", "close_legs", "modify_sl", "modify_tp", "close_position",
                      "cancel_order"})
MUTATING = ADDING | REDUCING

# what marks a stored decision as a shadow idea (execution_detail JSON key) — the candidate query skips those
SHADOW_KEY = "shadow"


class ShadowViolation(RuntimeError):
    """A shadow pair's order would have reached the backend — never raised on a working path."""


def shadow_pairs(s: Settings) -> frozenset[str]:
    """Every configured pair (enabled in this system or not) whose desk is in shadow mode."""
    return frozenset(name for name, p in s.pairs.items() if p.desk is not None and p.desk.mode == "shadow")


class ShadowGuard:
    """Transparent wrapper of a backend (``PaperBackend`` / ``MT5Backend``). ``pair_of(method, args, kwargs)`` resolves
    the pair a mutating call acts on (None = cannot tell)."""

    def __init__(self, backend: Any, pairs: frozenset[str], pair_of: Callable[[str, tuple, dict], str | None]) -> None:
        object.__setattr__(self, "_backend", backend)
        object.__setattr__(self, "_pairs", frozenset(pairs))
        object.__setattr__(self, "_pair_of", pair_of)

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._backend, name)
        if name not in MUTATING or not callable(attr):
            return attr

        def guarded(*args: Any, **kwargs: Any) -> Any:
            self._check(name, args, kwargs)
            return attr(*args, **kwargs)

        return guarded

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._backend, name, value)

    def _check(self, name: str, args: tuple, kwargs: dict) -> None:
        try:
            pair = self._pair_of(name, args, kwargs)
        except Exception:  # noqa: BLE001 — an unresolvable call is judged by its kind below
            pair = None
        if pair is not None and pair in self._pairs:
            log.error("shadow guard: %s on shadow pair %s refused", name, pair)
            raise ShadowViolation(f"{name} on shadow pair {pair} refused: a desk in shadow never touches the account")
        if pair is None and name in ADDING:
            log.error("shadow guard: %s with an unresolvable pair refused", name)
            raise ShadowViolation(f"{name} refused: its pair could not be resolved, so it cannot be shown not to "
                                  "belong to a shadow desk")

    @property
    def wrapped(self) -> Any:
        return self._backend


def assert_not_shadow(pairs: frozenset[str], pair: str, what: str) -> None:
    """The executor's own check before any backend-touching helper acts on ``pair``."""
    if pair in pairs:
        raise ShadowViolation(f"{what} on shadow pair {pair} refused: a desk in shadow never touches the account")
