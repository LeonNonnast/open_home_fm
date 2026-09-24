from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.routes_voice as routes_voice
import app.audio.tts as tts
import app.audio.voices as voices


class FakePiper:
    rendered: list[tuple[str, object]] = []

    def __init__(self, cache_dir, binary, settings):
        self.settings = settings

    def render(self, text: str, out_path: Path) -> None:
        FakePiper.rendered.append((text, self.settings))
        out_path.write_bytes(b"RIFF----WAVE")


@pytest.fixture
def client(config_env: Path, monkeypatch):
    voices_dir = config_env / "models" / "tts"
    voices_dir.mkdir(parents=True)
    for name in ("de_DE-test-medium.onnx", "de_DE-test-medium.onnx.json"):
        (voices_dir / name).write_text("{}", encoding="utf-8")
    (config_env / "outside.onnx").write_text("", encoding="utf-8")

    monkeypatch.setattr(tts, "ROOT_DIR", config_env)
    monkeypatch.setattr(voices, "ROOT_DIR", config_env)
    monkeypatch.setattr(voices, "VOICES_DIR", voices_dir)
    monkeypatch.setattr(routes_voice, "VOICES_DIR", voices_dir)
    monkeypatch.setattr(routes_voice, "catalog", lambda: [{"id": "de_DE-test-medium"}, {"id": "de_DE-other-low"}])
    monkeypatch.setattr(routes_voice, "PiperTTSEngine", FakePiper)
    monkeypatch.setattr(voices.urllib.request, "urlopen", lambda *a, **k: pytest.fail("no network in tests"))
    FakePiper.rendered = []

    app = FastAPI()
    app.include_router(routes_voice.router)
    return TestClient(app)


def preview(client, **overrides):
    body = {"text": "Hallo", "voice_model": "models/tts/de_DE-test-medium.onnx", **overrides}
    return client.post("/api/voice/preview", json=body)


def test_list_voices(client):
    data = client.get("/api/voice").json()
    assert data["installed"] == [{"id": "de_DE-test-medium", "voice_model": "models/tts/de_DE-test-medium.onnx", "speakers": []}]
    assert [v["installed"] for v in data["catalog"]] == [True, False]


def test_preview_renders_with_given_settings(client):
    res = preview(client, length_scale=1.2, pitch_semitones=-2, echo=0.3, speaker="")
    assert res.status_code == 200
    assert res.headers["content-type"] == "audio/wav"
    text, settings = FakePiper.rendered[0]
    assert text == "Hallo"
    assert (settings.length_scale, settings.pitch_semitones, settings.echo, settings.speaker) == (1.2, -2, 0.3, None)


@pytest.mark.parametrize("voice_model", [
    "models/tts/../../outside.onnx",  # traversal out of models/tts
    "outside.onnx",  # exists, but not under models/tts
    "/etc/passwd",
    "models/tts/missing.onnx",
])
def test_preview_rejects_models_outside_voice_dir(client, voice_model):
    res = preview(client, voice_model=voice_model)
    assert res.status_code == 400
    assert FakePiper.rendered == []


@pytest.mark.parametrize("overrides", [
    {"length_scale": 3},
    {"pitch_semitones": -20},
    {"echo": 1.5},
    {"text": "x" * 501},
])
def test_preview_validates_ranges(client, overrides):
    assert preview(client, **overrides).status_code == 422


@pytest.mark.parametrize("voice_id", ["../evil", "de_DE-test-medium/../../x", "en_US-foo-ultra", ""])
def test_download_rejects_invalid_ids(client, voice_id):
    assert client.post("/api/voice/download", json={"id": voice_id}).status_code == 400
