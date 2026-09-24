"""Which tracks actually went on air, and when - so a run doesn't plan songs that just played.

Written by the player (the only side that knows what really played - skipped songs don't count)
and read by the desks and the filler program. Plain JSON on disk, like the queue itself.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Entries older than this are dropped on write, whatever no_repeat_minutes is set to.
KEEP_HOURS = 24

_lock = threading.Lock()


def _load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("Could not read play history %s, starting fresh", path, exc_info=True)
        return []


def record_played(path: Path, uri: str, title: str) -> None:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=KEEP_HOURS)
    with _lock:
        entries = [e for e in _load(path) if datetime.fromisoformat(e["played_at"]) >= cutoff]
        entries.append({"uri": uri, "title": title, "played_at": now.isoformat()})
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8")


def recently_played(path: Path, minutes: int) -> list[dict]:
    """Entries from the last `minutes`, oldest first."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    with _lock:
        return [e for e in _load(path) if datetime.fromisoformat(e["played_at"]) >= cutoff]
