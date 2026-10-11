"""SQLite persistence; each operation owns a connection and its transaction."""

from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import sqlite3


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


CHAT_FIELDS = "id, title, model_id, created_at, updated_at"
MESSAGE_FIELDS = (
    "id, chat_id, role, content, status, model_id, created_at, "
    "duration_ms, time_to_first_token_ms, prompt_tokens, completion_tokens"
)


class ChatNotFound(Exception):
    pass


class Storage:
    def __init__(self, path: str | Path):
        self.path = str(path)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def initialize(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS chats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    model_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
                    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                    content TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('completed', 'failed', 'interrupted')),
                    model_id TEXT,
                    created_at TEXT NOT NULL,
                    duration_ms INTEGER,
                    time_to_first_token_ms INTEGER,
                    prompt_tokens INTEGER,
                    completion_tokens INTEGER
                );
                CREATE INDEX IF NOT EXISTS messages_chat_order
                    ON messages(chat_id, created_at, id);
                """
            )

    def list_chats(self) -> list[dict]:
        with closing(self._connect()) as db:
            rows = db.execute(
                f"SELECT {CHAT_FIELDS} FROM chats ORDER BY updated_at DESC, id DESC"
            ).fetchall()
            return [dict(row) for row in rows]

    def get_chat(self, chat_id: int) -> dict:
        with closing(self._connect()) as db:
            row = db.execute(f"SELECT {CHAT_FIELDS} FROM chats WHERE id = ?", (chat_id,)).fetchone()
            if row is None:
                raise ChatNotFound(chat_id)
            return dict(row)

    def create_chat(self, title: str, model_id: str | None) -> dict:
        now = _timestamp()
        with closing(self._connect()) as db, db:
            cursor = db.execute(
                "INSERT INTO chats(title, model_id, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (title, model_id, now, now),
            )
            return dict(db.execute(f"SELECT {CHAT_FIELDS} FROM chats WHERE id = ?", (cursor.lastrowid,)).fetchone())

    def update_chat(self, chat_id: int, changes: dict) -> dict:
        with closing(self._connect()) as db, db:
            if db.execute("SELECT 1 FROM chats WHERE id = ?", (chat_id,)).fetchone() is None:
                raise ChatNotFound(chat_id)
            if changes:
                values = list(changes.values())
                db.execute(
                    f"UPDATE chats SET {', '.join(f'{key} = ?' for key in changes)}, updated_at = ? WHERE id = ?",
                    (*values, _timestamp(), chat_id),
                )
            row = db.execute(f"SELECT {CHAT_FIELDS} FROM chats WHERE id = ?", (chat_id,)).fetchone()
            return dict(row)

    def delete_chat(self, chat_id: int) -> None:
        with closing(self._connect()) as db, db:
            cursor = db.execute("DELETE FROM chats WHERE id = ?", (chat_id,))
            if cursor.rowcount == 0:
                raise ChatNotFound(chat_id)

    def list_messages(self, chat_id: int) -> list[dict]:
        with closing(self._connect()) as db:
            if db.execute("SELECT 1 FROM chats WHERE id = ?", (chat_id,)).fetchone() is None:
                raise ChatNotFound(chat_id)
            return [dict(row) for row in db.execute(
                f"SELECT {MESSAGE_FIELDS} FROM messages WHERE chat_id = ? ORDER BY created_at, id",
                (chat_id,),
            )]

    def begin_turn(self, chat_id: int, content: str, model_id: str | None) -> tuple[dict, dict, list[dict]]:
        """Commit the user and an interrupted placeholder before any network activity."""
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM chats WHERE id = ?", (chat_id,)).fetchone() is None:
                raise ChatNotFound(chat_id)
            first_message = db.execute(
                "SELECT 1 FROM messages WHERE chat_id = ? AND role = 'user' LIMIT 1",
                (chat_id,),
            ).fetchone() is None
            now = _timestamp()
            user_id = db.execute(
                "INSERT INTO messages(chat_id, role, content, status, created_at) "
                "VALUES (?, 'user', ?, 'completed', ?)",
                (chat_id, content, now),
            ).lastrowid
            assistant_id = db.execute(
                "INSERT INTO messages(chat_id, role, content, status, model_id, created_at) "
                "VALUES (?, 'assistant', '', 'interrupted', ?, ?)",
                (chat_id, model_id, now),
            ).lastrowid
            if first_message:
                db.execute("UPDATE chats SET title = ?, updated_at = ? WHERE id = ?", (content.strip(), now, chat_id))
            else:
                db.execute("UPDATE chats SET updated_at = ? WHERE id = ?", (now, chat_id))
            prior = [dict(row) for row in db.execute(
                f"SELECT {MESSAGE_FIELDS} FROM messages WHERE chat_id = ? AND id <= ? "
                "ORDER BY created_at, id",
                (chat_id, user_id),
            )]
            user = dict(db.execute(f"SELECT {MESSAGE_FIELDS} FROM messages WHERE id = ?", (user_id,)).fetchone())
            assistant = dict(db.execute(f"SELECT {MESSAGE_FIELDS} FROM messages WHERE id = ?", (assistant_id,)).fetchone())
            return user, assistant, prior

    def finish_turn(
        self,
        message_id: int,
        content: str,
        status: str,
        duration_ms: int | None,
        first_token_ms: int | None,
        prompt_tokens: int | None,
        completion_tokens: int | None,
    ) -> dict:
        with closing(self._connect()) as db, db:
            db.execute(
                "UPDATE messages SET content = ?, status = ?, duration_ms = ?, "
                "time_to_first_token_ms = ?, prompt_tokens = ?, completion_tokens = ? WHERE id = ?",
                (content, status, duration_ms, first_token_ms, prompt_tokens, completion_tokens, message_id),
            )
            row = db.execute(f"SELECT {MESSAGE_FIELDS} FROM messages WHERE id = ?", (message_id,)).fetchone()
            return dict(row) if row else {}
