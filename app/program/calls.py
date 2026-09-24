"""Calls ("Zwischenrufe"): what listeners send as text or voice, and what the dispatch desk made of it.

One JSON file per call, `data/calls/<id>.json`:

    {id, created_at, updated_at, submitted_at, author, text, source: text|voice, status,
     actions: [{type, urgency, summary, status, note, eta, queue_item_id, wish_id, note_id,
                undo_available}],
     reply_text, final_message, eta, aired_at, error, attempts, dispatched, audio_file, reply_audio}

Call status: transcribing -> awaiting_confirmation (voice) -> new -> processing -> routed | queued
-> aired, or retrying (LLM unreachable, back to the dispatch desk with backoff) / failed /
expired / removed. Action status: done | failed (direct plugin action), queued | held | playing
| aired | expired | removed (queue item), noted | used | expired | removed (wish / news note).

After the dispatch desk is done with a call, its status follows its actions: `sync()` reads the
queue and the mailboxes (the player never touches calls; it only notifies when an item of a call
starts/ends, see QueuePlayer.on_item_event), so undo in the "Sendung" view, expiry and airing
all show up in the conversation without extra bookkeeping.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from app.program.mailboxes import Mailbox
from app.program.queue import ProgramQueue, QueueItem, write_json_atomic

logger = logging.getLogger(__name__)

PENDING_STATUSES = ("new", "retrying")
# Calls in these states are dropped from disk first when there are more than MAX_CALLS.
FINAL_STATUSES = ("routed", "aired", "failed", "expired", "removed")
MAX_CALLS = 300
# Voice calls stuck before confirmation (never confirmed, or a transcription that never came
# back) may be pruned too once they're this old.
STALE_DRAFT_AFTER = timedelta(days=1)
# A call `processing` this long while the dispatch desk isn't running was left by an error.
STUCK_PROCESSING_MINUTES = 5
ID_PATTERN = re.compile(r"^\d{8}T\d{6}-[0-9a-f]{6}$")
# ETA changes smaller than this don't count as a change of the call (keeps `since` polling quiet).
ETA_TOLERANCE_SECONDS = 30

QUEUE_ACTION_STATUS = {
    "queued": "queued", "playing": "playing", "played": "aired",
    "expired": "expired", "skipped": "expired", "removed": "removed",
}

FIELDS: dict[str, Any] = {
    "id": None, "created_at": None, "updated_at": None, "submitted_at": None, "author": None,
    "text": "", "source": "text", "status": "new", "actions": [], "reply_text": None,
    "final_message": None, "eta": None, "aired_at": None, "error": None, "attempts": 0,
    "dispatched": False, "audio_file": None, "reply_audio": None,
}


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


def hhmm(value: str | datetime | None, fmt: str = "%H:%M") -> str:
    """Local wall-clock time of an ISO string/datetime ("?" when missing)."""
    moment = _parse(value) if isinstance(value, str) else value
    return moment.astimezone().strftime(fmt) if moment else "?"


def new_action(type_: str, urgency: str, summary: str, status: str, note: str | None = None,
               **ids: str | None) -> dict[str, Any]:
    return {
        "type": type_, "urgency": urgency, "summary": summary, "status": status, "note": note,
        "eta": None, "queue_item_id": ids.get("queue_item_id"), "wish_id": ids.get("wish_id"),
        "note_id": ids.get("note_id"), "undo_available": False,
    }


def undo_available(action: dict[str, Any]) -> bool:
    if action.get("queue_item_id"):
        return action.get("status") in ("queued", "held")
    if action.get("wish_id") or action.get("note_id"):
        return action.get("status") == "noted"
    return False


def status_text(call: dict[str, Any]) -> str:
    """What the conversation shows under a call (German, for the UI)."""
    status = call["status"]
    actions = call.get("actions") or []
    queue_states = {a["status"] for a in actions if a.get("queue_item_id")}
    if status == "transcribing":
        return "Wird transkribiert…"
    if status == "awaiting_confirmation":
        return call.get("error") or "Bitte prüfen und absenden"
    if status in ("new", "processing"):
        return "Leitstelle sortiert ein…"
    if status == "retrying":
        return "Leitstelle gerade nicht erreichbar – wird erneut versucht"
    if status == "queued":
        if "playing" in queue_states:
            return "läuft jetzt"
        if "held" in queue_states:
            return f"Sender ruht bis {hhmm(call.get('eta'))} – dein Zwischenruf kommt dann dran"
        eta = _parse(call.get("eta"))
        if eta is not None:
            minutes = max(0, round((eta - _now()).total_seconds() / 60))
            return "läuft nach diesem Song" + (f" (ca. {minutes} min)" if minutes else "")
        return "eingeplant"
    if status == "aired":
        return f"gesendet {hhmm(call.get('aired_at'))}"
    if status == "routed":
        if not actions:
            # The model answered without using a tool: nothing happened, say so (and what it said).
            message = (call.get("final_message") or "").strip()
            return "keine Aktion" + (f": {message[:160]}" if message else "")
        return "erledigt"
    if status == "failed":
        return f"fehlgeschlagen: {call.get('error') or 'unbekannter Fehler'}"
    if status == "expired":
        return "verfallen – nochmal senden?"
    if status == "removed":
        return "entfernt"
    return status


def call_view(call: dict[str, Any]) -> dict[str, Any]:
    view = {**call, "status_text": status_text(call)}
    view["actions"] = [{**a, "undo_available": undo_available(a)} for a in call.get("actions") or []]
    view["reply_audio_url"] = f"/api/calls/{call['id']}/audio" if call.get("reply_audio") else None
    view.pop("audio_file", None)
    view.pop("reply_audio", None)
    return view


def reopen_unplayed_wishes(wishes: Mailbox, items: dict[str, QueueItem]) -> list[str]:
    """A wish counts as used as soon as its block is queued (so the music desk doesn't plan it
    twice) - but only stays used when its segment aired: a block that expired or was removed
    before that gives the wish back to the mailbox (if still valid), a restored block takes it
    again."""
    reopen, retake = [], []
    for wish in wishes.all():
        item = items.get(wish.get("queue_item_id") or "")
        if item is None:
            continue
        index = next((i for i, s in enumerate(item.segments) if s.wish_id == wish["id"]), None)
        aired = index is not None and item.next_segment > index
        if wish["status"] == "used" and item.status in ("expired", "skipped", "removed") and not aired:
            reopen.append(wish["id"])
        elif wish["status"] == "noted" and item.status in ("queued", "playing"):
            retake.append((wish["id"], item.id))
    if reopen:
        wishes.reopen(reopen)
        logger.info("Wishes back in the mailbox (their block didn't air): %s", ", ".join(reopen))
    for wish_id, item_id in retake:
        wishes.mark_used([wish_id], item_id)
    return reopen


class CallStore:
    def __init__(
        self,
        calls_dir: Path,
        queue: ProgramQueue | None = None,
        wishes: Mailbox | None = None,
        notes: Mailbox | None = None,
        start_estimates: Callable[[], dict[str, datetime]] | None = None,
    ):
        self.dir = calls_dir
        self.queue = queue
        self.wishes = wishes
        self.notes = notes
        self.start_estimates = start_estimates or (lambda: queue.start_estimates() if queue else {})
        self._lock = threading.RLock()

    # ---------- persistence ----------

    def _path(self, call_id: str) -> Path:
        if not ID_PATTERN.match(call_id or ""):
            raise KeyError(call_id)
        return self.dir / f"{call_id}.json"

    def _read(self, path: Path) -> dict[str, Any] | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("Could not read call %s", path, exc_info=True)
            return None
        if not isinstance(data, dict):
            return None
        return {**FIELDS, **{k: v for k, v in data.items() if k in FIELDS}}

    def _write(self, call: dict[str, Any]) -> None:
        write_json_atomic(self._path(call["id"]), call)

    def get(self, call_id: str) -> dict[str, Any] | None:
        try:
            path = self._path(call_id)
        except KeyError:
            return None
        with self._lock:
            return self._read(path) if path.exists() else None

    def all(self) -> list[dict[str, Any]]:
        """Newest first."""
        if not self.dir.exists():
            return []
        with self._lock:
            calls = [c for c in (self._read(p) for p in self.dir.glob("*.json")) if c is not None]
        calls.sort(key=lambda c: (c.get("created_at") or "", c["id"]), reverse=True)
        return calls

    def create(self, text: str, author: str | None = None, source: str = "text", status: str = "new",
               created_at: datetime | None = None, audio_file: str | None = None) -> dict[str, Any]:
        created = created_at or _now()
        call = {
            **FIELDS,
            "id": f"{created.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}",
            "created_at": created.isoformat(),
            "updated_at": _now().isoformat(),
            "submitted_at": _now().isoformat() if status == "new" else None,
            "author": (author or "").strip()[:40] or None,
            "text": text.strip(),
            "source": source,
            "status": status,
            "actions": [],
            "audio_file": audio_file,
        }
        with self._lock:
            self._write(call)
            self._prune()
        logger.info("Call %s (%s) from %s: %s", call["id"], status, call["author"] or "?", call["text"][:80])
        return call

    def update(self, call_id: str, change: Callable[[dict[str, Any]], bool | None]) -> dict[str, Any] | None:
        """Applies `change(call)` under the lock and saves; `change` returning False skips saving."""
        with self._lock:
            call = self.get(call_id)
            if call is None:
                return None
            if change(call) is False:
                return call
            call["updated_at"] = _now().isoformat()
            self._write(call)
            return call

    def _prune(self) -> None:
        files = sorted(self.dir.glob("*.json"))
        excess = len(files) - MAX_CALLS
        for path in files:
            if excess <= 0:
                break
            call = self._read(path)
            stale_draft = call is not None and call["status"] in ("transcribing", "awaiting_confirmation") and (
                (_parse(call.get("updated_at")) or _now()) < _now() - STALE_DRAFT_AFTER)
            if call is None or call["status"] in FINAL_STATUSES or stale_draft:
                path.unlink(missing_ok=True)
                for extra in (call or {}).get("audio_file"), (call or {}).get("reply_audio"):
                    if extra and Path(extra).parent == self.dir:
                        Path(extra).unlink(missing_ok=True)
                excess -= 1

    # ---------- dispatch desk ----------

    def pending(self) -> list[dict[str, Any]]:
        """Calls waiting for the dispatch desk, oldest first."""
        return [c for c in reversed(self.all()) if c["status"] in PENDING_STATUSES]

    def has_pending(self) -> bool:
        return bool(self.pending())

    def claim_next(self) -> dict[str, Any] | None:
        """The oldest pending call, marked `processing` (so it's worked on only once)."""
        with self._lock:
            for call in self.pending():
                claimed = self.update(call["id"], lambda c: c.update(status="processing") if c["status"] in PENDING_STATUSES else False)
                if claimed and claimed["status"] == "processing":
                    return claimed
        return None

    def recover_processing(self, older_than_minutes: float | None = None) -> int:
        """Calls left `processing` (by a crash at startup, or by an error while the dispatch desk
        isn't running: `older_than_minutes`) go back to the dispatch desk."""
        cutoff = _now() - timedelta(minutes=older_than_minutes or 0)
        count = 0
        for call in self.all():
            if call["status"] != "processing":
                continue
            if older_than_minutes is not None and (_parse(call.get("updated_at")) or cutoff) > cutoff:
                continue
            recovered = self.update(call["id"], lambda c: c.update(status="new") if c["status"] == "processing" else False)
            if recovered is not None and recovered["status"] == "new":
                logger.info("Call %s was left processing, back to the dispatch desk", call["id"])
                count += 1
        return count

    def interrupted_transcriptions(self) -> list[dict[str, Any]]:
        """At startup: voice calls whose transcription a restart cut off."""
        return [c for c in self.all() if c["status"] == "transcribing"]

    def expire_stale(self, minutes: int) -> int:
        """Pending calls the dispatch desk couldn't handle within `minutes` expire ("Nochmal senden")."""
        cutoff = _now() - timedelta(minutes=minutes)
        count = 0
        for call in self.pending():
            submitted = _parse(call.get("submitted_at") or call.get("created_at"))
            if submitted is not None and submitted < cutoff:
                def expire(c: dict[str, Any]) -> bool | None:
                    if c["status"] not in PENDING_STATUSES:
                        return False
                    c["status"] = "expired"
                    c["error"] = c.get("error") or "Leitstelle nicht erreichbar"
                self.update(call["id"], expire)
                logger.info("Call %s expired after %d min without dispatch", call["id"], minutes)
                count += 1
        return count

    # ---------- following the actions ----------

    def sync(self, call_ids: list[str] | None = None) -> None:
        """Updates the dispatched calls from the queue and the mailboxes."""
        items = {i.id: i for i in self.queue.items()} if self.queue else {}
        if self.wishes is not None:
            reopen_unplayed_wishes(self.wishes, items)
        wishes = {w["id"]: w for w in self.wishes.all()} if self.wishes else {}
        notes = {n["id"]: n for n in self.notes.all()} if self.notes else {}
        estimates: dict[str, datetime] | None = None
        for call in self.all():
            if not call.get("dispatched") or (call_ids is not None and call["id"] not in call_ids):
                continue
            # Final calls are still followed while an action can change: a queue item restored in
            # the UI, a wish back in the mailbox after its block expired.
            if call["status"] in ("removed", "expired", "failed") and not any(
                a.get("wish_id") or a.get("note_id") or a.get("queue_item_id") for a in call["actions"]
            ):
                continue
            if estimates is None:
                try:
                    estimates = self.start_estimates()
                except Exception:
                    logger.warning("Could not estimate start times", exc_info=True)
                    estimates = {}
            proposal = json.loads(json.dumps(call))
            if self._apply_sync(proposal, items, wishes, notes, estimates):
                self.update(call["id"], lambda c, p=proposal, old=call["status"]: c.update(
                    {k: p[k] for k in ("actions", "status", "eta", "aired_at")}
                ) if c["status"] == old else False)

    def on_queue_event(self, item: QueueItem, event: str) -> None:
        """QueuePlayer.on_item_event: an item of a call started or ended on air."""
        if item.call_id:
            self.sync([item.call_id])

    @staticmethod
    def _apply_sync(call: dict[str, Any], items: dict[str, QueueItem], wishes: dict, notes: dict,
                    estimates: dict[str, datetime]) -> bool:
        before = json.dumps(call, sort_keys=True)
        now = _now()
        aired_at = None
        for action in call["actions"]:
            item = items.get(action.get("queue_item_id") or "")
            if item is not None:
                status = QUEUE_ACTION_STATUS.get(item.status, action["status"])
                not_before = _parse(item.not_before)
                if status == "queued" and not_before is not None and not_before > now:
                    status = "held"
                action["status"] = status
                start = estimates.get(item.id)
                old_eta = _parse(action.get("eta"))
                if status in ("queued", "held") and start is not None and (
                    old_eta is None or abs((start - old_eta).total_seconds()) > ETA_TOLERANCE_SECONDS
                ):
                    action["eta"] = start.isoformat()
                if status == "aired":
                    aired_at = item.updated_at
            elif action.get("wish_id") in wishes:
                wish = wishes[action["wish_id"]]
                action["status"] = wish["status"]
                if wish["status"] == "noted":
                    action["note"] = None  # back in the mailbox (its block didn't air)
                block = items.get(wish.get("queue_item_id") or "")
                if wish["status"] == "used" and block is not None:
                    start = estimates.get(block.id)
                    if start is not None:
                        action["note"] = f"im Block ab {hhmm(start)}"
                    elif block.status in ("playing", "played"):
                        action["note"] = "im Programm gespielt" if block.status == "played" else "im laufenden Block"
            elif action.get("note_id") in notes:
                action["status"] = notes[action["note_id"]]["status"]

        queue_states = {a["status"] for a in call["actions"] if a.get("queue_item_id")}
        live = [a for a in call["actions"] if a["status"] not in ("removed", "failed", "expired")]
        if queue_states & {"queued", "held", "playing"}:
            status = "queued"
        elif "aired" in queue_states:
            status = "aired"
        elif call["actions"] and not live and any(a["status"] == "removed" for a in call["actions"]):
            status = "removed"
        elif call["actions"] and not live and any(a["status"] == "expired" for a in call["actions"]):
            status = "expired"
        else:
            status = "routed"
        etas = [a["eta"] for a in call["actions"]
                if a.get("queue_item_id") and a["status"] in ("queued", "held") and a.get("eta")]
        call["status"] = status
        call["eta"] = min(etas) if etas else None
        if status == "aired" and not call.get("aired_at"):
            call["aired_at"] = aired_at or now.isoformat()
        return json.dumps(call, sort_keys=True) != before

    # ---------- undo ----------

    def undo_action(self, call_id: str, index: int) -> tuple[dict[str, Any] | None, str | None]:
        """Removes what action `index` put in place: its queue item (shared by all program
        actions of the call), wish or news note. Returns (call, error message)."""
        call = self.get(call_id)
        if call is None:
            return None, "Zwischenruf nicht gefunden"
        self.sync([call_id])
        call = self.get(call_id)
        actions = call["actions"]
        if not 0 <= index < len(actions):
            return call, "Aktion nicht gefunden"
        action = actions[index]
        if not undo_available(action):
            if action["type"] == "plugin":
                return call, "Direkte Aktionen lassen sich nicht rückgängig machen – schick einfach einen neuen Zwischenruf."
            return call, f"Nicht mehr rückgängig zu machen ({action['status']})."
        if action.get("queue_item_id") and self.queue is not None:
            self.queue.remove(action["queue_item_id"])
        if action.get("wish_id") and self.wishes is not None:
            self.wishes.remove(action["wish_id"])
        if action.get("note_id") and self.notes is not None:
            self.notes.remove(action["note_id"])
        self.sync([call_id])
        return self.get(call_id), None

    def remove_call(self, call_id: str) -> dict[str, Any] | None:
        """Withdraws a whole call: undoes everything still undoable, a pending one is dropped.

        A dispatched call then shows what's left (sync): `removed` only when nothing of it is
        still on air, planned or done - a reply already playing keeps it `queued` until it aired."""
        call = self.get(call_id)
        if call is None:
            return None
        for index, action in enumerate(call["actions"]):
            if undo_available(action):
                self.undo_action(call_id, index)

        def remove(c: dict[str, Any]) -> bool | None:
            if c.get("dispatched"):
                return False
            c["status"] = "removed"
        self.update(call_id, remove)
        self.sync([call_id])
        return self.get(call_id)
