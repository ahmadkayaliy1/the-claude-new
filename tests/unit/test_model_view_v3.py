"""The model's view of the Phase 5 checkpoint-B blocks (the lead's B7 step bumps VIEW_VERSION to 3 and adds the rest of
this file): B1 depth, B2 previous week / month levels, B3 the forming bar - rendered compactly, and the cached legend
(prompts/shared/payload_legend.md) names every column the view leaves out of the payload."""
import json
from pathlib import Path

from tradingsystem.ai.model_view import model_view
from tradingsystem.analysis.snapshot import DEPTH_COLUMNS

LEGEND = Path(__file__).resolve().parents[2] / "src" / "tradingsystem" / "ai" / "prompts" / "shared" / "payload_legend.md"
AS_OF = "2026-09-29T07:25:00.000Z"
DEPTH = {"data_quality": "real", "time": "2026-09-29T07:24:02.108Z", "age_s": 58, "band_columns": DEPTH_COLUMNS,
         "bands": [[0.1, 4.26, 6.07, -0.17], [0.25, 9.39, 9.99, -0.03], [0.5, 12.63, 14.76, -0.08]],
         "imbalance_change_1h": {"0.5": -0.11}}


def view(payload: dict) -> dict:
    return model_view({"meta": {"as_of": AS_OF}, **payload})


def test_depth_bands_keep_their_columns_out_of_the_view_and_the_legend_names_them():
    v = view({"orderflow": {"depth": DEPTH}})["orderflow"]["depth"]
    assert "band_columns" not in v and v["time"] == "09-29 07:24"
    assert v["bands"] == DEPTH["bands"] and v["imbalance_change_1h"] == {"0.5": -0.11}
    assert DEPTH["band_columns"] == ["band_pct", "bid_musd", "ask_musd", "imbalance"]
    text = LEGEND.read_text(encoding="utf-8")
    assert "[band_pct, bid_musd, ask_musd, imbalance]" in text                   # the names live in the legend
    assert "orderflow.depth" in text and "±1 %" in text and "±2/±5 %" in text    # bands appear only where reached
    assert len(json.dumps(v, separators=(",", ":"))) / 4 <= 250                   # the B1 budget


def test_a_stale_depth_block_has_no_bands_and_the_capability_group_says_why():
    stale = {"data_quality": "stale", "time": "2026-09-29T07:20:00.000Z", "age_s": 300,
             "reason": "newest depth snapshot is 300 s old (limit 120 s)"}
    caps = {"order_book_depth": {"quality": "unavailable", "reason": stale["reason"]},
            "footprint": {"quality": "real", "reason": "x"}}
    v = view({"orderflow": {"depth": stale}, "capabilities": caps})
    assert "bands" not in v["orderflow"]["depth"] and v["orderflow"]["depth"]["data_quality"] == "stale"
    assert v["capabilities"] == {"unavailable": {"order_book_depth": stale["reason"]}, "real": ["footprint"]}


def test_forming_and_the_period_levels_are_rendered_and_documented():
    tf = {"recent": [["2026-09-29T07:15:00.000Z", 1, 2, 0.5, 1.5, 3]],
          "forming": ["2026-09-29T07:20:00.000Z", 1.5, 2.5, 1.0, 2.0, 0.4, 5]}
    lv = {"pdh": 1.0, "pwh": 2.0, "pwl": 0.5, "pmh": 3.0, "pml": 0.1, "month_open": 1.1, "year_open": 0.9}
    v = view({"timeframes": {"5m": tf}, "levels": lv})
    assert v["timeframes"]["5m"]["forming"] == ["09-29 07:20", 1.5, 2.5, 1.0, 2.0, 0.4, 5]
    assert v["timeframes"]["5m"]["recent"] == [["09-29 07:15", 1, 2, 0.5, 1.5, 3]] and v["levels"] == lv
    text = LEGEND.read_text(encoding="utf-8")
    assert "[open_time, open, high, low, close, volume, age_s]" in text and "NOT in `recent`" in text
    for name in ("pwh", "pwl", "pmh", "pml", "month_open", "year_open"):
        assert f"`{name}`" in text


def test_the_1m_timeframe_leaves_the_view_but_stays_in_the_stored_payload():
    """B7 / D-046 (c): the 1m view pays for the new blocks; the stored payload keeps it (data gate, dashboard)."""
    from tradingsystem.ai.model_view import VIEW_VERSION
    tfs = {tf: {"bars": 30, "quality": {"status": "ok"}, "recent": [[1, 2, 3, 4, 5, 6]]} for tf in ("1h", "15m", "5m", "1m")}
    payload = {"timeframes": tfs}
    v = model_view({"meta": {"as_of": AS_OF}, **payload})
    assert set(v["timeframes"]) == {"1h", "15m", "5m"} and "1m" in payload["timeframes"]
    assert v["meta"]["view_version"] == VIEW_VERSION == "3"
    single = model_view({"meta": {"as_of": AS_OF}, "timeframe": {"1m": tfs["1m"]}})    # an analyst slice keeps it
    assert set(single["timeframe"]) == {"1m"}


def test_the_legend_names_every_new_market_block():
    """Shared blocks in the shared legend; the gold-only ones in the XAU appendix (BTC/ETH never pay for them)."""
    text = LEGEND.read_text(encoding="utf-8")
    for block in ("market.session_stats", "market.cross", "orderflow.daily_profiles", "timeframes.15m.forming",
                  "`pwh`/`pwl`", "min_stop_set_by", "fits_now", "orderflow.depth"):
        assert block in text, block
    gold = (LEGEND.parents[1] / "desks" / "xau.md").read_text(encoding="utf-8")
    for block in ("market.gold_clock", "market.news", "levels.round", "confirms_xau_extreme", "votes"):
        assert block in gold and block not in text, block
