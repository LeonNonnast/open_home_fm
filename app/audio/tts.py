"""Text-to-speech: renders short spoken "Einspieler" (jingles) for the agent's script segments.

Default engine is Piper (fully local, runs fine on a Raspberry Pi 4/5). The interface is a
single `synthesize(text) -> Path` so swapping in a cloud TTS engine later only means adding
another class here and pointing `tts.engine` in config.yaml at it.
"""
from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import wave
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from app.config import ROOT_DIR

logger = logging.getLogger(__name__)


class TTSEngine(ABC):
    @abstractmethod
    def synthesize(self, text: str) -> Path:
        """Renders `text` to an audio file on disk and returns its path."""


class NullTTSEngine(TTSEngine):
    """Used when tts.engine: none - agent can still run, jingles are just skipped."""

    def synthesize(self, text: str) -> Path:
        raise RuntimeError("TTS is disabled (tts.engine: none in config.yaml)")


@dataclass(frozen=True)
class VoiceSettings:
    """Everything that shapes how the moderator sounds - editable in the web UI's voice card."""

    voice_model: str
    speaker: str | None = None  # name from the model's speaker_id_map, multi-speaker models only
    length_scale: float = 1.0  # >1 speaks slower
    pitch_semitones: float = 0.0  # post-processed with ffmpeg, keeps the tempo
    echo: float = 0.0  # 0..1, subtle room/"AI" reverb via ffmpeg

    @classmethod
    def from_config(cls, piper_cfg: dict) -> "VoiceSettings":
        return cls(
            voice_model=str(piper_cfg.get("voice_model", "")),
            speaker=(str(piper_cfg["speaker"]) if piper_cfg.get("speaker") not in (None, "") else None),
            length_scale=float(piper_cfg.get("length_scale", 1.0) or 1.0),
            pitch_semitones=float(piper_cfg.get("pitch_semitones", 0.0) or 0.0),
            echo=float(piper_cfg.get("echo", 0.0) or 0.0),
        )

    def cache_key(self, text: str) -> str:
        raw = f"{self.voice_model}|{self.speaker}|{self.length_scale}|{self.pitch_semitones}|{self.echo}|{text}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def speaker_names(voice_model: Path) -> list[str]:
    """Speaker names of a multi-speaker model (empty for single-speaker models)."""
    config_path = Path(f"{voice_model}.json")
    if not config_path.exists():
        return []
    try:
        speaker_map = json.loads(config_path.read_text(encoding="utf-8")).get("speaker_id_map") or {}
    except (OSError, ValueError):
        return []
    return list(speaker_map)


def _speaker_id(voice_model: Path, speaker: str | None) -> int | None:
    if not speaker:
        return None
    config_path = Path(f"{voice_model}.json")
    try:
        speaker_map = json.loads(config_path.read_text(encoding="utf-8")).get("speaker_id_map") or {}
    except (OSError, ValueError):
        return None
    return speaker_map.get(speaker)


def effect_filter(settings: VoiceSettings, sample_rate: int) -> str | None:
    """ffmpeg -af chain for pitch/echo, None when the raw Piper output is used as-is."""
    filters = []
    if settings.pitch_semitones:
        factor = 2 ** (settings.pitch_semitones / 12)
        # Resampling shifts pitch and tempo together; atempo undoes the tempo part.
        filters.append(f"asetrate={sample_rate * factor:.0f},aresample={sample_rate},atempo={1 / factor:.4f}")
    if settings.echo > 0:
        echo = min(settings.echo, 1.0)
        filters.append(f"aecho=0.85:0.9:35|70:{0.35 * echo:.3f}|{0.2 * echo:.3f}")
    return ",".join(filters) or None


class PiperTTSEngine(TTSEngine):
    def __init__(
        self,
        cache_dir: Path,
        binary: str = "piper",
        voice_model: str | Path = "",
        settings: VoiceSettings | None = None,
        ffmpeg_binary: str = "ffmpeg",
    ):
        self.cache_dir = cache_dir
        self.binary = binary
        self.settings = settings or VoiceSettings(voice_model=str(voice_model))
        self.voice_model = resolve_voice_model(self.settings.voice_model)
        self.ffmpeg_binary = ffmpeg_binary
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def synthesize(self, text: str) -> Path:
        out_path = self.cache_dir / f"jingle_{self.settings.cache_key(text)}.wav"
        if out_path.exists():
            return out_path
        self.render(text, out_path)
        return out_path

    def render(self, text: str, out_path: Path) -> None:
        """Renders `text` to `out_path`, bypassing the cache (used for web UI previews)."""
        if not self.voice_model.exists():
            raise FileNotFoundError(
                f"Piper voice model not found at {self.voice_model}. "
                "Download one from https://github.com/rhasspy/piper/releases and update "
                "tts.piper.voice_model in config.yaml."
            )

        command = [self.binary, "--model", str(self.voice_model), "--output_file", str(out_path)]
        speaker_id = _speaker_id(self.voice_model, self.settings.speaker)
        if speaker_id is not None:
            command += ["--speaker", str(speaker_id)]
        if self.settings.length_scale != 1.0:
            command += ["--length_scale", str(self.settings.length_scale)]

        logger.info("Synthesizing jingle (%d chars) with piper", len(text))
        result = subprocess.run(command, input=text.encode("utf-8"), capture_output=True)
        if result.returncode != 0:
            out_path.unlink(missing_ok=True)
            raise RuntimeError(f"piper failed: {result.stderr.decode('utf-8', errors='ignore')}")

        with wave.open(str(out_path), "rb") as f:
            sample_rate = f.getframerate()
        filters = effect_filter(self.settings, sample_rate)
        if filters:
            self._apply_effects(out_path, filters)

    def _apply_effects(self, wav_path: Path, filters: str) -> None:
        processed = wav_path.with_name(wav_path.stem + ".fx.wav")
        result = subprocess.run(
            [self.ffmpeg_binary, "-y", "-loglevel", "error", "-i", str(wav_path), "-af", filters, str(processed)],
            capture_output=True,
        )
        if result.returncode != 0:
            # The unprocessed voice is still usable - an effect is never worth a missing jingle.
            logger.warning("ffmpeg voice effect failed, using the plain voice: %s", result.stderr.decode(errors="ignore"))
            processed.unlink(missing_ok=True)
            return
        processed.replace(wav_path)

    @staticmethod
    def duration_seconds(wav_path: Path) -> float:
        with wave.open(str(wav_path), "rb") as f:
            return f.getnframes() / float(f.getframerate())


def resolve_voice_model(voice_model: str | Path) -> Path:
    path = Path(voice_model)
    return path if path.is_absolute() else ROOT_DIR / path


def create_tts_engine(config: dict, cache_dir: Path) -> TTSEngine:
    tts_cfg = config.get("tts", {})
    engine = tts_cfg.get("engine", "piper")

    if engine == "piper":
        piper_cfg = tts_cfg.get("piper", {})
        return PiperTTSEngine(
            cache_dir=cache_dir,
            binary=piper_cfg.get("binary", "piper"),
            settings=VoiceSettings.from_config(piper_cfg),
        )
    if engine == "none":
        return NullTTSEngine()

    raise ValueError(f"Unknown TTS engine: {engine}")
