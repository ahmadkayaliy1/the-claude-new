"""Table specifications generated from configuration (P2.4).

One hot database file per instrument (``data/hot/{venue}/{SYMBOL}.db``, single writer). Table names follow
the spec (``btcusdt_candles_1m``, ``xauusd_ticks`` …). Adding an instrument or data type in config creates
its tables — no code change (spec §9 scalability).

Column conventions: all times are int64 UTC milliseconds; MT5 rows also keep the raw server time
(``srv_*``) so they can be re-converted if the server-time model is ever revised (D-014).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..core.instruments import Instrument
from ..core.timeframes import Timeframe


@dataclass(frozen=True)
class Column:
    name: str
    sql_type: str          # INTEGER | REAL | TEXT
    nullable: bool = False


@dataclass(frozen=True)
class TableSpec:
    name: str
    datatype: str
    key: tuple[str, ...]              # primary key columns (idempotent upsert target)
    time_col: str                     # column used for range reads / last timestamp
    columns: tuple[Column, ...]
    timeframe: Timeframe | None = None
    indexes: tuple[tuple[str, ...], ...] = field(default_factory=tuple)
    # a later write of an existing key fills that row's NULL columns (never overwrites a value) — for sources whose
    # columns arrive at different times (the 5-min metrics: the taker ratio is published one bucket later)
    fill_nulls: bool = False

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.columns)

    @property
    def integer_key(self) -> bool:
        return len(self.key) == 1 and self._col(self.key[0]).sql_type == "INTEGER"

    def _col(self, name: str) -> Column:
        return next(c for c in self.columns if c.name == name)

    def ddl(self) -> list[str]:
        cols = []
        for c in self.columns:
            decl = f"{c.name} {c.sql_type}"
            if self.integer_key and c.name == self.key[0]:
                decl += " PRIMARY KEY"          # rowid alias: fastest single-integer key in SQLite
            elif not c.nullable:
                decl += " NOT NULL"
            cols.append(decl)
        if not self.integer_key:
            cols.append(f"PRIMARY KEY ({', '.join(self.key)})")
        suffix = "" if self.integer_key else " WITHOUT ROWID"
        stmts = [f"CREATE TABLE IF NOT EXISTS {self.name} ({', '.join(cols)}){suffix}"]
        for idx in self.indexes:
            stmts.append(f"CREATE INDEX IF NOT EXISTS {self.name}_{'_'.join(idx)} ON {self.name}({', '.join(idx)})")
        return stmts


def _c(name: str, t: str = "REAL", nullable: bool = False) -> Column:
    return Column(name, t, nullable)


_I, _R, _T = "INTEGER", "REAL", "TEXT"

_BINANCE_CANDLE = (
    _c("open_time", _I), _c("open"), _c("high"), _c("low"), _c("close"), _c("volume"), _c("quote_volume"),
    _c("trades", _I), _c("taker_buy_base"), _c("taker_buy_quote"),
)
_MT5_CANDLE = (
    _c("open_time", _I), _c("srv_time", _I), _c("open"), _c("high"), _c("low"), _c("close"),
    _c("tick_volume", _I), _c("spread", _I), _c("real_volume", _R, nullable=True),
)


def _candles(inst: Instrument, tf: Timeframe) -> TableSpec:
    cols = _MT5_CANDLE if inst.venue == "mt5" else _BINANCE_CANDLE
    return TableSpec(inst.table("candles", tf), "candles", ("open_time",), "open_time", cols, timeframe=tf)


def _specs_for(inst: Instrument, datatype: str) -> list[TableSpec]:
    t = inst.table
    if datatype == "candles":
        return [_candles(inst, tf) for tf in inst.timeframes]
    if datatype == "agg_trades":
        return [TableSpec(t("agg_trades"), datatype, ("agg_id",), "ts", (
            _c("agg_id", _I), _c("ts", _I), _c("price"), _c("qty"), _c("first_id", _I), _c("last_id", _I),
            _c("is_buyer_maker", _I)), indexes=(("ts",),))]
    if datatype == "ticks":
        return [TableSpec(t("ticks"), datatype, ("key",), "time_msc", (
            _c("key", _I), _c("time_msc", _I), _c("srv_msc", _I), _c("bid"), _c("ask"),
            _c("last", _R, True), _c("volume_real", _R, True), _c("flags", _I)), indexes=(("time_msc",),))]
    if datatype == "book_ticker":
        # stored on best-price change only (quantity-only updates are conflated)
        return [TableSpec(t("book_ticker"), datatype, ("key",), "ts", (
            _c("key", _I), _c("ts", _I), _c("update_id", _I), _c("bid"), _c("bid_qty"), _c("ask"), _c("ask_qty")),
            indexes=(("ts",),))]
    if datatype == "depth":
        # cumulative liquidity within ±percentage bands (negative = bid side), Vision `bookDepth` layout
        return [TableSpec(t("depth"), datatype, ("ts", "percentage"), "ts", (
            _c("ts", _I), _c("percentage"), _c("depth"), _c("notional")))]
    if datatype == "funding":
        return [TableSpec(t("funding"), datatype, ("funding_time",), "funding_time", (
            _c("funding_time", _I), _c("funding_rate"), _c("mark_price", _R, True)))]
    if datatype == "open_interest":
        return [TableSpec(t("open_interest"), datatype, ("ts",), "ts", (
            _c("ts", _I), _c("open_interest"), _c("open_interest_value", _R, True)))]
    if datatype == "metrics":
        # 5-min Binance futures metrics from five REST endpoints (live) or the Vision day file: the live poll stores a
        # row as soon as one endpoint has it; the taker ratio arrives a bucket later → fill_nulls completes the row
        return [TableSpec(t("metrics"), datatype, ("ts",), "ts", (
            _c("ts", _I), _c("sum_open_interest", _R, True), _c("sum_open_interest_value", _R, True),
            _c("count_toptrader_long_short_ratio", _R, True), _c("sum_toptrader_long_short_ratio", _R, True),
            _c("count_long_short_ratio", _R, True), _c("sum_taker_long_short_vol_ratio", _R, True)),
            fill_nulls=True)]
    if datatype == "mark_price":
        # sampled once per minute from markPrice@1s (last value of the minute)
        return [TableSpec(t("mark_price"), datatype, ("ts",), "ts", (
            _c("ts", _I), _c("mark_price"), _c("index_price", _R, True), _c("est_settle_price", _R, True),
            _c("funding_rate", _R, True), _c("next_funding_time", _I, True)))]
    if datatype == "liquidations":
        return [TableSpec(t("liquidations"), datatype, ("key",), "ts", (
            _c("key", _I), _c("ts", _I), _c("side", _T), _c("price"), _c("avg_price"), _c("qty"),
            _c("filled_qty"), _c("status", _T)), indexes=(("ts",),))]
    raise ValueError(f"no table spec for datatype {datatype!r}")


# --------------------------------------------------------------------------- system tables (per hot DB)
VISION_DONE = TableSpec(
    name="vision_done", datatype="system", key=("table_name", "period"), time_col="done_ms",
    columns=(_c("table_name", _T), _c("period", _T), _c("rows", _I), _c("done_ms", _I)),
)
FORMING = TableSpec(
    # latest state of the still-open candle per timeframe (replaced in place; closed candles go to candles_*)
    name="forming_candles", datatype="system", key=("tf",), time_col="updated_ms",
    columns=(_c("tf", _T), _c("open_time", _I), _c("open"), _c("high"), _c("low"), _c("close"),
             _c("volume", _R, True), _c("updated_ms", _I)),
)


def system_specs() -> list[TableSpec]:
    from .gaps import KNOWN_GAPS   # local import: gaps imports this module
    return [VISION_DONE, FORMING, KNOWN_GAPS]


def table_specs(inst: Instrument) -> list[TableSpec]:
    """All hot-store tables for one instrument (from its configured datatypes/timeframes)."""
    out: list[TableSpec] = []
    for dt_name in inst.datatypes:
        out.extend(_specs_for(inst, dt_name))
    names = [s.name for s in out]
    if len(names) != len(set(names)):
        raise ValueError(f"duplicate table names for {inst.key}: {names}")
    return out


def spec_for(inst: Instrument, datatype: str, timeframe: Timeframe | None = None) -> TableSpec:
    for s in table_specs(inst):
        if s.datatype == datatype and s.timeframe == timeframe:
            return s
    raise KeyError(f"{inst.key} has no table for {datatype} {timeframe}")
