"""P1.10 — Trading-session calendar per MT5 symbol, derived from real M1 history.

Recurring gaps between consecutive M1 bars (> 5 min) are grouped by (UTC weekday, start, end); gaps that
recur most weeks are session breaks (daily maintenance break, weekend). Server times are converted with the
measured ServerTimeModel (P1.7).
"""
from __future__ import annotations

import collections
import datetime as dt
import sys
from pathlib import Path

import MetaTrader5 as mt5
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from _report import Report  # noqa: E402

from tradingsystem.core.timeutil import iso  # noqa: E402
from tradingsystem.ingest.mt5.servertime import ServerTimeModel  # noqa: E402

PATH = r"C:/Program Files/MetaTrader 5/terminal64.exe"
SYMBOLS = ["XAUUSD@", "BTCUSD@", "ETHUSD@"]
UTC = dt.timezone.utc


def main() -> None:
    if not mt5.initialize(path=PATH):
        raise SystemExit(f"MT5 initialize failed: {mt5.last_error()}")
    model = ServerTimeModel()
    rep = Report("probe_sessions", "MT5 trading sessions derived from M1 history (P1.10)")
    n = mt5.terminal_info().maxbars - 1
    for s in SYMBOLS:
        mt5.symbol_select(s, True)
        r = mt5.copy_rates_from_pos(s, mt5.TIMEFRAME_M1, 0, n)
        if r is None or not len(r):
            rep.p(f"{s}: no M1 history ({mt5.last_error()})")
            continue
        utc = np.array([model.server_to_utc(int(t) * 1000, prefer="earlier") for t in r["time"]], dtype=np.int64)
        weeks = (utc[-1] - utc[0]) / (7 * 86_400_000)
        gaps = collections.Counter()
        examples: dict[tuple, list[str]] = collections.defaultdict(list)
        for i in np.nonzero(np.diff(utc) > 5 * 60_000)[0]:
            a = dt.datetime.fromtimestamp((utc[i] + 60_000) / 1000, tz=UTC)   # first missing minute
            b = dt.datetime.fromtimestamp(utc[i + 1] / 1000, tz=UTC)          # first minute back
            k = (a.strftime("%a %H:%M"), b.strftime("%a %H:%M"))
            gaps[k] += 1
            if len(examples[k]) < 2:
                examples[k].append(a.strftime("%Y-%m-%d"))
        rep.h(f"{s}")
        rep.p(f"M1 bars: {len(r)} from {iso(int(utc[0]))} to {iso(int(utc[-1]))} (UTC), ≈{weeks:.1f} weeks. "
              f"Gap groups: {len(gaps)} (showing those recurring ≥ 2 times).")
        rows = [[a, b, c, round(c / max(weeks, 1) * 100), ", ".join(examples[(a, b)])]
                for (a, b), c in gaps.most_common() if c >= 2][:25]
        rep.table(["closed from (UTC)", "reopens (UTC)", "occurrences", "% of weeks", "e.g."], rows)
        rep.raw[s] = {f"{a} → {b}": c for (a, b), c in gaps.most_common()}
    mt5.shutdown()
    rep.p("Notes: the M1 window is limited by the terminal `maxbars` setting (H1 → P1.6). A daily gap in the table "
          "is a session break; one-off gaps (occurrences = 1) are holidays or quiet minutes and are listed in the raw "
          "JSON only.")
    rep.save()


if __name__ == "__main__":
    main()
