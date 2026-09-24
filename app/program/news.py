"""The news desk's timing and its one program tool: slots, bulletin formats, `schedule_news`.

Slots come from `desks.news.slots` ([{minute: "00", format: full}, {minute: "30", format:
short}]): a bulletin at that minute of every hour, as long as the slot lies inside the broadcast
window. The desk runs `lead_minutes` before a slot (app/scheduler.py) and appends ONE `news`
item with `not_before = slot` and `expires_at = slot + max_delay_minutes`: the player airs it at
the first segment boundary after the slot (`placement: after_song`; `on_time` - cutting the song
- comes with the interruptions in Phase 4 and is treated as after_song until then). A bulletin
that found no segment boundary before `expires_at` is dropped - a missed bulletin is missed.

Slot times are computed from real instants (hour by hour in UTC, then shown in local time), so
they're DST-safe: in the spring the skipped hour has no bulletins, in the autumn the repeated
hour has them twice - as a clock on the wall would.

`expires_at` is capped at the next slot, so a bulletin held back by a long track never airs right
before (or after) the next one; the settings API also demands max_delay_minutes < the smallest
gap between two slots.

News notes (the mailbox the dispatch desk fills): a full bulletin reads the new notes first and
repeats already aired ones until their `valid_until` ("Morgen ist Sperrmüll") - at most
MAX_REPEATED_NOTES per bulletin (the soonest expiring) and each at most every
`note_repeat_hours`; the others are offered as optional. A short one reads only the new notes
(aired ones are optional, "only if it matters again"). A note in a bulletin is `used`; if that
bulletin never aired (expired, removed) the note goes back to `noted` (app/program/calls.py).
Re-preparing a slot ("Jetzt vorbereiten") offers the notes of the queued version it replaces as
new again, and only the notes the new version names stay with the slot.

A bulletin removed in the Sendung view counts as handled: that slot is not prepared again, its
notes come back and go into the next bulletin. A run that finds no usable content (every source
failed or empty, no notes to read) queues nothing instead of letting the model make news up.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any

from app.agent.tools import Tool
from app.audio.tts import PiperTTSEngine, TTSEngine
from app.config import is_broadcast_time
from app.program.mailboxes import Mailbox, parse_time
from app.program.queue import ACTIVE_STATUSES, ProgramQueue, QueueItem, Segment, write_json_atomic

logger = logging.getLogger(__name__)

FORMATS = ("full", "short")
FORMAT_LABELS = {"full": "ausführlich", "short": "kurz"}
PLACEMENTS = ("after_song", "on_time")
SOURCES = ("news", "weather", "notes")
# Plugins behind the sources (context, fetched at the start of every run).
SOURCE_PLUGINS = {"news": "get_news_headlines", "weather": "get_weather"}
MAX_SLOTS = 12
# How far ahead the next slot is searched (a narrow broadcast window may skip a night).
SEARCH_HOURS = 48
# Aired notes a full bulletin has to repeat at most (the soonest expiring first).
MAX_REPEATED_NOTES = 3
# Source results starting like this carry no content (plugin error messages, app/agent/desk.py).
UNAVAILABLE_MARKERS = ("nicht verfügbar", "news konnten nicht", "keine aktuellen nachrichten", "wetter derzeit nicht",
                       "kein ort namens", "fehler")
# Spoken words per minute (Piper, German) - for the length check of a bulletin.
WORDS_PER_MINUTE = 150
# (min words, max words) per format: full ~2-3 min, short ~30-60 s.
FORMAT_WORDS = {"full": (250, 480), "short": (60, 160)}
# (headlines to fetch, with weather forecast) per format.
FORMAT_SOURCES = {"full": (8, True), "short": (4, False)}

HOUR_WORDS = ["null", "ein", "zwei", "drei", "vier", "fünf", "sechs", "sieben", "acht", "neun", "zehn", "elf",
              "zwölf", "dreizehn", "vierzehn", "fünfzehn", "sechzehn", "siebzehn", "achtzehn", "neunzehn",
              "zwanzig", "einundzwanzig", "zweiundzwanzig", "dreiundzwanzig"]
HALF_WORDS = ["zwölf", "eins", "zwei", "drei", "vier", "fünf", "sechs", "sieben", "acht", "neun", "zehn", "elf"]
# A bulletin text that starts like this already has its intro.
INTRO_STARTS = ("die nachrichten", "nachrichten", "die kurznachrichten", "kurznachrichten", "hier sind die")


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------- slots ----------

def normalize_slots(value: Any) -> list[dict[str, str]] | None:
    """[{minute: "00".."59", format: full|short}] sorted by minute, or None if unusable."""
    if not isinstance(value, list) or not value or len(value) > MAX_SLOTS:
        return None
    slots: dict[int, str] = {}
    for raw in value:
        if not isinstance(raw, dict):
            return None
        minute = raw.get("minute")
        if isinstance(minute, bool):
            return None
        try:
            number = int(str(minute).strip())
        except (TypeError, ValueError):
            return None
        fmt = raw.get("format", "full")
        if not 0 <= number <= 59 or fmt not in FORMATS or number in slots:
            return None
        slots[number] = fmt
    return [{"minute": f"{m:02d}", "format": slots[m]} for m in sorted(slots)]


def slot_times(slots: list[dict[str, str]], start: datetime, hours: int = SEARCH_HOURS,
               tz: tzinfo | None = None) -> list[tuple[datetime, str]]:
    """All slots from `start` (exclusive) for `hours`, as aware local times (`tz`, default: the
    system's local time zone) with their format, in time order."""
    base = start.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    seen: dict[datetime, tuple[datetime, str]] = {}
    for k in range(hours + 2):
        local = (base + timedelta(hours=k)).astimezone(tz)
        hour_start = local.replace(minute=0, second=0, microsecond=0)
        for slot in slots:
            at = hour_start.replace(minute=int(slot["minute"]))
            key = at.astimezone(timezone.utc)
            if key > start and key not in seen:
                seen[key] = (at, slot["format"])
    return [seen[k] for k in sorted(seen)]


def slot_on_air(config: dict[str, Any], at: datetime) -> bool:
    """A slot counts when the station is on air at it and a minute later - a slot right at the
    end of the window would never find a song end before the player goes off air. `at` is a
    local time (its wall clock is compared with the window)."""
    return is_broadcast_time(config, at) and is_broadcast_time(config, at + timedelta(minutes=1))


def next_slot(config: dict[str, Any], settings: dict[str, Any], now: datetime | None = None,
              tz: tzinfo | None = None) -> tuple[datetime, str] | None:
    """The next slot after `now` inside the broadcast window: (aware local time, format)."""
    slots = normalize_slots(settings.get("slots"))
    if not slots:
        return None
    now = now or _now()
    if now.tzinfo is None:
        now = now.astimezone()
    for at, fmt in slot_times(slots, now, tz=tz):
        if slot_on_air(config, at):
            return at, fmt
    return None


def slot_configured(settings: dict[str, Any], slot: datetime) -> bool:
    """Is `slot` (an instant) still one of the configured slots (by its local minute)?"""
    slots = normalize_slots(settings.get("slots")) or []
    return slot.astimezone().minute in {int(s["minute"]) for s in slots}


def min_slot_gap(slots: list[dict[str, str]]) -> int:
    """The smallest gap between two consecutive slots in minutes (60 for a single slot)."""
    minutes = sorted(int(s["minute"]) for s in slots)
    return min(b - a for a, b in zip(minutes, minutes[1:] + [minutes[0] + 60]))


def cron_minutes(settings: dict[str, Any]) -> list[int]:
    """Minutes of the hour at which the desk prepares a slot (slot minute - lead_minutes)."""
    slots = normalize_slots(settings.get("slots")) or []
    lead = int(settings.get("lead_minutes", 5))
    return sorted({(int(s["minute"]) - lead) % 60 for s in slots})


def intro_text(slot: datetime, fmt: str) -> str:
    """ "Die Nachrichten um sieben Uhr." / "Die Kurznachrichten um halb acht." """
    local = slot
    what = "Die Nachrichten" if fmt == "full" else "Die Kurznachrichten"
    if local.minute == 0:
        when = "um Mitternacht" if local.hour == 0 else f"um {HOUR_WORDS[local.hour]} Uhr"
    elif local.minute == 30:
        when = f"um halb {HALF_WORDS[(local.hour + 1) % 12]}"
    else:
        when = f"um {local.hour} Uhr {local.minute}"
    return f"{what} {when}."


def hhmm_local(value: datetime | str | None) -> str:
    if isinstance(value, str):
        value = parse_time(value)
    return value.astimezone().strftime("%H:%M") if value else "?"


# ---------- notes ----------

def note_is_valid(note: dict[str, Any], now: datetime | None = None) -> bool:
    """Noted, or used and still before its valid_until (a used note without one aired once)."""
    status = note.get("status")
    if status == "noted":
        return True
    until = parse_time(note.get("valid_until"))
    return status == "used" and until is not None and until > (now or _now())


def notes_for(fmt: str, notes: Mailbox, now: datetime | None = None, replacing: Any = (),
              repeat_hours: float = 0, slot: datetime | None = None) -> tuple[list[dict], list[dict]]:
    """(mandatory, optional) notes for a bulletin of format `fmt`, new ones first.

    `replacing`: ids of the queued items of this slot a re-preparation replaces - their notes
    that never aired count as new again. A full bulletin repeats at most MAX_REPEATED_NOTES aired
    notes (soonest `valid_until` first), each only if its last bulletin (`news_slot`, for a
    replaced one `aired_slot`) is at least `repeat_hours` before `slot`; the rest is optional."""
    now = now or _now()
    replacing = set(replacing or ())
    new, aired = [], []
    for note in notes.all():
        pending = note["status"] == "used" and note.get("queue_item_id") in replacing
        if note["status"] == "noted" or (pending and not note.get("aired_at")):
            new.append(note)
        elif note_is_valid(note, now):
            aired.append(note)
    if fmt != "full":
        return new, aired
    ref = (slot or now).astimezone(timezone.utc)
    due, rest = [], []
    for note in aired:
        key = "aired_slot" if note.get("queue_item_id") in replacing else "news_slot"
        last = parse_time(note.get(key))
        recent = repeat_hours and last is not None and ref - last < timedelta(hours=float(repeat_hours))
        (rest if recent else due).append(note)
    due.sort(key=lambda n: parse_time(n.get("valid_until")) or ref)
    return new + due[:MAX_REPEATED_NOTES], due[MAX_REPEATED_NOTES:] + rest


def queued_bulletins(queue: ProgramQueue, slot: datetime) -> list[str]:
    """Ids of the queued news items of `slot` (what a re-preparation replaces)."""
    slot_utc = slot.astimezone(timezone.utc)
    return [i.id for i in queue.items()
            if i.lane == "news" and i.status == "queued" and parse_time(i.not_before) == slot_utc]


def usable_source(result: Any) -> bool:
    """A source result with content (not empty, not a plugin's error message)."""
    text = str(result or "").strip()
    return bool(text) and not text.casefold().startswith(UNAVAILABLE_MARKERS)


# ---------- last bulletin ----------

def load_last_bulletin(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("text") else None


# ---------- the tool ----------

class NewsSession:
    """`schedule_news` for one run of the news desk, bound to its slot and format."""

    def __init__(
        self,
        slot: datetime,
        fmt: str,
        settings: dict[str, Any],
        queue: ProgramQueue,
        notes: Mailbox,
        tts: TTSEngine | None,
        offered_notes: tuple[list[dict], list[dict]] = ([], []),
        last_path: Path | None = None,
        still_wanted: Any = None,
        replacing: Any = (),
    ):
        self.slot = slot
        self.fmt = fmt
        self.settings = settings
        self.queue = queue
        self.notes = notes
        self.tts = tts
        self.mandatory, self.optional = offered_notes
        self.last_path = last_path
        # () -> bool: the slot is still configured and the desk enabled (re-checked when scheduling).
        self.still_wanted = still_wanted
        self.replacing = set(replacing or ())
        self.item: QueueItem | None = None
        self.used_notes: list[str] = []

    @property
    def expires_at(self) -> datetime:
        # In UTC: Python adds to/compares datetimes of one tzinfo by wall clock (wrong across DST).
        slot_utc = self.slot.astimezone(timezone.utc)
        expires = slot_utc + timedelta(minutes=int(self.settings.get("max_delay_minutes", 15)))
        # Never past the next slot: two bulletins must not air back to back.
        following = slot_times(normalize_slots(self.settings.get("slots")) or [], slot_utc, hours=2)
        if following:
            expires = min(expires, following[0][0].astimezone(timezone.utc))
        return expires

    def schedule_news(self, text: str = "", note_ids: Any = None, **_ignored: Any) -> str:
        text = " ".join(str(text or "").split())
        if not text:
            return "Fehler: 'text' fehlt - der komplette Sprechtext der Ausgabe."
        if self.item is not None:
            return f"Die Ausgabe für {hhmm_local(self.slot)} ist schon eingeplant. Beende den Durchlauf."
        if _now() >= self.expires_at:
            return f"Zu spät: die Ausgabe für {hhmm_local(self.slot)} wäre schon verfallen. Beende den Durchlauf."
        if self.still_wanted is not None and not self.still_wanted():
            return (f"Der Slot {hhmm_local(self.slot)} ist nicht mehr eingeplant (Slots geändert oder Redaktion "
                    "ausgeschaltet) - nichts eingeplant. Beende den Durchlauf.")
        if self.tts is None:
            return "Keine Stimme verfügbar - die Nachrichten können nicht gesprochen werden."

        spoken = text
        if self.settings.get("intro", True) and not text.casefold().startswith(INTRO_STARTS):
            spoken = f"{intro_text(self.slot, self.fmt)} {text}"
        try:
            path = self.tts.synthesize(spoken)
        except Exception as exc:
            logger.warning("News: TTS failed: %s", exc)
            return f"Die Ausgabe konnte nicht gesprochen werden ({exc})."
        try:
            duration = PiperTTSEngine.duration_seconds(path)
        except Exception:
            duration = None

        label = f"Nachrichten {hhmm_local(self.slot)}"
        segment = Segment(type="jingle", title=label, audio_ref=str(path), text=spoken, duration_seconds=duration)
        slot_utc = self.slot.astimezone(timezone.utc)
        replaced = self._replace_earlier(slot_utc)
        item = QueueItem.new("news", "news", [segment], not_before=slot_utc.isoformat())
        item.expires_at = self.expires_at.astimezone(timezone.utc).isoformat()
        self.queue.append(item)
        self.item = item

        offered = {n["id"] for n in self.mandatory + self.optional}
        wanted = _id_list(note_ids)
        # Only what this version reads stays with the slot; notes of a replaced version it leaves
        # out go back to the mailbox (app/program/calls.py) and into the next bulletin.
        ids = [i for i in wanted if i in offered] if wanted is not None else [n["id"] for n in self.mandatory]
        self.used_notes = self.notes.mark_in_bulletin(ids, item.id, slot_utc.isoformat(),
                                                      replacing=self.replacing | set(replaced)) if ids else []
        if self.last_path is not None:
            write_json_atomic(self.last_path, {
                "slot": slot_utc.isoformat(), "format": self.fmt, "text": text, "item_id": item.id,
                "created_at": _now().isoformat(), "duration_seconds": duration,
            })

        result = (
            f"Ausgabe für {hhmm_local(self.slot)} ({FORMAT_LABELS[self.fmt]}) eingeplant: läuft nach dem Song, "
            f"der um {hhmm_local(self.slot)} läuft, und verfällt um {hhmm_local(self.expires_at)}."
        )
        low, high = FORMAT_WORDS[self.fmt]
        words = len(text.split())
        if words < low:
            result += f" Hinweis: mit {words} Wörtern recht kurz (Ziel {low}-{high})."
        elif words > high:
            result += f" Hinweis: mit {words} Wörtern recht lang (Ziel {low}-{high})."
        if self.used_notes:
            result += f" Hinweise verwendet: {', '.join(self.used_notes)}."
        unknown = sorted(set(wanted or []) - offered)
        if unknown:
            result += f" Unbekannte note_ids ignoriert: {', '.join(unknown)}."
        if replaced:
            result += " Die vorher vorbereitete Ausgabe für diesen Slot wurde ersetzt."
        return result + " Beende jetzt den Durchlauf."

    def _replace_earlier(self, slot_utc: datetime) -> list[str]:
        """A second preparation of the same slot ("Jetzt vorbereiten") replaces the first one."""
        replaced = []
        for item in self.queue.items():
            if item.lane == "news" and item.status == "queued" and parse_time(item.not_before) == slot_utc:
                self.queue.remove(item.id)
                replaced.append(item.id)
        return replaced

    def tool(self) -> Tool:
        return Tool(
            name="schedule_news",
            description=(
                "Plant die fertige Nachrichtenausgabe ein: der komplette Sprechtext als Fließtext (ohne "
                "Überschriften, Aufzählungen oder Markdown). Wird gesprochen und läuft nach dem Song, der zum "
                "Slot läuft. Genau einmal pro Durchlauf aufrufen."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "kompletter Sprechtext der Ausgabe"},
                    "note_ids": {"type": "array", "items": {"type": "string"},
                                 "description": "ids der verwendeten Hinweise (Standard: alle Pflicht-Hinweise)"},
                },
                "required": ["text"],
            },
            func=self.schedule_news,
        )


def _id_list(value: Any) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = [v.strip() for v in value.split(",") if v.strip()]
    if isinstance(value, str):
        value = [value]
    return [str(v) for v in value] if isinstance(value, list) else None


def prepared_for(queue: ProgramQueue, slot: datetime) -> QueueItem | None:
    """The news item of `slot`, if one was prepared: queued, on air, aired - or removed/skipped by
    hand (the slot counts as handled, its notes go into the next bulletin). Expired ones don't count."""
    slot_utc = slot.astimezone(timezone.utc)
    for item in reversed(queue.items()):
        if item.lane == "news" and (item.status in ACTIVE_STATUSES or item.status in ("played", "removed", "skipped")) \
                and parse_time(item.not_before) == slot_utc:
            return item
    return None


def drop_unconfigured(queue: ProgramQueue, settings: dict[str, Any], enabled: bool) -> list[str]:
    """Expires the queued bulletins of slots no longer configured (all of them when the desk is
    off); their notes come back with the next calls sync. Returns the expired item ids."""
    stale = [i.id for i in queue.items() if i.lane == "news" and i.status == "queued"
             and (not enabled or (at := parse_time(i.not_before)) is None or not slot_configured(settings, at))]
    return queue.expire_items(stale, "Slot nicht mehr eingeplant") if stale else []
