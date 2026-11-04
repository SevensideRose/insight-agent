"""FastAPI application entry point."""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

from app.config import BASE_DIR, settings
from app.core.logging import setup_logging
from app.db import init_db

STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings.ensure_dirs()
    setup_logging(settings.log_path, settings.log_level)
    init_db()
    logger.info("{} starting on {}:{}", settings.app_name, settings.app_host, settings.app_port)

    # Start the per-turn memory worker and retry recent unprocessed turns.
    try:
        from app.services.memory_worker import memory_worker

        memory_worker.start()
        memory_worker.sweep_recent()
    except Exception:  # noqa: BLE001
        logger.exception("Memory worker failed to start; chat will continue without it")

    yield

    try:
        from app.services.memory_worker import memory_worker

        memory_worker.stop()
    except Exception:  # noqa: BLE001
        pass
    logger.info("{} stopped", settings.app_name)


def create_app() -> FastAPI:
    app = FastAPI(title=settings.app_name, lifespan=lifespan, docs_url="/api/docs", redoc_url=None)

    from app.api.health import router as health_router

    app.include_router(health_router)

    try:
        from app.api.kbs import router as kbs_router

        app.include_router(kbs_router)
    except ImportError:
        pass
    try:
        from app.api.documents import router as documents_router

        app.include_router(documents_router)
    except ImportError:
        pass
    try:
        from app.api.chat import router as chat_router

        app.include_router(chat_router)
    except ImportError:
        pass
    try:
        from app.api.memories import router as memories_router

        app.include_router(memories_router)
    except ImportError:
        pass
    try:
        from app.api.jobs import router as jobs_router

        app.include_router(jobs_router)
    except ImportError:
        pass
    try:
        from app.api.settings import router as settings_router

        app.include_router(settings_router)
    except ImportError:
        pass

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    return app


app = create_app()
