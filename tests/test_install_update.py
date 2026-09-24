"""Simulates `./install.sh --update` on a Pi: a clone of a throwaway upstream, fake .venv (the
test interpreter, stub pip) and stubbed systemctl/sudo - the real service is never touched."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent

pytestmark = [
    pytest.mark.shell,
    pytest.mark.skipif(shutil.which("git") is None or shutil.which("bash") is None, reason="needs git + bash"),
    pytest.mark.skipif(not (REPO / ".git").exists(), reason="needs a git checkout of the project"),
]


def run(cmd: list[str], cwd: Path, env: dict | None = None) -> str:
    result = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)
    assert result.returncode == 0, f"{cmd} failed:\n{result.stdout}\n{result.stderr}"
    return result.stdout


def git(cwd: Path, *args: str) -> str:
    return run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd)


def old_install_revision() -> str | None:
    """The last committed version whose install.sh still used the stash/merge quick update."""
    commits = run(["git", "log", "--format=%H", "-S", "prompt_backup", "--", "install.sh"], REPO).split()
    for commit in commits:
        for rev in (commit, f"{commit}^"):
            show = subprocess.run(["git", "show", f"{rev}:install.sh"], cwd=REPO, capture_output=True, text=True)
            if show.returncode == 0 and "prompt_backup" in show.stdout:
                return rev
    return None


def copy_worktree(dest: Path) -> None:
    """The current code (tracked + new, unignored files) - what upstream ships next."""
    files = run(["git", "ls-files", "-co", "--exclude-standard", "-z"], REPO).split("\0")
    for rel in filter(None, files):
        src = REPO / rel
        if src.is_file():
            (dest / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest / rel)


def replace_tree(repo: Path, fill) -> None:
    for entry in repo.iterdir():
        if entry.name != ".git":
            shutil.rmtree(entry) if entry.is_dir() else entry.unlink()
    fill(repo)


def make_venv_and_stubs(pi: Path, stubs: Path) -> dict:
    bin_dir = pi / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "python3").write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    (bin_dir / "pip").write_text("#!/bin/sh\nexit 0\n")
    stubs.mkdir()
    (stubs / "systemctl").write_text("#!/bin/sh\nexit 1\n")
    (stubs / "sudo").write_text("#!/bin/sh\nexit 1\n")
    for f in [*bin_dir.iterdir(), *stubs.iterdir()]:
        f.chmod(0o755)
    (pi / ".env").write_text("OLLAMA_API_KEY=\n")
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["PATH"] = f"{stubs}{os.pathsep}{env['PATH']}"
    env["TERM"] = "dumb"
    return env


def update(pi: Path, env: dict) -> str:
    return run(["bash", "./install.sh", "--update"], pi, env)


def merged_config(pi: Path, env: dict) -> dict:
    out = run([str(pi / ".venv" / "bin" / "python3"), "-c",
               "import json; from app.config import load_config; print(json.dumps(load_config()))"], pi, env)
    return yaml.safe_load(out)


def push_upstream_change(seed: Path, change) -> None:
    path = seed / "config" / "config.yaml"
    defaults = yaml.safe_load(path.read_text(encoding="utf-8"))
    change(defaults)
    path.write_text(yaml.safe_dump(defaults, allow_unicode=True, sort_keys=False), encoding="utf-8")
    git(seed, "commit", "-qam", "upstream change")
    git(seed, "push", "-q", "origin", "HEAD:main")


def setup_pi(tmp_path: Path, fill_initial) -> tuple[Path, Path, dict]:
    upstream = tmp_path / "upstream.git"
    seed = tmp_path / "seed"
    pi = tmp_path / "pi"
    run(["git", "init", "-q", "--bare", "-b", "main", str(upstream)], tmp_path)
    run(["git", "init", "-q", "-b", "main", str(seed)], tmp_path)
    fill_initial(seed)
    git(seed, "add", "-A")
    git(seed, "commit", "-qm", "initial")
    git(seed, "remote", "add", "origin", str(upstream))
    git(seed, "push", "-q", "origin", "main")
    run(["git", "clone", "-q", str(upstream), str(pi)], tmp_path)
    env = make_venv_and_stubs(pi, tmp_path / "stubs")
    return seed, pi, env


def web_ui_dump(pi: Path) -> None:
    """What the pre-split web UI left behind: the whole config re-dumped, a few values changed."""
    path = pi / "config" / "config.yaml"
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    config["music"]["provider"] = "spotify"
    config["schedule"]["enabled"] = True
    config["plugins"]["disabled"] = []
    path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")
    (pi / "config" / "system_prompt.md").write_text("Mein eigener Prompt\n", encoding="utf-8")


def assert_user_values(pi: Path, env: dict) -> dict:
    config = merged_config(pi, env)
    assert config["music"]["provider"] == "spotify"
    assert config["schedule"]["enabled"] is True
    assert config["plugins"]["disabled"] == []
    assert (pi / "data" / "prompts" / "music.md").read_text(encoding="utf-8") == "Mein eigener Prompt\n"
    assert git(pi, "status", "--porcelain") == ""
    return config


def test_update_from_pre_split_version(tmp_path: Path):
    old_rev = old_install_revision()
    if old_rev is None:
        pytest.skip("no pre-split install.sh in the git history")

    def old_tree(dest: Path) -> None:
        archive = subprocess.run(["git", "archive", old_rev], cwd=REPO, capture_output=True, check=True)
        subprocess.run(["tar", "-x", "-C", str(dest)], input=archive.stdout, check=True)

    seed, pi, env = setup_pi(tmp_path, old_tree)
    web_ui_dump(pi)

    # Upstream ships the current code; the Pi still runs the old quick_update, which pulls and
    # hands over to the new script's --finish-update.
    replace_tree(seed, copy_worktree)
    git(seed, "add", "-A")
    git(seed, "commit", "-qm", "split user data")
    git(seed, "push", "-q", "origin", "main")
    update(pi, env)

    user_file = yaml.safe_load((pi / "data" / "config.yaml").read_text(encoding="utf-8"))
    assert user_file == {"schedule": {"enabled": True}, "music": {"provider": "spotify"}, "plugins": {"disabled": []}}
    assert_user_values(pi, env)
    assert not (pi / "config" / "system_prompt.md.neu").exists()

    # Next update with the new script on both sides: new defaults arrive, user values stay.
    push_upstream_change(seed, lambda d: d["llm"]["anthropic"].update(model="claude-next"))
    update(pi, env)
    assert assert_user_values(pi, env)["llm"]["anthropic"]["model"] == "claude-next"


def test_update_with_hand_edited_tracked_config(tmp_path: Path):
    seed, pi, env = setup_pi(tmp_path, copy_worktree)
    web_ui_dump(pi)  # hand edits of the tracked files, after the split
    push_upstream_change(seed, lambda d: d["audio"].update(output_device="hw:9"))

    update(pi, env)
    config = assert_user_values(pi, env)
    assert config["audio"]["output_device"] == "hw:9"

    # Idempotent: nothing new upstream, nothing to migrate.
    before = (pi / "data" / "config.yaml").read_text(encoding="utf-8")
    update(pi, env)
    assert (pi / "data" / "config.yaml").read_text(encoding="utf-8") == before
