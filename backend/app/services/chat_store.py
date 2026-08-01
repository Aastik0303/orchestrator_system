from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

DB_PATH = Path("storage/runtime.db")
DB_PATH.parent.mkdir(parents=True, exist_ok=True)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def initialize_chat_store() -> None:
    with _connect() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_sessions (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_messages (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                attachment_name TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(session_id) REFERENCES chat_sessions(id) ON DELETE CASCADE
            )
            """
        )


def create_session(title: str = "New chat", session_id: str | None = None) -> dict:
    initialize_chat_store()
    now = _now()
    chat_id = session_id or f"chat_{uuid4().hex[:12]}"
    with _connect() as connection:
        connection.execute(
            """
            INSERT OR IGNORE INTO chat_sessions (id, title, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            (chat_id, title.strip()[:80] or "New chat", now, now),
        )
        row = connection.execute(
            "SELECT id, title, created_at, updated_at FROM chat_sessions WHERE id = ?",
            (chat_id,),
        ).fetchone()
    return dict(row)


def ensure_session(session_id: str | None, title: str) -> dict:
    if session_id:
        session = get_session(session_id)
        if session:
            return session
    return create_session(title=title, session_id=session_id)


def get_session(session_id: str) -> dict | None:
    initialize_chat_store()
    with _connect() as connection:
        row = connection.execute(
            "SELECT id, title, created_at, updated_at FROM chat_sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
    return dict(row) if row else None


def list_sessions() -> list[dict]:
    initialize_chat_store()
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT
                s.id,
                CASE
                    WHEN s.title = 'New chat' THEN COALESCE(
                        (
                            SELECT m2.content
                            FROM chat_messages m2
                            WHERE m2.session_id = s.id AND m2.role = 'user'
                            ORDER BY m2.created_at ASC
                            LIMIT 1
                        ),
                        s.title
                    )
                    ELSE s.title
                END AS title,
                s.created_at,
                s.updated_at,
                COUNT(m.id) AS message_count
            FROM chat_sessions s
            LEFT JOIN chat_messages m ON m.session_id = s.id
            GROUP BY s.id
            HAVING COUNT(m.id) > 0
            ORDER BY s.updated_at DESC
            """
        ).fetchall()
    return [dict(row) for row in rows]


def add_message(session_id: str, role: str, content: str, attachment_name: str | None = None) -> dict:
    initialize_chat_store()
    now = _now()
    message_id = f"msg_{uuid4().hex[:12]}"
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO chat_messages (id, session_id, role, content, attachment_name, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (message_id, session_id, role, content, attachment_name, now),
        )
        connection.execute(
            "UPDATE chat_sessions SET updated_at = ? WHERE id = ?",
            (now, session_id),
        )
        row = connection.execute(
            """
            SELECT id, session_id, role, content, attachment_name, created_at
            FROM chat_messages
            WHERE id = ?
            """,
            (message_id,),
        ).fetchone()
    return dict(row)


def list_messages(session_id: str) -> list[dict]:
    initialize_chat_store()
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT id, session_id, role, content, attachment_name, created_at
            FROM chat_messages
            WHERE session_id = ?
            ORDER BY created_at ASC
            """,
            (session_id,),
        ).fetchall()
    return [dict(row) for row in rows]
