"""The deterministic half of the system: plays whatever script the agent loop last produced.

Runs independently of the LLM - it just polls `current_script.json` for a new script id and,
when one shows up, plays its segments strictly in order, blocking on each one. This means
playback keeps going even if the LLM/agent loop is slow, erroring, or offline; it only ever
starts a *new* program once the agent has finished building one.
"""
from __future__ import annotations

import logging
import subprocess
import threading
import time
from pathlib import Path

from app.agent.script import Script, load_script
from app.music.base import MusicProvider, Track

logger = logging.getLogger(__name__)


class ScriptPlayer:
    def __init__(
        self,
        provider: MusicProvider,
        script_path: Path,
        poll_interval: float = 5.0,
        jingle_player_binary: str = "ffplay",
    ):
        self.provider = provider
        self.script_path = script_path
        self.poll_interval = poll_interval
        self.jingle_player_binary = jingle_player_binary
        self._last_script_id: str | None = None
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self.current_segment_index: int | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run_forever, daemon=True, name="script-player")
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self.provider.stop()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _run_forever(self) -> None:
        while not self._stop_event.is_set():
            script = load_script(self.script_path)
            if script is not None and script.id != self._last_script_id:
                self._last_script_id = script.id
                self._play_script(script)
            else:
                time.sleep(self.poll_interval)

    def _play_script(self, script: Script) -> None:
        logger.info("Playing script %s (%d segments)", script.id, len(script.segments))
        for index, segment in enumerate(script.segments):
            if self._stop_event.is_set():
                return
            self.current_segment_index = index
            try:
                if segment.type == "track":
                    track = Track(
                        id=segment.audio_ref,
                        title=segment.title,
                        artist="",
                        uri=segment.audio_ref,
                        duration_seconds=segment.duration_seconds,
                    )
                    self.provider.play_and_wait(track)
                elif segment.type == "jingle":
                    self._play_jingle(segment.audio_ref)
                else:
                    logger.warning("Unknown segment type '%s', skipping", segment.type)
            except Exception:
                logger.exception("Failed to play segment %d of script %s, skipping", index, script.id)
        self.current_segment_index = None

    def _play_jingle(self, audio_path: str) -> None:
        logger.info("Playing jingle: %s", audio_path)
        subprocess.run(
            [self.jingle_player_binary, "-nodisp", "-autoexit", "-loglevel", "quiet", audio_path],
            check=False,
        )
