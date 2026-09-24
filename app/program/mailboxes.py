"""The two mailboxes the dispatch desk fills: music wishes and notes for the news.

- `data/music_wishes.json`: "demnächst" wishes. The music desk sees the open ones as context and
  marks a wish `used` when a planned segment carries its `wish_id`; if that block expires or is
  removed before the segment aired, the wish is `noted` again (calls.reopen_unplayed_wishes).
- `data/news_notes.json`: hints for the news. The news desk reads them (app/program/news.py) and
  marks those in a bulletin `used` (with `news_slot`); a used note is repeated in the full
  bulletins until its `valid_until`, and goes back to `noted` if its bulletin never aired.

Entry: {id, text, author, call_id, created_at, valid_until, status, used_at, queue_item_id}
with status `noted` -> `used` (-> `noted` again, see above) | `removed`; an entry past its `valid_until` counts as `expired`
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
        now = _now()
        kept = [
            e for e in entries
            if self.effective_status(e) == "noted"
            or (e.get("status") == "used" and (parse_time(e.get("valid_until")) or now) > now)
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

    def mark_in_bulletin(self, entry_ids: list[str], queue_item_id: str, slot: str,
                         replacing: set[str] | frozenset = frozenset()) -> list[str]:
        """News notes read in the bulletin `queue_item_id` for `slot`: noted ones, used ones that
        are still valid (repeated in the full bulletins) and used ones of a replaced version of the
        slot (`replacing`: its item ids) become/stay `used`; returns their ids."""
        if not entry_ids:
            return []
        now = _now()
        used = []
        with self._lock:
            entries = self._load()
            for entry in entries:
                if entry["id"] not in entry_ids:
                    continue
                status = self.effective_status(entry, now)
                until = parse_time(entry.get("valid_until"))
                if status == "noted" or (status == "used" and (
                        (until is not None and until > now) or entry.get("queue_item_id") in replacing)):
                    entry.update(status="used", used_at=entry.get("used_at") or now.isoformat(),
                                 updated_at=now.isoformat(), queue_item_id=queue_item_id, news_slot=slot)
                    used.append(entry["id"])
            if used:
                self._save(entries)
        return used

    def set_fields(self, entry_id: str, **values: Any) -> None:
        with self._lock:
            entries = self._load()
            for entry in entries:
                if entry["id"] == entry_id:
                    entry.update(values, updated_at=_now().isoformat())
                    self._save(entries)
                    return

    def reopen(self, entry_ids: list[str]) -> None:
        """Used entries go back to `noted` (their block didn't air); expired ones stay expired."""
        now = _now().isoformat()
        with self._lock:
            entries = self._load()
            changed = False
            for entry in entries:
                if entry["id"] in entry_ids and entry.get("status") == "used":
                    entry.update(status="noted", used_at=None, updated_at=now, news_slot=None)
                    changed = True
            if changed:
                self._save(entries)

    def remove(self, entry_id: str, also_used: bool = False) -> dict[str, Any] | None:
        """Discards a noted entry (undo) - with `also_used` also a used one (a news note repeated in
        the full bulletins); returns the entry (unchanged when it couldn't be discarded)."""
        with self._lock:
            entries = self._load()
            entry = next((e for e in entries if e["id"] == entry_id), None)
            if entry is None:
                return None
            if self.effective_status(entry) == "noted" or (also_used and entry.get("status") == "used"):
                entry.update(status="removed", updated_at=_now().isoformat())
                self._save(entries)
            entry["status"] = self.effective_status(entry)
            return entry


def wish_mailbox(data_dir: Path) -> Mailbox:
    return Mailbox(data_dir / "music_wishes.json", "wishes")


def news_mailbox(data_dir: Path) -> Mailbox:
    return Mailbox(data_dir / "news_notes.json", "notes")
