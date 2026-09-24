from __future__ import annotations

from pathlib import Path

import app.agent.desk as desk_module
from app.agent.desk import DeskRunner
from app.agent.play_history import record_played
from app.program.queue import ProgramQueue
from tests.conftest import FakeMusicProvider, ScriptedLLM, track


def _runner(config_env: Path, monkeypatch, provider, llm, fake_tts) -> DeskRunner:
    monkeypatch.setattr(desk_module, "create_music_provider", lambda config: provider)
    monkeypatch.setattr(desk_module, "create_tts_engine", lambda config, cache_dir: fake_tts)
    monkeypatch.setattr(desk_module, "create_llm_provider", lambda config: llm)
    queue = ProgramQueue(config_env / "data" / "queue.json", config_env / "data" / "player_cursor.json")
    return DeskRunner(config_env, queue)


def test_scripted_run_filters_recently_played(config_env: Path, fake_tts, monkeypatch):
    provider = FakeMusicProvider([track("Neu", "A", uri="u:neu"), track("Alt", "A", uri="u:alt")])
    llm = ScriptedLLM([
        [("search_songs", {"query": "A"})],
        [("append_program_block", {"segments": [
            {"type": "jingle", "text": "Guten Morgen"},
            {"type": "track", "uri": "u:alt"},
            {"type": "track", "uri": "u:neu"},
        ]})],
        "Fertig.",
    ])
    runner = _runner(config_env, monkeypatch, provider, llm, fake_tts)
    record_played(runner.play_history_path, "u:alt", "Alt - A")
    (config_env / "data" / "inbox").mkdir()
    (config_env / "data" / "inbox" / "20260924T070000.txt").write_text("Bitte was Neues", encoding="utf-8")

    result = runner.run("music", trigger="fill")

    assert result["error"] is None
    assert result["final_message"] == "Fertig."
    [item] = runner.queue.items()
    assert item.lane == "program" and item.desk == "music"
    assert [s.audio_ref for s in item.segments if s.type == "track"] == ["u:neu"]
    system, user = llm.requests[0][0], llm.requests[0][-1]
    assert system.content.startswith("Du bist der Standard-Redakteur")
    assert "Bitte was Neues" in user.content and "Alt - A" in user.content
    assert "erste Block nach Sendebeginn" in user.content
    tool_results = [m.content for m in llm.requests[-1] if m.role == "tool"]
    assert "Entfernt, weil kürzlich gespielt" in tool_results[-1]
    assert not list((config_env / "data" / "inbox").glob("*.txt"))
    [transcript] = (config_env / "data" / "transcripts").glob("*_music_*.json")


def test_followup_block_continues_instead_of_greeting(config_env: Path, fake_tts, monkeypatch):
    provider = FakeMusicProvider([track(f"S{i}", "A") for i in range(4)])
    llm = ScriptedLLM([
        [("append_program_block", {"segments": [{"query": "S0"}, {"query": "S1"}]})], "ok",
        [("append_program_block", {"segments": [{"query": "S1"}, {"query": "S2"}]})], "ok",
    ])
    runner = _runner(config_env, monkeypatch, provider, llm, fake_tts)
    runner.run("music")
    runner.run("music")
    second_input = llm.requests[2][-1].content
    assert "knüpfe an das Bisherige an" in second_input
    assert "S0 - A" in second_input and "S1 - A" in second_input  # queued = blocked
    assert [s.title for i in runner.queue.items() for s in i.segments] == ["S0 - A", "S1 - A", "S2 - A"]


def test_run_without_block_counts_as_error(config_env: Path, fake_tts, monkeypatch):
    runner = _runner(config_env, monkeypatch, FakeMusicProvider([]), ScriptedLLM(["Keine Lust."]), fake_tts)
    (config_env / "data" / "inbox").mkdir()
    (config_env / "data" / "inbox" / "w.txt").write_text("Wunsch", encoding="utf-8")
    result = runner.run("music")
    assert result["error"] and "keinen Programmblock" in result["error"]
    assert list((config_env / "data" / "inbox").glob("*.txt"))  # kept for the next try
