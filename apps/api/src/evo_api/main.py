from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from evo_api.config import get_settings
from evo_api.db.session import SessionLocal
from evo_api.routers import admin, capabilities
from evo_api.services.bootstrap import init_db, seed_capabilities


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    db = SessionLocal()
    try:
        seed_capabilities(db)
    finally:
        db.close()
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Evo 自演进投资系统",
        description="投资决策闭环 · Capability 注册 · 纸面交易 · 人工监督演进",
        version="0.1.0",
        lifespan=lifespan,
    )
    origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins or ["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(capabilities.router)
    app.include_router(admin.router)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()