"""Deprecated alias of the calls API for the pre-Phase-2 "Rufen" page - removed with the next version.

POST /api/inbox/text and /api/inbox/voice create calls (the voice one transcribes synchronously
and skips the confirmation step, like before); GET /api/inbox lists the calls still open.
"""
from __future__ import annotations

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from pydantic import BaseModel

from app.api.routes_calls import create_text_call, save_upload, wake_dispatch

router = APIRouter(prefix="/api/inbox", tags=["inbox"])

OPEN_STATUSES = ("new", "processing", "retrying", "queued")


class TextWishBody(BaseModel):
    text: str
    author: str | None = None


@router.get("")
def list_inbox(request: Request) -> dict:
    calls = [c for c in reversed(request.app.state.calls.all()) if c["status"] in OPEN_STATUSES]
    # `filename` starts with the UTC stamp the old page parses for its time column.
    return {"items": [{"filename": c["id"], "text": c["text"]} for c in calls]}


@router.post("/text")
def submit_text(body: TextWishBody, request: Request) -> dict:
    call = create_text_call(request, body.text, body.author)
    return {"status": "ok", "filename": call["id"], "call_id": call["id"]}


@router.post("/voice")
def submit_voice(request: Request, file: UploadFile = File(...)) -> dict:
    # A plain def: FastAPI runs it in its threadpool, so the synchronous STT doesn't block the loop.
    calls = request.app.state.calls
    audio_path = save_upload(calls, file)
    stt_engine = request.app.state.stt_engine
    if stt_engine is None:
        raise HTTPException(status_code=503, detail="Spracherkennung nicht verfügbar")
    text = stt_engine.transcribe(audio_path).strip()
    if not text:
        raise HTTPException(status_code=422, detail="Nichts verstanden")
    call = calls.create(text, source="voice", audio_file=str(audio_path))
    wake_dispatch(request)
    return {"status": "ok", "text": text, "filename": call["id"], "call_id": call["id"]}
