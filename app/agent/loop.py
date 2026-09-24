"""The generation half: one LLM-driven pass that turns the inbox + config into a new script.

This is deliberately re-entrant and stateless across runs (aside from files on disk): every
call to `run_once()` re-reads config.yaml / system_prompt.md and re-scans ./plugins, so editing
either through the web UI takes effect on the very next scheduled tick without a restart.
"""
from __future__ import annotations

import json
import logging
import shutil
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from app.agent.builtin_tools import build_builtin_tools
from app.agent.llm import LLMMessage, create_llm_provider
from app.agent.plugin_loader import register_plugins
from app.agent.script import load_script
from app.agent.tools import ToolRegistry
from app.agent.transcript import load_recent_transcripts, render_history_context, save_transcript
from app.audio.tts import create_tts_engine
from app.config import load_config, load_system_prompt, resolve_path
from app.music import create_music_provider

logger = logging.getLogger(__name__)


def min_program_minutes_for(config: dict) -> int:
    """How long a script has to run so playback never outruns the next generation run.

    One loop interval plus a buffer for the generation itself (LLM round-trips, TTS) and a
    failed run or two - the player switches to a newer script at the next segment boundary
    anyway, so a longer script never delays fresh content.
    """
    interval_minutes = config.get("agent", {}).get("loop_interval_seconds", 1800) / 60
    return round(interval_minutes + max(10, interval_minutes / 3))


class AgentLoop:
    def __init__(self, root_dir: Path):
        self.root_dir = root_dir
        self.inbox_dir = root_dir / "data" / "inbox"
        self.processed_dir = root_dir / "data" / "processed"
        self.script_path = root_dir / "data" / "playlists" / "current_script.json"
        self.state_path = root_dir / "data" / "state.json"
        self.transcripts_dir = root_dir / "data" / "transcripts"
        self.plugins_dir = root_dir / "plugins"

    def run_once(self) -> dict:
        config = load_config()
        system_prompt = load_system_prompt()

        provider = create_music_provider(config)
        tts_engine = create_tts_engine(config, resolve_path(config["audio"]["jingle_cache_dir"]))
        llm = create_llm_provider(config)

        min_program_minutes = min_program_minutes_for(config)
        registry = ToolRegistry()
        for tool in build_builtin_tools(provider, tts_engine, self.script_path, min_program_minutes):
            registry.register(tool)
        register_plugins(registry, self.plugins_dir, config.get("plugins", {}).get("disabled"))

        inbox_items = self._collect_inbox()
        user_content = self._build_user_message(inbox_items, min_program_minutes)

        # Saved as part of this run's transcript: static system prompt, auto-fetched plugin
        # context, the actual wishes, and the full tool-calling exchange.
        messages = [
            LLMMessage(role="system", content=system_prompt),
        ]
        context_message = self._build_plugin_context_message(registry)
        if context_message is not None:
            messages.append(context_message)
        messages.append(LLMMessage(role="user", content=user_content))

        # NOT saved: a summary of the last few runs, prepended only for the LLM calls below, so
        # it never gets embedded into this run's own transcript (which would otherwise compound
        # in size every run once a later run reads this one back).
        history_context = render_history_context(load_recent_transcripts(self.transcripts_dir, n=3))
        history_message = LLMMessage(role="system", content=history_context) if history_context else None

        max_iterations = config.get("agent", {}).get("max_tool_iterations", 20)
        tool_schemas = registry.to_schemas()

        final_text = ""
        error: str | None = None
        try:
            for _iteration in range(max_iterations):
                llm_input = ([history_message] if history_message else []) + messages
                response = llm.chat(llm_input, tool_schemas)
                messages.append(response)

                if not response.tool_calls:
                    final_text = response.content
                    break

                for tool_call in response.tool_calls:
                    logger.info("Tool call: %s(%s)", tool_call.name, tool_call.arguments)
                    result = registry.call(tool_call.name, tool_call.arguments)
                    messages.append(
                        LLMMessage(role="tool", content=result, tool_call_id=tool_call.id, name=tool_call.name)
                    )
            else:
                logger.warning("Agent loop hit max_tool_iterations (%d) without a final message", max_iterations)
        except Exception as exc:
            logger.exception("Agent loop run failed")
            error = str(exc)

        script = load_script(self.script_path)
        save_transcript(self.transcripts_dir, messages, final_text, script, error=error)

        self._archive_inbox(inbox_items)
        return self._write_state(inbox_items, final_text, error)

    def _build_plugin_context_message(self, registry: ToolRegistry) -> LLMMessage | None:
        blocks = []
        for tool in registry.all():
            if not tool.context:
                continue
            result = registry.call(tool.name, tool.context_args)
            blocks.append(f"### {tool.name}\n{result}")

        if not blocks:
            return None
        content = "Automatisch geladener Kontext (kein Tool-Aufruf nötig):\n\n" + "\n\n".join(blocks)
        return LLMMessage(role="system", content=content)

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

    #  datetime.strftime("%A") depends on the system locale, which usually isn't set to German
    #  on a fresh Raspberry Pi OS - spelling this out explicitly keeps the weekday name German
    #  regardless of locale.
    _GERMAN_WEEKDAYS = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]

    @classmethod
    def _now_description(cls) -> str:
        now = datetime.now()
        weekday = cls._GERMAN_WEEKDAYS[now.weekday()]
        return f"{weekday}, {now.strftime('%d.%m.%Y')}, {now.strftime('%H:%M')} Uhr"

    @classmethod
    def _build_user_message(cls, inbox_items: list[tuple[Path, str]], min_program_minutes: int) -> str:
        # Stated here rather than only in system_prompt.md: the prompt is user-editable, but the
        # length requirement is what keeps the station from going silent between runs.
        length = (
            f"Das Programm muss mindestens {min_program_minutes} Minuten füllen (ca. "
            f"{max(1, round(min_program_minutes / 3.5))} Songs plus Ansagen), damit bis zum nächsten "
            "Durchlauf keine Stille entsteht. Wiederhole keine Songs aus den letzten Durchläufen."
        )
        now = cls._now_description()
        if not inbox_items:
            return (
                f"Aktuelle Zeit: {now}. Es liegen keine neuen Hörerwünsche vor. "
                f"Baue trotzdem ein sinnvolles Programm für diesen Durchlauf. {length}"
            )
        wishes = "\n".join(f"{i + 1}. {text}" for i, (_path, text) in enumerate(inbox_items) if text)
        return f"Aktuelle Zeit: {now}. {length}\nNeue Hörerwünsche:\n{wishes}"

    def _archive_inbox(self, inbox_items: list[tuple[Path, str]]) -> None:
        self.processed_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        for path, _text in inbox_items:
            try:
                shutil.move(str(path), str(self.processed_dir / f"{stamp}_{path.name}"))
            except OSError:
                logger.exception("Could not archive inbox file %s", path)

    def _write_state(self, inbox_items: list[tuple[Path, str]], final_text: str, error: str | None) -> dict:
        script = load_script(self.script_path)
        state = {
            "last_run": datetime.now(timezone.utc).isoformat(),
            "inbox_items_processed": len(inbox_items),
            "final_message": final_text,
            "script": asdict(script) if script else None,
            "error": error,
        }
        self.state_path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
        return state
