"""Interactive install step for plugins, run by the installer (scripts/setup_plugins.py).

A plugin can optionally define, next to `execute`:

    def install(setup: PluginSetup) -> dict:
        location = setup.ask("Ort", setup.settings.get("location", "Berlin"))
        setup.set_env("MY_PLUGIN_TOKEN", setup.ask_secret("Token", "MY_PLUGIN_TOKEN"))
        return {"location": location}

The returned dict is stored in config.yaml under `plugins.settings.<plugin folder>` and read back
at runtime with `app.config.load_plugin_settings("<plugin folder>")`. Secrets don't belong there
(config.yaml is served by the web UI API) - put them in .env via `set_env`, read them back with
`os.environ`. Raising an exception aborts the plugin's setup; the installer then offers to
disable the plugin.
"""
from __future__ import annotations

import getpass
import os
from typing import Any


class PluginSetup:
    def __init__(self, settings: dict[str, Any]):
        # Settings stored by a previous run - use them as defaults so re-running the installer
        # only needs Enter presses.
        self.settings = settings
        self.env_updates: dict[str, str] = {}

    def note(self, text: str) -> None:
        print(f"    {text}")

    def ask(self, prompt: str, default: str = "") -> str:
        suffix = f" [{default}]" if default else ""
        answer = input(f"{prompt}{suffix}: ").strip()
        return answer or default

    def ask_yes_no(self, prompt: str, default: bool = True) -> bool:
        answer = input(f"{prompt} [{'y' if default else 'n'}]: ").strip().lower()
        return default if not answer else answer.startswith(("y", "j"))

    def ask_choice(self, prompt: str, options: list[str], default: int = 0) -> int:
        """Numbered list, returns the chosen index."""
        for i, option in enumerate(options, 1):
            print(f"  {i}) {option}")
        while True:
            answer = self.ask(prompt, str(default + 1))
            if answer.isdigit() and 1 <= int(answer) <= len(options):
                return int(answer) - 1
            self.note(f"Bitte eine Zahl von 1 bis {len(options)} eingeben.")

    def ask_secret(self, prompt: str, env_key: str) -> str:
        """Hidden input; Enter keeps the value currently in .env under `env_key`."""
        existing = self.env(env_key)
        if existing:
            prompt += " [Enter = bisherigen Wert behalten]"
        return getpass.getpass(f"{prompt}: ").strip() or existing

    def env(self, key: str) -> str:
        return self.env_updates.get(key, os.environ.get(key, ""))

    def set_env(self, key: str, value: str) -> None:
        self.env_updates[key] = value
