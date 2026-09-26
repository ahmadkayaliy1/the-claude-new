"""Account-wide drawdown stop (D-042): every system trading one account shares the account's equity high-water mark.

When the account's equity falls ``risk.account_drawdown_stop_pct`` below its peak, the stop *trips*: every system's
risk gate refuses new trades until the user resets it (``scripts\\reset_drawdown_stop.bat``) — a recovery of the
floating PnL does not re-arm trading by itself. Open positions keep their stop losses; nothing is closed.

The file ``data/shared/account_peak.json`` holds one entry per account (``mt5:<server>:<login>``; a paper account per
system, ``paper:<instance>``), updated under a machine-wide file lock. A deposit raises the peak; a withdrawal looks
like a loss — reset the stop after one.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from ..core.filelock import FileLock
from ..core.timeutil import iso, now_ms

log = logging.getLogger("executor")
FILE = "account_peak.json"


@dataclass
class PeakState:
    key: str
    peak: float
    equity: float
    drawdown_pct: float
    tripped_ms: int | None

    @property
    def tripped(self) -> bool:
        return self.tripped_ms is not None

    def as_detail(self) -> dict:
        return {"account": self.key, "peak": round(self.peak, 2), "drawdown_pct": round(self.drawdown_pct, 2),
                "tripped": iso(self.tripped_ms) if self.tripped_ms else None}


class PeakFileError(RuntimeError):
    """The peak file exists but cannot be read — never treated as "no peak" (that would clear a tripped stop)."""


def _parse(path: Path) -> dict:
    doc = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        raise ValueError("not a JSON object")
    return doc


def _read(path: Path) -> dict:
    """The recorded accounts. Only a missing file means "none yet"; a corrupt file falls back to the copy kept at
    the previous write (``.bak``); anything else raises :class:`PeakFileError` (the gate then refuses — fail closed)."""
    try:
        return _parse(path)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        bak = path.with_name(path.name + ".bak")
        try:
            doc = _parse(bak)
        except (OSError, ValueError) as exc2:
            raise PeakFileError(f"{path} unreadable ({exc}) and no usable backup ({exc2}) - fix or delete it "
                                f"(deleting re-arms trading: the peak restarts at the current equity)") from exc
        log.error("%s unreadable (%s) - using the backup copy %s", path, exc, bak.name)
        return doc


def _write(path: Path, doc: dict) -> None:
    """Crash-safe: the new content is flushed to disk before it replaces the file; the previous file is kept."""
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(doc, indent=1, sort_keys=True))
        fh.flush()
        os.fsync(fh.fileno())
    if path.exists():
        try:
            shutil.copyfile(path, path.with_name(path.name + ".bak"))
        except OSError as exc:
            log.warning("could not keep a backup of %s: %s", path, exc)
    os.replace(tmp, path)


class AccountPeak:
    def __init__(self, shared_dir: Path, key: str, stop_pct: float) -> None:
        self.path = Path(shared_dir) / FILE
        self.lock = FileLock(Path(shared_dir) / "locks" / "account_peak.lock")
        self.key, self.stop_pct = key, stop_pct
        self.last: PeakState | None = None

    def update(self, equity: float) -> PeakState:
        """Record ``equity`` (raises the peak; trips the stop at the limit) and return the account's state. Raises
        when the lock is not obtained within 5 s or the file cannot be read (the caller fails closed)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock.hold(timeout=5) as got:
            if not got:
                raise PeakFileError(f"{self.lock.path} busy for 5 s - account peak not checked")
            doc = _read(self.path)
            e = doc.get(self.key) or {}
            peak = max(float(e.get("peak") or 0.0), equity)
            tripped = e.get("tripped_ms")
            dd = (peak - equity) / peak * 100 if peak > 0 else 0.0
            changed = peak != e.get("peak")
            if tripped is None and dd >= self.stop_pct:
                tripped, changed = now_ms(), True
                log.error("ACCOUNT DRAWDOWN STOP: %s equity %.2f is %.1f%% below its peak %.2f (limit %.0f%%) — "
                          "no new trades on any system until scripts\\reset_drawdown_stop.bat", self.key, equity, dd,
                          peak, self.stop_pct)
            if changed:
                doc[self.key] = {"peak": peak, "peak_ms": now_ms() if peak != e.get("peak") else e.get("peak_ms"),
                                 "tripped_ms": tripped, "tripped_equity": equity if tripped and not e.get("tripped_ms")
                                 else e.get("tripped_equity")}
                _write(self.path, doc)
        self.last = PeakState(self.key, peak, equity, dd, tripped)
        return self.last


def reset(shared_dir: Path, key: str | None = None) -> list[str]:
    """Re-arm trading: forget the peak and the trip of ``key`` (every account when None). Returns the keys reset."""
    path = Path(shared_dir) / FILE
    with FileLock(Path(shared_dir) / "locks" / "account_peak.lock").hold(timeout=10) as got:
        if not got:
            raise PeakFileError("the account peak file is busy - try again")
        try:
            doc, broken = _read(path), False
        except PeakFileError:
            doc, broken = {}, True          # unreadable: resetting everything is exactly what the user asked for
        keys = [k for k in doc if key is None or k == key]
        for k in keys:
            doc.pop(k)
        if keys or broken:
            _write(path, doc)
    return keys + (["(unreadable file replaced)"] if broken else [])


def main(argv: list[str] | None = None) -> int:
    from ..core.settings import load_settings
    ap = argparse.ArgumentParser(prog="python -m tradingsystem.execution.drawdown",
                                 description="show or reset the account-wide drawdown stop")
    ap.add_argument("--reset", action="store_true", help="re-arm trading (the peak restarts at the current equity)")
    ap.add_argument("--account", help="only this entry (default: all)")
    args = ap.parse_args(argv)
    shared = load_settings().paths.shared()
    try:
        doc = _read(shared / FILE)
    except PeakFileError as exc:
        print(f"!! {exc}")
        doc = {}
    for k, e in doc.items():
        state = f"TRIPPED {iso(e['tripped_ms'])} at equity {e.get('tripped_equity')}" if e.get("tripped_ms") else "armed"
        print(f"{k}: peak {e.get('peak')} ({iso(e['peak_ms']) if e.get('peak_ms') else '?'}) - {state}")
    if not doc:
        print("no account recorded yet")
    if args.reset:
        done = reset(shared, args.account)
        print(f"reset: {', '.join(done) or 'nothing to reset'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
