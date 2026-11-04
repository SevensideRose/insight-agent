"""Knowledge base CRUD."""
from __future__ import annotations

import shutil

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.core.ids import new_id
from app.db import get_db
from app.models import Document, KnowledgeBase, utcnow
from app.schemas import KBCreate, KBOut, KBUpdate
from app.services.indexing import indexing_service

router = APIRouter(prefix="/api/kbs", tags=["knowledge-bases"])


def _kb_out(db: Session, kb: KnowledgeBase) -> KBOut:
    count = db.scalar(
        select(func.count(Document.id)).where(Document.kb_id == kb.id)
    ) or 0
    return KBOut(
        id=kb.id,
        name=kb.name,
        description=kb.description or "",
        document_count=int(count),
        created_at=kb.created_at,
        updated_at=kb.updated_at,
    )


@router.get("", response_model=list[KBOut])
def list_kbs(db: Session = Depends(get_db)) -> list[KBOut]:
    kbs = db.scalars(select(KnowledgeBase).order_by(KnowledgeBase.created_at.desc())).all()
    return [_kb_out(db, kb) for kb in kbs]


@router.post("", response_model=KBOut, status_code=201)
def create_kb(payload: KBCreate, db: Session = Depends(get_db)) -> KBOut:
    kb = KnowledgeBase(id=new_id(), name=payload.name.strip(), description=payload.description.strip())
    db.add(kb)
    db.commit()
    db.refresh(kb)
    return _kb_out(db, kb)


@router.get("/{kb_id}", response_model=KBOut)
def get_kb(kb_id: str, db: Session = Depends(get_db)) -> KBOut:
    kb = db.get(KnowledgeBase, kb_id)
    if kb is None:
        raise HTTPException(404, "知识库不存在")
    return _kb_out(db, kb)


@router.patch("/{kb_id}", response_model=KBOut)
def update_kb(kb_id: str, payload: KBUpdate, db: Session = Depends(get_db)) -> KBOut:
    kb = db.get(KnowledgeBase, kb_id)
    if kb is None:
        raise HTTPException(404, "知识库不存在")
    if payload.name is not None:
        kb.name = payload.name.strip()
    if payload.description is not None:
        kb.description = payload.description.strip()
    kb.updated_at = utcnow()
    db.commit()
    db.refresh(kb)
    return _kb_out(db, kb)


@router.delete("/{kb_id}", status_code=204)
def delete_kb(kb_id: str, db: Session = Depends(get_db)) -> None:
    kb = db.get(KnowledgeBase, kb_id)
    if kb is None:
        raise HTTPException(404, "知识库不存在")
    documents = db.scalars(select(Document).where(Document.kb_id == kb_id)).all()
    for document in documents:
        try:
            indexing_service.delete_document(document.id)
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(settings.upload_path / document.id, ignore_errors=True)
        (settings.parsed_path / f"{document.id}.json").unlink(missing_ok=True)
    db.delete(kb)
    db.commit()
