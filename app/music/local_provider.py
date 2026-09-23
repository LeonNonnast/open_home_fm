"""Local file-based music provider.

Treats `library_path` as the whole library: every audio file found recursively is one track,
and every immediate subfolder is a "playlist" (its tracks). Metadata (title/artist/duration)
is read via mutagen when available and falls back to the filename otherwise.

Playback is done by shelling out to a player binary (ffplay by default) writing to the
configured ALSA/PulseAudio output device, so it works headless on a Raspberry Pi without extra
Python audio bindings.
"""
from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path

from rapidfuzz import fuzz, process

from app.music.base import Device, MusicProvider, Playlist, Track

logger = logging.getLogger(__name__)

AUDIO_EXTENSIONS = {".mp3", ".flac", ".ogg", ".wav", ".m4a", ".opus"}


def _read_metadata(path: Path) -> tuple[str, str, float | None]:
    """Returns (title, artist, duration_seconds), falling back to filename-derived values."""
    try:
        from mutagen import File as MutagenFile

        audio = MutagenFile(path, easy=True)
        if audio is not None:
            title = (audio.get("title") or [path.stem])[0]
            artist = (audio.get("artist") or [""])[0]
            duration = getattr(audio.info, "length", None) if audio.info else None
            return title, artist, duration
    except Exception:
        logger.debug("Falling back to filename metadata for %s", path, exc_info=True)
    return path.stem, "", None


class LocalMusicProvider(MusicProvider):
    name = "local"

    def __init__(self, library_path: Path, output_device: str = "default", player_binary: str = "ffplay"):
        self.library_path = library_path
        self.output_device = output_device
        self.player_binary = player_binary
        self._tracks_by_id: dict[str, Track] = {}
        self._process: subprocess.Popen | None = None
        self.rescan()

    def rescan(self) -> None:
        self._tracks_by_id.clear()
        if not self.library_path.exists():
            logger.warning("Local library path %s does not exist", self.library_path)
            return
        for path in self.library_path.rglob("*"):
            if path.suffix.lower() in AUDIO_EXTENSIONS:
                track_id = str(path.relative_to(self.library_path))
                title, artist, duration = _read_metadata(path)
                self._tracks_by_id[track_id] = Track(
                    id=track_id,
                    title=title,
                    artist=artist,
                    uri=str(path),
                    duration_seconds=duration,
                )
        logger.info("Local library scan found %d tracks under %s", len(self._tracks_by_id), self.library_path)

    def search_tracks(self, query: str, limit: int = 10) -> list[Track]:
        choices = {
            tid: f"{t.title} {t.artist}" for tid, t in self._tracks_by_id.items()
        }
        matches = process.extract(query, choices, scorer=fuzz.WRatio, limit=limit)
        return [self._tracks_by_id[tid] for _, _score, tid in matches]

    def get_track_by_uri(self, uri: str) -> Track | None:
        return next((t for t in self._tracks_by_id.values() if t.uri == uri), None)

    def list_playlists(self) -> list[Playlist]:
        if not self.library_path.exists():
            return []
        playlists = []
        for folder in sorted(p for p in self.library_path.iterdir() if p.is_dir()):
            count = sum(1 for p in folder.rglob("*") if p.suffix.lower() in AUDIO_EXTENSIONS)
            playlists.append(Playlist(id=folder.name, name=folder.name, track_count=count))
        return playlists

    def get_playlist_tracks(self, playlist_id: str) -> list[Track]:
        folder = self.library_path / playlist_id
        if not folder.exists() or not folder.is_dir():
            return []
        return [t for tid, t in self._tracks_by_id.items() if tid.startswith(f"{playlist_id}/")]

    def list_devices(self) -> list[Device]:
        return [Device(id="local", name=f"Local output ({self.output_device})", is_active=True)]

    def play(self, track: Track, device: Device | None = None) -> None:
        self._stop_current()
        logger.info("Playing local track: %s - %s", track.artist, track.title)
        self._process = subprocess.Popen(
            [self.player_binary, "-nodisp", "-autoexit", "-loglevel", "quiet", track.uri],
            env={"AUDIODEV": self.output_device} if self.output_device != "default" else None,
        )

    def play_and_wait(self, track: Track, device: Device | None = None) -> None:
        self._stop_current()
        logger.info("Playing (blocking) local track: %s - %s", track.artist, track.title)
        subprocess.run(
            [self.player_binary, "-nodisp", "-autoexit", "-loglevel", "quiet", track.uri],
            check=False,
        )

    def stop(self, device: Device | None = None) -> None:
        self._stop_current()

    def _stop_current(self) -> None:
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._process.kill()
        self._process = None
