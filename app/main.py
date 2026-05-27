from time import perf_counter

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlmodel import Session

from app.api.main import api_router
from app.core.config import settings
from app.core.db import engine


def create_app() -> FastAPI:
    app = FastAPI(title=settings.PROJECT_NAME)

    if settings.all_cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.all_cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    app.include_router(api_router, prefix=settings.API_V1_STR)

    @app.get("/health", tags=["health"])
    def health_check() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/db", tags=["health"])
    def database_health_check() -> dict[str, float | str]:
        started_at = perf_counter()
        with Session(engine) as session:
            session.exec(text("SELECT 1")).one()

        return {
            "status": "ok",
            "db_ms": round((perf_counter() - started_at) * 1000, 2),
        }

    return app


app = create_app()
