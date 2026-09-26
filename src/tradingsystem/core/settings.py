"""Typed settings: ``config/config.yaml`` (+ optional ``config/config.local.yaml``) + ``.env``.

* Non-secret settings live in YAML (committed). Secrets live only in environment variables /
  ``.env`` (git-ignored) and are referenced from YAML by *name* (``*_env`` fields).
* A few switches can be flipped with one line in ``.env`` (spec §4.1): ``ACTIVE_AI_PROVIDER``,
  ``AGENT_MODE``, ``TRIGGER_POLICY``, ``EXECUTION_MODE``, ``EXECUTION_TRIGGER``, ``RESOURCE_PROFILE``,
  and ``<PROVIDER>_MODEL`` through each provider's ``model_env``.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .timeframes import Timeframe

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "config.yaml"
LOCAL_CONFIG = PROJECT_ROOT / "config" / "config.local.yaml"
DEFAULT_ENV = PROJECT_ROOT / ".env"

LIVE_CONFIRMATION_PHRASE = "I ACCEPT REAL-MONEY TRADING RISK"

Venue = Literal["binance_spot", "binance_usdm", "mt5"]
DataType = Literal[
    "candles", "agg_trades", "ticks", "book_ticker", "depth", "funding", "open_interest",
    "metrics", "liquidations", "mark_price",
]
Role = Literal["analysis_primary", "flow_context", "execution", "quote_reference"]
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
    data_dir: str = "data"
    logs_dir: str = "logs"

    def data(self) -> Path:
        return _resolve(self.data_dir)

    def logs(self) -> Path:
        return _resolve(self.logs_dir)


class StorageCfg(_Model):
    """Hot (SQLite) / cold (Parquet) split — D-020 (benchmark docs/benchmarks/storage.md)."""
    # days of raw high-volume data kept in the hot SQLite store before rollover to daily Parquet
    hot_days: dict[str, int] = Field(default_factory=lambda: {
        "agg_trades": 2, "ticks": 3, "book_ticker": 2, "liquidations": 30, "depth": 30})
    # hours after UTC midnight before a closed day is rolled to Parquet (gap-fill must have run)
    rollover_grace_hours: int = 2
    # stop backfills (not live capture) when free disk falls below this
    min_free_disk_gb: float = 10.0


class LoggingCfg(_Model):
    level: str = "INFO"
    max_bytes: int = 20 * 1024 * 1024
    backups: int = 10
    console: bool = True


class ResourceProfileCfg(_Model):
    duckdb_memory_mb: int = 512
    duckdb_threads: int = 2
    sqlite_cache_mb: int = 16
    engine_cycle_in_subprocess: bool = False
    vision_download_concurrency: int = 2
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
class ContractCfg(_Model):
    """Execution contract facts (measured from MT5 symbol_info, P1.5; re-validated by the executor at runtime)."""
    contract_size: float
    volume_min: float
    volume_step: float
    tick_size: float


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
    instruments: list[InstrumentCfg]

    @model_validator(mode="after")
    def _check_roles(self) -> "PairCfg":
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


class ExecutionCfg(_Model):
    paper_equity: float = 100.0            # starting equity of the paper account (user's intended capital, D-021)
    mode: ExecutionMode = "paper"
    trigger: ExecutionTrigger = "manual"
    venue_by_pair: dict[str, Venue] = Field(default_factory=dict)
    max_basis_deviation_pct: float = 0.15
    magic: int = 26092501
    live_confirmation: str = ""


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
    temperature: float | None = None      # sampling temperature; None = the model default (Gemini 3.x wants that)
    quota_reset_tz: str | None = None      # daily-quota reset time zone (Gemini: America/Los_Angeles); None = UTC


class AIBudgetCfg(_Model):
    daily_usd_cap: float = 0.0
    monthly_usd_cap: float = 0.0
    max_ai_cost_to_profit_ratio: float = 0.2
    rolling_window_days: int = 30
    min_trades_for_ratio: int = 20


class AICfg(_Model):
    active_provider: str
    fallback_provider: str | None = None    # used while the active provider is unavailable (D-030)
    agent_mode: AgentMode = "agent_per_pair"
    trigger_policy: TriggerPolicy = "hybrid"
    output_language: str = "en"
    min_minutes_between_calls: int = 15     # per pair (protects free-tier quotas)
    max_idle_minutes: int = 120             # hybrid policy: review a pair at least this often (market open)
    max_parallel_calls: int = 2
    review_floor_minutes: int = 5           # next_review price/candle triggers: not sooner after the last call
    max_backoff_minutes: int = 120          # per-pair back-off cap after failed cycles (spacing doubles per failure)
    cycle_deadline_s: float = 600.0         # an AI cycle is cut off after this; unfinished pairs stored as 'error'
    consensus_providers: list[str] = Field(default_factory=list)
    providers: dict[str, AIProviderCfg]
    budget: AIBudgetCfg = AIBudgetCfg()

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


# --------------------------------------------------------------------------- root
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
        names.update(k for k in os.environ if _SECRET_NAME_RE.search(k))
        return names


# --------------------------------------------------------------------------- loading
def _resolve(p: str) -> Path:
    path = Path(p)
    return path if path.is_absolute() else PROJECT_ROOT / path


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
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    local = local_path if local_path is not None else (LOCAL_CONFIG if config_path is None else None)
    if local is not None and local.exists():
        raw = _deep_merge(raw, yaml.safe_load(local.read_text(encoding="utf-8")) or {})
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    raw = _apply_env_overrides(raw, env)
    digest = hashlib.sha256(json.dumps(raw, sort_keys=True, default=str).encode()).hexdigest()[:16]
    raw["config_hash"] = digest
    return Settings.model_validate(raw)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton."""
    return load_settings()
