"""Phase 3 §3.7.4 acceptance: replaying 24 h of stored market data through the trigger policy fires at most
``ai.daily_calls_per_pair`` setup/idle calls per pair (reviews and events come on top and are rationed by the cap).

Integration: reads the production data root read-only (``tools/replay_triggers.py``); nothing is written."""
import importlib.util
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _replay():
    spec = importlib.util.spec_from_file_location("replay_triggers", ROOT / "tools" / "replay_triggers.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.replay


@pytest.mark.integration
@pytest.mark.parametrize("pair", ["BTCUSDT", "ETHUSDT", "XAUUSD"])
def test_replay_budget(pair):
    data_dir = os.environ.get("TS_REPLAY_DATA_DIR", r"C:\the_claude_new\data")
    if not Path(data_dir, "hot").exists():
        pytest.skip(f"no stored data under {data_dir}")
    out = _replay()(pair, 24.0, None, data_dir)
    if not out["screens"]:
        pytest.skip(f"{pair}: market closed for the whole window")
    assert out["calls_per_day"] <= out["cap"], out["by_strength"]
