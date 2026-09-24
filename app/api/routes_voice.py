from __future__ import annotations

import os
import tempfile
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.audio.tts import PiperTTSEngine, VoiceSettings, resolve_voice_model, speaker_names
from app.audio.voices import VOICES_DIR, catalog, download_voice, installed_voices
from app.config import load_config, resolve_path

router = APIRouter(prefix="/api/voice", tags=["voice"])


class PreviewBody(BaseModel):
    text: str = Field(max_length=500)
    voice_model: str
    speaker: str | None = None
    length_scale: float = Field(1.0, ge=0.5, le=2.0)
    pitch_semitones: float = Field(0.0, ge=-12, le=12)
    echo: float = Field(0.0, ge=0, le=1)


class DownloadBody(BaseModel):
    id: str


def _checked_model(voice_model: str) -> Path:
    # Only models under models/tts - the path comes from the browser.
    path = resolve_voice_model(voice_model).resolve()
    if not path.is_relative_to(VOICES_DIR.resolve()) or not path.exists():
        raise HTTPException(status_code=400, detail=f"Unknown voice model: {voice_model}")
    return path


@router.get("")
def get_voices() -> dict:
    installed = installed_voices()
    for voice in installed:
        voice["speakers"] = speaker_names(resolve_voice_model(voice["voice_model"]))
    installed_ids = {v["id"] for v in installed}
    return {
        "installed": installed,
        "catalog": [{**v, "installed": v["id"] in installed_ids} for v in catalog()],
    }


@router.post("/download")
def post_download(body: DownloadBody) -> dict:
    try:
        voice_model = download_voice(body.id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Download fehlgeschlagen: {exc}") from exc
    return {"voice_model": voice_model}


@router.post("/preview")
def post_preview(body: PreviewBody, background_tasks: BackgroundTasks) -> FileResponse:
    model = _checked_model(body.voice_model)
    config = load_config()
    piper_cfg = config.get("tts", {}).get("piper", {})
    engine = PiperTTSEngine(
        cache_dir=resolve_path(config["audio"]["jingle_cache_dir"]),
        binary=piper_cfg.get("binary", "piper"),
        settings=VoiceSettings(
            voice_model=str(model),
            speaker=body.speaker or None,
            length_scale=body.length_scale,
            pitch_semitones=body.pitch_semitones,
            echo=body.echo,
        ),
    )
    fd, name = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    out = Path(name)
    try:
        engine.render(body.text, out)
    except Exception as exc:
        out.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    background_tasks.add_task(out.unlink, missing_ok=True)
    return FileResponse(out, media_type="audio/wav")
