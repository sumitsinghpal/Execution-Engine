"""
Tests for src/execution/silo_evidence.py — the honest, narrow evidence
signals wired into HedgeHog Silo's probabilistic gate. See that module's
docstring for why eig/regime/execution are deliberately never fabricated
here; these tests exist to prove liquidity/freshness/concentration are
each computed correctly and fail closed (return None, not a fake 0/1)
whenever the input data can't actually support them.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from src.execution.silo_evidence import (
    compute_evidence,
    concentration_pct_of_equity,
    freshness_score,
    liquidity_score,
)


def _quote(bid=100.0, ask=100.0, last=100.0, quote_time=None):
    return {
        "bid": bid, "ask": ask, "last": last,
        "quote_time": quote_time or datetime.now(timezone.utc).isoformat(),
    }


class TestLiquidityScore:
    def test_zero_spread_scores_perfect_liquidity(self):
        assert liquidity_score(_quote(bid=100.0, ask=100.0, last=100.0)) == 1.0

    def test_a_wide_spread_scores_low_liquidity(self):
        # 3% spread on a floor of 2% -> clamped to 0.0, not negative
        score = liquidity_score(_quote(bid=98.5, ask=101.5, last=100.0))
        assert score == 0.0

    def test_a_narrow_but_nonzero_spread_scores_between_zero_and_one(self):
        # 1% spread against a 2% floor -> 0.5
        score = liquidity_score(_quote(bid=99.5, ask=100.5, last=100.0))
        assert abs(score - 0.5) < 1e-9

    def test_missing_bid_returns_none_not_a_fabricated_score(self):
        q = _quote()
        del q["bid"]
        assert liquidity_score(q) is None

    def test_zero_last_price_returns_none_rather_than_dividing_by_zero(self):
        assert liquidity_score(_quote(last=0.0)) is None


class TestFreshnessScore:
    def test_a_quote_from_right_now_scores_perfectly_fresh(self):
        score = freshness_score(_quote(quote_time=datetime.now(timezone.utc).isoformat()), max_age_seconds=10)
        assert score > 0.99

    def test_a_quote_at_exactly_the_staleness_limit_scores_zero(self):
        stale = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
        score = freshness_score(_quote(quote_time=stale), max_age_seconds=10)
        assert score == 0.0

    def test_a_quote_older_than_the_limit_clamps_to_zero_not_negative(self):
        very_stale = (datetime.now(timezone.utc) - timedelta(seconds=1000)).isoformat()
        score = freshness_score(_quote(quote_time=very_stale), max_age_seconds=10)
        assert score == 0.0

    def test_missing_quote_time_returns_none(self):
        q = _quote()
        del q["quote_time"]
        assert freshness_score(q, max_age_seconds=10) is None

    def test_unparseable_quote_time_returns_none_rather_than_raising(self):
        assert freshness_score(_quote(quote_time="not-a-timestamp"), max_age_seconds=10) is None


class TestComputeEvidence:
    def test_only_includes_factors_it_can_honestly_compute(self):
        evidence = compute_evidence(_quote(), max_quote_age_seconds=10)
        assert set(evidence) == {"liquidity", "freshness"}
        assert "eig" not in evidence and "regime" not in evidence and "execution" not in evidence

    def test_a_quote_missing_everything_produces_empty_evidence_not_fabricated_values(self):
        assert compute_evidence({}, max_quote_age_seconds=10) == {}


class TestConcentrationPctOfEquity:
    def test_computes_candidate_notional_as_a_percentage_of_equity(self):
        pct = concentration_pct_of_equity(Decimal("2500"), account_equity=50000.0)
        assert pct == Decimal("5")

    def test_missing_equity_returns_none_so_the_caller_can_fail_closed(self):
        assert concentration_pct_of_equity(Decimal("2500"), account_equity=None) is None

    def test_zero_equity_returns_none_rather_than_dividing_by_zero(self):
        assert concentration_pct_of_equity(Decimal("2500"), account_equity=0.0) is None
