"""The program queue: every planned item (music blocks, later replies/news), persistent on disk.

Replaces the old single `current_script.json` that every run overwrote. Desks only *append*
items, the player is the only side that changes an item's `status` (the UI may remove/restore),
and the player picks the next item by lane priority - so a new block never cuts the running one
short, and later replies/news slot in between two segments.

Persistence: `data/queue.json` (write tmp -> rename, one lock) plus `data/player_cursor.json`
with the segment currently on air. Assumes a single process (one uvicorn worker): the lock only
serializes threads of this process.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Highest priority first; FIFO within a lane.
LANES = ("urgent", "reply", "news", "program", "filler")
LANE_PRIORITY = {lane: i for i, lane in enumerate(LANES)}
ACTIVE_STATUSES = ("queued", "playing")
FINAL_STATUSES = ("played", "skipped", "expired", "removed")

# Tracks whose duration the provider doesn't report (some local files).
TRACK_ESTIMATE_SECONDS = 210
# Spoken segments whose WAV couldn't be measured (e.g. TTS disabled in tests).
JINGLE_ESTIMATE_SECONDS = 20
# Program blocks carry time references ("gleich halb acht") - never play them hours later.
PROGRAM_TTL = timedelta(hours=2)
# Replies to a call ("als Nächstes") are stale after this long.
REPLY_TTL = timedelta(minutes=30)
# Items in a final state are dropped from queue.json after this long.
CLEANUP_AFTER = timedelta(hours=24)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _known_fields(cls, data: dict[str, Any]) -> dict[str, Any]:
    # Tolerant loading: a newer version may have added fields - ignore them instead of crashing.
    names = {f.name for f in fields(cls)}
    return {k: v for k, v in data.items() if k in names}


@dataclass
class Segment:
    type: str  # "track" | "jingle"
    title: str
    audio_ref: str  # provider URI (track) or local audio file path (jingle)
    provider: str | None = None  # "local" | "spotify", only set for track segments
    duration_seconds: float | None = None  # tracks: from the provider; jingles: measured at render time
    text: str | None = None  # jingle text, kept for the UI/logs
    wish_id: str | None = None  # music wish this segment fulfils (wish inbox, Phase 2)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Segment":
        return cls(**_known_fields(cls, data))

    def estimated_seconds(self) -> float:
        if self.duration_seconds:
            return float(self.duration_seconds)
        return TRACK_ESTIMATE_SECONDS if self.type == "track" else JINGLE_ESTIMATE_SECONDS


@dataclass
class QueueItem:
    id: str
    lane: str
    desk: str
    segments: list[Segment]
    created_at: str
    not_before: str | None = None
    expires_at: str | None = None
    interrupt: bool = False
    resume_interrupted: bool = True
    call_id: str | None = None
    status: str = "queued"
    # Index of the next segment to play. Set when a segment *starts*, so after a restart playback
    # continues with the next segment instead of repeating the whole block.
    next_segment: int = 0
    updated_at: str | None = None
    note: str | None = None  # why it expired/was skipped, shown in the UI/logs
    removed_from: str | None = None  # status before the UI removed it, for restore

    @classmethod
    def new(
        cls,
        lane: str,
        desk: str,
        segments: list[Segment],
        ttl: timedelta | None = None,
        now: datetime | None = None,
        **kwargs: Any,
    ) -> "QueueItem":
        if lane not in LANE_PRIORITY:
            raise ValueError(f"Unknown lane: {lane}")
        now = now or _now()
        if ttl is None and lane == "program":
            ttl = PROGRAM_TTL
        elif ttl is None and lane == "reply":
            ttl = REPLY_TTL
        return cls(
            id=uuid.uuid4().hex[:12],
            lane=lane,
            desk=desk,
            segments=segments,
            created_at=now.isoformat(),
            expires_at=(now + ttl).isoformat() if ttl else None,
            updated_at=now.isoformat(),
            **kwargs,
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "QueueItem":
        values = _known_fields(cls, data)
        values["segments"] = [Segment.from_dict(s) for s in data.get("segments", []) if isinstance(s, dict)]
        return cls(**values)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def remaining_segments(self) -> list[Segment]:
        return self.segments[self.next_segment:]

    def is_expired(self, now: datetime) -> bool:
        expires = _parse(self.expires_at)
        return expires is not None and now >= expires


class ProgramQueue:
    def __init__(self, path: Path, cursor_path: Path):
        self.path = path
        self.cursor_path = cursor_path
        self._lock = threading.RLock()

    # ---------- persistence ----------

    def _load(self) -> list[QueueItem]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("Could not read queue %s, starting with an empty queue", self.path, exc_info=True)
            return []
        items = []
        for raw in data.get("items", []) if isinstance(data, dict) else []:
            try:
                items.append(QueueItem.from_dict(raw))
            except (AttributeError, TypeError, ValueError):
                logger.warning("Dropping unreadable queue item %r", raw)
        return items

    def _save(self, items: list[QueueItem]) -> None:
        cutoff = _now() - CLEANUP_AFTER
        kept = [
            i for i in items
            if i.status in ACTIVE_STATUSES or (_parse(i.updated_at) or _parse(i.created_at) or cutoff) > cutoff
        ]
        write_json_atomic(self.path, {"items": [i.to_dict() for i in kept]})

    def items(self) -> list[QueueItem]:
        with self._lock:
            return self._load()

    def get(self, item_id: str) -> QueueItem | None:
        return next((i for i in self.items() if i.id == item_id), None)

    # ---------- desks ----------

    def append(self, item: QueueItem) -> QueueItem:
        with self._lock:
            items = self._load()
            items.append(item)
            self._save(items)
        logger.info("Queued %s item %s from %s (%d segments)", item.lane, item.id, item.desk, len(item.segments))
        return item

    # ---------- player ----------

    def next_item(self, now: datetime | None = None) -> QueueItem | None:
        """Highest lane first, FIFO within a lane; a started item goes before queued ones of its lane.

        Expired items are marked `expired` on the way (with a log line saying why).
        """
        now = now or _now()
        with self._lock:
            items = self._load()
            changed = False
            candidates = []
            for item in items:
                if item.status not in ACTIVE_STATUSES:
                    continue
                if item.next_segment >= len(item.segments):
                    self._set_status(item, "played", now)
                    changed = True
                    continue
                if item.is_expired(now):
                    self._set_status(item, "expired", now, note="abgelaufen")
                    logger.info("Queue item %s (%s) expired at %s", item.id, item.lane, item.expires_at)
                    changed = True
                    continue
                not_before = _parse(item.not_before)
                if not_before is not None and not_before > now:
                    continue
                candidates.append(item)
            if changed:
                self._save(items)
            if not candidates:
                return None
            candidates.sort(key=lambda i: (LANE_PRIORITY.get(i.lane, len(LANES)), i.status != "playing", i.created_at))
            return candidates[0]

    def start_segment(self, item_id: str, index: int) -> bool:
        """Marks segment `index` as on air. False (and nothing changed) when the item is no
        longer active - removed in the UI or expired since `next_item()` - so the player bails out."""
        now = _now()
        with self._lock:
            items = self._load()
            item = next((i for i in items if i.id == item_id), None)
            if item is None or item.status not in ACTIVE_STATUSES:
                return False
            item.status = "playing"
            item.next_segment = index + 1
            item.updated_at = now.isoformat()
            self._save(items)
            write_json_atomic(
                self.cursor_path, {"item_id": item_id, "segment_index": index, "started_at": now.isoformat()}
            )
            return True

    def rewind(self, item_id: str, index: int) -> bool:
        """Puts segment `index` back as the next one to play (a segment that failed right away,
        e.g. during a Spotify outage, isn't consumed). Revives an item the player itself finished,
        but never one the UI removed or that expired."""
        now = _now()
        with self._lock:
            items = self._load()
            item = next((i for i in items if i.id == item_id), None)
            if item is None or item.status not in (*ACTIVE_STATUSES, "played") or item.is_expired(now):
                return False
            item.next_segment = min(item.next_segment, index)
            item.status = "playing" if item.next_segment > 0 else "queued"
            item.updated_at = now.isoformat()
            self._save(items)
            return True

    def finish_item(self, item_id: str, status: str = "played", note: str | None = None) -> None:
        with self._lock:
            items = self._load()
            for item in items:
                if item.id == item_id and item.status in ACTIVE_STATUSES:
                    self._set_status(item, status, _now(), note=note)
            self._save(items)
            cursor = self.cursor()
            if cursor and cursor.get("item_id") == item_id:
                self.cursor_path.unlink(missing_ok=True)

    def expire_lanes(self, lanes: tuple[str, ...], note: str) -> int:
        """Expires all active items of `lanes` (e.g. at the end of the broadcast window)."""
        now = _now()
        count = 0
        with self._lock:
            items = self._load()
            for item in items:
                if item.lane in lanes and item.status in ACTIVE_STATUSES:
                    self._set_status(item, "expired", now, note=note)
                    count += 1
            if count:
                self._save(items)
                logger.info("Expired %d queue item(s): %s", count, note)
        return count

    def expire_other_providers(self, provider: str, note: str, lanes: tuple[str, ...] = ("program", "filler")) -> int:
        """Expires active items of `lanes` with tracks of another music source than `provider`
        (their URIs can't be played after switching `music.provider`)."""
        now = _now()
        count = 0
        with self._lock:
            items = self._load()
            for item in items:
                if item.lane not in lanes or item.status not in ACTIVE_STATUSES:
                    continue
                if any(s.type == "track" and s.provider and s.provider != provider for s in item.segments):
                    self._set_status(item, "expired", now, note=note)
                    count += 1
            if count:
                self._save(items)
                logger.info("Expired %d queue item(s): %s", count, note)
        return count

    def cursor(self) -> dict | None:
        if not self.cursor_path.exists():
            return None
        try:
            return json.loads(self.cursor_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    # ---------- UI ----------

    def remove(self, item_id: str) -> QueueItem | None:
        """Marks an item `removed` (undo via restore). A playing item stops after its current segment."""
        with self._lock:
            items = self._load()
            item = next((i for i in items if i.id == item_id), None)
            if item is None or item.status not in ACTIVE_STATUSES:
                return item
            item.removed_from = item.status
            self._set_status(item, "removed", _now(), note="entfernt")
            self._save(items)
            return item

    def restore(self, item_id: str) -> QueueItem | None:
        now = _now()
        with self._lock:
            items = self._load()
            item = next((i for i in items if i.id == item_id), None)
            if item is None or item.status != "removed" or item.is_expired(now):
                return item
            item.status = item.removed_from or "queued"
            item.note = item.removed_from = None
            item.updated_at = now.isoformat()
            self._save(items)
            return item

    # ---------- queries ----------

    def active_items(self, now: datetime | None = None) -> list[QueueItem]:
        """Queued/playing items in play order (not-yet-due and expired ones included as they are)."""
        items = [i for i in self.items() if i.status in ACTIVE_STATUSES]
        items.sort(key=lambda i: (LANE_PRIORITY.get(i.lane, len(LANES)), i.status != "playing", i.created_at))
        return items

    def start_estimates(self, current_remaining: float = 0.0, now: datetime | None = None) -> dict[str, datetime]:
        """Estimated start of every queued item in play order, after the rest of the segment on
        air (`current_remaining`, from the player). A playing item has no estimate; an item held
        until `not_before` (e.g. the broadcast start) starts no earlier than that."""
        now = now or _now()
        cursor = now + timedelta(seconds=max(0.0, current_remaining))
        starts: dict[str, datetime] = {}
        for item in self.active_items():
            not_before = _parse(item.not_before)
            if item.status != "playing":
                starts[item.id] = max(cursor, not_before) if not_before else cursor
            if not_before is None or not_before <= cursor:
                cursor += timedelta(seconds=sum(s.estimated_seconds() for s in item.remaining_segments()))
        return starts

    def remaining_program_seconds(self, current_remaining: float = 0.0, now: datetime | None = None) -> float:
        """How long the `program` lane still runs: queued segments + the rest of the current one.

        `current_remaining` comes from the player's memory (started_at/duration of the segment on
        air) and is only added by the caller when that segment belongs to the program lane.
        """
        now = now or _now()
        total = 0.0
        for item in self.items():
            if item.lane != "program" or item.status not in ACTIVE_STATUSES or item.is_expired(now):
                continue
            total += sum(s.estimated_seconds() for s in item.remaining_segments())
        return total + max(0.0, current_remaining)

    def planned_tracks(self) -> list[Segment]:
        """Track segments of all queued/playing items - planned but not (fully) on air yet."""
        return [s for i in self.items() if i.status in ACTIVE_STATUSES for s in i.segments if s.type == "track"]

    def program_tail(self) -> list[Segment]:
        """All segments of active program items in play order - the end of it is what a new block
        continues from. Empty when no program is planned (e.g. right after the broadcast start)."""
        return [s for i in self.active_items() if i.lane == "program" for s in i.segments]

    def last_program_activity(self) -> datetime | None:
        times = [_parse(i.updated_at) for i in self.items() if i.lane == "program" and i.status != "removed"]
        times = [t for t in times if t is not None]
        return max(times) if times else None

    @staticmethod
    def _set_status(item: QueueItem, status: str, now: datetime, note: str | None = None) -> None:
        item.status = status
        item.updated_at = now.isoformat()
        if note is not None:
            item.note = note


def write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
