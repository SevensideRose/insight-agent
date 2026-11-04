"""LangGraph chat orchestration with token streaming."""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, TypedDict

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal, session_scope
from app.models import Message
from app.prompts.chat import build_prompt_messages
from app.services.llm import LLMError, llm_service
from app.services.memory import MemoryServiceError, memory_service
from app.services.retrieval import RetrievedChunk, RetrievalResult, retrieval_service


class ChatState(TypedDict, total=False):
    session_id: str
    user_message_id: str
    assistant_message_id: str
    question: str
    kb_ids: list[str]
    rag_enabled: bool
    memory_enabled: bool
    retrieval_mode: str
    rerank: bool | None
    parent_expand: bool
    history: list[dict]
    memories: list[dict]
    retrieved: list[RetrievedChunk]
    sources: list[dict]
    warnings: list[str]
    prompt_messages: list[dict[str, str]]
    answer: str
    usage: dict
    holder: dict


def _writer():
    try:
        from langgraph.config import get_stream_writer

        writer = get_stream_writer()
    except Exception:  # noqa: BLE001
        return lambda _payload: None
    return writer if callable(writer) else lambda _payload: None


class ChatGraph:
    def __init__(self) -> None:
        self._graph = None

    def build(self):
        if self._graph is not None:
            return self._graph
        from langgraph.graph import END, START, StateGraph

        builder = StateGraph(ChatState)
        builder.add_node("load_context", self._load_context)
        builder.add_node("memory_search", self._memory_search)
        builder.add_node("retrieve", self._retrieve)
        builder.add_node("build_prompt", self._build_prompt)
        builder.add_node("generate", self._generate)
        builder.add_node("persist", self._persist)
        builder.add_edge(START, "load_context")
        builder.add_edge("load_context", "memory_search")
        builder.add_edge("memory_search", "retrieve")
        builder.add_edge("retrieve", "build_prompt")
        builder.add_edge("build_prompt", "generate")
        builder.add_edge("generate", "persist")
        builder.add_edge("persist", END)
        self._graph = builder.compile()
        return self._graph

    async def astream(self, state: ChatState):
        graph = self.build()
        async for payload in graph.astream(state, stream_mode="custom"):
            yield payload

    # ------------------------------------------------------------------ nodes
    async def _load_context(self, state: ChatState) -> dict:
        def load() -> list[dict]:
            with SessionLocal() as db:
                rows = db.scalars(
                    select(Message)
                    .where(
                        Message.session_id == state["session_id"],
                        Message.id != state.get("user_message_id", ""),
                        Message.status != "streaming",
                    )
                    .order_by(Message.created_at.desc())
                    .limit(settings.history_max_messages)
                ).all()
                return [
                    {"role": message.role, "content": message.content}
                    for message in reversed(rows)
                    if message.role in {"user", "assistant"} and message.content
                ]

        history = await asyncio.to_thread(load)
        return {"history": history, "warnings": state.get("warnings", [])}

    async def _memory_search(self, state: ChatState) -> dict:
        if not state.get("memory_enabled") or not settings.memory_enabled:
            return {"memories": []}
        try:
            memories = await asyncio.to_thread(
                memory_service.search, state["question"], settings.memory_top_k
            )
        except MemoryServiceError as exc:
            logger.warning("Memory search unavailable: {}", exc)
            return {
                "memories": [],
                "warnings": state.get("warnings", []) + ["长期记忆暂不可用，已跳过"],
            }
        writer = _writer()
        if memories:
            writer(
                {
                    "type": "memory",
                    "count": len(memories),
                    "items": [
                        {
                            "category": item.get("category", "other"),
                            "content": item.get("content", ""),
                        }
                        for item in memories[:6]
                    ],
                }
            )
        return {"memories": memories}

    async def _retrieve(self, state: ChatState) -> dict:
        if not state.get("rag_enabled") or not state.get("kb_ids"):
            return {"retrieved": [], "sources": []}
        result: RetrievalResult = await asyncio.to_thread(
            retrieval_service.retrieve,
            state["question"],
            state.get("kb_ids") or [],
            state.get("retrieval_mode", "hybrid"),
            None,
            None,
            state.get("rerank"),
            state.get("parent_expand", True),
        )
        sources = retrieval_service.to_sources(result.chunks)
        writer = _writer()
        writer({"type": "sources", "sources": sources})
        for warning in result.warnings:
            writer({"type": "warning", "message": warning})
        return {
            "retrieved": result.chunks,
            "sources": sources,
            "warnings": state.get("warnings", []) + result.warnings,
        }

    async def _build_prompt(self, state: ChatState) -> dict:
        messages = build_prompt_messages(
            question=state["question"],
            history=state.get("history", []),
            memories=state.get("memories", []),
            chunks=state.get("retrieved", []),
            max_context_chars=settings.max_context_chars,
        )
        return {"prompt_messages": messages}

    async def _generate(self, state: ChatState) -> dict:
        writer = _writer()
        holder = state.get("holder")
        if holder is None:
            holder = {"text": "", "usage": {}}
        warnings = list(state.get("warnings", []))
        answer_parts: list[str] = []
        usage: dict = {}
        try:
            async for item in llm_service.astream(state["prompt_messages"]):
                if item["type"] == "token":
                    text = item["text"]
                    answer_parts.append(text)
                    holder["text"] = "".join(answer_parts)
                    writer({"type": "token", "text": text})
                elif item["type"] == "usage":
                    usage = item.get("usage") or {}
                    holder["usage"] = usage
        except LLMError as exc:
            holder["text"] = "".join(answer_parts)
            raise
        answer = "".join(answer_parts)
        if not answer.strip():
            raise LLMError("模型没有返回内容")
        return {"answer": answer, "usage": usage, "holder": holder, "warnings": warnings}

    async def _persist(self, state: ChatState) -> dict:
        answer = state.get("answer", "")
        if not answer:
            return {}
        sources = state.get("sources", [])
        usage = state.get("usage", {})

        def save() -> None:
            with session_scope() as db:
                assistant = db.get(Message, state["assistant_message_id"])
                if assistant is None:
                    return
                assistant.content = answer
                assistant.status = "complete"
                assistant.sources_json = json.dumps(sources, ensure_ascii=False)
                assistant.usage_json = json.dumps(usage, ensure_ascii=False)
                assistant.memory_extracted_at = None

        await asyncio.to_thread(save)

        if state.get("memory_enabled") and settings.memory_enabled:
            try:
                from app.services.memory_worker import MemoryTask, memory_worker

                memory_worker.enqueue(
                    MemoryTask(
                        session_id=state["session_id"],
                        user_message_id=state.get("user_message_id", ""),
                        assistant_message_id=state["assistant_message_id"],
                    )
                )
            except Exception:  # noqa: BLE001
                logger.exception("Failed to enqueue memory extraction")
        return {}


chat_graph = ChatGraph()
