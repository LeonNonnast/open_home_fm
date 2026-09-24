"""The two mailboxes the dispatch desk fills: music wishes and notes for the news.

- `data/music_wishes.json`: "demnächst" wishes. The music desk sees the open ones as context and
  marks a wish `used` when a planned segment carries its `wish_id`.
- `data/news_notes.json`: hints for the next news (stored only until the news desk arrives).

Entry: {id, text, author, call_id, created_at, valid_until, status, used_at, queue_item_id}
with status `noted` -> `used` | `removed`; an entry past its `valid_until` counts as `expired`
(computed, never stored - a wish can't come back to life, and there's nothing to write back).
"""
from __future__ import annotations

import json
import logging
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.program.queue import write_json_atomic

logger = logging.getLogger(__name__)

# Entries in a final state are dropped from the file after this long.
CLEANUP_AFTER = timedelta(days=7)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def parse_time(value: Any) -> datetime | None:
    """An ISO date/time as an aware datetime (naive = local time; a bare date = end of that day)."""
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    try:
        if len(text) == 10:  # YYYY-MM-DD
            day = datetime.fromisoformat(text)
            return day.replace(hour=23, minute=59).astimezone(timezone.utc)
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc)  # naive values are taken as local time


class Mailbox:
    def __init__(self, path: Path, key: str):
        self.path = path
        self.key = key  # top-level list name in the file: "wishes" | "notes"
        self._lock = threading.RLock()

    # ---------- persistence ----------

    def _load(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("Could not read %s", self.path, exc_info=True)
            return []
        entries = data.get(self.key, []) if isinstance(data, dict) else data
        return [e for e in entries if isinstance(e, dict) and e.get("id")] if isinstance(entries, list) else []

    def _save(self, entries: list[dict[str, Any]]) -> None:
        cutoff = _now() - CLEANUP_AFTER
        kept = [
            e for e in entries
            if self.effective_status(e) == "noted"
            or (parse_time(e.get("updated_at") or e.get("created_at")) or _now()) > cutoff
        ]
        write_json_atomic(self.path, {self.key: kept})

    # ---------- queries ----------

    @staticmethod
    def effective_status(entry: dict[str, Any], now: datetime | None = None) -> str:
        status = entry.get("status") or "noted"
        if status == "open":  # hand-written entries
            status = "noted"
        if status == "noted":
            until = parse_time(entry.get("valid_until"))
            if until is not None and until < (now or _now()):
                return "expired"
        return status

    def all(self) -> list[dict[str, Any]]:
        with self._lock:
            entries = self._load()
        for entry in entries:
            entry["status"] = self.effective_status(entry)
        return entries

    def get(self, entry_id: str) -> dict[str, Any] | None:
        return next((e for e in self.all() if e["id"] == entry_id), None)

    def open(self) -> list[dict[str, Any]]:
        """Noted and still valid, oldest first."""
        return [e for e in self.all() if e["status"] == "noted"]

    # ---------- changes ----------

    def add(self, text: str, author: str | None = None, call_id: str | None = None,
            valid_until: datetime | None = None) -> dict[str, Any]:
        now = _now()
        entry = {
            "id": uuid.uuid4().hex[:10],
            "text": text,
            "author": author,
            "call_id": call_id,
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "valid_until": valid_until.isoformat() if valid_until else None,
            "status": "noted",
            "used_at": None,
            "queue_item_id": None,
        }
        with self._lock:
            entries = self._load()
            entries.append(entry)
            self._save(entries)
        logger.info("%s: noted %s (%s)", self.path.name, entry["id"], text[:60])
        return entry

    def mark_used(self, entry_ids: list[str], queue_item_id: str | None = None) -> list[str]:
        """Marks open entries as used; returns the ids that were open."""
        if not entry_ids:
            return []
        now = _now()
        used = []
        with self._lock:
            entries = self._load()
            for entry in entries:
                if entry["id"] in entry_ids and self.effective_status(entry, now) == "noted":
                    entry.update(status="used", used_at=now.isoformat(), updated_at=now.isoformat(),
                                 queue_item_id=queue_item_id)
                    used.append(entry["id"])
            if used:
                self._save(entries)
        return used

    def remove(self, entry_id: str) -> dict[str, Any] | None:
        """Discards a noted entry (undo); returns the entry (unchanged when it wasn't noted)."""
        with self._lock:
            entries = self._load()
            entry = next((e for e in entries if e["id"] == entry_id), None)
            if entry is None:
                return None
            if self.effective_status(entry) == "noted":
                entry.update(status="removed", updated_at=_now().isoformat())
                self._save(entries)
            entry["status"] = self.effective_status(entry)
            return entry


def wish_mailbox(data_dir: Path) -> Mailbox:
    return Mailbox(data_dir / "music_wishes.json", "wishes")


def news_mailbox(data_dir: Path) -> Mailbox:
    return Mailbox(data_dir / "news_notes.json", "notes")
