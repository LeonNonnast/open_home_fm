from __future__ import annotations

import json

from fastapi import APIRouter, BackgroundTasks, Request

from app.config import resolve_path

router = APIRouter(prefix="/api/status", tags=["status"])


@router.get("")
def get_status(request: Request) -> dict:
    state_path = resolve_path("data/state.json")
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else None
    player = getattr(request.app.state, "script_player", None)
    return {
        "state": state,
        "player_current_segment_index": player.current_segment_index if player else None,
    }


@router.post("/trigger")
def trigger(request: Request, background_tasks: BackgroundTasks) -> dict:
    agent_loop = request.app.state.agent_loop
    background_tasks.add_task(agent_loop.run_once)
    return {"status": "triggered"}
