from __future__ import annotations

import json
from pathlib import Path

from app.agent.builtin_tools import build_builtin_tools
from app.program.queue import ProgramQueue, QueueItem, Segment
from tests.conftest import FakeMusicProvider, track


def _tool(tools, name):
    return next(t for t in tools if t.name == name)


def _queue(tmp_path: Path) -> ProgramQueue:
    return ProgramQueue(tmp_path / "queue.json", tmp_path / "cursor.json")


def _append(tmp_path, provider, tts, queue=None, **kwargs):
    queue = queue or _queue(tmp_path)
    tools = build_builtin_tools(provider, tts, queue, reserve_path=tmp_path / "reserve.json", **kwargs)
    return _tool(tools, "append_program_block").func, queue, tools


def _program_segments(queue: ProgramQueue) -> list[Segment]:
    return [s for item in queue.items() if item.lane == "program" for s in item.segments]


def test_repeat_filtering(tmp_path: Path, fake_tts):
    provider = FakeMusicProvider([
        track("Hey Jude", "The Beatles", uri="u:jude"),
        track("Hey Jude", "The Beatles", uri="u:jude-remaster"),
        track("Let It Be", "The Beatles", uri="u:letitbe"),
        track("Yesterday", "The Beatles", uri="u:yesterday"),
    ])
    recent = [{"uri": "u:letitbe", "title": "Let It Be - The Beatles"},
              {"uri": "u:other", "title": "Yesterday - The Beatles"}]
    append, queue, _ = _append(tmp_path, provider, fake_tts, recent_tracks=recent)

    result = append([
        {"type": "track", "uri": "u:jude"},
        {"type": "jingle", "text": "Hallo"},
        {"type": "track", "uri": "u:jude-remaster"},  # same song, other uri -> planned twice
        {"type": "track", "query": "Let It Be"},  # played recently, by uri
        {"type": "track", "uri": "u:yesterday"},  # played recently, by title
    ])

    segments = _program_segments(queue)
    assert [s.audio_ref for s in segments if s.type == "track"] == ["u:jude"]
    assert [s.text for s in segments if s.type == "jingle"] == ["Hallo"]
    assert "Entfernt, weil kürzlich gespielt oder schon eingeplant" in result
    assert "Let It Be - The Beatles" in result and "Yesterday - The Beatles" in result


def test_segment_type_inference_and_aliases(tmp_path: Path, fake_tts):
    provider = FakeMusicProvider([track(f"Song {i}", "Artist") for i in range(4)])
    append, queue, _ = _append(tmp_path, provider, fake_tts, songs_per_announcement=1)
    append([{"query": "Song 0"}, {"type": "announcement", "text": "Ansage"}, {"query": "Song 1"},
            {"text": "Noch eine"}, {"type": "track", "query": "gibtsnicht"}])
    assert [s.type for s in _program_segments(queue)] == ["track", "jingle", "track", "jingle"]
    assert fake_tts.texts == ["Ansage", "Noch eine"]


def test_min_length_feedback(tmp_path: Path, fake_tts):
    provider = FakeMusicProvider([track(f"Song {i}", "Artist", duration=240) for i in range(10)])
    append, _, _ = _append(tmp_path, provider, fake_tts, block_minutes=30)

    short = append([{"type": "track", "query": "Song 1"}])
    assert "zu kurz" in short and "ca. 30 Minuten" in short

    # The second call adds to the first: 1 + 7 songs x 4 min = 32 min in this run.
    longer = append([{"type": "track", "query": f"Song {i}"} for i in range(2, 9)])
    assert "ca. 28 Minuten) angehängt" in longer and "zu kurz" not in longer


def test_dupes_against_queue(tmp_path: Path, fake_tts):
    provider = FakeMusicProvider([track("A", "X", uri="u:a"), track("B", "X", uri="u:b")])
    queue = _queue(tmp_path)
    queue.append(QueueItem.new("program", "music", [Segment("track", "A - X", "u:a")]))
    append, _, _ = _append(tmp_path, provider, fake_tts, queue=queue)
    result = append([{"uri": "u:a"}, {"uri": "u:b"}])
    assert "A - X" in result.split("Entfernt")[1]
    assert [s.audio_ref for s in _program_segments(queue)] == ["u:a", "u:b"]


def test_announcement_rate_across_block_boundary(tmp_path: Path, fake_tts):
    provider = FakeMusicProvider([track(f"S{i}", "X") for i in range(8)])
    queue = _queue(tmp_path)
    # Queued program ends with an announcement followed by one song.
    queue.append(QueueItem.new("program", "music", [
        Segment("jingle", "Hallo", "/tmp/j.wav", text="Hallo"), Segment("track", "Alt - X", "u:alt"),
    ]))
    append, _, _ = _append(tmp_path, provider, fake_tts, queue=queue, songs_per_announcement=3)
    result = append([
        {"type": "jingle", "text": "Schon wieder hallo"},  # only 1 song since the last one: dropped
        {"query": "S0"}, {"query": "S1"},
        {"type": "jingle", "text": "Jetzt passt es"},  # 3 songs since: ok
        {"query": "S2"},
        {"type": "jingle", "text": "Zu früh"},  # 1 song: dropped
        {"query": "S3"},
    ])
    last = queue.items()[-1].segments
    assert [s.text for s in last if s.type == "jingle"] == ["Jetzt passt es"]
    assert "Schon wieder hallo" in result and "Zu früh" in result
    assert fake_tts.texts == ["Jetzt passt es"]


def test_first_block_may_start_with_greeting(tmp_path: Path, fake_tts):
    provider = FakeMusicProvider([track("S", "X")])
    append, queue, _ = _append(tmp_path, provider, fake_tts)
    append([{"type": "jingle", "text": "Guten Morgen"}, {"query": "S"}])
    assert [s.type for s in _program_segments(queue)] == ["jingle", "track"]


def test_cap_truncates_and_rejects(tmp_path: Path, fake_tts):
    provider = FakeMusicProvider([track(f"S{i}", "X", duration=300) for i in range(12)])
    append, queue, _ = _append(tmp_path, provider, fake_tts, max_queued_program_minutes=20)
    result = append([{"query": f"S{i}"} for i in range(6)])  # 30 min > 20 min cap
    assert len(_program_segments(queue)) == 4 and "Obergrenze" in result
    assert "Warteschlange ist voll" in append([{"query": "S7"}])


def test_update_reserve(tmp_path: Path, fake_tts):
    provider = FakeMusicProvider([track("A", "X", uri="u:a"), track("B", "X", uri="u:b")])
    _, _, tools = _append(tmp_path, provider, fake_tts, recent_tracks=[{"uri": "u:b", "title": "B - X"}])
    result = _tool(tools, "update_reserve").func(["A", {"uri": "u:a"}, {"query": "B"}, {"query": "nix"}])
    assert "1 Songs gespeichert" in result and "B - X" in result
    data = json.loads((tmp_path / "reserve.json").read_text(encoding="utf-8"))
    assert [t["uri"] for t in data["tracks"]] == ["u:a"]


def test_tolerates_json_strings_single_dicts_and_bad_segments(tmp_path: Path, fake_tts):
    provider = FakeMusicProvider([track(f"Song {i}", "Artist") for i in range(4)])
    append, queue, tools = _append(tmp_path, provider, fake_tts, songs_per_announcement=1)

    # Ollama quirk: the array as a JSON string.
    result = append(json.dumps([{"type": "jingle", "text": "Hallo"}, {"type": "track", "query": "Song 0"}]))
    assert "angehängt" in result and [s.type for s in _program_segments(queue)] == ["jingle", "track"]

    # A single object instead of an array.
    append({"type": "track", "query": "Song 1"})
    assert [s.title for s in _program_segments(queue)][-1] == "Song 1 - Artist"

    # A non-string type (inferred from its query), a non-object and an unknown type without
    # content cost only their own segment - the announcement already rendered stays in the block.
    result = append([{"type": "jingle", "text": "Weiter"}, {"type": 7, "query": "Song 2"}, "kaputt",
                     {"type": "podcast"}, {"type": "track", "query": "Song 3"}])
    titles = [s.title for s in _program_segments(queue)]
    assert "Weiter" in titles and "Song 2 - Artist" in titles and "Song 3 - Artist" in titles
    assert "Ignorierte Segmente" in result and "#3" in result and "podcast" in result

    assert "Kein Block angehängt" in append("kein json")

    # A segment whose lookup raises is skipped, the rest of the block (and its TTS) survives.
    search = provider.search_tracks
    provider.search_tracks = lambda q, limit=10: (_ for _ in ()).throw(RuntimeError("503")) if q == "boom" else search(q, limit)
    provider.tracks.append(track("Song 4", "Artist"))
    result = append([{"type": "jingle", "text": "Jetzt"}, {"query": "boom"}, {"query": "Song 4"}])
    assert "angehängt" in result and "503" in result
    assert [s.title for s in _program_segments(queue)][-2:] == ["Jetzt", "Song 4 - Artist"]

    # update_reserve: JSON string and a single search text.
    update = _tool(tools, "update_reserve").func
    assert "2 Songs" in update(json.dumps([{"query": "Song 0"}, {"query": "Song 1"}]))
    assert "1 Songs" in update("Song 2")


def test_set_playback_script_is_an_alias(tmp_path: Path, fake_tts):
    provider = FakeMusicProvider([track("Song 0", "Artist")])
    _, queue, tools = _append(tmp_path, provider, fake_tts)
    result = _tool(tools, "set_playback_script").func(segments=[{"type": "track", "query": "Song 0"}])
    assert "angehängt" in result and [s.title for s in _program_segments(queue)] == ["Song 0 - Artist"]
