"""The deterministic playback script: what the agent loop produces and the player executes.

Kept as a plain JSON file on disk so the two halves of the system (LLM-driven generation vs.
deterministic playback) never share in-process state - the player can be a completely separate
process/thread and just polls this file.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class Segment:
    type: str  # "track" | "jingle"
    title: str
    audio_ref: str  # playable reference: provider URI (track) or local audio file path (jingle)
    provider: str | None = None  # "local" | "spotify", only set for track segments
    duration_seconds: float | None = None
    text: str | None = None  # jingle script text, kept for the UI/logs


@dataclass
class Script:
    id: str
    created_at: str
    segments: list[Segment] = field(default_factory=list)

    @classmethod
    def new(cls, segments: list[Segment]) -> "Script":
        return cls(
            id=uuid.uuid4().hex[:12],
            created_at=datetime.now(timezone.utc).isoformat(),
            segments=segments,
        )


def save_script(script: Script, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(script), indent=2, ensure_ascii=False), encoding="utf-8")


def load_script(path: Path) -> Script | None:
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    segments = [Segment(**s) for s in data.get("segments", [])]
    return Script(id=data["id"], created_at=data["created_at"], segments=segments)
