"""Snapshot builder on the live database (integration): determinism, as-of causality, data-quality flags."""
import json
import re

import pytest

from tradingsystem.core.timeutil import MS_PER_HOUR, now_ms, parse_date_spec

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def builder():
    from tradingsystem.analysis.snapshot import SnapshotBuilder
    from tradingsystem.core.instruments import InstrumentRegistry
    from tradingsystem.core.settings import load_settings
    s = load_settings()
    b = SnapshotBuilder(s, InstrumentRegistry.from_settings(s))
    yield b
    b.close()


ISO = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z")


@pytest.mark.parametrize("pair", ["BTCUSDT", "ETHUSDT", "XAUUSD"])
def test_deterministic_and_causal(builder, pair):
    as_of = (now_ms() - 2 * MS_PER_HOUR) // 60_000 * 60_000
    a = builder.build(pair, as_of)
    b = builder.build(pair, as_of)
    assert a["meta"]["payload_hash"] == b["meta"]["payload_hash"]
    body = json.dumps({k: v for k, v in a.items() if k not in ("market", "meta")})
    latest = max(parse_date_spec(t) for t in ISO.findall(body))
    assert latest < as_of, "payload contains data from after its as-of time"


def test_quality_flags_follow_capabilities(builder):
    x = builder.build("XAUUSD", now_ms())
    assert x["orderflow"]["bar_delta"]["data_quality"] == "unavailable"
    assert x["orderflow"]["footprint"]["data_quality"] == "unavailable"
    assert x["derivatives"]["data_quality"] == "proxy"
    assert x["timeframes"]["15m"]["indicators"]["vwap_quality"] == "approx"
    b = builder.build("BTCUSDT", now_ms())
    assert b["orderflow"]["bar_delta"]["data_quality"] == "real"
    assert b["derivatives"]["data_quality"] == "real"
    assert b["meta"]["price_reference"] == "binance_spot:BTCUSDT"
    assert b["meta"]["execution_instrument"] == "mt5:BTCUSD@"
