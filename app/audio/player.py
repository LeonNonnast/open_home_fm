"""The deterministic half of the system: plays the program queue, segment by segment.

Runs independently of the LLM. Before every segment it asks the queue for the next item again
(highest lane first), so later replies/news slot in after the running segment and a new program
block takes over from the filler program at the next song end. When the program lane is empty
it plays the filler program (reserve, then favorite playlists) - the station never goes silent
because of the AI.

State the UI shows (current segment, position, mode, decision log) lives only in memory; the
queue and the cursor on disk are what survives a restart.
"""
from __future__ import annotations

import logging
import subprocess
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from app.agent.play_history import record_played
from app.config import is_broadcast_time, load_config
from app.music.base import MusicProvider, Track
from app.program.filler import FillerSource
from app.program.queue import ProgramQueue, QueueItem, Segment

logger = logging.getLogger(__name__)

# A song only counts for the no-repeat window once it ran this long (skipped songs don't).
RECORD_AFTER_SECONDS = 30
# Circuit breaker: this many segments in a row ending after less than SHORT_SEGMENT_SECONDS
# (e.g. the Spotify device vanished and every play returns after one poll) pause the player
# for BREAKER_PAUSE_SECONDS instead of draining the queue within seconds.
BREAKER_SEGMENTS = 3
SHORT_SEGMENT_SECONDS = 10
BREAKER_PAUSE_SECONDS = 60
LOG_LINES = 12

MODE_TEXTS = {
    "starting": "startet",
    "playing": "spielt",
    "filler": "Füllprogramm",
    "idle": "wartet auf Programm",
    "off_air": "Sendepause",
    "paused": "pausiert",
    "stopped": "gestoppt",
}

JinglePlayer = Callable[[str, threading.Event], None]


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


class QueuePlayer:
    def __init__(
        self,
        provider: MusicProvider,
        queue: ProgramQueue,
        play_history_path: Path | None = None,
        reserve_path: Path | None = None,
        idle_poll_seconds: float = 2.0,
        jingle_player_binary: str = "ffplay",
        jingle_player: JinglePlayer | None = None,
        breaker_pause_seconds: float = BREAKER_PAUSE_SECONDS,
    ):
        self.provider = provider
        self.queue = queue
        self.play_history_path = play_history_path
        self.idle_poll_seconds = idle_poll_seconds
        self.jingle_player_binary = jingle_player_binary
        self._jingle_player = jingle_player or self._play_jingle_ffplay
        self.breaker_pause_seconds = breaker_pause_seconds
        self.filler = FillerSource(provider, queue, reserve_path or Path("data/reserve.json"), play_history_path)

        self._stop_event = threading.Event()
        self._skip_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._state_lock = threading.Lock()
        self._current: dict | None = None
        self._mode = "starting"
        self._mode_text = MODE_TEXTS["starting"]
        self._notice: str | None = None
        self._log: deque[str] = deque(maxlen=LOG_LINES)
        self._short_in_a_row = 0
        self._was_on_air: bool | None = None

    # ---------- lifecycle ----------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run_forever, daemon=True, name="queue-player")
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._skip_event.set()
        if self._thread is not None:
            self._thread.join(timeout=8)
        try:
            self.provider.stop()
        except Exception:
            logger.warning("Could not stop the music provider", exc_info=True)
        self._set_mode("stopped")

    def skip(self) -> bool:
        """Ends the current segment early; False when nothing is playing."""
        with self._state_lock:
            if self._current is None:
                return False
        self._log_line("Überspringen angefordert")
        self._skip_event.set()
        return True

    # ---------- status ----------

    def status(self) -> dict:
        now = time.time()
        with self._state_lock:
            current = dict(self._current) if self._current else None
            status = {
                "mode": self._mode,
                "mode_text": self._mode_text,
                "notice": self._notice,
                "log": list(self._log),
                "server_time": _iso(now),
            }
        if current:
            started = current.pop("_started")
            current["position"] = round(now - started, 1)
            current["started_at"] = _iso(started)
        status["current"] = current
        return status

    def remaining_program_seconds(self) -> float:
        remaining = 0.0
        with self._state_lock:
            if self._current and self._current["lane"] == "program":
                remaining = max(0.0, (self._current["duration"] or 0) - (time.time() - self._current["_started"]))
        return self.queue.remaining_program_seconds(current_remaining=remaining)

    def _set_mode(self, mode: str, text: str | None = None) -> None:
        with self._state_lock:
            if (mode, text or MODE_TEXTS[mode]) != (self._mode, self._mode_text):
                self._mode = mode
                self._mode_text = text or MODE_TEXTS[mode]

    def _log_line(self, text: str) -> None:
        line = f"{datetime.now().strftime('%H:%M:%S')} {text}"
        logger.info("Player: %s", text)
        with self._state_lock:
            self._log.append(line)

    # ---------- main loop ----------

    def _run_forever(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._step()
            except Exception:
                logger.exception("Player step failed")
                self._stop_event.wait(self.idle_poll_seconds)

    def _step(self) -> None:
        config = load_config()
        on_air = is_broadcast_time(config)
        if on_air != self._was_on_air:
            if not on_air:
                # Blocks from 23:00 must not play at 06:00 with the wrong time references.
                expired = self.queue.expire_lanes(("program", "filler"), "Sendeschluss")
                self._log_line(f"Sendeschluss{f', {expired} Beiträge verfallen' if expired else ''}")
            elif self._was_on_air is False:
                self._log_line("Sendebeginn")
            self._was_on_air = on_air
        if not on_air:
            self._set_mode("off_air")
            self._stop_event.wait(self.idle_poll_seconds)
            return

        item = self.queue.next_item()
        if item is None:
            item = self._make_filler(config)
            if item is None:
                self._stop_event.wait(self.idle_poll_seconds)
                return
        elif item.lane != "filler":
            self._set_mode("playing")

        index = item.next_segment
        if index >= len(item.segments):
            self.queue.finish_item(item.id)
            return
        self._play_segment(item, index)
        if index == len(item.segments) - 1:
            self.queue.finish_item(item.id)

    def _make_filler(self, config: dict) -> QueueItem | None:
        picked = self.filler.next_segment(config)
        if picked is None:
            favorites = config.get("music", {}).get("favorite_playlists")
            self._set_mode(
                "idle",
                "wartet auf Programm – kein Füllprogramm verfügbar"
                + ("" if favorites else " (keine Lieblings-Playlists eingestellt)"),
            )
            return None
        segment, source = picked
        text = {
            "reserve": "Füllprogramm aus der Reserve",
            "favorites": "Füllprogramm aus den Lieblings-Playlists",
            "library": "Füllprogramm aus der Bibliothek",
        }[source]
        self._set_mode("filler", text)
        self._log_line(f"{text}: {segment.title}")
        return self.queue.append(QueueItem.new("filler", "filler", [segment]))

    def _play_segment(self, item: QueueItem, index: int) -> None:
        segment = item.segments[index]
        self.queue.start_segment(item.id, index)
        self._skip_event.clear()
        started = time.time()
        with self._state_lock:
            self._current = {
                "item_id": item.id,
                "lane": item.lane,
                "desk": item.desk,
                "segment_index": index,
                "segment_count": len(item.segments),
                "type": segment.type,
                "title": segment.title,
                "text": segment.text,
                "duration": segment.duration_seconds,
                "_started": started,
            }
        self._log_line(f"{'Ansage' if segment.type == 'jingle' else 'Song'}: {segment.title} ({item.lane})")

        record_timer = None
        if segment.type == "track" and self.play_history_path is not None:
            record_timer = threading.Timer(
                RECORD_AFTER_SECONDS, record_played, args=(self.play_history_path, segment.audio_ref, segment.title)
            )
            record_timer.daemon = True
            record_timer.start()

        stopped = False
        try:
            if segment.type == "track":
                result = self.provider.play_until(self._track(segment), self._skip_event)
                stopped = not result.finished
            elif segment.type == "jingle":
                self._jingle_player(segment.audio_ref, self._skip_event)
                stopped = self._skip_event.is_set()
            else:
                logger.warning("Unknown segment type '%s', skipping", segment.type)
        except Exception:
            logger.exception("Failed to play segment %d of %s, skipping", index, item.id)
        finally:
            elapsed = time.time() - started
            if record_timer is not None and elapsed < RECORD_AFTER_SECONDS:
                record_timer.cancel()
            with self._state_lock:
                self._current = None

        if stopped and not self._stop_event.is_set():
            self._log_line(f"übersprungen: {segment.title}")
        self._check_breaker(segment, elapsed, user_stopped=stopped)

    def _check_breaker(self, segment: Segment, elapsed: float, user_stopped: bool) -> None:
        # Short jingles are fine - only segments that end far before their expected length count.
        expected = segment.duration_seconds or (segment.estimated_seconds() if segment.type == "track" else 0)
        too_short = not user_stopped and elapsed < SHORT_SEGMENT_SECONDS and elapsed < expected * 0.5
        if not too_short:
            self._short_in_a_row = 0
            if self._notice:
                with self._state_lock:
                    self._notice = None
            return
        self._short_in_a_row += 1
        if self._short_in_a_row < BREAKER_SEGMENTS:
            return
        self._short_in_a_row = 0
        text = "pausiert: Wiedergabe bricht sofort ab – Musikquelle/Spotify-Gerät nicht erreichbar?"
        with self._state_lock:
            self._notice = text
        self._set_mode("paused", text)
        self._log_line(f"Schutzschalter: {BREAKER_SEGMENTS} Segmente in Folge nach < {SHORT_SEGMENT_SECONDS} s beendet, "
                       f"Pause {self.breaker_pause_seconds:.0f} s")
        self._stop_event.wait(self.breaker_pause_seconds)

    @staticmethod
    def _track(segment: Segment) -> Track:
        return Track(
            id=segment.audio_ref,
            title=segment.title,
            artist="",
            uri=segment.audio_ref,
            duration_seconds=segment.duration_seconds,
        )

    def _play_jingle_ffplay(self, audio_path: str, stop_event: threading.Event) -> None:
        process = subprocess.Popen(
            [self.jingle_player_binary, "-nodisp", "-autoexit", "-loglevel", "quiet", audio_path]
        )
        try:
            while process.poll() is None:
                if stop_event.wait(0.25):
                    process.terminate()
                    break
        finally:
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
