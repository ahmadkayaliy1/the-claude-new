"""Integrity report for a data directory: per table row counts, candle-grid gaps, aggTrade id gaps, duplicates.

Usage: python tools/check_integrity.py [DATA_DIR] [--since-hours N]
Reads only (hot SQLite + cold Parquet via InstrumentReader). Exit code 1 when any unexplained gap exists.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

from tradingsystem.core.instruments import InstrumentRegistry
from tradingsystem.core.sessions import calendar_for
from tradingsystem.core.settings import load_settings
from tradingsystem.core.timeutil import iso, now_ms
from tradingsystem.storage.gaps import candle_gaps, id_gaps
from tradingsystem.storage.reader import InstrumentReader
from tradingsystem.storage.tablespec import table_specs


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    since_h = float(sys.argv[sys.argv.index("--since-hours") + 1]) if "--since-hours" in sys.argv else None
    s = load_settings()
    data_dir = Path(args[0]) if args else s.paths.data()
    reg = InstrumentRegistry.from_settings(s)
    bad = 0
    for inst in reg.all():
        if not inst.hot_db_path(data_dir).exists():
            continue
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
            if spec.datatype == "candles" and spec.timeframe is not None:
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
    print("OK" if not bad else f"{bad} problem(s)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
