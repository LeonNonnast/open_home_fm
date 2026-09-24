from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.config import (
    is_prompt_customized,
    is_stopped,
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


PROVIDER_NOTICE_ID = "musikquelle-gewechselt"


def _provider_changed(request: Request, old: dict, new: dict) -> None:
    """The player keeps the music source it was started with: after a switch, drop the planned
    songs of the old source (their URIs don't play elsewhere) and ask for a service restart."""
    before = (old.get("music") or {}).get("provider", "local")
    after = (new.get("music") or {}).get("provider", "local")
    if before == after:
        return
    queue = getattr(request.app.state, "queue", None)
    if queue is not None:
        queue.expire_other_providers(after, "Musikquelle gewechselt")
    notices = getattr(request.app.state, "notices", None)
    if notices is not None and not any(n["id"] == PROVIDER_NOTICE_ID for n in notices):
        notices.append({"id": PROVIDER_NOTICE_ID, "text": "Musikquelle gewechselt – Neustart des Dienstes nötig."})


def _keep_stopped(config: dict, old: dict) -> dict:
    """`station.stopped` is the Stop/Play button's state (/api/player/stop|play, which also cut
    the segment on air) - a settings save with a stale copy must not flip it."""
    if is_stopped(config) == is_stopped(old):
        return config
    station = config.get("station")
    return {**config, "station": {**(station if isinstance(station, dict) else {}), "stopped": is_stopped(old)}}


@router.get("")
def get_config() -> dict:
    return load_config()


@router.put("")
def put_config(config: dict, request: Request) -> dict:
    """Replaces the whole config; keys left out fall back to their defaults."""
    old = load_config()
    save_config(_keep_stopped(config, old))
    _provider_changed(request, old, load_config())
    return {"status": "ok"}


@router.patch("")
def patch_config(partial: dict, request: Request) -> dict:
    """Deep-merges a partial config (dicts recursively, lists/values replaced), returns the result.

    Everything is re-read per run/segment, so changes apply without a restart - except
    `music.provider` for the player (see _provider_changed)."""
    old = load_config()
    new = update_config(_keep_stopped(partial, old) if "station" in partial else partial)
    _provider_changed(request, old, new)
    return new


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
