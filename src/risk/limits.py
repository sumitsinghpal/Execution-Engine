from __future__ import annotations

from datetime import UTC, datetime, time
from decimal import Decimal

from src.config import get_settings
from src.models.orders import TradeProposal


class RiskViolation(ValueError):
    pass


def _price_for_notional(proposal: TradeProposal) -> Decimal:
    if proposal.limit_price is None:
        return Decimal("100")
    return Decimal(str(proposal.limit_price))


def enforce_account_allowlist(proposal: TradeProposal) -> None:
    settings = get_settings()
    if proposal.account not in settings.allowed_accounts_list:
        raise RiskViolation("account is not allowlisted")


def enforce_symbol_policy(proposal: TradeProposal) -> None:
    settings = get_settings()
    symbol = proposal.symbol.upper()
    if symbol in settings.denied_symbols_list:
        raise RiskViolation("symbol is denylisted")
    if symbol not in settings.allowed_symbols_list:
        raise RiskViolation("symbol is not allowlisted")


def enforce_max_order_notional(proposal: TradeProposal) -> Decimal:
    settings = get_settings()
    notional = _price_for_notional(proposal) * Decimal(proposal.quantity)
    if notional > settings.max_order_notional:
        raise RiskViolation("order exceeds max notional")
    return notional


def enforce_max_concentration(notional: Decimal) -> None:
    settings = get_settings()
    concentration = notional / settings.account_equity_notional
    if concentration > settings.max_position_concentration:
        raise RiskViolation("order exceeds max position concentration")


def enforce_market_hours() -> None:
    settings = get_settings()
    if not settings.enforce_market_hours:
        return
    now = datetime.now(UTC)
    if now.weekday() >= 5:
        raise RiskViolation("market is closed")
    market_open = time(13, 30)
    market_close = time(20, 0)
    if not (market_open <= now.time().replace(tzinfo=None) <= market_close):
        raise RiskViolation("outside market hours policy")
