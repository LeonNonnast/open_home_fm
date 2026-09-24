from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app import config as cfg
from app.agent.desk import DeskRunner
from app.program.queue import ProgramQueue, QueueItem, Segment
from app.scheduler import DeskScheduler


class BlockingRunner(DeskRunner):
    """DeskRunner whose music run waits for `release` and optionally appends a block."""

    def __init__(self, root: Path):
        super().__init__(root, ProgramQueue(root / "data" / "queue.json", root / "data" / "cursor.json"))
        self.release = threading.Event()
        self.started = threading.Event()
        self.runs: list[str] = []
        self.append_minutes = 0
        self.error: str | None = None

    def run(self, name: str, trigger: str = "manual") -> dict:
        self.runs.append(trigger)
        self.started.set()
        assert self.release.wait(5)
        if self.append_minutes:
            self.queue.append(QueueItem.new("program", "music", [
                Segment("track", f"t{i}", f"u:{trigger}{i}", duration_seconds=60) for i in range(self.append_minutes)
            ]))
        return {"desk": name, "trigger": trigger, "error": self.error, "final_message": "ok"}


@pytest.fixture
def runner(config_env: Path) -> BlockingRunner:
    return BlockingRunner(config_env)


def test_fill_trigger_fires_once_and_marks_one_followup(runner):
    scheduler = DeskScheduler(runner)
    scheduler.watch()
    assert runner.started.wait(2)
    scheduler.watch()
    scheduler.watch()
    assert runner.status("music")["followup_pending"] and runner.status("music")["state"] == "running"

    runner.append_minutes = 20  # the first run fills the program ...
    runner.release.set()
    assert runner.wait_idle("music", 5)
    # ... so the follow-up re-checks the fill level and doesn't run.
    assert runner.runs == ["fill"]
    assert not runner.status("music")["followup_pending"]


def test_followup_runs_when_still_needed(runner):
    scheduler = DeskScheduler(runner)
    scheduler.watch()
    assert runner.started.wait(2)
    assert scheduler.request_run("music", "manual", force=True)["status"] == "queued"
    runner.release.set()
    assert runner.wait_idle("music", 5)
    assert runner.runs == ["fill", "manual"]


def test_watch_does_nothing_above_threshold(runner):
    runner.queue.append(QueueItem.new("program", "music", [Segment("track", "t", "u", duration_seconds=900)]))
    DeskScheduler(runner).watch()
    assert runner.runs == []


def test_cap_blocks_manual_runs(runner):
    runner.queue.append(QueueItem.new("program", "music", [Segment("track", "t", "u", duration_seconds=46 * 60)]))
    result = DeskScheduler(runner).request_run("music", "manual", force=True)
    assert result["status"] == "skipped" and "Obergrenze 45" in result["reason"]


def test_broadcast_start_triggers_a_run(runner, monkeypatch):
    import app.scheduler as scheduler_module

    on_air = [False]
    monkeypatch.setattr(scheduler_module, "is_broadcast_time", lambda config: on_air[0])
    scheduler = DeskScheduler(runner)
    scheduler.watch()
    assert runner.runs == []
    on_air[0] = True
    runner.release.set()
    scheduler.watch()
    assert runner.wait_idle("music", 5)
    assert runner.runs == ["broadcast_start"]


def test_backoff_after_failures(runner):
    runner.release.set()
    runner.error = "Ollama 500"
    delays = []
    for _ in range(5):
        runner._execute("music", "fill")
        until = datetime.fromisoformat(runner.status("music")["backoff_until"])
        delays.append(round((until - datetime.now(timezone.utc)).total_seconds() / 60))
    assert delays == [1, 2, 5, 10, 10]
    status = runner.status("music")
    assert status["state"] == "error" and status["consecutive_failures"] == 5 and status["last_error"] == "Ollama 500"

    assert runner.request("music", "fill") == "backoff"
    runner.error = None
    assert runner.request("music", "manual", force=True) == "started"
    assert runner.wait_idle("music", 5)
    status = runner.status("music")
    assert status["consecutive_failures"] == 0 and status["backoff_until"] is None and status["last_success_at"]


def test_disabled_desk_is_not_triggered(runner):
    cfg.update_config({"desks": {"music": {"enabled": False}}})
    assert runner.request("music", "fill") == "disabled"
