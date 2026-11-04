"""Explicit service checks (user-triggered)."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter
from sqlalchemy import text

from app.db import SessionLocal
from app.services.embedding import embedding_service
from app.services.llm import llm_service
from app.services.memory import memory_service
from app.services.milvus_store import milvus_store
from app.services.rerank import rerank_service

router = APIRouter(prefix="/api/settings", tags=["settings"])


def _db_check() -> dict:
    try:
        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


@router.post("/check")
async def check_services() -> dict:
    return {
        "database": await asyncio.to_thread(_db_check),
        "milvus": await asyncio.to_thread(milvus_store.health),
        "embedding": embedding_service.health(),
        "deepseek": await asyncio.to_thread(llm_service.check),
        "siliconflow": await asyncio.to_thread(rerank_service.check),
        "mem0": await asyncio.to_thread(memory_service.check),
    }


@router.post("/init-milvus")
def init_milvus(recreate: bool = False) -> dict:
    try:
        milvus_store.ensure_collection(recreate=recreate)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    return milvus_store.health()
