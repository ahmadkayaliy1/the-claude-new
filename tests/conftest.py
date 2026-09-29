import os
import sys
from pathlib import Path

import pytest

# Phase 4: the notifier must never pop a toast on the user's desktop or send a Telegram message from the unit suite
# (engine / executor / management tests build real services). tests/unit/test_notify.py removes it where needed.
os.environ["TS_NOTIFY_DISABLE"] = "1"
# … and never start a billed Claude diagnosis session (tools/monitor.py); test_monitor.py removes it in its fixture
os.environ["TS_MONITOR_NO_DIAGNOSE"] = "1"

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "real"
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))


def no_desk(s):
    """The settings with every pair's desk removed. config.yaml puts XAUUSD in shadow (D-049 / B14: never traded), so
    the executor tests that trade XAUUSD on paper (a stand-in for any traded pair) run with the desk off."""
    return s.model_copy(update={"pairs": {k: p.model_copy(update={"desk": None}) for k, p in s.pairs.items()}})


@pytest.fixture(scope="session")
def real_aggtrades():
    import pyarrow.csv as pacsv
    return pacsv.read_csv(FIXTURES / "btcusdt_aggtrades_3000.csv")


@pytest.fixture(scope="session")
def real_candles_1m():
    import pyarrow.csv as pacsv
    return pacsv.read_csv(FIXTURES / "btcusdt_candles_1m_3000.csv")


@pytest.fixture(scope="session")
def real_xau_ticks():
    import pyarrow.csv as pacsv
    return pacsv.read_csv(FIXTURES / "xauusd_ticks_3000.csv")
