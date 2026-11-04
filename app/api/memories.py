"""Long-term memory browsing, search and deletion."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db, session_scope
from app.core.ids import new_id
from app.models import Memory, MemoryEvent, utcnow
from app.schemas import MemoryOut, MemorySearchRequest, MemoryUndoRequest
from app.services.memory import MemoryServiceError, memory_service
from app.services.memory_worker import memory_worker

router = APIRouter(prefix="/api/memories", tags=["memories"])


def _memory_out(memory: Memory) -> MemoryOut:
    return MemoryOut(
        id=memory.id,
        mem0_id=memory.mem0_id,
        category=memory.category,
        content=memory.content,
        confidence=memory.confidence,
        stability=memory.stability,
        status=memory.status,
        source_session_id=memory.source_session_id,
        source_message_id=memory.source_message_id,
        created_at=memory.created_at,
        updated_at=memory.updated_at,
    )


@router.get("", response_model=list[MemoryOut])
def list_memories(
    category: str | None = None,
    q: str | None = None,
    status: str = "active",
    limit: int = Query(default=200, ge=1, le=500),
    db: Session = Depends(get_db),
) -> list[MemoryOut]:
    query = select(Memory)
    if status and status != "all":
        query = query.where(Memory.status == status)
    if category and category != "all":
        query = query.where(Memory.category == category)
    if q:
        query = query.where(Memory.content.contains(q))
    rows = db.scalars(query.order_by(Memory.updated_at.desc()).limit(limit)).all()
    return [_memory_out(row) for row in rows]


@router.post("/search")
def search_memories(payload: MemorySearchRequest, db: Session = Depends(get_db)) -> list[dict]:
    try:
        results = memory_service.search(payload.query, payload.limit)
    except MemoryServiceError as exc:
        raise HTTPException(502, str(exc)) from exc
    output = []
    for item in results:
        mem0_id = item.get("mem0_id")
        mirror = None
        if mem0_id:
            mirror = db.scalars(select(Memory).where(Memory.mem0_id == str(mem0_id))).first()
        output.append(
            {
                "mem0_id": mem0_id,
                "local_id": mirror.id if mirror else None,
                "content": item.get("content", ""),
                "category": mirror.category if mirror else item.get("category", "other"),
                "score": item.get("score", 0.0),
                "status": mirror.status if mirror else "unknown",
            }
        )
    return output


@router.get("/recent")
def recent_events(
    session_id: str,
    since: str | None = None,
) -> list[dict]:
    return memory_worker.recent_events(session_id, since)


@router.post("/undo")
def undo_events(payload: MemoryUndoRequest) -> dict:
    undone = 0
    for event_id in payload.event_ids:
        with session_scope() as db:
            event = db.get(MemoryEvent, event_id)
            if event is None or event.action != "ADD" or not event.mem0_id:
                continue
            mem0_id = event.mem0_id
            try:
                memory_service.delete(mem0_id)
            except MemoryServiceError as exc:
                raise HTTPException(502, str(exc)) from exc
            mirror = db.scalars(select(Memory).where(Memory.mem0_id == mem0_id)).first()
            if mirror is not None:
                mirror.status = "deleted"
                mirror.updated_at = utcnow()
            db.add(
                MemoryEvent(
                    id=new_id(),
                    session_id=event.session_id,
                    message_id=event.message_id,
                    action="UNDO",
                    mem0_id=mem0_id,
                    payload_json=json.dumps({"undone_event": event_id}, ensure_ascii=False),
                )
            )
            undone += 1
    return {"undone": undone}


@router.delete("/{memory_id}", status_code=204)
def delete_memory(memory_id: str, db: Session = Depends(get_db)) -> None:
    memory = db.get(Memory, memory_id)
    if memory is None:
        raise HTTPException(404, "记忆不存在")
    if memory.mem0_id:
        try:
            memory_service.delete(memory.mem0_id)
        except MemoryServiceError as exc:
            raise HTTPException(502, str(exc)) from exc
    memory.status = "deleted"
    memory.updated_at = utcnow()
    db.add(
        MemoryEvent(
            id=new_id(),
            session_id=memory.source_session_id,
            message_id=memory.source_message_id,
            action="DELETE",
            mem0_id=memory.mem0_id,
            payload_json=json.dumps({"source": "manual"}, ensure_ascii=False),
        )
    )
    db.commit()
