"""Exposes saved run transcripts (full message arrays) to the web UI."""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException

from app.agent.transcript import list_transcripts
from app.config import resolve_path

router = APIRouter(prefix="/api/transcripts", tags=["transcripts"])

TRANSCRIPTS_DIR = resolve_path("data/transcripts")


@router.get("")
def get_transcripts(desk: str | None = None) -> dict:
    transcripts = list_transcripts(TRANSCRIPTS_DIR, desk=desk)
    summaries = [
        {
            "id": t["id"],
            "desk": t.get("desk"),
            "trigger": t.get("trigger"),
            "inputs": t.get("inputs"),
            "created_at": t["created_at"],
            "final_message": t.get("final_message", ""),
            "error": t.get("error"),
            "segment_count": len((t.get("script") or {}).get("segments", [])),
            "message_count": len(t.get("messages", [])),
        }
        for t in transcripts
    ]
    return {"transcripts": summaries}


@router.get("/{transcript_id}")
def get_transcript(transcript_id: str) -> dict:
    path = TRANSCRIPTS_DIR / f"{transcript_id}.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Transcript not found")
    return json.loads(path.read_text(encoding="utf-8"))
