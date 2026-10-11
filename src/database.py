"""SQLite metadata and durable conversation history for a versioned corpus."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Sequence

from .chunking import Chunk


class Database:
    def __init__(self, path: str | Path = "data/app.db"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS generations (
                    version TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                    embedding_model TEXT NOT NULL,
                    dimension INTEGER NOT NULL CHECK (dimension > 0),
                    size INTEGER NOT NULL DEFAULT 1000,
                    overlap INTEGER NOT NULL DEFAULT 150
                );
                CREATE TABLE IF NOT EXISTS active_generation (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    version TEXT NOT NULL REFERENCES generations(version)
                );
                CREATE TABLE IF NOT EXISTS indexes (
                    version TEXT NOT NULL REFERENCES generations(version),
                    strategy TEXT NOT NULL CHECK (strategy IN ('fixed','structural')),
                    filename TEXT NOT NULL,
                    chunk_count INTEGER NOT NULL,
                    build_seconds REAL NOT NULL,
                    PRIMARY KEY(version, strategy)
                );
                CREATE TABLE IF NOT EXISTS chunks (
                    version TEXT NOT NULL REFERENCES generations(version),
                    chunk_id TEXT NOT NULL,
                    vector_id INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    title TEXT NOT NULL,
                    section TEXT NOT NULL,
                    page INTEGER,
                    strategy TEXT NOT NULL CHECK (strategy IN ('fixed','structural')),
                    text TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    PRIMARY KEY(version, chunk_id),
                    UNIQUE(version, strategy, vector_id)
                );
                CREATE INDEX IF NOT EXISTS chunks_strategy ON chunks(version, strategy, source, ordinal);
                CREATE TABLE IF NOT EXISTS conversations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id INTEGER NOT NULL REFERENCES conversations(id),
                    role TEXT NOT NULL CHECK (role IN ('user','assistant')),
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                    rag INTEGER NOT NULL CHECK (rag IN (0,1)),
                    strategy TEXT,
                    hits_json TEXT NOT NULL DEFAULT '[]'
                );
                CREATE INDEX IF NOT EXISTS messages_conversation ON messages(conversation_id, id);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def active(self) -> dict | None:
        with self.connect() as db:
            row = db.execute("""SELECT g.version, g.created_at, g.embedding_model, g.dimension,
                                       g.size, g.overlap
                                FROM active_generation a JOIN generations g ON g.version=a.version
                                WHERE a.singleton=1""").fetchone()
            return dict(row) if row else None

    def save_generation(self, db: sqlite3.Connection, version: str, model: str,
                        dimension: int, size: int, overlap: int,
                        records: dict[str, tuple[str, Sequence[Chunk], float]]) -> None:
        """Stage a complete generation, but do not expose it to any reader yet."""
        db.execute("INSERT INTO generations(version,embedding_model,dimension,size,overlap) VALUES (?,?,?,?,?)",
                   (version, model, dimension, size, overlap))
        for strategy, (filename, chunks, seconds) in records.items():
            db.execute("INSERT INTO indexes(version,strategy,filename,chunk_count,build_seconds) VALUES (?,?,?,?,?)",
                       (version, strategy, filename, len(chunks), seconds))
            db.executemany("""INSERT INTO chunks(version,chunk_id,vector_id,source,title,section,page,strategy,text,ordinal)
                              VALUES (?,?,?,?,?,?,?,?,?,?)""",
                           ((version, c.chunk_id, c.vector_id, c.source, c.title, c.section,
                             c.page, c.strategy, c.text, c.ordinal) for c in chunks))

    def publish(self, version: str) -> None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""INSERT INTO active_generation(singleton,version) VALUES (1,?)
                          ON CONFLICT(singleton) DO UPDATE SET version=excluded.version""", (version,))

    def index_info(self, version: str, strategy: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM indexes WHERE version=? AND strategy=?", (version, strategy)).fetchone()
            return dict(row) if row else None

    def get_chunks(self, version: str, strategy: str, ids: Sequence[int]) -> dict[int, dict]:
        if not ids:
            return {}
        with self.connect() as db:
            result = db.execute("""SELECT * FROM chunks WHERE version=? AND strategy=? AND vector_id IN (""" +
                                ",".join("?" for _ in ids) + ")", (version, strategy, *ids)).fetchall()
        return {int(row["vector_id"]): dict(row) for row in result}

    def list_chunks(self, version: str, strategy: str, limit: int, offset: int) -> list[Chunk]:
        with self.connect() as db:
            rows = db.execute("""SELECT chunk_id,vector_id,source,title,section,page,strategy,text,ordinal
                                 FROM chunks WHERE version=? AND strategy=? ORDER BY source,ordinal LIMIT ? OFFSET ?""",
                              (version, strategy, limit, offset)).fetchall()
        return [Chunk(**dict(row)) for row in rows]

    def size_stats(self, version: str, strategy: str) -> tuple[float, float]:
        with self.connect() as db:
            sizes = [row[0] for row in db.execute("SELECT length(text) FROM chunks WHERE version=? AND strategy=?",
                                                  (version, strategy))]
        if not sizes:
            return 0.0, 0.0
        import statistics
        return statistics.mean(sizes), statistics.median(sizes)

    def create_conversation(self, title: str | None = None) -> int:
        with self.connect() as db:
            cursor = db.execute("INSERT INTO conversations(title) VALUES (?)", (title or "Новый диалог",))
            return int(cursor.lastrowid)

    def list_conversations(self) -> list[dict]:
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT id,title,created_at FROM conversations ORDER BY id DESC")]

    def history(self, conversation_id: int) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM messages WHERE conversation_id=? ORDER BY id", (conversation_id,))
            return [{**dict(row), "rag": bool(row["rag"]), "hits": json.loads(row["hits_json"])}
                    for row in rows]

    def save_turn(self, conversation_id: int, question: str, answer: str,
                  rag: bool, strategy: str | None, hits: list[dict]) -> None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            conversation = db.execute("SELECT title FROM conversations WHERE id=?", (conversation_id,)).fetchone()
            if conversation is None:
                raise ValueError("Диалог не найден.")
            if (conversation["title"] == "Новый диалог"
                    and not db.execute("SELECT 1 FROM messages WHERE conversation_id=? LIMIT 1",
                                       (conversation_id,)).fetchone()):
                db.execute("UPDATE conversations SET title=? WHERE id=?",
                           (question.strip(), conversation_id))
            db.execute("""INSERT INTO messages(conversation_id,role,content,rag,strategy,hits_json)
                          VALUES (?, 'user', ?, ?, ?, '[]')""",
                       (conversation_id, question, int(rag), strategy))
            db.execute("""INSERT INTO messages(conversation_id,role,content,rag,strategy,hits_json)
                          VALUES (?, 'assistant', ?, ?, ?, ?)""",
                       (conversation_id, answer, int(rag), strategy, json.dumps(hits, ensure_ascii=False)))
