"""Short-term conversation state (chat sessions and messages).

Thin, user-scoped facade over the runtime repository. Every read and delete is
filtered by the owning user so one user can never list, read or delete
another user's conversation.
"""

from __future__ import annotations

from app.services.runtime_store import RuntimeStore, runtime_store

DEFAULT_USER = "local-user"


def initialize_chat_store(store: RuntimeStore = runtime_store) -> None:
    store.initialize()


def create_session(
    title: str = "New chat",
    session_id: str | None = None,
    *,
    user_id: str = DEFAULT_USER,
    store: RuntimeStore = runtime_store,
) -> dict:
    return store.create_chat_session(user_id=user_id, title=title, session_id=session_id)


def ensure_session(
    session_id: str | None,
    title: str,
    *,
    user_id: str = DEFAULT_USER,
    store: RuntimeStore = runtime_store,
) -> dict:
    if session_id:
        session = store.get_chat_session(session_id, user_id=user_id)
        if session:
            return session
        if store.chat_session_exists(session_id):
            # The id belongs to someone else: never attach to it, start fresh.
            session_id = None
    return store.create_chat_session(user_id=user_id, title=title, session_id=session_id)


def get_session(
    session_id: str, *, user_id: str = DEFAULT_USER, store: RuntimeStore = runtime_store
) -> dict | None:
    return store.get_chat_session(session_id, user_id=user_id)


def list_sessions(*, user_id: str = DEFAULT_USER, store: RuntimeStore = runtime_store) -> list[dict]:
    return store.list_chat_sessions(user_id=user_id)


def add_message(
    session_id: str,
    role: str,
    content: str,
    attachment_name: str | None = None,
    *,
    store: RuntimeStore = runtime_store,
) -> dict:
    return store.add_chat_message(session_id, role, content, attachment_name)


def list_messages(
    session_id: str,
    *,
    user_id: str = DEFAULT_USER,
    limit: int | None = None,
    store: RuntimeStore = runtime_store,
) -> list[dict]:
    return store.list_chat_messages(session_id, user_id=user_id, limit=limit)


def delete_session(
    session_id: str, *, user_id: str = DEFAULT_USER, store: RuntimeStore = runtime_store
) -> bool:
    return store.delete_chat_session(session_id, user_id=user_id)
