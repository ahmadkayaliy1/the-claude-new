"""Rebuild the risk gate's inputs from a stored gate record (``ai_decisions.execution_detail.gate``) - real rows of the
production BTCUSDT / ETHUSDT systems (tests/fixtures/real/gate_replay_btc_eth.json) - and run the gate again.

The record shows every input through its check's detail text (quote age, spread, ATR bound, stops level, open
positions, exposure, the day's PnL, the recommendation age); this parses them back. A row whose record cannot be parsed
(an older check set, an input the record does not show) is skipped by the caller - never guessed."""
from __future__ import annotations

import re

from tradingsystem.core.instruments import InstrumentRegistry
from tradingsystem.core.settings import Settings
from tradingsystem.core.timeutil import parse_date_spec
from tradingsystem.execution.price_mapping import translate
from tradingsystem.execution.risk_gate import ExecContext, GateResult, evaluate

_NUM = r"(-?\d+(?:\.\d+)?)"


class Unreplayable(Exception):
    pass


def _find(pat: str, text: str) -> re.Match:
    m = re.search(pat, text)
    if not m:
        raise Unreplayable(f"{pat!r} not in {text!r}")
    return m


def rebuild(row: dict, s: Settings) -> tuple[dict, str, ExecContext]:
    """(translated recommendation, pair, gate context) of one stored gate record."""
    pair, rec, det = row["pair"], row["rec"], row["detail"]
    g = {c["check"]: c for c in det["gate"]}
    text = {k: v["detail"] for k, v in g.items()}
    if "account_drawdown" not in g or "no_same_direction" not in g or "daily_loss_worst_case" not in g:
        raise Unreplayable("record of an older check set")
    exe = InstrumentRegistry.from_settings(s).with_role(pair, "execution")[0]
    c = exe.contract
    basis = (det.get("translation") or {}).get("basis") or 0.0
    rec_x = translate(rec, basis, c["tick_size"]) if det.get("translation") else rec
    buy = rec["decision"] == "BUY"
    spread = float(_find(r"spread " + _NUM, text["spread_vs_sl"]).group(1))
    # the touch the gate judged: a market entry is the ask (BUY) / bid (SELL); a pending order's own check names it
    if "pending_price_valid" in g:
        m = _find(r"from (ask|bid) " + _NUM, text["pending_price_valid"]) if " from " in text["pending_price_valid"]             else _find(r"vs (ask|bid) " + _NUM, text["pending_price_valid"])
        side, touch = m.group(1), float(m.group(2))
    else:
        side = "ask" if buy else "bid"
        touch = float(_find(r"touch " + _NUM, text["market_in_zone"]).group(1)) if "market_in_zone" in g             else det["executed_levels"]["entry"]
    ask, bid = (touch, touch - spread) if side == "ask" else (touch + spread, touch)
    age_s = float(_find(r"quote age " + _NUM, text["quote_fresh"]).group(1))
    ts = parse_date_spec(rec["timestamp"])
    rec_age = float(_find(_NUM + r"s old", text["recommendation_age"]).group(1))
    # the ATR is shown twice, rounded to 2 decimals: through the wider bound it is known to 0.002
    atr = float(_find(r"×ATR " + _NUM, text["sl_max_distance"]).group(1)) / s.risk.sl_atr_max_mult
    stops = float(_find(r"stops_level\+spread " + _NUM, text["sl_min_distance"]).group(1)) - spread
    eq = det["equity_at_entry"]
    nopen = int(_find(r"^(\d+) open positions", text["max_open_positions"]).group(1))
    real_pct, open_pct = (float(x) for x in _find(r"realised today " + _NUM + r"% − open SL risk " + _NUM + "%",
                                                  text["daily_loss_worst_case"]).groups())
    day_pct = float(_find(r"today " + _NUM + "%", text["daily_loss_limit"]).group(1))
    realised = real_pct * eq / 100
    other = re.search(r"of which " + _NUM + "% held by the other pairs", text["correlated_exposure"])
    others = float(other.group(1)) if other else 0.0
    sib = {next(p for p in ("BTCUSDT", "ETHUSDT") if p != pair): others} if others else {}
    dd = re.search(r"account " + _NUM + r"% below its peak", text["account_drawdown"])
    same = re.search(r"a (?:BUY|SELL) (.*) of this pair is live", text["no_same_direction"])
    ctx = ExecContext(
        now_ms=int(ts + rec_age * 1000), bid=bid, ask=ask, quote_age_s=age_s, market_open=g["market_open"]["ok"],
        atr=atr, stops_level_price=stops, contract_size=c["contract_size"], volume_min=c["volume_min"],
        volume_step=c["volume_step"], volume_max=None, equity=eq, open_positions=nopen,
        open_risk_pct_by_pair={pair: open_pct} if open_pct else {},
        realized_pnl_today_usd=realised, unrealized_pnl_usd=day_pct * eq / 100 - realised,
        kill_switch=not g["kill_switch"]["ok"], basis_ok=g["basis"]["ok"], basis_reason=text["basis"],
        sibling_risk_pct_by_pair=sib, live_sides={rec["decision"]: [same.group(1)]} if same else {},
        account_drawdown_pct=float(dd.group(1)) if dd else None,
        account_drawdown_tripped=not g["account_drawdown"]["ok"])
    return rec_x, pair, ctx


def replay(row: dict, s: Settings, *, min_confidence: int = 55) -> GateResult:
    rec_x, pair, ctx = rebuild(row, s)
    return evaluate(rec_x, pair, ctx, s.risk, s.risk.correlated_groups, min_confidence=min_confidence)


def as_records(gate: GateResult) -> list[dict]:
    return [{"check": n, "ok": ok, "detail": d} for n, ok, d in gate.checks]
