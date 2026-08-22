from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from src.config import get_settings
from src.db import configure_engine, init_db


@pytest.fixture
def client(tmp_path):
    db_file = tmp_path / "test.db"
    os.environ["DATABASE_URL"] = f"sqlite:///{db_file}"
    os.environ["SCHWAB_MOCK_MODE"] = "true"
    os.environ["ADMIN_API_KEY"] = "test-admin-key"
    os.environ["ALLOWED_SYMBOLS"] = "QQQ,SPY"
    get_settings.cache_clear()
    configure_engine(os.environ["DATABASE_URL"])
    init_db()

    from src.api.server import app

    with TestClient(app) as c:
        yield c
