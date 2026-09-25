"""MetaTrader 5 terminal access (P4.1).

Rules (D-019/D-023, docs/exploration/mt5_multiclient.md):
* Always attach with a pinned ``path`` — ``initialize(path)`` relaunches a closed terminal and it auto-logs
  into its saved account. Credentials are passed only when explicitly configured (owner process).
* Assert the connected account (server + demo/real) matches the configured profile before using it; never
  ingest from, or trade on, an unexpected account.
* MT5 calls hold the GIL — anything latency-sensitive runs in another process; hung calls are detected by the
  supervisor through the heartbeat (a Python watchdog thread in the same process cannot preempt them).
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

from ...core.settings import MT5ProfileCfg

log = logging.getLogger(__name__)
_TRADE_MODE = {"demo": 0, "real": 2}


class AccountMismatch(RuntimeError):
    pass


class MT5Unavailable(RuntimeError):
    pass


@dataclass
class AccountSnapshot:
    login: int
    server: str
    trade_mode: int
    currency: str
    leverage: int
    balance: float
    equity: float
    margin_free: float


class MT5Terminal:
    def __init__(self, profile: MT5ProfileCfg, *, use_credentials: bool = False) -> None:
        import MetaTrader5 as mt5

        self.mt5 = mt5
        self.profile = profile
        self.use_credentials = use_credentials
        self.connected = False

    def connect(self) -> AccountSnapshot:
        kwargs: dict[str, Any] = {"path": self.profile.terminal_path}
        if self.use_credentials and self.profile.login_env and os.environ.get(self.profile.login_env):
            kwargs.update(login=int(os.environ[self.profile.login_env]),
                          password=os.environ.get(self.profile.password_env or "", ""), server=self.profile.server)
        if not self.mt5.initialize(**kwargs):
            raise MT5Unavailable(f"initialize failed: {self.mt5.last_error()}")
        acc = self.account()
        if acc.server != self.profile.server:
            self.mt5.shutdown()
            raise AccountMismatch(f"terminal is on {acc.server!r}, expected {self.profile.server!r}")
        if acc.trade_mode != _TRADE_MODE[self.profile.account_type]:
            self.mt5.shutdown()
            raise AccountMismatch(f"account trade_mode {acc.trade_mode} is not {self.profile.account_type}")
        self.connected = True
        return acc

    def account(self) -> AccountSnapshot:
        a = self.mt5.account_info()
        if a is None:
            raise MT5Unavailable(f"account_info failed: {self.mt5.last_error()}")
        return AccountSnapshot(a.login, a.server, a.trade_mode, a.currency, a.leverage, a.balance, a.equity,
                               a.margin_free)

    def healthy(self) -> bool:
        ti = self.mt5.terminal_info()
        return bool(ti is not None and ti.connected)

    def ensure(self) -> None:
        if not self.connected or not self.healthy():
            self.shutdown()
            self.connect()

    def select_symbols(self, names: list[str]) -> dict[str, dict]:
        out = {}
        for n in names:
            if not self.mt5.symbol_select(n, True):
                raise MT5Unavailable(f"symbol {n!r} not available on {self.profile.server}: {self.mt5.last_error()}")
            out[n] = self.mt5.symbol_info(n)._asdict()
        return out

    def timeframe(self, attr: str) -> int:
        return getattr(self.mt5, attr)

    def shutdown(self) -> None:
        try:
            self.mt5.shutdown()
        except Exception:  # noqa: BLE001
            pass
        self.connected = False
