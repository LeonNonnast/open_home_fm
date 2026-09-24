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
from app.program.queue import AHEAD_OF_PROGRAM, ProgramQueue, QueueItem, Segment

logger = logging.getLogger(__name__)

# A song only counts for the no-repeat window once it ran this long (skipped songs don't).
RECORD_AFTER_SECONDS = 30
# Circuit breaker: this many segments in a row ending after less than SHORT_SEGMENT_SECONDS
# (e.g. the Spotify device vanished and every play returns after one poll) pause the player
# instead of draining the queue within seconds. The failed program segments are put back
# (not consumed), and each further trip without a successful segment in between pauses longer.
# A segment gets at most MAX_SEGMENT_ATTEMPTS fast failures: after that it's given up (logged) and
# the player moves on, so genuinely broken tracks can't hold the program up until it expires.
# While tripped (no segment played properly since), a single fresh failure trips again - a real
# outage then costs at most one new segment per pause.
BREAKER_SEGMENTS = 3
MAX_SEGMENT_ATTEMPTS = 2
SHORT_SEGMENT_SECONDS = 10
BREAKER_PAUSES = (60, 300, 900)
LOG_LINES = 12
# Segment types the music provider plays (episodes are Spotify podcast episodes).
PLAYABLE_TYPES = ("track", "episode")

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
        breaker_pauses: tuple[float, ...] = BREAKER_PAUSES,
    ):
        self.provider = provider
        self.queue = queue
        self.play_history_path = play_history_path
        self.idle_poll_seconds = idle_poll_seconds
        self.jingle_player_binary = jingle_player_binary
        self._jingle_player = jingle_player or self._play_jingle_ffplay
        self.breaker_pauses = breaker_pauses
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
        self._short_streak: list[tuple[str, str, int]] = []  # (item id, lane, index) of failed segments
        self._attempts: dict[tuple[str, int], int] = {}  # fast failures per (item id, segment index)
        self._breaker_trips = 0
        self._breaker_active = False  # tripped, and no segment played properly since
        self._was_on_air: bool | None = None
        # Called with (item, "playing" | "played") when an item starts/ends on air - the calls
        # use it to show "läuft jetzt"/"gesendet 07:14" (app/program/calls.py). Must not raise.
        self.on_item_event: Callable[[QueueItem, str], None] | None = None

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
        # Set under the lock that also guards the segment boundary: a skip can't land on the
        # next segment after the current one ended on its own.
        with self._state_lock:
            if self._current is None:
                return False
            self._skip_event.set()
        self._log_line("Überspringen angefordert")
        return True

    def breaker_active(self) -> bool:
        """True from a circuit-breaker trip until a segment plays properly again - the fill
        watcher doesn't start LLM runs for a program that can't be played anyway."""
        return self._breaker_active

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
            if self._current and self._current["lane"] in ("program", *AHEAD_OF_PROGRAM):
                remaining = max(0.0, (self._current["duration"] or 0) - (time.time() - self._current["_started"]))
        return self.queue.remaining_program_seconds(current_remaining=remaining)

    def current_remaining_seconds(self) -> float:
        """Rest of the segment on air (0 when nothing plays)."""
        with self._state_lock:
            if not self._current:
                return 0.0
            return max(0.0, (self._current["duration"] or 0) - (time.time() - self._current["_started"]))

    def start_estimates(self) -> dict[str, datetime]:
        return self.queue.start_estimates(self.current_remaining_seconds())

    def _notify(self, item: QueueItem, event: str) -> None:
        if self.on_item_event is None or not item.call_id:
            return
        try:
            self.on_item_event(item, event)
        except Exception:
            logger.exception("Item listener failed for %s (%s)", item.id, event)

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
        outcome = self._play_segment(item, index)
        if outcome is None:  # removed/expired meanwhile
            return
        if index == len(item.segments) - 1:
            self.queue.finish_item(item.id)
            self._notify(item, "played")
        self._check_breaker(item, index, *outcome)

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

    def _play_segment(self, item: QueueItem, index: int) -> tuple[float, bool, bool] | None:
        """Plays one segment; (elapsed, stopped by skip/stop, raised) or None if the item is gone."""
        segment = item.segments[index]
        if not self.queue.start_segment(item.id, index):
            self._log_line(f"nicht mehr eingeplant, ausgelassen: {segment.title}")
            return None
        started = time.time()
        with self._state_lock:
            # Cleared together with setting _current (see skip()); a pending stop still wins.
            self._skip_event.clear()
            if self._stop_event.is_set():
                self._skip_event.set()
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
        if index == 0:
            self._notify(item, "playing")

        record_timer = None
        if segment.type in PLAYABLE_TYPES and self.play_history_path is not None:
            record_timer = threading.Timer(
                RECORD_AFTER_SECONDS, record_played, args=(self.play_history_path, segment.audio_ref, segment.title)
            )
            record_timer.daemon = True
            record_timer.start()

        stopped = False
        error = False
        try:
            if segment.type in PLAYABLE_TYPES:
                result = self.provider.play_until(self._track(segment), self._skip_event)
                stopped = not result.finished
            elif segment.type == "jingle":
                self._jingle_player(segment.audio_ref, self._skip_event)
                stopped = self._skip_event.is_set()
            else:
                logger.warning("Unknown segment type '%s', skipping", segment.type)
        except Exception:
            error = True
            logger.exception("Failed to play segment %d of %s, skipping", index, item.id)
        finally:
            elapsed = time.time() - started
            if record_timer is not None and elapsed < RECORD_AFTER_SECONDS:
                record_timer.cancel()
            with self._state_lock:
                self._current = None

        if stopped and not self._stop_event.is_set():
            self._log_line(f"übersprungen: {segment.title}")
        return elapsed, stopped, error

    def _check_breaker(self, item: QueueItem, index: int, elapsed: float, user_stopped: bool, error: bool) -> None:
        segment = item.segments[index]
        # Short jingles are fine - only segments that end far before their expected length count.
        playable = segment.type in PLAYABLE_TYPES
        expected = segment.duration_seconds or (segment.estimated_seconds() if playable else 0)
        failed = not user_stopped and elapsed < SHORT_SEGMENT_SECONDS and (error or elapsed < expected * 0.5)
        if not failed:
            self._short_streak.clear()
            self._attempts.clear()
            self._breaker_trips = 0
            self._breaker_active = False
            if self._notice:
                with self._state_lock:
                    self._notice = None
            return
        if playable:
            # Never in the play history (< 30 s) - without this the filler picks it again and again.
            self.filler.report_failure(segment.audio_ref)
        key = (item.id, index)
        attempts = self._attempts.get(key, 0) + 1
        self._attempts[key] = attempts
        if item.lane != "filler" and attempts >= MAX_SEGMENT_ATTEMPTS:
            # Failed again after a pause: most likely the segment itself is broken. It stays
            # consumed and the player moves on right away (no new pause for it).
            self._attempts.pop(key, None)
            self._log_line(f"aufgegeben nach {attempts} Fehlversuchen: {segment.title}")
            logger.warning("Giving up on segment %d of %s (%s) after %d failed attempts",
                           index, item.id, segment.title, attempts)
            return
        self._short_streak.append((item.id, item.lane, index))
        if len(self._short_streak) < BREAKER_SEGMENTS and not self._breaker_active:
            return
        # Most likely the source is down, not the songs: put the program segments back. Filler
        # items aren't - the filler picks a fresh (not just failed) song next time anyway.
        rewound = 0
        for item_id, lane, seg_index in reversed(self._short_streak):
            if lane != "filler" and self.queue.rewind(item_id, seg_index):
                rewound += 1
        count = len(self._short_streak)
        self._short_streak.clear()
        pause = self.breaker_pauses[min(self._breaker_trips, len(self.breaker_pauses) - 1)]
        self._breaker_trips += 1
        self._breaker_active = True
        text = "pausiert: Wiedergabe bricht sofort ab – Musikquelle/Spotify-Gerät nicht erreichbar?"
        with self._state_lock:
            self._notice = text
        self._set_mode("paused", text)
        self._log_line(f"Schutzschalter: {count} Segment(e) in Folge nach < {SHORT_SEGMENT_SECONDS} s beendet, "
                       f"Pause {pause:.0f} s{f', {rewound} Segment(e) bleiben eingeplant' if rewound else ''}")
        self._stop_event.wait(pause)

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
