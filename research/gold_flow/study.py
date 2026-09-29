"""Gold-proxy study (P1.11 = Phase 5 B9): does the XAUUSDT perpetual's order flow track XAUUSD@ (the broker's gold)?

    python research/gold_flow/study.py --root C:\\the_claude_new [--out docs/exploration/gold_flow.md] [--json FILE]
                                       [--since 2025-12-11] [--until 2026-09-28] [--max-days N]

For every UTC day that both cold stores hold (Binance USD-M ``XAUUSDT`` aggTrades × MT5 ``XAUUSD@`` ticks, the
Parquet day files under ``data/cold``), ONE DAY-FILE AT A TIME (read with pyarrow, only the needed columns, freed before
the next day; the process runs at below-normal priority):
* the taker delta of each 1-minute bucket of the perp (taker-buy quantity − taker-sell quantity; ``is_buyer_maker`` = a
  seller-initiated trade) and of each 5-minute bucket;
* the broker's mid log return of the same bucket (the last mid of the bucket against the last mid of the previous one;
  a bucket without a tick has no return — nothing is filled forward);
* only minutes when the broker's gold market is open (``core/sessions`` ``ny_metals_fx``: the weekend and the daily
  break excluded, DST included) and not the first 5 minutes after a reopen;
* Pearson correlation of delta(t) with return(t + k) for k = −5 … +5 minutes (1-min buckets) and −1 … +1 buckets
  (5-min), pooled over the whole window, per ISO week, and over rolling 4-week windows (sums, so the memory is a few
  numbers per week, never the ticks);
* the Monday-gap check: the broker's gap (Friday's last mid → Sunday's first mid after the reopen) against the perp's
  weekend move over the same span (the perp trades through the weekend).

Verdict (H25): ``passes`` only when |corr(k = 0)| ≥ 0.5 pooled AND every rolling 4-week window has the same sign — then
the owner may set ``pairs.XAUUSD.flow_proxy_approved: true``; otherwise the proxy stays off (capability ``unavailable``).
Read-only: no database, no network, nothing on the trading path; the only writes are ``--out`` / ``--json``.
"""
from __future__ import annotations

import argparse
import datetime as dt
import gc
import json
import math
import sys
import time
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from tradingsystem.core.sessions import calendar_for  # noqa: E402

LAGS_1M = list(range(-5, 6))
LAGS_5M = [-1, 0, 1]
MIN_PER_DAY = 1440
REOPEN_SKIP_MIN = 5
PASS_CORR = 0.5
ROLL_WEEKS = 4
PERP = ("binance_usdm", "XAUUSDT", "xauusdt_agg_trades")
BROKER = ("mt5", "XAUUSD", "xauusd_ticks")


def day_file(cold: Path, src: tuple[str, str, str], day: dt.date) -> Path:
    venue, sym, table = src
    return cold / venue / sym / table / f"{day.year}" / f"{day.isoformat()}.parquet"


class Acc:
    """Pearson sums for one (lag) series: n, Σx, Σy, Σxy, Σx², Σy²."""
    __slots__ = ("n", "sx", "sy", "sxy", "sxx", "syy")

    def __init__(self) -> None:
        self.n = self.sx = self.sy = self.sxy = self.sxx = self.syy = 0.0

    def add(self, x: np.ndarray, y: np.ndarray) -> None:
        ok = np.isfinite(x) & np.isfinite(y)
        x, y = x[ok], y[ok]
        self.n += len(x)
        self.sx += float(x.sum())
        self.sy += float(y.sum())
        self.sxy += float((x * y).sum())
        self.sxx += float((x * x).sum())
        self.syy += float((y * y).sum())

    def merge(self, o: "Acc") -> "Acc":
        out = Acc()
        for k in Acc.__slots__:
            setattr(out, k, getattr(self, k) + getattr(o, k))
        return out

    def corr(self) -> float | None:
        if self.n < 30:
            return None
        vx = self.sxx - self.sx * self.sx / self.n
        vy = self.syy - self.sy * self.sy / self.n
        if vx <= 0 or vy <= 0:
            return None
        return (self.sxy - self.sx * self.sy / self.n) / math.sqrt(vx * vy)


def open_mask(day: dt.date) -> np.ndarray:
    """Minutes of the UTC day when the broker's gold market is open, minus the first minutes after a reopen."""
    cal = calendar_for("mt5", "XAUUSD@", "metal")
    start = int(dt.datetime(day.year, day.month, day.day, tzinfo=dt.timezone.utc).timestamp() * 1000)
    m = np.array([cal.is_open(start + i * 60_000 + 30_000) for i in range(MIN_PER_DAY)], dtype=bool)
    prev_closed = not cal.is_open(start - 60_000 + 30_000)
    out = m.copy()
    for i in range(MIN_PER_DAY):
        if m[i] and (i == 0 and prev_closed or i > 0 and not m[i - 1]):
            out[i:i + REOPEN_SKIP_MIN] = False
    return out


def perp_day(path: Path, start: int) -> tuple[np.ndarray, np.ndarray]:
    """(taker delta per minute, last trade price per minute - NaN when no trade) of one perp day file."""
    t = pq.read_table(path, columns=["ts", "price", "qty", "is_buyer_maker"])
    ts = t["ts"].to_numpy()
    qty = t["qty"].to_numpy()
    sign = np.where(t["is_buyer_maker"].to_numpy() == 0, 1.0, -1.0)
    price = t["price"].to_numpy()
    del t
    idx = ((ts - start) // 60_000).astype(np.int64)
    ok = (idx >= 0) & (idx < MIN_PER_DAY)
    delta = np.bincount(idx[ok], weights=(qty * sign)[ok], minlength=MIN_PER_DAY)[:MIN_PER_DAY]
    last = np.full(MIN_PER_DAY, np.nan)
    order = np.argsort(ts[ok], kind="stable")
    last[idx[ok][order]] = price[ok][order]                 # the last write per minute wins = the last trade
    return delta, last


def broker_day(path: Path, start: int) -> np.ndarray:
    """Last mid per minute of one broker tick day file (NaN when the minute has no tick)."""
    t = pq.read_table(path, columns=["time_msc", "bid", "ask"])
    ts = t["time_msc"].to_numpy()
    mid = (t["bid"].to_numpy() + t["ask"].to_numpy()) / 2
    del t
    idx = ((ts - start) // 60_000).astype(np.int64)
    ok = (idx >= 0) & (idx < MIN_PER_DAY) & (mid > 0)
    last = np.full(MIN_PER_DAY, np.nan)
    order = np.argsort(ts[ok], kind="stable")
    last[idx[ok][order]] = mid[ok][order]
    return last


def returns(last: np.ndarray, width: int) -> np.ndarray:
    """Log return per ``width``-minute bucket: the bucket's last value against the previous bucket's (NaN if either
    bucket is empty)."""
    b = last.reshape(-1, width)
    lastb = np.array([row[np.isfinite(row)][-1] if np.isfinite(row).any() else np.nan for row in b])
    out = np.full(len(lastb), np.nan)
    out[1:] = np.log(lastb[1:] / lastb[:-1])
    return out


def lagged(acc: dict[int, Acc], x: np.ndarray, y: np.ndarray, lags: list[int]) -> None:
    """corr(x[t], y[t + k]) sums within the day (no pair crosses a day boundary)."""
    n = len(x)
    for k in lags:
        if k >= 0:
            acc[k].add(x[:n - k] if k else x, y[k:])
        else:
            acc[k].add(x[-k:], y[:n + k])


def run(root: Path, since: dt.date | None, until: dt.date | None, max_days: int | None = None,
        progress=print) -> dict:
    cold = root / "data" / "cold"
    perp_days = {p.stem for p in (cold / PERP[0] / PERP[1] / PERP[2]).rglob("*.parquet")}
    broker_days = {p.stem for p in (cold / BROKER[0] / BROKER[1] / BROKER[2]).rglob("*.parquet")}
    days = sorted(dt.date.fromisoformat(d) for d in perp_days & broker_days)
    if since:
        days = [d for d in days if d >= since]
    if until:
        days = [d for d in days if d <= until]
    if max_days:
        days = days[:max_days]
    pooled1 = {k: Acc() for k in LAGS_1M}
    pooled5 = {k: Acc() for k in LAGS_5M}
    weekly: dict[str, dict[int, Acc]] = {}
    used, t0 = 0, time.time()
    fri_mid: tuple[int, float] | None = None
    fri_perp: tuple[int, float] | None = None
    gaps = []
    for i, day in enumerate(days):
        start = int(dt.datetime(day.year, day.month, day.day, tzinfo=dt.timezone.utc).timestamp() * 1000)
        try:
            delta, perp_last = perp_day(day_file(cold, PERP, day), start)
            mid = broker_day(day_file(cold, BROKER, day), start)
        except Exception as e:  # noqa: BLE001 - one unreadable day is skipped and counted
            progress(f"{day}: skipped ({e!r})")
            continue
        mask = open_mask(day)
        r1 = returns(mid, 1)
        r1[~mask] = np.nan
        d1 = np.where(mask, delta, np.nan)
        lagged(pooled1, d1, r1, LAGS_1M)
        wk = "%d-W%02d" % day.isocalendar()[:2]
        w = weekly.setdefault(wk, {k: Acc() for k in (0,)})
        w[0].add(d1, r1)
        m5 = mask.reshape(-1, 5).all(axis=1)
        d5 = np.where(m5, delta.reshape(-1, 5).sum(axis=1), np.nan)
        r5 = returns(mid, 5)
        r5[~m5] = np.nan
        lagged(pooled5, d5, r5, LAGS_5M)
        # Monday gap: Friday's last broker mid / perp price before the close; Sunday's first after the reopen
        if day.weekday() == 4:
            k = np.nonzero(np.isfinite(mid) & mask)[0]
            kp = np.nonzero(np.isfinite(perp_last))[0]
            if len(k) and len(kp):
                j = k[-1]
                jp = kp[kp <= j]
                fri_mid = (start + j * 60_000, float(mid[j]))
                fri_perp = (start + j * 60_000, float(perp_last[jp[-1]])) if len(jp) else None
        elif day.weekday() == 6 and fri_mid and fri_perp:
            k = np.nonzero(np.isfinite(mid))[0]
            if len(k):
                j = k[0]
                kp = np.nonzero(np.isfinite(perp_last[:j + 1]))[0]
                if len(kp):
                    gaps.append({"sunday": day.isoformat(), "broker_gap": math.log(float(mid[j]) / fri_mid[1]),
                                 "perp_weekend": math.log(float(perp_last[kp[-1]]) / fri_perp[1])})
            fri_mid = fri_perp = None
        used += 1
        del delta, perp_last, mid, r1, d1, d5, r5
        gc.collect()
        if (i + 1) % 20 == 0:
            progress(f"{i + 1}/{len(days)} days, {time.time() - t0:.0f} s")
    weeks = sorted(weekly)
    week_corr = {wk: weekly[wk][0].corr() for wk in weeks}
    roll = []
    for i in range(len(weeks) - ROLL_WEEKS + 1):
        acc = Acc()
        for wk in weeks[i:i + ROLL_WEEKS]:
            acc = acc.merge(weekly[wk][0])
        roll.append({"from": weeks[i], "to": weeks[i + ROLL_WEEKS - 1], "corr": acc.corr()})
    c0 = pooled1[0].corr()
    rc = [r["corr"] for r in roll if r["corr"] is not None]
    same_sign = bool(rc) and (all(c > 0 for c in rc) or all(c < 0 for c in rc))
    gx = np.array([g["perp_weekend"] for g in gaps])
    gy = np.array([g["broker_gap"] for g in gaps])
    gap = {"weekends": len(gaps),
           "corr": float(np.corrcoef(gx, gy)[0, 1]) if len(gaps) >= 5 and gx.std() > 0 and gy.std() > 0 else None,
           "median_abs_diff_pct": float(np.median(np.abs(gx - gy)) * 100) if gaps else None}
    return {"days_overlap": len(days), "days_used": used, "first": days[0].isoformat() if days else None,
            "last": days[-1].isoformat() if days else None, "seconds": round(time.time() - t0, 1),
            "corr_1m": {k: _r(a.corr()) for k, a in pooled1.items()}, "n_1m": int(pooled1[0].n),
            "corr_5m": {k: _r(a.corr()) for k, a in pooled5.items()}, "n_5m": int(pooled5[0].n),
            "weeks": {wk: _r(c) for wk, c in week_corr.items()}, "rolling_4w": [{**r, "corr": _r(r["corr"])} for r in roll],
            "rolling_same_sign": same_sign, "rolling_min_abs": _r(min((abs(c) for c in rc), default=None)),
            "monday_gap": {k: (_r(v) if isinstance(v, float) else v) for k, v in gap.items()},
            "passes": bool(c0 is not None and abs(c0) >= PASS_CORR and same_sign)}


def _r(x, nd=3):
    return None if x is None else round(float(x), nd)


def render(d: dict) -> str:
    c1, c5 = d["corr_1m"], d["corr_5m"]
    best = max(((k, v) for k, v in c1.items() if v is not None), key=lambda kv: abs(kv[1]), default=(None, None))
    wk = [v for v in d["weeks"].values() if v is not None]
    L = ["# Gold flow-proxy study (P1.11 / Phase 5 B9)", "",
         f"XAUUSDT perpetual taker delta (Binance USD-M aggTrades) vs XAUUSD@ mid returns (Windsor MT5 ticks), "
         f"{d['days_used']} of {d['days_overlap']} overlapping UTC days ({d['first']} → {d['last']}), one day-file at a "
         f"time, broker-closed minutes (weekend, daily break) and the first {REOPEN_SKIP_MIN} minutes after a reopen "
         f"excluded; {d['seconds']} s. Generated by `research/gold_flow/study.py` (read-only).", "",
         f"## Verdict: **{'PASSES' if d['passes'] else 'FAILS'}** "
         f"(|corr(k=0)| ≥ {PASS_CORR} pooled AND the same sign in every rolling {ROLL_WEEKS}-week window)", "",
         f"- pooled 1-min corr at k = 0: **{c1.get(0)}** (n {d['n_1m']}); the largest |corr| over k = −5…+5 min: "
         f"{best[1]} at k = {best[0]}",
         f"- pooled 5-min corr at k = −1 / 0 / +1 bucket: {c5.get(-1)} / {c5.get(0)} / {c5.get(1)} (n {d['n_5m']})",
         f"- weekly k = 0 corr: median {_r(float(np.median(wk))) if wk else None}, range "
         f"{_r(min(wk)) if wk else None} … {_r(max(wk)) if wk else None} over {len(wk)} weeks; "
         f"weeks with |corr| ≥ {PASS_CORR}: {sum(1 for v in wk if abs(v) >= PASS_CORR)}",
         f"- rolling {ROLL_WEEKS}-week windows: same sign in all = {d['rolling_same_sign']}, the smallest |corr| "
         f"{d['rolling_min_abs']}",
         f"- Monday gap ({d['monday_gap']['weekends']} weekends): corr(perp weekend move, broker gap) "
         f"{d['monday_gap']['corr']}, median |difference| {d['monday_gap']['median_abs_diff_pct']} %", "",
         "Consequence: " + ("the owner MAY set `pairs.XAUUSD.flow_proxy_approved: true` (H25) — the XAUUSDT order flow "
                            "then feeds XAU's bar delta / footprint as a proxy." if d["passes"] else
                            "H25 stays closed: `flow_proxy_approved` stays false and XAU's order-flow capabilities stay "
                            "`unavailable` (the perp is shown only as the `derivatives` proxy)."), "",
         "## Lead / lag (1-min buckets, corr of delta(t) with return(t + k))", "",
         "| k (min) | " + " | ".join(str(k) for k in c1) + " |", "|---|" + "---|" * len(c1),
         "| corr | " + " | ".join(str(v) for v in c1.values()) + " |", "",
         "## Weekly k = 0 correlation", "", "| week | corr |", "|---|---|"]
    L += [f"| {k} | {v} |" for k, v in d["weeks"].items()]
    L += ["", f"## Rolling {ROLL_WEEKS}-week windows", "", "| from | to | corr |", "|---|---|---|"]
    L += [f"| {r['from']} | {r['to']} | {r['corr']} |" for r in d["rolling_4w"]]
    return "\n".join(L) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", default=str(ROOT))
    ap.add_argument("--since")
    ap.add_argument("--until")
    ap.add_argument("--max-days", type=int)
    ap.add_argument("--out")
    ap.add_argument("--json")
    a = ap.parse_args(argv)
    try:
        import psutil
        psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS if sys.platform == "win32" else 10)
    except Exception:  # noqa: BLE001 - priority is a courtesy
        pass
    d = run(Path(a.root), dt.date.fromisoformat(a.since) if a.since else None,
            dt.date.fromisoformat(a.until) if a.until else None, a.max_days, progress=lambda m: print(m, flush=True))
    text = render(d)
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8", newline="\n")
    if a.json:
        Path(a.json).write_text(json.dumps(d, indent=1), encoding="utf-8", newline="\n")
    if not a.out:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
