"""Starts the real app (lifespan included) against fakes: no Spotify, Ollama or Piper."""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.agent.desk as desk_module
import app.api.routes_inbox as routes_inbox
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
    monkeypatch.setattr(desk_module, "create_tts_engine", lambda config, cache_dir: fake_tts)
    monkeypatch.setattr(desk_module, "create_llm_provider", lambda config: llm)
    monkeypatch.setattr(routes_transcripts, "TRANSCRIPTS_DIR", config_env / "data" / "transcripts")
    monkeypatch.setattr(routes_inbox, "INBOX_DIR", config_env / "data" / "inbox")
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
    assert status["state"]["script"]["segments"][0]["title"] == "Guten Morgen"
    assert status["player_current_segment_index"] == 0
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
    assert client.post("/api/status/trigger").json()["status"] in {"started", "queued", "skipped"}
    transcripts = client.get("/api/transcripts", params={"desk": "music"}).json()["transcripts"]
    assert transcripts and transcripts[0]["desk"] == "music"

    assert client.delete(f"/api/queue/{block['id']}").json()["status"] == "removed"
    assert client.post(f"/api/queue/{block['id']}/restore").json()["status"] == "playing"
    assert client.delete("/api/queue/nope").status_code == 404
    assert client.get("/api/desks/nope").status_code == 404

    started = time.monotonic()
    client.__exit__(None, None, None)
    assert time.monotonic() - started < 5
