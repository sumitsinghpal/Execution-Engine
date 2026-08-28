"""Request/response shapes for POST /v1/backtest/run (see src/api/server.py)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator

MAX_SYMBOLS_PER_REQUEST = 10
MAX_DATE_RANGE_DAYS = 366 * 15  # ~15 years — yfinance has more, but a single request shouldn't try to pull it all


class BacktestRequest(BaseModel):
    symbols: list[str] = Field(default_factory=lambda: ["QQQ", "SPY", "IWM"])
    strategy_ids: list[str] = Field(default_factory=lambda: ["golden_cross", "turtle_donchian", "rsi2_connors"])
    start_date: date
    end_date: date
    risk_pct: Decimal = Decimal("0.01")
    reward_risk_ratio: Decimal = Decimal("2")
    notional_per_trade_usd: Decimal = Decimal("1000")
    starting_capital: float = 100_000.0
    # 5bps default: a liquid ETF fill a few cents worse than the exact
    # stop/target price, not a compliance-grade slippage model.
    slippage_bps: Decimal = Decimal("5")
    # $0 default: Schwab (and most brokers now) charge no commission on
    # stock/ETF trades. Set this if modeling a broker that does.
    commission_per_order_usd: Decimal = Decimal("0")

    model_config = {"extra": "forbid"}

    @field_validator("symbols")
    @classmethod
    def _validate_symbols(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("symbols must not be empty")
        if len(v) > MAX_SYMBOLS_PER_REQUEST:
            raise ValueError(f"at most {MAX_SYMBOLS_PER_REQUEST} symbols per request — this fetches real network data per symbol")
        return [s.upper() for s in v]

    @field_validator("strategy_ids")
    @classmethod
    def _validate_strategy_ids(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("strategy_ids must not be empty")
        return v

    @model_validator(mode="after")
    def _validate_date_range(self) -> "BacktestRequest":
        if self.end_date <= self.start_date:
            raise ValueError("end_date must be after start_date")
        if (self.end_date - self.start_date).days > MAX_DATE_RANGE_DAYS:
            raise ValueError(f"date range too large (max {MAX_DATE_RANGE_DAYS} days)")
        return self


__all__ = ["BacktestRequest", "MAX_DATE_RANGE_DAYS", "MAX_SYMBOLS_PER_REQUEST"]
