"""Filler program: what the player plays when the program lane is empty - no LLM involved.

1. The reserve (`data/reserve.json`), picked by the music desk on every run (`update_reserve`)
   with the same rules as the program.
2. Once that's used up (or was never filled): random tracks from `music.favorite_playlists`,
   or - for the local source without favorites - from the whole library.

Both are filtered by the no-repeat window and by the tracks that just failed to play (Spotify
403/region lock, missing local file - the player reports them; they never reach the play
history, so without this the filler would pick the same dead track forever). If *everything*
played recently (small local library), a random candidate is played anyway: repeating a song
beats going silent - but never one that just failed.
"""
from __future__ import annotations

import json
import logging
import random
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.agent.play_history import recently_played
from app.music.base import MusicProvider, Track
from app.program.queue import ProgramQueue, Segment, write_json_atomic

logger = logging.getLogger(__name__)

# Favorite playlists are re-read from the provider at most this often (Spotify API calls).
PLAYLIST_CACHE_SECONDS = 1800
# A track that failed to play is not picked again for this long (in memory only).
FAILED_TRACK_SECONDS = 1800


def load_reserve(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"updated_at": None, "tracks": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("Could not read reserve %s", path, exc_info=True)
        return {"updated_at": None, "tracks": []}
    tracks = [t for t in data.get("tracks", []) if isinstance(t, dict) and t.get("uri")]
    return {"updated_at": data.get("updated_at"), "tracks": tracks}


def save_reserve(path: Path, tracks: list[dict[str, Any]]) -> None:
    write_json_atomic(path, {"updated_at": datetime.now(timezone.utc).isoformat(), "tracks": tracks})


def track_segment(track: Track, provider_name: str) -> Segment:
    title = f"{track.title} - {track.artist}" if track.artist else track.title
    return Segment(
        type="track", title=title, audio_ref=track.uri, provider=provider_name, duration_seconds=track.duration_seconds
    )


class FillerSource:
    def __init__(
        self,
        provider: MusicProvider,
        queue: ProgramQueue,
        reserve_path: Path,
        play_history_path: Path | None,
        rng: random.Random | None = None,
    ):
        self.provider = provider
        self.queue = queue
        self.reserve_path = reserve_path
        self.play_history_path = play_history_path
        self.rng = rng or random.Random()
        self._playlist_cache: tuple[float, tuple, list[Track]] | None = None
        self._failed: dict[str, float] = {}  # uri -> time.monotonic() of the failure
        self._reserve_offset = 0  # rotates through the reserve instead of always starting at 0

    def report_failure(self, uri: str) -> None:
        """Called by the player for a track that failed to play; skipped for FAILED_TRACK_SECONDS."""
        self._failed[uri] = time.monotonic()

    def _recently_failed(self) -> set[str]:
        cutoff = time.monotonic() - FAILED_TRACK_SECONDS
        self._failed = {uri: ts for uri, ts in self._failed.items() if ts > cutoff}
        return set(self._failed)

    def _blocked(self, config: dict) -> tuple[set[str], set[str]]:
        minutes = config.get("desks", {}).get("music", {}).get("no_repeat_minutes", 120)
        recent = recently_played(self.play_history_path, minutes) if self.play_history_path else []
        uris = {e["uri"] for e in recent} | self._recently_failed()
        titles = {e["title"].casefold() for e in recent}
        for seg in self.queue.planned_tracks():
            uris.add(seg.audio_ref)
            titles.add(seg.title.casefold())
        return uris, titles

    def next_segment(self, config: dict) -> tuple[Segment, str] | None:
        """The next filler track and where it came from ("reserve" | "favorites" | "library")."""
        blocked_uris, blocked_titles = self._blocked(config)

        def allowed(uri: str, title: str) -> bool:
            return uri not in blocked_uris and title.casefold() not in blocked_titles

        reserve = load_reserve(self.reserve_path)["tracks"]
        for n in range(len(reserve)):
            index = (self._reserve_offset + n) % len(reserve)
            entry = reserve[index]
            if allowed(entry["uri"], entry.get("title", "")):
                self._reserve_offset = index + 1
                return (
                    Segment(
                        type="track",
                        title=entry.get("title") or entry["uri"],
                        audio_ref=entry["uri"],
                        provider=entry.get("provider") or self.provider.name,
                        duration_seconds=entry.get("duration_seconds"),
                    ),
                    "reserve",
                )

        source, candidates = self._fallback_tracks(config)
        if not candidates:
            return None
        fresh = [t for t in candidates if allowed(t.uri, track_segment(t, "").title)]
        # Everything played recently: a repeat beats silence - but not a track that just failed.
        failed = self._recently_failed()
        playable = fresh or [t for t in candidates if t.uri not in failed]
        if not playable:
            return None
        chosen = self.rng.choice(playable)
        return track_segment(chosen, self.provider.name), source

    def _fallback_tracks(self, config: dict) -> tuple[str, list[Track]]:
        playlist_ids = tuple(config.get("music", {}).get("favorite_playlists") or [])
        if playlist_ids:
            cached = self._playlist_cache
            if cached is None or cached[1] != playlist_ids or time.monotonic() - cached[0] > PLAYLIST_CACHE_SECONDS:
                tracks: list[Track] = []
                for playlist_id in playlist_ids:
                    try:
                        tracks.extend(self.provider.get_playlist_tracks(playlist_id))
                    except Exception:
                        logger.warning("Could not load favorite playlist %s", playlist_id, exc_info=True)
                if not tracks:
                    return "favorites", []  # not cached: try again with the next song
                self._playlist_cache = (time.monotonic(), playlist_ids, tracks)
            return "favorites", self._playlist_cache[2]
        try:
            return "library", self.provider.library_tracks()
        except Exception:
            logger.warning("Could not list the music library", exc_info=True)
            return "library", []
