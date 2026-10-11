"""Versioned SQLite + two FAISS IDMap2 indexes with atomic publication."""

from __future__ import annotations

import os
import tempfile
import time
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path

import faiss
import numpy as np

from .chunking import FIXED_SIZE, OVERLAP, Chunk, chunk_documents
from .database import Database
from .embeddings import LMStudioClient
from .extract import Document
from .retrieval import SearchHit

STRATEGIES = ("fixed", "structural")
BATCH_SIZE = 16
Progress = Callable[[str, float], None]


def _matrix(vectors, expected_rows: int, dimension: int | None = None) -> np.ndarray:
    try:
        result = np.asarray(vectors, dtype=np.float32)
    except (TypeError, ValueError) as exc:
        raise ValueError("Эмбеддинги содержат векторы несовместимой размерности.") from exc
    if result.ndim != 2 or result.shape[0] != expected_rows or result.shape[1] < 1:
        raise ValueError("Неверное число или размерность эмбеддингов.")
    if dimension is not None and result.shape[1] != dimension:
        raise ValueError(f"Размерность модели изменилась: ожидалась {dimension}, получена {result.shape[1]}.")
    if not np.isfinite(result).all():
        raise ValueError("Эмбеддинг содержит NaN или бесконечность.")
    norm = np.linalg.norm(result.astype(np.float64), axis=1)
    if not np.isfinite(norm).all() or np.any(norm <= 0):
        raise ValueError("Нулевой или бесконечный вектор нельзя нормализовать.")
    result = np.ascontiguousarray(result)
    faiss.normalize_L2(result)
    normalized_norm = np.linalg.norm(result, axis=1)
    if not np.isfinite(result).all() or not np.isfinite(normalized_norm).all() or np.any(normalized_norm <= 0):
        raise ValueError("Нормализация эмбеддингов не удалась.")
    return result


class Store:
    def __init__(self, db_path: str | Path = "data/app.db", data_dir: str | Path = "data"):
        self.db = Database(db_path)
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._loaded: dict[tuple[str, str], faiss.Index] = {}

    def build(self, documents: Sequence[Document], client: LMStudioClient, embedding_model: str,
              callback: Progress | None = None, size: int = FIXED_SIZE,
              overlap: int = OVERLAP) -> dict[str, dict]:
        """Stage both strategies; switch SQLite pointer only after both files exist."""
        if not documents or not all(isinstance(doc, Document) for doc in documents):
            raise ValueError("Добавьте извлечённые документы перед индексацией.")
        if len({doc.source for doc in documents}) != len(documents):
            raise ValueError("Имена источников документов должны быть уникальными.")
        if not 0 <= overlap < size:
            raise ValueError("Размер чанка должен быть больше перекрытия.")
        client.ensure_model(embedding_model)
        notify = callback or (lambda _stage, _fraction: None)
        notify("Разбиение документов", 0.05)
        groups = {strategy: chunk_documents(list(documents), strategy, size, overlap) for strategy in STRATEGIES}
        if any(not group for group in groups.values()):
            raise ValueError("Из документов не удалось получить чанки для обеих стратегий.")
        version = uuid.uuid4().hex
        total = sum(len(group) for group in groups.values())
        embedded = 0
        dimension: int | None = None
        records: dict[str, tuple[str, list[Chunk], float]] = {}
        temp_paths: list[Path] = []
        final_paths: list[Path] = []
        published = False
        try:
            for strategy, chunks in groups.items():
                started = time.perf_counter()
                index: faiss.Index | None = None
                for first in range(0, len(chunks), BATCH_SIZE):
                    batch = chunks[first:first + BATCH_SIZE]
                    vectors = _matrix(client.embed([chunk.text for chunk in batch], embedding_model),
                                      len(batch), dimension)
                    if dimension is None:
                        dimension = int(vectors.shape[1])
                    if index is None:
                        index = faiss.IndexIDMap2(faiss.IndexFlatIP(dimension))
                    index.add_with_ids(vectors,
                                       np.asarray([chunk.vector_id for chunk in batch], dtype=np.int64))
                    embedded += len(batch)
                    notify(f"Эмбеддинги: {strategy}", 0.1 + 0.8 * embedded / total)
                assert index is not None and dimension is not None
                filename = f"index_{strategy}_{version}.faiss"
                with tempfile.NamedTemporaryFile(dir=self.data_dir, prefix=".index_", suffix=".faiss",
                                                  delete=False) as temporary:
                    temp = Path(temporary.name)
                temp_paths.append(temp)
                faiss.write_index(index, str(temp))
                with temp.open("rb") as file:
                    os.fsync(file.fileno())
                records[strategy] = (filename, chunks, time.perf_counter() - started)
            notify("Сохранение обоих индексов", 0.94)
            with self.db.connect() as db:
                self.db.save_generation(db, version, embedding_model, dimension, size, overlap, records)
            for strategy, temp in zip(STRATEGIES, temp_paths):
                target = self.data_dir / records[strategy][0]
                os.replace(temp, target)
                final_paths.append(target)
            # Flush the containing directory before exposing its filenames in SQLite.
            if hasattr(os, "O_DIRECTORY"):
                directory_fd = os.open(self.data_dir, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            notify("Публикация версии", 0.98)
            self.db.publish(version)
            published = True
        finally:
            for path in temp_paths:
                path.unlink(missing_ok=True)
            if not published:
                for path in final_paths:
                    path.unlink(missing_ok=True)
                with self.db.connect() as db:
                    db.execute("DELETE FROM chunks WHERE version=?", (version,))
                    db.execute("DELETE FROM indexes WHERE version=?", (version,))
                    db.execute("DELETE FROM generations WHERE version=?", (version,))
        return self.stats()

    def _active(self, embedding_model: str | None = None) -> dict:
        active = self.db.active()
        if active is None:
            raise ValueError("Индекс не создан: сначала проиндексируйте документы.")
        if embedding_model is not None and embedding_model != active["embedding_model"]:
            raise ValueError("Модель эмбеддингов отличается от модели индекса. Необходима переиндексация.")
        return active

    def _index(self, active: dict, strategy: str):
        if strategy not in STRATEGIES:
            raise ValueError("Неизвестная стратегия поиска.")
        key = (active["version"], strategy)
        info = self.db.index_info(*key)
        if info is None:
            raise ValueError("В выбранной версии отсутствует индекс выбранной стратегии.")
        if key not in self._loaded:
            file = self.data_dir / info["filename"]
            if not file.is_file():
                raise ValueError(f"Файл индекса {file.name} отсутствует; переиндексируйте документы.")
            index = faiss.read_index(str(file))
            if (not isinstance(index, faiss.IndexIDMap2) or index.d != active["dimension"]
                    or index.ntotal != info["chunk_count"]):
                raise ValueError("Индекс FAISS несовместим с метаданными SQLite; требуется переиндексация.")
            self._loaded[key] = index
        return self._loaded[key]

    def search_vector(self, vector, embedding_model: str, strategy: str = "fixed",
                      top_k: int = 5) -> list[SearchHit]:
        active = self._active(embedding_model)
        if top_k < 1:
            raise ValueError("top_k должен быть положительным.")
        query = _matrix(np.asarray(vector).reshape(1, -1), 1, active["dimension"])
        index = self._index(active, strategy)
        scores, ids = index.search(query, min(int(top_k), index.ntotal))
        found = self.db.get_chunks(active["version"], strategy, [int(n) for n in ids[0] if n >= 0])
        hits = []
        for vector_id, score in zip(ids[0], scores[0]):
            if vector_id < 0:
                continue
            row = found.get(int(vector_id))
            if row is None:
                raise ValueError("Вектор FAISS не имеет соответствующего чанка SQLite; требуется переиндексация.")
            hits.append(SearchHit(**{key: row[key] for key in SearchHit.__dataclass_fields__ if key != "score"},
                                  score=float(score)))
        return hits

    def search(self, query: str, client: LMStudioClient, embedding_model: str,
               strategy: str = "fixed", top_k: int = 5) -> list[SearchHit]:
        self._active(embedding_model)  # Reject model mismatch before generating a query vector.
        if not query.strip():
            raise ValueError("Введите непустой вопрос.")
        vector = client.embed([query], embedding_model)[0]
        return self.search_vector(vector, embedding_model, strategy, top_k)

    def stats(self) -> dict[str, dict]:
        active = self.db.active()
        if active is None:
            return {}
        result = {}
        for strategy in STRATEGIES:
            info = self.db.index_info(active["version"], strategy)
            if info is None:
                continue
            mean, median = self.db.size_stats(active["version"], strategy)
            result[strategy] = {
                "count": info["chunk_count"], "mean_chars": mean, "median_chars": median,
                "build_seconds": info["build_seconds"], "built_at": active["created_at"],
                "embedding_model": active["embedding_model"], "dimension": active["dimension"],
                "version": active["version"], "size": active["size"], "overlap": active["overlap"],
            }
        return result

    def list_chunks(self, strategy: str = "fixed", limit: int = 100, offset: int = 0) -> list[Chunk]:
        active = self.db.active()
        if active is None:
            return []
        if strategy not in STRATEGIES or limit < 0 or offset < 0:
            raise ValueError("Неверная стратегия или параметры страницы чанков.")
        return self.db.list_chunks(active["version"], strategy, limit, offset)


def build(documents: Sequence[Document], client: LMStudioClient, embedding_model: str,
          db_path: str | Path = "data/app.db", data_dir: str | Path = "data",
          callback: Progress | None = None, size: int = FIXED_SIZE,
          overlap: int = OVERLAP) -> dict[str, dict]:
    return Store(db_path, data_dir).build(documents, client, embedding_model, callback, size, overlap)
