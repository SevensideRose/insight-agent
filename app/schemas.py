"""Pydantic request/response models."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class KBCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=1000)


class KBUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=1000)


class KBOut(BaseModel):
    id: str
    name: str
    description: str = ""
    document_count: int = 0
    created_at: str = ""
    updated_at: str = ""


class DocumentOut(BaseModel):
    id: str
    kb_id: str
    filename: str
    file_type: str
    size_bytes: int
    status: str
    error: str = ""
    page_count: int = 0
    char_count: int = 0
    block_count: int = 0
    chunk_count: int = 0
    chunk_strategy: str = "recursive"
    chunk_config: dict[str, Any] = Field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""


class UploadItemResult(BaseModel):
    filename: str
    document_id: str | None = None
    job_id: str | None = None
    error: str | None = None


class JobOut(BaseModel):
    id: str
    type: str
    status: str
    progress: int
    stage: str
    error: str = ""
    result: dict[str, Any] = Field(default_factory=dict)
    created_at: str = ""
    started_at: str | None = None
    finished_at: str | None = None


class ChunkOut(BaseModel):
    id: str
    document_id: str
    parent_id: str = ""
    level: int
    chunk_index: int
    text: str
    heading_path: str = ""
    page: int | None = None
    char_count: int = 0
    meta: dict[str, Any] = Field(default_factory=dict)


class SessionCreate(BaseModel):
    title: str = Field(default="新对话", max_length=160)
    kb_ids: list[str] = Field(default_factory=list)
    rag_enabled: bool = True
    memory_enabled: bool = True


class SessionUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=160)
    kb_ids: list[str] | None = None
    rag_enabled: bool | None = None
    memory_enabled: bool | None = None


class SessionOut(BaseModel):
    id: str
    title: str
    kb_ids: list[str] = Field(default_factory=list)
    rag_enabled: bool
    memory_enabled: bool
    created_at: str
    updated_at: str
    ended_at: str | None = None
    message_count: int = 0


class MessageOut(BaseModel):
    id: str
    session_id: str
    role: str
    content: str
    status: str
    sources: list[dict[str, Any]] = Field(default_factory=list)
    usage: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class ChatRequest(BaseModel):
    session_id: str | None = None
    message: str = Field(min_length=1, max_length=20000)
    kb_ids: list[str] | None = None
    rag_enabled: bool | None = None
    memory_enabled: bool | None = None
    retrieval_mode: Literal["dense", "sparse", "hybrid"] = "hybrid"
    rerank: bool | None = None
    parent_expand: bool | None = None


class MemoryOut(BaseModel):
    id: str
    mem0_id: str | None = None
    category: str
    content: str
    confidence: float
    stability: str
    status: str
    source_session_id: str = ""
    source_message_id: str = ""
    created_at: str
    updated_at: str


class MemorySearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    limit: int = Field(default=8, ge=1, le=50)


class MemoryUndoRequest(BaseModel):
    event_ids: list[str] = Field(default_factory=list, max_length=50)
