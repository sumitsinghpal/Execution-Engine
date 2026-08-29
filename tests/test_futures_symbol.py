"""Tests for futures contract symbol parsing/formatting (src/models/futures_symbol.py)."""

from datetime import date

import pytest

from src.models.futures_symbol import (
    CONTRACT_MULTIPLIERS,
    format_futures_symbol,
    get_contract_multiplier,
    is_futures_symbol_shape,
    parse_futures_symbol,
)


class TestFormatFuturesSymbol:
    def test_formats_a_two_letter_root(self):
        assert format_futures_symbol("ES", "Z", 2026) == "ESZ26"

    def test_formats_a_three_letter_root(self):
        assert format_futures_symbol("RTY", "H", 2027) == "RTYH27"

    def test_uppercases_input(self):
        assert format_futures_symbol("es", "z", 2026) == "ESZ26"

    def test_rejects_root_longer_than_three(self):
        with pytest.raises(ValueError):
            format_futures_symbol("TOOLONG", "Z", 2026)

    def test_rejects_invalid_month_code(self):
        with pytest.raises(ValueError):
            format_futures_symbol("ES", "A", 2026)  # 'A' is not a real CME month code


class TestParseFuturesSymbol:
    def test_round_trips_with_format(self):
        original = format_futures_symbol("ES", "Z", 2026)
        parts = parse_futures_symbol(original)
        assert parts.root == "ES"
        assert parts.month_code == "Z"
        assert parts.year == 2026
        assert parts.contract_month == date(2026, 12, 1)

    def test_three_letter_root_round_trips(self):
        parts = parse_futures_symbol("RTYH27")
        assert parts.root == "RTY"
        assert parts.month_code == "H"
        assert parts.year == 2027
        assert parts.contract_month == date(2027, 3, 1)

    def test_one_letter_root_round_trips(self):
        # Not a real product, but the format itself must handle a 1-letter root.
        parts = parse_futures_symbol("XZ26")
        assert parts.root == "X"

    def test_plain_equity_ticker_is_rejected(self):
        with pytest.raises(ValueError):
            parse_futures_symbol("QQQ")

    def test_occ_option_symbol_is_rejected(self):
        with pytest.raises(ValueError):
            parse_futures_symbol("NVDA  280121C00120000")

    def test_bad_month_code_is_rejected(self):
        with pytest.raises(ValueError):
            parse_futures_symbol("ESA26")  # 'A' is not a real CME month code

    def test_too_long_root_is_rejected(self):
        with pytest.raises(ValueError):
            parse_futures_symbol("TOOLONGZ26")

    def test_lowercase_is_rejected(self):
        with pytest.raises(ValueError):
            parse_futures_symbol("esz26")


class TestIsFuturesSymbolShape:
    def test_true_for_a_two_letter_root(self):
        assert is_futures_symbol_shape("ESZ26") is True

    def test_true_for_a_three_letter_root(self):
        assert is_futures_symbol_shape("RTYH27") is True

    def test_false_for_a_plain_equity_ticker(self):
        assert is_futures_symbol_shape("QQQ") is False

    def test_false_for_an_occ_option_symbol(self):
        assert is_futures_symbol_shape("NVDA  280121C00120000") is False

    def test_false_for_lowercase(self):
        assert is_futures_symbol_shape("esz26") is False


class TestGetContractMultiplier:
    def test_known_product(self):
        assert get_contract_multiplier("ES") == 50

    def test_is_case_insensitive(self):
        assert get_contract_multiplier("es") == CONTRACT_MULTIPLIERS["ES"]

    def test_unknown_product_is_rejected(self):
        with pytest.raises(ValueError):
            get_contract_multiplier("ZZZ")
