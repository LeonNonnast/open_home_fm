"""Text-to-speech: renders short spoken "Einspieler" (jingles) for the agent's script segments.

Default engine is Piper (fully local, runs fine on a Raspberry Pi 4/5). The interface is a
single `synthesize(text) -> Path` so swapping in a cloud TTS engine later only means adding
another class here and pointing `tts.engine` in config.yaml at it.
"""
from __future__ import annotations

import hashlib
import logging
import subprocess
import wave
from abc import ABC, abstractmethod
from pathlib import Path

logger = logging.getLogger(__name__)


class TTSEngine(ABC):
    @abstractmethod
    def synthesize(self, text: str) -> Path:
        """Renders `text` to an audio file on disk and returns its path."""


class NullTTSEngine(TTSEngine):
    """Used when tts.engine: none - agent can still run, jingles are just skipped."""

    def synthesize(self, text: str) -> Path:
        raise RuntimeError("TTS is disabled (tts.engine: none in config.yaml)")


class PiperTTSEngine(TTSEngine):
    def __init__(self, cache_dir: Path, binary: str = "piper", voice_model: str | Path = ""):
        self.cache_dir = cache_dir
        self.binary = binary
        self.voice_model = Path(voice_model)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def synthesize(self, text: str) -> Path:
        cache_key = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        out_path = self.cache_dir / f"jingle_{cache_key}.wav"
        if out_path.exists():
            return out_path

        if not self.voice_model.exists():
            raise FileNotFoundError(
                f"Piper voice model not found at {self.voice_model}. "
                "Download one from https://github.com/rhasspy/piper/releases and update "
                "tts.piper.voice_model in config.yaml."
            )

        logger.info("Synthesizing jingle (%d chars) with piper", len(text))
        result = subprocess.run(
            [self.binary, "--model", str(self.voice_model), "--output_file", str(out_path)],
            input=text.encode("utf-8"),
            capture_output=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"piper failed: {result.stderr.decode('utf-8', errors='ignore')}")
        return out_path

    @staticmethod
    def duration_seconds(wav_path: Path) -> float:
        with wave.open(str(wav_path), "rb") as f:
            return f.getnframes() / float(f.getframerate())


def create_tts_engine(config: dict, cache_dir: Path) -> TTSEngine:
    tts_cfg = config.get("tts", {})
    engine = tts_cfg.get("engine", "piper")

    if engine == "piper":
        piper_cfg = tts_cfg.get("piper", {})
        return PiperTTSEngine(
            cache_dir=cache_dir,
            binary=piper_cfg.get("binary", "piper"),
            voice_model=piper_cfg.get("voice_model", ""),
        )
    if engine == "none":
        return NullTTSEngine()

    raise ValueError(f"Unknown TTS engine: {engine}")
