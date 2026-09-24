from __future__ import annotations

from pathlib import Path

import app.agent.loop as loop_module
from app.agent.loop import AgentLoop
from app.agent.play_history import record_played
from tests.conftest import FakeMusicProvider, ScriptedLLM, track


def test_scripted_run_filters_recently_played(config_env: Path, fake_tts, monkeypatch):
    provider = FakeMusicProvider([track("Neu", "A", uri="u:neu"), track("Alt", "A", uri="u:alt")])
    llm = ScriptedLLM([
        [("search_songs", {"query": "A"})],
        [("set_playback_script", {"segments": [
            {"type": "jingle", "text": "Guten Morgen"},
            {"type": "track", "uri": "u:alt"},
            {"type": "track", "uri": "u:neu"},
        ]})],
        "Fertig.",
    ])
    monkeypatch.setattr(loop_module, "create_music_provider", lambda config: provider)
    monkeypatch.setattr(loop_module, "create_tts_engine", lambda config, cache_dir: fake_tts)
    monkeypatch.setattr(loop_module, "create_llm_provider", lambda config: llm)

    agent = AgentLoop(config_env)
    record_played(agent.play_history_path, "u:alt", "Alt - A")
    (config_env / "data" / "inbox").mkdir()
    (config_env / "data" / "inbox" / "20260924T070000.txt").write_text("Bitte was Neues", encoding="utf-8")

    state = agent.run_once()

    assert state["error"] is None
    assert state["final_message"] == "Fertig."
    assert [s["audio_ref"] for s in state["script"]["segments"] if s["type"] == "track"] == ["u:neu"]
    system, user = llm.requests[0][0], llm.requests[0][-1]
    assert system.content.startswith("Du bist der Standard-Redakteur")
    assert "Bitte was Neues" in user.content and "Alt - A" in user.content
    tool_results = [m.content for m in llm.requests[-1] if m.role == "tool"]
    assert "Entfernt, weil kürzlich gespielt" in tool_results[-1]
    assert not list((config_env / "data" / "inbox").glob("*.txt"))
    assert list((config_env / "data" / "transcripts").glob("*.json"))
