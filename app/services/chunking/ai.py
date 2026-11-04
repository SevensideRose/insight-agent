"""AI-assisted chunk boundary selection.

The model only proposes boundaries between existing paragraph units; chunk
text always comes from the source document. Invalid output is retried with
the validation error, then falls back to recursive chunking.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from loguru import logger

from app.services.chunking.base import (
    ChunkConfig,
    ChunkDraft,
    normalize_draft_text,
    split_long_text,
)
from app.services.parsing import Block

JsonCaller = Callable[[str], dict]


def _default_json_caller(prompt: str) -> dict:
    from app.services.llm import llm_service

    return llm_service.chat_json(prompt)


def _paragraph_units(blocks: list[Block], max_unit: int = 700) -> list[dict[str, Any]]:
    units: list[dict[str, Any]] = []
    for block in blocks:
        text = normalize_draft_text(block.text)
        if not text:
            continue
        pieces = [text]
        if len(text) > max_unit:
            pieces = split_long_text(text, max_unit, 0)
        for piece in pieces:
            piece = normalize_draft_text(piece)
            if piece:
                units.append(
                    {
                        "id": f"p{len(units) + 1}",
                        "text": piece,
                        "heading_path": block.heading_path,
                        "page": block.page,
                    }
                )
    return units


def _build_prompt(units: list[dict[str, Any]], instructions: str, error: str = "") -> str:
    listing = "\n".join(
        f"[{unit['id']}] {unit['text'][:180]}" for unit in units
    )
    repair = f"\n上一次输出不合法，原因：{error}\n请修正后重新输出。" if error else ""
    return f"""你是文档分块助手。根据用户的定制要求，把下面的段落单元分成语义完整的块。

要求：
1. 只能使用给出的段落 id，不能新增、改写或删除正文。
2. 每个 id 必须且只能出现一次，顺序必须递增。
3. 每块建议不超过 {sum(len(u['text']) for u in units) // max(1, len(units)) * 8} 字。
4. 输出合法 JSON，不要 Markdown 代码块，不要解释。

用户定制要求：
{instructions or "按主题和逻辑完整性分块"}

段落单元：
{listing}
{repair}

输出格式：
{{"chunks":[{{"title":"块的短标题","paragraph_ids":["p1","p2"]}}]}}
"""


def _extract_groups(payload: dict, units: list[dict[str, Any]]) -> list[list[int]]:
    chunks = payload.get("chunks")
    if not isinstance(chunks, list) or not chunks:
        raise ValueError("chunks 必须是非空数组")
    valid_ids = {unit["id"] for unit in units}
    seen: list[str] = []
    groups: list[list[int]] = []
    index_of = {unit["id"]: i for i, unit in enumerate(units)}
    for chunk in chunks:
        if not isinstance(chunk, dict):
            raise ValueError("chunks 的元素必须是对象")
        ids = chunk.get("paragraph_ids")
        if not isinstance(ids, list) or not ids:
            raise ValueError("每个块必须有 paragraph_ids")
        normalized = [str(item) for item in ids]
        for item in normalized:
            if item not in valid_ids:
                raise ValueError(f"未知的段落 id：{item}")
            if item in seen:
                raise ValueError(f"段落 id 重复：{item}")
            seen.append(item)
        groups.append([index_of[item] for item in normalized])
    if len(seen) != len(units):
        missing = [unit["id"] for unit in units if unit["id"] not in seen]
        raise ValueError(f"有段落未覆盖：{', '.join(missing[:5])}")
    flattened = [i for group in groups for i in group]
    if flattened != sorted(flattened):
        raise ValueError("段落 id 顺序必须递增")
    return groups


class AIChunker:
    name = "ai"

    def __init__(self, json_caller: JsonCaller | None = None) -> None:
        self._json_caller = json_caller or _default_json_caller

    def chunk(self, blocks: list[Block], config: ChunkConfig) -> list[ChunkDraft]:
        units = _paragraph_units(blocks)
        if not units:
            return []
        if len(units) == 1:
            unit = units[0]
            return [
                ChunkDraft(
                    text=unit["text"],
                    heading_path=unit["heading_path"],
                    page=unit["page"],
                    meta={"strategy": "ai", "fallback": False},
                )
            ]

        groups: list[list[int]] | None = None
        error = ""
        last_payload: dict | None = None
        for attempt in range(3):
            prompt = _build_prompt(units, config.ai_instructions, error)
            try:
                payload = self._json_caller(prompt)
                last_payload = payload
                groups = _extract_groups(payload, units)
                break
            except Exception as exc:  # noqa: BLE001
                error = str(exc)
                logger.warning("AI chunking attempt {} failed: {}", attempt + 1, exc)

        if groups is None:
            logger.warning("AI chunking failed after retries, falling back to recursive")
            from app.services.chunking.recursive import RecursiveChunker

            drafts = RecursiveChunker().chunk(blocks, config)
            for draft in drafts:
                draft.meta["ai_fallback"] = True
                draft.meta["fallback_reason"] = error
            return drafts

        payload_chunks = []
        if isinstance(last_payload, dict) and isinstance(last_payload.get("chunks"), list):
            payload_chunks = last_payload["chunks"]

        drafts: list[ChunkDraft] = []
        for group_index, group in enumerate(groups):
            title = ""
            if group_index < len(payload_chunks) and isinstance(payload_chunks[group_index], dict):
                title = str(payload_chunks[group_index].get("title") or "")[:120]
            texts = [units[i]["text"] for i in group]
            text = normalize_draft_text("\n".join(texts))
            if not text:
                continue
            first = units[group[0]]
            if len(text) > int(config.chunk_size * 1.5):
                for part in split_long_text(text, config.chunk_size, config.chunk_overlap):
                    chunk = normalize_draft_text(part)
                    if chunk:
                        drafts.append(
                            ChunkDraft(
                                text=chunk,
                                heading_path=first["heading_path"],
                                page=first["page"],
                                meta={"strategy": "ai", "split_long": True, "title": title},
                            )
                        )
                continue
            drafts.append(
                ChunkDraft(
                    text=text,
                    heading_path=first["heading_path"],
                    page=first["page"],
                    meta={
                        "strategy": "ai",
                        "paragraph_ids": [units[i]["id"] for i in group],
                        "title": title,
                    },
                )
            )
        return drafts
