"""Deterministic risk gate (P9.1) — independent of the AI; every order must pass it (spec §0, §7.1, §7.3).

The gate evaluates an already contract-valid recommendation, *translated into the execution instrument's
prices*, against live execution state. It returns every check with pass/fail and detail (shown on the
dashboard), the executable entry price, and the position size. One failed check ⇒ no order.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..core.settings import RiskCfg
from ..core.timeutil import parse_date_spec
from .sizing import SizeResult, single_leg_index, size_position, split_volume


@dataclass
class ExecContext:
    now_ms: int
    bid: float
    ask: float
    quote_age_s: float
    market_open: bool
    atr: float                       # ATR(14) of the decision timeframe on the execution instrument's scale
    stops_level_price: float         # broker minimum stop distance in price units (stops_level × point)
    contract_size: float
    volume_min: float
    volume_step: float
    volume_max: float | None
    equity: float
    open_positions: int = 0          # decisions holding an open position or a live pending order
    open_risk_pct_by_pair: dict[str, float] = field(default_factory=dict)   # SL risk of positions + pending orders
    realized_pnl_today_usd: float = 0.0
    unrealized_pnl_usd: float = 0.0
    kill_switch: bool = False
    basis_ok: bool = True
    basis_reason: str = ""
    # D-042 — other systems of this project on the same account (correlated cap), this pair's live positions and
    # pending orders by side (no second trade in the same direction), the account's drawdown from its peak
    sibling_risk_pct_by_pair: dict[str, float] = field(default_factory=dict)
    live_sides: dict[str, list[str]] = field(default_factory=dict)
    account_drawdown_pct: float | None = None
    account_drawdown_tripped: bool = False


@dataclass
class GateResult:
    approved: bool
    checks: list[tuple[str, bool, str]]
    entry: float | None = None
    size: SizeResult | None = None
    rr_exec: float | None = None
    single_leg_tp: int | None = None        # 0-based TP index when the volume cannot be split (one position)

    def failures(self) -> list[str]:
        return [f"{n}: {d}" for n, ok, d in self.checks if not ok]


RR_EPS = 1e-6          # a reward-to-risk equal to the minimum within float noise passes


def _rr_ok(rr: float, min_rr: float) -> bool:
    return rr >= min_rr - RR_EPS


def _rr_text(rr: float, min_rr: float, ok: bool) -> str:
    """``1.500 ≥ 1.5`` / ``1.4996 < 1.5``: 3 decimals, more when rounding would show a value on the wrong side of
    the limit (RR 1.4996 printed as "1.50 ≥ 1.5" read as a failed pass — the model reads this in ``gate_reason``)."""
    for d in (3, 4, 6):
        s = f"{rr:.{d}f}"
        if _rr_ok(float(s), min_rr) == ok:
            break
    return f"{s} {'≥' if ok else '<'} {min_rr}"


def entry_price(rec: dict, bid: float, ask: float) -> float | None:
    """Executable entry: MARKET at the touch side; pending orders at the worst edge of their zone."""
    buy = rec["decision"] == "BUY"
    e = rec["entry"]
    if rec["order_type"] == "MARKET":
        return ask if buy else bid
    if e.get("range_min") is not None:
        return e["range_max"] if buy else e["range_min"]
    return e["price"]


def pending_price_check(order_type: str, price: float, bid: float, ask: float, stops_level: float) -> tuple[bool, str]:
    """MT5 pending-order price rule: the order must sit on the correct side of the touch by ≥ stops_level
    (BUY_LIMIT ask−p, BUY_STOP p−ask, SELL_LIMIT p−bid, SELL_STOP bid−p). A crossed order is refused by the broker
    (invalid price), so paper must refuse it too instead of filling it at once."""
    touch_name, touch = ("ask", ask) if order_type.startswith("BUY") else ("bid", bid)
    gap = {"BUY_LIMIT": ask - price, "BUY_STOP": price - ask, "SELL_LIMIT": price - bid, "SELL_STOP": bid - price}[order_type]
    ok = gap >= stops_level
    return ok, (f"{order_type} {price} is {gap:.2f} from {touch_name} {touch} (≥ stops level {stops_level:.2f})" if ok else
                f"{order_type} {price} vs {touch_name} {touch}: the market is already at/through the entry "
                f"({gap:.2f} < stops level {stops_level:.2f}) — the broker would refuse it")


def evaluate(rec: dict, pair: str, ctx: ExecContext, risk: RiskCfg, correlated_groups: list[list[str]],
             min_confidence: int = 55) -> GateResult:
    checks: list[tuple[str, bool, str]] = []

    def add(name: str, ok: bool, detail: str) -> bool:
        checks.append((name, bool(ok), detail))
        return bool(ok)

    if rec.get("decision") not in ("BUY", "SELL"):
        add("is_trade", False, f"decision is {rec.get('decision')}")
        return GateResult(False, checks)
    buy = rec["decision"] == "BUY"
    sl = rec.get("stop_loss")
    add("kill_switch", not ctx.kill_switch, "kill switch engaged" if ctx.kill_switch else "off")
    dd = ctx.account_drawdown_pct
    add("account_drawdown", not ctx.account_drawdown_tripped and (dd is None or dd < risk.account_drawdown_stop_pct),
        "no peak recorded yet" if dd is None else
        f"account {dd:.1f}% below its peak (stop at {risk.account_drawdown_stop_pct:.0f}%"
        + ("; TRIPPED — no new trades until scripts\\reset_drawdown_stop.bat)" if ctx.account_drawdown_tripped else ")"))
    same = ctx.live_sides.get(rec["decision"]) or []
    add("no_same_direction", not same,
        f"a {rec['decision']} {', '.join(same)} of this pair is live — no second trade in the same direction"
        if same else f"no live {rec['decision']} position or order on this pair")
    add("stop_loss_present", sl is not None, "SL present" if sl is not None else "no SL — invalid (spec §0)")
    add("market_open", ctx.market_open, "open" if ctx.market_open else "execution market closed")
    add("quote_fresh", ctx.quote_age_s <= 30, f"quote age {ctx.quote_age_s:.1f}s (≤30s)")
    add("basis", ctx.basis_ok, ctx.basis_reason or "same instrument")
    ts, vu = parse_date_spec(rec["timestamp"]), parse_date_spec(rec["valid_until"])
    age = (ctx.now_ms - ts) / 1000
    add("not_expired", ctx.now_ms < vu, f"valid until {rec['valid_until']}")
    add("recommendation_age", age <= risk.max_recommendation_age_s, f"{age:.0f}s old (≤{risk.max_recommendation_age_s}s)")
    add("confidence", rec.get("confidence", 0) >= min_confidence, f"{rec.get('confidence')} (≥{min_confidence})")
    if sl is None:
        return GateResult(False, checks)
    spread = ctx.ask - ctx.bid
    entry = entry_price(rec, ctx.bid, ctx.ask)
    if rec["order_type"] == "MARKET" and rec["entry"].get("range_min") is not None:
        lo, hi = rec["entry"]["range_min"], rec["entry"]["range_max"]
        px = ctx.ask if buy else ctx.bid
        add("market_in_zone", lo <= px <= hi, f"touch {px} vs zone {lo}-{hi}")
    if rec["order_type"] != "MARKET":
        add("pending_price_valid", *pending_price_check(rec["order_type"], entry, ctx.bid, ctx.ask, ctx.stops_level_price))
    tps = rec["take_profits"]
    risk_dist = (entry - sl) if buy else (sl - entry)
    add("sl_side", risk_dist > 0, f"entry {entry} vs SL {sl}")
    if risk_dist <= 0:
        return GateResult(False, checks, entry)
    min_dist = max(ctx.stops_level_price + spread, risk.sl_atr_min_mult * ctx.atr)
    add("sl_min_distance", risk_dist >= min_dist,
        f"{risk_dist:.2f} ≥ max(stops_level+spread {ctx.stops_level_price + spread:.2f}, {risk.sl_atr_min_mult}×ATR {risk.sl_atr_min_mult * ctx.atr:.2f})")
    add("sl_max_distance", risk_dist <= risk.sl_atr_max_mult * ctx.atr,
        f"{risk_dist:.2f} ≤ {risk.sl_atr_max_mult}×ATR {risk.sl_atr_max_mult * ctx.atr:.2f}")
    add("spread_vs_sl", spread <= risk.max_spread_to_sl_ratio * risk_dist,
        f"spread {spread:.2f} ≤ {risk.max_spread_to_sl_ratio:.0%} of SL distance {risk_dist:.2f}")
    # reward measured to where a TP actually fills (a BUY's TP triggers on the bid, a SELL's on the ask)
    frac = sum(tp["close_fraction"] for tp in tps)
    reward = sum(((tp["price"] - entry) if buy else (entry - tp["price"])) * tp["close_fraction"] for tp in tps) / frac
    rr = reward / risk_dist
    ok = _rr_ok(rr, risk.min_rr)
    add("rr_after_costs", ok, f"{_rr_text(rr, risk.min_rr, ok)}")
    tgt = min(rec.get("risk_management", {}).get("risk_percent_suggested", risk.risk_per_trade_pct),
              risk.risk_per_trade_pct)
    size = size_position(equity=ctx.equity, target_risk_pct=tgt, max_risk_pct=risk.max_risk_per_trade_pct,
                         entry=entry, stop=sl, contract_size=ctx.contract_size, volume_min=ctx.volume_min,
                         volume_step=ctx.volume_step, volume_max=ctx.volume_max)
    add("position_size", size.ok, f"{size.lots} lots, risk {size.risk_pct:.2f}% (${size.risk_usd:.2f}) — {size.reason}")
    # at the minimum lot the backends place ONE position (largest close fraction): measure RR on that leg (Phase 3)
    single = None
    if size.ok and len(tps) > 1 and split_volume(size.lots, [t["close_fraction"] for t in tps], ctx.volume_step,
                                                 ctx.volume_min) is None:
        single = single_leg_index([t["close_fraction"] for t in tps])
        tp = tps[single]["price"]
        rr = ((tp - entry) if buy else (entry - tp)) / risk_dist
        i = next(k for k, c in enumerate(checks) if c[0] == "rr_after_costs")
        ok = _rr_ok(rr, risk.min_rr)
        checks[i] = ("rr_after_costs", ok, f"{_rr_text(rr, risk.min_rr, ok)} (single leg at TP{single + 1}: "
                                           f"{size.lots} lots cannot be split)")
    if size.ok:
        notional = size.lots * ctx.contract_size * entry
        lev = notional / ctx.equity
        add("effective_leverage", lev <= risk.max_effective_leverage, f"{lev:.1f}× ≤ {risk.max_effective_leverage}×")
    add("max_open_positions", ctx.open_positions < risk.max_open_positions,
        f"{ctx.open_positions} open positions/pending orders (< {risk.max_open_positions})")
    # a pair outside every correlated group is its own group: repeat positions on one pair are capped the same way
    group = next((g for g in correlated_groups if pair in g), [pair])
    others = sum(v for p, v in ctx.sibling_risk_pct_by_pair.items() if p in group)
    corr = sum(v for p, v in ctx.open_risk_pct_by_pair.items() if p in group) + others + (size.risk_pct if size.ok else 0)
    add("correlated_exposure", corr <= risk.max_correlated_risk_pct,
        f"group {group}: {corr:.2f}% open+new SL risk (≤ {risk.max_correlated_risk_pct}%)"
        + (f", of which {others:.2f}% held by the other pairs' systems" if others else ""))
    day = (ctx.realized_pnl_today_usd + ctx.unrealized_pnl_usd) / ctx.equity * 100
    add("daily_loss_limit", day > -risk.max_daily_loss_pct, f"today {day:.2f}% (limit −{risk.max_daily_loss_pct}%)")
    # worst case: every open position / pending order and this new trade stop out today — the day may still not
    # lose more than the limit (so the limit holds even when several trades fail together; gaps beyond SL excepted)
    realised = ctx.realized_pnl_today_usd / ctx.equity * 100
    open_risk = sum(ctx.open_risk_pct_by_pair.values())
    worst = realised - open_risk - (size.risk_pct if size.ok else 0)
    add("daily_loss_worst_case", worst >= -risk.max_daily_loss_pct,
        f"realised today {realised:.2f}% − open SL risk {open_risk:.2f}% − this trade "
        f"{size.risk_pct if size.ok else 0:.2f}% = {worst:.2f}% (≥ −{risk.max_daily_loss_pct}%)")
    approved = all(ok for _, ok, _ in checks)
    return GateResult(approved, checks, entry, size, rr, single)
