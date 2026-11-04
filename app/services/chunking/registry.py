"""Chunker registry and top-level chunking entry point."""
from __future__ import annotations

from app.services.chunking.base import ChunkConfig, ChunkDraft
from app.services.parsing import Block


def get_chunker(strategy: str):
    if strategy == "fixed":
        from app.services.chunking.fixed import FixedChunker

        return FixedChunker()
    if strategy == "recursive":
        from app.services.chunking.recursive import RecursiveChunker

        return RecursiveChunker()
    if strategy == "semantic":
        from app.services.chunking.semantic import SemanticChunker

        return SemanticChunker()
    if strategy == "structure":
        from app.services.chunking.structure import StructureChunker

        return StructureChunker()
    if strategy == "ai":
        from app.services.chunking.ai import AIChunker

        return AIChunker()
    raise ValueError(f"未知分块策略：{strategy}")


def chunk_document(blocks: list[Block], config: ChunkConfig | dict) -> list[ChunkDraft]:
    if isinstance(config, dict):
        config = ChunkConfig.model_validate(config)
    if config.parent_child:
        from app.services.chunking.parent_child import chunk_parent_child

        drafts = chunk_parent_child(blocks, config)
    else:
        drafts = get_chunker(config.strategy).chunk(blocks, config)

    cleaned: list[ChunkDraft] = []
    for index, draft in enumerate(drafts):
        text = (draft.text or "").strip()
        if not text:
            continue
        draft.text = text
        draft.meta = {**draft.meta, "order": index, "char_count": len(text)}
        cleaned.append(draft)
    return cleaned
