"""P1.6 — MT5 (Windsor) history depth per symbol × timeframe, and tick history depth.

Binary-searches the earliest month that returns bars (copy_rates_range, server-scale epoch seconds — never naive
datetimes) and the earliest day with ticks. The terminal downloads history on demand, so empty answers are
retried after a short wait. Records timings and terminal RSS.
"""
from __future__ import annotations

import datetime as dt
import sys
import time
from pathlib import Path

import MetaTrader5 as mt5
import psutil

sys.path.insert(0, str(Path(__file__).parent))
from _report import Report  # noqa: E402

from tradingsystem.ingest.mt5.servertime import ServerTimeModel  # noqa: E402

PATH = r"C:/Program Files/MetaTrader 5/terminal64.exe"
SYMBOLS = ["XAUUSD@", "BTCUSD@", "ETHUSD@"]
TFS = [("M1", mt5.TIMEFRAME_M1), ("M5", mt5.TIMEFRAME_M5), ("H1", mt5.TIMEFRAME_H1), ("D1", mt5.TIMEFRAME_D1)]
UTC = dt.timezone.utc


def srv(d: dt.date) -> int:
    """Server-scale epoch seconds of 00:00 server time on date d."""
    return int(dt.datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp())


def rates(sym: str, tf: int, d0: dt.date, d1: dt.date, retries: int = 4):
    for i in range(retries):
        r = mt5.copy_rates_range(sym, tf, srv(d0), srv(d1))
        if r is not None and len(r):
            return r
        time.sleep(1.5 * (i + 1))
    return None


def ticks_on(sym: str, d: dt.date, retries: int = 3) -> int:
    for i in range(retries):
        t = mt5.copy_ticks_range(sym, srv(d), srv(d) + 86400, mt5.COPY_TICKS_ALL)
        if t is not None and len(t):
            return len(t)
        time.sleep(1.0 * (i + 1))
    return 0


def month_add(d: dt.date, n: int) -> dt.date:
    y, m = divmod(d.month - 1 + n, 12)
    return dt.date(d.year + y, m + 1, 1)


def earliest_month(sym: str, tf: int, lo: dt.date, hi: dt.date) -> dt.date | None:
    """Earliest month in [lo, hi] with bars (assumes history is contiguous from its start)."""
    months = []
    d = lo
    while d <= hi:
        months.append(d)
        d = month_add(d, 1)
    if rates(sym, tf, months[-1], month_add(months[-1], 1)) is None:
        return None
    a, b = 0, len(months) - 1
    while a < b:
        mid = (a + b) // 2
        if rates(sym, tf, months[mid], month_add(months[mid], 1)) is not None:
            b = mid
        else:
            a = mid + 1
    return months[a]


def earliest_tick_day(sym: str, lo: dt.date, hi: dt.date) -> dt.date | None:
    def weekday(d: dt.date) -> dt.date:
        while d.weekday() >= 5:
            d += dt.timedelta(days=1)
        return d
    if not ticks_on(sym, weekday(hi - dt.timedelta(days=7))):
        return None
    a, b = lo.toordinal(), (hi - dt.timedelta(days=7)).toordinal()
    while a < b:
        mid = (a + b) // 2
        if ticks_on(sym, weekday(dt.date.fromordinal(mid))):
            b = mid
        else:
            a = mid + 1
    return dt.date.fromordinal(a)


def terminal_rss() -> float:
    for p in psutil.process_iter(["name"]):
        if (p.info["name"] or "").lower() == "terminal64.exe":
            return p.memory_info().rss / 2**20
    return float("nan")


def main() -> None:
    if not mt5.initialize(path=PATH):
        raise SystemExit(mt5.last_error())
    model = ServerTimeModel()
    rep = Report("probe_mt5_history", "MetaTrader 5 (Windsor) — actual history depth (P1.6)")
    rep.p(f"Terminal maxbars = {mt5.terminal_info().maxbars:,}; terminal RSS at start {terminal_rss():.0f} MB.")
    today = dt.date.today()
    rows = []
    for sym in SYMBOLS:
        mt5.symbol_select(sym, True)
        for name, tf in TFS:
            t0 = time.time()
            first = earliest_month(sym, tf, dt.date(2000, 1, 1), today.replace(day=1))
            n = None
            if first is not None:
                r = rates(sym, tf, first, month_add(first, 1))
                first_bar = int(r["time"][0]) * 1000 if r is not None else None
                utc_first = model.server_to_utc(first_bar, prefer="earlier") if first_bar else None
                n_recent = rates(sym, tf, month_add(today.replace(day=1), -1), today.replace(day=1))
                n = len(n_recent) if n_recent is not None else 0
            else:
                utc_first = None
            rows.append([sym, name, dt.datetime.fromtimestamp(utc_first / 1000, UTC).strftime("%Y-%m-%d %H:%M")
                         if utc_first else "none", n, round(time.time() - t0, 1)])
            print(rows[-1], flush=True)
    rep.h("Earliest bar per timeframe (UTC)")
    rep.table(["symbol", "tf", "earliest bar", "bars in last full month", "search s"], rows)
    trows = []
    for sym in SYMBOLS:
        t0 = time.time()
        first = earliest_tick_day(sym, dt.date(2010, 1, 1), today)
        dens = []
        if first:
            for back in (0, 1, 2):
                d = dt.date(max(first.year, today.year - back), 6, 15)
                while d.weekday() >= 5:
                    d += dt.timedelta(days=1)
                if d >= first:
                    dens.append(f"{d}: {ticks_on(sym, d):,}")
        trows.append([sym, str(first) if first else "none", "; ".join(dens), round(time.time() - t0, 1)])
        print(trows[-1], flush=True)
    rep.h("Earliest tick day (server date) and sample daily tick counts")
    rep.table(["symbol", "earliest ticks", "sample days", "search s"], trows)
    rep.p(f"Terminal RSS after the history requests: {terminal_rss():.0f} MB. History is downloaded by the terminal "
          "on demand and cached in its `bases` folder.")
    rep.raw = {"bars": rows, "ticks": trows}
    mt5.shutdown()
    rep.save()


if __name__ == "__main__":
    main()
