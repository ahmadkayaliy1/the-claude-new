"""Cross-process locks on a file (D-042: several independent systems share one MT5 terminal, one Claude login and
one account): ``msvcrt.locking`` on Windows, ``fcntl.flock`` elsewhere. The OS drops the lock when its holder dies,
so a crashed process never leaves a stale lock behind. Not re-entrant: do not nest the same lock in one process.
"""
from __future__ import annotations

import contextlib
import os
import time
from pathlib import Path
from typing import IO, Iterator

if os.name == "nt":
    import msvcrt

    def _try(fh: IO[bytes]) -> bool:
        fh.seek(0)
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def _unlock(fh: IO[bytes]) -> None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
else:  # pragma: no cover — the system runs on Windows; kept for tooling on other platforms
    import fcntl

    def _try(fh: IO[bytes]) -> bool:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def _unlock(fh: IO[bytes]) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


class FileLock:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._fh: IO[bytes] | None = None

    @property
    def held(self) -> bool:
        return self._fh is not None

    def acquire(self, timeout: float | None = 0.0, poll: float = 0.1) -> bool:
        """True once held; False after ``timeout`` seconds (0 = one try, None = wait forever)."""
        if self._fh is not None:
            raise RuntimeError(f"{self.path} is already held by this object")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+b")                 # noqa: SIM115 — kept open while held
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if _try(fh):
                self._fh = fh
                return True
            if deadline is not None and time.monotonic() >= deadline:
                fh.close()
                return False
            time.sleep(poll)

    def release(self) -> None:
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            _unlock(fh)
        except OSError:
            pass
        finally:
            fh.close()

    @contextlib.contextmanager
    def hold(self, timeout: float | None = 0.0, poll: float = 0.1) -> Iterator[bool]:
        """``with lock.hold(5) as got:`` — ``got`` says whether the lock is held inside the block."""
        got = self.acquire(timeout, poll)
        try:
            yield got
        finally:
            if got:
                self.release()


def locks_dir(settings) -> Path:
    """``data/shared/locks`` — locks every system on this machine agrees on (any instance, or the single system)."""
    return settings.paths.shared() / "locks"


__all__ = ["FileLock", "locks_dir"]
