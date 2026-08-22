from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from src.config import get_settings
from src.models.orders import KillSwitchState

_engine = None


def _build_engine(database_url: str):
    if database_url.startswith("sqlite"):
        connect_args = {"check_same_thread": False}
        if database_url.endswith(":memory:") or database_url == "sqlite://":
            return create_engine(
                database_url,
                connect_args=connect_args,
                poolclass=StaticPool,
                echo=False,
            )
        return create_engine(database_url, connect_args=connect_args, echo=False)
    return create_engine(database_url, echo=False)


def configure_engine(database_url: str | None = None):
    global _engine
    _engine = _build_engine(database_url or get_settings().database_url)
    return _engine


def get_engine():
    global _engine
    if _engine is None:
        _engine = _build_engine(get_settings().database_url)
    return _engine


def init_db() -> None:
    engine = get_engine()
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        state = session.exec(select(KillSwitchState).where(KillSwitchState.id == 1)).first()
        if state is None:
            session.add(KillSwitchState(id=1, is_on=False))
            session.commit()


@contextmanager
def session_scope() -> Iterator[Session]:
    session = Session(get_engine())
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Iterator[Session]:
    with Session(get_engine()) as session:
        yield session
