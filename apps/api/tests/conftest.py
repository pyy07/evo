import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# Ensure mock market data and isolated sqlite DB before app import
ROOT = Path(__file__).resolve().parents[3]
os.environ["MARKET_DATA_MODE"] = "mock"
os.environ["ADMIN_TOKEN"] = "test-admin"
os.environ["DATABASE_URL"] = "sqlite://"
os.environ["INSTRUMENTS_CONFIG"] = str(ROOT / "config" / "instruments.yaml")

from evo_api.db.base import Base  # noqa: E402
from evo_api.db.session import get_db  # noqa: E402
from evo_api.main import create_app  # noqa: E402
from evo_api.services.bootstrap import seed_capabilities  # noqa: E402


@pytest.fixture()
def client():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    Base.metadata.create_all(bind=engine)
    db = TestingSessionLocal()
    seed_capabilities(db)
    db.close()

    def _override():
        session = TestingSessionLocal()
        try:
            yield session
        finally:
            session.close()

    app = create_app()
    app.dependency_overrides[get_db] = _override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()