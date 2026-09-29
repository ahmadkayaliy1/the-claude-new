"""Capability registry (P6.12, spec §3.2 "what suits each pair").

For every pair, each analysis family is classified from the instruments actually configured (and the data
they really provide, docs/data_availability.md):
  real        — computed from genuine data of the analysed instrument
  approx      — computed from a stand-in that is disclosed (e.g. tick volume instead of traded volume)
  proxy       — computed from a *different* related instrument (e.g. XAUUSDT perp order flow for spot gold)
  unavailable — not computed (no honest data source)
Every payload block carries its flag; prompts require the model to disclose approx/proxy evidence.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..core.instruments import Instrument, InstrumentRegistry
from ..core.settings import Settings
from .cross import neighbours


@dataclass(frozen=True)
class Capability:
    quality: str        # real | approx | proxy | unavailable
    source: str | None  # instrument key the analysis is computed from
    reason: str


ANALYSES = ("price_indicators", "market_structure_smc", "price_action", "liquidity", "volume_weighted",
            "bar_delta_cvd", "footprint", "volume_profile", "time_profile_tpo", "order_book_depth", "derivatives",
            "cross_asset", "news_calendar")


def capability_matrix(settings: Settings, reg: InstrumentRegistry, pair: str) -> dict[str, Capability]:
    pcfg = settings.pairs[pair]
    primary = reg.primary(pair)
    flow = [i for i in reg.with_role(pair, "flow_context")]
    binance_primary = primary.venue.startswith("binance")
    usdm = next((i for i in flow if i.venue == "binance_usdm"), None)
    out: dict[str, Capability] = {}
    price_src = f"real OHLC of {primary.key}"
    out["price_indicators"] = Capability("real", primary.key, price_src)
    out["market_structure_smc"] = Capability("real", primary.key, price_src)
    out["price_action"] = Capability("real", primary.key, price_src)
    out["liquidity"] = Capability("real", primary.key, price_src)
    out["time_profile_tpo"] = Capability("real", primary.key, "time-at-price from real 1m bars")

    if binance_primary:
        out["volume_weighted"] = Capability("real", primary.key, "exchange traded volume")
        out["bar_delta_cvd"] = Capability("real", primary.key, "exact taker-buy volume in Binance klines")
        out["footprint"] = (Capability("real", primary.key, "Binance aggTrades (taker side known)")
                            if "agg_trades" in primary.datatypes else
                            Capability("unavailable", None, "aggTrades not collected"))
        out["volume_profile"] = Capability("real", primary.key, "traded volume (footprint / kline volume)")
        out["order_book_depth"] = (Capability("real", primary.key, "REST depth snapshots in ±% bands")
                                   if "depth" in primary.datatypes else
                                   Capability("unavailable", None, "depth not collected"))
    else:
        out["volume_weighted"] = Capability("approx", primary.key, "tick volume only — broker reports no traded volume")
        out["volume_profile"] = Capability("approx", primary.key, "tick-volume distribution — not traded volume")
        if usdm is not None and pcfg.flow_proxy_approved and "agg_trades" in usdm.datatypes:
            reason = f"order flow of {usdm.key} (different instrument; approved by P1.11 study)"
            out["bar_delta_cvd"] = Capability("proxy", usdm.key, reason)
            out["footprint"] = Capability("proxy", usdm.key, reason)
        else:
            why = ("no trades / real volume on the broker feed; proxy "
                   + (f"{usdm.key} pending validation (P1.11)" if usdm else "not configured"))
            out["bar_delta_cvd"] = Capability("unavailable", None, why)
            out["footprint"] = Capability("unavailable", None, why)
        out["order_book_depth"] = Capability("unavailable", None, "broker DOM is empty (P1.5)")

    if usdm is not None and {"funding", "open_interest"} & set(usdm.datatypes):
        q = "real" if binance_primary else "proxy"
        why = "USDⓈ-M perpetual of the same asset" if binance_primary else f"{usdm.key} perpetual (different instrument)"
        out["derivatives"] = Capability(q, usdm.key, why)
    else:
        out["derivatives"] = Capability("unavailable", None, "no derivatives instrument configured")

    nbs = neighbours(settings, reg, pair)          # B6 / B17: configured here; the snapshot downgrades it when the data is not there
    if nbs:
        out["cross_asset"] = Capability("real", nbs[0].inst.key, "15m returns vs " + ", ".join(n.inst.key for n in nbs))
    else:
        out["cross_asset"] = Capability("unavailable", None, "no correlated pair or context instrument configured")
    nb = settings.pairs[pair].news_blackout          # B8: only a pair with the blackout on lists it (the snapshot
    if nb.enabled:                                   # downgrades it while the stored calendar is not fresh)
        out["news_calendar"] = Capability("real", None, f"{', '.join(nb.currencies)} releases from the weekly calendar "
                                                        "feed (fetched hourly)")
    return out


def matrix_markdown(settings: Settings, reg: InstrumentRegistry) -> str:
    pairs = reg.pairs()
    mats = {p: capability_matrix(settings, reg, p) for p in pairs}
    lines = ["# Capability matrix (generated — `python -m tradingsystem engine --capabilities`)", "",
             "| analysis | " + " | ".join(pairs) + " |", "|---|" + "---|" * len(pairs)]
    for a in ANALYSES:
        cells = [f"**{mats[p][a].quality}** — {mats[p][a].reason}" if a in mats[p] else "— (not configured)"
                 for p in pairs]
        lines.append(f"| {a} | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def instrument_of(reg: InstrumentRegistry, key: str | None) -> Instrument | None:
    return reg.get(key) if key else None
