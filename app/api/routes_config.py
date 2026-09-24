from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.config import (
    is_prompt_customized,
    load_config,
    load_system_prompt,
    reset_system_prompt,
    save_config,
    save_system_prompt,
    update_config,
)

router = APIRouter(prefix="/api/config", tags=["config"])


class SystemPromptBody(BaseModel):
    text: str


def _apply_interval(request: Request, previous: dict, config: dict) -> None:
    # The scheduler reads the interval only at startup - apply a change right away instead of
    # silently waiting for the next service restart.
    previous_interval = previous.get("agent", {}).get("loop_interval_seconds")
    interval = config.get("agent", {}).get("loop_interval_seconds")
    if interval and interval != previous_interval:
        request.app.state.scheduler.reschedule(int(interval))


@router.get("")
def get_config() -> dict:
    return load_config()


@router.put("")
def put_config(config: dict, request: Request) -> dict:
    """Replaces the whole config; keys left out fall back to their defaults."""
    previous = load_config()
    save_config(config)
    _apply_interval(request, previous, config)
    return {"status": "ok"}


@router.patch("")
def patch_config(partial: dict, request: Request) -> dict:
    """Deep-merges a partial config (dicts recursively, lists/values replaced), returns the result."""
    previous = load_config()
    config = update_config(partial)
    _apply_interval(request, previous, config)
    return config


@router.get("/system_prompt")
def get_system_prompt() -> dict:
    return {"text": load_system_prompt(), "customized": is_prompt_customized()}


@router.put("/system_prompt")
def put_system_prompt(body: SystemPromptBody) -> dict:
    save_system_prompt(body.text)
    return {"status": "ok", "customized": is_prompt_customized()}


@router.post("/system_prompt/reset")
def post_reset_system_prompt() -> dict:
    return {"text": reset_system_prompt(), "customized": False}
