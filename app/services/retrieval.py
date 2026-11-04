"""Dense / sparse / hybrid retrieval with rerank and parent expansion."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from loguru import logger

from app.config import settings
from app.db import SessionLocal
from app.models import Chunk, Document
from app.services.embedding import EmbeddingError, embedding_service
from app.services.milvus_store import MilvusError, milvus_store
from app.services.rerank import RerankError, rerank_service

RetrievalMode = Literal["dense", "sparse", "hybrid"]

_ID_RE = re.compile(r"^[a-f0-9]{32}$")


@dataclass
class RetrievedChunk:
    chunk_id: str
    document_id: str
    kb_id: str = ""
    text: str = ""
    heading_path: str = ""
    page: int | None = None
    score: float = 0.0
    source: str = ""
    parent_id: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class RetrievalResult:
    chunks: list[RetrievedChunk] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    mode: str = "hybrid"


def _valid_ids(values: list[str] | None) -> list[str]:
    if not values:
        return []
    return [value for value in values if _ID_RE.match(value or "")]


class RetrievalService:
    def retrieve(
        self,
        query: str,
        kb_ids: list[str] | None = None,
        mode: RetrievalMode = "hybrid",
        top_k: int | None = None,
        candidate_k: int | None = None,
        rerank: bool | None = None,
        parent_expand: bool = True,
        document_ids: list[str] | None = None,
    ) -> RetrievalResult:
        top_k = top_k or settings.retrieval_top_k
        candidate_k = max(top_k, candidate_k or settings.retrieval_candidate_k)
        use_rerank = settings.rerank_enabled if rerank is None else rerank
        warnings: list[str] = []

        safe_kbs = _valid_ids(kb_ids)
        safe_docs = _valid_ids(document_ids)
        if kb_ids and not safe_kbs:
            return RetrievalResult(warnings=["知识库参数不合法"], mode=mode)
        if document_ids and not safe_docs:
            return RetrievalResult(warnings=["文档参数不合法"], mode=mode)

        expr = milvus_store.build_filter(safe_kbs or None, safe_docs or None, level=1)
        query_vector: list[float] | None = None
        if mode in {"dense", "hybrid"}:
            try:
                query_vector = embedding_service.encode_query(query)
            except EmbeddingError as exc:
                logger.warning("Query embedding failed: {}", exc)
                return RetrievalResult(warnings=[str(exc)], mode=mode)

        try:
            if mode == "dense":
                raw = milvus_store.search_dense(query_vector or [], candidate_k, expr)
            elif mode == "sparse":
                raw = milvus_store.search_sparse(query, candidate_k, expr)
            else:
                raw = milvus_store.hybrid_search(query_vector or [], query, candidate_k, expr)
        except (MilvusError, Exception) as exc:  # noqa: BLE001
            logger.exception("Retrieval failed")
            return RetrievalResult(warnings=[f"知识库检索失败：{exc}"], mode=mode)

        chunks = self._to_chunks(raw, mode)
        if not chunks:
            warnings.append("知识库中没有检索到相关内容")
            return RetrievalResult(warnings=warnings, mode=mode)

        if use_rerank and len(chunks) > 1:
            try:
                pairs = rerank_service.rerank(query, [chunk.text for chunk in chunks], top_k * 2)
                reranked: list[RetrievedChunk] = []
                for index, score in pairs:
                    if 0 <= index < len(chunks):
                        chunk = chunks[index]
                        chunk.score = float(score)
                        chunk.meta["rerank"] = True
                        reranked.append(chunk)
                if reranked:
                    chunks = reranked
            except RerankError as exc:
                logger.warning("Rerank failed, keeping original order: {}", exc)
                warnings.append(f"重排序不可用，已按原检索顺序返回：{exc}")

        if parent_expand:
            chunks = self._expand_parents(chunks, warnings)

        chunks = self._dedupe(chunks)
        chunks = chunks[:top_k]
        self._attach_filenames(chunks)
        return RetrievalResult(chunks=chunks, warnings=warnings, mode=mode)

    @staticmethod
    def _to_chunks(raw: list[dict], mode: str) -> list[RetrievedChunk]:
        chunks: list[RetrievedChunk] = []
        for item in raw:
            chunk_id = str(item.get("id") or "")
            if not chunk_id:
                continue
            meta = item.get("meta") or {}
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except json.JSONDecodeError:
                    meta = {}
            if not isinstance(meta, dict):
                meta = {}
            page = item.get("page")
            chunks.append(
                RetrievedChunk(
                    chunk_id=chunk_id,
                    document_id=str(item.get("document_id") or ""),
                    kb_id=str(item.get("kb_id") or ""),
                    text=str(item.get("text") or ""),
                    heading_path=str(item.get("heading_path") or ""),
                    page=int(page) if isinstance(page, int) and page > 0 else None,
                    score=float(item.get("score") or 0.0),
                    source=mode,
                    parent_id=str(item.get("parent_id") or ""),
                    meta=meta,
                )
            )
        return chunks

    @staticmethod
    def _expand_parents(
        chunks: list[RetrievedChunk], warnings: list[str]
    ) -> list[RetrievedChunk]:
        parent_ids = sorted({chunk.parent_id for chunk in chunks if chunk.parent_id})
        if not parent_ids:
            return chunks
        try:
            rows = milvus_store.get_by_ids(
                parent_ids,
                output_fields=[
                    "id",
                    "document_id",
                    "kb_id",
                    "parent_id",
                    "level",
                    "heading_path",
                    "page",
                    "text",
                    "meta",
                ],
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Parent expansion failed: {}", exc)
            warnings.append(f"父块展开失败，已使用子块：{exc}")
            return chunks
        parents = {str(row.get("id")): row for row in rows}
        for chunk in chunks:
            parent = parents.get(chunk.parent_id)
            if not parent:
                continue
            parent_text = str(parent.get("text") or "")
            if parent_text:
                chunk.meta["child_text"] = chunk.text
                chunk.text = parent_text
                chunk.heading_path = str(parent.get("heading_path") or chunk.heading_path)
                page = parent.get("page")
                if isinstance(page, int) and page > 0:
                    chunk.page = page
                chunk.meta["parent_expanded"] = True
        return chunks

    @staticmethod
    def _dedupe(chunks: list[RetrievedChunk]) -> list[RetrievedChunk]:
        best: dict[tuple[str, str], RetrievedChunk] = {}
        order: list[tuple[str, str]] = []
        for chunk in chunks:
            key = (chunk.document_id, chunk.parent_id or chunk.chunk_id)
            if key not in best:
                best[key] = chunk
                order.append(key)
            elif chunk.score > best[key].score:
                best[key] = chunk
        return [best[key] for key in order]

    @staticmethod
    def _attach_filenames(chunks: list[RetrievedChunk]) -> None:
        document_ids = sorted({chunk.document_id for chunk in chunks if chunk.document_id})
        if not document_ids:
            return
        plain_ids = sorted(
            {
                chunk.chunk_id
                for chunk in chunks
                if chunk.chunk_id and not chunk.meta.get("parent_expanded")
            }
        )
        parent_ids = sorted(
            {
                chunk.parent_id
                for chunk in chunks
                if chunk.parent_id and chunk.meta.get("parent_expanded")
            }
        )
        with SessionLocal() as db:
            rows = db.query(Document).filter(Document.id.in_(document_ids)).all()
            filenames = {row.id: row.filename for row in rows}
            texts = {}
            ids = sorted(set(plain_ids) | set(parent_ids))
            if ids:
                chunk_rows = db.query(Chunk).filter(Chunk.id.in_(ids)).all()
                texts = {row.id: row.text for row in chunk_rows}
        for chunk in chunks:
            chunk.meta["filename"] = filenames.get(chunk.document_id, "")
            # Milvus stores heading + text for retrieval; show the clean source
            # text in citations instead of the duplicated heading prefix.
            if chunk.meta.get("parent_expanded"):
                chunk.text = texts.get(chunk.parent_id, chunk.text)
            else:
                chunk.text = texts.get(chunk.chunk_id, chunk.text)

    @staticmethod
    def to_sources(chunks: list[RetrievedChunk], preview_chars: int = 1200) -> list[dict]:
        sources: list[dict] = []
        for index, chunk in enumerate(chunks, start=1):
            sources.append(
                {
                    "index": index,
                    "chunk_id": chunk.chunk_id,
                    "document_id": chunk.document_id,
                    "filename": chunk.meta.get("filename", ""),
                    "heading_path": chunk.heading_path,
                    "page": chunk.page,
                    "score": round(chunk.score, 6),
                    "text": chunk.text[:preview_chars],
                    "source": chunk.source,
                }
            )
        return sources


retrieval_service = RetrievalService()
