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
from typing import Any

from app.agent.llm import LLMMessage

# Per desk, so frequent short runs of one desk never push another desk's history out.
MAX_TRANSCRIPTS_ON_DISK = 50
LEGACY_DESK = "music"  # transcripts from before the desks carry no desk in their name


def _desk_of(path: Path) -> str:
    # <stamp>_<desk>_<rand>.json; older files are <stamp>_<rand>.json
    parts = path.stem.split("_")
    return parts[1] if len(parts) >= 3 else LEGACY_DESK


def _files(transcripts_dir: Path, desk: str | None = None) -> list[Path]:
    """Oldest first (the timestamp leads the name)."""
    if not transcripts_dir.exists():
        return []
    files = sorted(transcripts_dir.glob("*.json"))
    return [f for f in files if desk is None or _desk_of(f) == desk]


def save_transcript(
    transcripts_dir: Path,
    messages: list[LLMMessage],
    final_message: str,
    script: dict[str, Any] | None,
    error: str | None = None,
    desk: str = LEGACY_DESK,
    trigger: str | None = None,
    inputs: str | None = None,
) -> Path:
    """`script` is what the run put on air: {"items": [...queue item ids], "segments": [...]}.
    `inputs` is the short version of the run's input (wishes) for the next runs' history."""
    transcripts_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    transcript_id = f"{now.strftime('%Y%m%dT%H%M%S')}_{desk}_{uuid.uuid4().hex[:6]}"
    data = {
        "id": transcript_id,
        "desk": desk,
        "trigger": trigger,
        "inputs": inputs,
        "created_at": now.isoformat(),
        "messages": [asdict(m) for m in messages],
        "final_message": final_message,
        "script": script,
        "error": error,
    }
    path = transcripts_dir / f"{transcript_id}.json"
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    _prune_old(transcripts_dir, desk)
    return path


def _prune_old(transcripts_dir: Path, desk: str, keep: int = MAX_TRANSCRIPTS_ON_DISK) -> None:
    files = _files(transcripts_dir, desk)
    for f in files[: max(0, len(files) - keep)]:
        f.unlink(missing_ok=True)


def _read(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    data.setdefault("desk", _desk_of(path))
    return data


def list_transcripts(transcripts_dir: Path, desk: str | None = None) -> list[dict]:
    """Newest first. Used by the web UI."""
    return [_read(f) for f in reversed(_files(transcripts_dir, desk))]


def load_recent_transcripts(transcripts_dir: Path, n: int = 3, desk: str = LEGACY_DESK) -> list[dict]:
    """Newest first. Used to build the next run's injected history context."""
    return [_read(f) for f in list(reversed(_files(transcripts_dir, desk)))[:n]]


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
        segment_lines = "\n".join(f"  - [{s['type']}] {s['title']}" for s in segments) or "  (nichts eingeplant)"
        blocks.append(
            f"Lauf vom {t['created_at']} (id={t['id']}):\n"
            f"- Eingang: {(t['inputs'] if 'inputs' in t else _first_user_message(t)) or '(keine Wünsche)'}\n"
            f"- Ansage/Ergebnis: {t.get('final_message') or '(keine)'}\n"
            f"- Eingeplant:\n{segment_lines}"
        )

    return (
        "Bisherige Durchläufe, neueste zuerst (nur zur Orientierung - kein neuer Auftrag):\n\n"
        + "\n\n".join(blocks)
    )
