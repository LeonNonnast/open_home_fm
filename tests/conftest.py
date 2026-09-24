"""Shared fakes and fixtures: no real Spotify, Ollama, Piper or config files are touched."""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest
import yaml

from app import config as cfg
from app.agent.llm import LLMMessage, LLMProvider, ToolCall
from app.audio.tts import TTSEngine
from app.music.base import Device, MusicProvider, PlaybackResult, Playlist, Track

DEFAULTS = {
    "desks": {"music": {"enabled": True, "fill_threshold_minutes": 10, "block_minutes": 20,
                        "max_queued_program_minutes": 45, "songs_per_announcement": 3,
                        "no_repeat_minutes": 120, "max_tool_iterations": 20}},
    "schedule": {"enabled": False, "start_time": "06:00", "end_time": "23:00"},
    "llm": {"provider": "ollama", "ollama": {"model": "m1", "host": ""}, "anthropic": {"model": "a1"}},
    "music": {"provider": "local", "local": {"library_path": "data/library"}, "favorite_playlists": []},
    "audio": {"output_device": "default", "jingle_cache_dir": "data/audio_cache"},
    "tts": {"engine": "piper", "piper": {"binary": "piper", "voice_model": "models/tts/x.onnx", "speaker": None}},
    "plugins": {"disabled": ["control_hue_lights"], "settings": {}},
}
DEFAULT_PROMPT = "Du bist der Standard-Redakteur.\n"


@pytest.fixture
def config_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Points app.config at throwaway defaults/user files under tmp_path, returns that root."""
    (tmp_path / "config" / "desks").mkdir(parents=True)
    (tmp_path / "data").mkdir()
    (tmp_path / "config" / "config.yaml").write_text(yaml.safe_dump(DEFAULTS, sort_keys=False), encoding="utf-8")
    (tmp_path / "config" / "desks" / "music.md").write_text(DEFAULT_PROMPT, encoding="utf-8")
    monkeypatch.setattr(cfg, "ROOT_DIR", tmp_path)
    monkeypatch.setattr(cfg, "DEFAULTS_PATH", tmp_path / "config" / "config.yaml")
    monkeypatch.setattr(cfg, "USER_CONFIG_PATH", tmp_path / "data" / "config.yaml")
    monkeypatch.setattr(cfg, "DESKS_DIR", tmp_path / "config" / "desks")
    monkeypatch.setattr(cfg, "USER_PROMPTS_DIR", tmp_path / "data" / "prompts")
    monkeypatch.setattr(cfg, "_cache", None)
    return tmp_path


class FakeMusicProvider(MusicProvider):
    """In-memory catalog; search matches case-insensitively on "title - artist".

    play_until returns at once, unless `block` is set: then it waits for the stop event (like a
    long song) and reports `finished=False` when stopped."""

    name = "fake"

    def __init__(self, tracks: list[Track] | None = None, block: bool = False, library: bool = False):
        self.tracks = tracks or []
        self.played: list[Track] = []
        self.block = block
        self.library = library
        self.playing = threading.Event()

    def search_tracks(self, query: str, limit: int = 10) -> list[Track]:
        q = query.casefold()
        hits = [t for t in self.tracks if q in f"{t.title} - {t.artist}".casefold() or q in f"{t.artist} - {t.title}".casefold()]
        return hits[:limit]

    def list_playlists(self) -> list[Playlist]:
        return [Playlist(id="all", name="Alles", track_count=len(self.tracks))]

    def get_playlist_tracks(self, playlist_id: str) -> list[Track]:
        return list(self.tracks) if playlist_id == "all" else []

    def get_track_by_uri(self, uri: str) -> Track | None:
        return next((t for t in self.tracks if t.uri == uri), None)

    def list_devices(self) -> list[Device]:
        return [Device(id="dev", name="open-home-fm", is_active=True)]

    def play(self, track: Track, device: Device | None = None) -> None:
        self.played.append(track)

    def play_until(self, track: Track, stop_event: threading.Event, device: Device | None = None) -> PlaybackResult:
        self.played.append(track)
        if not self.block:
            return PlaybackResult(finished=True, position_seconds=track.duration_seconds or 0)
        self.playing.set()
        stop_event.wait(30)
        self.playing.clear()
        return PlaybackResult(finished=not stop_event.is_set(), position_seconds=0)

    def library_tracks(self) -> list[Track]:
        return list(self.tracks) if self.library else []

    def stop(self, device: Device | None = None) -> None:
        pass


class FakeTTS(TTSEngine):
    """Writes a tiny placeholder file per text instead of running Piper."""

    def __init__(self, out_dir: Path):
        self.out_dir = out_dir
        self.texts: list[str] = []

    def synthesize(self, text: str) -> Path:
        self.texts.append(text)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        path = self.out_dir / f"jingle_{len(self.texts)}.wav"
        path.write_bytes(b"RIFF")
        return path


class ScriptedLLM(LLMProvider):
    """Replays predefined turns. Each turn is either a final text (str) or a list of
    (tool name, arguments) tuples the "model" calls. Records every request it received."""

    def __init__(self, turns: list[str | list[tuple[str, dict[str, Any]]]]):
        self.turns = list(turns)
        self.requests: list[list[LLMMessage]] = []

    def chat(self, messages: list[LLMMessage], tools: list[dict[str, Any]]) -> LLMMessage:
        self.requests.append(list(messages))
        if not self.turns:
            return LLMMessage(role="assistant", content="")
        turn = self.turns.pop(0)
        if isinstance(turn, str):
            return LLMMessage(role="assistant", content=turn)
        calls = [ToolCall(id=f"call_{len(self.requests)}_{i}", name=n, arguments=a) for i, (n, a) in enumerate(turn)]
        return LLMMessage(role="assistant", tool_calls=calls)


def track(title: str, artist: str, uri: str | None = None, duration: float | None = 200) -> Track:
    uri = uri or f"fake:{artist}:{title}".replace(" ", "_").lower()
    return Track(id=uri, title=title, artist=artist, uri=uri, duration_seconds=duration)


@pytest.fixture
def fake_tts(tmp_path: Path) -> FakeTTS:
    return FakeTTS(tmp_path / "jingles")
