"""Sessions and streaming chat."""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.core.ids import new_id
from app.core.logging import redact
from app.core.sse import sse_event
from app.core.text import truncate
from app.db import get_db, session_scope
from app.models import Message, Session as ChatSession, utcnow
from app.schemas import (
    ChatRequest,
    MessageOut,
    SessionCreate,
    SessionOut,
    SessionUpdate,
)
from app.services.chat_graph import chat_graph
from app.services.memory_worker import memory_worker

router = APIRouter(prefix="/api", tags=["chat"])


def _parse_list(value: str | None) -> list[str]:
    try:
        parsed = json.loads(value or "[]")
        return [str(item) for item in parsed] if isinstance(parsed, list) else []
    except json.JSONDecodeError:
        return []


def _session_out(db: Session, session: ChatSession) -> SessionOut:
    count = db.scalar(
        select(func.count(Message.id)).where(Message.session_id == session.id)
    ) or 0
    return SessionOut(
        id=session.id,
        title=session.title,
        kb_ids=_parse_list(session.kb_ids_json),
        rag_enabled=bool(session.rag_enabled),
        memory_enabled=bool(session.memory_enabled),
        created_at=session.created_at,
        updated_at=session.updated_at,
        ended_at=session.ended_at,
        message_count=int(count),
    )


def _message_out(message: Message) -> MessageOut:
    try:
        sources = json.loads(message.sources_json or "[]")
    except json.JSONDecodeError:
        sources = []
    try:
        usage = json.loads(message.usage_json or "{}")
    except json.JSONDecodeError:
        usage = {}
    return MessageOut(
        id=message.id,
        session_id=message.session_id,
        role=message.role,
        content=message.content,
        status=message.status,
        sources=sources if isinstance(sources, list) else [],
        usage=usage if isinstance(usage, dict) else {},
        created_at=message.created_at,
    )


@router.get("/sessions", response_model=list[SessionOut])
def list_sessions(db: Session = Depends(get_db)) -> list[SessionOut]:
    sessions = db.scalars(
        select(ChatSession).order_by(ChatSession.updated_at.desc()).limit(100)
    ).all()
    return [_session_out(db, session) for session in sessions]


@router.post("/sessions", response_model=SessionOut, status_code=201)
def create_session(payload: SessionCreate, db: Session = Depends(get_db)) -> SessionOut:
    session = ChatSession(
        id=new_id(),
        title=payload.title.strip() or "新对话",
        kb_ids_json=json.dumps(payload.kb_ids, ensure_ascii=False),
        rag_enabled=1 if payload.rag_enabled else 0,
        memory_enabled=1 if payload.memory_enabled else 0,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return _session_out(db, session)


@router.patch("/sessions/{session_id}", response_model=SessionOut)
def update_session(
    session_id: str, payload: SessionUpdate, db: Session = Depends(get_db)
) -> SessionOut:
    session = db.get(ChatSession, session_id)
    if session is None:
        raise HTTPException(404, "会话不存在")
    if payload.title is not None:
        session.title = payload.title.strip() or session.title
    if payload.kb_ids is not None:
        session.kb_ids_json = json.dumps(payload.kb_ids, ensure_ascii=False)
    if payload.rag_enabled is not None:
        session.rag_enabled = 1 if payload.rag_enabled else 0
    if payload.memory_enabled is not None:
        session.memory_enabled = 1 if payload.memory_enabled else 0
    session.updated_at = utcnow()
    db.commit()
    db.refresh(session)
    return _session_out(db, session)


@router.delete("/sessions/{session_id}", status_code=204)
def delete_session(session_id: str, db: Session = Depends(get_db)) -> None:
    session = db.get(ChatSession, session_id)
    if session is None:
        raise HTTPException(404, "会话不存在")
    db.delete(session)
    db.commit()


@router.get("/sessions/{session_id}/messages", response_model=list[MessageOut])
def list_messages(
    session_id: str, limit: int = 200, db: Session = Depends(get_db)
) -> list[MessageOut]:
    if db.get(ChatSession, session_id) is None:
        raise HTTPException(404, "会话不存在")
    messages = db.scalars(
        select(Message)
        .where(Message.session_id == session_id)
        .order_by(Message.created_at.asc())
        .limit(max(1, min(limit, 500)))
    ).all()
    return [_message_out(message) for message in messages]


@router.post("/sessions/{session_id}/end")
async def end_session(session_id: str, db: Session = Depends(get_db)) -> dict:
    session = db.get(ChatSession, session_id)
    if session is None:
        raise HTTPException(404, "会话不存在")
    session.ended_at = utcnow()
    session.updated_at = utcnow()
    db.commit()
    queued = await asyncio.to_thread(memory_worker.sweep_session, session_id)
    return {"queued": queued, "ended_at": session.ended_at}


def _finalize_message(
    message_id: str,
    content: str,
    status: str,
    sources: list[dict] | None = None,
    usage: dict | None = None,
) -> None:
    try:
        with session_scope() as db:
            message = db.get(Message, message_id)
            if message is None or message.status != "streaming":
                return
            message.content = content
            message.status = status
            message.sources_json = json.dumps(sources or [], ensure_ascii=False)
            message.usage_json = json.dumps(usage or {}, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        pass


@router.post("/chat/stream")
async def chat_stream(payload: ChatRequest) -> StreamingResponse:
    if not settings.deepseek_api_key:
        raise HTTPException(400, "未配置 DEEPSEEK_API_KEY")
    question = payload.message.strip()
    if not question:
        raise HTTPException(422, "消息不能为空")

    with session_scope() as db:
        if payload.session_id:
            session = db.get(ChatSession, payload.session_id)
            if session is None:
                raise HTTPException(404, "会话不存在")
        else:
            session = ChatSession(id=new_id(), title="新对话")
            db.add(session)
            db.flush()
        if payload.kb_ids is not None:
            session.kb_ids_json = json.dumps(payload.kb_ids, ensure_ascii=False)
        if payload.rag_enabled is not None:
            session.rag_enabled = 1 if payload.rag_enabled else 0
        if payload.memory_enabled is not None:
            session.memory_enabled = 1 if payload.memory_enabled else 0
        if session.title == "新对话":
            session.title = truncate(question, 30)
        session.updated_at = utcnow()
        session_id = session.id
        kb_ids = _parse_list(session.kb_ids_json)
        rag_enabled = bool(session.rag_enabled)
        memory_enabled = bool(session.memory_enabled)

        user_message = Message(
            id=new_id(),
            session_id=session_id,
            role="user",
            content=question,
            status="complete",
        )
        assistant_message = Message(
            id=new_id(),
            session_id=session_id,
            role="assistant",
            content="",
            status="streaming",
        )
        db.add(user_message)
        db.add(assistant_message)
        db.flush()
        user_message_id = user_message.id
        assistant_message_id = assistant_message.id

    state = {
        "session_id": session_id,
        "user_message_id": user_message_id,
        "assistant_message_id": assistant_message_id,
        "question": question,
        "kb_ids": kb_ids,
        "rag_enabled": rag_enabled,
        "memory_enabled": memory_enabled,
        "retrieval_mode": payload.retrieval_mode,
        "rerank": payload.rerank,
        "parent_expand": True if payload.parent_expand is None else payload.parent_expand,
        "warnings": [],
        "holder": {"text": "", "usage": {}},
    }
    started = time.monotonic()

    async def event_stream():
        yield sse_event(
            "meta",
            {
                "session_id": session_id,
                "user_message_id": user_message_id,
                "assistant_message_id": assistant_message_id,
            },
        )
        try:
            async for item in chat_graph.astream(state):
                kind = str(item.get("type", "message"))
                if kind == "token":
                    yield sse_event("token", {"text": item.get("text", "")})
                elif kind == "sources":
                    yield sse_event("sources", {"sources": item.get("sources", [])})
                elif kind == "memory":
                    yield sse_event(
                        "memory",
                        {
                            "count": item.get("count", 0),
                            "items": item.get("items", []),
                        },
                    )
                elif kind == "warning":
                    yield sse_event("warning", {"message": item.get("message", "")})
                else:
                    yield sse_event("message", item)
            holder = state.get("holder", {})
            yield sse_event(
                "done",
                {
                    "session_id": session_id,
                    "message_id": assistant_message_id,
                    "usage": holder.get("usage", {}),
                    "elapsed_ms": int((time.monotonic() - started) * 1000),
                },
            )
        except asyncio.CancelledError:
            holder = state.get("holder", {})
            _finalize_message(
                assistant_message_id,
                holder.get("text", ""),
                "cancelled",
                state.get("sources", []),
                holder.get("usage", {}),
            )
            raise
        except Exception as exc:  # noqa: BLE001
            holder = state.get("holder", {})
            message = redact(str(exc))
            _finalize_message(
                assistant_message_id,
                holder.get("text", "") or f"生成失败：{message}",
                "error",
                state.get("sources", []),
                holder.get("usage", {}),
            )
            yield sse_event("error", {"message": message})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
