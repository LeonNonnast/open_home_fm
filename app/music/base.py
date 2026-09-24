"""Provider-agnostic music model.

The agent talks to whichever provider is configured (Spotify or a local file library) purely
through this interface, so `music.provider: spotify|local` in config.yaml is a one-line swap
for the rest of the system.
"""
from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

DEFAULT_WAIT_SECONDS = 180.0


@dataclass
class Track:
    id: str
    title: str
    artist: str
    uri: str  # provider-specific playable reference: spotify:track:... or an absolute file path
    duration_seconds: float | None = None
    album: str | None = None


@dataclass
class Playlist:
    id: str
    name: str
    track_count: int | None = None


@dataclass
class Device:
    id: str
    name: str
    is_active: bool = False


@dataclass
class PlaybackResult:
    finished: bool  # False: stopped via the stop event (skip, shutdown)
    position_seconds: float


class MusicProvider(ABC):
    name: str = "base"

    @abstractmethod
    def search_tracks(self, query: str, limit: int = 10) -> list[Track]:
        ...

    @abstractmethod
    def list_playlists(self) -> list[Playlist]:
        ...

    @abstractmethod
    def get_playlist_tracks(self, playlist_id: str) -> list[Track]:
        ...

    @abstractmethod
    def get_track_by_uri(self, uri: str) -> Track | None:
        """Exact lookup for a uri already known (e.g. from an earlier search_tracks result).

        Used when finalizing a script: the LLM sometimes echoes the `uri` field from a prior
        tool result instead of the originally-requested search text, so resolving by uri first
        (falling back to a fresh search) is more reliable than only ever searching by text.
        """

    @abstractmethod
    def list_devices(self) -> list[Device]:
        ...

    @abstractmethod
    def play(self, track: Track, device: Device | None = None) -> None:
        """Start playback. Must return promptly (used for ad-hoc 'play now' requests)."""

    def play_until(self, track: Track, stop_event: threading.Event, device: Device | None = None) -> PlaybackResult:
        """Play a track and block until it has (roughly) finished or `stop_event` is set.

        Used by the queue player, which needs segments to play back to back and must be able to
        skip a song or shut down mid-song. The default implementation is duration-based;
        providers with a real playback-status API (e.g. Spotify) override it.
        """
        self.play(track, device)
        started = time.monotonic()
        if stop_event.wait(track.duration_seconds or DEFAULT_WAIT_SECONDS):
            self.stop(device)
            return PlaybackResult(finished=False, position_seconds=time.monotonic() - started)
        return PlaybackResult(finished=True, position_seconds=time.monotonic() - started)

    def play_and_wait(self, track: Track, device: Device | None = None) -> None:
        """Blocking playback without a way to stop it early."""
        self.play_until(track, threading.Event(), device)

    def library_tracks(self) -> list[Track]:
        """Every track of the source, for the filler program. Empty where that's not feasible (Spotify)."""
        return []

    @abstractmethod
    def stop(self, device: Device | None = None) -> None:
        ...
