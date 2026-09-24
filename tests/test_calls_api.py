"""The calls API end to end: the real app (lifespan, scheduler, player) against fakes."""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.agent.desk as desk_module
import app.main as main
from app import config as cfg
from app.audio.player import QueuePlayer
from tests.conftest import FakeMusicProvider, ScriptedLLM, track


def wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class FakeSTT:
    def __init__(self):
        self.release = threading.Event()
        self.text = "Als Nächstes bitte Queen"

    def transcribe(self, audio_path: Path) -> str:
        assert self.release.wait(5)
        return self.text


class Holder:
    llm = ScriptedLLM([])


@pytest.fixture
def client(config_env: Path, fake_tts, monkeypatch):
    # Only the dispatch desk talks to the (scripted) LLM here.
    cfg.update_config({"desks": {"music": {"enabled": False}}})
    tracks = [track("Bohemian Rhapsody", "Queen", uri="u:queen")]
    stt = FakeSTT()
    holder = Holder()
    monkeypatch.setattr(main, "DATA_DIR", config_env / "data")
    monkeypatch.setattr(main, "ROOT_DIR", config_env)
    monkeypatch.setattr(main, "create_music_provider", lambda config: FakeMusicProvider(tracks, block=True))
    monkeypatch.setattr(main, "create_stt_engine", lambda config: stt)
    monkeypatch.setattr(QueuePlayer, "_play_jingle_ffplay", lambda self, path, stop_event: stop_event.wait(30))
    monkeypatch.setattr(desk_module, "create_music_provider", lambda config: FakeMusicProvider(tracks))
    monkeypatch.setattr(desk_module, "create_tts_engine", lambda config, cache_dir: fake_tts)
    monkeypatch.setattr(desk_module, "create_llm_provider", lambda config: holder.llm)
    inbox = config_env / "data" / "inbox"
    inbox.mkdir()
    (inbox / "20260923T071915043891.txt").write_text("Alter Wunsch", encoding="utf-8")
    with TestClient(main.app) as c:
        c.stt = stt
        c.holder = holder
        yield c


def _call(client, call_id):
    return client.get(f"/api/calls/{call_id}").json()


def test_text_call_is_dispatched_right_away(client):
    # The migrated inbox wish is a call and gets dispatched on start (LLM: just a sentence).
    assert wait_for(lambda: client.get("/api/calls").json()["calls"][-1]["status"] == "routed")
    assert any(n["id"] == "zwischenrufe" for n in client.get("/api/status").json()["notices"])

    client.holder.llm = ScriptedLLM([[("play_next", {"query": "Queen", "announce_text": "Für Mama!"})], "ok"])
    created = client.post("/api/calls/text", json={"text": "Als Nächstes Queen", "author": "Mama"}).json()
    assert created["author"] == "Mama" and created["source"] == "text"
    assert created["status"] in ("new", "processing") and created["status_text"] == "Leitstelle sortiert ein…"
    assert wait_for(lambda: _call(client, created["id"])["status"] == "queued")

    call = _call(client, created["id"])
    [action] = call["actions"]
    assert action["type"] == "play_next" and action["undo_available"] and call["reply_audio_url"]
    assert client.get(call["reply_audio_url"]).status_code == 200
    queue = client.get("/api/queue").json()["items"]
    assert [i["lane"] for i in queue][0] == "reply" and queue[0]["call_id"] == call["id"]
    assert queue[0]["call"] == {"id": call["id"], "author": call["author"], "text": call["text"]}

    listing = client.get("/api/calls", params={"since": call["updated_at"]}).json()
    assert listing["calls"] == [] and listing["server_time"] and listing["dispatch"]["state"]
    assert len(client.get("/api/calls", params={"limit": 1}).json()["calls"]) == 1

    undone = client.delete(f"/api/calls/{call['id']}/actions/0").json()
    assert undone["actions"][0]["status"] == "removed" and undone["status"] == "removed"
    assert client.delete(f"/api/calls/{call['id']}/actions/0").status_code == 409
    assert client.delete(f"/api/calls/{call['id']}/actions/5").status_code == 404
    assert client.get("/api/calls/nope").status_code == 404
    assert client.post("/api/calls/text", json={"text": "   "}).status_code == 422


def test_voice_call_transcribes_in_the_background_and_waits_for_confirmation(client):
    response = client.post("/api/calls/voice", files={"file": ("rec.webm", b"audio", "audio/webm")},
                           data={"author": "Opa"})
    call = response.json()
    assert call["status"] == "transcribing" and call["author"] == "Opa" and call["source"] == "voice"
    assert call["status_text"] == "Wird transkribiert…" and "audio_file" not in call

    client.stt.release.set()
    assert wait_for(lambda: _call(client, call["id"])["status"] == "awaiting_confirmation")
    assert _call(client, call["id"])["text"] == "Als Nächstes bitte Queen"
    assert client.post(f"/api/calls/{call['id']}/retry").status_code == 409

    client.holder.llm = ScriptedLLM([[("reply", {"text": "Kommt!"})], "ok"])
    confirmed = client.patch(f"/api/calls/{call['id']}", json={"text": "Als Nächstes bitte Queen, danke"}).json()
    assert confirmed["status"] in ("new", "processing") and confirmed["text"].endswith("danke")
    assert wait_for(lambda: _call(client, call["id"])["status"] == "queued")
    assert client.patch(f"/api/calls/{call['id']}", json={"text": "zu spät"}).status_code == 409


def test_expired_call_can_be_retried(client):
    calls = client.app.state.calls
    assert wait_for(lambda: calls.all()[-1]["status"] == "routed")  # the migrated wish is through
    call = calls.create("Hallo?")
    calls.update(call["id"], lambda c: c.update(status="expired", error="Leitstelle nicht erreichbar"))
    client.holder.llm = ScriptedLLM(["Hallo zurück."])
    retried = client.post(f"/api/calls/{call['id']}/retry").json()
    assert retried["status"] in ("new", "processing", "routed")
    assert wait_for(lambda: _call(client, call["id"])["status"] == "routed")
    assert _call(client, call["id"])["final_message"] == "Hallo zurück."


def test_withdraw_a_call(client):
    call = client.post("/api/calls/voice", files={"file": ("rec.webm", b"audio", "audio/webm")}).json()
    assert client.delete(f"/api/calls/{call['id']}").json()["status"] == "removed"
    client.stt.release.set()
    time.sleep(0.2)
    assert _call(client, call["id"])["status"] == "removed"


def test_desks_include_the_dispatch_desk(client):
    desks = {d["name"]: d for d in client.get("/api/desks").json()["desks"]}
    dispatch = desks["dispatch"]
    assert dispatch["label"] == "Leitstelle" and dispatch["settings"]["allow_interrupt"] is True
    assert dispatch["settings"]["max_tool_iterations"] == 6 and "open_calls" in dispatch
    assert dispatch["next_trigger"] == "sofort bei jedem Zwischenruf"
    assert "open_wishes" in desks["music"]
    status = client.get("/api/status").json()
    assert "open_calls" in status["desks"]["dispatch"] and "last_run_at" in status["desks"]["dispatch"]

    for bad in ({"reply_expires_minutes": 0}, {"allow_interrupt": "ja"}, {"fill_threshold_minutes": 5},
                {"min_minutes_between_interrupts": None}, {"plugins": "control_hue_lights"}):
        assert client.patch("/api/desks/dispatch", json=bad).status_code == 422, bad
    data = client.patch("/api/desks/dispatch", json={"allow_interrupt": False, "plugins": ["get_weather"]}).json()
    assert data["settings"]["allow_interrupt"] is False and data["settings"]["plugins"] == ["get_weather"]
    assert client.get("/api/desks/dispatch/prompt").json()["text"] == "Du bist die Leitstelle.\n"


def test_mailboxes_api(client):
    runner = client.app.state.desk_runner
    wish = runner.wishes.add("Beatles", author="Mama")
    note = runner.news_notes.add("Sperrmüll")
    assert [w["id"] for w in client.get("/api/wishes").json()["wishes"]] == [wish["id"]]
    assert client.delete(f"/api/wishes/{wish['id']}").json()["status"] == "removed"
    assert client.delete(f"/api/wishes/{wish['id']}").status_code == 409
    assert client.get("/api/news-notes").json()["notes"][0]["id"] == note["id"]
    assert client.delete(f"/api/news-notes/{note['id']}").json()["status"] == "removed"
    assert client.delete("/api/news-notes/nope").status_code == 404


def test_inbox_alias_creates_calls(client):
    client.holder.llm = ScriptedLLM(["ok", "ok"])
    result = client.post("/api/inbox/text", json={"text": "Grüße an Opa"}).json()
    assert result["status"] == "ok" and client.app.state.calls.get(result["call_id"])["text"] == "Grüße an Opa"
    client.stt.release.set()
    voice = client.post("/api/inbox/voice", files={"file": ("wish.webm", b"x", "audio/webm")}).json()
    assert voice["text"] == "Als Nächstes bitte Queen"
    assert client.app.state.calls.get(voice["call_id"])["source"] == "voice"
    assert "items" in client.get("/api/inbox").json()
