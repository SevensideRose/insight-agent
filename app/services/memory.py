"""Mem0 Platform adapter.

The platform performs extraction/consolidation. We call `add` per chat turn
with `custom_instructions`; this module isolates SDK-version differences and
normalizes event shapes for the local mirror.
"""
from __future__ import annotations

import re
import threading
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from loguru import logger

from app.config import settings
from app.prompts.memory import CATEGORY_NAMES, MEMORY_CATEGORIES, MEMORY_INSTRUCTIONS


class MemoryServiceError(RuntimeError):
    pass


def local_time_note() -> str:
    """Give the extractor an explicit local clock.

    Mem0 observes timestamps in UTC and infers dates from that; relying on
    custom_instructions alone made "明天" resolve to the UTC date + 1.
    """
    zone = ZoneInfo(settings.app_timezone)
    now = datetime.now(zone)
    return (
        f"对话发生的本地时间是 {now:%Y-%m-%d %H:%M}（{settings.app_timezone}，UTC{now:%z}）。"
        "用户说的“今天/明天/后天/下周”等相对日期，一律以这个本地时间为基准换算成绝对日期。"
    )


_CATEGORY_TAG_RE = re.compile(
    r"^\s*\[(" + "|".join(sorted(CATEGORY_NAMES)) + r")\]\s*"
)


def _as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in ("results", "memories", "data", "events"):
            if isinstance(value.get(key), list):
                return value[key]
    return []


def _event_action(item: dict) -> str:
    action = item.get("event") or item.get("action") or item.get("type") or ""
    action = str(action).upper()
    if action in {"ADD", "UPDATE", "DELETE", "NONE", "PENDING"}:
        return action
    if (item.get("memory") or item.get("data")) and item.get("id"):
        return "ADD"
    return "NONE"


def normalize_event(item: Any) -> dict:
    if not isinstance(item, dict):
        return {"action": "NONE", "mem0_id": None, "content": "", "metadata": {}, "raw": item}
    data = item.get("data")
    if not isinstance(data, dict):
        data = {}
    metadata = item.get("metadata") or data.get("metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}
    # The v3 add response wraps the memory in {"data": {"memory": ...}};
    # list/search responses expose it at the top level.
    content = item.get("memory") or data.get("memory") or item.get("text") or data.get("text") or ""
    if not isinstance(content, str):
        content = str(content)
    categories = (
        item.get("categories")
        or data.get("categories")
        or metadata.get("category")
        or metadata.get("categories")
    )
    if isinstance(categories, list):
        category = str(categories[0]) if categories else ""
    else:
        category = str(categories or "")
    if category not in CATEGORY_NAMES:
        category = category if category else "other"
    tag = _CATEGORY_TAG_RE.match(content)
    if tag:
        if category not in CATEGORY_NAMES:
            category = tag.group(1)
        content = _CATEGORY_TAG_RE.sub("", content, count=1).strip()
    return {
        "action": _event_action(item),
        "mem0_id": item.get("id") or data.get("id") or item.get("memory_id"),
        "content": content,
        "category": category,
        "confidence": float(metadata.get("confidence", 0.7) or 0.7),
        "stability": str(metadata.get("stability", "durable") or "durable"),
        "metadata": metadata,
        "raw": item,
    }


class MemoryService:
    def __init__(self) -> None:
        self._client = None
        self._lock = threading.Lock()

    @property
    def configured(self) -> bool:
        return bool(settings.mem0_api_key)

    def client(self):
        if not self.configured:
            raise MemoryServiceError("未配置 MEM0_API_KEY")
        if self._client is None:
            with self._lock:
                if self._client is None:
                    try:
                        from mem0 import MemoryClient
                    except Exception as exc:  # noqa: BLE001
                        raise MemoryServiceError(f"mem0ai 导入失败：{exc}") from exc
                    self._client = MemoryClient(api_key=settings.mem0_api_key)
        return self._client

    # ------------------------------------------------------------------ read
    def _filters(self) -> dict:
        return {"user_id": settings.mem0_user_id}

    def search(self, query: str, limit: int | None = None) -> list[dict]:
        limit = limit or settings.memory_top_k
        try:
            raw = self.client().search(
                query=query,
                filters=self._filters(),
                top_k=limit,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Mem0 search failed: {}", exc)
            raise MemoryServiceError(f"记忆检索失败：{exc}") from exc
        items = []
        for item in _as_list(raw):
            normalized = normalize_event(item)
            normalized["score"] = float(item.get("score", 0.0)) if isinstance(item, dict) else 0.0
            items.append(normalized)
        items.sort(key=lambda item: item.get("score", 0.0), reverse=True)
        return items

    def get_all(self) -> list[dict]:
        try:
            raw = self.client().get_all(
                filters=self._filters(),
                page=1,
                page_size=200,
            )
        except Exception as exc:  # noqa: BLE001
            raise MemoryServiceError(f"记忆列表获取失败：{exc}") from exc
        return [normalize_event(item) for item in _as_list(raw)]

    # ------------------------------------------------------------------ write
    def add_turn(
        self,
        user_content: str,
        assistant_content: str,
        session_id: str,
        message_id: str,
    ) -> list[dict]:
        messages = [
            {"role": "system", "content": local_time_note()},
            {"role": "user", "content": user_content},
            {"role": "assistant", "content": assistant_content},
        ]
        metadata = {
            "session_id": session_id,
            "message_id": message_id,
            "source": "insight-agent",
        }
        client = self.client()
        # Mem0 v3 `add` takes the entity ID at the top level, while
        # `search`/`get_all` take it inside `filters`. Keep the two apart.
        base = {
            "messages": messages,
            "user_id": settings.mem0_user_id,
            "custom_instructions": MEMORY_INSTRUCTIONS,
            "metadata": metadata,
        }
        with self._lock:
            try:
                raw = client.add(
                    **base,
                    custom_categories=MEMORY_CATEGORIES,
                )
            except TypeError:
                # Older SDKs may not accept custom_categories.
                raw = client.add(**base)
            except ValueError as exc:
                if "custom_categories" not in str(exc):
                    raise MemoryServiceError(f"记忆写入失败：{exc}") from exc
                raw = client.add(**base)
            except Exception as exc:  # noqa: BLE001
                raise MemoryServiceError(f"记忆写入失败：{exc}") from exc
        # v3 `add` is asynchronous: it returns {"event_id", "status": "PENDING"}.
        # Poll the event so the local mirror receives the extracted memories.
        if isinstance(raw, dict) and raw.get("event_id") and not _as_list(raw):
            event_id = str(raw["event_id"])
            event = self.wait_for_event(event_id)
            status = str(event.get("status") or "").upper()
            if status == "FAILED":
                raise MemoryServiceError(
                    f"记忆提取失败：{event.get('error') or status}"
                )
            if status != "SUCCEEDED":
                return [
                    normalize_event(
                        {"event": "PENDING", "event_id": event_id, "status": status}
                    )
                ]
            events = [normalize_event(item) for item in _as_list(event)]
            if not events:
                events = [normalize_event({"event": "NONE", "event_id": event_id})]
            return events

        events = [normalize_event(item) for item in _as_list(raw)]
        if not events:
            events = [normalize_event({"event": "NONE", "message": raw})]
        return events

    def get_event(self, event_id: str) -> dict:
        try:
            response = self.client().client.get(f"/v1/event/{event_id}/")
            response.raise_for_status()
            raw = response.json()
        except Exception as exc:  # noqa: BLE001
            raise MemoryServiceError(f"记忆事件查询失败：{exc}") from exc
        return raw if isinstance(raw, dict) else {}

    def wait_for_event(self, event_id: str, timeout: float | None = None) -> dict:
        """Poll the v3 async add event until it finishes or times out."""
        timeout = settings.memory_event_timeout if timeout is None else timeout
        deadline = time.monotonic() + max(0.0, timeout)
        event: dict = {}
        while True:
            event = self.get_event(event_id)
            status = str(event.get("status") or "").upper()
            if status in {"SUCCEEDED", "FAILED"} or time.monotonic() >= deadline:
                return event
            time.sleep(settings.memory_event_poll_interval)

    def delete(self, mem0_id: str) -> None:
        try:
            self.client().delete(memory_id=mem0_id)
        except Exception as exc:  # noqa: BLE001
            raise MemoryServiceError(f"记忆删除失败：{exc}") from exc

    def check(self) -> dict:
        if not self.configured:
            return {"ok": False, "configured": False, "error": "未配置 MEM0_API_KEY"}
        try:
            items = self.search("学习", limit=1)
            return {"ok": True, "configured": True, "sample_count": len(items)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "configured": True, "error": str(exc)}


memory_service = MemoryService()
