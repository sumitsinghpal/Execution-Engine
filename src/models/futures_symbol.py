"""
Futures contract symbol format — the standard CME-style symbol for a
specific futures contract month: a 1-3 letter product root, a 1-letter
month code, and a 2-digit year (e.g. "ESZ26" = E-mini S&P 500, December
2026).

Month codes (the actual letters CME/ICE use — not simply J=January):
F=Jan G=Feb H=Mar J=Apr K=May M=Jun N=Jul Q=Aug U=Sep V=Oct X=Nov Z=Dec

This is deliberately the smaller, scoped-down piece of "futures support":
symbol parsing/validation, a contract-multiplier lookup so notional/risk
checks price a contract correctly, and PAPER-mode quotes (which fall
straight through PaperBrokerAdapter's existing synthetic-price path — a
futures symbol needs no special quote handling once it parses). What this
module does NOT attempt: SPAN margin, a real futures chain/rollover
calendar, or continuous-contract price stitching — a real futures desk's
concerns, out of scope for what this system needs to route a paper trade
correctly.

Mirrors src/models/occ_symbol.py's shape: parse/format functions plus a
shape-check, so TradeProposal validation (src/models/orders.py) and order
construction (src/broker/order_builder.py) have exactly one place to go.
"""

from dataclasses import dataclass
from datetime import date

_MONTH_CODES = {
    "F": 1, "G": 2, "H": 3, "J": 4, "K": 5, "M": 6,
    "N": 7, "Q": 8, "U": 9, "V": 10, "X": 11, "Z": 12,
}
_ROOT_MIN_LEN = 1
_ROOT_MAX_LEN = 3

# A deliberately small, representative subset of actively-traded CME/ICE
# contracts — not the full exchange catalog. An unlisted root is rejected
# outright (see get_contract_multiplier) rather than silently priced as if
# it were 1 share, which would understate real notional risk exactly the
# way an un-multiplied option premium would (see
# src/risk/limits.py's _OPTION_CONTRACT_MULTIPLIER).
CONTRACT_MULTIPLIERS: dict[str, int] = {
    "ES": 50,      # E-mini S&P 500
    "NQ": 20,      # E-mini Nasdaq-100
    "YM": 5,       # E-mini Dow
    "RTY": 50,     # E-mini Russell 2000
    "CL": 1000,    # Crude Oil (WTI)
    "NG": 10000,   # Natural Gas
    "GC": 100,     # Gold
    "SI": 5000,    # Silver
    "ZB": 1000,    # 30-Year U.S. Treasury Bond
    "ZN": 1000,    # 10-Year U.S. Treasury Note
    "ZC": 5000,    # Corn
    "ZS": 5000,    # Soybeans
    "ZW": 5000,    # Wheat
}


@dataclass(frozen=True)
class FuturesSymbolParts:
    root: str
    month_code: str
    year: int  # full 4-digit year
    contract_month: date  # the 1st of the contract's delivery month — for display, not an exact expiration date


def is_futures_symbol_shape(symbol: str) -> bool:
    """
    A cheap, non-throwing check: could this be a futures symbol at all
    (vs a plain equity ticker or an OCC option symbol)? An equity ticker
    is pure letters (see orders.py's `[A-Z]{1,5}` rule) and an OCC symbol
    is always exactly 21 characters — a futures symbol is the one shape
    that's short AND ends in digits, which is enough to disambiguate
    without fully parsing it.
    """
    return 4 <= len(symbol) <= 6 and symbol[-2:].isdigit() and symbol[:-2].isalpha() and symbol[:-2].isupper()


def parse_futures_symbol(symbol: str) -> FuturesSymbolParts:
    """Raises ValueError with a specific reason on anything malformed — never returns a partially-valid result."""
    if not is_futures_symbol_shape(symbol):
        raise ValueError(f"Futures symbol must be a 1-3 letter root + month code + 2-digit year (e.g. 'ESZ26'), got {symbol!r}")

    root = symbol[:-3]
    month_code = symbol[-3]
    year_part = symbol[-2:]

    if not (_ROOT_MIN_LEN <= len(root) <= _ROOT_MAX_LEN):
        raise ValueError(f"Futures symbol root must be 1-3 letters, got {root!r} in {symbol!r}")
    if month_code not in _MONTH_CODES:
        raise ValueError(f"Futures symbol month code must be one of {sorted(_MONTH_CODES)}, got {month_code!r} in {symbol!r}")

    year = 2000 + int(year_part)
    return FuturesSymbolParts(root=root, month_code=month_code, year=year, contract_month=date(year, _MONTH_CODES[month_code], 1))


def format_futures_symbol(root: str, month_code: str, year: int) -> str:
    """The inverse of parse_futures_symbol — builds a valid symbol from its parts."""
    root = root.upper()
    if not (_ROOT_MIN_LEN <= len(root) <= _ROOT_MAX_LEN) or not root.isalpha():
        raise ValueError(f"root must be 1-{_ROOT_MAX_LEN} letters, got {root!r}")
    month_code = month_code.upper()
    if month_code not in _MONTH_CODES:
        raise ValueError(f"month_code must be one of {sorted(_MONTH_CODES)}, got {month_code!r}")
    return f"{root}{month_code}{year % 100:02d}"


def get_contract_multiplier(root: str) -> int:
    """Raises ValueError for a root not in CONTRACT_MULTIPLIERS — reject-by-default, same as everywhere else in this codebase, rather than silently pricing an unknown contract as 1x."""
    root = root.upper()
    if root not in CONTRACT_MULTIPLIERS:
        raise ValueError(f"No contract multiplier configured for futures root {root!r} — see CONTRACT_MULTIPLIERS")
    return CONTRACT_MULTIPLIERS[root]


__all__ = [
    "CONTRACT_MULTIPLIERS",
    "FuturesSymbolParts",
    "format_futures_symbol",
    "get_contract_multiplier",
    "is_futures_symbol_shape",
    "parse_futures_symbol",
]
