"""Recursive character chunking."""
from __future__ import annotations

from app.services.chunking.base import (
    ChunkConfig,
    ChunkDraft,
    build_stream,
    locate,
    normalize_draft_text,
    split_long_text,
)
from app.services.parsing import Block


class RecursiveChunker:
    name = "recursive"

    def chunk(self, blocks: list[Block], config: ChunkConfig) -> list[ChunkDraft]:
        text, segments = build_stream(blocks)
        if not text:
            return []
        parts = split_long_text(text, config.chunk_size, config.chunk_overlap)
        drafts: list[ChunkDraft] = []
        cursor = 0
        for part in parts:
            chunk = normalize_draft_text(part)
            if not chunk:
                continue
            start = text.find(chunk, cursor)
            if start < 0:
                start = cursor
            segment = locate(segments, start)
            drafts.append(
                ChunkDraft(
                    text=chunk,
                    heading_path=segment.heading_path,
                    page=segment.page,
                    meta={"strategy": "recursive", "start": start},
                )
            )
            cursor = max(cursor + 1, start + max(1, len(chunk) - config.chunk_overlap))
        return drafts
