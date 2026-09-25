"""P1.7 — MT5 server-time model.

1. Live offset: newest crypto tick time (server scale) vs true UTC (local clock corrected by Binance).
2. Historical offset per ISO week: correlate BTCUSD@ H1 returns (server time) with Binance BTCUSDT 1h
   returns (UTC) for shifts k ∈ [-4, +6] h; best k per week.
3. XAUUSD@ weekly close (server time) vs 17:00 New York.
Output feeds ``tradingsystem.ingest.mt5.servertime`` (P4.2).
"""
from __future__ import annotations

import collections
import datetime as dt
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import MetaTrader5 as mt5
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from _report import Report  # noqa: E402

from tradingsystem.core.timeutil import MS_PER_HOUR, iso, now_ms  # noqa: E402

PATH = r"C:/Program Files/MetaTrader 5/terminal64.exe"
UTC = dt.timezone.utc
NY = ZoneInfo("America/New_York")
SHIFTS = list(range(-4, 7))


def binance_offset(c: httpx.Client) -> float:
    best = None
    for _ in range(5):
        t0 = now_ms()
        srv = c.get("https://api.binance.com/api/v3/time").json()["serverTime"]
        t1 = now_ms()
        if best is None or t1 - t0 < best[1]:
            best = (srv - (t0 + t1) / 2, t1 - t0)
    return best[0]


def binance_h1(c: httpx.Client, start_ms: int) -> dict[int, float]:
    out: dict[int, float] = {}
    t = start_ms
    while True:
        rows = c.get("https://api.binance.com/api/v3/klines",
                     params={"symbol": "BTCUSDT", "interval": "1h", "startTime": t, "limit": 1000}).json()
        if not rows:
            break
        for r in rows:
            out[r[0] // 1000] = float(r[4])
        if len(rows) < 1000:
            break
        t = rows[-1][0] + MS_PER_HOUR
    return out


def main() -> None:
    if not mt5.initialize(path=PATH):
        raise SystemExit(f"MT5 initialize failed: {mt5.last_error()}")
    rep = Report("probe_mt5_time", "MetaTrader 5 (Windsor) — server time model (P1.7)")
    with httpx.Client(timeout=30) as c:
        off = binance_offset(c)
        # ---- 1. live offset
        rows = []
        for s in ("BTCUSD@", "ETHUSD@", "XAUUSD@"):
            mt5.symbol_select(s, True)
            tick = mt5.symbol_info_tick(s)
            true_utc = now_ms() + off
            diff_h = (tick.time_msc - true_utc) / MS_PER_HOUR
            rows.append([s, iso(tick.time_msc), iso(int(true_utc)), round(diff_h, 4), round(diff_h * 2) / 2])
        rep.h("Live offset (server-scale tick time − true UTC)")
        rep.table(["symbol", "last tick (server scale)", "true UTC now", "diff h", "rounded"], rows)
        rep.p(f"Local clock correction applied: Binance − local = {off:.0f} ms. Stale ticks (closed market) show a "
              "larger negative residual; the crypto CFDs trade continuously and give the clean reading.")

        # ---- 2. historical per-week offset
        rates = mt5.copy_rates_from_pos("BTCUSD@", mt5.TIMEFRAME_H1, 0, mt5.terminal_info().maxbars - 1)
        if rates is None or len(rates) == 0:
            raise SystemExit(f"no BTCUSD@ H1 history: {mt5.last_error()}")
        t_srv = rates["time"].astype(np.int64)
        close = rates["close"].astype(float)
        start = int(t_srv[0] - 8 * 3600) * 1000
        bn = binance_h1(c, max(start, 1502942400000))
    rep.h("History available")
    rep.p(f"BTCUSD@ H1 bars: {len(rates)} from {iso(int(t_srv[0]) * 1000)} (server scale) to "
          f"{iso(int(t_srv[-1]) * 1000)}; Binance BTCUSDT 1h bars fetched: {len(bn)}. Terminal maxbars limits "
          "this depth (H1 → P1.6).")

    # returns on consecutive bars only
    consec = np.diff(t_srv) == 3600
    r_mt5 = np.diff(np.log(close))
    weeks: dict[str, dict[int, list[tuple[float, float]]]] = collections.defaultdict(lambda: collections.defaultdict(list))
    for i in np.nonzero(consec)[0]:
        t1 = int(t_srv[i + 1])
        wk = dt.datetime.fromtimestamp(t1, tz=UTC).strftime("%G-W%V")
        for k in SHIFTS:
            a, b = bn.get(t1 - k * 3600), bn.get(t1 - k * 3600 - 3600)
            if a and b:
                weeks[wk][k].append((r_mt5[i], np.log(a / b)))
    table = []
    for wk in sorted(weeks):
        corrs = {}
        for k, pairs in weeks[wk].items():
            if len(pairs) >= 24:
                x, y = np.array(pairs).T
                if x.std() > 0 and y.std() > 0:
                    corrs[k] = float(np.corrcoef(x, y)[0, 1])
        if not corrs:
            continue
        ranked = sorted(corrs.items(), key=lambda kv: -kv[1])
        best_k, best_c = ranked[0]
        second = ranked[1][1] if len(ranked) > 1 else float("nan")
        table.append((wk, best_k, best_c, second, len(weeks[wk][best_k])))
    # compress into regimes
    regimes = []
    for wk, k, cbest, c2, n in table:
        if regimes and regimes[-1]["k"] == k:
            regimes[-1]["to"] = wk
            regimes[-1]["weeks"] += 1
            regimes[-1]["min_corr"] = min(regimes[-1]["min_corr"], cbest)
        else:
            regimes.append({"k": k, "from": wk, "to": wk, "weeks": 1, "min_corr": cbest})
    rep.h("Historical offset regimes (best-correlated shift per ISO week)")
    rep.table(["from week", "to week", "weeks", "server = UTC+k", "min best corr"],
              [[r["from"], r["to"], r["weeks"], r["k"], round(r["min_corr"], 3)] for r in regimes])
    weak = [t for t in table if t[2] < 0.8 or (t[2] - t[3]) < 0.2]
    rep.p(f"Weeks analysed: {len(table)}; weeks with weak identification (corr < 0.8 or margin < 0.2): {len(weak)}.")
    rep.raw["weeks"] = table

    # compare with US-DST rule: UTC+3 when New York is on DST, else UTC+2
    mismatch = []
    for wk, k, *_ in table:
        monday = dt.datetime.strptime(wk + "-3", "%G-W%V-%u").replace(tzinfo=UTC)  # Wednesday of the week
        expected = 3 if monday.astimezone(NY).dst() else 2
        if k != expected:
            mismatch.append((wk, k, expected))
    rep.h("Check against rule `UTC+2, UTC+3 while New York observes DST`")
    rep.p(f"Weeks not matching the rule: {len(mismatch)} of {len(table)}.")
    if mismatch:
        rep.table(["week", "measured k", "rule k"], mismatch[:60])
    rep.raw["rule_mismatch"] = mismatch

    # ---- 3. XAU weekly close
    x = mt5.copy_rates_from_pos("XAUUSD@", mt5.TIMEFRAME_H1, 0, 24 * 7 * 12)
    rows = []
    if x is not None and len(x):
        ts = x["time"].astype(np.int64)
        gaps = np.nonzero(np.diff(ts) > 24 * 3600)[0]
        for g in gaps[-8:]:
            last_open = int(ts[g])
            srv = dt.datetime.fromtimestamp(last_open, tz=UTC)
            rule_k = 3 if srv.astimezone(NY).dst() else 2
            close_utc = dt.datetime.fromtimestamp(last_open + 3600 - rule_k * 3600, tz=UTC)
            rows.append([srv.strftime("%a %Y-%m-%d %H:%M"), rule_k, close_utc.strftime("%a %H:%M UTC"),
                         close_utc.astimezone(NY).strftime("%a %H:%M NY")])
    rep.h("XAUUSD@ weekly close (last H1 bar before weekend)")
    rep.table(["last bar open (server)", "k by rule", "session end (UTC)", "session end (New York)"], rows)
    rep.p("Expected: session end = Friday 17:00 New York (standard 'NY-close' broker convention).")

    # ---- 4. XAU-derived offset per week (independent of Binance): session end assumed 17:00 New York
    xa = mt5.copy_rates_from_pos("XAUUSD@", mt5.TIMEFRAME_H1, 0, mt5.terminal_info().maxbars - 1)
    xau_weeks: list[tuple[str, int]] = []
    if xa is not None and len(xa):
        ts = xa["time"].astype(np.int64)
        for g in np.nonzero(np.diff(ts) > 36 * 3600)[0]:
            end_srv = dt.datetime.fromtimestamp(int(ts[g]) + 3600, tz=UTC)          # server-scale session end
            if end_srv.weekday() not in (4, 5):                                       # Fri/Sat (server) only
                continue
            ny_close = dt.datetime.combine(end_srv.date() if end_srv.hour > 6 else end_srv.date() - dt.timedelta(days=1),
                                           dt.time(17, 0), tzinfo=NY)
            k = round((end_srv.replace(tzinfo=None) - ny_close.astimezone(UTC).replace(tzinfo=None)).total_seconds() / 3600)
            xau_weeks.append((ny_close.strftime("%G-W%V"), k))
    xreg = []
    for wk, k in xau_weeks:
        if xreg and xreg[-1]["k"] == k:
            xreg[-1]["to"], xreg[-1]["weeks"] = wk, xreg[-1]["weeks"] + 1
        else:
            xreg.append({"k": k, "from": wk, "to": wk, "weeks": 1})
    rep.h("XAUUSD@-derived offset regimes (weekly close = 17:00 New York assumption)")
    rep.p(f"XAUUSD@ H1 history: {len(xa) if xa is not None else 0} bars from "
          f"{iso(int(xa['time'][0]) * 1000) if xa is not None and len(xa) else '-'} (server scale).")
    rep.table(["from week", "to week", "weeks", "server = UTC+k"], [[r["from"], r["to"], r["weeks"], r["k"]] for r in xreg])
    btc_k = {wk: k for wk, k, *_ in table}
    both = [(wk, k, btc_k[wk]) for wk, k in xau_weeks if wk in btc_k]
    agree = sum(1 for _, a, b in both if a == b)
    rep.p(f"Weeks where both BTC-correlation and XAU-close estimates exist: {len(both)}; agreeing: {agree}. "
          "Disagreements concentrate in the US/EU DST gap weeks, where the gold close is not at 17:00 NY in server "
          "terms — i.e. the server clock follows EU DST, the gold session follows New York.")
    rep.raw["xau_weeks"] = xau_weeks

    rep.h("Local timezone trap")
    rep.p("The MetaTrader5 Python package converts **naive** `datetime` arguments using the PC's local timezone "
          "(here 'Middle East Standard Time', currently UTC+3 — coincidentally equal to the server offset). "
          "Rule: always pass integer epoch seconds on the server scale (or aware datetimes) and convert results "
          "with the model above; never pass naive datetimes.")
    mt5.shutdown()
    rep.save()


if __name__ == "__main__":
    main()
