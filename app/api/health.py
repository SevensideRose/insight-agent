"""Health and public configuration endpoints."""
from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import text

from app.config import settings
from app.db import SessionLocal
from app.services.embedding import embedding_service
from app.services.milvus_store import milvus_store

router = APIRouter(prefix="/api", tags=["health"])


@router.get("/health")
def health() -> dict:
    db_status: dict = {"ok": True}
    try:
        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        db_status = {"ok": False, "error": str(exc)}

    return {
        "app": "ok",
        "name": settings.app_name,
        "database": db_status,
        "milvus": milvus_store.health(),
        "embedding": embedding_service.health(),
        "deepseek": {"configured": bool(settings.deepseek_api_key)},
        "siliconflow": {"configured": bool(settings.siliconflow_api_key)},
        "mem0": {"configured": bool(settings.mem0_api_key)},
    }


@router.get("/settings/public")
def public_settings() -> dict:
    return settings.public_config()
