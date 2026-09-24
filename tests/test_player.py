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
from app.music.base import PlaybackResult
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
    player = _player(config_env, provider, queue, breaker_pauses=(60,))
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


class FailingProvider(FakeMusicProvider):
    """Raises for the uris in `bad` (Spotify 403 / region lock); blocks like a long song otherwise."""

    def __init__(self, *args, bad: set[str], **kwargs):
        super().__init__(*args, block=True, **kwargs)
        self.bad = bad

    def play_until(self, track, stop_event, device=None):
        if track.uri in self.bad:
            self.played.append(track)
            raise RuntimeError("403 Forbidden")
        return super().play_until(track, stop_event, device)


def test_unplayable_filler_track_is_not_retried(config_env, queue):
    (config_env / "data" / "reserve.json").write_text(json.dumps({"tracks": [
        {"uri": "u:bad", "title": "Bad", "duration_seconds": 200},
        {"uri": "u:good", "title": "Good", "duration_seconds": 200},
    ]}), encoding="utf-8")
    provider = FailingProvider(bad={"u:bad"})
    player = _player(config_env, provider, queue)
    player.start()
    try:
        assert wait_for(lambda: provider.playing.is_set())
        assert player.status()["current"]["title"] == "Good"
        player.skip()  # "Good" ends after < 30 s: not in the history, so it may come again - "Bad" may not
        assert wait_for(lambda: len(provider.played) >= 3 and provider.playing.is_set())
    finally:
        player.stop()
    assert [t.uri for t in provider.played].count("u:bad") == 1
    assert player.status()["mode"] != "paused"


def test_filler_rotates_reserve_and_skips_failed(config_env, queue):
    reserve = config_env / "data" / "reserve.json"
    reserve.write_text(json.dumps({"tracks": [
        {"uri": "u:r1", "title": "R1"}, {"uri": "u:r2", "title": "R2"}, {"uri": "u:r3", "title": "R3"},
    ]}), encoding="utf-8")
    filler = FillerSource(FakeMusicProvider(), queue, reserve, config_env / "data" / "history.json")
    config = cfg.load_config()
    assert [filler.next_segment(config)[0].audio_ref for _ in range(4)] == ["u:r1", "u:r2", "u:r3", "u:r1"]
    for uri in ("u:r1", "u:r2", "u:r3"):
        filler.report_failure(uri)
    # Fallback source: everything else failed too - nothing beats a dead track loop.
    provider = FakeMusicProvider([track("F", "X", uri="u:f")], library=True)
    filler.provider = provider
    assert filler.next_segment(config)[0].audio_ref == "u:f"
    filler.report_failure("u:f")
    assert filler.next_segment(config) is None


def test_breaker_rewinds_program_and_pauses_longer_each_time(config_env, queue):
    provider = FakeMusicProvider()  # every play "ends" at once: a real outage
    block = queue.append(_block(*"abcdef"))
    player = _player(config_env, provider, queue, breaker_pauses=(0.05, 60))
    player.start()
    try:
        assert wait_for(lambda: any("Pause 60 s" in line for line in player.status()["log"]))
        time.sleep(0.2)
        assert player.breaker_active()
    finally:
        player.stop()
    # a-c were retried once after the first pause and then given up; while tripped, the first
    # fresh failure (d) trips again at once - an outage costs one new segment per pause.
    assert [t.title for t in provider.played] == ["a", "b", "c", "a", "b", "c", "d"]
    assert queue.get(block.id).next_segment == 3 and queue.get(block.id).status == "playing"


def test_broken_tracks_get_two_attempts_then_the_player_moves_on(config_env, queue):
    provider = FailingProvider(bad={"u:a", "u:b", "u:c"})  # three genuinely broken tracks, then a good one
    block = queue.append(_block("a", "b", "c", "d"))
    player = _player(config_env, provider, queue, breaker_pauses=(0.05, 60))
    player.start()
    try:
        assert wait_for(lambda: provider.playing.is_set())
        assert player.status()["current"]["title"] == "d"
        assert player.status()["mode"] == "playing"
        log = player.status()["log"]
        assert sum("aufgegeben nach 2 Fehlversuchen" in line for line in log) == 3
    finally:
        player.stop()
    assert [t.title for t in provider.played] == ["a", "b", "c", "a", "b", "c", "d"]
    assert queue.get(block.id).next_segment == 4


def test_skip_between_segments_does_not_hit_the_next_one(config_env, queue):
    class SlowThenBlocking(FakeMusicProvider):
        def play_until(self, track, stop_event, device=None):
            if track.title == "a":  # ends on its own while the skip request is under way
                self.played.append(track)
                time.sleep(0.3)
                return PlaybackResult(finished=True, position_seconds=0.3)
            return super().play_until(track, stop_event, device)

    provider = SlowThenBlocking(block=True)
    queue.append(_block("a", "b"))
    player = _player(config_env, provider, queue)
    original_log = player._log_line

    def slow_log(text):
        if text == "Überspringen angefordert":  # the old code set the event only after this
            wait_for(lambda: provider.playing.is_set(), timeout=1)
        original_log(text)

    player._log_line = slow_log
    player.start()
    try:
        assert wait_for(lambda: player.status()["current"] is not None)
        time.sleep(0.1)
        assert player.skip()
        assert wait_for(lambda: provider.playing.is_set())
        time.sleep(0.2)
        assert provider.playing.is_set() and player.status()["current"]["title"] == "b"
    finally:
        player.stop()


def test_halt_cuts_the_segment_and_nothing_starts_until_play(config_env, queue):
    provider = FakeMusicProvider(block=True)
    reply = queue.append(QueueItem.new("reply", "dispatch", [Segment("track", "r", "u:r", duration_seconds=200)]))
    block = queue.append(_block("a", "b"))
    player = _player(config_env, provider, queue)
    player.start()
    try:
        assert wait_for(lambda: provider.playing.is_set())
        assert player.status()["current"]["title"] == "r"
        cfg.set_stopped(True)  # like POST /api/player/stop: flag first, then halt()
        assert player.halt()
        assert wait_for(lambda: player.status()["mode"] == "stopped")
        time.sleep(0.3)
        assert [t.title for t in provider.played] == ["r"] and player.status()["current"] is None
        assert player.status()["mode_text"] == "gestoppt"
        log = player.status()["log"]
        assert any("abgebrochen (Sender gestoppt): r" in line for line in log)
        assert not any("übersprungen" in line or "Schutzschalter" in line for line in log)
        assert any("Sender gestoppt, 1 Beiträge verfallen" in line for line in log)
        # The cut reply segment isn't consumed, the program block expired (fresh plan after Play).
        assert queue.get(reply.id).next_segment == 0 and queue.get(block.id).status == "expired"
        assert not player.halt()  # nothing on air any more

        cfg.set_stopped(False)
        assert wait_for(lambda: len(provider.played) == 2 and provider.playing.is_set())
        assert player.status()["current"]["title"] == "r"
        assert any("Sender gestartet" in line for line in player.status()["log"])
    finally:
        player.stop()


def test_segment_picked_before_a_stop_does_not_start(config_env, queue, monkeypatch):
    # The stop lands between next_item() and the segment start: halt() found nothing to cut.
    provider = FakeMusicProvider(block=True)
    block = queue.append(_block("a"))
    player = _player(config_env, provider, queue)
    original = queue.start_segment

    def start_then_stop(item_id, index):
        cfg.set_stopped(True)
        return original(item_id, index)

    monkeypatch.setattr(queue, "start_segment", start_then_stop)
    player._was_on_air = True
    player._step()
    assert provider.played == [] and player.status()["current"] is None
    assert queue.get(block.id).next_segment == 0


def test_stop_and_play_end_a_circuit_breaker_pause(config_env, queue):
    provider = FakeMusicProvider()  # every play "ends" at once: the breaker trips, pause 60 s
    queue.append(_block(*"abcdef"))
    player = _player(config_env, provider, queue, breaker_pauses=(60,))
    player.start()
    try:
        assert wait_for(lambda: player.status()["mode"] == "paused")
        cfg.set_stopped(True)
        assert not player.halt()  # nothing on air - but the pause ends
        assert wait_for(lambda: player.status()["mode"] == "stopped", timeout=1)
        assert player.status()["notice"] is None and not player.breaker_active()
        played = len(provider.played)
        cfg.set_stopped(False)
        player.resume()
        queue.append(_block("x"))  # the program expired at the stop - the music desk plans anew
        assert wait_for(lambda: len(provider.played) > played, timeout=1)
    finally:
        player.stop()


def test_play_expires_blocks_appended_while_stopped(config_env, queue):
    # A desk run still going at the stop appends its block afterwards: planned for back then.
    provider = FakeMusicProvider(block=True)
    player = _player(config_env, provider, queue)
    cfg.set_stopped(True)
    player._step()
    assert player.status()["mode"] == "stopped"
    stale = queue.append(_block("old"))
    cfg.set_stopped(False)
    player.resume()
    time.sleep(0.01)
    fresh = queue.append(_block("new"))
    player.start()
    try:
        assert wait_for(lambda: provider.playing.is_set())
        assert player.status()["current"]["title"] == "new"
        assert queue.get(stale.id).status == "expired"
        assert any("Sender gestartet, 1 Beiträge verfallen" in line for line in player.status()["log"])
    finally:
        player.stop()
    assert queue.get(fresh.id).status != "expired"
