import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "real"
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))


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
