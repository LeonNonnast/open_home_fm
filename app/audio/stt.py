"""Speech-to-text for voice wishes submitted through the web UI.

The inbox route saves an uploaded recording and immediately transcribes it to a `.txt` file
with the same stem, so the agent loop only ever has to deal with plain text inbox items
regardless of whether the listener typed or spoke their request.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from pathlib import Path

logger = logging.getLogger(__name__)


class STTEngine(ABC):
    @abstractmethod
    def transcribe(self, audio_path: Path) -> str:
        ...


class NullSTTEngine(STTEngine):
    def transcribe(self, audio_path: Path) -> str:
        raise RuntimeError("STT is disabled (stt.engine: none in config.yaml)")


class FasterWhisperSTTEngine(STTEngine):
    def __init__(self, model_size: str = "small", device: str = "cpu", compute_type: str = "int8"):
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self._model = None  # loaded lazily: the model download/init must not block app startup

    def _get_model(self):
        if self._model is None:
            from faster_whisper import WhisperModel

            logger.info(
                "Loading faster-whisper model '%s' (%s/%s)", self.model_size, self.device, self.compute_type
            )
            self._model = WhisperModel(self.model_size, device=self.device, compute_type=self.compute_type)
        return self._model

    def transcribe(self, audio_path: Path) -> str:
        segments, _info = self._get_model().transcribe(str(audio_path))
        text = " ".join(segment.text.strip() for segment in segments)
        return text.strip()


def create_stt_engine(config: dict) -> STTEngine:
    stt_cfg = config.get("stt", {})
    engine = stt_cfg.get("engine", "faster_whisper")

    if engine == "faster_whisper":
        fw_cfg = stt_cfg.get("faster_whisper", {})
        return FasterWhisperSTTEngine(
            model_size=fw_cfg.get("model_size", "small"),
            device=fw_cfg.get("device", "cpu"),
            compute_type=fw_cfg.get("compute_type", "int8"),
        )
    if engine == "none":
        return NullSTTEngine()

    raise ValueError(f"Unknown STT engine: {engine}")
