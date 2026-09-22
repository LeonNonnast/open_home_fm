"""Provider-agnostic music model.

The agent talks to whichever provider is configured (Spotify or a local file library) purely
through this interface, so `music.provider: spotify|local` in config.yaml is a one-line swap
for the rest of the system.
"""
from __future__ import annotations

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
    def list_devices(self) -> list[Device]:
        ...

    @abstractmethod
    def play(self, track: Track, device: Device | None = None) -> None:
        """Start playback. Must return promptly (used for ad-hoc 'play now' requests)."""

    def play_and_wait(self, track: Track, device: Device | None = None) -> None:
        """Play a track and block until it has (roughly) finished.

        Used by the deterministic script player, which needs segments to play back to back.
        The default implementation is duration-based; providers with a real playback-status
        API (e.g. Spotify) may override this with tighter polling.
        """
        self.play(track, device)
        time.sleep(track.duration_seconds or DEFAULT_WAIT_SECONDS)

    @abstractmethod
    def stop(self, device: Device | None = None) -> None:
        ...
