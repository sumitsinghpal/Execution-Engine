"""Request shapes for the watchlist endpoints (see src/api/server.py, src/execution/watchlists.py)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class AddWatchlistSymbolRequest(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=10)

    model_config = {"extra": "forbid"}


__all__ = ["AddWatchlistSymbolRequest"]
