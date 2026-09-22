"""Persists each agent loop run's full message array to disk, and builds the compact summary
of the last few runs that gets injected back into the next run's input.

Two representations on purpose:
  - the saved file: the *full* message array (system/user/assistant/tool, including tool-call
    arguments and results) - kept for the web UI and for debugging.
  - the injected context: a short summary (wishes + final announcement + resulting script) of
    each of the last few runs - full raw transcripts would be noisy and, if ever re-saved
    verbatim into the next run's own transcript, would grow without bound across runs. The
    summary is deliberately never written back into a transcript file, so injected context never
    compounds across runs.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from app.agent.llm import LLMMessage
from app.agent.script import Script

MAX_TRANSCRIPTS_ON_DISK = 50


def save_transcript(
    transcripts_dir: Path,
    messages: list[LLMMessage],
    final_message: str,
    script: Script | None,
    error: str | None = None,
) -> Path:
    transcripts_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    transcript_id = f"{now.strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:6]}"
    data = {
        "id": transcript_id,
        "created_at": now.isoformat(),
        "messages": [asdict(m) for m in messages],
        "final_message": final_message,
        "script": asdict(script) if script else None,
        "error": error,
    }
    path = transcripts_dir / f"{transcript_id}.json"
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    _prune_old(transcripts_dir)
    return path


def _prune_old(transcripts_dir: Path, keep: int = MAX_TRANSCRIPTS_ON_DISK) -> None:
    files = sorted(transcripts_dir.glob("*.json"))
    for f in files[: max(0, len(files) - keep)]:
        f.unlink(missing_ok=True)


def list_transcripts(transcripts_dir: Path) -> list[dict]:
    """Newest first. Used by the web UI."""
    files = sorted(transcripts_dir.glob("*.json"), reverse=True)
    return [json.loads(f.read_text(encoding="utf-8")) for f in files]


def load_recent_transcripts(transcripts_dir: Path, n: int = 3) -> list[dict]:
    """Newest first. Used to build the next run's injected history context."""
    if not transcripts_dir.exists():
        return []
    files = sorted(transcripts_dir.glob("*.json"), reverse=True)[:n]
    return [json.loads(f.read_text(encoding="utf-8")) for f in files]


def _first_user_message(transcript: dict) -> str:
    for m in transcript.get("messages", []):
        if m.get("role") == "user":
            return m["content"]
    return ""


def render_history_context(transcripts: list[dict]) -> str:
    """Compact, human-readable summary of the given (newest-first) transcripts."""
    if not transcripts:
        return ""

    blocks = []
    for t in transcripts:
        script = t.get("script")
        segments = script.get("segments", []) if script else []
        segment_lines = "\n".join(f"  - [{s['type']}] {s['title']}" for s in segments) or "  (kein Script erzeugt)"
        blocks.append(
            f"Lauf vom {t['created_at']} (id={t['id']}):\n"
            f"- Eingang: {_first_user_message(t) or '(keine Wünsche)'}\n"
            f"- Ansage/Ergebnis: {t.get('final_message') or '(keine)'}\n"
            f"- Gebautes Script:\n{segment_lines}"
        )

    return (
        "Bisherige Durchläufe, neueste zuerst (nur zur Orientierung - kein neuer Auftrag):\n\n"
        + "\n\n".join(blocks)
    )
