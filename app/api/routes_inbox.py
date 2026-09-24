"""Where listener wishes enter the system, as text or as a voice recording (STT'd immediately).

The music desk only ever reads `*.txt` files from data/inbox (see app/agent/desk.py), so both
paths converge on the same plain-text format before the agent ever sees them. A new wish asks
the music desk to plan right away (within the queue cap). Replaced by the calls in Phase 2.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, File, Request, UploadFile
from pydantic import BaseModel

from app.config import resolve_path

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/inbox", tags=["inbox"])

INBOX_DIR = resolve_path("data/inbox")


class TextWishBody(BaseModel):
    text: str


@router.get("")
def list_inbox() -> dict:
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    items = [
        {"filename": path.name, "text": path.read_text(encoding="utf-8")}
        for path in sorted(INBOX_DIR.glob("*.txt"))
    ]
    return {"items": items}


def _wake_music_desk(request: Request) -> None:
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is None:
        return
    try:
        scheduler.request_run("music", "wish")
    except Exception:
        logger.exception("Could not trigger the music desk for a new wish")


@router.post("/text")
def submit_text(body: TextWishBody, request: Request) -> dict:
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    path = INBOX_DIR / f"{stamp}.txt"
    path.write_text(body.text.strip(), encoding="utf-8")
    _wake_music_desk(request)
    return {"status": "ok", "filename": path.name}


@router.post("/voice")
async def submit_voice(request: Request, file: UploadFile = File(...)) -> dict:
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    ext = Path(file.filename or "recording.webm").suffix or ".webm"
    audio_path = INBOX_DIR / f"{stamp}{ext}"
    audio_path.write_bytes(await file.read())

    stt_engine = request.app.state.stt_engine
    text = stt_engine.transcribe(audio_path)

    text_path = INBOX_DIR / f"{stamp}.txt"
    text_path.write_text(text, encoding="utf-8")
    _wake_music_desk(request)
    return {"status": "ok", "text": text, "filename": text_path.name}
