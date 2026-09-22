from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from app.config import load_config, load_system_prompt, save_config, save_system_prompt

router = APIRouter(prefix="/api/config", tags=["config"])


class SystemPromptBody(BaseModel):
    text: str


@router.get("")
def get_config() -> dict:
    return load_config()


@router.put("")
def put_config(config: dict) -> dict:
    save_config(config)
    return {"status": "ok"}


@router.get("/system_prompt")
def get_system_prompt() -> dict:
    return {"text": load_system_prompt()}


@router.put("/system_prompt")
def put_system_prompt(body: SystemPromptBody) -> dict:
    save_system_prompt(body.text)
    return {"status": "ok"}
