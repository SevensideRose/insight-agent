"""SQLite models.

SQLite is the source of truth for knowledge bases, documents, chunks,
sessions and messages. Mem0 is the source of truth for long-term memories;
the `memories` table here is a display/audit mirror only.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Base(DeclarativeBase):
    pass


class KnowledgeBase(Base):
    __tablename__ = "knowledge_bases"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[str] = mapped_column(String(40), default=utcnow)
    updated_at: Mapped[str] = mapped_column(String(40), default=utcnow, onupdate=utcnow)

    documents: Mapped[list["Document"]] = relationship(
        back_populates="knowledge_base",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (Index("ix_documents_kb", "kb_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    kb_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False
    )
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    stored_path: Mapped[str] = mapped_column(Text, nullable=False)
    file_type: Mapped[str] = mapped_column(String(16), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(24), default="uploaded")
    error: Mapped[str] = mapped_column(Text, default="")
    page_count: Mapped[int] = mapped_column(Integer, default=0)
    char_count: Mapped[int] = mapped_column(Integer, default=0)
    block_count: Mapped[int] = mapped_column(Integer, default=0)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    chunk_strategy: Mapped[str] = mapped_column(String(24), default="recursive")
    chunk_config_json: Mapped[str] = mapped_column(Text, default="{}")
    embedding_version: Mapped[str] = mapped_column(String(64), default="")
    parsed_path: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[str] = mapped_column(String(40), default=utcnow)
    updated_at: Mapped[str] = mapped_column(String(40), default=utcnow, onupdate=utcnow)

    knowledge_base: Mapped[KnowledgeBase] = relationship(back_populates="documents")
    chunks: Mapped[list["Chunk"]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class Chunk(Base):
    __tablename__ = "chunks"
    __table_args__ = (
        Index("ix_chunks_doc_level", "document_id", "level", "chunk_index"),
        Index("ix_chunks_parent", "parent_id"),
        Index("ix_chunks_kb", "kb_id"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    document_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    kb_id: Mapped[str] = mapped_column(String(32), nullable=False)
    parent_id: Mapped[str] = mapped_column(String(32), default="")
    level: Mapped[int] = mapped_column(Integer, default=1)
    chunk_index: Mapped[int] = mapped_column(Integer, default=0)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    search_text: Mapped[str] = mapped_column(Text, nullable=False)
    heading_path: Mapped[str] = mapped_column(String(1024), default="")
    page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    char_count: Mapped[int] = mapped_column(Integer, default=0)
    meta_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[str] = mapped_column(String(40), default=utcnow)

    document: Mapped[Document] = relationship(back_populates="chunks")


class Session(Base):
    __tablename__ = "sessions"
    __table_args__ = (Index("ix_sessions_updated", "updated_at"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    title: Mapped[str] = mapped_column(String(160), default="新对话")
    kb_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    rag_enabled: Mapped[int] = mapped_column(Integer, default=1)
    memory_enabled: Mapped[int] = mapped_column(Integer, default=1)
    summary: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[str] = mapped_column(String(40), default=utcnow)
    updated_at: Mapped[str] = mapped_column(String(40), default=utcnow, onupdate=utcnow)
    ended_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    memory_swept_at: Mapped[str | None] = mapped_column(String(40), nullable=True)

    messages: Mapped[list["Message"]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (Index("ix_messages_session", "session_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="complete")
    sources_json: Mapped[str] = mapped_column(Text, default="[]")
    usage_json: Mapped[str] = mapped_column(Text, default="{}")
    memory_extracted_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default=utcnow)

    session: Mapped[Session] = relationship(back_populates="messages")


class Memory(Base):
    __tablename__ = "memories"
    __table_args__ = (
        Index("ix_memories_category", "category", "created_at"),
        Index("ix_memories_status", "status"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    mem0_id: Mapped[str | None] = mapped_column(String(80), unique=True, nullable=True)
    category: Mapped[str] = mapped_column(String(40), default="other")
    content: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.7)
    stability: Mapped[str] = mapped_column(String(16), default="durable")
    status: Mapped[str] = mapped_column(String(16), default="active")
    source_session_id: Mapped[str] = mapped_column(String(32), default="")
    source_message_id: Mapped[str] = mapped_column(String(32), default="")
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[str] = mapped_column(String(40), default=utcnow)
    updated_at: Mapped[str] = mapped_column(String(40), default=utcnow, onupdate=utcnow)


class MemoryEvent(Base):
    __tablename__ = "memory_events"
    __table_args__ = (Index("ix_memory_events_session", "session_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(32), default="")
    message_id: Mapped[str] = mapped_column(String(32), default="")
    action: Mapped[str] = mapped_column(String(16), default="NONE")
    mem0_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[str] = mapped_column(String(40), default=utcnow)


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (Index("ix_jobs_status", "status", "created_at"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    type: Mapped[str] = mapped_column(String(24), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="queued")
    progress: Mapped[int] = mapped_column(Integer, default=0)
    stage: Mapped[str] = mapped_column(String(24), default="queued")
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    result_json: Mapped[str] = mapped_column(Text, default="{}")
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[str] = mapped_column(String(40), default=utcnow)
    started_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    finished_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
