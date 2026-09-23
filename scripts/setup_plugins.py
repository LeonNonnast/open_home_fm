#!/usr/bin/env python3
"""Interactive plugin setup - asks per plugin whether to enable it and runs its install() step.

Called by install.sh, can also be run on its own to (re)configure plugins later:

Usage: .venv/bin/python scripts/setup_plugins.py [<plugin folder> ...]
  without arguments all plugins under ./plugins are offered, otherwise only the named ones
  (e.g. `scripts/setup_plugins.py weather hue`).

Enabled/disabled state goes to plugins.disabled, install() results to plugins.settings.<folder>
in config/config.yaml, secrets set via PluginSetup.set_env to .env. See app/agent/plugin_setup.py
for the install() contract.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402

from app.agent.plugin_loader import _load_module  # noqa: E402
from app.agent.plugin_setup import PluginSetup  # noqa: E402
from app.config import ROOT_DIR, load_config, save_config  # noqa: E402

PLUGINS_DIR = ROOT_DIR / "plugins"
ENV_PATH = ROOT_DIR / ".env"


def update_env_file(updates: dict[str, str]) -> None:
    """Sets KEY=value lines in .env in place, appending keys that aren't there yet."""
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    remaining = dict(updates)
    for i, line in enumerate(lines):
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=", line)
        if match and match.group(1) in remaining:
            lines[i] = f"{match.group(1)}={remaining.pop(match.group(1))}"
    lines += [f"{key}={value}" for key, value in remaining.items()]
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    ENV_PATH.chmod(0o600)


def main(only: list[str]) -> int:
    config = load_config()
    plugins_cfg = config.setdefault("plugins", {})
    disabled = set(plugins_cfg.get("disabled") or [])
    all_settings = plugins_cfg.setdefault("settings", {}) or {}
    plugins_cfg["settings"] = all_settings
    env_updates: dict[str, str] = {}

    plugin_dirs = sorted(
        p for p in PLUGINS_DIR.iterdir()
        if p.is_dir() and (p / "manifest.yaml").exists() and (p / "plugin.py").exists()
    )
    unknown = set(only) - {p.name for p in plugin_dirs}
    if unknown:
        print(f"Unbekannte Plugins: {', '.join(sorted(unknown))}", file=sys.stderr)
        return 2

    for plugin_dir in plugin_dirs:
        if only and plugin_dir.name not in only:
            continue
        manifest = yaml.safe_load((plugin_dir / "manifest.yaml").read_text(encoding="utf-8")) or {}
        name = manifest.get("name", plugin_dir.name)
        enabled_now = manifest.get("enabled", True) and name not in disabled

        print(f"\n--- Plugin '{plugin_dir.name}': {manifest.get('description', '')}")
        setup = PluginSetup(dict(all_settings.get(plugin_dir.name) or {}))
        if not setup.ask_yes_no("Aktivieren?", enabled_now):
            disabled.add(name)
            continue
        disabled.discard(name)

        install = getattr(_load_module(plugin_dir / "plugin.py", f"open_home_fm_setup_{plugin_dir.name}"), "install", None)
        if install is None:
            continue
        try:
            result = install(setup)
        except (KeyboardInterrupt, EOFError):
            raise
        except Exception as exc:
            print(f"    [FEHLER] Einrichtung fehlgeschlagen: {exc}")
            if not setup.ask_yes_no("Plugin trotzdem aktiviert lassen?", False):
                disabled.add(name)
            continue
        if result is not None:
            all_settings[plugin_dir.name] = result
        env_updates.update(setup.env_updates)

    plugins_cfg["disabled"] = sorted(disabled)
    save_config(config)
    if env_updates:
        update_env_file(env_updates)
    print("\nPlugin-Einstellungen gespeichert.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
