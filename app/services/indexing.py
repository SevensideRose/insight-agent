"""Background ingestion: parse -> chunk -> embed -> Milvus."""
from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from loguru import logger

from app.config import settings
from app.core.ids import new_id
from app.db import session_scope
from app.models import Chunk, Document, Job, utcnow
from app.services.chunking import ChunkConfig, ChunkDraft, chunk_document
from app.services.embedding import embedding_service
from app.services.milvus_store import MilvusError, milvus_store
from app.services.parsing import parse_document


class IndexingService:
    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ingest")
        self._active: set[str] = set()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ public
    def submit(self, document_id: str, config: ChunkConfig) -> str:
        with self._lock:
            if document_id in self._active:
                raise RuntimeError("该文档正在处理中")
            self._active.add(document_id)

        job_id = new_id()
        with session_scope() as db:
            doc = db.get(Document, document_id)
            if doc is None:
                raise ValueError("文档不存在")
            doc.chunk_strategy = config.strategy
            doc.chunk_config_json = json.dumps(config.model_dump(), ensure_ascii=False)
            doc.status = "uploaded"
            doc.error = ""
            db.add(
                Job(
                    id=job_id,
                    type="ingest",
                    status="queued",
                    stage="queued",
                    payload_json=json.dumps(
                        {"document_id": document_id, "config": config.model_dump()},
                        ensure_ascii=False,
                    ),
                )
            )
        self._executor.submit(self._run, job_id, document_id, config)
        return job_id

    def busy(self, document_id: str) -> bool:
        with self._lock:
            return document_id in self._active

    # ------------------------------------------------------------------ worker
    def _run(self, job_id: str, document_id: str, config: ChunkConfig) -> None:
        try:
            self._run_inner(job_id, document_id, config)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Ingestion failed for {}", document_id)
            self._fail(job_id, document_id, str(exc))
        finally:
            with self._lock:
                self._active.discard(document_id)

    def _run_inner(self, job_id: str, document_id: str, config: ChunkConfig) -> None:
        with session_scope() as db:
            doc = db.get(Document, document_id)
            if doc is None:
                raise ValueError("文档不存在")
            kb_id = doc.kb_id
            stored_path = Path(doc.stored_path)
            file_type = doc.file_type
            doc.status = "parsing"
            doc.error = ""
        self._job(job_id, progress=3, stage="parse", status="running")

        result = parse_document(stored_path, file_type, document_id)
        if not result.blocks:
            raise RuntimeError("没有解析出可用文本。扫描件请确认 OCR 已启用，或上传文本版 PDF。")

        with session_scope() as db:
            doc = db.get(Document, document_id)
            if doc is None:
                raise ValueError("文档不存在")
            doc.status = "chunking"
            doc.page_count = result.page_count
            doc.char_count = result.char_count
            doc.block_count = len(result.blocks)
            doc.parsed_path = str(settings.parsed_path / f"{document_id}.json")
            doc.embedding_version = settings.embedding_version
        self._job(job_id, progress=15, stage="chunk")

        drafts = chunk_document(result.blocks, config)
        if not drafts:
            raise RuntimeError("分块结果为空，请调整分块参数")

        # Assign stable IDs and resolve parent references before writing.
        ids = [new_id() for _ in drafts]
        parent_map = {
            draft.parent_key: ids[index]
            for index, draft in enumerate(drafts)
            if draft.level == 0 and draft.parent_key
        }

        rows_to_embed: list[dict[str, Any]] = []
        with session_scope() as db:
            db.query(Chunk).filter(Chunk.document_id == document_id).delete()
            for index, draft in enumerate(drafts):
                chunk_id = ids[index]
                parent_id = ""
                if draft.level == 1 and draft.parent_key:
                    parent_id = parent_map.get(draft.parent_key, "")
                search_text = draft.text
                if draft.heading_path:
                    search_text = f"{draft.heading_path}\n{draft.text}"
                db.add(
                    Chunk(
                        id=chunk_id,
                        document_id=document_id,
                        kb_id=kb_id,
                        parent_id=parent_id,
                        level=draft.level,
                        chunk_index=index,
                        text=draft.text,
                        search_text=search_text,
                        heading_path=draft.heading_path or "",
                        page=draft.page,
                        char_count=len(draft.text),
                        meta_json=json.dumps(draft.meta, ensure_ascii=False),
                    )
                )
                rows_to_embed.append(
                    {
                        "id": chunk_id,
                        "kb_id": kb_id,
                        "document_id": document_id,
                        "parent_id": parent_id,
                        "level": draft.level,
                        "chunk_index": index,
                        "page": draft.page or 0,
                        "heading_path": (draft.heading_path or "")[:1024],
                        "text": search_text[:65535],
                        "embedding_version": settings.embedding_version,
                        "meta": draft.meta,
                    }
                )
            doc = db.get(Document, document_id)
            if doc is not None:
                doc.status = "embedding"
                doc.chunk_count = sum(1 for draft in drafts if draft.level == 1)
        self._job(job_id, progress=30, stage="embed")

        # Remove previous vectors (reindex) before inserting the new set.
        milvus_store.delete_document(document_id)

        # Embed in batches and insert into Milvus.
        batch_size = max(1, min(16, settings.embedding_batch_size * 2))
        total = len(rows_to_embed)
        inserted = 0
        for start in range(0, total, batch_size):
            batch = rows_to_embed[start : start + batch_size]
            vectors = embedding_service.encode([row["text"] for row in batch], batch_size=settings.embedding_batch_size)
            for row, vector in zip(batch, vectors):
                row["dense"] = vector
            milvus_store.insert_chunks(batch)
            inserted += len(batch)
            progress = 30 + int((inserted / max(total, 1)) * 55)
            self._job(job_id, progress=min(progress, 85), stage="embed")

        milvus_store.flush()
        self._job(job_id, progress=92, stage="index")

        with session_scope() as db:
            doc = db.get(Document, document_id)
            if doc is not None:
                doc.status = "ready"
                doc.error = ""
                doc.chunk_count = sum(1 for draft in drafts if draft.level == 1)
                doc.updated_at = utcnow()
            job = db.get(Job, job_id)
            if job is not None:
                job.status = "succeeded"
                job.progress = 100
                job.stage = "done"
                job.result_json = json.dumps(
                    {
                        "chunks": len(drafts),
                        "search_chunks": doc.chunk_count if doc else 0,
                        "char_count": result.char_count,
                        "warnings": result.warnings,
                    },
                    ensure_ascii=False,
                )
                job.finished_at = utcnow()
        logger.info("Ingested {} ({} chunks)", document_id, len(drafts))

    # ------------------------------------------------------------------ helpers
    def _job(self, job_id: str, **fields) -> None:
        with session_scope() as db:
            job = db.get(Job, job_id)
            if job is None:
                return
            for key, value in fields.items():
                setattr(job, key, value)

    def _fail(self, job_id: str, document_id: str, error: str) -> None:
        try:
            milvus_store.delete_document(document_id)
        except Exception:  # noqa: BLE001
            logger.warning("Could not clean Milvus entities for {}", document_id)
        try:
            with session_scope() as db:
                db.query(Chunk).filter(Chunk.document_id == document_id).delete()
                doc = db.get(Document, document_id)
                if doc is not None:
                    doc.status = "failed"
                    doc.error = error[:2000]
                    doc.chunk_count = 0
                job = db.get(Job, job_id)
                if job is not None:
                    job.status = "failed"
                    job.error = error[:2000]
                    job.stage = "failed"
                    job.finished_at = utcnow()
        except Exception:  # noqa: BLE001
            logger.exception("Failed to record ingestion failure")

    def delete_document(self, document_id: str) -> None:
        try:
            milvus_store.delete_document(document_id)
        except MilvusError:
            logger.warning("Milvus unavailable while deleting {}", document_id)


indexing_service = IndexingService()
