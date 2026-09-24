"""Slim status for all pages: on air, what's playing, desks at a glance, notices."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from app.config import is_broadcast_time, is_stopped, load_config, next_broadcast_start

router = APIRouter(prefix="/api/status", tags=["status"])
notices_router = APIRouter(prefix="/api/notices", tags=["notices"])


def _notices(request: Request) -> list[dict]:
    notices = list(getattr(request.app.state, "notices", []))
    player = request.app.state.player
    notice = player.status().get("notice")
    if notice:
        notices.append({"id": "player-breaker", "text": notice, "dismissible": False})
    return notices


@router.get("")
def get_status(request: Request) -> dict:
    config = load_config()
    player = request.app.state.player
    player_status = player.status()
    current = player_status["current"]
    now_playing = None
    if current:
        now_playing = {**current, "mode": player_status["mode"], "mode_text": player_status["mode_text"]}
    next_start = next_broadcast_start(config)
    scheduler = request.app.state.scheduler
    desks = {}
    for d in scheduler.desks_status():
        desks[d["name"]] = {k: d.get(k) for k in (
            "state", "enabled", "last_success_at", "consecutive_failures", "last_error", "backoff_until",
            "next_trigger_at", "fill", "open_calls", "last_run_at",
            "next_slot_at", "next_slot_format", "prepare_at", "prepared", "last_bulletin", "open_notes", "slots",
        )}
    return {
        "on_air": is_broadcast_time(config),  # false while stopped
        "stopped": is_stopped(config),
        "next_on_air_at": next_start.astimezone().isoformat() if next_start else None,
        "now_playing": now_playing,
        "player": {k: player_status[k] for k in ("mode", "mode_text", "log")},
        "remaining_program_seconds": round(player.remaining_program_seconds()),
        "desks": desks,
        "interrupt_available_at": None,  # Phase 4
        "notices": _notices(request),
        "server_time": player_status["server_time"],
    }


@notices_router.post("/{notice_id}/dismiss")
def dismiss_notice(notice_id: str, request: Request) -> dict:
    notices = getattr(request.app.state, "notices", [])
    remaining = [n for n in notices if n["id"] != notice_id]
    if len(remaining) == len(notices):
        raise HTTPException(status_code=404, detail="Hinweis nicht gefunden")
    request.app.state.notices = remaining
    return {"status": "ok"}
