"""Builtin tools of the music desk: plain functions exposing the music provider (Spotify or local)
plus the program tools that append to the queue - no MCP server involved, just Tool objects
calling the provider APIs directly.

These are registered directly (not discovered from ./plugins) because they're core to the
agent, not user-extensible - but they use the exact same Tool/ToolRegistry contract as plugins.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable

from app.agent.tools import Tool
from app.audio.tts import PiperTTSEngine, TTSEngine
from app.music.base import MusicProvider, Track
from app.program.filler import save_reserve, track_segment
from app.program.mailboxes import Mailbox
from app.program.queue import ProgramQueue, QueueItem, Segment

logger = logging.getLogger(__name__)

# Upper bound for update_reserve, so a runaway model can't dump a whole playlist in there.
MAX_RESERVE_TRACKS = 30

# Tool-calling models don't always stick to the enum in the schema below (observed in
# practice: a model announcing spoken segments as "announcement" instead of "jingle") -
# tolerate the common synonyms rather than silently dropping the segment.
JINGLE_TYPE_ALIASES = {"jingle", "announcement", "announce", "tts", "speech", "ansage", "voice"}


def _as_list(value: Any) -> list[Any] | None:
    """An array argument as a list, or None if unusable. Some models (seen with Ollama) send
    the array as a JSON string, or a single object instead of a one-element array."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    if isinstance(value, dict):
        return [value]
    return value if isinstance(value, list) else None


def songs_since_last_announcement(segments: list[Segment]) -> int | None:
    """Songs after the last jingle in `segments`; None when there's no announcement at all."""
    count = 0
    for seg in reversed(segments):
        if seg.type == "jingle":
            return count
        count += 1
    return None


def build_builtin_tools(
    provider: MusicProvider,
    tts_engine: TTSEngine,
    queue: ProgramQueue,
    *,
    recent_tracks: list[dict] | None = None,
    block_minutes: int = 20,
    songs_per_announcement: int = 3,
    max_queued_program_minutes: int = 45,
    remaining_program_seconds: Callable[[], float] | None = None,
    reserve_path: Path | None = None,
    desk: str = "music",
    appended: list[QueueItem] | None = None,
    wishes: Mailbox | None = None,
) -> list[Tool]:
    """`appended` collects the items this run added to the queue (for the transcript). A segment's
    `wish_id` marks that wish in `wishes` as used once its block is queued."""
    remaining = remaining_program_seconds or queue.remaining_program_seconds
    appended = appended if appended is not None else []
    # Matched by uri and by title: the same song often exists under several uris (single, album,
    # remaster), and the model tends to pick whichever version the search returns first.
    recent_uris = {e["uri"] for e in recent_tracks or []}
    recent_titles = {e["title"].casefold() for e in recent_tracks or []}
    run_seconds = [0.0]  # program minutes this run appended over all calls

    def resolve(raw: dict[str, Any]) -> Track | None:
        # Models sometimes echo the `uri` from an earlier search_songs result instead of
        # restating the search text - try that exact lookup before a fresh text search.
        uri = str(raw["uri"]) if raw.get("uri") else None
        query = str(raw.get("query") or "")
        track = provider.get_track_by_uri(uri) if uri else None
        if track is None:
            candidates = provider.search_tracks(query or uri or "", limit=1)
            track = candidates[0] if candidates else None
        return track

    def search_songs(query: str, limit: int = 5) -> str:
        tracks = provider.search_tracks(query, limit=limit)
        if not tracks:
            return f"Keine Treffer für '{query}'."
        return "\n".join(
            f"- {t.title}{f' von {t.artist}' if t.artist else ''} (uri={t.uri}, dauer={t.duration_seconds or '?'}s)"
            for t in tracks
        )

    def get_playlists() -> str:
        playlists = provider.list_playlists()
        if not playlists:
            return "Keine Playlists gefunden."
        return "\n".join(f"- {p.name} (id={p.id}, {p.track_count or '?'} Songs)" for p in playlists)

    def get_playlist_tracks(playlist_id: str) -> str:
        tracks = provider.get_playlist_tracks(playlist_id)
        if not tracks:
            return f"Playlist '{playlist_id}' ist leer oder existiert nicht."
        return "\n".join(f"- {t.title}{f' von {t.artist}' if t.artist else ''} (uri={t.uri})" for t in tracks)

    def find_devices() -> str:
        devices = provider.list_devices()
        if not devices:
            return "Keine Wiedergabegeräte gefunden."
        return "\n".join(
            f"- {d.name} (id={d.id}){' [aktiv]' if d.is_active else ''}" for d in devices
        )

    def append_program_block(segments: list[dict[str, Any]]) -> str:
        """Appends one block of songs and announcements to the end of the program queue.

        Each segment is either:
          {"type": "track", "query": "artist - title"}  (or "uri")
          {"type": "jingle", "text": "kurzer gesprochener Text"}
        """
        items = _as_list(segments)
        if items is None:
            return (
                "Kein Block angehängt: 'segments' muss eine Liste von Segment-Objekten sein "
                f"(erhalten: {str(segments)[:80]!r})."
            )
        # Planned but not yet played counts as much as played: never queue a song twice.
        blocked_uris = set(recent_uris)
        blocked_titles = set(recent_titles)
        for seg in queue.planned_tracks():
            blocked_uris.add(seg.audio_ref)
            blocked_titles.add(seg.title.casefold())

        room = max_queued_program_minutes * 60 - remaining()
        if room <= 0:
            return (
                f"Nicht angehängt: die Warteschlange ist voll (Obergrenze {max_queued_program_minutes} "
                "Minuten Programm). Beende den Durchlauf."
            )

        # The announcement rate is checked across the block boundary: the end of the already
        # queued program counts, so a new block doesn't start with a second greeting.
        since = songs_since_last_announcement(queue.program_tail())
        resolved: list[Segment] = []
        skipped_repeats: list[str] = []
        skipped_announcements: list[str] = []
        failed: list[str] = []
        ignored: list[str] = []
        truncated = 0
        total = 0.0

        def add(raw: dict[str, Any], number: int) -> None:
            nonlocal since, total, truncated
            seg_type = str(raw.get("type") or "").strip().lower()
            if seg_type not in {"track", *JINGLE_TYPE_ALIASES}:
                # Some models drop the `type` field entirely (observed in practice) - infer it
                # from whichever content field was actually supplied.
                if raw.get("uri") or raw.get("query"):
                    seg_type = "track"
                elif raw.get("text"):
                    seg_type = "jingle"

            if seg_type == "track":
                track = resolve(raw)
                if track is None:
                    logger.warning("append_program_block: no track resolved for %r, skipping", raw)
                    failed.append(str(raw.get("query") or raw.get("uri") or "?"))
                    return
                segment = track_segment(track, provider.name)
                segment.wish_id = raw.get("wish_id")
                key = segment.title.casefold()
                if track.uri in blocked_uris or key in blocked_titles:
                    skipped_repeats.append(segment.title)
                    return
                if total + segment.estimated_seconds() > room:
                    truncated += 1
                    return
                blocked_uris.add(track.uri)
                blocked_titles.add(key)
                resolved.append(segment)
                total += segment.estimated_seconds()
                since = None if since is None else since + 1
            elif seg_type in JINGLE_TYPE_ALIASES:
                text = str(raw.get("text") or "").strip()
                if not text:
                    ignored.append(f"#{number}: Ansage ohne 'text'")
                    return
                if since is not None and since < songs_per_announcement:
                    skipped_announcements.append(text[:60])
                    return
                try:
                    audio_path = tts_engine.synthesize(text)
                except Exception as exc:
                    logger.warning("append_program_block: TTS failed for %r: %s", text[:60], exc)
                    failed.append(f"Ansage '{text[:40]}' ({exc})")
                    return
                try:
                    duration = PiperTTSEngine.duration_seconds(audio_path)
                except Exception:
                    duration = None
                segment = Segment(
                    type="jingle", title=text[:60], audio_ref=str(audio_path), text=text,
                    duration_seconds=duration, wish_id=raw.get("wish_id"),
                )
                resolved.append(segment)
                total += segment.estimated_seconds()
                since = 0
            else:
                logger.warning("append_program_block: unknown segment type '%s', skipping", seg_type)
                ignored.append(f"#{number}: unbekannter Typ '{seg_type[:20]}' (erlaubt: track, jingle)")

        for number, raw in enumerate(items, 1):
            if not isinstance(raw, dict):
                ignored.append(f"#{number}: kein Objekt ({str(raw)[:40]!r})")
                continue
            # One broken segment must not cost the whole block (incl. announcements already rendered).
            try:
                add(raw, number)
            except Exception as exc:
                logger.warning("append_program_block: segment %d %r failed, skipping", number, raw, exc_info=True)
                ignored.append(f"#{number}: Fehler ({exc})")

        notes = []
        if skipped_repeats:
            notes.append("Entfernt, weil kürzlich gespielt oder schon eingeplant: " + "; ".join(skipped_repeats) + ".")
        if skipped_announcements:
            notes.append(
                f"Ansage entfernt (höchstens eine Ansage je {songs_per_announcement} Songs, auch über die "
                "Blockgrenze hinweg): " + "; ".join(f"'{t}'" for t in skipped_announcements) + "."
            )
        if failed:
            notes.append("Nicht auflösbar: " + "; ".join(failed) + ".")
        if ignored:
            notes.append("Ignorierte Segmente: " + "; ".join(ignored) + ".")
        if truncated:
            notes.append(
                f"{truncated} Song(s) weggelassen: sonst wäre die Obergrenze von {max_queued_program_minutes} "
                "Minuten Programm überschritten."
            )

        if not any(s.type == "track" for s in resolved):
            return "Kein Block angehängt: kein Song übrig. " + " ".join(notes)

        open_wishes = {w["id"] for w in wishes.open()} if wishes is not None else set()
        unknown = sorted({str(seg.wish_id) for seg in resolved if seg.wish_id and seg.wish_id not in open_wishes})
        for seg in resolved:
            if seg.wish_id and seg.wish_id not in open_wishes:
                seg.wish_id = None
        if unknown:
            notes.append("Unbekannte oder schon erfüllte wish_id ignoriert: " + ", ".join(unknown) + ".")

        item = queue.append(QueueItem.new("program", desk, resolved))
        appended.append(item)
        if wishes is not None:
            used = wishes.mark_used(list({seg.wish_id for seg in resolved if seg.wish_id}), item.id)
            if used:
                notes.append(f"Wunsch erfüllt: {', '.join(used)}.")
        run_seconds[0] += total
        result = (
            f"Block {item.id} mit {len(resolved)} Segmenten (ca. {round(total / 60)} Minuten) angehängt. "
            f"Das Programm reicht jetzt ca. {round(remaining() / 60)} Minuten."
        )
        if notes:
            result += " " + " ".join(notes)
        if round(run_seconds[0] / 60) < block_minutes and not truncated:
            # The block is queued either way, so a model that stops here still leaves something
            # playable - but the nudge usually gets it to add more.
            result += (
                f" Das ist zu kurz: plane in diesem Durchlauf insgesamt ca. {block_minutes} Minuten ein. "
                "Rufe append_program_block mit weiteren Songs erneut auf - sie werden hinten angehängt."
            )
        return result

    def set_playback_script(segments: Any = None, **kwargs: Any) -> str:
        # The pre-queue tool name, still in owner-customized prompts: same as append_program_block.
        if segments is None:
            segments = kwargs.get("script") or kwargs.get("items") or []
        return append_program_block(segments)

    def update_reserve(tracks: list[Any]) -> str:
        """Replaces the reserve: songs the player falls back on when no program is queued."""
        entries: list[dict[str, Any]] = []
        seen: set[str] = set()
        skipped: list[str] = []
        items = _as_list(tracks)
        if items is None:
            items = [tracks] if isinstance(tracks, str) else []  # a single search text
        for raw in items:
            raw = {"query": raw} if isinstance(raw, str) else raw
            if not isinstance(raw, dict):
                continue
            track = resolve(raw)
            if track is None:
                continue
            segment = track_segment(track, provider.name)
            key = segment.title.casefold()
            if key in seen or track.uri in seen:
                continue
            if track.uri in recent_uris or key in recent_titles:
                skipped.append(segment.title)
                continue
            seen.update({key, track.uri})
            entries.append({
                "uri": track.uri, "title": segment.title,
                "duration_seconds": track.duration_seconds, "provider": provider.name,
            })
            if len(entries) >= MAX_RESERVE_TRACKS:
                break
        if not entries:
            return "Reserve unverändert: kein Song auflösbar."
        save_reserve(reserve_path or Path("data/reserve.json"), entries)
        result = f"Reserve mit {len(entries)} Songs gespeichert."
        if skipped:
            result += " Entfernt, weil kürzlich gespielt: " + "; ".join(skipped) + "."
        return result

    track_items = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Suchtext 'Artist - Titel'"},
            "uri": {"type": "string", "description": "uri aus search_songs/get_playlist_tracks"},
        },
    }
    return [
        Tool(
            name="search_songs",
            description="Sucht Songs in der aktiven Musikquelle (Spotify oder lokale Bibliothek).",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Suchbegriff, z.B. 'Artist - Titel'"},
                    "limit": {"type": "integer", "description": "Max. Anzahl Treffer", "default": 5},
                },
                "required": ["query"],
            },
            func=search_songs,
        ),
        Tool(
            name="get_playlists",
            description="Listet verfügbare Playlists der aktiven Musikquelle auf.",
            parameters={"type": "object", "properties": {}},
            func=get_playlists,
        ),
        Tool(
            name="get_playlist_tracks",
            description="Listet die Songs einer bestimmten Playlist auf.",
            parameters={
                "type": "object",
                "properties": {"playlist_id": {"type": "string"}},
                "required": ["playlist_id"],
            },
            func=get_playlist_tracks,
        ),
        Tool(
            name="find_devices",
            description="Listet verfügbare Wiedergabegeräte auf.",
            parameters={"type": "object", "properties": {}},
            func=find_devices,
        ),
        Tool(
            name="append_program_block",
            description=(
                "Hängt einen Programmblock aus Song- und Ansage-Segmenten hinten an die "
                "Warteschlange an (das laufende Programm bleibt unverändert). Track-Segmente: "
                "'query' (Suchtext 'Artist - Titel', wird automatisch aufgelöst - vorheriges "
                "search_songs ist nicht nötig) oder 'uri' (aus search_songs/get_playlist_tracks). "
                "Ansage-Segmente (type 'jingle'): 'text' (wird als Audio gesprochen). Songs, die "
                "kürzlich liefen oder schon eingeplant sind, und zu dichte Ansagen werden entfernt."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "segments": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "type": {"type": "string", "enum": ["track", "jingle"]},
                                "query": {"type": "string"},
                                "uri": {"type": "string"},
                                "text": {"type": "string"},
                                "wish_id": {"type": "string", "description": "id des erfüllten Hörerwunschs"},
                            },
                            "required": ["type"],
                        },
                    }
                },
                "required": ["segments"],
            },
            func=append_program_block,
        ),
        Tool(
            # Tolerant alias: owner-customized prompts from before the queue still name it.
            name="set_playback_script",
            description="Veraltet - wie append_program_block (bitte append_program_block verwenden).",
            parameters={
                "type": "object",
                "properties": {"segments": {"type": "array", "items": {"type": "object"}}},
                "required": ["segments"],
            },
            func=set_playback_script,
        ),
        Tool(
            name="update_reserve",
            description=(
                "Ersetzt die Reserve: 15-20 Songs, die ohne Ansagen laufen, falls die Redaktion "
                "einmal kein Programm liefert. Einmal pro Durchlauf aufrufen, nach denselben Regeln "
                "auswählen wie das Programm."
            ),
            parameters={
                "type": "object",
                "properties": {"tracks": {"type": "array", "items": track_items}},
                "required": ["tracks"],
            },
            func=update_reserve,
        ),
    ]
