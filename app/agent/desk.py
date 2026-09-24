"""Desks: one agent configuration each (prompt + tools + trigger), run by the same DeskRunner.

- music: appends program blocks to the queue whenever the program runs low (see
  app/scheduler.py) and keeps the reserve for the filler program fresh.
- news: prepares a bulletin `lead_minutes` before every slot (:00 full, :30 short by default)
  from the news/weather plugins and the news mailbox - see app/program/news.py.
- dispatch ("Leitstelle"): runs right away for every listener call and routes it (lights,
  "als Nächstes", wish mailbox, news mailbox) - see app/agent/dispatch.py.

Every run re-reads the config and the desk prompt and re-scans ./plugins, so edits through the
web UI take effect on the very next run without a restart.

Concurrency (single process, one uvicorn worker): each desk runs at most once at a time. A
trigger arriving while the desk runs doesn't wait and isn't dropped - it marks *one* follow-up
run ("dirty"). After failures a desk backs off (1, 2, 5, 10 min) so a dead LLM isn't asked
every 30 s. There's no global LLM lock (runs mostly wait on the network); only Piper rendering
is serialized (app.audio.tts.RENDER_LOCK).
"""
from __future__ import annotations

import logging
import random
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from app.agent.builtin_tools import build_builtin_tools, songs_since_last_announcement
from app.agent.dispatch import DispatchSession, InfoCache, call_line
from app.agent.llm import LLMMessage, create_llm_provider
from app.agent.play_history import recently_played
from app.agent.plugin_loader import discover_plugins
from app.agent.tools import ToolRegistry
from app.agent.transcript import load_recent_transcripts, render_history_context, save_transcript
from app.audio.tts import create_tts_engine
from app.config import (
    default_prompt_path,
    is_broadcast_time,
    load_config,
    load_system_prompt,
    next_broadcast_start,
    resolve_path,
    user_prompt_path,
)
from app.music import create_music_provider
from app.program.calls import CallStore, hhmm
from app.program.mailboxes import news_mailbox, wish_mailbox
from app.program.news import (
    FORMAT_LABELS,
    FORMAT_SOURCES,
    FORMAT_WORDS,
    PLACEMENTS,
    SOURCE_PLUGINS,
    SOURCES,
    NewsSession,
    hhmm_local,
    load_last_bulletin,
    next_slot,
    normalize_slots,
    notes_for,
    prepared_for,
)
from app.program.queue import ProgramQueue, QueueItem

logger = logging.getLogger(__name__)

DESKS = ("music", "news", "dispatch")
DESK_LABELS = {"music": "Musikredaktion", "news": "Nachrichtenredaktion", "dispatch": "Leitstelle"}

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
    "news": {
        "enabled": True,
        "slots": [{"minute": "00", "format": "full"}, {"minute": "30", "format": "short"}],
        "lead_minutes": 5,
        "placement": "after_song",
        "max_delay_minutes": 15,
        "sources": ["news", "weather", "notes"],
        "intro": True,
        "max_tool_iterations": 8,
        "history_runs": 0,
        "plugins": [],
    },
    "dispatch": {
        "enabled": True,
        "allow_interrupt": True,
        "min_minutes_between_interrupts": 10,
        "reply_expires_minutes": 30,
        "wish_default_valid_hours": 24,
        "max_tool_iterations": 6,
        "plugins": ["control_hue_lights", "get_weather", "get_news_headlines", "get_favorite_songs"],
    },
}
# The news desk retries within the few lead minutes before its slot (the scheduler re-requests).
BACKOFF_SECONDS = {"music": (60, 120, 300, 600), "news": (60, 120, 300, 600), "dispatch": (10, 30, 60)}
# Sent once when the dispatch desk answered a call with plain text and no tool.
NO_ACTION_NUDGE = (
    "Du hast noch kein Tool benutzt, beim Hörer ist also nichts angekommen. Nutze die Tools: reply für eine "
    "Antwort oder Rückfrage, play_next für Songs, add_music_wish für Wünsche, note_for_news für Hinweise, "
    "Plugins für direkte Aktionen. Wenn wirklich nichts zu tun ist, antworte nur kurz mit dem Grund."
)
# Earlier calls (with their results) the dispatch desk sees as context.
DISPATCH_RECENT_CALLS = 5
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
    "min_minutes_between_interrupts": (0, 240),
    "reply_expires_minutes": (1, 1440),
    "wish_default_valid_hours": (1, 336),
    "lead_minutes": (1, 15),
    "max_delay_minutes": (1, 30),
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
        elif key == "slots":
            slots = normalize_slots(value)
            if slots is not None:
                settings[key] = slots
                continue
        elif key == "sources":
            if isinstance(value, list) and all(v in SOURCES for v in value):
                continue
        elif key == "placement":
            if value in PLACEMENTS:
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
        self.play_history_path = root_dir / "data" / "playlists" / "play_history.json"
        self.reserve_path = root_dir / "data" / "reserve.json"
        self.news_last_path = root_dir / "data" / "news_last.json"
        self.transcripts_dir = root_dir / "data" / "transcripts"
        self.plugins_dir = root_dir / "plugins"
        self.wishes = wish_mailbox(root_dir / "data")
        self.news_notes = news_mailbox(root_dir / "data")
        self.calls = CallStore(root_dir / "data" / "calls", queue, self.wishes, self.news_notes, self.start_estimates)
        self.info_cache = InfoCache()
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
        if name == "news":
            return self._run_news(trigger)
        if name == "dispatch":
            return self._run_dispatch(trigger)
        raise ValueError(f"Unknown desk: {name}")

    def remaining_program_seconds(self) -> float:
        if self.player is not None:
            return self.player.remaining_program_seconds()
        return self.queue.remaining_program_seconds()

    def start_estimates(self) -> dict[str, datetime]:
        if self.player is not None:
            return self.player.start_estimates()
        return self.queue.start_estimates()

    def _run_music(self, trigger: str) -> dict:
        config = load_config()
        desk = DeskConfig.from_config("music", config)
        s = desk.settings

        provider = create_music_provider(config)
        tts_engine = create_tts_engine(config, resolve_path(config["audio"]["jingle_cache_dir"]))
        llm = create_llm_provider(config)

        recent_tracks = recently_played(self.play_history_path, int(s["no_repeat_minutes"]))
        try:
            self.calls.sync()  # gives wishes of blocks that expired unplayed back to the mailbox
        except Exception:
            logger.warning("Could not sync the calls before the music desk run", exc_info=True)
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
            wishes=self.wishes,
        ):
            registry.register(tool)
        self._register_plugins(registry, desk, config)

        open_wishes = self.wishes.open()
        user_content = self._music_user_message(config, desk, trigger, provider, open_wishes, recent_tracks)

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
        wishes = "\n".join(w["text"] for w in open_wishes)
        transcript = save_transcript(
            self.transcripts_dir, messages, final_text, script, error=error,
            desk="music", trigger=trigger, inputs=wishes,
        )
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
        open_wishes: list[dict],
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

        if open_wishes:
            lines = []
            for wish in open_wishes:
                author = f" (von {wish['author']})" if wish.get("author") else ""
                until = f", gültig bis {hhmm(wish['valid_until'], '%d.%m. %H:%M')}" if wish.get("valid_until") else ""
                lines.append(f"- {wish.get('text', '')}{author} [wish_id={wish['id']}{until}]")
            parts.append(
                "Offene Musikwünsche aus dem Wunsch-Postfach (in diesem oder einem der nächsten Blöcke einbauen, "
                "gern mit Gruß an den Absender; das erfüllende Segment mit wish_id markieren; auch in der Reserve "
                "berücksichtigen):\n" + "\n".join(lines)
            )
        else:
            parts.append("Es liegen keine offenen Musikwünsche vor.")

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

    # ---------- news desk ----------

    def next_news_slot(self, config: dict | None = None) -> tuple[datetime, str] | None:
        config = config or load_config()
        return next_slot(config, DeskConfig.from_config("news", config).settings)

    def news_prepared(self, slot: datetime) -> bool:
        return prepared_for(self.queue, slot) is not None

    def last_bulletin(self) -> dict | None:
        last = load_last_bulletin(self.news_last_path)
        if last is None:
            return None
        item = self.queue.get(last.get("item_id") or "")
        return {**last, "status": item.status if item else None}

    def _run_news(self, trigger: str) -> dict:
        """Prepares the bulletin of the next slot (in the broadcast window): fetches the sources,
        hands the format to the LLM, which calls schedule_news once."""
        config = load_config()
        desk = DeskConfig.from_config("news", config)
        s = desk.settings
        target = next_slot(config, s)
        if target is None:
            return {"desk": "news", "trigger": trigger, "error": None, "items": [],
                    "final_message": "Keine Ausgabe im Sendefenster der nächsten 48 Stunden."}
        slot, fmt = target
        sources = set(s["sources"])
        tts = create_tts_engine(config, resolve_path(config["audio"]["jingle_cache_dir"]))
        llm = create_llm_provider(config)
        offered = notes_for(fmt, self.news_notes) if "notes" in sources else ([], [])
        session = NewsSession(slot, fmt, s, self.queue, self.news_notes, tts, offered, self.news_last_path)

        registry = ToolRegistry()
        registry.register(session.tool())
        self._register_plugins(registry, desk, config)
        context = self._news_context(config, sources, fmt)

        messages = [LLMMessage(role="system", content=load_system_prompt("news"))]
        if context:
            messages.append(LLMMessage(role="system", content=context))
        messages.append(LLMMessage(role="user", content=self._news_user_message(slot, fmt, s, offered)))
        final_text, error = self._llm_loop(llm, messages, None, registry, desk.max_tool_iterations)
        if error is None and session.item is None:
            # Counts as a failure (backoff): the scheduler asks again while the slot is still ahead.
            error = "Die Nachrichtenredaktion hat keine Ausgabe eingeplant."

        item = session.item
        script = {"items": [item.id] if item else [], "segments": [asdict(seg) for seg in item.segments] if item else []}
        transcript = save_transcript(
            self.transcripts_dir, messages, final_text, script, error=error, desk="news", trigger=trigger,
            inputs=f"Ausgabe {hhmm_local(slot)} ({FORMAT_LABELS[fmt]})",
        )
        return {
            "desk": "news", "trigger": trigger, "final_message": final_text, "error": error,
            "items": script["items"], "slot": slot.isoformat(), "format": fmt,
            "notes_used": session.used_notes, "transcript_id": transcript.stem,
        }

    def _news_context(self, config: dict, sources: set[str], fmt: str) -> str:
        """The news and weather plugins' results for this bulletin (always fetched fresh)."""
        wanted = {SOURCE_PLUGINS[s]: s for s in ("news", "weather") if s in sources}
        if not wanted:
            return ""
        headlines, forecast = FORMAT_SOURCES[fmt]
        args = {"news": {"limit": headlines}, "weather": {"forecast": True} if forecast else {}}
        disabled = config.get("plugins", {}).get("disabled")
        tools = {t.name: t for t in discover_plugins(self.plugins_dir, disabled) if t.name in wanted}
        titles = {"news": "Schlagzeilen", "weather": "Wetter" + (" mit Vorhersage" if forecast else " (jetzt)")}
        blocks = []
        for name, source in wanted.items():
            tool = tools.get(name)
            if tool is None:
                blocks.append(f"### {titles[source]}\nnicht verfügbar (Plugin {name} fehlt oder ist ausgeschaltet) - "
                              "diesen Teil weglassen.")
                continue
            try:
                try:
                    result = tool.func(**args[source])
                except TypeError:  # an older plugin without these arguments
                    result = tool.func()
            except Exception as exc:
                logger.warning("News source %s failed: %s", name, exc)
                result = f"nicht verfügbar ({exc}) - diesen Teil weglassen."
            blocks.append(f"### {titles[source]}\n{result}")
        return "Quellen für diese Ausgabe (automatisch geladen, kein Tool-Aufruf nötig):\n\n" + "\n\n".join(blocks)

    def _news_user_message(self, slot: datetime, fmt: str, s: dict, offered: tuple[list[dict], list[dict]]) -> str:
        sources = set(s["sources"])
        low, high = FORMAT_WORDS[fmt]
        minutes = "ca. 2-3 Minuten" if fmt == "full" else "ca. 30-60 Sekunden"
        parts = [f"Aktuelle Zeit: {now_description()}."]
        if fmt == "full":
            spec = [
                f"Bereite die Nachrichten um {hhmm_local(slot)} Uhr vor. Format: AUSFÜHRLICH ({minutes}, "
                f"ca. {low}-{high} Wörter).",
                "- mehrere Schlagzeilen (4-6), je 1-2 Sätze" if "news" in sources else None,
                "- das Wetter mit Vorhersage" if "weather" in sources else None,
                "- alle Hinweise aus dem Meldungs-Postfach unten" if "notes" in sources else None,
            ]
        else:
            spec = [
                f"Bereite die Kurznachrichten um {hhmm_local(slot)} Uhr vor. Format: KURZ ({minutes}, "
                f"ca. {low}-{high} Wörter).",
                "- 2-3 Schlagzeilen, je ein Satz" if "news" in sources else None,
                "- das Wetter jetzt, ein Satz" if "weather" in sources else None,
                "- nur die neuen Hinweise aus dem Meldungs-Postfach unten" if "notes" in sources else None,
            ]
        parts.append("\n".join(line for line in spec if line))
        if s.get("intro", True):
            parts.append("Die Einleitung („Die Nachrichten um …“) setzt der Sender automatisch davor - beginne "
                         "direkt mit der ersten Meldung.")

        if "notes" in sources:
            mandatory, optional = offered

            def line(note: dict) -> str:
                author = f" (von {note['author']})" if note.get("author") else ""
                until = f", gültig bis {hhmm(note['valid_until'], '%d.%m. %H:%M')}" if note.get("valid_until") else ""
                aired = " - schon einmal gemeldet" if note["status"] == "used" else ""
                return f"- {note.get('text', '')}{author} [note_id={note['id']}{until}{aired}]"

            if mandatory:
                parts.append("Hinweise aus dem Meldungs-Postfach (von Hörern, alle erwähnen, als lokale Meldung "
                             "formulieren):\n" + "\n".join(line(n) for n in mandatory))
            else:
                parts.append("Keine neuen Hinweise im Meldungs-Postfach.")
            if optional:
                parts.append("Schon gemeldet, noch gültig (nur erwähnen, wenn es gerade wieder wichtig ist, z.B. "
                             "weil es bald so weit ist - dann note_ids mitgeben):\n" + "\n".join(line(n) for n in optional))

        last = load_last_bulletin(self.news_last_path)
        if last:
            parts.append(
                f"Die letzte Ausgabe ({hhmm_local(last.get('slot'))} Uhr, {FORMAT_LABELS.get(last.get('format'), '')}) "
                f"lautete:\n„{last['text']}“\nWiederhole sie nicht wortgleich: gleiche Themen neu formulieren, "
                "Neues nach vorn, Überholtes weglassen."
            )
        parts.append("Rufe dann genau einmal schedule_news mit dem kompletten Sprechtext auf: Fließtext zum "
                     "Vorlesen, keine Überschriften, keine Aufzählungszeichen, kein Markdown.")
        return "\n\n".join(parts)

    # ---------- dispatch desk ----------

    def _run_dispatch(self, trigger: str) -> dict:
        """Works through the pending calls, oldest first, one LLM conversation per call (so every
        action is attributable). Calls arriving meanwhile are picked up by the same loop. Stops at
        the first LLM failure: that call is `retrying`, the desk backs off, the rest waits."""
        config = load_config()
        desk = DeskConfig.from_config("dispatch", config)
        self.calls.expire_stale(int(desk.settings["reply_expires_minutes"]))
        tools: dict[str, Any] = {}
        handled: list[str] = []
        error = None
        while (call := self.calls.claim_next()) is not None:
            handled.append(call["id"])
            try:
                error = self._dispatch_call(call, config, desk, trigger, tools)
            except Exception as exc:
                # Outside the LLM loop (plugins, context, commit): never leave the call `processing`.
                logger.exception("Dispatching call %s failed", call["id"])
                error = self._dispatch_failed(call, [], None, f"interner Fehler: {exc}", trigger)
            if error is not None:
                break
        self.calls.sync()
        if not handled:
            final = "Keine offenen Zwischenrufe."
        else:
            final = f"{len(handled)} Zwischenruf(e) bearbeitet" + (" – Leitstelle nicht erreichbar" if error else ".")
        return {"desk": "dispatch", "trigger": trigger, "final_message": final, "error": error, "calls": handled}

    def _dispatch_tools(self, config: dict, tools: dict[str, Any]) -> dict[str, Any]:
        """Music source, TTS and LLM, created once per run (lazily: a run without calls needs none)."""
        if not tools:
            try:
                tools["provider"] = create_music_provider(config)
            except Exception as exc:
                logger.warning("Dispatch: music source unavailable: %s", exc)
                tools["provider"], tools["provider_error"] = None, str(exc)
            try:
                tools["tts"] = create_tts_engine(config, resolve_path(config["audio"]["jingle_cache_dir"]))
            except Exception as exc:
                logger.warning("Dispatch: TTS unavailable: %s", exc)
                tools["tts"] = None
        if tools.get("llm") is None:
            tools["llm"] = create_llm_provider(config)
        return tools

    def _dispatch_call(self, call: dict, config: dict, desk: DeskConfig, trigger: str, tools: dict) -> str | None:
        """One call through the dispatch desk; returns an error (LLM unreachable) or None."""
        on_air = is_broadcast_time(config)
        next_start = next_broadcast_start(config)
        next_start = next_start.astimezone(timezone.utc) if next_start else None
        try:
            tools = self._dispatch_tools(config, tools)
        except Exception as exc:
            return self._dispatch_failed(call, [], None, str(exc), trigger)
        session = DispatchSession(
            call, desk.settings, self.queue, self.wishes, self.news_notes, tools["provider"], tools["tts"],
            self.start_estimates, on_air=on_air, next_on_air=next_start, provider_error=tools.get("provider_error"),
        )
        # Each direct action is saved on the call right away: after a crash mid-run it isn't
        # repeated (the next attempt is told about it, and the same action is skipped).
        session.on_done = lambda done: self.calls.update(call["id"], lambda c: c.update(actions=list(done)))
        registry = ToolRegistry()
        for tool in session.tools():
            registry.register(tool)
        disabled = config.get("plugins", {}).get("disabled")
        for tool in discover_plugins(self.plugins_dir, disabled):
            if tool.name in desk.tools:
                registry.register(session.wrap_plugin(tool) if tool.action else self.info_cache.wrap(tool))

        messages = [
            LLMMessage(role="system", content=load_system_prompt("dispatch")),
            LLMMessage(role="user", content=self._dispatch_user_message(call, on_air, next_start)),
        ]
        final_text, error = self._llm_loop(tools["llm"], messages, None, registry, desk.max_tool_iterations)
        if error is None and not session.staged and not session.done:
            # Plain text only, nothing staged: ask once more. A text without tools stays text - it
            # isn't read on air (it's usually addressed to us, not to the listener); the call
            # shows it as "keine Aktion: …" instead of "erledigt".
            logger.info("Call %s: the dispatch desk used no tool, nudging once", call["id"])
            messages.append(LLMMessage(role="user", content=NO_ACTION_NUDGE))
            retry_text, error = self._llm_loop(tools["llm"], messages, None, registry, desk.max_tool_iterations)
            final_text = retry_text or final_text
        if error is not None:
            return self._dispatch_failed(call, messages, session.done, error, trigger)

        current = self.calls.get(call["id"])
        if current is None or current["status"] != "processing":
            # Withdrawn meanwhile: direct actions already happened, nothing else is planned.
            logger.info("Call %s was withdrawn during dispatch, not committing", call["id"])
            if current is not None:
                self.calls.update(call["id"], lambda c: c.update(actions=session.done))
            return None
        committed = session.commit()

        def done(c: dict) -> None:
            c.update(
                status="queued" if committed.item else "routed",
                actions=committed.actions,
                reply_text=committed.reply_text,
                reply_audio=committed.reply_audio,
                final_message=final_text or None,
                eta=committed.eta,
                error=None,
                attempts=c.get("attempts", 0) + 1,
                dispatched=True,
            )
        self.calls.update(call["id"], done)
        self.calls.sync([call["id"]])
        save_transcript(
            self.transcripts_dir, messages, final_text,
            {"items": [committed.item.id] if committed.item else [],
             "segments": [asdict(seg) for seg in committed.item.segments] if committed.item else []},
            desk="dispatch", trigger=trigger, inputs=f"{call.get('author') or 'jemand'}: {call['text']}",
        )
        logger.info("Call %s dispatched: %s", call["id"], "; ".join(a["summary"] for a in committed.actions) or "keine Aktion")
        return None

    def _dispatch_failed(self, call: dict, messages: list, done: list | None, error: str, trigger: str) -> str:
        """`done`: the direct actions executed so far (None = unchanged)."""
        def retry(c: dict) -> None:
            # Direct actions that already ran are kept (and not repeated on the next attempt).
            c.update(status="retrying" if c["status"] == "processing" else c["status"], error=error,
                     attempts=c.get("attempts", 0) + 1)
            if done is not None:
                c["actions"] = done
        self.calls.update(call["id"], retry)
        if messages:
            save_transcript(self.transcripts_dir, messages, "", None, error=error, desk="dispatch", trigger=trigger,
                            inputs=f"{call.get('author') or 'jemand'}: {call['text']}")
        logger.warning("Call %s: dispatch failed, will retry: %s", call["id"], error)
        return error

    def _dispatch_user_message(self, call: dict, on_air: bool, next_start: datetime | None) -> str:
        parts = [f"Aktuelle Zeit: {now_description()}."]
        if on_air:
            parts.append("Der Sender ist auf Sendung.")
        else:
            start = f" bis {hhmm(next_start)}" if next_start else ""
            parts.append(
                f"Sendepause{start}: Ansagen und Songs laufen erst zum Sendebeginn; direkte Aktionen (z.B. Licht), "
                "Musikwünsche und Hinweise für die Nachrichten werden trotzdem sofort erledigt."
            )
        now_playing = self._now_playing_line()
        if now_playing:
            parts.append(f"Läuft gerade: {now_playing}")
        upcoming = []
        for item in self.queue.active_items():
            if item.status == "playing":
                segments = item.remaining_segments()
            else:
                segments = item.segments
            upcoming.extend(f"- [{'Ansage' if s.type == 'jingle' else 'Song'}] {s.text or s.title}" for s in segments)
            if len(upcoming) >= 3:
                break
        if upcoming:
            parts.append("Als Nächstes geplant:\n" + "\n".join(upcoming[:3]))
        parts.append(
            "Hinweis: Unterbrechen ist in dieser Version noch nicht möglich - alles mit Dringlichkeit „sofort“ "
            "läuft direkt nach dem aktuellen Song."
        )
        recent = [c for c in self.calls.all() if c["id"] != call["id"] and c.get("dispatched")][:DISPATCH_RECENT_CALLS]
        if recent:
            parts.append(
                "Letzte Zwischenrufe mit ihrem Ergebnis, neueste zuerst (nur zur Orientierung, nicht erneut "
                "erledigen):\n" + "\n".join(call_line(c) for c in recent)
            )
        done = [a for a in call.get("actions") or [] if a.get("type") == "plugin"]
        if done:
            parts.append(
                "Bei diesem Zwischenruf bereits erledigt (nicht wiederholen):\n"
                + "\n".join(f"- {a['summary']}" for a in done)
            )
        who = call.get("author") or "ein Hörer (Name unbekannt)"
        parts.append(f"Neuer Zwischenruf von {who} ({hhmm(call.get('created_at'))} Uhr):\n„{call['text']}“")
        return "\n\n".join(parts)
