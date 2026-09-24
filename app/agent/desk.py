"""Desks: one agent configuration each (prompt + tools + trigger), run by the same DeskRunner.

Phase 1 has the music desk: it appends program blocks to the queue whenever the program runs
low (see app/scheduler.py) and keeps the reserve for the filler program fresh.

Every run re-reads the config and the desk prompt and re-scans ./plugins, so edits through the
web UI take effect on the very next run without a restart.

Concurrency (single process, one uvicorn worker): each desk runs at most once at a time. A
trigger arriving while the desk runs doesn't wait and isn't dropped - it marks *one* follow-up
run ("dirty"). After failures a desk backs off (1, 2, 5, 10 min) so a dead LLM isn't asked
every 30 s. There's no global LLM lock (runs mostly wait on the network); only Piper rendering
is serialized (app.audio.tts.RENDER_LOCK).
"""
from __future__ import annotations

import json
import logging
import random
import shutil
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from app.agent.builtin_tools import build_builtin_tools, songs_since_last_announcement
from app.agent.llm import LLMMessage, create_llm_provider
from app.agent.play_history import recently_played
from app.agent.plugin_loader import discover_plugins
from app.agent.tools import ToolRegistry
from app.agent.transcript import load_recent_transcripts, render_history_context, save_transcript
from app.audio.tts import create_tts_engine
from app.config import default_prompt_path, load_config, load_system_prompt, resolve_path, user_prompt_path
from app.music import create_music_provider
from app.program.queue import ProgramQueue, QueueItem

logger = logging.getLogger(__name__)

DESKS = ("music",)
DESK_LABELS = {"music": "Musikredaktion"}

# Fallbacks for keys missing from desks.<name> (the real defaults live in config/config.yaml).
DESK_DEFAULTS: dict[str, dict[str, Any]] = {
    "music": {
        "enabled": True,
        "fill_threshold_minutes": 10,
        "block_minutes": 20,
        "max_queued_program_minutes": 45,
        "songs_per_announcement": 3,
        "no_repeat_minutes": 120,
        "max_tool_iterations": 20,
        "history_runs": 3,
        "plugins": ["get_weather", "get_favorite_songs", "get_news_headlines"],
        "context_plugins": ["get_weather"],
    },
}
BACKOFF_SECONDS = {"music": (60, 120, 300, 600)}
# (min, max) of the numeric desk settings - the API rejects values outside, from_config falls
# back to the default for a bad value already on disk (hand edit, older version).
SETTING_BOUNDS: dict[str, tuple[int, int]] = {
    "fill_threshold_minutes": (1, 240),
    "block_minutes": (1, 240),
    "max_queued_program_minutes": (2, 480),
    "songs_per_announcement": (1, 50),
    "no_repeat_minutes": (0, 10080),
    "max_tool_iterations": (1, 200),
    "history_runs": (0, 50),
}


def _sane_settings(name: str, settings: dict[str, Any]) -> dict[str, Any]:
    """Replaces unusable values (wrong type, out of range) by the defaults, with a warning."""
    defaults = DESK_DEFAULTS.get(name, {})
    for key, default in defaults.items():
        value = settings.get(key)
        if key in SETTING_BOUNDS:
            low, high = SETTING_BOUNDS[key]
            try:
                number = int(value)
                ok = low <= number <= high and not isinstance(value, bool)
            except (TypeError, ValueError):
                ok = False
            if ok:
                settings[key] = number
                continue
        elif isinstance(default, bool):
            if isinstance(value, bool):
                continue
        elif isinstance(default, list):
            if isinstance(value, list):
                continue
        else:
            continue
        logger.warning("desks.%s.%s = %r is not usable, using the default %r", name, key, value, default)
        settings[key] = default
    if "fill_threshold_minutes" in defaults and settings["fill_threshold_minutes"] >= settings["max_queued_program_minutes"]:
        logger.warning("desks.%s: fill_threshold_minutes must be below max_queued_program_minutes, using the defaults", name)
        settings["fill_threshold_minutes"] = defaults["fill_threshold_minutes"]
        settings["max_queued_program_minutes"] = max(defaults["max_queued_program_minutes"],
                                                     settings["max_queued_program_minutes"])
    return settings

#  datetime.strftime("%A") depends on the system locale, which usually isn't set to German
#  on a fresh Raspberry Pi OS - spelling this out explicitly keeps the weekday name German
#  regardless of locale.
GERMAN_WEEKDAYS = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]

# The first block after this long without any program greets the listeners again.
GREETING_GAP = timedelta(minutes=60)


def now_description(now: datetime | None = None) -> str:
    now = now or datetime.now()
    return f"{GERMAN_WEEKDAYS[now.weekday()]}, {now.strftime('%d.%m.%Y')}, {now.strftime('%H:%M')} Uhr"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class DeskConfig:
    name: str
    enabled: bool
    tools: set[str]  # plugin tools this desk may use (builtins are always there)
    context_plugins: set[str]  # plugins whose result is fetched automatically into the input
    history_runs: int
    max_tool_iterations: int
    settings: dict[str, Any]  # the complete desks.<name> block, defaults filled in

    @classmethod
    def from_config(cls, name: str, config: dict[str, Any]) -> "DeskConfig":
        stored = (config.get("desks") or {}).get(name)
        settings = _sane_settings(name, {**DESK_DEFAULTS.get(name, {}), **(stored if isinstance(stored, dict) else {})})
        return cls(
            name=name,
            enabled=bool(settings.get("enabled", True)),
            tools=set(settings.get("plugins") or []),
            context_plugins=set(settings.get("context_plugins") or []),
            history_runs=int(settings.get("history_runs", 3)),
            max_tool_iterations=int(settings.get("max_tool_iterations", 20)),
            settings=settings,
        )

    @property
    def prompt_path(self) -> Path:
        user = user_prompt_path(self.name)
        return user if user.exists() else default_prompt_path(self.name)


@dataclass
class DeskStatus:
    state: str = "idle"  # idle | running | error
    last_run_at: str | None = None
    last_success_at: str | None = None
    last_trigger: str | None = None
    last_final_message: str | None = None
    consecutive_failures: int = 0
    last_error: str | None = None
    backoff_until: str | None = None
    followup_pending: bool = False


@dataclass
class _Slot:
    status: DeskStatus = field(default_factory=DeskStatus)
    running: bool = False
    # (trigger, condition, force) of the one follow-up run, if a trigger came in while running.
    pending: tuple[str, Callable[[], bool] | None, bool] | None = None
    thread: threading.Thread | None = None


class DeskRunner:
    def __init__(self, root_dir: Path, queue: ProgramQueue, player: Any = None):
        self.root_dir = root_dir
        self.queue = queue
        self.player = player  # QueuePlayer, for "what's on air" and the fill level; optional
        self.inbox_dir = root_dir / "data" / "inbox"
        self.processed_dir = root_dir / "data" / "processed"
        self.play_history_path = root_dir / "data" / "playlists" / "play_history.json"
        self.reserve_path = root_dir / "data" / "reserve.json"
        self.wishes_path = root_dir / "data" / "music_wishes.json"
        self.transcripts_dir = root_dir / "data" / "transcripts"
        self.plugins_dir = root_dir / "plugins"
        self._lock = threading.Lock()
        self._slots = {name: _Slot() for name in DESKS}

    # ---------- triggering ----------

    def request(
        self, name: str, trigger: str, condition: Callable[[], bool] | None = None, force: bool = False
    ) -> str:
        """Starts a run in the background, without ever blocking the caller.

        Returns "started", "queued" (desk busy - one follow-up run is marked), "backoff" (recent
        failures, try later) or "disabled". `condition` is re-checked right before the run (and
        before a follow-up), e.g. "program still below the fill threshold". `force` (manual
        runs) ignores the backoff and a disabled desk.
        """
        slot = self._slots[name]
        with self._lock:
            if not force and not DeskConfig.from_config(name, load_config()).enabled:
                return "disabled"
            if not force and self._in_backoff(slot):
                return "backoff"
            if slot.running:
                slot.pending = (trigger, condition, force or bool(slot.pending and slot.pending[2]))
                slot.status.followup_pending = True
                return "queued"
            slot.running = True
            slot.thread = threading.Thread(
                target=self._worker, args=(name, trigger, condition, force), daemon=True, name=f"desk-{name}"
            )
            slot.thread.start()
            return "started"

    def is_running(self, name: str) -> bool:
        return self._slots[name].running

    def wait_idle(self, name: str, timeout: float = 10.0) -> bool:
        """For tests and the CLI: waits until the desk (incl. its follow-up) is done."""
        thread = self._slots[name].thread
        if thread is not None:
            thread.join(timeout)
        return not self._slots[name].running

    def _in_backoff(self, slot: _Slot) -> bool:
        until = slot.status.backoff_until
        return until is not None and datetime.fromisoformat(until) > _utcnow()

    def _worker(self, name: str, trigger: str, condition: Callable[[], bool] | None, force: bool) -> None:
        slot = self._slots[name]
        while True:
            try:
                if condition is None or condition():
                    self._execute(name, trigger)
                else:
                    logger.info("Desk %s: %s run no longer needed, skipped", name, trigger)
            except Exception:
                logger.exception("Desk %s worker failed", name)
            with self._lock:
                pending = slot.pending
                slot.pending = None
                slot.status.followup_pending = False
                if pending is not None and not pending[2] and self._in_backoff(slot):
                    logger.info("Desk %s: follow-up run dropped, backing off after a failure", name)
                    pending = None
                if pending is None:
                    slot.running = False
                    return
            trigger, condition, force = pending

    def _execute(self, name: str, trigger: str) -> dict:
        slot = self._slots[name]
        started = _utcnow()
        slot.status.state = "running"
        slot.status.last_trigger = trigger
        slot.status.last_run_at = started.isoformat()
        try:
            result = self.run(name, trigger)
        except Exception as exc:
            logger.exception("Desk %s run failed", name)
            result = {"desk": name, "trigger": trigger, "error": str(exc), "final_message": ""}
        with self._lock:
            status = slot.status
            status.last_final_message = result.get("final_message") or status.last_final_message
            if result.get("error"):
                status.consecutive_failures += 1
                status.last_error = result["error"]
                steps = BACKOFF_SECONDS.get(name, (60,))
                delay = steps[min(status.consecutive_failures, len(steps)) - 1]
                status.backoff_until = (_utcnow() + timedelta(seconds=delay)).isoformat()
                status.state = "error"
                logger.warning("Desk %s failed (%dx in a row), next try in %ds: %s",
                               name, status.consecutive_failures, delay, result["error"])
            else:
                status.consecutive_failures = 0
                status.last_error = None
                status.backoff_until = None
                status.last_success_at = _utcnow().isoformat()
                status.state = "idle"
        return result

    def status(self, name: str) -> dict:
        slot = self._slots[name]
        with self._lock:
            data = asdict(slot.status)
            if slot.running:
                data["state"] = "running"
        return data

    # ---------- running ----------

    def run(self, name: str, trigger: str = "manual") -> dict:
        """One synchronous run of desk `name` (no locking - use request() for that)."""
        if name == "music":
            return self._run_music(trigger)
        raise ValueError(f"Unknown desk: {name}")

    def remaining_program_seconds(self) -> float:
        if self.player is not None:
            return self.player.remaining_program_seconds()
        return self.queue.remaining_program_seconds()

    def _run_music(self, trigger: str) -> dict:
        config = load_config()
        desk = DeskConfig.from_config("music", config)
        s = desk.settings

        provider = create_music_provider(config)
        tts_engine = create_tts_engine(config, resolve_path(config["audio"]["jingle_cache_dir"]))
        llm = create_llm_provider(config)

        recent_tracks = recently_played(self.play_history_path, int(s["no_repeat_minutes"]))
        appended: list[QueueItem] = []
        registry = ToolRegistry()
        for tool in build_builtin_tools(
            provider,
            tts_engine,
            self.queue,
            recent_tracks=recent_tracks,
            block_minutes=int(s["block_minutes"]),
            songs_per_announcement=int(s["songs_per_announcement"]),
            max_queued_program_minutes=int(s["max_queued_program_minutes"]),
            remaining_program_seconds=self.remaining_program_seconds,
            reserve_path=self.reserve_path,
            desk="music",
            appended=appended,
        ):
            registry.register(tool)
        self._register_plugins(registry, desk, config)

        inbox_items = self._collect_inbox()
        user_content = self._music_user_message(config, desk, trigger, provider, inbox_items, recent_tracks)

        # Saved as part of this run's transcript: static system prompt, auto-fetched plugin
        # context, the actual input, and the full tool-calling exchange.
        messages = [LLMMessage(role="system", content=load_system_prompt("music"))]
        context_message = self._plugin_context_message(registry, desk)
        if context_message is not None:
            messages.append(context_message)
        messages.append(LLMMessage(role="user", content=user_content))

        # NOT saved: a summary of the last few runs of this desk, prepended only for the LLM
        # calls below, so it never gets embedded into this run's own transcript (which would
        # otherwise compound in size every run once a later run reads this one back).
        history = render_history_context(
            load_recent_transcripts(self.transcripts_dir, n=desk.history_runs, desk="music")
        )
        history_message = LLMMessage(role="system", content=history) if history else None

        final_text, error = self._llm_loop(llm, messages, history_message, registry, desk.max_tool_iterations)

        if error is None and not appended and self.remaining_program_seconds() < s["fill_threshold_minutes"] * 60:
            # Counts as a failure (backoff) - otherwise the fill watcher would ask again in 30 s.
            error = "Die Musikredaktion hat keinen Programmblock angehängt."

        script = {
            "items": [i.id for i in appended],
            "segments": [asdict(seg) for item in appended for seg in item.segments],
        }
        wishes = "\n".join(text for _path, text in inbox_items if text)
        transcript = save_transcript(
            self.transcripts_dir, messages, final_text, script, error=error,
            desk="music", trigger=trigger, inputs=wishes,
        )
        if error is None:
            self._archive_inbox(inbox_items)
        return {
            "desk": "music",
            "trigger": trigger,
            "final_message": final_text,
            "error": error,
            "items": script["items"],
            "segment_count": len(script["segments"]),
            "transcript_id": transcript.stem,
        }

    @staticmethod
    def _llm_loop(llm, messages, history_message, registry: ToolRegistry, max_iterations: int) -> tuple[str, str | None]:
        tool_schemas = registry.to_schemas()
        try:
            for _iteration in range(max_iterations):
                llm_input = ([history_message] if history_message else []) + messages
                response = llm.chat(llm_input, tool_schemas)
                messages.append(response)

                if not response.tool_calls:
                    return response.content, None

                for tool_call in response.tool_calls:
                    logger.info("Tool call: %s(%s)", tool_call.name, tool_call.arguments)
                    result = registry.call(tool_call.name, tool_call.arguments)
                    messages.append(
                        LLMMessage(role="tool", content=result, tool_call_id=tool_call.id, name=tool_call.name)
                    )
            logger.warning("Desk run hit max_tool_iterations (%d) without a final message", max_iterations)
            return "", None
        except Exception as exc:
            logger.exception("Desk LLM loop failed")
            return "", str(exc)

    def _register_plugins(self, registry: ToolRegistry, desk: DeskConfig, config: dict) -> None:
        disabled = config.get("plugins", {}).get("disabled")
        for tool in discover_plugins(self.plugins_dir, disabled):
            if tool.name in desk.tools or tool.name in desk.context_plugins:
                tool.context = tool.name in desk.context_plugins
                registry.register(tool)

    @staticmethod
    def _plugin_context_message(registry: ToolRegistry, desk: DeskConfig) -> LLMMessage | None:
        blocks = []
        for tool in registry.all():
            if tool.source != "plugin" or tool.name not in desk.context_plugins:
                continue
            result = registry.call(tool.name, tool.context_args)
            blocks.append(f"### {tool.name}\n{result}")
        if not blocks:
            return None
        content = "Automatisch geladener Kontext (kein Tool-Aufruf nötig):\n\n" + "\n\n".join(blocks)
        return LLMMessage(role="system", content=content)

    # ---------- music desk context ----------

    def _music_user_message(
        self,
        config: dict,
        desk: DeskConfig,
        trigger: str,
        provider,
        inbox_items: list[tuple[Path, str]],
        recent_tracks: list[dict],
    ) -> str:
        s = desk.settings
        block_minutes = int(s["block_minutes"])
        per_announcement = int(s["songs_per_announcement"])
        tail = self.queue.program_tail()
        remaining_minutes = round(self.remaining_program_seconds() / 60)
        parts = [f"Aktuelle Zeit: {now_description()}."]

        now_playing = self._now_playing_line()
        if now_playing:
            parts.append(f"Läuft gerade: {now_playing}")

        last_activity = self.queue.last_program_activity()
        greet = trigger == "broadcast_start" or (
            not tail and (last_activity is None or _utcnow() - last_activity > GREETING_GAP)
        )
        if greet:
            parts.append("Das ist der erste Block nach Sendebeginn: begrüße die Hörer kurz.")
        else:
            parts.append(
                "Das Programm läuft bereits: knüpfe an das Bisherige an und begrüße nicht neu."
            )

        if tail:
            lines = [f"- [{'Ansage' if seg.type == 'jingle' else 'Song'}] {seg.text or seg.title}" for seg in tail[-8:]]
            parts.append("Ende der Warteschlange (dein Block wird danach gespielt):\n" + "\n".join(lines))
        since = songs_since_last_announcement(tail)
        rule = (
            f"Plane jetzt einen Block von ca. {block_minutes} Minuten (ca. {max(1, round(block_minutes / 3.5))} "
            f"Songs plus Ansagen) und hänge ihn mit append_program_block an. Das Programm reicht noch ca. "
            f"{remaining_minutes} Minuten. Höchstens eine Ansage je {per_announcement} Songs"
        )
        if since is not None:
            rule += f" (seit der letzten Ansage sind {since} Songs eingeplant)"
        parts.append(rule + ". Jeder Song darf nur einmal vorkommen.")

        planned = [seg.title for seg in self.queue.planned_tracks()]
        blocked = list(dict.fromkeys([e["title"] for e in recent_tracks] + planned))
        if blocked:
            parts.append(
                f"Liefen in den letzten {s['no_repeat_minutes']} Minuten oder sind schon eingeplant (nicht "
                "erneut einplanen, werden sonst automatisch entfernt):\n" + "\n".join(f"- {t}" for t in blocked)
            )

        favorites = self._favorites_context(config, provider)
        if favorites:
            parts.append("Lieblings-Playlists des Haushalts (Geschmack treffen):\n" + favorites)

        wishes = self._open_wishes()
        if wishes:
            parts.append(
                "Offene Musikwünsche (in einem der nächsten Blöcke einbauen, gern mit Gruß an den Absender; "
                "Segment mit wish_id markieren; auch in der Reserve berücksichtigen):\n" + wishes
            )
        if inbox_items:
            texts = "\n".join(f"{i + 1}. {text}" for i, (_path, text) in enumerate(inbox_items) if text)
            parts.append("Neue Hörerwünsche:\n" + texts)
        else:
            parts.append("Es liegen keine neuen Hörerwünsche vor.")

        parts.append(
            "Aktualisiere außerdem mit update_reserve die Reserve (15-20 Songs für den Notfall, ohne "
            "Ansagen, nach denselben Regeln ausgewählt)."
        )
        return "\n\n".join(parts)

    def _now_playing_line(self) -> str | None:
        if self.player is None:
            return None
        current = self.player.status().get("current")
        if not current:
            return None
        kind = "Ansage" if current.get("type") == "jingle" else "Song"
        return f"[{kind}] {current.get('text') or current.get('title')}"

    @staticmethod
    def _favorites_context(config: dict, provider) -> str:
        playlist_ids = config.get("music", {}).get("favorite_playlists") or []
        if not playlist_ids:
            return ""
        try:
            names = {p.id: p.name for p in provider.list_playlists()}
        except Exception:
            names = {}
        blocks = []
        for playlist_id in playlist_ids[:5]:
            try:
                tracks = provider.get_playlist_tracks(playlist_id)
            except Exception:
                logger.warning("Could not load favorite playlist %s", playlist_id, exc_info=True)
                continue
            sample = random.sample(tracks, min(8, len(tracks)))
            titles = ", ".join(f"{t.title}{f' ({t.artist})' if t.artist else ''}" for t in sample)
            blocks.append(f"- {names.get(playlist_id, playlist_id)} (id={playlist_id}): {titles or 'leer'}")
        return "\n".join(blocks)

    def _open_wishes(self) -> str:
        """Open wishes from the wish inbox (filled by the dispatch desk from Phase 2 on)."""
        if not self.wishes_path.exists():
            return ""
        try:
            data = json.loads(self.wishes_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("Could not read %s", self.wishes_path, exc_info=True)
            return ""
        wishes = data.get("wishes", []) if isinstance(data, dict) else data
        now = _utcnow()
        lines = []
        for wish in wishes if isinstance(wishes, list) else []:
            if not isinstance(wish, dict) or wish.get("status") not in (None, "open", "noted"):
                continue
            valid_until = wish.get("valid_until")
            try:
                if valid_until and datetime.fromisoformat(valid_until).astimezone(timezone.utc) < now:
                    continue  # expired wishes are dropped silently
            except ValueError:
                pass
            author = f" (von {wish['author']})" if wish.get("author") else ""
            until = f", gültig bis {valid_until}" if valid_until else ""
            lines.append(f"- {wish.get('text', '')}{author} [wish_id={wish.get('id', '?')}{until}]")
        return "\n".join(lines)

    # ---------- legacy inbox (until the calls of Phase 2) ----------

    def _collect_inbox(self) -> list[tuple[Path, str]]:
        if not self.inbox_dir.exists():
            return []
        items = []
        for path in sorted(self.inbox_dir.glob("*.txt")):
            try:
                items.append((path, path.read_text(encoding="utf-8").strip()))
            except OSError:
                logger.exception("Could not read inbox file %s", path)
        return items

    def _archive_inbox(self, inbox_items: list[tuple[Path, str]]) -> None:
        self.processed_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        for path, _text in inbox_items:
            try:
                shutil.move(str(path), str(self.processed_dir / f"{stamp}_{path.name}"))
            except OSError:
                logger.exception("Could not archive inbox file %s", path)
