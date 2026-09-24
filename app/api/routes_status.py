"""Slim status for all pages: on air, what's playing, desks at a glance, notices."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request

from app.config import is_broadcast_time, load_config, next_broadcast_start

router = APIRouter(prefix="/api/status", tags=["status"])
notices_router = APIRouter(prefix="/api/notices", tags=["notices"])

# How many upcoming segments the legacy `state.script` (old Studio page) shows.
LEGACY_UPCOMING_SEGMENTS = 15


def _notices(request: Request) -> list[dict]:
    notices = list(getattr(request.app.state, "notices", []))
    player = request.app.state.player
    notice = player.status().get("notice")
    if notice:
        notices.append({"id": "player-breaker", "text": notice, "dismissible": False})
    return notices


def _legacy_state(request: Request, now_playing: dict | None) -> dict | None:
    """The old `state` shape ({last_run, final_message, error, script.segments}) built from the
    queue, so the pre-queue Studio page keeps showing the program. Index 0 is what's on air."""
    desk = request.app.state.desk_runner.status("music")
    segments = []
    if now_playing:
        segments.append({"type": now_playing["type"], "title": now_playing["title"], "text": now_playing.get("text"),
                         "duration_seconds": now_playing.get("duration")})
    for item in request.app.state.queue.active_items():
        for seg in item.remaining_segments():
            segments.append({"type": seg.type, "title": seg.title, "text": seg.text,
                             "duration_seconds": seg.duration_seconds})
    if not segments and not desk["last_run_at"]:
        return None
    return {
        "last_run": desk["last_run_at"] or datetime.now(timezone.utc).isoformat(),
        "final_message": desk["last_final_message"] or "",
        "error": desk["last_error"],
        "script": {"segments": segments[:LEGACY_UPCOMING_SEGMENTS]},
    }


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
            "state", "enabled", "last_success_at", "consecutive_failures", "last_error", "next_trigger_at", "fill"
        )}
    return {
        "on_air": is_broadcast_time(config),
        "next_on_air_at": next_start.astimezone().isoformat() if next_start else None,
        "now_playing": now_playing,
        "player": {k: player_status[k] for k in ("mode", "mode_text", "log")},
        "remaining_program_seconds": round(player.remaining_program_seconds()),
        "desks": desks,
        "interrupt_available_at": None,  # Phase 4
        "notices": _notices(request),
        "server_time": player_status["server_time"],
        # Legacy fields for the pre-queue Studio page (web/static/app.js), until the UI is redone.
        "state": _legacy_state(request, current),
        "player_current_segment_index": 0 if current else None,
    }


@router.post("/trigger")
def trigger(request: Request) -> dict:
    """Deprecated alias of POST /api/desks/music/run (the old UI still calls it)."""
    return request.app.state.scheduler.request_run("music", "manual", force=True)


@notices_router.post("/{notice_id}/dismiss")
def dismiss_notice(notice_id: str, request: Request) -> dict:
    notices = getattr(request.app.state, "notices", [])
    remaining = [n for n in notices if n["id"] != notice_id]
    if len(remaining) == len(notices):
        raise HTTPException(status_code=404, detail="Hinweis nicht gefunden")
    request.app.state.notices = remaining
    return {"status": "ok"}
