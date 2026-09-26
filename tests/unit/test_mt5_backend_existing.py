"""MT5Backend.existing(): an unreachable terminal is 'unknown', never 'nothing was placed' (integration review of
EXE-03 reconciliation). Pure control flow on the MetaTrader5 call contract (None + last_error on failure)."""
from types import SimpleNamespace

import pytest

from tradingsystem.execution.backends.mt5_backend import MT5Backend


def backend(answer, err):
    b = object.__new__(MT5Backend)
    b.mt5 = SimpleNamespace(orders_get=lambda: answer, positions_get=lambda: answer,
                            history_deals_get=lambda a, b: answer, last_error=lambda: err)
    return b


def test_link_down_raises_instead_of_reporting_nothing():
    with pytest.raises(RuntimeError, match="state unknown"):
        backend(None, (-10004, "No IPC connection")).existing("d" * 32)


def test_empty_answers_are_empty():
    assert backend(None, (1, "Success")).existing("d" * 32) == {"orders": [], "positions": [], "deals": []}
    assert backend((), (1, "Success")).existing("d" * 32) == {"orders": [], "positions": [], "deals": []}


def test_only_this_decisions_tag_is_counted():
    mine, other = SimpleNamespace(comment="ts:" + "d" * 20 + ":0"), SimpleNamespace(comment="ts:" + "e" * 20 + ":0")
    got = backend((mine, other), (1, "Success")).existing("d" * 32)
    assert got["orders"] == [mine] and got["positions"] == [mine] and got["deals"] == [mine]
