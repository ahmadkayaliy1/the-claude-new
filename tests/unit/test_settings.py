import copy
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from tradingsystem.core.instruments import InstrumentRegistry
from tradingsystem.core.settings import DEFAULT_CONFIG, LIVE_CONFIRMATION_PHRASE, load_settings

BASE = yaml.safe_load(DEFAULT_CONFIG.read_text(encoding="utf-8"))
NO_ENV = Path("does-not-exist.env")


def _write(tmp_path, data):
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    return p


def _load(tmp_path, data=None, **env):
    return load_settings(_write(tmp_path, data or BASE), env_path=NO_ENV, extra_env=env)


def test_default_config_is_valid(tmp_path):
    s = _load(tmp_path)
    assert s.ai.active_provider == "gemini"
    assert s.execution.mode == "paper"
    assert len(s.config_hash) == 16


def test_one_line_provider_switch(tmp_path):
    s = _load(tmp_path, ACTIVE_AI_PROVIDER="anthropic", AGENT_MODE="agent_per_timeframe")
    assert s.ai.active_provider == "anthropic"
    assert s.ai.agent_mode == "agent_per_timeframe"


def test_unknown_provider_rejected(tmp_path):
    with pytest.raises(ValidationError):
        _load(tmp_path, ACTIVE_AI_PROVIDER="nope")


def test_live_mode_requires_confirmation(tmp_path):
    with pytest.raises(ValidationError):
        _load(tmp_path, EXECUTION_MODE="live")
    data = copy.deepcopy(BASE)
    data["execution"]["live_confirmation"] = LIVE_CONFIRMATION_PHRASE
    assert _load(tmp_path, data, EXECUTION_MODE="live").execution.mode == "live"


def test_pair_needs_one_primary(tmp_path):
    data = copy.deepcopy(BASE)
    data["pairs"]["XAUUSD"]["instruments"][0]["roles"] = ["execution"]
    with pytest.raises(ValidationError):
        _load(tmp_path, data)


def test_api_must_bind_loopback(tmp_path):
    data = copy.deepcopy(BASE)
    data["api"]["host"] = "0.0.0.0"
    with pytest.raises(ValidationError):
        _load(tmp_path, data)


def test_unknown_keys_rejected(tmp_path):
    data = copy.deepcopy(BASE)
    data["risk"]["risk_per_trad_pct"] = 1  # typo must not be silently ignored
    with pytest.raises(ValidationError):
        _load(tmp_path, data)


def test_registry_tables_and_paths(tmp_path):
    s = _load(tmp_path)
    reg = InstrumentRegistry.from_settings(s)
    gold = reg.primary("XAUUSD")
    assert gold.key == "mt5:XAUUSD@"
    assert gold.table("candles", s.timeframes[0]) == "xauusd_candles_1m"
    assert gold.hot_db_path(Path("data")).as_posix() == "data/hot/mt5/XAUUSD.db"
    flow = reg.with_role("XAUUSD", "flow_context")
    assert [i.key for i in flow] == ["binance_usdm:XAUUSDT"]
    assert reg.primary("BTCUSDT").table("agg_trades") == "btcusdt_agg_trades"


def test_adding_a_pair_is_config_only(tmp_path):
    data = copy.deepcopy(BASE)
    data["pairs"]["SOLUSDT"] = {
        "asset_class": "crypto", "pip_size": 0.01,
        "instruments": [{"venue": "binance_spot", "symbol": "SOLUSDT",
                         "roles": ["analysis_primary", "execution"], "datatypes": ["candles"]}],
    }
    reg = InstrumentRegistry.from_settings(_load(tmp_path, data))
    assert reg.primary("SOLUSDT").table("candles", reg.primary("SOLUSDT").timeframes[2]) == "solusdt_candles_15m"
