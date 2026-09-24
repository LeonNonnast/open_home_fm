from __future__ import annotations

import yaml
from fastapi import APIRouter
from pydantic import BaseModel

from app.config import ROOT_DIR, load_config, save_config

router = APIRouter(prefix="/api/plugins", tags=["plugins"])

PLUGINS_DIR = ROOT_DIR / "plugins"


class ToggleBody(BaseModel):
    enabled: bool


@router.get("")
def list_plugins() -> dict:
    config = load_config()
    disabled = set(config.get("plugins", {}).get("disabled", []))

    plugins = []
    if PLUGINS_DIR.exists():
        for plugin_dir in sorted(p for p in PLUGINS_DIR.iterdir() if p.is_dir()):
            manifest_path = plugin_dir / "manifest.yaml"
            if not manifest_path.exists():
                continue
            manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
            name = manifest.get("name", plugin_dir.name)
            plugins.append(
                {
                    "folder": plugin_dir.name,
                    "name": name,
                    "description": manifest.get("description", ""),
                    "enabled": manifest.get("enabled", True) and name not in disabled,
                    "context": bool(manifest.get("context", False)),
                    "action": bool(manifest.get("action", False)),
                }
            )
    return {"plugins": plugins}


@router.post("/{name}/toggle")
def toggle_plugin(name: str, body: ToggleBody) -> dict:
    config = load_config()
    plugins_cfg = config.setdefault("plugins", {})
    disabled = set(plugins_cfg.get("disabled", []))

    if body.enabled:
        disabled.discard(name)
    else:
        disabled.add(name)

    plugins_cfg["disabled"] = sorted(disabled)
    save_config(config)
    return {"status": "ok"}
