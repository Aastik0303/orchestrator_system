"""Caller identity.

AUTH_MODE=api_key (required when APP_ENV=production):
    API_KEYS="<sha256-hex-or-plaintext-key>:<user_id>,..."
    Callers send `Authorization: Bearer <key>` or `X-API-Key: <key>`. The user id
    is derived from the key; any user_id supplied in the request is ignored
    unless it matches.

AUTH_MODE=dev (local development only):
    The caller's `X-User-Id` header or `user_id` parameter is trusted, defaulting
    to "local-user". This mode is refused when APP_ENV=production.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from functools import lru_cache

from fastapi import HTTPException, Request, WebSocket

from app.config import get_settings

USER_ID_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,128}$")
DEFAULT_USER = "local-user"


@dataclass(frozen=True)
class Principal:
    user_id: str
    authenticated: bool


@lru_cache
def _key_table(raw: str) -> dict[str, str]:
    table: dict[str, str] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry or ":" not in entry:
            continue
        key, user_id = entry.rsplit(":", 1)
        key = key.strip()
        digest = key.lower() if re.fullmatch(r"[0-9a-fA-F]{64}", key) else hashlib.sha256(key.encode()).hexdigest()
        table[digest] = user_id.strip()
    return table


def _lookup(api_key: str) -> str | None:
    table = _key_table(get_settings().api_keys or "")
    digest = hashlib.sha256(api_key.encode()).hexdigest()
    for known, user_id in table.items():
        if hmac.compare_digest(known, digest):
            return user_id
    return None


def validate_auth_configuration() -> None:
    settings = get_settings()
    mode = settings.auth_mode.lower()
    if mode not in {"dev", "api_key"}:
        raise RuntimeError(f"Unsupported AUTH_MODE: {settings.auth_mode}")
    if settings.is_production and mode == "dev":
        raise RuntimeError("AUTH_MODE=dev is not allowed when APP_ENV=production.")
    if mode == "api_key" and not _key_table(settings.api_keys or ""):
        raise RuntimeError("AUTH_MODE=api_key requires API_KEYS.")


def _resolve(headers, query_user: str | None) -> Principal:
    settings = get_settings()
    if settings.auth_mode.lower() == "api_key":
        authorization = headers.get("authorization", "")
        api_key = authorization[7:].strip() if authorization.lower().startswith("bearer ") else headers.get("x-api-key", "")
        user_id = _lookup(api_key) if api_key else None
        if not user_id:
            raise HTTPException(status_code=401, detail="A valid API key is required.")
        if query_user and query_user not in {user_id, DEFAULT_USER}:
            raise HTTPException(status_code=403, detail="The supplied user_id does not match the API key.")
        return Principal(user_id=user_id, authenticated=True)
    user_id = headers.get("x-user-id") or query_user or DEFAULT_USER
    if not USER_ID_RE.match(user_id):
        raise HTTPException(status_code=422, detail="Invalid user id.")
    return Principal(user_id=user_id, authenticated=False)


def principal_from_request(request: Request, supplied_user_id: str | None = None) -> Principal:
    return _resolve(request.headers, supplied_user_id or request.query_params.get("user_id"))


async def get_principal(request: Request) -> Principal:
    return principal_from_request(request)


def principal_from_websocket(websocket: WebSocket) -> Principal:
    headers = dict(websocket.headers)
    # Browsers cannot set headers on WebSocket upgrades; allow the key as a query param.
    if websocket.query_params.get("api_key") and "x-api-key" not in headers:
        headers["x-api-key"] = websocket.query_params["api_key"]
    return _resolve(headers, websocket.query_params.get("user_id"))
