from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from src.execution.silo_runtime import ProbabilityBands, SiloMandate, SiloRuntime


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
