from __future__ import annotations

from pathlib import Path

from app.agent.builtin_tools import build_builtin_tools
from app.agent.script import load_script
from tests.conftest import FakeMusicProvider, track


def _tool(tools, name):
    return next(t for t in tools if t.name == name)


def _set_script(tmp_path, provider, tts, **kwargs):
    script_path = tmp_path / "script.json"
    tools = build_builtin_tools(provider, tts, script_path, **kwargs)
    return _tool(tools, "set_playback_script").func, script_path


def test_repeat_filtering(tmp_path: Path, fake_tts):
    provider = FakeMusicProvider([
        track("Hey Jude", "The Beatles", uri="u:jude"),
        track("Hey Jude", "The Beatles", uri="u:jude-remaster"),
        track("Let It Be", "The Beatles", uri="u:letitbe"),
        track("Yesterday", "The Beatles", uri="u:yesterday"),
    ])
    recent = [{"uri": "u:letitbe", "title": "Let It Be - The Beatles"},
              {"uri": "u:other", "title": "Yesterday - The Beatles"}]
    set_script, script_path = _set_script(tmp_path, provider, fake_tts, recent_tracks=recent)

    result = set_script([
        {"type": "track", "uri": "u:jude"},
        {"type": "jingle", "text": "Hallo"},
        {"type": "track", "uri": "u:jude-remaster"},  # same song, other uri -> planned twice
        {"type": "track", "query": "Let It Be"},  # played recently, by uri
        {"type": "track", "uri": "u:yesterday"},  # played recently, by title
    ])

    script = load_script(script_path)
    assert [s.audio_ref for s in script.segments if s.type == "track"] == ["u:jude"]
    assert [s.text for s in script.segments if s.type == "jingle"] == ["Hallo"]
    assert "Entfernt, weil kürzlich gespielt oder doppelt eingeplant" in result
    assert "Let It Be - The Beatles" in result and "Yesterday - The Beatles" in result


def test_segment_type_inference_and_aliases(tmp_path: Path, fake_tts):
    provider = FakeMusicProvider([track("Song", "Artist", uri="u:song")])
    set_script, script_path = _set_script(tmp_path, provider, fake_tts)
    set_script([{"query": "Song"}, {"type": "announcement", "text": "Ansage"}, {"text": "Noch eine"}, {"type": "track", "query": "gibtsnicht"}])
    assert [s.type for s in load_script(script_path).segments] == ["track", "jingle", "jingle"]
    assert fake_tts.texts == ["Ansage", "Noch eine"]


def test_min_length_feedback(tmp_path: Path, fake_tts):
    provider = FakeMusicProvider([track(f"Song {i}", "Artist", duration=240) for i in range(10)])
    set_script, _ = _set_script(tmp_path, provider, fake_tts, min_program_minutes=30)

    short = set_script([{"type": "track", "query": "Song 1"}])
    assert "zu kurz" in short and "mindestens 30 Minuten" in short

    long = set_script([{"type": "track", "query": f"Song {i}"} for i in range(8)])  # 8 x 4 min
    assert "ca. 32 Minuten" in long and "zu kurz" not in long
