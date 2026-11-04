"""Document upload, listing, preview and deletion."""
from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.core.ids import new_id
from app.core.text import file_type_of, safe_filename
from app.db import get_db
from app.models import Chunk, Document, KnowledgeBase, utcnow
from app.schemas import ChunkOut, DocumentOut, UploadItemResult
from app.services.chunking import ChunkConfig
from app.services.indexing import indexing_service

router = APIRouter(prefix="/api", tags=["documents"])

ALLOWED_TYPES = {"docx", "html", "md", "pdf"}


def _document_out(document: Document) -> DocumentOut:
    try:
        config = json.loads(document.chunk_config_json or "{}")
    except json.JSONDecodeError:
        config = {}
    return DocumentOut(
        id=document.id,
        kb_id=document.kb_id,
        filename=document.filename,
        file_type=document.file_type,
        size_bytes=document.size_bytes,
        status=document.status,
        error=document.error or "",
        page_count=document.page_count,
        char_count=document.char_count,
        block_count=document.block_count,
        chunk_count=document.chunk_count,
        chunk_strategy=document.chunk_strategy,
        chunk_config=config,
        created_at=document.created_at,
        updated_at=document.updated_at,
    )


@router.post("/kbs/{kb_id}/documents", response_model=list[UploadItemResult])
async def upload_documents(
    kb_id: str,
    files: list[UploadFile] = File(...),
    config: str = Form("{}"),
    db: Session = Depends(get_db),
) -> list[UploadItemResult]:
    if db.get(KnowledgeBase, kb_id) is None:
        raise HTTPException(404, "知识库不存在")
    try:
        chunk_config = ChunkConfig.model_validate_json(config or "{}")
    except ValidationError as exc:
        raise HTTPException(422, f"分块参数不合法：{exc.errors()}") from exc

    results: list[UploadItemResult] = []
    for upload in files:
        original_name = upload.filename or "file"
        filename = safe_filename(original_name)
        file_type = file_type_of(filename)
        if file_type not in ALLOWED_TYPES:
            results.append(
                UploadItemResult(
                    filename=original_name,
                    error="只支持 docx / html / md / pdf",
                )
            )
            continue

        data = await upload.read()
        max_bytes = settings.max_upload_mb * 1024 * 1024
        if not data:
            results.append(UploadItemResult(filename=original_name, error="文件为空"))
            continue
        if len(data) > max_bytes:
            results.append(
                UploadItemResult(
                    filename=original_name,
                    error=f"文件超过 {settings.max_upload_mb} MB 限制",
                )
            )
            continue

        document_id = new_id()
        directory = settings.upload_path / document_id
        directory.mkdir(parents=True, exist_ok=True)
        stored_path = directory / filename
        try:
            await asyncio.to_thread(stored_path.write_bytes, data)
        except OSError as exc:
            results.append(UploadItemResult(filename=original_name, error=f"保存失败：{exc}"))
            continue

        document = Document(
            id=document_id,
            kb_id=kb_id,
            filename=filename,
            stored_path=str(stored_path),
            file_type=file_type,
            size_bytes=len(data),
            status="uploaded",
            chunk_strategy=chunk_config.strategy,
            chunk_config_json=json.dumps(chunk_config.model_dump(), ensure_ascii=False),
        )
        db.add(document)
        db.commit()

        try:
            job_id = indexing_service.submit(document_id, chunk_config)
            results.append(
                UploadItemResult(
                    filename=original_name,
                    document_id=document_id,
                    job_id=job_id,
                )
            )
        except Exception as exc:  # noqa: BLE001
            document.status = "failed"
            document.error = str(exc)[:2000]
            db.commit()
            results.append(
                UploadItemResult(
                    filename=original_name,
                    document_id=document_id,
                    error=str(exc)[:500],
                )
            )
    return results


@router.get("/kbs/{kb_id}/documents", response_model=list[DocumentOut])
def list_documents(kb_id: str, db: Session = Depends(get_db)) -> list[DocumentOut]:
    if db.get(KnowledgeBase, kb_id) is None:
        raise HTTPException(404, "知识库不存在")
    documents = db.scalars(
        select(Document).where(Document.kb_id == kb_id).order_by(Document.created_at.desc())
    ).all()
    return [_document_out(document) for document in documents]


@router.get("/documents/{document_id}", response_model=DocumentOut)
def get_document(document_id: str, db: Session = Depends(get_db)) -> DocumentOut:
    document = db.get(Document, document_id)
    if document is None:
        raise HTTPException(404, "文档不存在")
    return _document_out(document)


@router.get("/documents/{document_id}/chunks")
def list_chunks(
    document_id: str,
    level: int | None = 1,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
) -> dict:
    if db.get(Document, document_id) is None:
        raise HTTPException(404, "文档不存在")
    limit = max(1, min(limit, 200))
    query = select(Chunk).where(Chunk.document_id == document_id)
    count_query = select(func.count(Chunk.id)).where(Chunk.document_id == document_id)
    if level is not None:
        query = query.where(Chunk.level == level)
        count_query = count_query.where(Chunk.level == level)
    total = int(db.scalar(count_query) or 0)
    chunks = db.scalars(
        query.order_by(Chunk.chunk_index).offset(offset).limit(limit)
    ).all()
    items = []
    for chunk in chunks:
        try:
            meta = json.loads(chunk.meta_json or "{}")
        except json.JSONDecodeError:
            meta = {}
        items.append(
            ChunkOut(
                id=chunk.id,
                document_id=chunk.document_id,
                parent_id=chunk.parent_id,
                level=chunk.level,
                chunk_index=chunk.chunk_index,
                text=chunk.text,
                heading_path=chunk.heading_path,
                page=chunk.page,
                char_count=chunk.char_count,
                meta=meta,
            ).model_dump()
        )
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.post("/documents/{document_id}/reindex")
def reindex_document(document_id: str, db: Session = Depends(get_db)) -> dict:
    document = db.get(Document, document_id)
    if document is None:
        raise HTTPException(404, "文档不存在")
    if indexing_service.busy(document_id):
        raise HTTPException(409, "该文档正在处理中")
    try:
        config = ChunkConfig.model_validate_json(document.chunk_config_json or "{}")
    except ValidationError as exc:
        raise HTTPException(422, f"已保存的分块参数不合法：{exc.errors()}") from exc
    try:
        job_id = indexing_service.submit(document_id, config)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"job_id": job_id, "document_id": document_id}


@router.delete("/documents/{document_id}", status_code=204)
def delete_document(document_id: str, db: Session = Depends(get_db)) -> None:
    document = db.get(Document, document_id)
    if document is None:
        raise HTTPException(404, "文档不存在")
    if indexing_service.busy(document_id):
        raise HTTPException(409, "文档正在处理中，请稍后再删")
    try:
        indexing_service.delete_document(document_id)
    except Exception:  # noqa: BLE001
        pass
    db.delete(document)
    db.commit()
    shutil.rmtree(settings.upload_path / document_id, ignore_errors=True)
    (settings.parsed_path / f"{document_id}.json").unlink(missing_ok=True)
