"""Parent-child chunk wrapping."""
from __future__ import annotations

from app.services.chunking.base import ChunkConfig, ChunkDraft
from app.services.parsing import Block


def chunk_parent_child(blocks: list[Block], config: ChunkConfig) -> list[ChunkDraft]:
    from app.services.chunking.registry import get_chunker
    from app.services.chunking.structure import StructureChunker

    parent_config = ChunkConfig(
        strategy="structure",
        chunk_size=config.parent_size,
        chunk_overlap=0,
        min_chunk_size=config.min_chunk_size,
        parent_child=False,
        parent_size=config.parent_size,
    )
    parents = StructureChunker().chunk(blocks, parent_config)
    if not parents:
        return []

    child_config = config.model_copy(update={"parent_child": False})
    child_chunker = get_chunker(config.strategy)

    drafts: list[ChunkDraft] = []
    for index, parent in enumerate(parents):
        parent_key = f"p{index}"
        drafts.append(
            ChunkDraft(
                text=parent.text,
                heading_path=parent.heading_path,
                page=parent.page,
                meta={**parent.meta, "strategy": config.strategy, "parent": True},
                level=0,
                parent_key=parent_key,
            )
        )
        synthetic = [
            Block(
                text=parent.text,
                category="NarrativeText",
                heading_path=parent.heading_path,
                page=parent.page,
            )
        ]
        children = child_chunker.chunk(synthetic, child_config)
        for child in children:
            child.level = 1
            child.parent_key = parent_key
            drafts.append(child)

    return drafts
