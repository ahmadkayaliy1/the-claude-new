"""Typed settings: ``config/config.yaml`` (+ optional ``config/config.local.yaml``) + ``.env``.

* Non-secret settings live in YAML (committed). Secrets live only in environment variables /
  ``.env`` (git-ignored) and are referenced from YAML by *name* (``*_env`` fields).
* A few switches can be flipped with one line in ``.env`` (spec §4.1): ``ACTIVE_AI_PROVIDER``,
  ``AGENT_MODE``, ``TRIGGER_POLICY``, ``EXECUTION_MODE``, ``EXECUTION_TRIGGER``, ``RESOURCE_PROFILE``,
  and ``<PROVIDER>_MODEL`` through each provider's ``model_env``.
"""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

import yaml
from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .timeframes import Timeframe

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "config.yaml"
LOCAL_CONFIG = PROJECT_ROOT / "config" / "config.local.yaml"
DEFAULT_ENV = PROJECT_ROOT / ".env"

LIVE_CONFIRMATION_PHRASE = "I ACCEPT REAL-MONEY TRADING RISK"
TELEGRAM_SECRET_ENV = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")   # Phase 4 notifier (H18), read with secret()

Venue = Literal["binance_spot", "binance_usdm", "mt5"]
DataType = Literal[
    "candles", "agg_trades", "ticks", "book_ticker", "depth", "funding", "open_interest",
    "metrics", "liquidations", "mark_price",
]
# cross_context (Phase 5 B17): a candles-only instrument recorded as intermarket context (EURUSD@, XAGUSD@ for gold); no
# role lookup (primary, execution, quote, flow) ever returns it and no decision or gate reads it
Role = Literal["analysis_primary", "flow_context", "execution", "quote_reference", "cross_context"]
AgentMode = Literal[
    "single_agent_global", "agent_per_pair", "agent_per_timeframe", "agent_per_pair_and_timeframe",
    "agent_per_pair_with_risk_reviewer", "multi_provider_consensus",
]
TriggerPolicy = Literal["every_close", "on_setup_event", "hybrid"]
ExecutionMode = Literal["paper", "demo", "live"]
ExecutionTrigger = Literal["manual", "auto"]
ProviderKind = Literal["gemini", "anthropic", "openai", "openai_compat", "claude_code"]

_ENV_OVERRIDES: dict[str, tuple[str, ...]] = {
    "ACTIVE_AI_PROVIDER": ("ai", "active_provider"),
    "AI_FALLBACK_PROVIDER": ("ai", "fallback_provider"),
    "AGENT_MODE": ("ai", "agent_mode"),
    "TRIGGER_POLICY": ("ai", "trigger_policy"),
    "EXECUTION_MODE": ("execution", "mode"),
    "EXECUTION_TRIGGER": ("execution", "trigger"),
    "RESOURCE_PROFILE": ("profile",),
}
_SECRET_NAME_RE = re.compile(r"(KEY|SECRET|PASSWORD|TOKEN|PASSPHRASE)", re.I)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --------------------------------------------------------------------------- infrastructure
class PathsCfg(_Model):
    """``data()`` = market data (hot/cold stores, Vision cache) — shared by every instance, one file per instrument,
    so instances never write the same file. ``state()`` = the running system's own state (app.db, STOP_ALL,
    KILL_SWITCH, supervisor lock/state): ``data/instances/<PAIR>`` for an instance (D-042), else ``data``.
    ``shared()`` = cross-instance files (AI usage ledger, account high-water mark, locks, global KILL_SWITCH)."""
    data_dir: str = "data"
    logs_dir: str = "logs"
    instance: str | None = None            # set by the TS_INSTANCE overlay (one system per pair); None = all pairs

    def data(self) -> Path:
        return _resolve(self.data_dir)

    def logs(self) -> Path:
        base = _resolve(self.logs_dir)
        return base / self.instance if self.instance else base

    def state(self) -> Path:
        return self.data() / "instances" / self.instance if self.instance else self.data()

    def shared(self) -> Path:
        return self.data() / "shared"


def system_state_dirs(settings: "Settings") -> list[Path]:
    """The state dir of every system that may run on this data root: the all-pairs one (``data``) and one per
    configured instance (``data/instances/<PAIR>``) — whether or not it runs (callers check the heartbeats)."""
    base = settings.paths.data()
    return [base, *(base / "instances" / name for name in settings.instances)]


class StorageCfg(_Model):
    """Hot (SQLite) / cold (Parquet) split — D-020 (benchmark docs/benchmarks/storage.md)."""
    # days of raw high-volume data kept in the hot SQLite store before rollover to daily Parquet
    hot_days: dict[str, int] = Field(default_factory=lambda: {
        "agg_trades": 2, "ticks": 3, "book_ticker": 2, "liquidations": 30, "depth": 30})
    # hours after UTC midnight before a closed day is rolled to Parquet (gap-fill must have run)
    rollover_grace_hours: int = 2
    # stop backfills (not live capture) when free disk falls below this
    min_free_disk_gb: float = 10.0
    # live writers skip the daily cold-archive rollover below this (the hot store keeps the rows; a later day rolls
    # them once there is room) — Phase 5 A7
    cold_archive_min_free_gb: float = Field(2.0, ge=0)


class LoggingCfg(_Model):
    level: str = "INFO"
    max_bytes: int = 20 * 1024 * 1024
    backups: int = 10
    console: bool = True


class ResourceProfileCfg(_Model):
    # Phase 5 A7: duckdb_memory_mb, duckdb_threads, engine_cycle_in_subprocess and vision_download_concurrency were
    # removed — nothing read them (the profiles only size the SQLite cache and the Vision CSV parse blocks)
    sqlite_cache_mb: int = 16
    parse_chunk_rows: int = 500_000


class MT5ProfileCfg(_Model):
    terminal_path: str
    server: str
    account_type: Literal["demo", "real"]
    login_env: str | None = None
    password_env: str | None = None


class MT5Cfg(_Model):
    profiles: dict[str, MT5ProfileCfg]
    data_profile: str = "demo"
    execution_profile_by_mode: dict[str, str] = Field(default_factory=lambda: {"demo": "demo", "live": "live"})
    poll_interval_ms: int = 100
    call_timeout_s: float = 10.0

    @model_validator(mode="after")
    def _check_profiles(self) -> "MT5Cfg":
        if self.data_profile not in self.profiles:
            raise ValueError(f"mt5.data_profile {self.data_profile!r} not in mt5.profiles")
        for mode, prof in self.execution_profile_by_mode.items():
            if prof not in self.profiles:
                raise ValueError(f"mt5.execution_profile_by_mode[{mode}]={prof!r} not in mt5.profiles")
        return self


class BinanceCfg(_Model):
    spot_rest: str = "https://api.binance.com"
    spot_ws: str = "wss://stream.binance.com:9443"
    usdm_rest: str = "https://fapi.binance.com"
    usdm_ws: str = "wss://fstream.binance.com"
    vision: str = "https://data.binance.vision"
    api_key_env: str | None = "BINANCE_API_KEY"
    api_secret_env: str | None = "BINANCE_API_SECRET"
    rest_weight_budget_per_min: int = 3000


# --------------------------------------------------------------------------- pairs/instruments
_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _check_tz(name: str, where: str) -> str:
    try:
        ZoneInfo(name)
    except Exception as exc:  # noqa: BLE001 — ZoneInfoNotFoundError, ValueError (bad key), OSError
        raise ValueError(f"{where}: unknown time zone {name!r}") from exc
    return name


def _check_hhmm(v: str, where: str) -> str:
    if not isinstance(v, str) or not _HHMM_RE.match(v):
        raise ValueError(f"{where}: {v!r} is not a 24-h HH:MM time")
    return v


class DeskWindowCfg(_Model):
    """One call window of a desk (B15 reads it): local wall-clock ``start``..``end`` in ``tz`` (DST follows the zone)."""
    name: str = Field(min_length=1)
    tz: str
    start: str
    end: str

    @model_validator(mode="after")
    def _check(self) -> "DeskWindowCfg":
        _check_tz(self.tz, f"desk window {self.name!r}")
        _check_hhmm(self.start, f"desk window {self.name!r} start")
        _check_hhmm(self.end, f"desk window {self.name!r} end")
        if self.start >= self.end:
            raise ValueError(f"desk window {self.name!r}: start {self.start} must be before end {self.end} "
                             "(no window crosses midnight)")
        return self


class DeskClockCfg(_Model):
    """A wall-clock time in a zone (the Friday cutoff)."""
    time: str
    tz: str

    @model_validator(mode="after")
    def _check(self) -> "DeskClockCfg":
        _check_hhmm(self.time, "desk friday_cutoff time")
        _check_tz(self.tz, "desk friday_cutoff")
        return self


def _default_desk_windows() -> list[DeskWindowCfg]:
    return [DeskWindowCfg(name="london", tz="Europe/London", start="07:45", end="11:00"),
            DeskWindowCfg(name="new_york", tz="America/New_York", start="08:15", end="11:30")]


class DeskCfg(_Model):
    """A desk (D-049): a pair that is analysed and scored but never traded. ``mode: shadow`` is the only mode — the
    executor gates every valid BUY/SELL of the pair in full for the record and stores it ``not_executed``/``shadow``
    (scored in R by the virtual outcomes); it never places, modifies or cancels an order for the pair. A real
    ("live") mode is a future owner decision (H32) and does not exist. Code default of ``PairCfg.desk`` is None =
    the pair trades as before."""
    mode: Literal["shadow"] = "shadow"
    brief: str = Field("", pattern=r"^(desks/[a-z0-9_]+)?$")   # prompt appendix under ai/prompts: "desks/xau" (B18)
    windows: list[DeskWindowCfg] = Field(default_factory=_default_desk_windows)          # entry-call windows (B15)
    reopen_grace_min: int = Field(60, ge=0, le=600)     # no entry call this long after the Sunday reopen (B15)
    friday_cutoff: DeskClockCfg = DeskClockCfg(time="12:00", tz="America/New_York")     # none after it on Friday (B15)

    @model_validator(mode="after")
    def _check(self) -> "DeskCfg":
        names = [w.name for w in self.windows]
        if len(set(names)) != len(names):
            raise ValueError("desk.windows: duplicate window names")
        return self


NewsTier = Literal["fomc", "high", "medium"]


class NewsBlackoutCfg(_Model):
    """News blackout from a public weekly economic-calendar feed (Phase 5 B8, D-046 a; the gold tiers of D-049).
    ``windows`` = minutes [before, after] a release per tier; a tier left out is never blacked out (``analysis/news.py``
    classifies: ``fomc`` = the FOMC statement / rate decision / press conference, ``high`` = every other high-impact
    release of ``currencies``, ``medium`` = PPI, retail sales, ISM, JOLTS and GDP when the feed rates them medium).
    While the fetched file is fresh (``stale_hours``) the gate refuses an entry inside a window (check
    ``news_blackout``) and entry calls wait; a stale file = the capability unavailable + one warning event and no
    blackout — except for a desk pair, whose entry calls then stop altogether (D-049: no money is at stake in shadow).
    Code default: off."""
    enabled: bool = False
    source_url: str = Field("https://nfs.faireconomy.media/ff_calendar_thisweek.json", pattern=r"^https://")
    refresh_minutes: int = Field(60, ge=15, le=1440)       # the feed allows 2 downloads per 5 min; once an hour is plenty
    stale_hours: float = Field(24.0, gt=0, le=72)
    currencies: list[str] = Field(default_factory=lambda: ["USD"], min_length=1)
    windows: dict[NewsTier, tuple[int, int]] = Field(default_factory=lambda: {"fomc": (15, 15), "high": (15, 15)})

    @model_validator(mode="after")
    def _check(self) -> "NewsBlackoutCfg":
        for tier, (before, after) in self.windows.items():
            if not (0 <= before <= 240 and 0 <= after <= 240):
                raise ValueError(f"news_blackout.windows.{tier}: minutes must be within 0..240")
        return self


class ContractCfg(_Model):
    """Execution contract facts (measured from MT5 symbol_info, P1.5; re-validated by the executor at runtime).
    The cost fields feed ``market.execution.costs`` in the snapshot so the model plans with the venue's real limits."""
    contract_size: float
    volume_min: float
    volume_step: float
    tick_size: float
    stops_level_points: int | None = None            # SYMBOL_TRADE_STOPS_LEVEL (× tick_size = min SL/TP distance)
    swap_long: float | None = None                   # per night, unit given by swap_mode
    swap_short: float | None = None
    swap_mode: Literal["points", "annual_pct"] | None = None
    triple_swap_weekday: Literal["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"] | None = None
    commission_per_lot: float = 0.0                  # USD per lot per side (measured 0 on the demo, P9.5)


class InstrumentCfg(_Model):
    venue: Venue
    symbol: str | None = None
    symbol_by_profile: dict[str, str] | None = None
    roles: list[Role]
    datatypes: list[DataType]
    timeframes: list[Timeframe] | None = None
    start: dict[str, str] = Field(default_factory=dict)
    contract: ContractCfg | None = None

    @model_validator(mode="after")
    def _check_symbol(self) -> "InstrumentCfg":
        if not self.symbol and not self.symbol_by_profile:
            raise ValueError("instrument needs `symbol` or `symbol_by_profile`")
        if self.symbol_by_profile and self.venue != "mt5":
            raise ValueError("`symbol_by_profile` is only valid for venue mt5")
        unknown = set(self.start) - set(self.datatypes)
        if unknown:
            raise ValueError(f"start dates for datatypes not collected: {sorted(unknown)}")
        if "cross_context" in self.roles and (self.roles != ["cross_context"] or self.venue != "mt5"
                                              or self.datatypes != ["candles"]):
            raise ValueError("a cross_context instrument is MT5, has no other role and records `candles` only")
        return self


class PairCfg(_Model):
    enabled: bool = True
    asset_class: Literal["crypto", "metal", "fx", "index", "commodity"]
    decision_timeframe: Timeframe = Timeframe.M15
    timeframes: list[Timeframe] | None = None
    pip_size: float
    price_decimals: int = 2
    footprint_bucket: float = 1.0          # price bucket for footprint / volume profile
    flow_proxy_approved: bool = False      # use a flow_context instrument's order flow as proxy (P1.11)
    desk: DeskCfg | None = None            # Phase 5 B14 (D-049): shadow desk — metal pairs only; None = trades as before
    news_blackout: NewsBlackoutCfg = NewsBlackoutCfg()   # Phase 5 B8: off unless the pair's config turns it on
    instruments: list[InstrumentCfg]

    @model_validator(mode="after")
    def _check_roles(self) -> "PairCfg":
        if self.desk is not None and self.asset_class != "metal":
            raise ValueError(f"desk is only valid for metal pairs (asset_class is {self.asset_class!r})")
        primaries = [i for i in self.instruments if "analysis_primary" in i.roles]
        if len(primaries) != 1:
            raise ValueError("each pair needs exactly one `analysis_primary` instrument")
        if not any("execution" in i.roles for i in self.instruments):
            raise ValueError("each pair needs at least one `execution` instrument")
        return self


# --------------------------------------------------------------------------- risk/execution
class RiskCfg(_Model):
    risk_per_trade_pct: float = 0.5
    max_risk_per_trade_pct: float = 1.0
    max_daily_loss_pct: float = 2.0
    min_rr: float = 1.5
    max_open_positions: int = 3
    max_effective_leverage: float = 10.0
    sl_atr_min_mult: float = 0.5
    sl_atr_max_mult: float = 5.0
    max_spread_to_sl_ratio: float = 0.2
    min_confidence: int = Field(55, ge=50, le=90)     # the gate executes nothing below this confidence
    account_drawdown_stop_pct: float = Field(25.0, gt=0, le=100)   # whole account below its high-water mark (D-039)
    max_recommendation_age_s: int = 300
    max_data_staleness_s: int = 120
    correlated_groups: list[list[str]] = Field(default_factory=lambda: [["BTCUSDT", "ETHUSDT"]])
    max_correlated_risk_pct: float = 1.0
    allow_high_risk_display: bool = True

    @model_validator(mode="after")
    def _check(self) -> "RiskCfg":
        if not 0 < self.risk_per_trade_pct <= self.max_risk_per_trade_pct:
            raise ValueError("risk_per_trade_pct must be in (0, max_risk_per_trade_pct]")
        if self.min_rr <= 0:
            raise ValueError("min_rr must be > 0")
        return self


class ManagementCfg(_Model):
    """P9.6 — the executor applies the ``management`` rules the model declared with a trade (D-043)."""
    enabled: bool = True
    dry_run: bool = False                                   # record what would be done, touch nothing
    breakeven_buffer_spread_mult: float = Field(1.0, ge=0, le=5)   # breakeven = fill ± this × spread (≥ stops+spread)
    min_sl_change_ticks: int = Field(5, ge=1, le=10_000)    # smaller trailing moves are ignored


class PositionActionsCfg(_Model):
    """Bounded actions the model may take on its live trades on later cycles (D-043: tighten/close/cancel only)."""
    enabled: bool = True
    max_per_decision: int = Field(4, ge=1, le=4)
    max_per_pair_per_day: int = Field(12, ge=0, le=100)     # applied actions per pair per UTC day
    min_minutes_between_sl_changes: int = Field(15, ge=1, le=1440)   # per position


class ExecutionCfg(_Model):
    paper_equity: float = 100.0            # starting equity of the paper account (user's intended capital, D-021)
    mode: ExecutionMode = "paper"
    trigger: ExecutionTrigger = "manual"
    venue_by_pair: dict[str, Venue] = Field(default_factory=dict)
    max_basis_deviation_pct: float = 0.15
    magic: int = 26092501
    live_confirmation: str = ""
    management: ManagementCfg = ManagementCfg()
    position_actions: PositionActionsCfg = PositionActionsCfg()


# --------------------------------------------------------------------------- AI
class AIProviderCfg(_Model):
    kind: ProviderKind
    model: str
    model_env: str | None = None
    fallback_model: str | None = None
    api_key_env: str | None = None
    base_url: str | None = None
    rpm: int | None = None
    rpd: int | None = None
    tpm: int | None = None
    usd_per_mtok_in: float = 0.0
    usd_per_mtok_out: float = 0.0
    model_prices: dict[str, tuple[float, float]] = Field(default_factory=dict)   # model -> (in, out) USD/MTok
    structured_output: Literal["native", "json_object", "prompt"] = "native"
    effort: str | None = None
    timeout_s: float = 120.0
    max_output_tokens: int = 4096
    free_tier: bool = False
    cli_path: str | None = None            # claude_code: the Claude Code executable (default: found on PATH)
    max_concurrency: int = 1               # claude_code: CLI processes at once (~170 MB each)
    temperature: float | None = None      # sampling temperature; None = the model default (Gemini 3.x wants that)
    quota_reset_tz: str | None = None      # daily-quota reset time zone (Gemini: America/Los_Angeles); None = UTC


class AIBudgetCfg(_Model):
    daily_usd_cap: float = 0.0
    monthly_usd_cap: float = 0.0
    max_ai_cost_to_profit_ratio: float = 0.2
    rolling_window_days: int = 30
    min_trades_for_ratio: int = 20


ChartOverlay = Literal["levels", "zones", "liquidity", "structure", "holdings", "ema"]


class ChartsCfg(_Model):
    """Candle-chart images sent with the payload (D-038, Phase 3). ``enabled: false`` = text-only calls."""
    enabled: bool = True
    width: int = Field(720, ge=320, le=1600)
    height: int = Field(400, ge=200, le=1200)
    timeframes: list[str] = Field(default_factory=lambda: ["1w", "1d", "4h", "1h", "15m", "5m"])
    bars: dict[str, int] = Field(default_factory=lambda: {"1w": 60, "1d": 120, "4h": 120, "1h": 120, "15m": 96,
                                                          "5m": 96})
    overlays: list[ChartOverlay] = Field(default_factory=lambda: ["levels", "zones", "liquidity", "structure",
                                                                  "holdings", "ema"])

    @model_validator(mode="after")
    def _check(self) -> "ChartsCfg":
        for tf in self.timeframes:
            Timeframe.parse(tf)
            if not 20 <= self.bars.get(tf, 0) <= 300:
                raise ValueError(f"ai.charts.bars[{tf}] must be 20..300")
        return self


class RoleModelCfg(_Model):
    """The model of one role. ``model``: a Claude Code alias (sonnet | opus | fable) or a full model name; None =
    the provider's configured model. Applies to the claude_code provider; other providers (the fallback) keep their
    own model. ``effort``: low | medium | high | xhigh | max; None = the provider's configured effort."""
    model: str | None = None
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None


class AIModelsCfg(_Model):
    decision: RoleModelCfg = RoleModelCfg()
    escalation: RoleModelCfg = RoleModelCfg(model="opus", effort="high")
    review: RoleModelCfg = RoleModelCfg(model="opus", effort="high")        # Phase 4 review sessions
    monitor: RoleModelCfg = RoleModelCfg(model="sonnet", effort="low")      # Phase 4 diagnosis


class AIEscalationCfg(_Model):
    """A stronger model confirms (or downgrades) a strong setup before it can be executed (D-043)."""
    enabled: bool = False
    on_strength: list[Literal["strong", "weak", "review", "event", "idle", "close"]] = Field(
        default_factory=lambda: ["strong"])
    min_confidence: int = Field(60, ge=50, le=95)
    max_per_day_per_pair: int = Field(6, ge=0, le=40)
    on_failure: Literal["withhold", "keep"] = "withhold"
    timeout_s: float = Field(150.0, ge=30, le=600)


class AIUsageCfg(_Model):
    """Ledger-based usage gauge (Phase 4): rolling 7-day / 5-hour token sums of every Claude call (decisions,
    escalations, operator sessions) against budgets. The budgets are calibration guesses until a week of data exists
    (H21); until ``enforce`` is set the gauge is only published (status, health report, review pack) and never
    rations calls — the CLI's own usage limit stays the hard stop."""
    weekly_token_budget: int = Field(12_000_000, ge=100_000)
    five_hour_token_budget: int = Field(1_500_000, ge=10_000)
    cache_read_weight: float = Field(0.1, ge=0.0, le=1.0)   # a cached input token counts this much
    level1_pct: float = Field(70.0, gt=0, le=100)           # ≥ → reviews and events only
    level2_pct: float = Field(90.0, gt=0, le=100)           # ≥ → events only
    enforce: bool = False
    cache_s: float = Field(60.0, ge=5, le=600)              # the sums are re-read at most this often

    @model_validator(mode="after")
    def _levels(self) -> "AIUsageCfg":
        if self.level1_pct >= self.level2_pct:
            raise ValueError("ai.usage.level1_pct must be below level2_pct")
        return self


class AICfg(_Model):
    active_provider: str
    fallback_provider: str | None = None    # used while the active provider is unavailable (D-030)
    agent_mode: AgentMode = "agent_per_pair"
    trigger_policy: TriggerPolicy = "hybrid"
    output_language: str = "en"
    min_minutes_between_calls: int = 15     # per pair (protects free-tier quotas)
    max_idle_minutes: int = 120             # hybrid policy: review a pair at least this often (market open)
    max_parallel_calls: int = 2
    # one system per pair (D-042/D-043): the provider's rpd counts every instance through one shared ledger, and one
    # pair may make at most this many calls a day (every attempt counts: first calls, repairs, escalations)
    daily_calls_per_pair: int = Field(40, ge=10, le=120)
    event_calls_per_day: int = Field(6, ge=0, le=20)     # calls woken by fills / closes / outcomes, per pair and day
    screen_timeframe: str = "5m"            # Python screens every close of this TF; Claude is called only on change
    screen_move_atr: float = Field(0.5, ge=0.2, le=1.0)  # a time-based review needs a move > this × ATR (or a new setup)
    weak_min: int = Field(2, ge=1, le=4)    # weak setup reasons needed to call (Phase 4 overlay may raise it)
    weak_needs_location: bool = True        # weak calls only while price is at a 15m/1h zone or liquidity (5m = confirmation)
    liquidity_atr: float = Field(0.3, ge=0.1, le=0.6)    # "price near liquidity" distance in ATR
    review_floor_minutes: int = 5           # next_review price/candle triggers: not sooner after the last call
    max_backoff_minutes: int = 120          # per-pair back-off cap after failed cycles (spacing doubles per failure)
    cycle_deadline_s: float = 600.0         # an AI cycle is cut off after this; unfinished pairs stored as 'error'
    # Phase 5 A5 (D-046): no trader call while the pair's execution market is closed and nothing of the pair is open
    # or pending (the setup signature still advances, so the reopen does not fire on the closed session's structure)
    skip_closed_market: bool = True
    # Phase 5 B12: a pair WITHOUT a desk gets no entry call while ``account.min_position_risk.fits_now`` is false (the
    # minimum lot at the minimum stop breaks the risk or leverage cap: any answer would be rejected at the gate); the
    # setup signature advances, reviews / event calls of a pair holding something still run; absent fits_now = unknown
    # = never suppressed. A desk pair uses its windows (B15) instead. false = today's behaviour
    skip_entry_calls_when_no_fit: bool = True
    transient_retry_s: float = Field(60.0, ge=0, le=600)   # OAuth refresh race / 403: one retry after this; 0 = off
    # share of daily_calls_per_pair kept for the busy UTC hours [start, end): BEFORE the window starts in the UTC
    # quota day a pair may use at most (1 − share) × cap, so the afternoon sessions keep calls; from `start` on every
    # call left is free (after the window the kept calls could only expire). Event calls and the reviews of a pair
    # holding a position or pending order (or whose holdings are unknown) are exempt; a provider whose quota day is
    # not the UTC day gets no reserve; 0 = no reserve
    quota_reserve_share: float = Field(0.4, ge=0, le=0.9)
    quota_reserve_hours_utc: list[int] = Field(default_factory=lambda: [12, 21])
    consensus_providers: list[str] = Field(default_factory=list)
    providers: dict[str, AIProviderCfg]
    budget: AIBudgetCfg = AIBudgetCfg()
    charts: ChartsCfg = ChartsCfg()
    models: AIModelsCfg = AIModelsCfg()
    escalation: AIEscalationCfg = AIEscalationCfg()
    usage: AIUsageCfg = AIUsageCfg()

    @model_validator(mode="after")
    def _check_provider(self) -> "AICfg":
        if self.active_provider not in self.providers:
            raise ValueError(
                f"ai.active_provider {self.active_provider!r} not configured; "
                f"available: {sorted(self.providers)}"
            )
        missing = [p for p in self.consensus_providers if p not in self.providers]
        if missing:
            raise ValueError(f"ai.consensus_providers not configured: {missing}")
        if self.fallback_provider is not None and self.fallback_provider not in self.providers:
            raise ValueError(f"ai.fallback_provider {self.fallback_provider!r} not configured")
        Timeframe.parse(self.screen_timeframe)
        h = self.quota_reserve_hours_utc
        if len(h) != 2 or not 0 <= h[0] < h[1] <= 24:
            raise ValueError("ai.quota_reserve_hours_utc must be [start, end) with 0 <= start < end <= 24")
        return self

    @property
    def fallback(self) -> str | None:
        """The fallback provider, or None when it is unset or the same as the active one (e.g. after the
        one-line ``ACTIVE_AI_PROVIDER`` switch in ``.env`` — that must never stop the services from starting)."""
        return self.fallback_provider if self.fallback_provider != self.active_provider else None


class ApiCfg(_Model):
    host: str = "127.0.0.1"
    port: int = 8765
    token_env: str = "DASHBOARD_TOKEN"

    @field_validator("host")
    @classmethod
    def _local_only(cls, v: str) -> str:
        if v not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("api.host must be a loopback address (spec §9 security)")
        return v


class SupervisorCfg(_Model):
    """Supervisor ops knobs (P5.1/P5.3, docs/ops_windows.md)."""
    keep_awake: bool = True                  # block *idle* sleep while running (lid/buttons: Windows power plan)
    manage_mt5_terminal: bool = True         # start a missing MT5 terminal outside the supervisor's job/tree (OPS-04)
    mt5_task: str = "TradingSystem-MT5"      # scheduled task that starts the data terminal (scripts/install_autostart.ps1)
    child_log_max_bytes: int = 5 * 1024 * 1024   # logs/<service>.stderr.log rolled at (re)start above this
    child_log_backups: int = 2


class AdaptiveSettings(_Model):
    """The per-pair adaptive overlay (Phase 4, D-039): ``data/adaptive/<PAIR>/`` holds bounded, expiring values and a
    playbook written only by ``tools/tune.py``; the services re-read them without a restart."""
    enabled: bool = True                     # false = defaults everywhere and tune.py refuses every change
    reload_check_s: float = Field(5.0, ge=1, le=60)
    max_expiry_days: int = Field(14, ge=1, le=30)
    cooldown_days: int = Field(7, ge=0, le=30)          # per key
    max_changes_per_day: int = Field(1, ge=1, le=5)     # per pair and UTC day
    min_samples_strategy: int = Field(20, ge=1)         # resolved virtual outcomes in the window (floor, trigger, hints)
    min_samples_activity: int = Field(10, ge=1)         # … for keys that only reduce activity (spacing, idle, pause)
    unhealthy_freeze_pct: float = Field(25.0, ge=0, le=100)
    default_window_hours: int = Field(168, ge=24, le=720)


class NotifyCfg(_Model):
    """Notifications (D-043): always a log line; a Windows toast; Telegram when TELEGRAM_BOT_TOKEN and
    TELEGRAM_CHAT_ID are in .env (H18). Never raises into the caller."""
    enabled: bool = True
    toast: bool = True
    telegram: bool = True
    min_level: Literal["info", "warn", "critical"] = "info"
    toast_min_level: Literal["info", "warn", "critical"] = "info"
    rate_per_hour: int = Field(20, ge=1, le=500)         # per process, info and warn each (critical: no limit)
    dedupe_minutes: int = Field(30, ge=0, le=1440)       # same key → one message (shared by every system)
    timeout_s: float = Field(10.0, ge=1, le=60)


class MonitorCfg(_Model):
    """The pure-Python monitor (tools/monitor.py, every 15 min from Task Scheduler; docs/monitoring.md)."""
    enabled: bool = True
    stale_heartbeat_min: int = Field(15, ge=2)
    ipc_hung_min: int = Field(5, ge=1)
    order_burst_per_hour: int = Field(3, ge=1)          # more 'order' events per pair and hour → that pair's switch
    daily_loss_warn_margin_pct: float = Field(2.0, ge=0)    # warn at −(risk.max_daily_loss_pct − margin)
    equity_drop_warn_pct: float = Field(5.0, gt=0)      # between two runs
    equity_drop_kill_pct: float = Field(10.0, gt=0)     # … → the global switch
    quote_stale_min: int = Field(10, ge=1)
    free_ram_warn_mb: int = Field(300, ge=0)
    free_disk_warn_gb: float = Field(5.0, ge=0)
    restart_loop_per_hour: int = Field(3, ge=1)
    outage_warn_min: int = Field(10, ge=1)
    vpn_adapter_names: list[str] = Field(default_factory=list)
    review_overdue_hours: int = Field(30, ge=1)
    snapshot_build_warn_ms: int = Field(3000, ge=100)   # the engine's payload build (5-min screening cost)
    diagnose_enabled: bool = True                       # a warning or worse starts a Claude diagnosis session …
    diagnose_every_hours: float = Field(3.0, ge=0.5)    # … at most this often
    # Phase 5 A4: the machine (the outages of 2026-09-26/27 were critical-battery hibernates and shutdowns)
    battery_warn_pct: int = Field(30, ge=0, le=100)     # on battery and below this → warn …
    battery_critical_pct: int = Field(15, ge=0, le=100)  # … below this → critical
    on_battery_warn_min: int = Field(5, ge=1)           # running on battery longer than this → warn (charger out)
    commit_warn_pct: float = Field(85.0, gt=0, le=100)  # committed memory / commit limit (RAM + page file)
    recorder_stall_min: int = Field(30, ge=0)           # the P1.12 recorder's last flush older → warn; 0 = not watched
    diagnose_max_per_day: int = Field(2, ge=0)          # billed diagnosis sessions per UTC day (shared Max plan)
    diagnose_max_gauge_level: int = Field(0, ge=0, le=2)    # no diagnosis while the usage gauge is above this level

    @model_validator(mode="after")
    def _drops(self) -> "MonitorCfg":
        if self.equity_drop_warn_pct >= self.equity_drop_kill_pct:
            raise ValueError("monitor.equity_drop_warn_pct must be below equity_drop_kill_pct")
        if self.battery_critical_pct > self.battery_warn_pct:
            raise ValueError("monitor.battery_critical_pct must not be above battery_warn_pct")
        return self


class OperatorCfg(_Model):
    """Claude operator sessions from Task Scheduler (tools/operator; docs/operator_sessions.md)."""
    enabled: bool = True
    daily_max_turns: int = Field(30, ge=1, le=100)
    weekly_max_turns: int = Field(40, ge=1, le=100)
    diagnose_max_turns: int = Field(12, ge=1, le=100)
    daily_timeout_min: int = Field(20, ge=1, le=120)
    weekly_timeout_min: int = Field(40, ge=1, le=180)
    diagnose_timeout_min: int = Field(15, ge=1, le=120)
    daily_pack_hours: int = Field(24, ge=1, le=720)
    weekly_pack_hours: int = Field(168, ge=1, le=720)
    summary_max_chars: int = Field(1500, ge=100, le=4000)
    record_usage: bool = True            # sessions are written to the shared AI ledger (role review / diagnose)


class BackupCfg(_Model):
    """``tools/backup_state.py`` (Phase 5 A2): WAL-consistent copies of the state that exists only on this machine
    (the pairs' app.db, the shared ledger, the account peak, config.local.yaml, the adaptive overlay, reviews and the
    monitor/notify state) — never ``.env``. docs/ops_windows.md §9."""
    enabled: bool = True
    dir: str = "backups"             # relative to the project root (git-ignored) or absolute (e.g. another drive)
    keep: int = Field(14, ge=1, le=365)


class EvaluationCfg(_Model):
    """The demo evaluation window (D-046, D-047) read by tools/demo_report.py and tools/go_live_inputs.py."""
    demo_start_utc: str | None = None    # ISO-8601 UTC, e.g. "2026-09-27T21:50:00Z" (the Phase 4 restart, H20)
    demo_days: int = Field(5, ge=1, le=90)

    @model_validator(mode="after")
    def _start(self) -> "EvaluationCfg":
        if self.demo_start_utc is not None:
            v = self.demo_start_utc.strip()
            if not (v.endswith("Z") or v[-6:-5] in "+-"):
                raise ValueError("evaluation.demo_start_utc must carry a UTC offset (…Z)")
            dt.datetime.fromisoformat(v.replace("Z", "+00:00"))
        return self


# --------------------------------------------------------------------------- root
class InstanceCfg(_Model):
    """One fully independent system for one pair (D-042): ``run all --instance <PAIR>``."""
    api_port: int = Field(ge=1024, le=65535)
    magic_offset: int = Field(ge=1, le=999)   # MT5 magic = execution.magic + offset → per-instance risk limits
    # settings of this system only, merged over the rest (e.g. ``{ai: {charts: {enabled: false}}}`` to keep charts
    # for one pair on a tight-RAM machine); validated like the rest; never paths/pairs/instances
    overrides: dict[str, Any] = Field(default_factory=dict)


class Settings(_Model):
    profile: Literal["low", "standard"] = "standard"
    timeframes: list[Timeframe]
    paths: PathsCfg = PathsCfg()
    storage: StorageCfg = StorageCfg()
    logging: LoggingCfg = LoggingCfg()
    resources: dict[str, ResourceProfileCfg]
    mt5: MT5Cfg
    binance: BinanceCfg = BinanceCfg()
    pairs: dict[str, PairCfg]
    risk: RiskCfg = RiskCfg()
    execution: ExecutionCfg = ExecutionCfg()
    ai: AICfg
    api: ApiCfg = ApiCfg()
    supervisor: SupervisorCfg = SupervisorCfg()
    instances: dict[str, InstanceCfg] = Field(default_factory=dict)
    adaptive: AdaptiveSettings = AdaptiveSettings()
    notify: NotifyCfg = NotifyCfg()
    monitor: MonitorCfg = MonitorCfg()
    operator: OperatorCfg = OperatorCfg()
    backup: BackupCfg = BackupCfg()
    evaluation: EvaluationCfg = EvaluationCfg()

    # populated by the loader, not by YAML
    config_hash: str = ""

    @model_validator(mode="after")
    def _cross_checks(self) -> "Settings":
        if self.profile not in self.resources:
            raise ValueError(f"resources profile {self.profile!r} not defined")
        for name, pair in self.pairs.items():
            tfs = pair.timeframes or self.timeframes
            if pair.decision_timeframe not in tfs:
                raise ValueError(f"pairs.{name}.decision_timeframe not in its timeframes")
        for pair in self.execution.venue_by_pair:
            if pair not in self.pairs:
                raise ValueError(f"execution.venue_by_pair references unknown pair {pair!r}")
        for group in self.risk.correlated_groups:
            for pair in group:
                if pair not in self.pairs:
                    raise ValueError(f"risk.correlated_groups references unknown pair {pair!r}")
        for name in self.instances:
            if name not in self.pairs:
                raise ValueError(f"instances.{name} is not a configured pair")
        ports = [i.api_port for i in self.instances.values()] + [self.api.port]
        offsets = [i.magic_offset for i in self.instances.values()]
        if len(set(offsets)) != len(offsets) or (not self.paths.instance and len(set(ports)) != len(ports)):
            raise ValueError("instances need distinct api_port (and != api.port) and distinct magic_offset")
        if self.execution.mode == "live" and self.execution.live_confirmation != LIVE_CONFIRMATION_PHRASE:
            raise ValueError(
                "execution.mode=live requires execution.live_confirmation to equal "
                f"{LIVE_CONFIRMATION_PHRASE!r} (D-012)"
            )
        return self

    # ------------------------------------------------------------------ helpers
    @property
    def resource(self) -> ResourceProfileCfg:
        return self.resources[self.profile]

    def enabled_pairs(self) -> dict[str, PairCfg]:
        return {k: v for k, v in self.pairs.items() if v.enabled}

    def pair_timeframes(self, pair: str) -> list[Timeframe]:
        return list(self.pairs[pair].timeframes or self.timeframes)

    def active_provider_cfg(self) -> AIProviderCfg:
        return self.ai.providers[self.ai.active_provider]

    def provider_model(self, name: str) -> str:
        cfg = self.ai.providers[name]
        if cfg.model_env and os.environ.get(cfg.model_env, "").strip():
            return os.environ[cfg.model_env].strip()
        return cfg.model

    def mt5_data_profile(self) -> MT5ProfileCfg:
        return self.mt5.profiles[self.mt5.data_profile]

    def secret_env_names(self) -> set[str]:
        """Names of every env var that holds a secret (used by the log redaction filter)."""
        names: set[str] = {self.api.token_env}
        for p in self.ai.providers.values():
            if p.api_key_env:
                names.add(p.api_key_env)
        for prof in self.mt5.profiles.values():
            if prof.password_env:
                names.add(prof.password_env)
        for n in (self.binance.api_key_env, self.binance.api_secret_env):
            if n:
                names.add(n)
        names.update(TELEGRAM_SECRET_ENV)         # the notifier's bot (H18): the chat id is personal data too
        names.update(k for k in os.environ if _SECRET_NAME_RE.search(k))
        return names


# --------------------------------------------------------------------------- loading
def _resolve(p: str) -> Path:
    path = Path(p)
    return path if path.is_absolute() else PROJECT_ROOT / path


class _UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader that refuses a repeated key: ``execution: {…}`` written twice in config.local.yaml would silently
    drop the first block (e.g. a rollback switch) — a named error at start-up instead."""


def _construct_unique(loader: yaml.SafeLoader, node: yaml.MappingNode, deep: bool = False) -> dict:
    seen: set = set()
    for key_node, _ in node.value:
        if key_node.tag == "tag:yaml.org,2002:merge":          # '<<: *anchor' — flattened by construct_mapping
            continue
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise yaml.constructor.ConstructorError(
                None, None, f"duplicate key {key!r} — merge the two blocks into one", key_node.start_mark)
        seen.add(key)
    return loader.construct_mapping(node, deep=deep)


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique)


def _load_yaml(path: Path) -> dict[str, Any]:
    return yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueKeyLoader) or {}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _apply_env_overrides(raw: dict[str, Any], env: dict[str, str]) -> dict[str, Any]:
    out = copy.deepcopy(raw)
    for var, path in _ENV_OVERRIDES.items():
        value = env.get(var, "").strip()
        if not value or value.startswith("#"):  # empty or a stray inline comment from .env
            continue
        node = out
        for key in path[:-1]:
            node = node.setdefault(key, {})
        node[path[-1]] = value
    return out


INSTANCE_ENV = "TS_INSTANCE"

# instances.<PAIR>.overrides.risk may only TIGHTEN (D-049 / B14): per RiskCfg field the direction that is stricter.
# "down" = the override must be <= the base value, "up" = >=. A field without a direction here (lists, flags) is
# refused — a new RiskCfg field must be classified before an instance may override it (test_desk_cfg checks that).
RISK_TIGHTEN: dict[str, Literal["down", "up"]] = {
    "risk_per_trade_pct": "down", "max_risk_per_trade_pct": "down", "max_daily_loss_pct": "down",
    "min_rr": "up", "max_open_positions": "down", "max_effective_leverage": "down",
    "sl_atr_min_mult": "up", "sl_atr_max_mult": "down", "max_spread_to_sl_ratio": "down",
    "min_confidence": "up", "account_drawdown_stop_pct": "down", "max_recommendation_age_s": "down",
    "max_data_staleness_s": "down", "max_correlated_risk_pct": "down",
}


def _check_risk_tightens(instance: str, base_risk: dict[str, Any], override: Any) -> None:
    """Refuse an instance risk override that loosens (or cannot be judged as tightening) a RiskCfg field."""
    where = f"instances.{instance}.overrides.risk"
    if not isinstance(override, dict):
        raise ValueError(f"{where} must be a mapping")
    base = RiskCfg.model_validate(base_risk or {})
    for key, val in override.items():
        if key not in RiskCfg.model_fields:
            raise ValueError(f"{where}.{key}: unknown risk setting")
        direction = RISK_TIGHTEN.get(key)
        if direction is None:
            raise ValueError(f"{where}.{key} may not be overridden per instance (no tightening direction is defined "
                             "for it) — change it globally")
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            raise ValueError(f"{where}.{key}: {val!r} is not a number")
        cur = getattr(base, key)
        if (val > cur) if direction == "down" else (val < cur):
            raise ValueError(f"{where}.{key}: {val} would loosen the limit {cur} (an instance may only "
                             f"tighten: {'at most' if direction == 'down' else 'at least'} {cur})")


def _apply_instance(raw: dict[str, Any], instance: str) -> dict[str, Any]:
    """One system per pair (D-042): only ``instance`` is enabled; state, logs, API port and MT5 magic are the
    instance's own; Binance REST budgets are shared out between the configured instances (one IP limit)."""
    if not instance:
        return raw
    out = copy.deepcopy(raw)
    pairs = out.get("pairs") or {}
    icfg = (out.get("instances") or {}).get(instance)
    if instance not in pairs or icfg is None:
        raise ValueError(f"{INSTANCE_ENV}={instance!r}: not a configured instance (config 'instances:' has "
                         f"{sorted(out.get('instances') or {})})")
    ov = icfg.get("overrides") or {}
    bad = sorted(set(ov) & {"paths", "pairs", "instances", "config_hash"})
    if bad:
        raise ValueError(f"instances.{instance}.overrides may not set {bad}")
    if "risk" in ov:
        _check_risk_tightens(instance, out.get("risk") or {}, ov["risk"])
    if "execution" in ov and (not isinstance(ov["execution"], dict) or "magic" in ov["execution"]):
        raise ValueError(f"instances.{instance}.overrides.execution may not set 'magic' (the instance's magic is "
                         "execution.magic + magic_offset)")
    out = _deep_merge(out, ov)
    pairs = out.get("pairs") or {}
    for name, p in pairs.items():
        p["enabled"] = name == instance
    out.setdefault("paths", {})["instance"] = instance
    out.setdefault("api", {})["port"] = icfg["api_port"]
    ex = out.setdefault("execution", {})
    ex["magic"] = int(ex.get("magic", ExecutionCfg().magic)) + int(icfg["magic_offset"])
    n = max(1, len(out.get("instances") or {}))
    b = out.setdefault("binance", {})
    b["rest_weight_budget_per_min"] = max(300, int(b.get("rest_weight_budget_per_min", 3000)) // n)
    return out


def load_env(env_path: Path | None = None) -> None:
    """Load ``.env`` into ``os.environ`` without overriding variables already set."""
    path = env_path or DEFAULT_ENV
    if path.exists():
        for k, v in dotenv_values(path).items():
            if v is not None and k not in os.environ:
                os.environ[k] = v


def load_settings(
    config_path: Path | None = None,
    *,
    local_path: Path | None = None,
    env_path: Path | None = None,
    extra_env: dict[str, str] | None = None,
) -> Settings:
    load_env(env_path)
    if config_path is None and os.environ.get("TRADINGSYSTEM_CONFIG"):
        config_path = Path(os.environ["TRADINGSYSTEM_CONFIG"])     # alternate config (tests / dry runs)
    path = config_path or DEFAULT_CONFIG
    if not path.exists():
        raise FileNotFoundError(f"config file not found: {path}")
    raw = _load_yaml(path)
    local = local_path if local_path is not None else (LOCAL_CONFIG if config_path is None else None)
    if local is not None and local.exists():
        raw = _deep_merge(raw, _load_yaml(local))
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    raw = _apply_env_overrides(raw, env)
    instance = (env.get(INSTANCE_ENV) or "").strip().upper()
    base = raw
    raw = _apply_instance(raw, instance)
    digest = hashlib.sha256(json.dumps(raw, sort_keys=True, default=str).encode()).hexdigest()[:16]
    raw["config_hash"] = digest
    settings = Settings.model_validate(raw)
    if not instance:
        # every system's own view must load too: a bad per-pair override is caught by `tradingsystem config` and by
        # the *_all.bat pre-check, not half-way through restart_all (one pair failing after others were stopped)
        for name, icfg in (base.get("instances") or {}).items():
            if not (icfg or {}).get("overrides"):
                continue
            try:
                Settings.model_validate({**_apply_instance(base, str(name)), "config_hash": "check"})
            except Exception as exc:  # noqa: BLE001
                raise ValueError(f"instances.{name}.overrides: {exc}") from exc
    return settings


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton."""
    return load_settings()
