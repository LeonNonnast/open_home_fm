"""Loads and persists config/config.yaml + config/system_prompt.md.

Both files are meant to be human-editable (by hand or through the web UI), so this module
re-reads them from disk on every access rather than caching indefinitely - the agent loop and
the config API must always see the latest version.
"""
from __future__ import annotations

import threading
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT_DIR / "config"
CONFIG_PATH = CONFIG_DIR / "config.yaml"
SYSTEM_PROMPT_PATH = CONFIG_DIR / "system_prompt.md"

load_dotenv(ROOT_DIR / ".env")

_lock = threading.Lock()


def load_config() -> dict[str, Any]:
    with _lock:
        with CONFIG_PATH.open("r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}


def save_config(config: dict[str, Any]) -> None:
    with _lock:
        with CONFIG_PATH.open("w", encoding="utf-8") as f:
            yaml.safe_dump(config, f, allow_unicode=True, sort_keys=False)


def load_system_prompt() -> str:
    with _lock:
        return SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")


def save_system_prompt(text: str) -> None:
    with _lock:
        SYSTEM_PROMPT_PATH.write_text(text, encoding="utf-8")


def resolve_path(relative: str) -> Path:
    """Resolve a config value that may be relative to the project root."""
    path = Path(relative)
    return path if path.is_absolute() else ROOT_DIR / path


def _parse_hhmm(value: str) -> dtime:
    return datetime.strptime(value, "%H:%M").time()


def is_broadcast_time(config: dict[str, Any], now: datetime | None = None) -> bool:
    """Whether the station should currently be generating/playing a program.

    `schedule.enabled: false` (the default) means "always on air". When enabled, `start_time`/
    `end_time` ("HH:MM") define a daily window; an end time earlier than the start time is
    treated as spanning midnight (e.g. 22:00-06:00).
    """
    schedule = config.get("schedule", {})
    if not schedule.get("enabled", False):
        return True

    current = (now or datetime.now()).time()
    start = _parse_hhmm(schedule.get("start_time", "00:00"))
    end = _parse_hhmm(schedule.get("end_time", "23:59"))

    if start <= end:
        return start <= current <= end
    return current >= start or current <= end
