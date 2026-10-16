from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from evo_api.config import get_settings

settings = get_settings()
connect_args: dict = {}
if settings.database_url.startswith("sqlite"):
    connect_args = {"check_same_thread": False}
elif settings.database_url.startswith("postgresql"):
    # 避免远端 PG 卡住时 uvicorn lifespan 无限挂起（表现为 8000 起不来 / 前端 ECONNREFUSED）
    connect_args = {"connect_timeout": 8}
engine = create_engine(
    settings.database_url,
    future=True,
    connect_args=connect_args,
    pool_pre_ping=True,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()