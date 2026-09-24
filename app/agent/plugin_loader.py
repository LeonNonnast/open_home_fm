"""Discovers plugins under ./plugins and registers them as Tools.

Plugin contract (kept intentionally minimal so a plugin author only needs two small files):

    plugins/<name>/manifest.yaml
        name: get_weather          # tool name exposed to the LLM
        description: "..."         # tool description exposed to the LLM
        parameters:                 # JSON Schema for the tool arguments
          type: object
          properties:
            location: {type: string}
          required: [location]
        enabled: true               # optional, defaults to true
        context: false               # optional, defaults to false - see below
        context_args: {}             # optional, arguments used when context: true
        action: false                # optional: true = changes something (lights) instead of
                                     # looking something up - see below

    plugins/<name>/plugin.py
        def execute(**kwargs) -> str:
            ...

`execute` receives exactly the arguments the LLM supplied (validated against the manifest's
JSON Schema is *not* enforced here - keep plugins defensive) and must return a string that is
fed back to the LLM as the tool result.

`context: true` marks a plugin as always-relevant: instead of waiting for the agent to decide to
call it, a run calls `execute(**context_args)` once at its start and injects the result directly
into the input, so the information is available without a tool call. It remains callable as a
normal tool too (e.g. to ask about a different city than the default). Which plugins a desk
uses - and which of them as context - is decided per desk (`desks.<name>.plugins` /
`context_plugins`, see app/agent/desk.py); the manifest flag is informational for the web UI.

`action: true` marks a direct action (e.g. switching lights): the dispatch desk shows each call
of it as an action of the listener's call and never caches its result. Plugins without it are
lookups (weather, news); the dispatch desk caches their results for 10 minutes.

Optionally `plugin.py` also defines `install(setup) -> dict`, an interactive setup step the
installer runs (see app/agent/plugin_setup.py) - the loader itself ignores it.
"""
from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
from types import ModuleType
from typing import Any

import yaml

from app.agent.tools import Tool, ToolRegistry

logger = logging.getLogger(__name__)


def _load_module(path: Path, module_name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load plugin module at {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def discover_plugins(plugins_dir: Path, disabled: list[str] | None = None) -> list[Tool]:
    disabled = set(disabled or [])
    tools: list[Tool] = []

    if not plugins_dir.exists():
        return tools

    for plugin_dir in sorted(p for p in plugins_dir.iterdir() if p.is_dir()):
        manifest_path = plugin_dir / "manifest.yaml"
        plugin_path = plugin_dir / "plugin.py"
        if not manifest_path.exists() or not plugin_path.exists():
            continue

        try:
            manifest: dict[str, Any] = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
            name = manifest["name"]

            if name in disabled or not manifest.get("enabled", True):
                logger.info("Plugin '%s' is disabled, skipping", name)
                continue

            module = _load_module(plugin_path, f"open_home_fm_plugin_{plugin_dir.name}")
            execute = getattr(module, "execute", None)
            if execute is None:
                logger.error("Plugin '%s' has no execute() function, skipping", plugin_dir.name)
                continue

            tools.append(
                Tool(
                    name=name,
                    description=manifest.get("description", ""),
                    parameters=manifest.get("parameters", {"type": "object", "properties": {}}),
                    func=execute,
                    source="plugin",
                    context=manifest.get("context", False),
                    context_args=manifest.get("context_args", {}) or {},
                    action=bool(manifest.get("action", False)),
                )
            )
            logger.info("Loaded plugin tool '%s' from %s", name, plugin_dir.name)
        except Exception:
            logger.exception("Failed to load plugin at %s", plugin_dir)

    return tools


def register_plugins(registry: ToolRegistry, plugins_dir: Path, disabled: list[str] | None = None) -> None:
    for tool in discover_plugins(plugins_dir, disabled):
        registry.register(tool)
