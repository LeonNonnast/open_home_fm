"""Desks: status, settings, prompt and "run now" (through the scheduler, i.e. lock + cap)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app.agent.desk import DESKS, SETTING_BOUNDS, DeskConfig
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


def _bounded(key: str):
    low, high = SETTING_BOUNDS[key]
    # Default None = "not sent" (dropped via exclude_unset); an explicit null/"" is a 422.
    return Field(None, ge=low, le=high)


class DeskSettingsPatch(BaseModel):
    """What PATCH /api/desks/{name} accepts; anything else is a 422 instead of a stored value
    that breaks every later run."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = None
    fill_threshold_minutes: int = _bounded("fill_threshold_minutes")
    block_minutes: int = _bounded("block_minutes")
    max_queued_program_minutes: int = _bounded("max_queued_program_minutes")
    songs_per_announcement: int = _bounded("songs_per_announcement")
    no_repeat_minutes: int = _bounded("no_repeat_minutes")
    max_tool_iterations: int = _bounded("max_tool_iterations")
    history_runs: int = _bounded("history_runs")
    plugins: list[str] = None
    context_plugins: list[str] = None


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
def patch_desk(name: str, partial: DeskSettingsPatch, request: Request) -> dict:
    """Deep-merges `partial` into desks.<name> of the user config."""
    values = partial.model_dump(exclude_unset=True)
    merged = {**DeskConfig.from_config(_checked(name), load_config()).settings, **values}
    if merged.get("fill_threshold_minutes", 0) >= merged.get("max_queued_program_minutes", float("inf")):
        raise HTTPException(
            status_code=422,
            detail="„Nachplanen unter“ muss kleiner sein als „Höchstens eingeplant“ (fill_threshold_minutes "
                   "< max_queued_program_minutes).",
        )
    update_config({"desks": {name: values}})
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
