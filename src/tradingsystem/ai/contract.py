"""Unified output contract v1 (spec §5, plan item 11) — the only shape that reaches the user or execution.

Semantic validation is part of the contract (not left to the AI):
* BUY/SELL require an order type consistent with the side, an entry (price or range), a stop-loss on the
  correct side, take-profits in the trade direction, and risk fields — otherwise the model is invalid
  (spec §0: a recommendation without SL must never reach execution).
* NO_TRADE is a first-class outcome (spec §5 note) and must carry no trade levels.
* Risk/reward is recomputed from the levels; the model's own figure is kept only for comparison.
"""
from __future__ import annotations

import datetime as dt
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CONTRACT_VERSION = "1.0"


class Decision(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    NO_TRADE = "NO_TRADE"


class OrderType(str, Enum):
    MARKET = "MARKET"
    BUY_LIMIT = "BUY_LIMIT"
    SELL_LIMIT = "SELL_LIMIT"
    BUY_STOP = "BUY_STOP"
    SELL_STOP = "SELL_STOP"


_SIDE_ORDERS = {
    Decision.BUY: {OrderType.MARKET, OrderType.BUY_LIMIT, OrderType.BUY_STOP},
    Decision.SELL: {OrderType.MARKET, OrderType.SELL_LIMIT, OrderType.SELL_STOP},
}


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Entry(_M):
    price: float | None = Field(None, gt=0, description="Single entry price (reference price for MARKET)")
    range_min: float | None = Field(None, gt=0, description="Lower bound of an entry zone")
    range_max: float | None = Field(None, gt=0, description="Upper bound of an entry zone")

    @model_validator(mode="after")
    def _check(self) -> "Entry":
        if self.price is None and (self.range_min is None or self.range_max is None):
            raise ValueError("entry needs `price` or both `range_min` and `range_max`")
        if (self.range_min is None) != (self.range_max is None):
            raise ValueError("entry range needs both bounds")
        if self.range_min is not None and self.range_min > self.range_max:
            raise ValueError("entry range_min > range_max")
        if self.price is not None and self.range_min is not None and not (self.range_min <= self.price <= self.range_max):
            raise ValueError("entry price outside its range")
        return self

    @property
    def worst(self) -> tuple[float, float]:
        lo = self.range_min if self.range_min is not None else self.price
        hi = self.range_max if self.range_max is not None else self.price
        return float(lo), float(hi)

    @property
    def mid(self) -> float:
        if self.price is not None:
            return float(self.price)
        return (float(self.range_min) + float(self.range_max)) / 2


class TakeProfit(_M):
    price: float = Field(gt=0)
    close_fraction: float = Field(gt=0, le=1, description="Fraction of the position closed at this target")
    label: str | None = Field(None, max_length=40)


class ManagementRule(_M):
    action: Literal["move_sl_to_breakeven", "partial_close", "trail_atr", "trail_structure", "close_all"]
    trigger: Literal["tp_hit", "price_reached", "r_multiple", "minutes_elapsed", "candle_close"]
    value: float | None = Field(None, description="TP index (1-based), price, R multiple or minutes, per trigger")
    params: dict[str, float] = Field(default_factory=dict, description="e.g. {'atr_mult': 1.5, 'fraction': 0.5}")
    note: str | None = Field(None, max_length=200)


class ReviewCondition(_M):
    kind: Literal["price_above", "price_below", "candle_close_above", "candle_close_below", "minutes_elapsed"]
    value: float = Field(description="Price level or minutes")
    timeframe: Literal["1m", "5m", "15m", "1h", "4h", "1d"] | None = None
    note: str | None = Field(None, max_length=200)


class NextReview(_M):
    in_minutes: int = Field(ge=1, le=1440)
    conditions: list[ReviewCondition] = Field(default_factory=list, max_length=6)
    or_condition: str = Field("", max_length=400, description="Free-text restatement for humans")


class RiskManagement(_M):
    risk_percent_suggested: float = Field(gt=0, le=10)
    risk_reward_ratio: float = Field(gt=0, description="Model's own RR (recomputed by the system)")
    invalidation_reason: str = Field(min_length=5, max_length=500)
    sl_basis: str = Field(min_length=5, max_length=300, description="Why the SL sits there (ATR / structure)")


class Recommendation(_M):
    contract_version: Literal["1.0"] = CONTRACT_VERSION
    pair: str = Field(min_length=3, max_length=20)
    timestamp: dt.datetime
    valid_until: dt.datetime
    price_reference: str = Field(description="Venue:symbol whose price space the levels use, e.g. 'mt5:XAUUSD@'")
    market_summary: str = Field(min_length=10, max_length=1500)
    decision: Decision
    order_type: OrderType | None = None
    entry: Entry | None = None
    take_profits: list[TakeProfit] = Field(default_factory=list, max_length=4)
    stop_loss: float | None = Field(None, gt=0)
    risk_management: RiskManagement | None = None
    confidence: int = Field(ge=0, le=100)
    instructions: str = Field("", max_length=1500)
    management: list[ManagementRule] = Field(default_factory=list, max_length=6)
    next_review: NextReview
    reasoning_trace: str = Field(min_length=10, max_length=3000)
    data_quality_notes: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("timestamp", "valid_until")
    @classmethod
    def _aware(cls, v: dt.datetime) -> dt.datetime:
        if v.tzinfo is None:
            raise ValueError("timestamps must include a timezone (UTC)")
        return v.astimezone(dt.timezone.utc)

    @model_validator(mode="after")
    def _semantics(self) -> "Recommendation":
        if self.valid_until <= self.timestamp:
            raise ValueError("valid_until must be after timestamp")
        if self.decision is Decision.NO_TRADE:
            if any(x is not None for x in (self.order_type, self.entry, self.stop_loss)) or self.take_profits:
                raise ValueError("NO_TRADE must not carry order_type/entry/stop_loss/take_profits")
            return self
        # ---- BUY / SELL
        if self.order_type is None or self.order_type not in _SIDE_ORDERS[self.decision]:
            raise ValueError(f"order_type {self.order_type} is not valid for {self.decision.value}")
        if self.entry is None:
            raise ValueError("BUY/SELL requires an entry")
        if self.order_type is not OrderType.MARKET and self.entry.price is None and self.entry.range_min is None:
            raise ValueError("pending orders need an entry price or range")
        if self.stop_loss is None:
            raise ValueError("BUY/SELL without stop_loss is invalid (spec §0)")
        if self.risk_management is None:
            raise ValueError("BUY/SELL requires risk_management")
        if not self.take_profits:
            raise ValueError("BUY/SELL requires at least one take-profit")
        lo, hi = self.entry.worst
        buy = self.decision is Decision.BUY
        if buy and not self.stop_loss < lo:
            raise ValueError("BUY stop_loss must be below the entry (range)")
        if not buy and not self.stop_loss > hi:
            raise ValueError("SELL stop_loss must be above the entry (range)")
        prices = [tp.price for tp in self.take_profits]
        if buy and not all(p > hi for p in prices):
            raise ValueError("BUY take-profits must be above the entry (range)")
        if not buy and not all(p < lo for p in prices):
            raise ValueError("SELL take-profits must be below the entry (range)")
        if prices != sorted(prices, reverse=not buy):
            raise ValueError("take-profits must be ordered from nearest to farthest")
        if sum(tp.close_fraction for tp in self.take_profits) > 1.0 + 1e-6:
            raise ValueError("take-profit close fractions exceed 100 %")
        return self

    # ------------------------------------------------------------------ derived (never trusted from the model)
    @property
    def is_trade(self) -> bool:
        return self.decision is not Decision.NO_TRADE

    def rr_computed(self) -> float | None:
        """Reward/risk using the worst entry edge and the fraction-weighted targets."""
        if not self.is_trade:
            return None
        lo, hi = self.entry.worst
        entry = hi if self.decision is Decision.BUY else lo          # worst fill inside the zone
        risk = abs(entry - self.stop_loss)
        if risk <= 0:
            return None
        total = sum(tp.close_fraction for tp in self.take_profits)
        reward = sum(abs(tp.price - entry) * tp.close_fraction for tp in self.take_profits) / total
        return reward / risk

    def rr_mismatch(self, tol: float = 0.2) -> bool:
        rr = self.rr_computed()
        if rr is None or self.risk_management is None:
            return False
        return abs(rr - self.risk_management.risk_reward_ratio) > tol * max(rr, 1e-9)


class KeyLevel(_M):
    price: float = Field(gt=0)
    kind: Literal["support", "resistance", "liquidity_high", "liquidity_low", "order_block", "fvg", "poc", "vwap",
                  "other"]
    note: str | None = Field(None, max_length=160)


class TimeframeAssessment(_M):
    """Sub-agent output in multi-agent modes (per timeframe) — consumed by a coordinator, never executed."""
    pair: str
    timeframe: Literal["1m", "5m", "15m", "1h", "4h", "1d", "1w"]
    bias: Literal["bullish", "bearish", "neutral", "unclear"]
    confidence: int = Field(ge=0, le=100)
    structure: str = Field(max_length=800)
    key_levels: list[KeyLevel] = Field(default_factory=list, max_length=12)
    candidate_setups: list[str] = Field(default_factory=list, max_length=4)
    risks: list[str] = Field(default_factory=list, max_length=6)
    data_quality_notes: list[str] = Field(default_factory=list, max_length=6)


class RecommendationSet(_M):
    """single_agent_global output: one recommendation per pair."""
    recommendations: list[Recommendation] = Field(min_length=1, max_length=20)


class AssessmentSet(_M):
    """timeframe analyst output when a payload covers several pairs (agent_per_timeframe)."""
    assessments: list[TimeframeAssessment] = Field(min_length=1, max_length=20)


class RiskReview(_M):
    """risk_reviewer output (agent_per_pair_with_risk_reviewer)."""
    verdict: Literal["approve", "modify", "reject"]
    issues: list[str] = Field(default_factory=list, max_length=10)
    final_recommendation: Recommendation

    @model_validator(mode="after")
    def _reject_means_no_trade(self) -> "RiskReview":
        if self.verdict == "reject" and self.final_recommendation.is_trade:
            raise ValueError("a rejected proposal must end as NO_TRADE")
        return self


def recommendation_schema() -> dict:
    return Recommendation.model_json_schema()


def assessment_schema() -> dict:
    return TimeframeAssessment.model_json_schema()
