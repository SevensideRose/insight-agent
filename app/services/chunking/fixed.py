"""Fixed-size character chunking."""
from __future__ import annotations

from app.services.chunking.base import (
    ChunkConfig,
    ChunkDraft,
    build_stream,
    locate,
    normalize_draft_text,
)
from app.services.parsing import Block

_BOUNDARY = "。！？!?；;\n"


def _adjusted_end(text: str, start: int, end: int) -> int:
    window_start = max(start, end - max(1, int((end - start) * 0.15)))
    for index in range(end - 1, window_start - 1, -1):
        if text[index] in _BOUNDARY:
            candidate = index + 1
            if candidate - start >= 50:
                return candidate
            break
    return end


class FixedChunker:
    name = "fixed"

    def chunk(self, blocks: list[Block], config: ChunkConfig) -> list[ChunkDraft]:
        text, segments = build_stream(blocks)
        if not text:
            return []
        size = config.chunk_size
        overlap = config.chunk_overlap
        drafts: list[ChunkDraft] = []
        start = 0
        while start < len(text):
            end = min(len(text), start + size)
            end = _adjusted_end(text, start, end)
            if end <= start:
                end = min(len(text), start + size)
            chunk = normalize_draft_text(text[start:end])
            if chunk:
                segment = locate(segments, start)
                drafts.append(
                    ChunkDraft(
                        text=chunk,
                        heading_path=segment.heading_path,
                        page=segment.page,
                        meta={"strategy": "fixed", "start": start, "end": end},
                    )
                )
            if end >= len(text):
                break
            start = max(start + 1, end - overlap)
        return drafts
