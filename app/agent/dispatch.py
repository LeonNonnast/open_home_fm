"""The dispatch desk ("Leitstelle"): one small, fast agent run per listener call.

It rates how urgent each request in a call is (sofort / als Nächstes / demnächst / Nachrichten)
and routes it: direct plugin actions (lights) run right away, songs and spoken replies go into
the `reply` lane, "demnächst" wishes into the wish mailbox for the music desk, hints into the
news mailbox.

Program actions are *staged* while the model works and only written when the run succeeded:
`play_next` + `reply` of one call become ONE reply item (announcements first, then the songs),
never two items that could air in the wrong order; a run that fails leaves nothing half planned.
Interrupting the music comes in Phase 4 - until then `play_now`, `breaking` and
`reply(when="now")` exist but run "als Nächstes", and the action says so.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from app.agent.tools import Tool
from app.audio.tts import PiperTTSEngine, TTSEngine
from app.music.base import MusicProvider
from app.program.calls import hhmm, new_action
from app.program.filler import track_segment
from app.program.mailboxes import Mailbox, parse_time
from app.program.queue import REPLY_TTL, ProgramQueue, QueueItem, Segment

logger = logging.getLogger(__name__)

NOW, NEXT, SOON, NEWS = "sofort", "als Nächstes", "demnächst", "Nachrichten"
DOWNGRADED = "Unterbrechen kommt später"
DOWNGRADED_OFF = "Unterbrechen ist ausgeschaltet"
# Songs/episodes one call may put on air - a runaway model must not queue an album.
MAX_PLAYS_PER_CALL = 3
# Episodes run 30-120 min and hold back the whole program: one per call.
MAX_EPISODES_PER_CALL = 1
MAX_VALID_DAYS = 14
# Lookup plugins (weather, news) are cached this long - the dispatch desk has to be quick.
INFO_CACHE_SECONDS = 600
PLUGIN_LABELS = {"control_hue_lights": "Licht"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Staged:
    type: str  # play_next | play_now | reply | breaking | music_wish | news_note
    urgency: str
    text: str | None = None  # announcement / reply / wish / note text
    segment: Segment | None = None  # the song or episode
    valid_until: datetime | None = None
    note: str | None = None


@dataclass
class Committed:
    actions: list[dict[str, Any]]
    item: QueueItem | None
    reply_text: str | None
    reply_audio: str | None
    eta: str | None


class InfoCache:
    """Results of lookup plugins, shared by all dispatch runs (in memory)."""

    def __init__(self, seconds: float = INFO_CACHE_SECONDS):
        self.seconds = seconds
        self._entries: dict[str, tuple[float, str]] = {}

    def wrap(self, tool: Tool) -> Tool:
        def cached(**kwargs: Any) -> str:
            key = f"{tool.name}:{json.dumps(kwargs, sort_keys=True, default=str)}"
            hit = self._entries.get(key)
            if hit and time.monotonic() - hit[0] < self.seconds:
                return hit[1]
            result = tool.func(**kwargs)
            result = result if isinstance(result, str) else str(result)
            if not result.startswith("Error"):
                self._entries[key] = (time.monotonic(), result)
            return result

        return Tool(name=tool.name, description=tool.description, parameters=tool.parameters, func=cached,
                    source=tool.source, action=False)


class DispatchSession:
    """Tools and staging for one call; `commit()` writes the staged actions at the end of the run."""

    def __init__(
        self,
        call: dict[str, Any],
        settings: dict[str, Any],
        queue: ProgramQueue,
        wishes: Mailbox,
        notes: Mailbox,
        provider: MusicProvider | None,
        tts: TTSEngine | None,
        start_estimates: Callable[[], dict[str, datetime]],
        on_air: bool = True,
        next_on_air: datetime | None = None,
        provider_error: str | None = None,
    ):
        self.call = call
        self.settings = settings
        self.queue = queue
        self.wishes = wishes
        self.notes = notes
        self.provider = provider
        self.provider_error = provider_error
        self.tts = tts
        self.start_estimates = start_estimates
        self.on_air = on_air
        self.next_on_air = next_on_air
        self.staged: list[Staged] = []
        # Direct actions already executed - also those of an earlier, failed attempt of this call.
        self.done: list[dict[str, Any]] = [a for a in call.get("actions") or [] if a.get("type") == "plugin"]
        # Called with `done` after every direct action (the desk saves it on the call at once).
        self.on_done: Callable[[list[dict[str, Any]]], Any] | None = None

    # ---------- helpers ----------

    def _downgrade_note(self) -> str:
        return DOWNGRADED if self.settings.get("allow_interrupt", True) else DOWNGRADED_OFF

    def _valid_until(self, value: Any, default_hours: float) -> tuple[datetime, str | None]:
        now = _now()
        parsed = parse_time(value) if value else None
        note = None
        if value and parsed is None:
            note = f"valid_until '{str(value)[:30]}' nicht lesbar (ISO-Datum erwartet), Standard verwendet"
        if parsed is None or parsed <= now:
            parsed = now + timedelta(hours=default_hours)
        return min(parsed, now + timedelta(days=MAX_VALID_DAYS)), note

    def _plays(self) -> list[Staged]:
        return [s for s in self.staged if s.segment is not None]

    def _hold_hint(self) -> str:
        if self.on_air:
            return ""
        start = f" um {hhmm(self.next_on_air)}" if self.next_on_air else ""
        return f" Gerade ist Sendepause: es läuft erst zum Sendebeginn{start}."

    # ---------- tools ----------

    def play(self, kind: str, query: str | None = None, episode_query: str | None = None,
             announce_text: str | None = None, uri: str | None = None, **_ignored: Any) -> str:
        if len(self._plays()) >= MAX_PLAYS_PER_CALL:
            return f"Nicht vorgemerkt: höchstens {MAX_PLAYS_PER_CALL} Titel pro Zwischenruf."
        if self.provider is None:
            return f"Die Musikquelle ist gerade nicht erreichbar ({self.provider_error}). Sag das dem Hörer mit reply."
        if episode_query:
            if sum(1 for s in self._plays() if s.segment.type == "episode") >= MAX_EPISODES_PER_CALL:
                return (f"Nicht vorgemerkt: höchstens {MAX_EPISODES_PER_CALL} Episode pro Zwischenruf "
                        "(Episoden sind lang und halten das Programm auf).")
            if not self.provider.supports_episodes:
                return ("Podcasts/Episoden kann die aktuelle Musikquelle (lokale Bibliothek) nicht abspielen. "
                        "Sag das dem Hörer kurz mit reply.")
            episodes = self.provider.search_episodes(str(episode_query), limit=1)
            if not episodes:
                return f"Keine Episode gefunden für '{episode_query}'."
            ep = episodes[0]
            segment = Segment(type="episode", title=f"{ep.title} - {ep.artist}" if ep.artist else ep.title,
                              audio_ref=ep.uri, provider=self.provider.name, duration_seconds=ep.duration_seconds)
        else:
            wanted = query or uri
            if not wanted:
                return "Fehler: 'query' (Artist - Titel) oder 'episode_query' angeben."
            track = self.provider.get_track_by_uri(str(uri)) if uri else None
            if track is None:
                hits = self.provider.search_tracks(str(wanted), limit=1)
                track = hits[0] if hits else None
            if track is None:
                return f"Kein Song gefunden für '{wanted}'. Versuch eine andere Schreibweise oder sag es dem Hörer."
            segment = track_segment(track, self.provider.name)
        text = str(announce_text).strip() if announce_text else None
        urgency = NOW if kind == "play_now" else NEXT
        note = self._downgrade_note() if kind == "play_now" else None
        self.staged.append(Staged(type=kind, urgency=urgency, text=text, segment=segment, note=note))
        result = f"Vorgemerkt: {segment.title} läuft als Nächstes nach dem aktuellen Song"
        result += " (mit deiner Ansage davor)." if text else "."
        if kind == "play_now":
            result += " Unterbrechen ist in dieser Version noch nicht möglich - deshalb nach dem Song."
        return result + self._hold_hint()

    def reply(self, text: str = "", when: str = "next", **_ignored: Any) -> str:
        text = str(text or "").strip()
        if not text:
            return "Fehler: 'text' fehlt."
        now = str(when or "next").strip().lower() in ("now", "jetzt", "sofort")
        self.staged.append(Staged(type="reply", urgency=NOW if now else NEXT, text=text,
                                  note=self._downgrade_note() if now else None))
        result = "Antwort vorgemerkt: läuft als Ansage nach dem aktuellen Song."
        if now:
            result += " (Unterbrechen ist in dieser Version noch nicht möglich.)"
        return result + self._hold_hint()

    def breaking(self, text: str = "", **_ignored: Any) -> str:
        text = str(text or "").strip()
        if not text:
            return "Fehler: 'text' fehlt."
        self.staged.append(Staged(type="breaking", urgency=NOW, text=text, note=self._downgrade_note()))
        return ("Eilmeldung vorgemerkt: läuft nach dem aktuellen Song (Unterbrechen ist in dieser Version noch "
                "nicht möglich) und kommt zusätzlich ins Meldungs-Postfach für die Nachrichten." + self._hold_hint())

    def add_music_wish(self, text: str = "", valid_until: str | None = None, **_ignored: Any) -> str:
        text = str(text or "").strip()
        if not text:
            return "Fehler: 'text' fehlt."
        until, note = self._valid_until(valid_until, float(self.settings.get("wish_default_valid_hours", 24)))
        self.staged.append(Staged(type="music_wish", urgency=SOON, text=text, valid_until=until, note=note))
        return f"Wunsch vorgemerkt bis {until.astimezone():%d.%m. %H:%M}: die Musikredaktion baut ihn in einen der nächsten Blöcke ein."

    def note_for_news(self, text: str = "", valid_until: str | None = None, **_ignored: Any) -> str:
        text = str(text or "").strip()
        if not text:
            return "Fehler: 'text' fehlt."
        until, note = self._valid_until(valid_until, float(self.settings.get("wish_default_valid_hours", 24)))
        self.staged.append(Staged(type="news_note", urgency=NEWS, text=text, valid_until=until, note=note))
        return f"Hinweis für die Nachrichten vorgemerkt (gültig bis {until.astimezone():%d.%m. %H:%M})."

    def wrap_plugin(self, tool: Tool) -> Tool:
        """A direct action plugin: executed at once, recorded as an action of the call."""
        label = PLUGIN_LABELS.get(tool.name, tool.name)

        def run(**kwargs: Any) -> str:
            args = json.dumps(kwargs, sort_keys=True, default=str)
            earlier = next((a for a in self.done if a.get("target") == tool.name and a.get("args") == args
                            and a["status"] == "done"), None)
            if earlier is not None:
                # Already ran in an earlier attempt of this call (e.g. before a crash).
                return f"Bereits erledigt, nicht wiederholt: {earlier['summary']}"
            try:
                result = tool.func(**kwargs)
                result = result if isinstance(result, str) else str(result)
                failed = result.startswith("Error") or "nicht eingerichtet" in result
            except Exception as exc:
                logger.exception("Dispatch plugin %s failed", tool.name)
                result, failed = f"Fehler: {exc}", True
            action = new_action("plugin", NOW, f"{label}: {result[:160]}", "failed" if failed else "done")
            action["target"] = tool.name
            action["args"] = args
            self.done.append(action)
            if self.on_done is not None:
                try:
                    self.on_done(self.done)
                except Exception:
                    logger.warning("Could not save the direct action of call %s", self.call["id"], exc_info=True)
            return result

        return Tool(name=tool.name, description=tool.description, parameters=tool.parameters, func=run,
                    source=tool.source, action=True)

    def tools(self) -> list[Tool]:
        query_props = {
            "query": {"type": "string", "description": "Song als 'Artist - Titel'"},
            "episode_query": {"type": "string", "description": "Podcast-Episode (Suchtext), statt query"},
            "announce_text": {"type": "string", "description": "optionale kurze Ansage davor (1-2 Sätze)"},
        }
        text_only = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}
        with_valid = {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "valid_until": {"type": "string", "description": "ISO-Datum/-Zeit, bis wann es gilt (optional)"},
            },
            "required": ["text"],
        }
        return [
            Tool("play_next", "Als Nächstes: spielt einen Song (oder eine Podcast-Episode) direkt nach dem "
                 "laufenden Song, optional mit kurzer Ansage davor.",
                 {"type": "object", "properties": query_props}, lambda **kw: self.play("play_next", **kw)),
            Tool("play_now", "Sofort: spielt einen Song/eine Episode jetzt (unterbricht die Musik). In dieser "
                 "Version noch ohne Unterbrechung - läuft nach dem aktuellen Song.",
                 {"type": "object", "properties": query_props}, lambda **kw: self.play("play_now", **kw)),
            Tool("reply", "Antwortet dem Hörer mit einer kurzen gesprochenen Ansage. when='next' (Standard): nach "
                 "dem laufenden Song; when='now': sofort (in dieser Version ebenfalls nach dem Song).",
                 {"type": "object", "properties": {
                     "text": {"type": "string", "description": "gesprochener Text, 1-3 Sätze"},
                     "when": {"type": "string", "enum": ["next", "now"]},
                 }, "required": ["text"]}, self.reply),
            Tool("breaking", "Echte Eilmeldung (Unwetter, Gefahr): Ansage sofort, kommt zusätzlich in die "
                 "Nachrichten. In dieser Version noch ohne Unterbrechung.", text_only, self.breaking),
            Tool("add_music_wish", "Demnächst: legt einen Musikwunsch ins Wunsch-Postfach; die Musikredaktion "
                 "baut ihn in einen der nächsten Blöcke ein.", with_valid, self.add_music_wish),
            Tool("note_for_news", "Nachrichten: legt einen Hinweis für die nächsten Nachrichten ab.",
                 with_valid, self.note_for_news),
        ]

    # ---------- commit ----------

    def _speak(self, text: str) -> tuple[Segment | None, str | None]:
        if self.tts is None:
            return None, "keine Stimme verfügbar"
        try:
            path = self.tts.synthesize(text)
        except Exception as exc:
            logger.warning("Dispatch: TTS failed for %r: %s", text[:60], exc)
            return None, str(exc)
        try:
            duration = PiperTTSEngine.duration_seconds(path)
        except Exception:
            duration = None
        return Segment(type="jingle", title=text[:60], audio_ref=str(path), text=text, duration_seconds=duration), None

    def commit(self) -> Committed:
        """Writes wishes, notes and ONE reply item (announcements first, then the songs)."""
        actions: list[dict[str, Any]] = list(self.done)
        segments: list[Segment] = []
        program: list[dict[str, Any]] = []  # actions that point at the reply item
        replies: list[str] = []
        audio: str | None = None

        for staged in self.staged:
            if staged.type in ("reply", "breaking"):
                segment, error = self._speak(staged.text)
                label = {"reply": "Antwort", "breaking": "Eilmeldung"}[staged.type]
                when = f"als Nächstes statt jetzt – {staged.note}" if staged.note else "als Nächstes"
                action = new_action(staged.type, staged.urgency, f"{label} {when}: „{staged.text[:80]}“",
                                    "queued", note=staged.note)
                if segment is None:
                    action.update(status="failed", note=f"Ansage konnte nicht erzeugt werden: {error}")
                else:
                    segments.append(segment)
                    replies.append(staged.text)
                    audio = audio or segment.audio_ref
                    program.append(action)
                if staged.type == "breaking":
                    entry = self.notes.add(staged.text, self.call.get("author"), self.call["id"],
                                           _now() + timedelta(hours=float(self.settings.get("wish_default_valid_hours", 24))))
                    action["note_id"] = entry["id"]
                actions.append(action)

        for staged in self._plays():
            where = f"als Nächstes statt jetzt – {staged.note}" if staged.note else "als Nächstes"
            action = new_action(staged.type, staged.urgency, f"{where}: {staged.segment.title}", "queued",
                                note=staged.note)
            if staged.text:
                segment, error = self._speak(staged.text)
                if segment is not None:
                    segments.append(segment)
                    replies.append(staged.text)
                    audio = audio or segment.audio_ref
                    action["summary"] += " (mit Ansage)"
                else:
                    action["note"] = "; ".join(n for n in (action["note"], f"ohne Ansage: {error}") if n)
            segments.append(staged.segment)
            program.append(action)
            actions.append(action)

        for staged in self.staged:
            if staged.type == "music_wish":
                entry = self.wishes.add(staged.text, self.call.get("author"), self.call["id"], staged.valid_until)
                actions.append(new_action("music_wish", SOON, f"Musikredaktion: „{staged.text[:80]}“ vorgemerkt "
                                          f"(bis {staged.valid_until.astimezone():%d.%m. %H:%M})", "noted",
                                          note=staged.note, wish_id=entry["id"]))
            elif staged.type == "news_note":
                entry = self.notes.add(staged.text, self.call.get("author"), self.call["id"], staged.valid_until)
                actions.append(new_action("news_note", NEWS, f"Nachrichten: Hinweis „{staged.text[:80]}“ vorgemerkt",
                                          "noted", note=staged.note, note_id=entry["id"]))

        item = None
        eta = None
        if segments:
            kwargs: dict[str, Any] = {"call_id": self.call["id"]}
            if not self.on_air and self.next_on_air is not None:
                # Held until the broadcast starts; it's stale 30 min after that, not after now.
                kwargs["not_before"] = self.next_on_air.isoformat()
            item = QueueItem.new("reply", "dispatch", segments, **kwargs)
            if "not_before" in kwargs:
                item.expires_at = (self.next_on_air + REPLY_TTL).isoformat()
            self.queue.append(item)
            try:
                start = self.start_estimates().get(item.id)
            except Exception:
                logger.warning("Could not estimate the start of %s", item.id, exc_info=True)
                start = None
            eta = start.isoformat() if start else None
            for action in program:
                action.update(queue_item_id=item.id, eta=eta, status="queued" if self.on_air else "held")
        return Committed(actions=actions, item=item, reply_text="\n".join(replies) or None,
                         reply_audio=audio, eta=eta)


def call_line(call: dict[str, Any]) -> str:
    """One line per earlier call for the dispatch context: what was asked and what came of it."""
    who = call.get("author") or "jemand"
    results = "; ".join(f"{a['summary']} [{a['status']}]" for a in call.get("actions") or []) or \
        (call.get("final_message") or "keine Aktion")
    return f"- {hhmm(call.get('created_at'))} {who}: „{call.get('text', '')[:120]}“ → {results}"
