from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from src.execution.silo_runtime import ProbabilityBands, SiloMandate, SiloRuntime, compute_mandate_hmac


def mandate():
    return SiloMandate(
        silo_id="test-silo",
        armed_by="pytest",
        runtime_end="23:59",
        trades_per_minute=2,
        max_per_trade_usd=Decimal("5000"),
        max_concentration_pct=Decimal("25"),
        max_drawdown_pct=Decimal("5"),
        authorized_symbols=["QQQ"],
        authorized_strategy_ids=["golden_cross"],
        probability_weights={"eig": 0.6, "liquidity": 0.4},
        decision_bands=ProbabilityBands(execute_above=0.7, resize_above=0.5),
    )


def test_arm_and_probability_gate(tmp_path: Path):
    runtime=SiloRuntime(str(tmp_path/'state.json'))
    runtime.arm(mandate())
    result=runtime.evaluate_candidate(symbol='QQQ',strategy_id='golden_cross',estimated_notional_usd=Decimal('1000'),evidence={'eig':.8,'liquidity':.8})
    assert result.allowed is True
    assert result.action == 'EXECUTE'


def test_rate_budget(tmp_path: Path):
    runtime=SiloRuntime(str(tmp_path/'state.json'))
    runtime.arm(mandate())
    runtime.record_trade(datetime.utcnow()); runtime.record_trade(datetime.utcnow())
    result=runtime.evaluate_candidate(symbol='QQQ',strategy_id='golden_cross',estimated_notional_usd=Decimal('1000'),evidence={'eig':.9,'liquidity':.9})
    assert result.allowed is False
    assert result.action == 'DEFER'


def test_probability_gate_uses_only_the_evidence_keys_it_is_given(tmp_path: Path):
    """
    A mandate can weight factors (eig/regime/execution) this codebase has
    no real data source for yet — see src/execution/silo_evidence.py.
    evaluate_candidate() must re-normalize over only the supplied keys
    rather than treating a missing factor as 0, so a mandate weighting
    eig=0.6/liquidity=0.4 can still EXECUTE on liquidity evidence alone.
    """
    runtime = SiloRuntime(str(tmp_path / 'state.json'))
    runtime.arm(mandate())
    # mandate() weights eig=0.6, liquidity=0.4, execute_above=0.7 -- supplying
    # ONLY liquidity=0.9 (no eig) must score 0.9 (100% of the supplied weight
    # mass), not 0.36 (as if the missing eig silently counted as 0).
    result = runtime.evaluate_candidate(
        symbol='QQQ', strategy_id='golden_cross',
        estimated_notional_usd=Decimal('1000'), evidence={'liquidity': 0.9},
    )
    assert result.allowed is True
    assert result.action == 'EXECUTE'
    assert abs(result.probability_score - 0.9) < 1e-9


def test_concentration_over_mandate_defers_as_resize(tmp_path: Path):
    runtime = SiloRuntime(str(tmp_path / 'state.json'))
    runtime.arm(mandate())  # max_concentration_pct=25
    result = runtime.evaluate_candidate(
        symbol='QQQ', strategy_id='golden_cross', estimated_notional_usd=Decimal('1000'),
        concentration_pct=Decimal('30'), evidence={'eig': .9, 'liquidity': .9},
    )
    assert result.allowed is False
    assert result.action == 'RESIZE'


def test_drawdown_at_mandate_limit_defers(tmp_path: Path):
    runtime = SiloRuntime(str(tmp_path / 'state.json'))
    runtime.arm(mandate())  # max_drawdown_pct=5
    result = runtime.evaluate_candidate(
        symbol='QQQ', strategy_id='golden_cross', estimated_notional_usd=Decimal('1000'),
        drawdown_pct=Decimal('6'), evidence={'eig': .9, 'liquidity': .9},
    )
    assert result.allowed is False
    assert result.action == 'DEFER'


def test_arm_stamps_a_timezone_aware_runtime_start(tmp_path: Path):
    runtime = SiloRuntime(str(tmp_path / 'state.json'))
    armed = runtime.arm(mandate())
    assert armed.runtime_start.tzinfo is not None


def test_a_naive_runtime_start_is_read_as_utc_not_as_the_mandate_timezone(tmp_path: Path):
    """
    Regression test: arm() stamps runtime_start as a UTC clock reading.
    active_mandate() must interpret a naive runtime_start as UTC before
    converting to the mandate's own timezone. Treating it as already
    being in that timezone instead silently blocked every mandate from
    reporting active for hours after arming whenever the mandate's
    timezone trails UTC (e.g. America/New_York, UTC-4/-5): a naive 00:30
    reading is really 00:30 UTC, i.e. 20:30 the previous day in New York
    -- long past, not hours in the future.
    """
    runtime = SiloRuntime(str(tmp_path / 'state.json'))
    m = mandate()
    m.runtime_start = datetime(2026, 9, 28, 0, 30, 0)  # naive UTC reading
    runtime.arm(m)
    now_ny = datetime(2026, 9, 27, 20, 35, 0, tzinfo=ZoneInfo('America/New_York'))
    assert runtime.active_mandate(now=now_ny) is not None


def test_concentration_and_drawdown_within_mandate_still_allow_execution(tmp_path: Path):
    """Guards against a fix that accidentally makes these checks fail-closed even when comfortably inside the mandate."""
    runtime = SiloRuntime(str(tmp_path / 'state.json'))
    runtime.arm(mandate())  # max_concentration_pct=25, max_drawdown_pct=5
    result = runtime.evaluate_candidate(
        symbol='QQQ', strategy_id='golden_cross', estimated_notional_usd=Decimal('1000'),
        concentration_pct=Decimal('10'), drawdown_pct=Decimal('1'), evidence={'eig': .9, 'liquidity': .9},
    )
    assert result.allowed is True
    assert result.action == 'EXECUTE'


def _live_mandate(**overrides):
    defaults = dict(
        silo_id="test-live-silo", armed_by="pytest", mode="AUTONOMOUS_LIVE",
        trades_per_minute=1, max_per_trade_usd=Decimal("100"), max_concentration_pct=Decimal("10"),
        authorized_symbols=["QQQ"], authorized_strategy_ids=["golden_cross"],
        decision_bands=ProbabilityBands(execute_above=0.7, resize_above=0.5),
        broker="robinhood", account_alias="robinhood_live", live_execution_authorized=True,
        approved_algos=["algo-1"],
    )
    defaults.update(overrides)
    return SiloMandate(**defaults)


class TestAutonomousLiveMandateValidator:
    """
    SiloMandate's model_validator refuses to even CONSTRUCT an
    AUTONOMOUS_LIVE mandate missing any of its required conditions --
    a live-shaped mandate object either satisfies every one of them or
    doesn't exist at all. AUTONOMOUS_PAPER (the default) needs none of
    these fields.
    """

    def test_a_fully_specified_live_mandate_constructs_cleanly(self):
        _live_mandate()  # must not raise

    def test_autonomous_paper_needs_none_of_the_live_only_fields(self):
        mandate()  # the plain AUTONOMOUS_PAPER helper from above -- must not raise

    def test_rejects_a_non_robinhood_broker(self):
        with pytest.raises(ValidationError, match="broker='robinhood'"):
            _live_mandate(broker=None)

    def test_rejects_a_missing_account_alias(self):
        with pytest.raises(ValidationError, match="account_alias"):
            _live_mandate(account_alias=None)

    def test_rejects_live_execution_authorized_false(self):
        with pytest.raises(ValidationError, match="live_execution_authorized"):
            _live_mandate(live_execution_authorized=False)

    def test_rejects_empty_authorized_symbols(self):
        with pytest.raises(ValidationError, match="authorized_symbols"):
            _live_mandate(authorized_symbols=[])

    def test_rejects_empty_authorized_strategy_ids(self):
        with pytest.raises(ValidationError, match="authorized_strategy_ids"):
            _live_mandate(authorized_strategy_ids=[])

    def test_rejects_no_approved_trade_cards_or_algos(self):
        with pytest.raises(ValidationError, match="approved_trade_cards or approved_algos"):
            _live_mandate(approved_algos=[])

    def test_approved_trade_cards_alone_is_sufficient_without_algos(self):
        _live_mandate(approved_algos=[], approved_trade_cards=["card-1"])  # must not raise


class TestMandateHmac:
    """
    An AUTONOMOUS_LIVE mandate's mandate_hmac must be keyed on a secret
    (settings.api_key_admin) rather than a plain public-algorithm
    checksum -- see compute_mandate_hmac's own docstring for why a plain
    hash is not tamper-resistant.
    """

    def test_arming_a_live_mandate_without_an_admin_key_is_refused(self, tmp_path: Path):
        runtime = SiloRuntime(str(tmp_path / 'state.json'))
        with pytest.raises(ValueError, match="admin key"):
            runtime.arm(_live_mandate())

    def test_arming_a_live_mandate_stamps_a_verifiable_hmac(self, tmp_path: Path):
        runtime = SiloRuntime(str(tmp_path / 'state.json'))
        armed = runtime.arm(_live_mandate(), admin_key="test-admin-key")
        assert armed.mandate_hmac
        active = runtime.active_mandate(admin_key="test-admin-key")
        assert active is not None
        assert active.mode == "AUTONOMOUS_LIVE"

    def test_a_live_mandate_is_inactive_without_the_right_admin_key(self, tmp_path: Path):
        runtime = SiloRuntime(str(tmp_path / 'state.json'))
        runtime.arm(_live_mandate(), admin_key="test-admin-key")
        assert runtime.active_mandate() is None  # no admin_key at all
        assert runtime.active_mandate(admin_key="wrong-key") is None

    def test_a_hand_edited_live_mandate_field_is_treated_as_inactive(self, tmp_path: Path):
        """
        Simulates a tamperer with filesystem write access editing the
        state file directly (e.g. widening authorized_symbols) without
        knowing the admin key needed to recompute a matching HMAC.
        """
        state_path = tmp_path / 'state.json'
        runtime = SiloRuntime(str(state_path))
        runtime.arm(_live_mandate(), admin_key="test-admin-key")

        raw = state_path.read_text()
        tampered = raw.replace('"QQQ"', '"SPY"')  # widen authorized_symbols without recomputing the hmac
        assert tampered != raw
        state_path.write_text(tampered)

        assert runtime.active_mandate(admin_key="test-admin-key") is None

    def test_compute_mandate_hmac_differs_for_different_secrets(self):
        m = _live_mandate()
        assert compute_mandate_hmac(m, "secret-one") != compute_mandate_hmac(m, "secret-two")

    def test_an_autonomous_paper_mandate_needs_no_admin_key_to_arm_or_activate(self, tmp_path: Path):
        runtime = SiloRuntime(str(tmp_path / 'state.json'))
        runtime.arm(mandate())  # no admin_key -- must not raise
        assert runtime.active_mandate() is not None  # no admin_key needed to read it back either
