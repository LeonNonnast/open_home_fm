"""Moves settings out of tracked files into the gitignored user files (idempotent).

Up to v0.1 the web UI wrote straight into the tracked `config/config.yaml` and
`config/system_prompt.md`. If either is locally modified, its user values move to
`data/config.yaml` (difference to the committed defaults) / `data/prompts/music.md`, then the
tracked file is restored - so `git pull` works again. Runs from `install.sh` (update and full
setup) and at app startup, for installations updated with a plain `git pull`.

`migrate_agent_settings()` (run once at app start) moves the pre-desk `agent.*` settings to
`desks.music.*` and removes the runtime files the queue replaced. `migrate_inbox()` turns
unprocessed wishes from `data/inbox/*.txt` into calls for the dispatch desk.

Run standalone with: .venv/bin/python -m app.migrate
"""
from __future__ import annotations

import logging
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import yaml

from app import config as cfg

logger = logging.getLogger(__name__)

LEGACY_CONFIG = "config/config.yaml"
LEGACY_PROMPT = "config/system_prompt.md"
# Left behind by the old quick update as "the new default prompt, for comparison".
LEGACY_PROMPT_COPY = "config/system_prompt.md.neu"
CONFLICT_MARKERS = ("<<<<<<< ", ">>>>>>> ")


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=False)


def _modified_vs_head(root: Path, relpath: str) -> str | None:
    """The committed version of `relpath` if the working copy differs from it, else None."""
    try:
        diff = _git(root, "diff", "--quiet", "HEAD", "--", relpath)
    except FileNotFoundError:  # no git at all - nothing to compare against
        return None
    if diff.returncode != 1 or not (root / relpath).exists():
        return None
    head = _git(root, "show", f"HEAD:{relpath}")
    return head.stdout if head.returncode == 0 else None


def _restore(root: Path, relpath: str) -> None:
    result = _git(root, "checkout", "HEAD", "--", relpath)
    if result.returncode != 0:
        logger.warning("Could not restore %s: %s", relpath, result.stderr.strip())


def migrate_config(root: Path) -> bool:
    head_text = _modified_vs_head(root, LEGACY_CONFIG)
    if head_text is None:
        return False
    local_text = (root / LEGACY_CONFIG).read_text(encoding="utf-8")
    try:
        defaults = yaml.safe_load(head_text) or {}
        local = yaml.safe_load(local_text) or {}
    except yaml.YAMLError:
        logger.warning("%s is modified but not valid YAML (merge conflict?) - left untouched", LEGACY_CONFIG)
        return False
    if not isinstance(local, dict):
        logger.warning("%s is modified but not a mapping - left untouched", LEGACY_CONFIG)
        return False
    # Values edited in the tracked file win over an existing user file: they're the newer
    # change (e.g. a hand edit out of old habit after an earlier migration).
    overrides = cfg.deep_merge(cfg.load_user_config(), cfg.config_diff(defaults, local))
    cfg.write_user_config(overrides)
    _restore(root, LEGACY_CONFIG)
    logger.info("Moved local settings from %s to %s", LEGACY_CONFIG, cfg.USER_CONFIG_PATH)
    return True


def migrate_prompt(root: Path) -> bool:
    if _modified_vs_head(root, LEGACY_PROMPT) is None:
        return False
    text = (root / LEGACY_PROMPT).read_text(encoding="utf-8")
    if any(marker in text for marker in CONFLICT_MARKERS):
        logger.warning("%s contains merge conflict markers - left untouched", LEGACY_PROMPT)
        return False
    cfg.save_system_prompt(text, desk="music")
    _restore(root, LEGACY_PROMPT)
    logger.info("Moved customized %s to %s", LEGACY_PROMPT, cfg.user_prompt_path("music"))
    return True


def migrate_user_data(root: Path | None = None) -> list[str]:
    """Runs all migrations, returns the tracked files that were moved into user files."""
    root = root or cfg.ROOT_DIR
    moved = []
    if migrate_config(root):
        moved.append(LEGACY_CONFIG)
    if migrate_prompt(root):
        moved.append(LEGACY_PROMPT)
    (root / LEGACY_PROMPT_COPY).unlink(missing_ok=True)
    return moved


# agent.<key> -> desks.music.<key>; agent.loop_interval_seconds is gone (fill level instead).
AGENT_KEYS_TO_MUSIC_DESK = ("max_tool_iterations", "no_repeat_minutes")
LEGACY_SCRIPT = "data/playlists/current_script.json"


def migrate_agent_settings(root: Path | None = None) -> list[str]:
    """Moves `agent.*` in the user config to `desks.music.*` (idempotent). Returns what changed."""
    root = root or cfg.ROOT_DIR
    changes = []
    with cfg._lock:
        overrides = cfg.load_user_config()
        agent = overrides.get("agent")
        if isinstance(agent, dict):
            music = overrides.setdefault("desks", {}).setdefault("music", {})
            defaults = cfg.load_defaults().get("desks", {}).get("music", {})
            for key in AGENT_KEYS_TO_MUSIC_DESK:
                if key in agent:
                    value = agent.pop(key)
                    # A value already set on the desk is the newer one; the default isn't a
                    # user setting (pre-split installs dumped the whole config).
                    if key not in music and value != defaults.get(key):
                        music[key] = value
                    changes.append(f"agent.{key} -> desks.music.{key}")
            if agent.pop("loop_interval_seconds", None) is not None:
                changes.append("agent.loop_interval_seconds entfernt")
            if not agent:
                overrides.pop("agent")
            if not music:
                overrides["desks"].pop("music")
            if not overrides["desks"]:
                overrides.pop("desks")
            if changes:
                cfg.write_user_config(overrides)
                logger.info("Migrated agent settings: %s", ", ".join(changes))
    # Replaced by data/queue.json; its content is stale after the update anyway.
    legacy_script = root / LEGACY_SCRIPT
    if legacy_script.exists():
        legacy_script.unlink()
        changes.append(f"{LEGACY_SCRIPT} gelöscht")
    return changes


LEGACY_INBOX = "data/inbox"
LEGACY_PROCESSED = "data/processed"


def migrate_inbox(calls, root: Path | None = None) -> int:
    """Unprocessed wishes (`data/inbox/*.txt`) become calls with status `new` - the dispatch desk
    works them off. The files (and their recordings) move to data/processed. Idempotent."""
    root = root or cfg.ROOT_DIR
    inbox = root / LEGACY_INBOX
    if not inbox.exists():
        return 0
    processed = root / LEGACY_PROCESSED
    count = 0
    for path in sorted(inbox.glob("*.txt")):
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            logger.warning("Could not read inbox file %s", path, exc_info=True)
            continue
        try:
            created = datetime.strptime(path.stem[:15], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
        except ValueError:
            created = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        recordings = [p for p in inbox.glob(f"{path.stem}.*") if p.suffix != ".txt"]
        if text:
            calls.create(text, source="voice" if recordings else "text", created_at=created)
            count += 1
        processed.mkdir(parents=True, exist_ok=True)
        for moved in (path, *recordings):
            shutil.move(str(moved), str(processed / f"migrated_{moved.name}"))
    if count:
        logger.info("Migrated %d inbox wish(es) to calls", count)
    return count


# Tools a customized prompt may still name although they were renamed (still work as aliases).
OUTDATED_TOOL_NAMES = {"set_playback_script": "append_program_block"}


def outdated_prompt_notice(desk: str = "music") -> dict | None:
    """A UI notice when the owner's customized prompt for `desk` still names an old tool."""
    path = cfg.user_prompt_path(desk)
    try:
        text = path.read_text(encoding="utf-8") if path.exists() else ""
    except OSError:
        return None
    old = [name for name in OUTDATED_TOOL_NAMES if name in text]
    if not old:
        return None
    renamed = ", ".join(f"{name} → {OUTDATED_TOOL_NAMES[name]}" for name in old)
    return {
        "id": f"prompt-veraltet-{desk}",
        "text": f"Dein angepasster Prompt nennt ein umbenanntes Werkzeug ({renamed}). Es funktioniert "
                "vorerst weiter; besser „Prompt auf Standard zurücksetzen“ und eigene Änderungen neu eintragen.",
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    for path in migrate_user_data():
        print(f"{path}: lokale Einstellungen nach data/ übernommen, Datei auf den Repo-Stand zurückgesetzt.")
    for change in migrate_agent_settings():
        print(change)
