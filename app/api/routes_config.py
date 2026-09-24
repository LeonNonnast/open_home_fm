from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.config import load_config, load_system_prompt, save_config, save_system_prompt

router = APIRouter(prefix="/api/config", tags=["config"])


class SystemPromptBody(BaseModel):
    text: str


@router.get("")
def get_config() -> dict:
    return load_config()


@router.put("")
def put_config(config: dict, request: Request) -> dict:
    previous_interval = load_config().get("agent", {}).get("loop_interval_seconds")
    save_config(config)
    # The scheduler reads the interval only at startup - apply a change right away instead of
    # silently waiting for the next service restart.
    interval = config.get("agent", {}).get("loop_interval_seconds")
    if interval and interval != previous_interval:
        request.app.state.scheduler.reschedule(int(interval))
    return {"status": "ok"}


@router.get("/system_prompt")
def get_system_prompt() -> dict:
    return {"text": load_system_prompt()}


@router.put("/system_prompt")
def put_system_prompt(body: SystemPromptBody) -> dict:
    save_system_prompt(body.text)
    return {"status": "ok"}
