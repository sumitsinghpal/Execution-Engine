"""
Named lists of symbols a customer wants to keep an eye on — the "save a
list of tickers, see them together with live prices" pattern real
brokerage apps all have, that this dashboard never had (only one symbol
field, shared by the chart and order form, nothing persisted).

One row per (list_name, symbol) pair rather than a separate "watchlist
header" table — a list is just "every row with this list_name," created
implicitly by adding its first symbol and gone once its last symbol is
removed. Simple by design: this is a single-customer system (see
memory/prior design decisions on multi-tenancy), so there's no owner
column to scope by.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlmodel import Field, Session, SQLModel, select

from src.logging_config import get_logger

logger = get_logger(__name__)

DEFAULT_LIST_NAME = "Watchlist"


class WatchlistItemRecord(SQLModel, table=True):
    __tablename__ = "watchlist_items"

    id: Optional[int] = Field(default=None, primary_key=True)
    list_name: str = Field(index=True)
    symbol: str = Field(index=True)
    added_at: datetime = Field(default_factory=datetime.utcnow)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "list_name": self.list_name, "symbol": self.symbol, "added_at": self.added_at}


class WatchlistService:
    def __init__(self, session: Session):
        self.session = session

    def list_names(self) -> list[str]:
        """Every distinct list that currently has at least one symbol in it, alphabetical."""
        stmt = select(WatchlistItemRecord.list_name).distinct()
        return sorted(set(self.session.exec(stmt).all()))

    def items_in(self, list_name: str) -> list[WatchlistItemRecord]:
        stmt = select(WatchlistItemRecord).where(WatchlistItemRecord.list_name == list_name).order_by(WatchlistItemRecord.added_at)
        return list(self.session.exec(stmt).all())

    def all_lists(self) -> dict[str, list[str]]:
        """{list_name: [symbol, ...]} for every list — what the dashboard actually wants on load, one call instead of N."""
        result: dict[str, list[str]] = {}
        for item in self.session.exec(select(WatchlistItemRecord).order_by(WatchlistItemRecord.added_at)).all():
            result.setdefault(item.list_name, []).append(item.symbol)
        return result

    def add_symbol(self, list_name: str, symbol: str) -> WatchlistItemRecord:
        symbol = symbol.upper().strip()
        list_name = list_name.strip() or DEFAULT_LIST_NAME
        if not symbol:
            raise ValueError("symbol must not be empty")

        existing = self.session.exec(
            select(WatchlistItemRecord).where(WatchlistItemRecord.list_name == list_name, WatchlistItemRecord.symbol == symbol)
        ).first()
        if existing:
            return existing  # already on this list — adding again is a no-op, not a duplicate row

        item = WatchlistItemRecord(list_name=list_name, symbol=symbol)
        self.session.add(item)
        self.session.commit()
        self.session.refresh(item)
        logger.info("watchlist_symbol_added", list_name=list_name, symbol=symbol)
        return item

    def remove_symbol(self, list_name: str, symbol: str) -> bool:
        symbol = symbol.upper().strip()
        item = self.session.exec(
            select(WatchlistItemRecord).where(WatchlistItemRecord.list_name == list_name, WatchlistItemRecord.symbol == symbol)
        ).first()
        if not item:
            return False
        self.session.delete(item)
        self.session.commit()
        logger.info("watchlist_symbol_removed", list_name=list_name, symbol=symbol)
        return True

    def delete_list(self, list_name: str) -> int:
        """Removes every symbol in a list — the list itself has no independent existence beyond its rows, so this is how a list disappears. Returns how many symbols were removed."""
        items = self.items_in(list_name)
        for item in items:
            self.session.delete(item)
        self.session.commit()
        if items:
            logger.info("watchlist_deleted", list_name=list_name, symbol_count=len(items))
        return len(items)


__all__ = ["DEFAULT_LIST_NAME", "WatchlistItemRecord", "WatchlistService"]
