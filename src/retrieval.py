"""Typed search hits and bounded, source-labelled RAG context."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Sequence


@dataclass(frozen=True)
class SearchHit:
    chunk_id: str
    vector_id: int
    source: str
    title: str
    section: str
    page: int | None
    strategy: str
    text: str
    ordinal: int
    score: float


def select_context(hits: Sequence[SearchHit], max_chars: int = 12000) -> tuple[str, list[SearchHit]]:
    """Return the prompt and precisely those (possibly excerpted) hits it contains."""
    if max_chars < 0:
        raise ValueError("Размер контекста должен быть неотрицательным.")
    blocks: list[str] = []
    selected: list[SearchHit] = []
    used = 0
    for hit in hits:
        label = f"[Источник: {hit.source}; раздел: {hit.section}"
        if hit.page is not None:
            label += f"; страница: {hit.page}"
        label += f"; сходство: {hit.score:.3f}]\n"
        remaining = max_chars - used - len(label) - 2
        if remaining < 80:
            break
        excerpt = hit.text[:remaining]
        if len(excerpt) < len(hit.text):
            excerpt = excerpt.rsplit(" ", 1)[0] or excerpt
        selected.append(replace(hit, text=excerpt))
        block = label + excerpt + "\n"
        blocks.append(block)
        used += len(block) + 1
    return "\n".join(blocks), selected


def format_context(hits: Sequence[SearchHit], max_chars: int = 12000) -> str:
    return select_context(hits, max_chars)[0]
