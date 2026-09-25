"""MetaTrader 5 trade server return codes (MQL5 TRADE_RETCODE_*) → meaning, retry policy (P9.4, no silent failures)."""
from __future__ import annotations

# code: (name, human meaning, retryable)
RETCODES: dict[int, tuple[str, str, bool]] = {
    0: ("OK", "order_check passed", False),
    10004: ("REQUOTE", "requote", True),
    10006: ("REJECT", "request rejected", False),
    10007: ("CANCEL", "request cancelled by trader", False),
    10008: ("PLACED", "order placed", False),
    10009: ("DONE", "request completed", False),
    10010: ("DONE_PARTIAL", "only part of the request was completed", False),
    10011: ("ERROR", "request processing error", False),
    10012: ("TIMEOUT", "request timed out (verify before retrying!)", True),
    10013: ("INVALID", "invalid request", False),
    10014: ("INVALID_VOLUME", "invalid volume", False),
    10015: ("INVALID_PRICE", "invalid price", False),
    10016: ("INVALID_STOPS", "invalid stops (SL/TP too close or wrong side)", False),
    10017: ("TRADE_DISABLED", "trading is disabled", False),
    10018: ("MARKET_CLOSED", "market is closed", False),
    10019: ("NO_MONEY", "not enough money", False),
    10020: ("PRICE_CHANGED", "prices changed", True),
    10021: ("PRICE_OFF", "no quotes to process the request", True),
    10022: ("INVALID_EXPIRATION", "invalid order expiration", False),
    10023: ("ORDER_CHANGED", "order state changed", False),
    10024: ("TOO_MANY_REQUESTS", "too frequent requests", True),
    10025: ("NO_CHANGES", "no changes in request", False),
    10026: ("SERVER_DISABLES_AT", "autotrading disabled by server", False),
    10027: ("CLIENT_DISABLES_AT", "autotrading disabled in the terminal (enable Algo Trading)", False),
    10028: ("LOCKED", "request locked for processing", True),
    10029: ("FROZEN", "order or position frozen", False),
    10030: ("INVALID_FILL", "unsupported filling mode", False),
    10031: ("CONNECTION", "no connection with the trade server", True),
    10032: ("ONLY_REAL", "operation allowed only for live accounts", False),
    10033: ("LIMIT_ORDERS", "pending orders limit reached", False),
    10034: ("LIMIT_VOLUME", "volume limit for the symbol reached", False),
    10035: ("INVALID_ORDER", "incorrect or prohibited order type", False),
    10036: ("POSITION_CLOSED", "position already closed", False),
    10038: ("INVALID_CLOSE_VOLUME", "close volume exceeds position volume", False),
    10039: ("CLOSE_ORDER_EXIST", "a close order already exists", False),
    10040: ("LIMIT_POSITIONS", "open positions limit reached", False),
    10041: ("REJECT_CANCEL", "pending order activation rejected, order cancelled", False),
    10042: ("LONG_ONLY", "only long positions allowed", False),
    10043: ("SHORT_ONLY", "only short positions allowed", False),
    10044: ("CLOSE_ONLY", "only position closing allowed", False),
    10045: ("FIFO_CLOSE", "FIFO rule: close the oldest position first", False),
    10046: ("HEDGE_PROHIBITED", "opposite positions prohibited (hedging disabled)", False),
}
SUCCESS = {10008, 10009}


def describe(code: int | None) -> str:
    if code is None:
        return "no result (terminal did not answer)"
    name, text, _ = RETCODES.get(code, ("UNKNOWN", f"unknown retcode {code}", False))
    return f"{code} {name}: {text}"


def retryable(code: int | None) -> bool:
    return code is None or RETCODES.get(code, ("", "", False))[2]
