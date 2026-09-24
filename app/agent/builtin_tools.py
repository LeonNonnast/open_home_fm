"""Builtin tools: plain functions exposing the music provider (Spotify or local) plus the
script/jingle tools - no MCP server involved, just Tool objects calling the provider APIs
directly.

These are registered directly (not discovered from ./plugins) because they're core to the
agent, not user-extensible - but they use the exact same Tool/ToolRegistry contract as plugins.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from app.agent.script import Script, Segment, save_script
from app.agent.tools import Tool
from app.audio.tts import TTSEngine
from app.music.base import Device, MusicProvider, Track

logger = logging.getLogger(__name__)


# Rough length of a spoken segment, used for the program length estimate (TTS audio length isn't
# known without decoding the file).
JINGLE_ESTIMATE_SECONDS = 20
# Tracks whose duration the provider doesn't report (some local files).
TRACK_ESTIMATE_SECONDS = 210


def build_builtin_tools(
    provider: MusicProvider, tts_engine: TTSEngine, script_path: Path, min_program_minutes: int = 0
) -> list[Tool]:
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

    def play_on_device(track_uri: str, device_id: str | None = None) -> str:
        """Ad-hoc immediate playback, outside of the deterministic script - use sparingly.

        `track_uri` must be a uri as returned by search_songs/get_playlist_tracks, not free text.
        """
        device = Device(id=device_id, name=device_id) if device_id else None
        track = Track(id=track_uri, title=track_uri, artist="", uri=track_uri)
        provider.play(track, device)
        return f"Spiele jetzt: {track_uri}"

    # Tool-calling models don't always stick to the enum in the schema below (observed in
    # practice: a model announcing spoken segments as "announcement" instead of "jingle") -
    # tolerate the common synonyms rather than silently dropping the segment.
    JINGLE_TYPE_ALIASES = {"jingle", "announcement", "announce", "tts", "speech", "ansage", "voice"}

    def set_playback_script(segments: list[dict[str, Any]]) -> str:
        """Finalizes this loop iteration's program. Called exactly once, at the end.

        Each segment is either:
          {"type": "track", "query": "artist - title"}
          {"type": "jingle", "text": "kurzer gesprochener Text"}
        """
        resolved: list[Segment] = []
        for raw in segments:
            seg_type = (raw.get("type") or "").lower()
            if seg_type not in {"track", *JINGLE_TYPE_ALIASES}:
                # Some models drop the `type` field entirely (observed in practice) - infer it
                # from whichever content field was actually supplied rather than dropping the
                # segment outright.
                if raw.get("uri") or raw.get("query"):
                    seg_type = "track"
                elif raw.get("text"):
                    seg_type = "jingle"

            if seg_type == "track":
                # Models sometimes echo the `uri` from an earlier search_songs result instead
                # of restating the search text - try that exact lookup before falling back to
                # a fresh text search.
                uri = raw.get("uri")
                query = raw.get("query", "")
                track = provider.get_track_by_uri(uri) if uri else None
                if track is None:
                    candidates = provider.search_tracks(query or uri or "", limit=1)
                    track = candidates[0] if candidates else None
                if track is None:
                    logger.warning("set_playback_script: no track resolved for %r, skipping", raw)
                    continue
                title = f"{track.title} - {track.artist}" if track.artist else track.title
                resolved.append(
                    Segment(
                        type="track",
                        title=title,
                        audio_ref=track.uri,
                        provider=provider.name,
                        duration_seconds=track.duration_seconds,
                    )
                )
            elif seg_type in JINGLE_TYPE_ALIASES:
                text = (raw.get("text") or "").strip()
                if not text:
                    continue
                audio_path = tts_engine.synthesize(text)
                resolved.append(
                    Segment(
                        type="jingle",
                        title=text[:60],
                        audio_ref=str(audio_path),
                        text=text,
                    )
                )
            else:
                logger.warning("set_playback_script: unknown segment type '%s', skipping", seg_type)

        script = Script.new(resolved)
        save_script(script, script_path)
        total_minutes = round(
            sum(
                (seg.duration_seconds or TRACK_ESTIMATE_SECONDS) if seg.type == "track" else JINGLE_ESTIMATE_SECONDS
                for seg in resolved
            )
            / 60
        )
        result = f"Script {script.id} mit {len(resolved)} Segmenten (ca. {total_minutes} Minuten) gespeichert."
        if total_minutes < min_program_minutes:
            # The script is saved either way, so a model that stops here still leaves something
            # playable - but the nudge usually gets it to extend the program instead.
            result += (
                f" Das ist zu kurz: das Programm muss mindestens {min_program_minutes} Minuten füllen, "
                "sonst entsteht Stille bis zum nächsten Durchlauf. Ergänze weitere Songs und rufe "
                "set_playback_script mit dem vollständigen, verlängerten Programm erneut auf."
            )
        return result

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
            name="play_on_device",
            description="Spielt einen Track sofort ab (außerhalb des regulären Scripts). Nur für Sonderfälle.",
            parameters={
                "type": "object",
                "properties": {
                    "track_uri": {"type": "string"},
                    "device_id": {"type": "string"},
                },
                "required": ["track_uri"],
            },
            func=play_on_device,
        ),
        Tool(
            name="set_playback_script",
            description=(
                "Finalisiert das Sendeprogramm dieses Durchlaufs als geordnete Liste aus Song- "
                "und Jingle-Segmenten. Am Ende des Durchlaufs aufrufen. Track-Segmente: 'query' "
                "(Suchtext 'Artist - Titel', wird automatisch aufgelöst - vorheriges search_songs "
                "ist nicht nötig) oder 'uri' (aus search_songs/get_playlist_tracks). Jingle-"
                "Segmente: 'text' (wird als Audio gesprochen)."
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
                            },
                            "required": ["type"],
                        },
                    }
                },
                "required": ["segments"],
            },
            func=set_playback_script,
        ),
    ]
