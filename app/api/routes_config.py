from __future__ import annotations

from fastapi import APIRouter
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


@router.get("")
def get_config() -> dict:
    return load_config()


@router.put("")
def put_config(config: dict) -> dict:
    """Replaces the whole config; keys left out fall back to their defaults."""
    save_config(config)
    return {"status": "ok"}


@router.patch("")
def patch_config(partial: dict) -> dict:
    """Deep-merges a partial config (dicts recursively, lists/values replaced), returns the result.

    Everything is re-read per run/segment, so changes apply without a restart."""
    return update_config(partial)


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
