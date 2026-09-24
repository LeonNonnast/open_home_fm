from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from app import config as cfg
from app.migrate import migrate_user_data
from tests.conftest import DEFAULT_PROMPT, DEFAULTS

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(config_env: Path) -> Path:
    """config_env as a git checkout with the defaults + legacy prompt committed."""
    (config_env / "config" / "system_prompt.md").write_text(DEFAULT_PROMPT, encoding="utf-8")
    (config_env / ".gitignore").write_text("data/config.yaml\ndata/prompts/\n", encoding="utf-8")
    git(config_env, "init", "-q")
    git(config_env, "add", "-A")
    git(config_env, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "defaults")
    return config_env


def test_modified_tracked_files_move_to_user_files(repo: Path):
    # What the old web UI left behind: a full dump (no comments) with a few changed values.
    local = cfg.deep_merge(DEFAULTS, {"music": {"provider": "spotify"}, "plugins": {"disabled": []}})
    (repo / "config" / "config.yaml").write_text(yaml.safe_dump(local), encoding="utf-8")
    (repo / "config" / "system_prompt.md").write_text("Mein Prompt", encoding="utf-8")
    (repo / "config" / "system_prompt.md.neu").write_text(DEFAULT_PROMPT, encoding="utf-8")

    moved = migrate_user_data(repo)

    assert moved == ["config/config.yaml", "config/system_prompt.md"]
    assert yaml.safe_load(cfg.USER_CONFIG_PATH.read_text(encoding="utf-8")) == {
        "music": {"provider": "spotify"},
        "plugins": {"disabled": []},
    }
    assert cfg.load_config() == local
    assert cfg.load_system_prompt() == "Mein Prompt"
    assert git(repo, "status", "--porcelain") == ""
    assert not (repo / "config" / "system_prompt.md.neu").exists()


def test_migration_is_idempotent(repo: Path):
    (repo / "config" / "config.yaml").write_text(
        yaml.safe_dump(cfg.deep_merge(DEFAULTS, {"schedule": {"enabled": True}})), encoding="utf-8"
    )
    assert migrate_user_data(repo) == ["config/config.yaml"]
    before = cfg.USER_CONFIG_PATH.read_text(encoding="utf-8")
    assert migrate_user_data(repo) == []
    assert cfg.USER_CONFIG_PATH.read_text(encoding="utf-8") == before


def test_tracked_edit_merges_into_existing_user_file(repo: Path):
    cfg.update_config({"music": {"provider": "spotify"}, "schedule": {"enabled": True}})
    (repo / "config" / "config.yaml").write_text(
        yaml.safe_dump(cfg.deep_merge(DEFAULTS, {"schedule": {"enabled": False, "start_time": "08:00"}})),
        encoding="utf-8",
    )
    migrate_user_data(repo)
    loaded = cfg.load_config()
    assert loaded["music"]["provider"] == "spotify"
    # Tracked edit is the newer one; its value equal to the default doesn't count as an edit.
    assert loaded["schedule"] == {"enabled": True, "start_time": "08:00", "end_time": "23:00"}


def test_unmodified_or_broken_files_are_left_alone(repo: Path):
    assert migrate_user_data(repo) == []
    assert not cfg.USER_CONFIG_PATH.exists()

    broken = "<<<<<<< HEAD\nagent: {\n"
    (repo / "config" / "config.yaml").write_text(broken, encoding="utf-8")
    assert migrate_user_data(repo) == []
    assert (repo / "config" / "config.yaml").read_text(encoding="utf-8") == broken


def test_no_git_checkout_is_a_noop(config_env: Path):
    (config_env / "config" / "system_prompt.md").write_text("irgendwas", encoding="utf-8")
    assert migrate_user_data(config_env) == []
