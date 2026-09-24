"""Piper voice catalog + downloads for the web UI's voice card.

The catalog comes from the official voices.json on Hugging Face (same source the installer
downloads from), limited to German voices since the station moderates in German.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.request
from pathlib import Path

from app.config import ROOT_DIR

logger = logging.getLogger(__name__)

VOICES_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/main"
VOICES_DIR = ROOT_DIR / "models" / "tts"
LANGUAGE_PREFIX = "de_"
VOICE_ID_PATTERN = re.compile(r"^[a-z]{2}_[A-Z]{2}-[A-Za-z0-9_]+-(x_low|low|medium|high)$")

_CATALOG_TTL_SECONDS = 6 * 3600
_catalog_cache: tuple[float, list[dict]] | None = None
_download_lock = threading.Lock()


def installed_voices() -> list[dict]:
    if not VOICES_DIR.exists():
        return []
    voices = []
    for model in sorted(VOICES_DIR.glob("*.onnx")):
        if not Path(f"{model}.json").exists():
            continue
        voices.append({"id": model.stem, "voice_model": str(model.relative_to(ROOT_DIR))})
    return voices


def catalog() -> list[dict]:
    """German voices available for download, with size - cached, empty when offline."""
    global _catalog_cache
    if _catalog_cache and time.time() - _catalog_cache[0] < _CATALOG_TTL_SECONDS:
        return _catalog_cache[1]
    try:
        with urllib.request.urlopen(f"{VOICES_URL}/voices.json", timeout=10) as response:
            data = json.load(response)
    except Exception:
        logger.warning("Could not fetch the Piper voice catalog", exc_info=True)
        return _catalog_cache[1] if _catalog_cache else []

    voices = []
    for voice_id, info in sorted(data.items()):
        if not voice_id.startswith(LANGUAGE_PREFIX):
            continue
        size = sum(f.get("size_bytes", 0) for name, f in info.get("files", {}).items() if name.endswith(".onnx"))
        voices.append(
            {
                "id": voice_id,
                "name": info.get("name", voice_id),
                "quality": info.get("quality", ""),
                "speakers": info.get("num_speakers", 1),
                "size_mb": round(size / 1_000_000),
            }
        )
    _catalog_cache = (time.time(), voices)
    return voices


def download_voice(voice_id: str) -> str:
    """Downloads model + config into models/tts, returns the config-ready relative model path."""
    if not VOICE_ID_PATTERN.match(voice_id):
        raise ValueError(f"Invalid voice id: {voice_id}")
    lang, rest = voice_id.split("-", 1)
    speaker, quality = rest.rsplit("-", 1)
    remote = f"{VOICES_URL}/{lang.split('_')[0]}/{lang}/{speaker}/{quality}/{voice_id}.onnx"

    VOICES_DIR.mkdir(parents=True, exist_ok=True)
    with _download_lock:
        for suffix in (".json", ""):  # config first: a model without its config is unusable
            target = VOICES_DIR / f"{voice_id}.onnx{suffix}"
            if target.exists() and target.stat().st_size > 0:
                continue
            partial = target.with_name(target.name + ".part")
            logger.info("Downloading Piper voice file %s", target.name)
            try:
                urllib.request.urlretrieve(remote + suffix, partial)
            except Exception:
                partial.unlink(missing_ok=True)
                raise
            partial.replace(target)
    return str((VOICES_DIR / f"{voice_id}.onnx").relative_to(ROOT_DIR))
