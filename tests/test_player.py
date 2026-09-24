from __future__ import annotations

import json
import random
import time
from pathlib import Path

import pytest

import app.audio.player as player_module
from app import config as cfg
from app.agent.play_history import record_played
from app.audio.player import QueuePlayer
from app.program.filler import FillerSource
from app.program.queue import ProgramQueue, QueueItem, Segment
from tests.conftest import FakeMusicProvider, track


def wait_for(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def queue(config_env: Path) -> ProgramQueue:
    return ProgramQueue(config_env / "data" / "queue.json", config_env / "data" / "player_cursor.json")


def _player(config_env: Path, provider, queue, **kwargs) -> QueuePlayer:
    return QueuePlayer(
        provider, queue,
        play_history_path=config_env / "data" / "history.json",
        reserve_path=config_env / "data" / "reserve.json",
        idle_poll_seconds=0.05,
        jingle_player=lambda path, stop_event: None,
        **kwargs,
    )


def _block(*titles: str) -> QueueItem:
    return QueueItem.new("program", "music", [Segment("track", t, f"u:{t}", duration_seconds=200) for t in titles])


def test_skip_stops_the_song_and_moves_on(config_env, queue):
    provider = FakeMusicProvider(block=True)
    block = queue.append(_block("a", "b"))
    player = _player(config_env, provider, queue)
    player.start()
    try:
        assert wait_for(lambda: provider.playing.is_set())
        assert player.status()["current"]["title"] == "a"
        assert player.remaining_program_seconds() == pytest.approx(400, abs=2)
        assert player.skip()
        assert wait_for(lambda: len(provider.played) == 2 and provider.playing.is_set())
        assert player.status()["current"]["title"] == "b"
        assert any("übersprungen: a" in line for line in player.status()["log"])
    finally:
        started = time.monotonic()
        player.stop()
        assert time.monotonic() - started < 3
    # Skipped after < 30 s: doesn't count for the no-repeat window.
    assert not (config_env / "data" / "history.json").exists()
    assert queue.get(block.id).next_segment == 2


def test_circuit_breaker_pauses_after_three_short_segments(config_env, queue):
    provider = FakeMusicProvider()  # every play "ends" at once, like a vanished Spotify device
    queue.append(_block(*"abcdef"))
    player = _player(config_env, provider, queue, breaker_pause_seconds=60)
    player.start()
    try:
        assert wait_for(lambda: player.status()["mode"] == "paused")
        time.sleep(0.2)
        assert [t.title for t in provider.played] == ["a", "b", "c"]
        assert "nicht erreichbar" in player.status()["notice"]
    finally:
        started = time.monotonic()
        player.stop()
        assert time.monotonic() - started < 3


def test_filler_plays_until_program_arrives(config_env, queue):
    provider = FakeMusicProvider(block=True)
    (config_env / "data" / "reserve.json").write_text(json.dumps({"tracks": [
        {"uri": "u:r1", "title": "R1", "duration_seconds": 200},
    ]}), encoding="utf-8")
    player = _player(config_env, provider, queue)
    player.start()
    try:
        assert wait_for(lambda: provider.playing.is_set())
        assert player.status()["current"]["lane"] == "filler"
        assert player.status()["mode_text"] == "Füllprogramm aus der Reserve"
        queue.append(_block("p"))
        player.skip()  # = the filler song ends
        assert wait_for(lambda: len(provider.played) == 2 and provider.playing.is_set())
        assert player.status()["current"]["lane"] == "program" and player.status()["mode"] == "playing"
    finally:
        player.stop()


def test_off_air_expires_program(config_env, queue, monkeypatch):
    monkeypatch.setattr(player_module, "is_broadcast_time", lambda config: False)
    block = queue.append(_block("a"))
    provider = FakeMusicProvider()
    player = _player(config_env, provider, queue)
    player.start()
    try:
        assert wait_for(lambda: player.status()["mode"] == "off_air")
    finally:
        player.stop()
    assert queue.get(block.id).status == "expired" and provider.played == []


def test_filler_selection_order(config_env, queue):
    history = config_env / "data" / "history.json"
    reserve = config_env / "data" / "reserve.json"
    provider = FakeMusicProvider([track("F1", "X", uri="u:f1"), track("F2", "X", uri="u:f2")])
    filler = FillerSource(provider, queue, reserve, history, rng=random.Random(1))
    config = cfg.load_config()

    # Nothing configured, no library (Spotify-like): nothing to play.
    assert filler.next_segment(config) is None

    # Reserve first, minus recently played and already queued songs.
    reserve.write_text(json.dumps({"tracks": [
        {"uri": "u:r1", "title": "R1"}, {"uri": "u:r2", "title": "R2"}, {"uri": "u:r3", "title": "R3"},
    ]}), encoding="utf-8")
    record_played(history, "u:r1", "R1")
    queue.append(QueueItem.new("program", "music", [Segment("track", "R2", "u:r2")]))
    segment, source = filler.next_segment(config)
    assert (segment.audio_ref, source) == ("u:r3", "reserve")

    # Reserve used up: favorite playlists, without repeats.
    record_played(history, "u:r3", "R3")
    record_played(history, "u:f1", "F1 - X")
    config["music"]["favorite_playlists"] = ["all"]
    segment, source = filler.next_segment(config)
    assert (segment.audio_ref, source) == ("u:f2", "favorites")

    # Local source without favorites: the whole library; all played -> a repeat beats silence.
    config["music"]["favorite_playlists"] = []
    provider.library = True
    record_played(history, "u:f2", "F2 - X")
    segment, source = filler.next_segment(config)
    assert source == "library" and segment.audio_ref in {"u:f1", "u:f2"}
