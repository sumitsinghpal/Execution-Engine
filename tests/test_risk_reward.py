"""Tests for src/execution/risk_reward.py."""

from decimal import Decimal

import pytest

from src.execution.risk_reward import compute_standardized_exit, size_position


class TestComputeStandardizedExit:
    def test_one_to_two_ratio_at_one_percent_risk(self):
        exit_levels = compute_standardized_exit(100.0, risk_pct=Decimal("0.01"), reward_risk_ratio=Decimal("2"))

        assert exit_levels.risk_distance == pytest.approx(1.0)
        assert exit_levels.stop_loss_price == pytest.approx(99.0)
        assert exit_levels.take_profit_price == pytest.approx(102.0)

    def test_ratio_scales_with_reward_risk_ratio(self):
        exit_levels = compute_standardized_exit(200.0, risk_pct=Decimal("0.02"), reward_risk_ratio=Decimal("3"))

        # risk_distance = 200 * 0.02 = 4.0
        assert exit_levels.stop_loss_price == pytest.approx(196.0)
        assert exit_levels.take_profit_price == pytest.approx(212.0)  # 200 + 3*4

    def test_rejects_non_positive_entry_price(self):
        with pytest.raises(ValueError):
            compute_standardized_exit(0.0, risk_pct=Decimal("0.01"), reward_risk_ratio=Decimal("2"))
        with pytest.raises(ValueError):
            compute_standardized_exit(-10.0, risk_pct=Decimal("0.01"), reward_risk_ratio=Decimal("2"))

    def test_rejects_non_positive_risk_pct(self):
        with pytest.raises(ValueError):
            compute_standardized_exit(100.0, risk_pct=Decimal("0"), reward_risk_ratio=Decimal("2"))

    def test_rejects_non_positive_reward_risk_ratio(self):
        with pytest.raises(ValueError):
            compute_standardized_exit(100.0, risk_pct=Decimal("0.01"), reward_risk_ratio=Decimal("0"))


class TestSizePosition:
    def test_fits_whole_shares_only(self):
        assert size_position(Decimal("1000"), 333.0) == 3  # 999 fits, 1332 doesn't

    def test_exact_fit(self):
        assert size_position(Decimal("1000"), 100.0) == 10

    def test_too_expensive_for_even_one_share_returns_zero(self):
        assert size_position(Decimal("100"), 500.0) == 0

    def test_non_positive_price_returns_zero(self):
        assert size_position(Decimal("1000"), 0.0) == 0
        assert size_position(Decimal("1000"), -50.0) == 0
