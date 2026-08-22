"""
Shared test fixtures and configuration.
"""

import os
import tempfile
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import Session, SQLModel

# Import all models to register them with SQLModel.metadata
from src.execution.idempotency import IdempotencyRecord
from src.execution.approval import ApprovalRecord
from src.execution.executor import OrderRecord
from src.execution.reconciliation import ReconciliationEvent
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
        database_url=f"sqlite:///{db_path}",
        schwab_app_key="test-key",
        schwab_app_secret="test-secret",
        schwab_refresh_token="test-token",
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
def app_with_test_db(test_db_engine_and_session):
    """Create app with test database."""
    from src.api.server import app, get_db
    
    engine, test_session = test_db_engine_and_session
    
    def get_test_db_session():
        """Test database dependency."""
        try:
            yield test_session
        finally:
            pass
    
    # Override the get_db dependency
    app.dependency_overrides[get_db] = get_test_db_session
    
    yield app
    
    app.dependency_overrides.clear()


@pytest.fixture
def sample_trade_proposal():
    """Create a sample trade proposal."""
    return TradeProposal(
        decision_id="edge-20260821-001",
        account="primary",
        symbol="QQQ",
        asset_type=AssetType.ETF,
        instruction=Instruction.BUY,
        quantity=10,
        order_type=OrderType.LIMIT,
        limit_price=Decimal("721.50"),
    )


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
