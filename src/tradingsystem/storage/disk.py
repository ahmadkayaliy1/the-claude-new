"""Free-disk guards shared by the writers (Phase 5 A7).

* Backfills (Binance Vision and the MT5 history worker) stop — without touching live capture — while the free disk
  is below ``storage.min_free_disk_gb``: :func:`require_free` raises :class:`DiskFullError`, the pass ends as
  ``disk_full`` and is retried after the longest backoff.
* The live writers' hot → cold rollover waits while the free disk is below ``storage.cold_archive_min_free_gb``
  (``retention.ColdArchiveGuard``): the rows stay in the hot store and a later rollover moves them.
"""
from __future__ import annotations

import shutil
from pathlib import Path


class DiskFullError(RuntimeError):
    """Free disk below the configured floor: the writer pauses (never live capture)."""


def usage_path(path: Path) -> Path:
    """The nearest existing ancestor of ``path`` (``shutil.disk_usage`` needs an existing path; a data root that
    was never written to still names its drive)."""
    p = Path(path)
    while not p.exists() and p.parent != p:
        p = p.parent
    return p


def free_gb(path: Path) -> float:
    return shutil.disk_usage(usage_path(path)).free / 2**30


def require_free(path: Path, min_free_gb: float, need_bytes: int = 0, what: str = "backfill") -> float:
    """Free GB on ``path``'s drive; raises :class:`DiskFullError` when it (minus the next write of ``need_bytes``)
    is below ``min_free_gb`` (0 disables the check)."""
    free = free_gb(path)
    if min_free_gb > 0 and free - need_bytes / 2**30 < min_free_gb:
        raise DiskFullError(f"free disk {free:.1f} GB (next write {need_bytes / 2**20:.0f} MB) < {min_free_gb} GB — "
                            f"{what} paused")
    return free
