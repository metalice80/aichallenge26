"""Side-by-side vector search and source/section retrieval evaluation."""

from __future__ import annotations

import time
from collections.abc import Iterable
from pathlib import PurePosixPath

from .embeddings import LMStudioClient
from .index_store import Store, STRATEGIES


def compare(question: str, store: Store, client: LMStudioClient,
            embedding_model: str, top_k: int = 5) -> dict[str, dict]:
    """Embed once for a fair two-index comparison; timings exclude that shared step."""
    store._active(embedding_model)
    if not question.strip():
        raise ValueError("Введите непустой вопрос для сравнения.")
    query_vector = client.embed([question], embedding_model)[0]
    results: dict[str, dict] = {}
    for strategy in STRATEGIES:
        started = time.perf_counter()
        hits = store.search_vector(query_vector, embedding_model, strategy, top_k)
        results[strategy] = {"hits": hits, "search_seconds": time.perf_counter() - started}
    return results


def hit_at_5(cases: Iterable[dict], store: Store, client: LMStudioClient,
             embedding_model: str) -> dict[str, float]:
    """Fraction retrieving the expected source, independent of chunk section labels."""
    counters = {strategy: 0 for strategy in STRATEGIES}
    count = 0
    for case in cases:
        question = case["question"]
        source = str(case["source"])
        results = compare(question, store, client, embedding_model, top_k=5)
        count += 1
        for strategy in STRATEGIES:
            if any(PurePosixPath(hit.source).name.casefold() == PurePosixPath(source).name.casefold()
                   for hit in results[strategy]["hits"]):
                counters[strategy] += 1
    return {strategy: counters[strategy] / count if count else 0.0 for strategy in STRATEGIES}
