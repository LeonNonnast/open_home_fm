"""Starts the real app (lifespan included) against fakes: no Spotify, Ollama or Piper."""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.agent.desk as desk_module
import app.api.routes_music as routes_music
import app.api.routes_transcripts as routes_transcripts
import app.main as main
from app.audio.player import QueuePlayer
from tests.conftest import FakeMusicProvider, ScriptedLLM, track


def wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def client(config_env: Path, fake_tts, monkeypatch):
    tracks = [track(f"Song {i}", "Band", duration=240) for i in range(8)]
    player_provider = FakeMusicProvider(tracks, block=True)
    llm = ScriptedLLM([
        [("append_program_block", {"segments": [{"type": "jingle", "text": "Guten Morgen"}] +
                                   [{"query": f"Song {i}"} for i in range(6)]})],
        [("update_reserve", {"tracks": [{"query": "Song 7"}]})],
        "Block steht.",
    ])
    monkeypatch.setattr(main, "DATA_DIR", config_env / "data")
    monkeypatch.setattr(main, "ROOT_DIR", config_env)
    monkeypatch.setattr(main, "create_music_provider", lambda config: player_provider)
    monkeypatch.setattr(main, "create_stt_engine", lambda config: None)
    # No ffplay in tests: a "jingle" plays until skipped.
    monkeypatch.setattr(QueuePlayer, "_play_jingle_ffplay", lambda self, path, stop_event: stop_event.wait(30))
    monkeypatch.setattr(desk_module, "create_music_provider", lambda config: FakeMusicProvider(tracks))
    monkeypatch.setattr(routes_music, "create_music_provider", lambda config: FakeMusicProvider(tracks))
    monkeypatch.setattr(desk_module, "create_tts_engine", lambda config, cache_dir: fake_tts)
    monkeypatch.setattr(desk_module, "create_llm_provider", lambda config: llm)
    monkeypatch.setattr(routes_transcripts, "TRANSCRIPTS_DIR", config_env / "data" / "transcripts")
    with TestClient(main.app) as c:
        c.player_provider = player_provider
        yield c


def test_startup_plans_plays_and_shuts_down(client):
    # The fill watcher runs right at startup: the empty queue gets a block, the player plays it.
    assert wait_for(lambda: client.get("/api/queue").json()["items"])
    assert wait_for(lambda: client.get("/api/status").json()["now_playing"] is not None)

    status = client.get("/api/status").json()
    assert status["on_air"] and status["now_playing"]["lane"] == "program"
    assert status["now_playing"]["title"] == "Guten Morgen"  # the greeting opens the block
    assert status["now_playing"]["position"] >= 0 and status["server_time"]
    assert wait_for(lambda: client.get("/api/desks").json()["desks"][0]["state"] == "idle")
    desk = client.get("/api/desks").json()["desks"][0]
    assert desk["name"] == "music" and desk["last_success_at"] and desk["reserve"]["count"] == 1
    assert desk["fill"]["remaining_seconds"] > 20 * 60

    queue = client.get("/api/queue").json()
    [block] = queue["items"]
    assert block["status"] == "playing" and len(block["segments"]) == 7

    assert client.post("/api/player/skip").json() == {"skipped": True}
    assert wait_for(lambda: (client.get("/api/status").json()["now_playing"] or {}).get("title") == "Song 0 - Band")

    assert client.post("/api/desks/music/run").json()["status"] in {"started", "queued", "skipped"}
    transcripts = client.get("/api/transcripts", params={"desk": "music"}).json()["transcripts"]
    assert transcripts and transcripts[0]["desk"] == "music"

    assert client.delete(f"/api/queue/{block['id']}").json()["status"] == "removed"
    assert client.post(f"/api/queue/{block['id']}/restore").json()["status"] == "playing"
    assert client.delete("/api/queue/nope").status_code == 404
    assert client.get("/api/desks/nope").status_code == 404

    # Favorite playlists come from the configured source (here: the fake's single playlist).
    assert client.get("/api/music/playlists").json()["playlists"] == [{"id": "all", "name": "Alles", "track_count": 8}]
    # Every page and its script is served.
    for page in ("/", "/index.html", "/inbox.html", "/desks.html", "/settings.html", "/static/common.js"):
        assert client.get(page).status_code == 200, page

    started = time.monotonic()
    client.__exit__(None, None, None)
    assert time.monotonic() - started < 5


def test_playlists_unreachable_source_is_no_500(client, monkeypatch):
    def broken(config):
        raise RuntimeError("Spotify nicht angemeldet")

    monkeypatch.setattr(routes_music, "create_music_provider", broken)
    data = client.get("/api/music/playlists").json()
    assert data["playlists"] == [] and data["error"] == "Spotify nicht angemeldet"


def test_desk_patch_validates(client):
    for bad in ({"fill_threshold_minutes": ""}, {"fill_threshold_minutes": None}, {"block_minutes": 0},
                {"fill_threshold_minutes": 50, "max_queued_program_minutes": 45}, {"nonsense": 1}):
        assert client.patch("/api/desks/music", json=bad).status_code == 422, bad
    assert client.get("/api/desks/music").json()["settings"]["fill_threshold_minutes"] == 10

    data = client.patch("/api/desks/music", json={"fill_threshold_minutes": 15, "enabled": True}).json()
    assert data["settings"]["fill_threshold_minutes"] == 15
    # Only valid against the stored cap: 45.
    assert client.patch("/api/desks/music", json={"max_queued_program_minutes": 15}).status_code == 422


def test_switching_the_music_source_drops_its_planned_songs(client):
    from app.program.queue import QueueItem, Segment

    queue = client.app.state.queue
    item = queue.append(QueueItem.new("program", "music", [Segment("track", "x", "/m/x.mp3", provider="local")]))
    client.patch("/api/config", json={"music": {"provider": "spotify"}})
    assert queue.get(item.id).status == "expired"
    notices = client.get("/api/status").json()["notices"]
    assert any("Neustart des Dienstes nötig" in n["text"] for n in notices)


def test_news_desk_patch_validates_and_shows_up_in_status(client):
    for bad in ({"slots": [{"minute": "60", "format": "full"}]}, {"slots": [{"minute": "7", "format": "full"}]},
                {"slots": [{"minute": "00", "format": "long"}]}, {"slots": []},
                {"slots": [{"minute": "00", "format": "full"}, {"minute": "00", "format": "short"}]},
                {"slots": [{"minute": "00"}]}, {"lead_minutes": 0}, {"lead_minutes": 16}, {"max_delay_minutes": 0},
                {"placement": "sometimes"}, {"sources": ["tv"]}, {"nonsense": True}, {"lead_minutes": None}):
        assert client.patch("/api/desks/news", json=bad).status_code == 422, bad
    data = client.patch("/api/desks/news", json={
        "slots": [{"minute": "45", "format": "short"}, {"minute": "15", "format": "full"}],
        "lead_minutes": 15, "sources": ["news", "notes", "news"], "placement": "on_time",
    }).json()
    assert data["settings"]["slots"] == [{"minute": "15", "format": "full"}, {"minute": "45", "format": "short"}]
    assert data["settings"]["sources"] == ["news", "notes"] and data["placement_note"]
    assert data["next_slot_at"] and data["prepare_at"] and data["next_slot_format"] in ("full", "short")
    assert sorted(j.id for j in client.app.state.scheduler._scheduler.get_jobs() if j.id.startswith("news-")) \
        == ["news-00", "news-30"]
    news = client.get("/api/status").json()["desks"]["news"]
    assert news["next_slot_at"] == data["next_slot_at"] and news["prepared"] is False
    assert [d["name"] for d in client.get("/api/desks").json()["desks"]] == ["music", "news", "dispatch"]
    assert client.get("/api/desks/news/prompt").json()["text"].startswith("Du bist die Nachrichtenredaktion")


def test_news_patch_checks_max_delay_and_drops_bulletins_of_removed_slots(client):
    from datetime import datetime, timedelta, timezone

    from app.program.queue import QueueItem, Segment

    assert client.patch("/api/desks/news", json={"max_delay_minutes": 30}).status_code == 422  # gap :00/:30
    assert client.patch("/api/desks/news", json={
        "slots": [{"minute": "00", "format": "full"}, {"minute": "10", "format": "short"}]}).status_code == 422
    queue, notes = client.app.state.queue, client.app.state.desk_runner.news_notes
    slot = (datetime.now(timezone.utc) + timedelta(hours=1)).replace(minute=30, second=0, microsecond=0)
    item = queue.append(QueueItem.new("news", "news", [Segment("jingle", "N", "/x.wav")], not_before=slot.isoformat()))
    note = notes.add("Sperrmüll")
    notes.mark_in_bulletin([note["id"]], item.id, slot.isoformat())
    kept = queue.append(QueueItem.new("news", "news", [Segment("jingle", "N", "/y.wav")],
                                      not_before=slot.replace(minute=0).isoformat()))
    assert client.patch("/api/desks/news", json={"enabled": True, "slots": [{"minute": "00", "format": "full"}]}).status_code == 200
    assert queue.get(item.id).status == "expired" and queue.get(kept.id).status == "queued"
    assert notes.get(note["id"])["status"] == "noted"
    assert client.patch("/api/desks/news", json={"enabled": False}).status_code == 200
    assert queue.get(kept.id).status == "expired"
