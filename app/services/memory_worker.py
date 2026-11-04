"""Per-turn long-term memory extraction worker.

One daemon thread consumes a bounded queue. Every chat turn enqueues the
assistant message after the answer is persisted. Failures never block chat;
unprocessed messages are swept on startup and when a session ends.
"""
from __future__ import annotations

import json
import queue
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.core.ids import new_id
from app.db import SessionLocal, session_scope
from app.models import Memory, MemoryEvent, Message, Session as ChatSession, utcnow
from app.services.memory import (
    MemoryServiceError,
    _as_list,
    memory_service,
    normalize_event,
)


@dataclass
class MemoryTask:
    session_id: str
    user_message_id: str
    assistant_message_id: str


def _event_payload(value) -> str:  # noqa: ANN001
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        text = str(value)
    return text[:4000]


class MemoryWorker:
    def __init__(self) -> None:
        self._queue: queue.Queue[MemoryTask | None] = queue.Queue(maxsize=200)
        self._thread: threading.Thread | None = None
        self._reconcile_thread: threading.Thread | None = None
        self._stop = threading.Event()

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="memory-worker", daemon=True)
        self._thread.start()
        self._reconcile_thread = threading.Thread(
            target=self._reconcile_loop, name="memory-reconciler", daemon=True
        )
        self._reconcile_thread.start()
        logger.info("Memory worker started")

    def stop(self) -> None:
        self._stop.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        if self._thread:
            self._thread.join(timeout=5)
        if self._reconcile_thread:
            self._reconcile_thread.join(timeout=5)
        self._thread = None
        self._reconcile_thread = None

    def enqueue(self, task: MemoryTask) -> bool:
        try:
            self._queue.put_nowait(task)
            return True
        except queue.Full:
            logger.warning("Memory queue is full; skipping message {}", task.assistant_message_id)
            return False

    # ------------------------------------------------------------------ worker
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                task = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                if task is None:
                    break
                self._process(task)
            except Exception:  # noqa: BLE001
                logger.exception("Memory worker task failed")
            finally:
                try:
                    self._queue.task_done()
                except ValueError:
                    pass

    def _reconcile_loop(self) -> None:
        """Check PENDING Mem0 events that outlived the synchronous wait."""
        while not self._stop.is_set():
            try:
                self.reconcile_pending()
            except Exception:  # noqa: BLE001
                logger.exception("Memory reconciliation failed")
            if self._stop.wait(max(5.0, settings.memory_reconcile_interval)):
                return

    def _process(self, task: MemoryTask) -> None:
        with SessionLocal() as db:
            assistant = db.get(Message, task.assistant_message_id)
            if assistant is None or assistant.role != "assistant":
                return
            if assistant.memory_extracted_at:
                return
            if assistant.status != "complete":
                return
            session = db.get(ChatSession, assistant.session_id)
            if session is None:
                return
            if not settings.memory_enabled or not session.memory_enabled:
                self._mark_skipped(db, assistant, "记忆功能已关闭")
                db.commit()
                return
            user_message = db.scalars(
                select(Message)
                .where(
                    Message.session_id == assistant.session_id,
                    Message.role == "user",
                    Message.created_at <= assistant.created_at,
                )
                .order_by(Message.created_at.desc())
                .limit(1)
            ).first()
            if user_message is None:
                self._mark_skipped(db, assistant, "找不到对应用户消息")
                db.commit()
                return
            user_content = user_message.content
            assistant_content = assistant.content
            user_id = user_message.id
            session_id = assistant.session_id
            assistant_id = assistant.id

        if not assistant_content.strip():
            with session_scope() as db:
                self._mark_skipped(db, db.get(Message, assistant_id), "助手回复为空")
            return

        events: list[dict] | None = None
        last_error = ""
        for attempt in range(settings.memory_worker_retries + 1):
            try:
                events = memory_service.add_turn(
                    user_content=user_content,
                    assistant_content=assistant_content,
                    session_id=session_id,
                    message_id=assistant_id,
                )
                break
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                logger.warning("Memory add attempt {} failed: {}", attempt + 1, exc)
                if attempt < settings.memory_worker_retries:
                    time.sleep(2 ** attempt)

        with session_scope() as db:
            assistant = db.get(Message, assistant_id)
            user_message = db.get(Message, user_id)
            if events is None:
                db.add(
                    MemoryEvent(
                        id=new_id(),
                        session_id=session_id,
                        message_id=assistant_id,
                        action="ERROR",
                        payload_json=_event_payload({"error": last_error}),
                    )
                )
                return

            self._record_event_rows(db, events, session_id, assistant_id)

            now = utcnow()
            if assistant is not None:
                assistant.memory_extracted_at = now
            if user_message is not None:
                user_message.memory_extracted_at = now

    @staticmethod
    def _record_event_rows(
        db: Session, events: list[dict], session_id: str, message_id: str
    ) -> None:
        """Persist event audit rows and keep the local memory mirror in sync."""
        for event in events:
            action = str(event.get("action", "NONE")).upper()
            mem0_id = event.get("mem0_id")
            content = str(event.get("content") or "").strip()
            db.add(
                MemoryEvent(
                    id=new_id(),
                    session_id=session_id,
                    message_id=message_id,
                    action=action,
                    mem0_id=str(mem0_id) if mem0_id else None,
                    payload_json=_event_payload(event.get("raw", event)),
                )
            )
            if not mem0_id:
                continue
            if action in {"ADD", "UPDATE"} and content:
                mirror = db.scalars(
                    select(Memory).where(Memory.mem0_id == str(mem0_id))
                ).first()
                if mirror is None:
                    mirror = Memory(
                        id=new_id(),
                        mem0_id=str(mem0_id),
                        category=str(event.get("category") or "other"),
                        content=content,
                        confidence=float(event.get("confidence") or 0.7),
                        stability=str(event.get("stability") or "durable"),
                        status="active",
                        source_session_id=session_id,
                        source_message_id=message_id,
                        metadata_json=_event_payload(event.get("metadata", {})),
                    )
                    db.add(mirror)
                else:
                    mirror.category = str(event.get("category") or mirror.category)
                    mirror.content = content
                    mirror.confidence = float(event.get("confidence") or mirror.confidence)
                    mirror.stability = str(event.get("stability") or mirror.stability)
                    mirror.status = "active"
                    mirror.updated_at = utcnow()
            elif action == "DELETE":
                mirror = db.scalars(
                    select(Memory).where(Memory.mem0_id == str(mem0_id))
                ).first()
                if mirror is not None:
                    mirror.status = "deleted"
                    mirror.updated_at = utcnow()

    def reconcile_pending(self, limit: int = 20) -> int:
        """Import results for adds that timed out while Mem0 was still PENDING."""
        if not memory_service.configured:
            return 0
        with SessionLocal() as db:
            rows = db.scalars(
                select(MemoryEvent)
                .where(MemoryEvent.action == "PENDING")
                .order_by(MemoryEvent.created_at.asc())
                .limit(limit)
            ).all()
            pending = [
                (row.id, row.session_id, row.message_id, row.payload_json) for row in rows
            ]
        handled = 0
        for row_id, session_id, message_id, payload_json in pending:
            try:
                payload = json.loads(payload_json or "{}")
            except json.JSONDecodeError:
                payload = {}
            event_id = str(payload.get("event_id") or "")
            if not event_id:
                with session_scope() as db:
                    row = db.get(MemoryEvent, row_id)
                    if row is not None:
                        row.action = "NONE"
                continue
            try:
                event = memory_service.get_event(event_id)
            except MemoryServiceError as exc:
                logger.warning("Memory reconcile query failed for {}: {}", event_id, exc)
                continue
            status = str(event.get("status") or "").upper()
            if status == "SUCCEEDED":
                events = [normalize_event(item) for item in _as_list(event)]
                if not events:
                    events = [normalize_event({"event": "NONE", "event_id": event_id})]
                with session_scope() as db:
                    self._record_event_rows(db, events, session_id, message_id)
                    row = db.get(MemoryEvent, row_id)
                    if row is not None:
                        row.action = "RECONCILED"
                        row.payload_json = _event_payload(
                            {"event_id": event_id, "status": status, "reconciled": True}
                        )
                handled += 1
            elif status == "FAILED":
                with session_scope() as db:
                    row = db.get(MemoryEvent, row_id)
                    if row is not None:
                        row.action = "ERROR"
                        row.payload_json = _event_payload(
                            {"event_id": event_id, "error": event.get("error") or "FAILED"}
                        )
                handled += 1
        if handled:
            logger.info("Reconciled {} pending memory events", handled)
        return handled

    @staticmethod
    def _mark_skipped(db: Session, assistant: Message | None, reason: str) -> None:
        if assistant is None:
            return
        now = utcnow()
        assistant.memory_extracted_at = now
        db.add(
            MemoryEvent(
                id=new_id(),
                session_id=assistant.session_id,
                message_id=assistant.id,
                action="SKIPPED",
                payload_json=_event_payload({"reason": reason}),
            )
        )

    # ------------------------------------------------------------------ sweeps
    def sweep_recent(self, days: int = 2, limit: int = 50) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
        with SessionLocal() as db:
            rows = db.scalars(
                select(Message)
                .where(
                    Message.role == "assistant",
                    Message.status == "complete",
                    Message.memory_extracted_at.is_(None),
                    Message.created_at >= cutoff,
                )
                .order_by(Message.created_at.asc())
                .limit(limit)
            ).all()
            tasks = [
                MemoryTask(
                    session_id=message.session_id,
                    user_message_id="",
                    assistant_message_id=message.id,
                )
                for message in rows
            ]
        for task in tasks:
            self.enqueue(task)
        if tasks:
            logger.info("Memory sweep enqueued {} messages", len(tasks))
        return len(tasks)

    def sweep_session(self, session_id: str) -> int:
        with SessionLocal() as db:
            rows = db.scalars(
                select(Message)
                .where(
                    Message.session_id == session_id,
                    Message.role == "assistant",
                    Message.status == "complete",
                    Message.memory_extracted_at.is_(None),
                )
                .order_by(Message.created_at.asc())
            ).all()
            tasks = [
                MemoryTask(
                    session_id=session_id,
                    user_message_id="",
                    assistant_message_id=message.id,
                )
                for message in rows
            ]
            session = db.get(ChatSession, session_id)
            if session is not None:
                session.memory_swept_at = utcnow()
        for task in tasks:
            self.enqueue(task)
        return len(tasks)

    # ------------------------------------------------------------------ events
    @staticmethod
    def recent_events(session_id: str, since: str | None = None) -> list[dict]:
        with SessionLocal() as db:
            query = select(MemoryEvent).where(MemoryEvent.session_id == session_id)
            if since:
                query = query.where(MemoryEvent.created_at > since)
            events = db.scalars(query.order_by(MemoryEvent.created_at.asc()).limit(50)).all()
            memory_ids = [event.mem0_id for event in events if event.mem0_id]
            mirrors = {}
            if memory_ids:
                rows = db.scalars(select(Memory).where(Memory.mem0_id.in_(memory_ids))).all()
                mirrors = {row.mem0_id: row for row in rows}
            result = []
            for event in events:
                mirror = mirrors.get(event.mem0_id)
                try:
                    payload = json.loads(event.payload_json or "{}")
                except json.JSONDecodeError:
                    payload = {}
                result.append(
                    {
                        "id": event.id,
                        "session_id": event.session_id,
                        "message_id": event.message_id,
                        "action": event.action,
                        "mem0_id": event.mem0_id,
                        "memory_id": mirror.id if mirror else None,
                        "category": mirror.category if mirror else "",
                        "content": mirror.content if mirror else "",
                        "created_at": event.created_at,
                        "payload": payload,
                    }
                )
            return result


memory_worker = MemoryWorker()
