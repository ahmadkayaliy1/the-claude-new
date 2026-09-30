"""D-046 (b): the review pack counts the decisions whose data_quality_notes ask for more candles, depth or history -
the evidence for building the deferred MCP data tools (at >= 10 %); the persistent session stays reserved (per_call)."""
import importlib.util
import json
from pathlib import Path

import pytest

from tradingsystem.core.settings import AIProviderCfg, load_settings

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("review_pack_data_asks", ROOT / "tools" / "review_pack.py")
rp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rp)


def dec(notes, status="valid"):
    return {"status": status, "recommendation": json.dumps({"data_quality_notes": notes})}


def test_decisions_asking_for_more_data_are_counted():
    decs = [dec(["Need more 1h history to judge the range."]), dec(["depth unavailable for this pair"]),
            dec(["orderflow is real but short-horizon; not used as sole trigger here."]),     # the live call's note
            dec(["1h/4h structure.trend field says 'bearish' while EMA alignment is 'bullish'"]), dec([]),
            dec(["more candles would help"], status="invalid")]
    d = rp.data_asks(decs)
    assert d["decisions"] == 5 and d["asking_for_more_data"] == 2 and d["share"] == 0.4
    assert d["build_threshold"] == 0.10 and len(d["examples"]) == 2


def test_the_persistent_session_is_reserved_per_call_only():
    s = load_settings(env_path=Path("nope.env"))
    assert s.ai.providers["claude_code"].session_mode == "per_call"
    with pytest.raises(Exception):
        AIProviderCfg(kind="claude_code", model="sonnet", session_mode="persistent")
