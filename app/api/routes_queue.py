"""The program queue and the player: what's coming up, remove/undo, skip the current song."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Request

from app.program.queue import ACTIVE_STATUSES, ProgramQueue, QueueItem

router = APIRouter(prefix="/api/queue", tags=["queue"])
player_router = APIRouter(prefix="/api/player", tags=["player"])

# Finished items shown with ?include_done=true.
DONE_ITEMS = 20


def item_view(item: QueueItem, starts_at: datetime | None = None) -> dict:
    remaining = item.remaining_segments() if item.status in ACTIVE_STATUSES else []
    return {
        **item.to_dict(),
        "duration_seconds": round(sum(s.estimated_seconds() for s in item.segments)),
        "remaining_seconds": round(sum(s.estimated_seconds() for s in remaining)),
        "starts_at": starts_at.isoformat() if starts_at else None,
    }


def queue_view(queue: ProgramQueue, player, include_done: bool = False) -> dict:
    """Active items in play order with estimated start times (from the rest of what's on air)."""
    now = datetime.now(timezone.utc)
    player_status = player.status() if player is not None else {"current": None}
    current = player_status.get("current")
    cursor = now
    if current and current.get("duration"):
        cursor += timedelta(seconds=max(0.0, current["duration"] - current.get("position", 0)))

    items = []
    for item in queue.active_items():
        starts = None if item.status == "playing" else cursor
        items.append(item_view(item, starts))
        cursor += timedelta(seconds=sum(s.estimated_seconds() for s in item.remaining_segments()))
    view = {
        "items": items,
        "now_playing": current,
        "remaining_program_seconds": round(
            player.remaining_program_seconds() if player is not None else queue.remaining_program_seconds()
        ),
        "server_time": now.isoformat(),
    }
    if include_done:
        done = [i for i in queue.items() if i.status not in ACTIVE_STATUSES]
        done.sort(key=lambda i: i.updated_at or i.created_at, reverse=True)
        view["done"] = [item_view(i) for i in done[:DONE_ITEMS]]
    return view


@router.get("")
def get_queue(request: Request, include_done: bool = False) -> dict:
    return queue_view(request.app.state.queue, getattr(request.app.state, "player", None), include_done)


@router.delete("/{item_id}")
def delete_item(item_id: str, request: Request) -> dict:
    """Removes an item (undo via /restore). A playing block stops after its current segment."""
    item = request.app.state.queue.remove(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Beitrag nicht gefunden")
    if item.status != "removed":
        raise HTTPException(status_code=409, detail=f"Beitrag ist bereits {item.status}")
    return item_view(item)


@router.post("/{item_id}/restore")
def restore_item(item_id: str, request: Request) -> dict:
    item = request.app.state.queue.restore(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Beitrag nicht gefunden")
    if item.status not in ACTIVE_STATUSES:
        raise HTTPException(status_code=409, detail="Beitrag kann nicht wiederhergestellt werden (abgelaufen?)")
    return item_view(item)


@player_router.post("/skip")
def skip(request: Request) -> dict:
    return {"skipped": request.app.state.player.skip()}
