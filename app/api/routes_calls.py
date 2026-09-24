"""Calls ("Zwischenrufe"): send as text or voice, the conversation with the dispatch desk's
results, undo; plus the two mailboxes the dispatch desk fills (music wishes, news notes).

A new call starts the dispatch desk right away (through the scheduler: lock + follow-up run).
Voice is asynchronous: POST /api/calls/voice answers at once with status `transcribing`, the
transcript arrives as `awaiting_confirmation`, and PATCH /api/calls/{id} confirms (optionally
edited) - only then does the dispatch desk see it.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.program.calls import PENDING_STATUSES, CallStore, call_view

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/calls", tags=["calls"])
wishes_router = APIRouter(prefix="/api/wishes", tags=["calls"])
notes_router = APIRouter(prefix="/api/news-notes", tags=["calls"])

MAX_TEXT = 1000
MAX_AUDIO_BYTES = 20 * 1024 * 1024
UPLOAD_CHUNK = 256 * 1024
AUDIO_SUFFIXES = {".webm", ".ogg", ".oga", ".wav", ".mp3", ".m4a", ".mp4", ".aac", ".opus"}


class TextCallBody(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_TEXT)
    author: str | None = Field(None, max_length=40)


class ConfirmBody(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_TEXT)


def _calls(request: Request) -> CallStore:
    return request.app.state.calls


def wake_dispatch(request: Request, force: bool = False) -> None:
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is None:
        return
    try:
        scheduler.request_run("dispatch", "call", force=force)
    except Exception:
        logger.exception("Could not trigger the dispatch desk for a new call")


def _get(request: Request, call_id: str) -> dict:
    call = _calls(request).get(call_id)
    if call is None:
        raise HTTPException(status_code=404, detail="Zwischenruf nicht gefunden")
    return call


def create_text_call(request: Request, text: str, author: str | None) -> dict:
    text = text.strip()
    if not text:
        raise HTTPException(status_code=422, detail="Der Zwischenruf ist leer.")
    call = _calls(request).create(text, author=author, source="text")
    wake_dispatch(request)
    return call


def save_upload(calls: CallStore, file: UploadFile) -> Path:
    """Copies the upload to data/calls in chunks, never more than MAX_AUDIO_BYTES."""
    if file.size is not None and file.size > MAX_AUDIO_BYTES:
        raise HTTPException(status_code=413, detail="Aufnahme zu groß")
    suffix = Path(file.filename or "recording.webm").suffix.lower()
    suffix = suffix if suffix in AUDIO_SUFFIXES else ".webm"
    calls.dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    path = calls.dir / f"rec_{stamp}{suffix}"
    written = 0
    with path.open("wb") as out:
        while chunk := file.file.read(UPLOAD_CHUNK):
            written += len(chunk)
            if written > MAX_AUDIO_BYTES:
                out.close()
                path.unlink(missing_ok=True)
                raise HTTPException(status_code=413, detail="Aufnahme zu groß")
            out.write(chunk)
    return path


# ---------- calls ----------

@router.get("")
def list_calls(request: Request, since: str | None = None, limit: int = 50) -> dict:
    """Newest first. `since` (ISO time, e.g. the previous `server_time`) returns only calls
    changed after it - the UI polls with it."""
    calls = _calls(request)
    calls.sync()
    items = calls.all()
    if since:
        try:
            cutoff = datetime.fromisoformat(since.replace(" ", "+"))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="since: ISO-Zeitpunkt erwartet") from exc
        cutoff = cutoff if cutoff.tzinfo else cutoff.replace(tzinfo=timezone.utc)
        items = [c for c in items if datetime.fromisoformat(c["updated_at"]) > cutoff]
    scheduler = getattr(request.app.state, "scheduler", None)
    dispatch = scheduler.desk_status("dispatch") if scheduler is not None else None
    return {
        "calls": [call_view(c) for c in items[: max(1, min(limit, 200))]],
        "dispatch": {k: dispatch.get(k) for k in ("state", "enabled", "last_error", "backoff_until", "open_calls")}
        if dispatch else None,
        "interrupt_available_at": None,  # Phase 4
        "server_time": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/{call_id}")
def get_call(call_id: str, request: Request) -> dict:
    _calls(request).sync([call_id])
    return call_view(_get(request, call_id))


@router.post("/text")
def post_text(body: TextCallBody, request: Request) -> dict:
    return call_view(create_text_call(request, body.text, body.author))


def _transcribe(calls: CallStore, stt_engine, call_id: str, audio_path: Path) -> None:
    try:
        if stt_engine is None:
            raise RuntimeError("Spracherkennung nicht verfügbar")
        text = (stt_engine.transcribe(audio_path) or "").strip()
    except Exception as exc:
        logger.exception("Transcribing call %s failed", call_id)
        error = f"Transkription fehlgeschlagen: {exc}"
        calls.update(call_id, lambda c: c.update(status="failed", error=error)
                     if c["status"] == "transcribing" else False)
        return

    def done(c: dict) -> bool | None:
        if c["status"] != "transcribing":
            return False  # withdrawn meanwhile
        c.update(status="awaiting_confirmation", text=text,
                 error=None if text else "Nichts verstanden – bitte eintippen oder nochmal aufnehmen.")
    calls.update(call_id, done)


def start_transcription(calls: CallStore, stt_engine, call: dict) -> None:
    threading.Thread(
        target=_transcribe, args=(calls, stt_engine, call["id"], Path(call["audio_file"])),
        daemon=True, name=f"stt-{call['id']}",
    ).start()


def resume_transcriptions(calls: CallStore, stt_engine) -> int:
    """At startup: voice calls a restart left `transcribing` are transcribed again (their
    recording is still there) - or, without it, wait for the listener to type the text."""
    count = 0
    for call in calls.interrupted_transcriptions():
        if call.get("audio_file") and Path(call["audio_file"]).exists():
            start_transcription(calls, stt_engine, call)
        else:
            calls.update(call["id"], lambda c: c.update(
                status="awaiting_confirmation",
                error="Transkription unterbrochen – bitte eintippen oder nochmal aufnehmen.",
            ) if c["status"] == "transcribing" else False)
        count += 1
    return count


@router.post("/voice")
def post_voice(request: Request, file: UploadFile = File(...), author: str | None = Form(None)) -> dict:
    """Stores the recording and answers at once (`transcribing`); STT runs in the background."""
    calls = _calls(request)
    audio_path = save_upload(calls, file)
    call = calls.create("", author=author, source="voice", status="transcribing", audio_file=str(audio_path))
    start_transcription(calls, getattr(request.app.state, "stt_engine", None), call)
    return call_view(call)


@router.patch("/{call_id}")
def confirm_call(call_id: str, body: ConfirmBody, request: Request) -> dict:
    """Confirms (and optionally edits) a transcribed call - or edits one the desk hasn't taken yet."""
    _get(request, call_id)
    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=422, detail="Der Zwischenruf ist leer.")
    now = datetime.now(timezone.utc).isoformat()

    def confirm(c: dict) -> bool | None:
        editable = c["status"] in ("awaiting_confirmation", "new") or (
            c["status"] == "failed" and not c.get("dispatched"))
        if not editable:
            return False
        c.update(text=text, status="new", error=None, submitted_at=now)
    call = _calls(request).update(call_id, confirm)
    if call["status"] != "new" or call["text"] != text:
        raise HTTPException(status_code=409, detail=f"Zwischenruf ist bereits {call['status']}")
    wake_dispatch(request)
    return call_view(call)


@router.post("/{call_id}/retry")
def retry_call(call_id: str, request: Request) -> dict:
    """Gives an expired/failed (or still retrying) call to the dispatch desk again ("Nochmal senden")."""
    _get(request, call_id)
    now = datetime.now(timezone.utc).isoformat()

    def retry(c: dict) -> bool | None:
        if c["status"] not in ("expired", "failed", *PENDING_STATUSES) or c.get("dispatched") or not c["text"]:
            return False
        c.update(status="new", error=None, attempts=0, submitted_at=now)
    call = _calls(request).update(call_id, retry)
    if call["status"] != "new":
        raise HTTPException(status_code=409, detail=f"Zwischenruf ist {call['status']} – nichts zu wiederholen")
    wake_dispatch(request, force=True)
    return call_view(call)


@router.delete("/{call_id}/actions/{index}")
def undo_action(call_id: str, index: int, request: Request) -> dict:
    """Undoes one action: removes its queue item (shared by the call's program actions), wish or note."""
    call, error = _calls(request).undo_action(call_id, index)
    if call is None:
        raise HTTPException(status_code=404, detail=error)
    if error:
        raise HTTPException(status_code=404 if error == "Aktion nicht gefunden" else 409, detail=error)
    return call_view(call)


@router.delete("/{call_id}")
def delete_call(call_id: str, request: Request) -> dict:
    """Withdraws a call: everything still undoable is undone, a pending one won't be dispatched."""
    _get(request, call_id)
    return call_view(_calls(request).remove_call(call_id))


@router.get("/{call_id}/audio")
def get_reply_audio(call_id: str, request: Request) -> FileResponse:
    """The spoken reply of the dispatch desk (WAV), to listen to in the browser."""
    call = _get(request, call_id)
    path = Path(call["reply_audio"]) if call.get("reply_audio") else None
    if path is None or not path.exists():
        raise HTTPException(status_code=404, detail="Keine Antwort-Ansage vorhanden")
    return FileResponse(path, media_type="audio/wav")


# ---------- mailboxes ----------

@wishes_router.get("")
def get_wishes(request: Request) -> dict:
    return {"wishes": list(reversed(request.app.state.desk_runner.wishes.all()))}


@wishes_router.delete("/{wish_id}")
def delete_wish(wish_id: str, request: Request) -> dict:
    mailbox = request.app.state.desk_runner.wishes
    wish = mailbox.get(wish_id)
    if wish is None:
        raise HTTPException(status_code=404, detail="Wunsch nicht gefunden")
    if wish["status"] != "noted":
        raise HTTPException(status_code=409, detail=f"Wunsch ist bereits {wish['status']}")
    wish = mailbox.remove(wish_id)
    _calls(request).sync()
    return wish


@notes_router.get("")
def get_notes(request: Request) -> dict:
    return {"notes": list(reversed(request.app.state.desk_runner.news_notes.all()))}


@notes_router.delete("/{note_id}")
def delete_note(note_id: str, request: Request) -> dict:
    mailbox = request.app.state.desk_runner.news_notes
    note = mailbox.get(note_id)
    if note is None:
        raise HTTPException(status_code=404, detail="Hinweis nicht gefunden")
    if note["status"] != "noted":
        raise HTTPException(status_code=409, detail=f"Hinweis ist bereits {note['status']}")
    note = mailbox.remove(note_id)
    _calls(request).sync()
    return note
