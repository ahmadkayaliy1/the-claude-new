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


def _read(path: Path) -> dict:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(path: Path, doc: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(doc, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


class AccountPeak:
    def __init__(self, shared_dir: Path, key: str, stop_pct: float) -> None:
        self.path = Path(shared_dir) / FILE
        self.lock = FileLock(Path(shared_dir) / "locks" / "account_peak.lock")
        self.key, self.stop_pct = key, stop_pct
        self.last: PeakState | None = None

    def update(self, equity: float) -> PeakState:
        """Record ``equity`` (raises the peak; trips the stop at the limit) and return the account's state. A lock
        not obtained within 5 s leaves the file alone: the state is then computed from what the file says."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock.hold(timeout=5) as got:
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
            if changed and got:
                doc[self.key] = {"peak": peak, "peak_ms": now_ms() if peak != e.get("peak") else e.get("peak_ms"),
                                 "tripped_ms": tripped, "tripped_equity": equity if tripped and not e.get("tripped_ms")
                                 else e.get("tripped_equity")}
                _write(self.path, doc)
        self.last = PeakState(self.key, peak, equity, dd, tripped)
        return self.last


def reset(shared_dir: Path, key: str | None = None) -> list[str]:
    """Re-arm trading: forget the peak and the trip of ``key`` (every account when None). Returns the keys reset."""
    path = Path(shared_dir) / FILE
    with FileLock(Path(shared_dir) / "locks" / "account_peak.lock").hold(timeout=10):
        doc = _read(path)
        keys = [k for k in doc if key is None or k == key]
        for k in keys:
            doc.pop(k)
        if keys:
            _write(path, doc)
    return keys


def main(argv: list[str] | None = None) -> int:
    from ..core.settings import load_settings
    ap = argparse.ArgumentParser(prog="python -m tradingsystem.execution.drawdown",
                                 description="show or reset the account-wide drawdown stop")
    ap.add_argument("--reset", action="store_true", help="re-arm trading (the peak restarts at the current equity)")
    ap.add_argument("--account", help="only this entry (default: all)")
    args = ap.parse_args(argv)
    shared = load_settings().paths.shared()
    doc = _read(shared / FILE)
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
