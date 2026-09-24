from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from app.agent.play_history import recently_played, record_played


def test_record_and_window(tmp_path):
    path = tmp_path / "history.json"
    old = (datetime.now(timezone.utc) - timedelta(minutes=90)).isoformat()
    path.write_text(json.dumps([{"uri": "u:old", "title": "Alt", "played_at": old}]), encoding="utf-8")
    record_played(path, "u:new", "Neu")

    assert [e["uri"] for e in recently_played(path, 120)] == ["u:old", "u:new"]
    assert [e["uri"] for e in recently_played(path, 60)] == ["u:new"]


def test_entries_older_than_a_day_are_pruned_on_write(tmp_path):
    path = tmp_path / "history.json"
    ancient = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    path.write_text(json.dumps([{"uri": "u:ancient", "title": "Uralt", "played_at": ancient}]), encoding="utf-8")
    record_played(path, "u:new", "Neu")
    assert [e["uri"] for e in json.loads(path.read_text(encoding="utf-8"))] == ["u:new"]


def test_missing_or_corrupt_file(tmp_path):
    path = tmp_path / "history.json"
    assert recently_played(path, 120) == []
    path.write_text("{kaputt", encoding="utf-8")
    assert recently_played(path, 120) == []
    record_played(path, "u:new", "Neu")
    assert len(recently_played(path, 120)) == 1
