"""P4.4 verification: stored MT5 ticks vs a fresh ``copy_ticks_range`` re-fetch of the same window.

Usage: python tools/verify_mt5_ticks.py DATA_DIR [SYMBOL ...]
Reports, per symbol, whether the stored key set equals the re-fetched key set (no loss, no duplicates).
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import MetaTrader5 as mt5
import numpy as np

from tradingsystem.ingest.mt5.convert import ticks_to_rows
from tradingsystem.ingest.mt5.servertime import MonotonicServerClock, ServerTimeModel


def main() -> int:
    data = Path(sys.argv[1])
    symbols = sys.argv[2:] or ["XAUUSD@", "BTCUSD@", "ETHUSD@"]
    if not mt5.initialize(path=r"C:/Program Files/MetaTrader 5/terminal64.exe"):
        raise SystemExit(mt5.last_error())
    bad = 0
    for sym in symbols:
        stem = sym.replace("@", "").upper()
        con = sqlite3.connect(data / "hot" / "mt5" / f"{stem}.db")
        tbl = f"{stem.lower()}_ticks"
        rows = con.execute(f"SELECT key, srv_msc FROM {tbl} ORDER BY key").fetchall()
        if not rows:
            print(sym, "no stored ticks")
            continue
        keys = np.array([r[0] for r in rows], dtype=np.int64)
        srv = np.array([r[1] for r in rows], dtype=np.int64)
        lo, hi = int(srv.min()), int(srv.max())
        # re-fetch from the start of the first stored second so same-ms sequence numbers line up
        ref = mt5.copy_ticks_range(sym, lo // 1000, hi // 1000 + 1, mt5.COPY_TICKS_ALL)
        ref_rows = ticks_to_rows(ref, MonotonicServerClock(ServerTimeModel()))
        ref_keys = np.array([r[0] for r in ref_rows if lo <= r[2] <= hi], dtype=np.int64)
        missing = np.setdiff1d(ref_keys, keys)
        extra = np.setdiff1d(keys, ref_keys)
        dup = len(keys) - len(np.unique(keys))
        ok = len(missing) == 0 and len(extra) == 0 and dup == 0
        bad += not ok
        print(f"{sym}: stored={len(keys):,} refetch={len(ref_keys):,} missing={len(missing)} extra={len(extra)} "
              f"dup={dup} → {'IDENTICAL' if ok else 'MISMATCH'}")
    mt5.shutdown()
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
