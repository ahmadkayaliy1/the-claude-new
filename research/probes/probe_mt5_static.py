"""P1.5 — MT5 static probe: symbol specs, calc functions, order_check (never order_send)."""
from __future__ import annotations

import sys
from pathlib import Path

import MetaTrader5 as mt5

sys.path.insert(0, str(Path(__file__).parent))
from _report import Report  # noqa: E402

PATH = r"C:/Program Files/MetaTrader 5/terminal64.exe"
SYMBOLS = ["XAUUSD@", "BTCUSD@", "ETHUSD@"]
FIELDS = ["digits", "point", "trade_tick_size", "trade_tick_value", "trade_tick_value_profit", "trade_tick_value_loss",
          "trade_contract_size", "volume_min", "volume_max", "volume_step", "volume_limit", "spread", "spread_float",
          "trade_stops_level", "trade_freeze_level", "trade_mode", "trade_exemode", "filling_mode", "order_mode",
          "expiration_mode", "order_gtc_mode", "trade_calc_mode", "margin_initial", "margin_maintenance",
          "margin_hedged", "swap_mode", "swap_long", "swap_short", "swap_rollover3days", "currency_base",
          "currency_profit", "currency_margin", "session_deals", "session_volume", "chart_mode", "path", "description"]


def bits(v: int, names: dict[int, str]) -> str:
    return "|".join(n for b, n in names.items() if v & b) or str(v)


FILL = {1: "FOK", 2: "IOC", 4: "BOC"}
ORDER = {1: "MARKET", 2: "LIMIT", 4: "STOP", 8: "STOP_LIMIT", 16: "SL", 32: "TP", 64: "CLOSE_BY"}
EXPIRE = {1: "GTC", 2: "DAY", 4: "SPECIFIED", 8: "SPECIFIED_DAY"}


def main() -> None:
    if not mt5.initialize(path=PATH):
        raise SystemExit(f"MT5 initialize failed: {mt5.last_error()}")
    rep = Report("probe_mt5_static", "MetaTrader 5 (Windsor) — symbol specifications (P1.5)")
    ti, ai = mt5.terminal_info(), mt5.account_info()
    rep.h("Terminal & account")
    rep.table(["field", "value"], [
        ["terminal build", mt5.version()], ["connected", ti.connected], ["trade_allowed", ti.trade_allowed],
        ["tradeapi_disabled", ti.tradeapi_disabled], ["maxbars", ti.maxbars], ["ping_last (µs)", ti.ping_last],
        ["server", ai.server], ["trade_mode (0 demo/2 real)", ai.trade_mode], ["currency", ai.currency],
        ["leverage", ai.leverage], ["margin_mode (2=hedging)", ai.margin_mode], ["limit_orders", ai.limit_orders],
        ["margin_so_call / so_so", f"{ai.margin_so_call} / {ai.margin_so_so}"],
    ])
    rep.h("Symbol specifications")
    infos = {}
    for s in SYMBOLS:
        mt5.symbol_select(s, True)
        infos[s] = mt5.symbol_info(s)._asdict()
    rep.table(["field", *SYMBOLS], [[f, *[infos[s].get(f) for s in SYMBOLS]] for f in FIELDS])
    rep.table(["decoded", *SYMBOLS], [
        ["filling_mode", *[bits(infos[s]["filling_mode"], FILL) for s in SYMBOLS]],
        ["order_mode", *[bits(infos[s]["order_mode"], ORDER) for s in SYMBOLS]],
        ["expiration_mode", *[bits(infos[s]["expiration_mode"], EXPIRE) for s in SYMBOLS]],
    ])
    rep.raw["symbol_info"] = infos

    rep.h("Profit / margin per 1.00 lot (order_calc_*) at current prices")
    rows = []
    for s in SYMBOLS:
        tick = mt5.symbol_info_tick(s)
        info = infos[s]
        move = 100 * info["point"] * (100 if s.startswith("XAU") else 1000)
        p_buy = mt5.order_calc_profit(mt5.ORDER_TYPE_BUY, s, 1.0, tick.ask, tick.ask + move)
        p_sell = mt5.order_calc_profit(mt5.ORDER_TYPE_SELL, s, 1.0, tick.bid, tick.bid - move)
        m = mt5.order_calc_margin(mt5.ORDER_TYPE_BUY, s, 1.0, tick.ask)
        rows.append([s, tick.bid, tick.ask, round(tick.ask - tick.bid, info["digits"]), move, p_buy, p_sell, m,
                     round(p_buy / move, 4) if p_buy else None])
    rep.table(["symbol", "bid", "ask", "spread", "price move", "profit BUY 1 lot", "profit SELL 1 lot",
               "margin 1 lot", "USD per 1.0 price unit per lot"], rows)
    rep.p("USD per price unit per lot = contract size for USD-quoted symbols → position size for a given risk is "
          "`risk_usd / (|entry − SL| × usd_per_unit)` rounded **down** to `volume_step`.")

    rep.h("order_check (validation only — nothing is sent)")
    rows = []
    for s in SYMBOLS:
        tick, info = mt5.symbol_info_tick(s), infos[s]
        dist = max(info["trade_stops_level"] * 3, 100) * info["point"]
        for label, req in {
            "market BUY 0.01 with SL/TP": dict(action=mt5.TRADE_ACTION_DEAL, type=mt5.ORDER_TYPE_BUY, price=tick.ask,
                                               sl=round(tick.ask - dist, info["digits"]), tp=round(tick.ask + 2 * dist, info["digits"])),
            "BUY LIMIT 0.01 below": dict(action=mt5.TRADE_ACTION_PENDING, type=mt5.ORDER_TYPE_BUY_LIMIT,
                                         price=round(tick.ask - 2 * dist, info["digits"]),
                                         sl=round(tick.ask - 3 * dist, info["digits"]), tp=round(tick.ask, info["digits"])),
            "SL inside stops_level": dict(action=mt5.TRADE_ACTION_DEAL, type=mt5.ORDER_TYPE_BUY, price=tick.ask,
                                          sl=round(tick.bid - info["point"], info["digits"]), tp=0.0),
        }.items():
            r = mt5.order_check({"symbol": s, "volume": 0.01, "deviation": 0, "magic": 1,
                                 "type_filling": mt5.ORDER_FILLING_FOK, "type_time": mt5.ORDER_TIME_GTC, **req})
            rows.append([s, label, r.retcode if r else mt5.last_error(), r.comment if r else "",
                         round(r.margin, 2) if r else "", round(r.margin_free, 2) if r else ""])
    rep.table(["symbol", "request", "retcode (0 = would pass)", "comment", "margin", "margin_free after"], rows)
    mt5.shutdown()
    rep.save()


if __name__ == "__main__":
    main()
