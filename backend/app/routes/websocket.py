import asyncio

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

from app.security.auth import principal_from_websocket
from app.services.runtime_store import TERMINAL_RUN_STATUSES, runtime_store

router = APIRouter(tags=["websocket"])
TERMINAL_STATUSES = set(TERMINAL_RUN_STATUSES)
POLL_SECONDS = 0.25


@router.websocket("/ws/runs/{run_id}")
async def run_events(websocket: WebSocket, run_id: str):
    try:
        principal = principal_from_websocket(websocket)
    except HTTPException:
        await websocket.close(code=4401, reason="Unauthorized.")
        return
    run = await asyncio.to_thread(runtime_store.get_run, run_id, user_id=principal.user_id)
    if not run:
        await websocket.close(code=4404, reason="Run was not found.")
        return
    await websocket.accept()
    sequence = 0
    try:
        while True:
            # Status first, then events: the orchestrator flushes all events
            # before writing a terminal status, so this read order guarantees
            # the final events are delivered before the stream closes.
            status, _ = await asyncio.to_thread(runtime_store.get_run_status, run_id)
            events = await asyncio.to_thread(runtime_store.list_events, run_id, sequence)
            for event in events:
                sequence = max(sequence, event["sequence"])
                await websocket.send_json(event)
            if status in TERMINAL_STATUSES:
                await websocket.send_json({"type": "stream_closed", "run_id": run_id, "status": status})
                await websocket.close(code=1000)
                return
            await asyncio.sleep(POLL_SECONDS)
    except WebSocketDisconnect:
        return
