"""Desks: status, settings, prompt and "run now" (through the scheduler, i.e. lock + cap)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.agent.desk import DESKS, DeskConfig
from app.config import (
    is_prompt_customized,
    load_config,
    load_default_prompt,
    load_system_prompt,
    reset_system_prompt,
    save_system_prompt,
    update_config,
)
from app.program.filler import load_reserve

router = APIRouter(prefix="/api/desks", tags=["desks"])


class PromptBody(BaseModel):
    text: str


def _checked(name: str) -> str:
    if name not in DESKS:
        raise HTTPException(status_code=404, detail=f"Unbekannte Redaktion: {name}")
    return name


def _desk_view(request: Request, name: str) -> dict:
    data = request.app.state.scheduler.desk_status(name)
    data["settings"] = DeskConfig.from_config(name, load_config()).settings
    data["prompt_customized"] = is_prompt_customized(name)
    if name == "music":
        reserve = load_reserve(request.app.state.desk_runner.reserve_path)
        data["reserve"] = {"updated_at": reserve["updated_at"], "count": len(reserve["tracks"])}
    return data


@router.get("")
def get_desks(request: Request) -> dict:
    return {"desks": [_desk_view(request, name) for name in DESKS]}


@router.get("/{name}")
def get_desk(name: str, request: Request) -> dict:
    return _desk_view(request, _checked(name))


@router.patch("/{name}")
def patch_desk(name: str, partial: dict, request: Request) -> dict:
    """Deep-merges `partial` into desks.<name> of the user config."""
    update_config({"desks": {_checked(name): partial}})
    return _desk_view(request, name)


@router.post("/{name}/run")
def run_desk(name: str, request: Request) -> dict:
    return request.app.state.scheduler.request_run(_checked(name), "manual", force=True)


@router.get("/{name}/reserve")
def get_reserve(name: str, request: Request) -> dict:
    if _checked(name) != "music":
        raise HTTPException(status_code=404, detail="Nur die Musikredaktion hat eine Reserve")
    return load_reserve(request.app.state.desk_runner.reserve_path)


@router.get("/{name}/prompt")
def get_prompt(name: str) -> dict:
    _checked(name)
    text = load_system_prompt(name)
    return {"text": text, "customized": is_prompt_customized(name), "default": load_default_prompt(name)}


@router.put("/{name}/prompt")
def put_prompt(name: str, body: PromptBody) -> dict:
    save_system_prompt(body.text, desk=_checked(name))
    return {"status": "ok", "customized": is_prompt_customized(name)}


@router.post("/{name}/prompt/reset")
def post_reset_prompt(name: str) -> dict:
    return {"text": reset_system_prompt(_checked(name)), "customized": False}
