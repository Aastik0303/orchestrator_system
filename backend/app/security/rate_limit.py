"""Abuse controls: per-user request rate and concurrent active runs."""

from __future__ import annotations

import asyncio

from fastapi import HTTPException

from app.config import get_settings
from app.infra.cache import get_rate_limiter
from app.services.runtime_store import RuntimeStore, runtime_store


def enforce_rate_limit(user_id: str, *, scope: str = "chat") -> None:
    limit = get_settings().rate_limit_per_minute
    if limit <= 0:
        return
    allowed, retry_after = get_rate_limiter().hit(f"{scope}:{user_id}", limit=limit, window_seconds=60)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded. Please retry later.",
            headers={"Retry-After": str(retry_after)},
        )


async def enforce_active_run_limit(user_id: str, store: RuntimeStore = runtime_store) -> None:
    limit = get_settings().max_active_runs_per_user
    if limit <= 0:
        return
    active = await asyncio.to_thread(store.count_active_runs, user_id=user_id)
    if active >= limit:
        raise HTTPException(
            status_code=429,
            detail=f"You already have {active} active runs (limit {limit}). Wait for them to finish or cancel one.",
        )
