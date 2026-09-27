"""RISK-02: MT5Backend.account() returns the same keys as the paper backend (risk by *pair*, floating PnL, pending
orders counted) and the executor feeds the broker's live specs into the gate. A fake ``mt5`` namespace stands in for
the terminal (pure logic — nothing is ever sent to MetaTrader)."""
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

import tradingsystem.execution.executor as ex_mod
import tradingsystem.ingest.mt5.terminal as term_mod
from tradingsystem.ai.store import DecisionRecord
from tradingsystem.core.settings import PathsCfg, load_settings
from tradingsystem.core.timeutil import iso, now_ms
from tradingsystem.execution.backends.mt5_backend import MT5Backend
from tradingsystem.execution.backends.paper import PaperBackend, Tick

CONTRACT = {"mode", "currency", "balance", "equity", "unrealized_usd", "open_positions", "open_orders",
            "open_risk_pct_by_pair", "realized_today_usd"}
MAGIC = 26092501
SIZE = {"XAUUSD@": 100, "BTCUSD@": 1, "EURUSD@": 100_000}


def fake_mt5(positions=(), orders=(), sent=None):
    def calc(otype, symbol, volume, p_open, p_close):
        return (p_close - p_open) * (1 if otype == 0 else -1) * volume * SIZE[symbol]

    def refuse(req):
        sent.append(req)
        raise AssertionError("an order reached the (fake) terminal")

    return NS(POSITION_TYPE_BUY=0, POSITION_TYPE_SELL=1, ORDER_TYPE_BUY=0, ORDER_TYPE_SELL=1, ORDER_TYPE_BUY_LIMIT=2,
              ORDER_TYPE_SELL_LIMIT=3, ORDER_TYPE_BUY_STOP=4, ORDER_TYPE_SELL_STOP=5,
              positions_get=lambda: list(positions), orders_get=lambda: list(orders), order_calc_profit=calc,
              history_deals_get=lambda a, b: [], order_send=refuse, order_check=refuse, last_error=lambda: (0, ""),
              symbol_info=lambda s: NS(trade_stops_level=37, point=0.01, volume_min=0.01, volume_step=0.01, volume_max=7.0,
                                       trade_contract_size=100, digits=2, filling_mode=1))


def terminal(mt5, server="WindsorBrokers1-Demo"):
    acc = NS(trade_mode=0, server=server, currency="USD", balance=10_003.5, equity=10_000.0, margin_free=9_000.0)
    return NS(mt5=mt5, account=lambda: acc, profile=NS(server=server), connect=lambda: None)


def test_mt5_account_has_the_paper_contract(tmp_path):
    pos = [NS(symbol="XAUUSD@", type=0, volume=0.02, price_open=2400.0, sl=2390.0, profit=-3.0, swap=-0.5,
              magic=MAGIC, comment="ts:aaaa:1"),
           NS(symbol="XAUUSD@", type=0, volume=5.0, price_open=2400.0, sl=0.0, profit=900.0, swap=0.0,
              magic=1, comment="manual")]                                          # other EA / manual: ignored
    orders = [NS(symbol="XAUUSD@", type=2, volume_current=0.01, price_open=2380.0, sl=2370.0, magic=MAGIC, comment="ts:bbbb:1"),
              NS(symbol="BTCUSD@", type=5, volume_current=0.01, price_open=60_000.0, sl=61_000.0, magic=MAGIC,
                 comment="ts:cccc:1"),
              NS(symbol="EURUSD@", type=3, volume_current=0.01, price_open=1.2, sl=1.21, magic=MAGIC, comment="ts:dddd:1"),
              NS(symbol="XAUUSD@", type=3, volume_current=0.01, price_open=2450.0, sl=2460.0, magic=MAGIC, comment="ts:aaaa:2")]
    b = MT5Backend(terminal(fake_mt5(pos, orders)), MAGIC, expected_account_type="demo",
                   pair_by_symbol={"XAUUSD@": "XAUUSD", "BTCUSD@": "BTCUSDT"})
    acct = b.account()
    assert CONTRACT <= set(acct) and CONTRACT <= set(PaperBackend(tmp_path / "app.db", 100).account())
    # XAU: position 10×0.02×100 + BUY_LIMIT 10×0.01×100 + SELL_LIMIT of the same decision 10×0.01×100 = $40
    assert acct["open_risk_pct_by_pair"]["XAUUSD"] == pytest.approx(40 / 10_000 * 100)
    assert acct["open_risk_pct_by_pair"]["BTCUSDT"] == pytest.approx(1000 * 0.01 / 10_000 * 100)
    assert acct["open_risk_pct_by_pair"]["EURUSD@"] == pytest.approx(0.01 * 0.01 * 100_000 / 10_000 * 100)   # never dropped
    assert acct["unrealized_usd"] == pytest.approx(-3.5)
    assert acct["open_positions"] == 4 and acct["open_orders"] == 3                    # aaaa has a position


def test_executor_uses_broker_specs_and_mt5_account(tmp_path, monkeypatch):
    sent = []
    mt5 = fake_mt5(sent=sent)
    monkeypatch.setattr(term_mod, "MT5Terminal", lambda prof: terminal(mt5, prof.server))
    s = load_settings(env_path=Path("nope.env"), extra_env={"EXECUTION_MODE": "demo", "EXECUTION_TRIGGER": "manual"})
    s = s.model_copy(update={"paths": PathsCfg(data_dir=str(tmp_path / "data"), logs_dir=str(tmp_path / "logs"))})
    ex = ex_mod.Executor(s)
    assert ex.mt5.pair_by_symbol == {"BTCUSD@": "BTCUSDT", "ETHUSD@": "ETHUSDT", "XAUUSD@": "XAUUSD"}
    now = now_ms()
    monkeypatch.setattr(ex, "latest_quote", lambda key, max_age_ms=None: Tick(now - 500, 2400.0, 2400.3, now * 1000))
    monkeypatch.setattr(ex, "atr", lambda pair, as_of=None: 7.0)
    monkeypatch.setattr(ex_mod, "calendar_for", lambda *a: NS(is_open=lambda t: False))    # closed → never places
    r = {"pair": "XAUUSD", "timestamp": iso(now - 5_000), "valid_until": iso(now + 3_600_000), "decision": "BUY",
         "order_type": "BUY_LIMIT", "entry": {"price": 2390.0}, "stop_loss": 2380.0,
         "take_profits": [{"price": 2420.0, "close_fraction": 1.0}], "confidence": 70,
         "risk_management": {"risk_percent_suggested": 0.5}}
    ex.store.save(DecisionRecord(pair="XAUUSD", mode="test", trigger="test", status="valid", id="m1", recommendation=r))
    ex.store.set_execution_state("m1", "queued")
    ex.process_candidates()
    st, det = ex.state_of("m1")
    gate = {g["check"]: g for g in det["gate"]}
    assert st == "rejected" and not gate["market_open"]["ok"] and not sent
    assert "0.37" in gate["pending_price_valid"]["detail"]               # live stops level (37 × 0.01), not the table
    assert gate["correlated_exposure"]["ok"] and gate["daily_loss_limit"]["ok"]
    assert det["spread_at_gate"] == pytest.approx(0.3)                   # Phase 4: the gate-time spread as a number
