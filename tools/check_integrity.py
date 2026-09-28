"""Integrity report for a data directory: per table row counts, candle-grid gaps, aggTrade id gaps, duplicates.

Usage: python tools/check_integrity.py [DATA_DIR] [--since-hours N]
Reads only (hot SQLite + cold Parquet via InstrumentReader). Exit code 1 when any unexplained gap exists, 2 when the
data directory does not exist or holds no hot store of a configured instrument (nothing checked is never "OK": until
Phase 5 the value of --since-hours was taken as DATA_DIR and such a run printed OK after checking nothing).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

from tradingsystem.core.instruments import InstrumentRegistry
from tradingsystem.core.sessions import calendar_for
from tradingsystem.core.settings import load_settings
from tradingsystem.core.timeutil import iso, now_ms
from tradingsystem.ingest.mt5.convert import mt5_candle_gaps
from tradingsystem.ingest.mt5.servertime import ServerTimeModel
from tradingsystem.storage.gaps import candle_gaps, id_gaps
from tradingsystem.storage.reader import InstrumentReader
from tradingsystem.storage.tablespec import table_specs


def parse_args(argv: list[str]) -> argparse.Namespace:
    """``[DATA_DIR] [--since-hours N]`` in any order."""
    ap = argparse.ArgumentParser(description="Integrity report of a data directory (read-only).")
    ap.add_argument("data_dir", nargs="?", help="default: the configured data directory")
    ap.add_argument("--since-hours", type=float, default=None, help="only rows of the last N hours")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    a = parse_args(sys.argv[1:] if argv is None else argv)
    since_h = a.since_hours
    s = load_settings()
    data_dir = Path(a.data_dir) if a.data_dir else s.paths.data()
    if not data_dir.is_dir():
        print(f"not a directory: {data_dir}")
        return 2
    reg = InstrumentRegistry.from_settings(s)
    bad = checked = 0
    for inst in reg.all():
        if not inst.hot_db_path(data_dir).exists():
            continue
        checked += 1
        rd = InstrumentReader(inst, data_dir)
        cal = calendar_for(inst.venue, inst.symbol, s.pairs[inst.pair].asset_class)
        print(f"== {inst.key}")
        for spec in table_specs(inst):
            start = now_ms() - int(since_h * 3_600_000) if since_h else None
            cols = rd.read_range(spec, start, None, columns=list(spec.key))
            n = len(cols[spec.key[0]])
            if not n:
                continue
            k = cols[spec.key[0]].astype(np.int64)
            dup = n - len(np.unique(k)) if len(spec.key) == 1 else 0
            msg = f"   {spec.name:<28} rows={n:>9,} first={iso(int(k.min()) if spec.datatype == 'candles' else None) or ''}"
            if spec.datatype == "candles" and spec.timeframe is not None and inst.venue == "mt5":
                srv = rd.read_range(spec, start, None, columns=["srv_time"])["srv_time"].astype(np.int64)
                gaps = mt5_candle_gaps(srv, spec.timeframe, int(srv.min()), int(srv.max()), cal, ServerTimeModel())
                msg += f" gaps={len(gaps)}" + (f" (first srv {iso(gaps[0].start)} ×{gaps[0].count})" if gaps else "")
                bad += len(gaps)
            elif spec.datatype == "candles" and spec.timeframe is not None:
                last_closed = spec.timeframe.floor(now_ms()) - spec.timeframe.ms
                gaps = candle_gaps(k, spec.timeframe, int(k.min()), min(int(k.max()), last_closed), cal)
                msg += f" gaps={len(gaps)}" + (f" (first {iso(gaps[0].start)} ×{gaps[0].count})" if gaps else "")
                bad += len(gaps)
            elif spec.datatype == "agg_trades":
                gaps = id_gaps(k)
                msg += f" id_gaps={len(gaps)}" + (f" (first {gaps[0].start}..{gaps[0].end})" if gaps else "")
                bad += len(gaps)
            if dup:
                msg += f" DUPLICATES={dup}"
                bad += dup
            print(msg)
        rd.close()
    if not checked:
        print(f"no hot store of a configured instrument under {data_dir} - nothing checked")
        return 2
    print("OK" if not bad else f"{bad} problem(s)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
