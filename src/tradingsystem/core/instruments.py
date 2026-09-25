"""Instrument registry: logical pairs -> venue instruments with roles (D-008).

A *pair* (e.g. ``XAUUSD``) is what the AI and the user reason about. An *instrument* is a
concrete tradable/observable symbol on one venue (e.g. ``mt5:XAUUSD@`` or
``binance_usdm:XAUUSDT``). Storage is organised per instrument (one hot DB file each, one
writer each); tables keep the spec's names (``xauusd_candles_1m`` ...).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .settings import InstrumentCfg, Settings
from .timeframes import Timeframe
from .timeutil import parse_date_spec

_SAFE_RE = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class Instrument:
    pair: str
    venue: str
    symbol: str                      # venue-native symbol, e.g. "XAUUSD@"
    roles: tuple[str, ...]
    datatypes: tuple[str, ...]
    timeframes: tuple[Timeframe, ...]
    start_specs: dict[str, str] = field(default_factory=dict, hash=False, compare=False)

    @property
    def key(self) -> str:
        return f"{self.venue}:{self.symbol}"

    @property
    def table_prefix(self) -> str:
        """``BTCUSD@`` -> ``btcusd``; ``BTCUSDT`` -> ``btcusdt``."""
        return _SAFE_RE.sub("", self.symbol.lower())

    @property
    def file_stem(self) -> str:
        return _SAFE_RE.sub("_", self.symbol.lower()).strip("_").upper()

    def table(self, datatype: str, timeframe: Timeframe | None = None) -> str:
        name = f"{self.table_prefix}_{datatype}"
        return f"{name}_{timeframe.value}" if timeframe is not None else name

    def has_role(self, role: str) -> bool:
        return role in self.roles

    def start_ms(self, datatype: str, *, now: int | None = None) -> int | None:
        spec = self.start_specs.get(datatype)
        return None if spec is None else parse_date_spec(spec, now=now)

    def hot_db_path(self, data_dir: Path) -> Path:
        return data_dir / "hot" / self.venue / f"{self.file_stem}.db"


class InstrumentRegistry:
    def __init__(self, instruments: list[Instrument]) -> None:
        self._all = instruments
        self._by_key: dict[str, Instrument] = {}
        for inst in instruments:
            existing = self._by_key.get(inst.key)
            if existing is not None and existing.pair != inst.pair:
                # The same venue symbol shared by two pairs would mean two writers for one file.
                raise ValueError(f"instrument {inst.key} is configured for two pairs: {existing.pair}, {inst.pair}")
            self._by_key[inst.key] = inst

    @classmethod
    def from_settings(cls, settings: Settings, *, mt5_profile: str | None = None) -> "InstrumentRegistry":
        profile = mt5_profile or settings.mt5.data_profile
        out: list[Instrument] = []
        for pair_name, pair in settings.enabled_pairs().items():
            pair_tfs = tuple(pair.timeframes or settings.timeframes)
            for icfg in pair.instruments:
                out.append(
                    Instrument(
                        pair=pair_name,
                        venue=icfg.venue,
                        symbol=_resolve_symbol(icfg, profile),
                        roles=tuple(icfg.roles),
                        datatypes=tuple(icfg.datatypes),
                        timeframes=tuple(icfg.timeframes or pair_tfs),
                        start_specs=dict(icfg.start),
                    )
                )
        return cls(out)

    def all(self) -> list[Instrument]:
        return list(self._all)

    def get(self, key: str) -> Instrument:
        return self._by_key[key]

    def for_venue(self, venue: str) -> list[Instrument]:
        return [i for i in self._all if i.venue == venue]

    def for_pair(self, pair: str) -> list[Instrument]:
        return [i for i in self._all if i.pair == pair]

    def with_role(self, pair: str, role: str) -> list[Instrument]:
        return [i for i in self.for_pair(pair) if i.has_role(role)]

    def primary(self, pair: str) -> Instrument:
        return self.with_role(pair, "analysis_primary")[0]

    def pairs(self) -> list[str]:
        return sorted({i.pair for i in self._all})


def _resolve_symbol(icfg: InstrumentCfg, mt5_profile: str) -> str:
    if icfg.symbol_by_profile:
        try:
            return icfg.symbol_by_profile[mt5_profile]
        except KeyError as exc:
            raise ValueError(f"no symbol configured for MT5 profile {mt5_profile!r}: {icfg.symbol_by_profile}") from exc
    assert icfg.symbol is not None
    return icfg.symbol
