"""Loads and persists the configuration: tracked defaults + the user's own overrides.

- `config/config.yaml` (tracked) holds the defaults, `data/config.yaml` (gitignored) only the
  values the user changed. `load_config()` merges the two, `save_config()` writes back just the
  difference - so updates never conflict with web-UI edits, and a changed default reaches
  everyone who never touched that value.
- Prompts work the same way: `config/desks/<desk>.md` is the default, `data/prompts/<desk>.md`
  exists only once the prompt was customized.

Both user files are human-editable, so they're re-read whenever their mtime changes (cheap
enough to call per player segment) rather than cached indefinitely.
"""
from __future__ import annotations

import copy
import os
import threading
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT_DIR / "config"
DATA_DIR = ROOT_DIR / "data"
DEFAULTS_PATH = CONFIG_DIR / "config.yaml"
USER_CONFIG_PATH = DATA_DIR / "config.yaml"
DESKS_DIR = CONFIG_DIR / "desks"
USER_PROMPTS_DIR = DATA_DIR / "prompts"

USER_CONFIG_HEADER = (
    "# Your settings - only the values that differ from config/config.yaml (the defaults).\n"
    "# Written by the web UI/installer; hand edits are fine. Delete a key to go back to its default.\n"
)

load_dotenv(ROOT_DIR / ".env")

# Reentrant: update_config() loads and saves under one lock.
_lock = threading.RLock()
_cache: tuple[tuple, dict[str, Any]] | None = None


def deep_merge(base: Any, override: Any) -> Any:
    """Dicts merge recursively; anything else (lists included) is replaced by `override`."""
    if isinstance(base, dict) and isinstance(override, dict):
        return {**base, **{key: deep_merge(base.get(key), value) for key, value in override.items()}}
    return copy.deepcopy(override)


def config_diff(defaults: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """The part of `config` that differs from `defaults` - the inverse of deep_merge.

    Keys missing from `config` can't be expressed and simply fall back to their default.
    """
    diff: dict[str, Any] = {}
    for key, value in config.items():
        default = defaults.get(key) if isinstance(defaults, dict) else None
        if isinstance(value, dict) and isinstance(default, dict):
            nested = config_diff(default, value)
            if nested:
                diff[key] = nested
        elif key not in defaults or value != default:
            diff[key] = copy.deepcopy(value)
    return diff


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _stamp(path: Path) -> tuple | None:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


def _write_atomic(path: Path, text: str) -> None:
    # A crash mid-write must never leave a truncated settings file behind.
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def load_defaults() -> dict[str, Any]:
    return _read_yaml(DEFAULTS_PATH)


def load_user_config() -> dict[str, Any]:
    return _read_yaml(USER_CONFIG_PATH)


def load_config() -> dict[str, Any]:
    """Defaults merged with the user's overrides. Returns a fresh copy callers may mutate."""
    global _cache
    with _lock:
        key = (DEFAULTS_PATH, _stamp(DEFAULTS_PATH), USER_CONFIG_PATH, _stamp(USER_CONFIG_PATH))
        if _cache is None or _cache[0] != key:
            _cache = (key, deep_merge(load_defaults(), load_user_config()))
        return copy.deepcopy(_cache[1])


def write_user_config(overrides: dict[str, Any]) -> None:
    global _cache
    with _lock:
        body = yaml.safe_dump(overrides, allow_unicode=True, sort_keys=False) if overrides else ""
        _write_atomic(USER_CONFIG_PATH, USER_CONFIG_HEADER + body)
        _cache = None


def save_config(config: dict[str, Any]) -> None:
    """Stores a complete config - only its difference from the defaults ends up on disk."""
    with _lock:
        write_user_config(config_diff(load_defaults(), config))


def update_config(partial: dict[str, Any]) -> dict[str, Any]:
    """Deep-merges `partial` into the current config, saves, and returns the new config."""
    with _lock:
        config = deep_merge(load_config(), partial)
        save_config(config)
        return config


def load_plugin_settings(plugin: str) -> dict[str, Any]:
    """Settings a plugin's install() stored under plugins.settings.<plugin folder> (re-read each call)."""
    return (load_config().get("plugins", {}).get("settings", {}) or {}).get(plugin, {}) or {}


def default_prompt_path(desk: str = "music") -> Path:
    return DESKS_DIR / f"{desk}.md"


def user_prompt_path(desk: str = "music") -> Path:
    return USER_PROMPTS_DIR / f"{desk}.md"


def load_default_prompt(desk: str = "music") -> str:
    return default_prompt_path(desk).read_text(encoding="utf-8")


def is_prompt_customized(desk: str = "music") -> bool:
    return user_prompt_path(desk).exists()


def load_system_prompt(desk: str = "music") -> str:
    """The user's prompt for `desk` if customized, else the default."""
    with _lock:
        path = user_prompt_path(desk)
        return path.read_text(encoding="utf-8") if path.exists() else load_default_prompt(desk)


def save_system_prompt(text: str, desk: str = "music") -> None:
    with _lock:
        # Saving the unchanged default keeps following the default (and its future updates).
        if text == load_default_prompt(desk):
            user_prompt_path(desk).unlink(missing_ok=True)
        else:
            _write_atomic(user_prompt_path(desk), text)


def reset_system_prompt(desk: str = "music") -> str:
    """Drops the customized prompt, returns the default that's active again."""
    with _lock:
        user_prompt_path(desk).unlink(missing_ok=True)
        return load_default_prompt(desk)


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


def next_broadcast_start(config: dict[str, Any], now: datetime | None = None) -> datetime | None:
    """When the station goes on air next (local time); None while on air or always on air."""
    now = now or datetime.now()
    if is_broadcast_time(config, now):
        return None
    start = _parse_hhmm(config.get("schedule", {}).get("start_time", "00:00"))
    candidate = datetime.combine(now.date(), start)
    return candidate if candidate > now else candidate + timedelta(days=1)
