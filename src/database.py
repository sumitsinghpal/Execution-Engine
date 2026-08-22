"""
Database configuration and session management.
Uses SQLAlchemy with SQLModel for type-safe ORM.
"""

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel, Session

from src.config import get_settings

settings = get_settings()

# Create engine
engine = create_engine(
    settings.database_url,
    echo=(settings.env == "development"),
    connect_args={"check_same_thread": False} if "sqlite" in settings.database_url else {},
)

# Create session factory
SessionLocal = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)


def init_db() -> None:
    """Initialize database schema."""
    SQLModel.metadata.create_all(engine)


def get_session() -> Session:
    """Get database session."""
    return SessionLocal()
