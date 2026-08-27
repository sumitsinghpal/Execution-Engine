"""
OCC (Options Clearing Corporation) option symbol format — the standard
21-character symbol real brokers (including Schwab) use for a specific
option contract: a 6-char root (space-padded), a 6-digit expiration date
(YYMMDD), a 1-char right (C/P), and an 8-digit strike price in thousandths
of a dollar (e.g. strike $120.00 -> "00120000").

Example: "NVDA  280121C00120000" is a NVDA $120 call expiring 2028-01-21.

This is the one place both symbol validation (src/models/orders.py) and
order construction (src/broker/order_builder.py) go to parse or build one,
so the format is defined and tested in exactly one spot.
"""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

OCC_SYMBOL_LENGTH = 21
_ROOT_LEN = 6
_DATE_LEN = 6
_RIGHT_LEN = 1
_STRIKE_LEN = 8


@dataclass(frozen=True)
class OCCSymbolParts:
    underlying: str
    expiration: date
    right: str  # "C" or "P"
    strike: Decimal


def is_occ_symbol_shape(symbol: str) -> bool:
    """A cheap, non-throwing check: is this 21 characters, i.e. shaped like an OCC option symbol at all (vs a plain equity ticker)?"""
    return len(symbol) == OCC_SYMBOL_LENGTH


def parse_occ_symbol(symbol: str) -> OCCSymbolParts:
    """Raises ValueError with a specific reason on anything malformed — never returns a partially-valid result."""
    if len(symbol) != OCC_SYMBOL_LENGTH:
        raise ValueError(f"OCC option symbol must be exactly {OCC_SYMBOL_LENGTH} characters, got {len(symbol)}: {symbol!r}")

    root_raw = symbol[0:_ROOT_LEN]
    date_part = symbol[_ROOT_LEN : _ROOT_LEN + _DATE_LEN]
    right = symbol[_ROOT_LEN + _DATE_LEN : _ROOT_LEN + _DATE_LEN + _RIGHT_LEN]
    strike_part = symbol[_ROOT_LEN + _DATE_LEN + _RIGHT_LEN :]

    root = root_raw.rstrip()
    if not root or not root.isalpha() or not root.isupper():
        raise ValueError(f"OCC symbol root must be 1-6 uppercase letters (space-padded): {root_raw!r}")

    if right not in ("C", "P"):
        raise ValueError(f"OCC symbol right must be 'C' (call) or 'P' (put), got {right!r}")

    if not date_part.isdigit():
        raise ValueError(f"OCC symbol expiration must be 6 digits (YYMMDD): {date_part!r}")
    try:
        expiration = datetime.strptime(date_part, "%y%m%d").date()
    except ValueError as exc:
        raise ValueError(f"OCC symbol expiration {date_part!r} is not a valid YYMMDD date") from exc

    if not strike_part.isdigit() or len(strike_part) != _STRIKE_LEN:
        raise ValueError(f"OCC symbol strike must be 8 digits (thousandths of a dollar): {strike_part!r}")
    try:
        strike = Decimal(strike_part) / Decimal(1000)
    except InvalidOperation as exc:
        raise ValueError(f"OCC symbol strike {strike_part!r} could not be parsed") from exc
    if strike <= 0:
        raise ValueError("OCC symbol strike must be positive")

    return OCCSymbolParts(underlying=root, expiration=expiration, right=right, strike=strike)


def format_occ_symbol(underlying: str, expiration: date, right: str, strike: Decimal) -> str:
    """The inverse of parse_occ_symbol — builds a valid 21-char OCC symbol from its parts."""
    underlying = underlying.upper()
    if not underlying or len(underlying) > _ROOT_LEN or not underlying.isalpha():
        raise ValueError(f"underlying must be 1-{_ROOT_LEN} letters, got {underlying!r}")
    right = right.upper()
    if right not in ("C", "P"):
        raise ValueError(f"right must be 'C' or 'P', got {right!r}")
    if strike <= 0:
        raise ValueError("strike must be positive")

    root = underlying.ljust(_ROOT_LEN)
    date_part = expiration.strftime("%y%m%d")
    strike_thousandths = int(round(strike * 1000))
    strike_part = f"{strike_thousandths:0{_STRIKE_LEN}d}"
    if len(strike_part) != _STRIKE_LEN:
        raise ValueError(f"strike {strike} is out of the representable OCC range")

    return f"{root}{date_part}{right}{strike_part}"


__all__ = ["OCCSymbolParts", "OCC_SYMBOL_LENGTH", "is_occ_symbol_shape", "parse_occ_symbol", "format_occ_symbol"]
