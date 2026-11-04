"""Semantic chunking: sentence split -> embeddings -> distance breaks."""
from __future__ import annotations

import re
from collections.abc import Callable

from loguru import logger

from app.services.chunking.base import (
    ChunkConfig,
    ChunkDraft,
    build_stream,
    locate,
    normalize_draft_text,
    split_long_text,
)
from app.services.parsing import Block

_SENTENCE_RE = re.compile(r"[^。！？!?；;\n]+[。！？!?；;]?")

Embedder = Callable[[list[str]], list[list[float]]]


def _default_embedder(texts: list[str]) -> list[list[float]]:
    from app.services.embedding import embedding_service

    return embedding_service.encode(texts)


def _percentile(values: list[float], percentile: int) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = (len(ordered) - 1) * (percentile / 100.0)
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = index - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def _cosine_distance(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    return 1.0 - dot


class SemanticChunker:
    name = "semantic"

    def __init__(self, embedder: Embedder | None = None) -> None:
        self._embedder = embedder or _default_embedder

    def chunk(self, blocks: list[Block], config: ChunkConfig) -> list[ChunkDraft]:
        text, segments = build_stream(blocks)
        if not text:
            return []

        sentences: list[tuple[str, str, int | None, int]] = []
        for match in _SENTENCE_RE.finditer(text):
            sentence = normalize_draft_text(match.group(0))
            if len(sentence) < 2:
                continue
            segment = locate(segments, match.start())
            sentences.append((sentence, segment.heading_path, segment.page, match.start()))

        if not sentences:
            return []
        if len(sentences) == 1:
            sentence, heading, page, _ = sentences[0]
            return [
                ChunkDraft(
                    text=sentence,
                    heading_path=heading,
                    page=page,
                    meta={"strategy": "semantic", "sentence_count": 1},
                )
            ]

        try:
            vectors = self._embedder([s[0] for s in sentences])
        except Exception as exc:  # noqa: BLE001
            logger.warning("Semantic chunking embedding failed, falling back: {}", exc)
            from app.services.chunking.recursive import RecursiveChunker

            drafts = RecursiveChunker().chunk(blocks, config)
            for draft in drafts:
                draft.meta["semantic_fallback"] = True
                draft.meta["fallback_reason"] = str(exc)
            return drafts

        distances = [
            _cosine_distance(vectors[i], vectors[i + 1]) for i in range(len(vectors) - 1)
        ]
        threshold = _percentile(distances, config.semantic_percentile)
        breaks = [i + 1 for i, distance in enumerate(distances) if distance >= threshold]
        if not breaks and distances:
            breaks = [max(range(len(distances)), key=lambda i: distances[i]) + 1]
        # Avoid an explosion of tiny chunks.
        max_breaks = max(1, len(sentences) // 5)
        if len(breaks) > max_breaks:
            strongest = sorted(breaks, key=lambda i: distances[i - 1], reverse=True)[:max_breaks]
            breaks = sorted(strongest)

        groups: list[list[tuple[str, str, int | None, int]]] = []
        start = 0
        for break_at in breaks + [len(sentences)]:
            if break_at <= start:
                continue
            groups.append(sentences[start:break_at])
            start = break_at

        drafts: list[ChunkDraft] = []
        for group in groups:
            group_text = "".join(item[0] for item in group)
            heading = group[0][1]
            page = group[0][2]
            if len(group_text) <= config.chunk_size:
                drafts.append(
                    ChunkDraft(
                        text=group_text,
                        heading_path=heading,
                        page=page,
                        meta={
                            "strategy": "semantic",
                            "sentence_count": len(group),
                            "threshold": round(threshold, 4),
                        },
                    )
                )
                continue
            for part in split_long_text(group_text, config.chunk_size, 0):
                chunk = normalize_draft_text(part)
                if chunk:
                    drafts.append(
                        ChunkDraft(
                            text=chunk,
                            heading_path=heading,
                            page=page,
                            meta={
                                "strategy": "semantic",
                                "sentence_count": len(group),
                                "split_long": True,
                            },
                        )
                    )

        # Merge tiny trailing chunks.
        merged: list[ChunkDraft] = []
        for draft in drafts:
            if (
                merged
                and len(draft.text) < config.min_chunk_size
                and len(merged[-1].text) + len(draft.text) <= config.chunk_size
            ):
                merged[-1].text = (merged[-1].text + draft.text).strip()
                continue
            merged.append(draft)
        return merged
