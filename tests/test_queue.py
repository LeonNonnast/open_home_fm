from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.program.queue import TRACK_ESTIMATE_SECONDS, ProgramQueue, QueueItem, Segment


def _queue(tmp_path: Path) -> ProgramQueue:
    return ProgramQueue(tmp_path / "queue.json", tmp_path / "cursor.json")


def _track(title: str, duration: float | None = 200) -> Segment:
    return Segment("track", title, f"u:{title}", duration_seconds=duration)


def _item(lane: str, *segments: Segment, minutes_ago: float = 0, **kwargs) -> QueueItem:
    now = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return QueueItem.new(lane, "test", list(segments), now=now, **kwargs)


def test_lane_priority_then_fifo(tmp_path):
    q = _queue(tmp_path)
    p1 = q.append(_item("program", _track("p1"), minutes_ago=5))
    q.append(_item("program", _track("p2"), minutes_ago=1))
    f = q.append(_item("filler", _track("f"), minutes_ago=10))
    assert q.next_item().id == p1.id
    r = q.append(_item("reply", _track("r")))
    assert q.next_item().id == r.id
    q.finish_item(r.id)
    assert q.next_item().id == p1.id
    for item in q.items():
        if item.lane == "program":
            q.finish_item(item.id)
    assert q.next_item().id == f.id


def test_started_item_goes_first_in_its_lane(tmp_path):
    q = _queue(tmp_path)
    q.append(_item("program", _track("a"), minutes_ago=5))
    late = q.append(_item("program", _track("b1"), _track("b2"), minutes_ago=1))
    q.start_segment(late.id, 0)
    assert q.next_item().id == late.id


def test_not_before_and_expiry(tmp_path):
    q = _queue(tmp_path)
    future = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
    q.append(_item("news", _track("n"), not_before=future))
    old = q.append(_item("program", _track("old"), minutes_ago=121))  # program blocks live 2 h
    assert q.next_item() is None
    assert q.get(old.id).status == "expired"


def test_expire_lanes_at_broadcast_end(tmp_path):
    q = _queue(tmp_path)
    q.append(_item("program", _track("p")))
    reply = q.append(_item("reply", _track("r")))
    assert q.expire_lanes(("program", "filler"), "Sendeschluss") == 1
    assert q.next_item().id == reply.id


def test_remaining_program_seconds(tmp_path):
    q = _queue(tmp_path)
    block = q.append(_item(
        "program",
        _track("a", 200), _track("b", None), Segment("jingle", "J", "/j.wav", duration_seconds=12.5),
    ))
    q.append(_item("reply", _track("r", 300)))  # other lanes don't count
    assert q.remaining_program_seconds() == 200 + TRACK_ESTIMATE_SECONDS + 12.5
    q.start_segment(block.id, 0)  # "a" is on air now: only its remainder (from the player) counts
    assert q.remaining_program_seconds(current_remaining=50) == 50 + TRACK_ESTIMATE_SECONDS + 12.5


def test_cursor_resumes_with_next_segment_after_restart(tmp_path):
    q = _queue(tmp_path)
    block = q.append(_item("program", _track("a"), _track("b"), _track("c")))
    q.start_segment(block.id, 1)
    assert q.cursor()["item_id"] == block.id and q.cursor()["segment_index"] == 1

    restarted = _queue(tmp_path)
    item = restarted.next_item()
    assert item.id == block.id and item.status == "playing" and item.next_segment == 2
    restarted.start_segment(item.id, 2)
    restarted.finish_item(item.id)
    assert restarted.get(block.id).status == "played" and restarted.cursor() is None


def test_tolerant_loading_and_cleanup(tmp_path):
    q = _queue(tmp_path)
    old = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    data = {"items": [
        {"id": "new1", "lane": "program", "desk": "music", "created_at": datetime.now(timezone.utc).isoformat(),
         "future_field": 1, "segments": [{"type": "track", "title": "t", "audio_ref": "u", "loudness": -9}]},
        {"id": "gone", "lane": "program", "desk": "music", "created_at": old, "updated_at": old,
         "status": "played", "segments": []},
        "kaputt",
    ]}
    (tmp_path / "queue.json").write_text(json.dumps(data), encoding="utf-8")
    assert [i.id for i in q.items()] == ["new1", "gone"]
    q.append(_item("program", _track("x")))
    assert "gone" not in [i.id for i in q.items()]


def test_remove_and_restore(tmp_path):
    q = _queue(tmp_path)
    block = q.append(_item("program", _track("a"), _track("b")))
    q.start_segment(block.id, 0)
    assert q.remove(block.id).status == "removed"
    assert q.next_item() is None and q.remaining_program_seconds() == 0
    assert q.restore(block.id).status == "playing"
    assert q.next_item().id == block.id


def test_corrupt_file_starts_empty(tmp_path):
    (tmp_path / "queue.json").write_text("{kaputt", encoding="utf-8")
    assert _queue(tmp_path).items() == []
