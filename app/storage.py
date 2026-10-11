"""SQLite storage; opening an existing database never discards legacy data."""
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path


def initialize(path: str):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        migrating = 'chats' not in tables and ('messages' in tables or 'legacy_messages' in tables)
        if migrating and 'messages' in tables:
            db.execute('ALTER TABLE messages RENAME TO legacy_messages')
        db.executescript('''
        CREATE TABLE IF NOT EXISTS profiles (
          id INTEGER PRIMARY KEY, name TEXT NOT NULL, model TEXT NOT NULL,
          variant TEXT NOT NULL, temperature REAL NOT NULL, max_tokens INTEGER NOT NULL,
          context_length INTEGER NOT NULL, system_prompt TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS chats (
          id INTEGER PRIMARY KEY, title TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS messages (
          id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
          role TEXT NOT NULL, content TEXT NOT NULL, status TEXT NOT NULL, error TEXT,
          settings TEXT, total_seconds REAL, first_token_seconds REAL,
          prompt_tokens INTEGER, completion_tokens INTEGER, load_seconds REAL,
          ram_after_bytes INTEGER, ram_peak_bytes INTEGER, ram_reason TEXT,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS test_suites (
          id INTEGER PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS test_cases (
          id INTEGER PRIMARY KEY, suite_id INTEGER NOT NULL REFERENCES test_suites(id) ON DELETE CASCADE,
          question TEXT NOT NULL, criteria TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS experiment_runs (
          id INTEGER PRIMARY KEY, suite_id INTEGER NOT NULL REFERENCES test_suites(id),
          baseline_settings TEXT NOT NULL, optimized_settings TEXT NOT NULL,
          repeats INTEGER NOT NULL, status TEXT NOT NULL, error TEXT,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS experiment_results (
          id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES experiment_runs(id) ON DELETE CASCADE,
          case_id INTEGER NOT NULL REFERENCES test_cases(id), side TEXT NOT NULL,
          repeat_index INTEGER NOT NULL, question TEXT NOT NULL, criteria TEXT NOT NULL,
          answer TEXT NOT NULL DEFAULT '', status TEXT NOT NULL, error TEXT,
          settings TEXT NOT NULL, total_seconds REAL, first_token_seconds REAL,
          prompt_tokens INTEGER, completion_tokens INTEGER, tokens_per_second REAL,
          load_seconds REAL, ram_after_bytes INTEGER, ram_peak_bytes INTEGER,
          ram_reason TEXT, accuracy INTEGER, completeness INTEGER, format_score INTEGER);
        ''')
        if 'error' not in {column[1] for column in db.execute('PRAGMA table_info(messages)')}:
            db.execute('ALTER TABLE messages ADD COLUMN error TEXT')
        if migrating:
            db.execute('INSERT INTO chats(id,title,created_at) SELECT id,title,created_at FROM conversations')
            db.execute('''INSERT INTO messages(id,chat_id,role,content,status,created_at)
                SELECT id,conversation_id,role,content,'legacy',created_at FROM legacy_messages''')
        db.execute("UPDATE experiment_runs SET status='interrupted',error='Приложение перезапущено во время опыта' WHERE status='running'")


@contextmanager
def connect(path: str):
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    try:
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def one(db, query, params=()):
    row = db.execute(query, params).fetchone()
    return dict(row) if row else None


def many(db, query, params=()):
    return [dict(row) for row in db.execute(query, params)]


def decode(row, *columns):
    if row:
        for column in columns:
            if row.get(column) is not None:
                row[column] = json.loads(row[column])
    return row
