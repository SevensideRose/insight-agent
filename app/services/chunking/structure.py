"""Heading-structure aware chunking."""
from __future__ import annotations

from app.services.chunking.base import (
    ChunkConfig,
    ChunkDraft,
    normalize_draft_text,
    split_long_text,
)
from app.services.parsing import Block


def _sections(blocks: list[Block]) -> list[tuple[str, list[Block]]]:
    sections: list[tuple[str, list[Block]]] = []
    for block in blocks:
        if not (block.text or "").strip():
            continue
        key = block.heading_path or ""
        if not sections or sections[-1][0] != key:
            sections.append((key, [block]))
        else:
            sections[-1][1].append(block)
    return sections


class StructureChunker:
    name = "structure"

    def chunk(self, blocks: list[Block], config: ChunkConfig) -> list[ChunkDraft]:
        sections = _sections(blocks)
        if not sections:
            return []
        has_headings = any(key for key, _ in sections)

        drafts: list[ChunkDraft] = []
        for heading, section_blocks in sections:
            text = normalize_draft_text("\n\n".join(b.text for b in section_blocks))
            if not text:
                continue
            page = next((b.page for b in section_blocks if b.page), None)
            if len(text) <= config.chunk_size:
                drafts.append(
                    ChunkDraft(
                        text=text,
                        heading_path=heading,
                        page=page,
                        meta={"strategy": "structure", "section": True},
                    )
                )
                continue
            for part in split_long_text(text, config.chunk_size, config.chunk_overlap):
                chunk = normalize_draft_text(part)
                if chunk:
                    drafts.append(
                        ChunkDraft(
                            text=chunk,
                            heading_path=heading,
                            page=page,
                            meta={"strategy": "structure", "section": True},
                        )
                    )

        # Merge short sections into the previous draft when they share a
        # heading prefix and fit together.
        merged: list[ChunkDraft] = []
        for draft in drafts:
            if (
                merged
                and len(draft.text) < config.min_chunk_size
                and len(merged[-1].text) + len(draft.text) <= config.chunk_size
                and (
                    merged[-1].heading_path == draft.heading_path
                    or merged[-1].heading_path.startswith(draft.heading_path)
                    or draft.heading_path.startswith(merged[-1].heading_path)
                )
            ):
                merged[-1].text = (merged[-1].text + "\n" + draft.text).strip()
                continue
            merged.append(draft)

        if not has_headings:
            for draft in merged:
                draft.meta["structure_fallback"] = True
        return merged
