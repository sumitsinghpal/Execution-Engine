"""Tests for OCC option symbol parsing/formatting (src/models/occ_symbol.py)."""

from datetime import date
from decimal import Decimal

import pytest

from src.models.occ_symbol import format_occ_symbol, is_occ_symbol_shape, parse_occ_symbol


class TestFormatOccSymbol:
    def test_formats_a_known_example(self):
        symbol = format_occ_symbol("NVDA", date(2028, 1, 21), "C", Decimal("120"))
        assert symbol == "NVDA  280121C00120000"
        assert len(symbol) == 21

    def test_pads_a_short_root(self):
        symbol = format_occ_symbol("F", date(2027, 6, 18), "P", Decimal("12.50"))
        assert symbol.startswith("F     ")  # 1 letter + 5 spaces = 6 chars

    def test_rejects_root_longer_than_six(self):
        with pytest.raises(ValueError):
            format_occ_symbol("TOOLONGX", date(2027, 1, 1), "C", Decimal("10"))

    def test_rejects_non_positive_strike(self):
        with pytest.raises(ValueError):
            format_occ_symbol("QQQ", date(2027, 1, 1), "C", Decimal("0"))

    def test_rejects_invalid_right(self):
        with pytest.raises(ValueError):
            format_occ_symbol("QQQ", date(2027, 1, 1), "X", Decimal("10"))


class TestParseOccSymbol:
    def test_round_trips_with_format(self):
        original = format_occ_symbol("SPY", date(2026, 12, 18), "P", Decimal("450.50"))
        parts = parse_occ_symbol(original)
        assert parts.underlying == "SPY"
        assert parts.expiration == date(2026, 12, 18)
        assert parts.right == "P"
        assert parts.strike == Decimal("450.500")

    def test_wrong_length_is_rejected(self):
        with pytest.raises(ValueError, match="21 characters"):
            parse_occ_symbol("QQQ")

    def test_bad_right_character_is_rejected(self):
        malformed = "QQQ   280121X00120000"
        with pytest.raises(ValueError, match="right"):
            parse_occ_symbol(malformed)

    def test_non_numeric_date_is_rejected(self):
        malformed = "QQQ   XXXXXXC00120000"
        with pytest.raises(ValueError):
            parse_occ_symbol(malformed)

    def test_impossible_date_is_rejected(self):
        malformed = "QQQ   281332C00120000"  # month 13
        with pytest.raises(ValueError):
            parse_occ_symbol(malformed)

    def test_non_numeric_strike_is_rejected(self):
        malformed = "QQQ   280121CXXXXXXXX"
        with pytest.raises(ValueError):
            parse_occ_symbol(malformed)

    def test_lowercase_root_is_rejected(self):
        malformed = format_occ_symbol("QQQ", date(2027, 1, 1), "C", Decimal("10")).replace("QQQ", "qqq")
        with pytest.raises(ValueError):
            parse_occ_symbol(malformed)


class TestIsOccSymbolShape:
    def test_true_for_21_chars(self):
        assert is_occ_symbol_shape("Q" * 21) is True

    def test_false_for_a_plain_ticker(self):
        assert is_occ_symbol_shape("QQQ") is False
