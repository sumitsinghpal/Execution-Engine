"""
Shared test fixtures and configuration.
"""

import asyncio
import os
import tempfile
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import Session, SQLModel

from src.brokers.paper import PaperBrokerAdapter

# Import all models to register them with SQLModel.metadata
from src.execution.algo_slices import AlgoSliceRecord
from src.execution.idempotency import IdempotencyRecord
from src.execution.approval import ApprovalRecord
from src.execution.drawdown_guard import DailyEquityBaseline
from src.execution.executor import OrderRecord
from src.execution.kill_switch_state import KillSwitchRecord
from src.execution.reconciliation import ReconciliationEvent
from src.execution.strategy_signals import StrategySignalRecord
from src.execution.external_signals import ExternalSignalRecord
from src.audit.ledger import AuditLedger

from src.config import Settings
from src.models.orders import AssetType, Instruction, OrderType, TradeProposal


@pytest.fixture(scope="session")
def test_settings():
    """Create test settings with SQLite file."""
    # Create a temporary database file
    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(db_fd)
    
    return Settings(
        _env_file=None,  # ignore any local .env so tests are hermetic
        database_url=f"sqlite:///{db_path}",
        schwab_app_key="test-key",
        schwab_app_secret="test-secret",
        schwab_refresh_token="test-token",
        api_key_admin="change-me-in-prod",
        env="test",
        kill_switch_enabled=False,
    )


@pytest.fixture
def test_db_engine_and_session(test_settings):
    """Create test database engine and session with initialized schema."""
    engine = create_engine(
        test_settings.database_url,
        connect_args={"check_same_thread": False},
    )
    
    # Create all tables
    SQLModel.metadata.create_all(engine)
    
    TestSessionLocal = sessionmaker(bind=engine, class_=Session)
    session = TestSessionLocal()
    
    yield engine, session
    
    session.close()
    engine.dispose()


@pytest.fixture
def test_db(test_db_engine_and_session):
    """Return test database session."""
    _, session = test_db_engine_and_session
    return session


@pytest.fixture
def app_with_test_db(test_db_engine_and_session, test_settings):
    """Create app with test database."""
    from src.api.server import app, get_db, get_settings_dep

    engine, test_session = test_db_engine_and_session

    def get_test_db_session():
        """Test database dependency."""
        try:
            yield test_session
        finally:
            pass

    # Override the get_db dependency
    app.dependency_overrides[get_db] = get_test_db_session
    # Override settings too, so tests don't silently pick up whatever the
    # developer's local .env happens to contain (e.g. a non-default admin
    # API key), which otherwise makes admin-key-gated tests fail/pass based
    # on machine-local state rather than the fixture's known test values.
    app.dependency_overrides[get_settings_dep] = lambda: test_settings

    yield app

    app.dependency_overrides.clear()


def _live_paper_quote(symbol: str) -> dict:
    """
    Fetches PaperBrokerAdapter's actual current synthetic quote for a
    symbol, synchronously, for use in fixtures. Deliberately NOT a
    hardcoded number: PaperBrokerAdapter's synthetic price moves over time
    (a slow sine-wave drift keyed to the wall-clock day — see
    _bucket_close in src/brokers/paper.py), and get_quote()/get_price_history()
    are the single source of truth for "QQQ's price right now" everywhere
    else in this codebase. A hardcoded fixture price drifts out of
    RiskChecker's 3% limit-price-deviation tolerance as real days pass —
    this fixture can't go stale the same way, since it asks the same
    adapter everything else asks.
    """
    return asyncio.run(PaperBrokerAdapter().get_quote(symbol))


@pytest.fixture
def sample_trade_proposal():
    """
    Create a sample trade proposal. limit_price is the live synthetic
    quote for QQQ (see _live_paper_quote above) — RiskChecker rejects a
    LIMIT price that deviates too far from the live quote as likely
    mispriced, and this fixture needs to actually pass that check to still
    mean "a valid order" everywhere it's used as one.
    """
    quote = _live_paper_quote("QQQ")
    return TradeProposal(
        decision_id="edge-20260821-001",
        account="primary",
        symbol="QQQ",
        asset_type=AssetType.ETF,
        instruction=Instruction.BUY,
        quantity=10,
        order_type=OrderType.LIMIT,
        limit_price=Decimal(str(quote["last"])),
    )


@pytest.fixture
def sample_quote():
    """
    PaperBrokerAdapter's actual current synthetic quote for QQQ — the same
    kind of dict RiskChecker.evaluate() expects from
    BrokerAdapter.get_quote(), for tests that call evaluate() directly
    without going through Executor's async quote-fetch.
    """
    quote = _live_paper_quote("QQQ")
    return {
        "symbol": "QQQ",
        "bid": Decimal(str(quote["bid"])),
        "ask": Decimal(str(quote["ask"])),
        "last": Decimal(str(quote["last"])),
        "quote_time": datetime.now(UTC).isoformat(),
        "mode": "PAPER",
    }


@pytest.fixture
def sample_market_order():
    """Create a sample market order."""
    return TradeProposal(
        decision_id="edge-20260821-002",
        account="primary",
        symbol="SPY",
        asset_type=AssetType.ETF,
        instruction=Instruction.SELL,
        quantity=5,
        order_type=OrderType.MARKET,
    )


@pytest.fixture
def sample_stop_order():
    """Create a sample stop order."""
    return TradeProposal(
        decision_id="edge-20260821-003",
        account="primary",
        symbol="IWM",
        asset_type=AssetType.ETF,
        instruction=Instruction.BUY,
        quantity=20,
        order_type=OrderType.STOP,
        stop_price=Decimal("200.00"),
    )
