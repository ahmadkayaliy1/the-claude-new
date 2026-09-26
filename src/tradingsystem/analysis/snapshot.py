"""Snapshot builder (P6.13): the desk's prepared "screens" for one pair, as a compact deterministic JSON payload.

Only closed candles are analysed (as-of semantics); every block carries its data-quality flag from the
capability registry; numbers are rounded to the instrument's precision; the payload is hashed so each AI
decision can be linked to exactly what the model saw (spec §8.3). ``data_problems`` is the gate that keeps AI
calls off payloads whose data is missing, stale or too short (stall F5, F11).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import numpy as np

from ..core.instruments import Instrument, InstrumentRegistry
from ..core.sessions import SessionCalendar, calendar_for
from ..core.settings import RiskCfg, Settings
from ..core.timeframes import Timeframe
from ..core.timeutil import MS_PER_HOUR, MS_PER_MINUTE, iso
from ..storage.reader import InstrumentReader
from ..storage.tablespec import spec_for
from . import context as ctx
from . import indicators as ind
from . import orderflow as of
from .frames import MIN_BARS, Frame, load_frame
from .price_action import patterns, range_state
from .registry import capability_matrix
from .structure import analyze_structure, premium_discount
from .zones import fair_value_gaps, nearest_active, order_blocks, update_mitigation

PAYLOAD_VERSION = "3"
HTF_GATE = ("4h", "1d")         # decision context: without enough history here the AI is not asked (F11)
RECENT_GAP_BARS = 16            # a gap this close to the decision bar blocks the AI call; older ones are warnings
# timeframe → (bars analysed, recent candles shown)
TF_PLAN: dict[str, tuple[int, int]] = {"1w": (80, 6), "1d": (200, 10), "4h": (240, 12), "1h": (300, 12),
                                       "15m": (320, 16), "5m": (300, 12), "1m": (240, 10)}


def _r(x, d: int):
    if x is None:
        return None
    try:
        if np.isnan(x):
            return None
    except TypeError:
        return x
    return round(float(x), d)


def execution_costs(contract: dict, bid: float, ask: float, spreads_1h: np.ndarray, spreads_24h: np.ndarray,
                    atr: float | None, risk: RiskCfg, d: int) -> dict:
    """What executing on this venue costs and the stop distances the risk gate will accept — the same rule as
    ``risk_gate.evaluate``: a stop must be at least max(stops level + spread, sl_atr_min_mult × ATR, spread /
    max_spread_to_sl_ratio) from the entry and at most sl_atr_max_mult × ATR. The spread used is the larger of the
    current and the 1-hour median (a momentarily tight quote must not invite a stop the gate rejects later).
    Distances are the same in the analysis and the execution price space (levels are translated by the basis)."""
    tick = float(contract.get("tick_size") or 0.01)
    spread_now = float(ask - bid)
    med_1h = float(np.median(spreads_1h)) if len(spreads_1h) else None
    spread = max(spread_now, med_1h or 0.0)
    stops = float(contract.get("stops_level_points") or 0) * tick
    parts = {"stops_level_plus_spread": stops + spread}
    if risk.max_spread_to_sl_ratio > 0:
        parts["spread_rule"] = spread / risk.max_spread_to_sl_ratio
    if atr:
        parts["atr_floor"] = risk.sl_atr_min_mult * atr
    min_stop = max(parts.values())
    out = {
        "spread_now": _r(spread_now, d), "spread_median_1h": _r(med_1h, d),
        "spread_p95_24h": _r(float(np.percentile(spreads_24h, 95)), d) if len(spreads_24h) else None,
        "spread_max_24h": _r(float(spreads_24h.max()), d) if len(spreads_24h) else None,
        "stops_level": _r(stops, d),
        "min_stop_distance": _r(min_stop, d), "min_stop_set_by": max(parts, key=parts.get),
        "max_stop_distance": _r(risk.sl_atr_max_mult * atr, d) if atr else None,
        "max_spread_pct_of_stop": round(risk.max_spread_to_sl_ratio * 100),
        "commission_per_lot_per_side": contract.get("commission_per_lot") or 0.0,
    }
    vol, cs, mode = contract.get("volume_min"), contract.get("contract_size"), contract.get("swap_mode")
    if mode and vol and cs and contract.get("swap_long") is not None:
        mid = (bid + ask) / 2

        def per_night(v: float) -> float:
            return v * tick * cs * vol if mode == "points" else mid * cs * vol * v / 100 / 360
        out["swap_per_night_at_min_lot_usd"] = {"long": round(per_night(contract["swap_long"]), 3),
                                               "short": round(per_night(contract["swap_short"]), 3),
                                               "triple_on": contract.get("triple_swap_weekday")}
    return out


class SnapshotBuilder:
    def __init__(self, settings: Settings, registry: InstrumentRegistry) -> None:
        self.s = settings
        self.reg = registry
        self.data = settings.paths.data()
        self._readers: dict[str, InstrumentReader] = {}

    def reader(self, inst: Instrument) -> InstrumentReader:
        if inst.key not in self._readers:
            self._readers[inst.key] = InstrumentReader(inst, self.data, cache_mb=self.s.resource.sqlite_cache_mb)
        return self._readers[inst.key]

    def close(self) -> None:
        for r in self._readers.values():
            r.close()
        self._readers.clear()

    # ------------------------------------------------------------------ public
    def build(self, pair: str, as_of: int, *, account: dict | None = None, history: list[dict] | None = None,
              timeframes: list[str] | None = None, memory: dict | None = None,
              performance: dict | None = None) -> dict:
        pcfg = self.s.pairs[pair]
        primary = self.reg.primary(pair)
        execu = self.reg.with_role(pair, "execution")[0]
        caps = capability_matrix(self.s, self.reg, pair)
        cal = calendar_for(primary.venue, primary.symbol, pcfg.asset_class)
        exec_cal = calendar_for(execu.venue, execu.symbol, pcfg.asset_class)
        d = pcfg.price_decimals
        rd = self.reader(primary)
        tfs = [t for t in (timeframes or list(TF_PLAN)) if Timeframe.parse(t) in primary.timeframes]
        frames = {t: load_frame(rd, primary, Timeframe.parse(t), TF_PLAN[t][0], as_of, cal) for t in tfs}
        per_tf = {t: self._analyze_tf(frames[t], d, TF_PLAN[t][1], primary.venue == "mt5") for t in tfs}
        dec_tf = pcfg.decision_timeframe.value
        dec_atr = ((per_tf.get(dec_tf) or {}).get("indicators") or {}).get("atr14")
        market = self._market(pair, primary, execu, frames.get("1m"), as_of, exec_cal, d, dec_atr)
        payload = {
            "meta": {"pair": pair, "as_of": iso(as_of), "decision_timeframe": dec_tf,
                     "price_reference": primary.key, "execution_instrument": execu.key,
                     "payload_version": PAYLOAD_VERSION, "config_hash": self.s.config_hash},
            "account": self._account(account, execu, dec_atr,
                                     ((market.get("execution") or {}).get("costs") or {}).get("min_stop_distance"), d),
            "market": market,
            "capabilities": {k: {"quality": v.quality, "reason": v.reason} for k, v in caps.items()},
            "levels": self._levels(frames.get("1h"), as_of, pcfg.asset_class, d),
            "timeframes": per_tf,
            "confluence": ctx.confluence({t: {"trend": v["structure"]["trend"],
                                              "ema_alignment": v["indicators"]["ema_alignment"]}
                                          for t, v in per_tf.items()}),
            "orderflow": self._orderflow(pair, primary, frames, caps, as_of, pcfg, d),
            "derivatives": self._derivatives(pair, caps, as_of, d),
            "history": history or [],
            "memory": memory or {},
            "performance": performance or {},
        }
        payload["meta"]["data_warnings"] = [
            f"{t}: {f.quality.get('status')}" + (f" ({len(f)} bars)" if f.quality.get("short_history") else "")
            for t, f in frames.items() if f.quality.get("status") != "ok"]
        payload["meta"]["payload_hash"] = payload_hash(payload)
        return payload

    # ------------------------------------------------------------------ per timeframe
    def _analyze_tf(self, fr: Frame, d: int, n_recent: int, tick_volume: bool) -> dict:
        if len(fr) < 30:
            return {"bars": len(fr), "quality": fr.quality, "structure": {"trend": None},
                    "indicators": {"ema_alignment": None}, "note": "insufficient history"}
        o, h, l, c, v = fr.open, fr.high, fr.low, fr.close, fr.volume
        atr = ind.atr(h, l, c)
        st = analyze_structure(h, l, c, atr)
        price = float(c[-1])
        fvg = update_mitigation(fair_value_gaps(h, l, c, atr), h, l, c)
        obs = update_mitigation(order_blocks(o, h, l, c, atr, st), h, l, c)
        t = lambda i: iso(int(fr.open_time[i]))  # noqa: E731

        def zone(z):
            return {"dir": z.direction, "top": _r(z.top, d), "bottom": _r(z.bottom, d), "formed": t(z.origin_idx),
                    "fill_pct": round(z.fill * 100), "touched": z.first_touch_idx is not None,
                    "strength_atr": round(z.strength, 2), "age_bars": len(c) - 1 - z.known_idx}

        pools_up = sorted([p for p in st.pools if p.swept_idx is None and p.side == "buy_side" and p.level > price],
                          key=lambda p: p.level)[:3]
        pools_dn = sorted([p for p in st.pools if p.swept_idx is None and p.side == "sell_side" and p.level < price],
                          key=lambda p: -p.level)[:3]
        rsi, (macd_l, macd_s, macd_h) = ind.rsi(c), ind.macd(c)
        bb_lo, bb_mid, bb_hi = ind.bollinger(c)
        adx_v, pdi, mdi = ind.adx(h, l, c)
        es = ctx.ema_stack(c)
        vw = ind.session_vwap(fr.open_time, h, l, c, v) if fr.tf.ms <= MS_PER_HOUR else None
        out = {
            "bars": len(fr), "quality": fr.quality,
            "recent": [[t(i), _r(o[i], d), _r(h[i], d), _r(l[i], d), _r(c[i], d), _r(v[i], 3)]
                       for i in range(max(0, len(c) - n_recent), len(c))],
            "recent_columns": ["open_time", "open", "high", "low", "close",
                               "tick_volume" if tick_volume else "volume"],
            "structure": {
                "trend": st.trend,
                "last_swing_high": ({"price": _r(st.last_high.price, d), "time": t(st.last_high.idx)}
                                    if st.last_high else None),
                "last_swing_low": ({"price": _r(st.last_low.price, d), "time": t(st.last_low.idx)}
                                   if st.last_low else None),
                "events": [{"time": t(e.idx), "kind": e.kind, "dir": e.direction, "level": _r(e.level, d)}
                           for e in st.events[-6:]],
            },
            "premium_discount": premium_discount(st, price, h, l),
            "zones": {"fvg": [zone(z) for z in nearest_active(fvg, price)],
                      "order_blocks": [zone(z) for z in nearest_active(obs, price)]},
            "liquidity": {
                "buy_side_above": [{"level": _r(p.level, d), "touches": len(p.touches)} for p in pools_up],
                "sell_side_below": [{"level": _r(p.level, d), "touches": len(p.touches)} for p in pools_dn],
            },
            "indicators": {
                "close": _r(price, d), "atr14": _r(atr[-1], d), "rsi14": _r(rsi[-1], 1),
                "macd_hist": _r(macd_h[-1], d), "adx14": _r(adx_v[-1], 1), "plus_di": _r(pdi[-1], 1),
                "minus_di": _r(mdi[-1], 1),
                "bb_width_pct": _r((bb_hi[-1] - bb_lo[-1]) / bb_mid[-1] * 100, 3) if not np.isnan(bb_mid[-1]) else None,
                "bb_pct_b": _r((price - bb_lo[-1]) / (bb_hi[-1] - bb_lo[-1]), 3) if not np.isnan(bb_hi[-1]) and bb_hi[-1] > bb_lo[-1] else None,
                "ema20": _r(es["ema20"], d), "ema50": _r(es["ema50"], d), "ema200": _r(es["ema200"], d),
                "ema_alignment": es["alignment"],
                "session_vwap": _r(vw[-1], d) if vw is not None else None,
                "vwap_quality": ("approx" if tick_volume else "real") if vw is not None else None,
            },
            "patterns": {"last_bar": patterns(o, h, l, c, len(c) - 1), "prev_bar": patterns(o, h, l, c, len(c) - 2)},
            "range": range_state(h, l, c, atr),
        }
        if fr.tf in (Timeframe.H1, Timeframe.M15, Timeframe.H4):
            out["regime"] = ctx.regime(h, l, c)
        return out

    # ------------------------------------------------------------------ blocks
    def _account(self, account: dict | None, execu: Instrument, atr: float | None, min_stop: float | None,
                 d: int) -> dict:
        """The account state passed in, plus what the minimum executable position risks (rule 8): the execution
        instrument's minimum lot at the minimum stop distance the gate accepts (``market.execution.costs``)."""
        if not account:
            return {}
        out = dict(account)
        eq, spec = account.get("equity"), execu.contract
        if eq and spec and (min_stop or atr) and "USD" in execu.symbol.upper():
            per_unit = spec["volume_min"] * spec["contract_size"]          # USD per 1.0 price move at the min lot
            sl = min_stop or self.s.risk.sl_atr_min_mult * atr
            out["min_position_risk"] = {
                "instrument": execu.key, "volume_min": spec["volume_min"], "contract_size": spec["contract_size"],
                "usd_per_price_unit_at_min_lot": round(per_unit, 4), "min_stop_distance": _r(sl, d),
                "risk_pct_at_min_lot_and_min_stop": round(per_unit * sl / eq * 100, 2),
                "max_stop_distance_at_min_lot_within_max_risk":
                    _r(eq * self.s.risk.max_risk_per_trade_pct / 100 / per_unit, d)}
        return out

    def quote_at(self, inst: Instrument, as_of: int) -> dict | None:
        """Last stored bid/ask at or before ``as_of`` (causal; works for live and replay)."""
        rd = self.reader(inst)
        if "book_ticker" in inst.datatypes:
            c = rd.read_range(spec_for(inst, "book_ticker"), as_of - 10 * MS_PER_MINUTE, as_of + 1,
                              ["ts", "bid", "ask"])
            if len(c["ts"]):
                return {"ts": int(c["ts"][-1]), "bid": float(c["bid"][-1]), "ask": float(c["ask"][-1])}
        if "ticks" in inst.datatypes:
            c = rd.read_range(spec_for(inst, "ticks"), as_of - 10 * MS_PER_MINUTE, as_of + 1,
                              ["time_msc", "bid", "ask"])
            if len(c["time_msc"]):
                return {"ts": int(c["time_msc"][-1]), "bid": float(c["bid"][-1]), "ask": float(c["ask"][-1])}
        return None

    def _spreads(self, inst: Instrument, as_of: int) -> tuple[np.ndarray, np.ndarray]:
        """(spreads of the last hour, of the last 24 h) from the stored quotes — causal (<= as_of)."""
        dt = "ticks" if "ticks" in inst.datatypes else "book_ticker" if "book_ticker" in inst.datatypes else None
        if dt is None:
            return np.array([]), np.array([])
        tcol = "time_msc" if dt == "ticks" else "ts"
        c = self.reader(inst).read_range(spec_for(inst, dt), as_of - 24 * MS_PER_HOUR, as_of + 1, [tcol, "bid", "ask"])
        if not len(c[tcol]):
            return np.array([]), np.array([])
        s = np.asarray(c["ask"], dtype=float) - np.asarray(c["bid"], dtype=float)
        ok = s >= 0
        t = np.asarray(c[tcol])[ok]
        s = s[ok]
        return s[t >= as_of - MS_PER_HOUR], s

    def _market(self, pair: str, primary: Instrument, execu: Instrument, m1: Frame | None, as_of: int,
                exec_cal: SessionCalendar, d: int, dec_atr: float | None = None) -> dict:
        pq, eq = self.quote_at(primary, as_of), self.quote_at(execu, as_of)
        out: dict = {"session": ctx.sessions(as_of), "execution_market_open": exec_cal.is_open(as_of)}
        if pq:
            out["analysis_price"] = {"bid": _r(pq["bid"], d), "ask": _r(pq["ask"], d),
                                     "age_s": max(0.0, round((as_of - pq["ts"]) / 1000, 1))}
        elif m1 is not None and len(m1):
            out["analysis_price"] = {"last_close_1m": _r(m1.last_close, d)}
        if eq:
            spread = eq["ask"] - eq["bid"] if eq["ask"] and eq["bid"] else None
            out["execution"] = {"instrument": execu.key, "bid": _r(eq["bid"], d), "ask": _r(eq["ask"], d),
                                "spread": _r(spread, d), "age_s": max(0.0, round((as_of - eq["ts"]) / 1000, 1))}
            if execu.contract and spread is not None:
                s1h, s24h = self._spreads(execu, as_of)
                out["execution"]["costs"] = execution_costs(execu.contract, eq["bid"], eq["ask"], s1h, s24h, dec_atr,
                                                            self.s.risk, d)
            if pq and primary.key != execu.key:
                basis = (eq["bid"] + eq["ask"]) / 2 - (pq["bid"] + pq["ask"]) / 2
                out["basis_exec_minus_analysis"] = _r(basis, d)
                out["note"] = ("levels are expressed in the analysis instrument's prices; the execution layer "
                               "translates them by the live basis before sending orders")
        return out

    def _levels(self, h1: Frame | None, as_of: int, asset_class: str, d: int) -> dict:
        if h1 is None or not len(h1):
            return {}
        lv = ctx.reference_levels(h1.open_time, h1.high, h1.low, h1.close, h1.open, as_of,
                                  day_roll="ny17" if asset_class in ("metal", "fx") else "utc")
        return {k: _r(v, d) for k, v in lv.items()}

    def _orderflow(self, pair: str, primary: Instrument, frames: dict[str, Frame], caps, as_of: int, pcfg,
                   d: int) -> dict:
        out: dict = {}
        dec = pcfg.decision_timeframe
        fr = frames.get(dec.value)
        if caps["bar_delta_cvd"].quality == "real" and fr is not None and fr.taker_buy is not None and len(fr):
            delta = of.bar_delta(fr.volume, fr.taker_buy)
            cv = of.cvd(delta)
            st = analyze_structure(fr.high, fr.low, fr.close, ind.atr(fr.high, fr.low, fr.close))
            div = of.delta_divergence([(p.idx, p.price, p.kind) for p in st.pivots], cv)
            n = 16
            out["bar_delta"] = {"data_quality": "real", "timeframe": dec.value,
                                "last": [_r(x, 3) for x in delta[-n:]],
                                "cvd_change_last_n": _r(cv[-1] - cv[-n], 3) if len(cv) > n else None,
                                "divergence": div and {**div, "price": [_r(x, d) for x in div["price"]],
                                                        "cvd": [_r(x, 2) for x in div["cvd"]]}}
        else:
            out["bar_delta"] = {"data_quality": caps["bar_delta_cvd"].quality, "reason": caps["bar_delta_cvd"].reason}
        if caps["footprint"].quality == "real" and "agg_trades" in primary.datatypes:
            win = 8 * dec.ms
            lo = dec.floor(as_of) - win
            a = self.reader(primary).read_range(spec_for(primary, "agg_trades"), lo, dec.floor(as_of),
                                                ["ts", "price", "qty", "is_buyer_maker"])
            fps = of.merge_bars(of.footprint(a["ts"], a["price"], a["qty"], a["is_buyer_maker"], MS_PER_MINUTE,
                                             pcfg.footprint_bucket), dec.ms) if len(a["ts"]) else []
            bars = []
            for fb in fps:
                idx = np.nonzero(fr.open_time == fb.open_time)[0] if fr is not None else []
                ab = of.absorption(fb, float(fr.open[idx[0]]), float(fr.close[idx[0]])) if len(idx) else None
                im = of.imbalances(fb)
                bars.append({"time": iso(fb.open_time), "volume": _r(fb.volume, 3), "delta": _r(fb.delta, 3),
                             "poc": _r(fb.poc, d), "stacked_buy": [[_r(a0, d), _r(b0, d)] for a0, b0 in im["stacked_buy"]],
                             "stacked_sell": [[_r(a0, d), _r(b0, d)] for a0, b0 in im["stacked_sell"]],
                             "absorption": ab})
            prof = of.profile_from_footprint(fps)
            out["footprint"] = {"data_quality": "real", "timeframe": dec.value, "bucket": pcfg.footprint_bucket,
                                "bars": bars, "profile_last_8_bars": prof and {k: _r(v, d) if k != "coverage" else v
                                                                              for k, v in prof.items()}}
        else:
            out["footprint"] = {"data_quality": caps["footprint"].quality, "reason": caps["footprint"].reason}
        m1 = frames.get("1m")
        if m1 is not None and len(m1):
            day0 = as_of - as_of % 86_400_000
            m = m1.open_time >= day0
            if m.sum() >= 30:
                tpo = of.tpo_profile(m1.high[m], m1.low[m], pcfg.footprint_bucket)
                out["tpo_today"] = {"data_quality": "real", **{k: _r(v, d) if k != "coverage" else v
                                                              for k, v in (tpo or {}).items()}}
                if m1.volume_kind == "tick":
                    tv = of.tick_volume_profile(m1.high[m], m1.low[m], m1.volume[m], pcfg.footprint_bucket)
                    out["tick_volume_profile_today"] = tv and {k: (_r(v, d) if isinstance(v, float) else v)
                                                               for k, v in tv.items()}
        return out

    def _derivatives(self, pair: str, caps, as_of: int, d: int) -> dict:
        cap = caps["derivatives"]
        if cap.quality == "unavailable" or not cap.source:
            return {"data_quality": "unavailable", "reason": cap.reason}
        inst = self.reg.get(cap.source)
        rd = self.reader(inst)
        out: dict = {"data_quality": cap.quality, "source": inst.key, "reason": cap.reason}
        if "funding" in inst.datatypes:
            f = rd.read_range(spec_for(inst, "funding"), as_of - 3 * 86_400_000, as_of)
            out["funding"] = [{"time": iso(int(t)), "rate": _r(r, 6)} for t, r in
                              zip(f["funding_time"][-3:], f["funding_rate"][-3:])]
        if "open_interest" in inst.datatypes:
            oi = rd.read_range(spec_for(inst, "open_interest"), as_of - 26 * MS_PER_HOUR, as_of)
            if len(oi["ts"]):
                last = float(oi["open_interest"][-1])

                def chg(hours: int):
                    tgt = as_of - hours * MS_PER_HOUR
                    i = int(np.searchsorted(oi["ts"], tgt))
                    if i >= len(oi["ts"]) or abs(int(oi["ts"][i]) - tgt) > 10 * MS_PER_MINUTE:
                        return None
                    return _r((last / float(oi["open_interest"][i]) - 1) * 100, 3)
                out["open_interest"] = {"last": _r(last, 3), "change_pct_1h": chg(1), "change_pct_4h": chg(4),
                                        "change_pct_24h": chg(24)}
        if "metrics" in inst.datatypes:
            mt = rd.read_range(spec_for(inst, "metrics"), as_of - 2 * MS_PER_HOUR, as_of)
            if len(mt["ts"]):
                out["positioning"] = {"time": iso(int(mt["ts"][-1])),
                                      "top_trader_account_ls": _r(mt["count_toptrader_long_short_ratio"][-1], 3),
                                      "top_trader_position_ls": _r(mt["sum_toptrader_long_short_ratio"][-1], 3),
                                      "global_account_ls": _r(mt["count_long_short_ratio"][-1], 3),
                                      "taker_buy_sell_vol_ratio": _r(mt["sum_taker_long_short_vol_ratio"][-1], 3)}
        if "mark_price" in inst.datatypes:
            mp = rd.read_range(spec_for(inst, "mark_price"), as_of - 10 * MS_PER_MINUTE, as_of)
            if len(mp["ts"]):
                out["mark"] = {"mark": _r(mp["mark_price"][-1], d), "index": _r(mp["index_price"][-1], d)}
        if "liquidations" in inst.datatypes:
            lq = rd.read_range(spec_for(inst, "liquidations"), as_of - MS_PER_HOUR, as_of,
                               ["side", "avg_price", "filled_qty"])
            sides = lq["side"]
            notional = lq["avg_price"] * lq["filled_qty"] if len(sides) else np.array([])
            out["liquidations_1h"] = {"data_quality": "partial (exchange sends ≤1 event/s/symbol)",
                                      "longs_liquidated_usd": _r(notional[sides == "SELL"].sum(), 0) if len(sides) else 0,
                                      "shorts_liquidated_usd": _r(notional[sides == "BUY"].sum(), 0) if len(sides) else 0,
                                      "events": int(len(sides))}
        return out


def _latest_quotes(app_db: Path) -> dict[str, dict]:
    if not app_db.exists():
        return {}
    con = sqlite3.connect(f"file:{app_db.as_posix()}?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT instrument, ts, bid, ask FROM latest_quote").fetchall()
    finally:
        con.close()
    return {r[0]: {"ts": r[1], "bid": r[2], "ask": r[3]} for r in rows}


def data_problems(payload: dict, as_of: int, max_staleness_s: float) -> list[str]:
    """Why an AI call on this payload would be wasted (empty = go): the decision timeframe's last bar missing,
    stale, too short or gapped near the decision bar; a stale live analysis price while the market is open; or
    too little 4h/1d history (stall F5, F11)."""
    out: list[str] = []
    tfs = payload.get("timeframes") or {}
    dec = payload["meta"]["decision_timeframe"]
    t = tfs.get(dec)
    q = (t or {}).get("quality") or {}
    st = q.get("status")
    if t is None or st in ("empty", "stale", "missing_last_bar", "short_history"):
        out.append(f"{dec} decision candles: {st or 'missing'}" + (f" ({t.get('bars')} bars)" if t else ""))
    tf_ms = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000}.get(dec, 900_000)
    recent = [g for g in q.get("gaps") or [] if g[0] >= as_of - RECENT_GAP_BARS * tf_ms]
    if recent:
        out.append(f"{dec} decision candles: {sum(g[1] for g in recent)} missing bar(s) in the last "
                   f"{RECENT_GAP_BARS} bars")
    for h in HTF_GATE:
        x = tfs.get(h)
        if x is not None and (x.get("bars") or 0) < MIN_BARS:
            out.append(f"{h}: only {x.get('bars') or 0} bars of history (< {MIN_BARS})")
    mk = payload.get("market") or {}
    if mk.get("execution_market_open", True):
        age = (mk.get("analysis_price") or {}).get("age_s")
        if age is None:
            age = (((tfs.get("1m") or {}).get("quality") or {}).get("last_bar_end_lag_s"))
        if age is None:
            out.append("no recent analysis price")
        elif age > max_staleness_s:
            out.append(f"analysis price is {age:.0f}s old (> {max_staleness_s:.0f}s)")
    return out


def payload_hash(payload: dict) -> str:
    body = {k: v for k, v in payload.items() if k != "meta"} | {"meta": {k: v for k, v in payload["meta"].items()
                                                                        if k != "payload_hash"}}
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()[:16]


def to_json(payload: dict) -> str:
    return json.dumps(payload, sort_keys=False, default=str, separators=(",", ":"), ensure_ascii=False)
