"""The dispatch desk ("Leitstelle") with a ScriptedLLM: routing, staging, retry, undo, airing."""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import app.agent.desk as desk_module
from app import config as cfg
from app.agent.desk import DeskRunner
from app.agent.llm import LLMMessage
from app.audio.player import QueuePlayer
from app.migrate import migrate_inbox
from app.program.calls import call_view, status_text
from app.program.queue import ProgramQueue, QueueItem, Segment
from tests.conftest import FakeMusicProvider, ScriptedLLM, track

HUE_MANIFEST = """name: control_hue_lights
description: Licht
parameters: {type: object, properties: {action: {type: string}}}
action: true
"""
HUE_PLUGIN = """from pathlib import Path

def execute(action, target=None, **kwargs):
    with (Path(__file__).parent / "calls.log").open("a") as f:
        f.write(action + "\\n")
    return "Wohnzimmer: eingeschaltet." if action == "on" else "Wohnzimmer: ausgeschaltet."
"""
WEATHER_MANIFEST = """name: get_weather
description: Wetter
parameters: {type: object, properties: {}}
"""
WEATHER_PLUGIN = """from pathlib import Path

def execute(**kwargs):
    with (Path(__file__).parent / "calls.log").open("a") as f:
        f.write("x\\n")
    return "Sonnig, 21 Grad."
"""


def wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class Env:
    def __init__(self, root: Path, runner: DeskRunner, provider: FakeMusicProvider, tts):
        self.root = root
        self.runner = runner
        self.provider = provider
        self.tts = tts
        self.llm: object = ScriptedLLM([])

    @property
    def calls(self):
        return self.runner.calls

    def dispatch(self, text: str, turns, author: str | None = "Mama") -> dict:
        call = self.calls.create(text, author=author)
        self.llm = ScriptedLLM(turns)
        result = self.runner.run("dispatch", "call")
        assert result["error"] is None, result
        return self.calls.get(call["id"])

    def plugin_calls(self, name: str) -> list[str]:
        log = self.root / "plugins" / name / "calls.log"
        return log.read_text().split() if log.exists() else []


@pytest.fixture
def env(config_env: Path, monkeypatch, fake_tts) -> Env:
    for name, manifest, code in (("hue", HUE_MANIFEST, HUE_PLUGIN), ("weather", WEATHER_MANIFEST, WEATHER_PLUGIN)):
        folder = config_env / "plugins" / name
        folder.mkdir(parents=True)
        (folder / "manifest.yaml").write_text(manifest, encoding="utf-8")
        (folder / "plugin.py").write_text(code, encoding="utf-8")
    cfg.update_config({"plugins": {"disabled": []}})
    provider = FakeMusicProvider([
        track("Bohemian Rhapsody", "Queen", uri="u:queen", duration=354),
        track("Hey Jude", "The Beatles", uri="u:jude"),
        track("Let It Be", "The Beatles", uri="u:letitbe"),
    ])
    queue = ProgramQueue(config_env / "data" / "queue.json", config_env / "data" / "player_cursor.json")
    runner = DeskRunner(config_env, queue)
    e = Env(config_env, runner, provider, fake_tts)
    monkeypatch.setattr(desk_module, "create_music_provider", lambda config: provider)
    monkeypatch.setattr(desk_module, "create_tts_engine", lambda config, cache_dir: fake_tts)
    monkeypatch.setattr(desk_module, "create_llm_provider", lambda config: e.llm)
    return e


def _tool_results(llm: ScriptedLLM) -> list[str]:
    return [m.content for m in llm.requests[-1] if m.role == "tool"]


# ---------- routing table ----------

def test_light_is_a_direct_action_and_leaves_the_program_alone(env):
    call = env.dispatch("Schalte das Licht an", [[("control_hue_lights", {"action": "on"})], "Licht ist an."])

    assert env.plugin_calls("hue") == ["on"]
    assert call["status"] == "routed" and call["final_message"] == "Licht ist an."
    [action] = call["actions"]
    assert (action["type"], action["urgency"], action["status"]) == ("plugin", "sofort", "done")
    assert action["summary"] == "Licht: Wohnzimmer: eingeschaltet."
    assert env.runner.queue.items() == []
    system, user = env.llm.requests[0]
    assert system.content == "Du bist die Leitstelle.\n"
    assert "Mama" in user.content and "Schalte das Licht an" in user.content
    [transcript] = (env.root / "data" / "transcripts").glob("*_dispatch_*.json")


def test_play_next_and_reply_become_one_item_announcement_first(env):
    call = env.dispatch("Licht an und spiel als Nächstes Queen", [
        [("control_hue_lights", {"action": "on"}),
         ("play_next", {"query": "Queen - Bohemian Rhapsody", "announce_text": "Für Mama: Queen!"}),
         ("reply", {"text": "Licht ist an, Mama.", "when": "next"})],
        "Licht an, Queen kommt als Nächstes.",
    ])

    [item] = env.runner.queue.items()
    assert (item.lane, item.desk, item.call_id, item.interrupt) == ("reply", "dispatch", call["id"], False)
    assert [s.type for s in item.segments] == ["jingle", "jingle", "track"]
    assert [s.text for s in item.segments[:2]] == ["Licht ist an, Mama.", "Für Mama: Queen!"]
    assert item.segments[2].audio_ref == "u:queen"
    assert item.expires_at and datetime.fromisoformat(item.expires_at) - datetime.fromisoformat(item.created_at) == timedelta(minutes=30)

    assert call["status"] == "queued" and call["eta"] and call["dispatched"]
    types = [a["type"] for a in call["actions"]]
    assert types == ["plugin", "reply", "play_next"]
    for action in call["actions"][1:]:
        assert action["queue_item_id"] == item.id and action["status"] == "queued" and action["urgency"] == "als Nächstes"
    assert "Bohemian Rhapsody - Queen (mit Ansage)" in call["actions"][2]["summary"]
    assert call["reply_text"] == "Licht ist an, Mama.\nFür Mama: Queen!"
    assert call["reply_audio"] == item.segments[0].audio_ref
    assert status_text(call).startswith("läuft nach diesem Song")


def test_now_is_downgraded_to_next_with_a_note(env):
    call = env.dispatch("Spiel jetzt Hey Jude, und Unwetterwarnung für heute Abend!", [
        [("play_now", {"query": "Hey Jude"}), ("breaking", {"text": "Achtung: Unwetter heute Abend."})],
        [("reply", {"text": "Kommt sofort.", "when": "now"})],
        "ok",
    ])
    [item] = env.runner.queue.items()
    assert item.lane == "reply" and not item.interrupt
    assert [s.type for s in item.segments] == ["jingle", "jingle", "track"]
    by_type = {a["type"]: a for a in call["actions"]}
    for kind in ("play_now", "breaking", "reply"):
        action = by_type[kind]
        assert action["urgency"] == "sofort" and action["note"] == "Unterbrechen kommt später"
        assert "als Nächstes statt jetzt" in action["summary"] and action["queue_item_id"] == item.id
    assert "noch nicht möglich" in [m.content for m in env.llm.requests[1] if m.role == "tool"][0]
    # A breaking news item goes to the news mailbox as well.
    [note] = env.runner.news_notes.all()
    assert note["text"] == "Achtung: Unwetter heute Abend." and by_type["breaking"]["note_id"] == note["id"]


def test_allow_interrupt_off_says_so(env):
    cfg.update_config({"desks": {"dispatch": {"allow_interrupt": False}}})
    call = env.dispatch("jetzt Hey Jude", [[("play_now", {"query": "Hey Jude"})], "ok"])
    assert call["actions"][0]["note"] == "Unterbrechen ist ausgeschaltet"


def test_wish_is_stored_and_later_consumed_by_the_music_desk(env):
    call = env.dispatch("Beatles fänd ich demnächst mal top", [
        [("add_music_wish", {"text": "Mal wieder die Beatles"})], "Vorgemerkt.",
    ])
    [action] = call["actions"]
    assert (action["type"], action["urgency"], action["status"]) == ("music_wish", "demnächst", "noted")
    [wish] = env.runner.wishes.open()
    assert wish["id"] == action["wish_id"] and wish["author"] == "Mama" and wish["call_id"] == call["id"]
    until = datetime.fromisoformat(wish["valid_until"])
    assert timedelta(hours=23) < until - datetime.now(timezone.utc) <= timedelta(hours=24)
    assert call["status"] == "routed" and env.runner.queue.items() == []

    music_llm = ScriptedLLM([
        [("append_program_block", {"segments": [
            {"type": "jingle", "text": "Für Mama: die Beatles!", "wish_id": wish["id"]},
            {"query": "Hey Jude", "wish_id": wish["id"]}, {"query": "Let It Be", "wish_id": "gibtsnicht"},
        ]})],
        "Block steht.",
    ])
    env.llm = music_llm
    assert env.runner.run("music", "fill")["error"] is None
    assert "Mal wieder die Beatles (von Mama)" in music_llm.requests[0][-1].content
    assert f"wish_id={wish['id']}" in music_llm.requests[0][-1].content
    assert "Unbekannte oder schon erfüllte wish_id ignoriert: gibtsnicht" in _tool_results(music_llm)[0]

    [block] = env.runner.queue.items()
    used = env.runner.wishes.get(wish["id"])
    assert used["status"] == "used" and used["queue_item_id"] == block.id
    assert env.runner.wishes.open() == []
    env.calls.sync()
    action = env.calls.get(call["id"])["actions"][0]
    assert action["status"] == "used" and action["note"].startswith("im Block ab")
    assert env.calls.get(call["id"])["status"] == "routed"


def test_expired_wishes_are_not_offered(env):
    env.runner.wishes.add("alt", valid_until=datetime.now(timezone.utc) - timedelta(minutes=1))
    assert env.runner.wishes.open() == [] and env.runner.wishes.all()[0]["status"] == "expired"


def test_news_note_is_stored(env):
    call = env.dispatch("Morgen ist Sperrmüll", [
        [("note_for_news", {"text": "Morgen ist Sperrmüll", "valid_until": "2099-01-01T12:00:00"})], "ok",
    ])
    [action] = call["actions"]
    assert (action["type"], action["urgency"], action["status"]) == ("news_note", "Nachrichten", "noted")
    [note] = env.runner.news_notes.open()
    assert note["id"] == action["note_id"] and note["text"] == "Morgen ist Sperrmüll"
    # Clamped to at most two weeks.
    assert datetime.fromisoformat(note["valid_until"]) - datetime.now(timezone.utc) <= timedelta(days=14)


def test_episodes_need_a_source_that_can_play_them(env):
    env.dispatch("Spiel die Lage der Nation", [
        [("play_next", {"episode_query": "Lage der Nation"})],
        [("reply", {"text": "Podcasts gehen leider noch nicht."})],
        "ok",
    ])
    assert "nicht abspielen" in [m.content for m in env.llm.requests[1] if m.role == "tool"][0]

    class EpisodeProvider(FakeMusicProvider):
        supports_episodes = True

        def search_episodes(self, query, limit=5):
            return [track("Folge 12", "Lage der Nation", uri="spotify:episode:e1", duration=1800)]

    env.provider.__class__ = EpisodeProvider
    call = env.dispatch("Spiel die Lage der Nation", [[("play_next", {"episode_query": "Lage"})], "ok"])
    item = env.runner.queue.get(call["actions"][0]["queue_item_id"])
    assert [(s.type, s.audio_ref, s.duration_seconds) for s in item.segments if s.type != "jingle"] == \
        [("episode", "spotify:episode:e1", 1800)]


def test_info_plugins_are_cached(env):
    for _ in range(2):
        env.dispatch("Brauche ich einen Schirm?", [[("get_weather", {})], [("reply", {"text": "Nein."})], "ok"])
    assert env.plugin_calls("weather") == ["x"]
    call = env.calls.all()[0]
    assert [a["type"] for a in call["actions"]] == ["reply"]  # lookups are no actions


def test_context_has_recent_calls_with_results(env):
    env.dispatch("Licht an", [[("control_hue_lights", {"action": "on"})], "ok"], author="Opa")
    env.dispatch("Und jetzt?", ["Nichts zu tun."])
    user = env.llm.requests[0][-1].content
    assert "Letzte Zwischenrufe" in user and "Opa" in user and "Licht: Wohnzimmer: eingeschaltet. [done]" in user


# ---------- lifecycle ----------

class FlakyLLM(ScriptedLLM):
    """Raises like an unreachable Ollama on the request numbers in `fail_on` (1-based)."""

    def __init__(self, turns, fail_on: set[int]):
        super().__init__(turns)
        self.fail_on = fail_on
        self.count = 0

    def chat(self, messages, tools):
        self.count += 1
        if self.count in self.fail_on:
            raise ConnectionError("Ollama nicht erreichbar")
        return super().chat(messages, tools)


def test_llm_failure_keeps_the_call_open_and_retries_with_backoff(env):
    call = env.calls.create("Licht an und erzähl was", author="Mama")
    other = env.calls.create("Spiel Queen")
    env.llm = FlakyLLM([[("control_hue_lights", {"action": "on"})]], fail_on={2})
    result = env.runner._execute("dispatch", "call")
    assert result["error"] == "Ollama nicht erreichbar"

    failed = env.calls.get(call["id"])
    assert failed["status"] == "retrying" and failed["attempts"] == 1 and failed["error"]
    assert status_text(failed) == "Leitstelle gerade nicht erreichbar – wird erneut versucht"
    assert [a["summary"] for a in failed["actions"]] == ["Licht: Wohnzimmer: eingeschaltet."]
    assert env.calls.get(other["id"])["status"] == "new"  # waits behind it
    assert env.runner.queue.items() == []
    status = env.runner.status("dispatch")
    assert status["state"] == "error"
    backoff = datetime.fromisoformat(status["backoff_until"]) - datetime.now(timezone.utc)
    assert timedelta(seconds=5) < backoff <= timedelta(seconds=10)
    assert env.runner.request("dispatch", "poll") == "backoff"

    # The LLM is back: the retry doesn't switch the light again, both calls get handled.
    env.llm = ScriptedLLM([[("reply", {"text": "Hallo Mama."})], "ok", [("play_next", {"query": "Queen"})], "ok"])
    assert env.runner._execute("dispatch", "poll")["error"] is None
    assert env.plugin_calls("hue") == ["on"]
    assert "bereits erledigt" in env.llm.requests[0][-1].content and "Wohnzimmer" in env.llm.requests[0][-1].content
    done = env.calls.get(call["id"])
    assert done["status"] == "queued" and done["attempts"] == 2 and done["error"] is None
    assert [a["type"] for a in done["actions"]] == ["plugin", "reply"]
    assert env.calls.get(other["id"])["status"] == "queued"
    assert env.runner.status("dispatch")["consecutive_failures"] == 0


def test_pending_calls_expire(env):
    call = env.calls.create("Hallo?")
    old = (datetime.now(timezone.utc) - timedelta(minutes=31)).isoformat()
    env.calls.update(call["id"], lambda c: c.update(status="retrying", submitted_at=old))
    assert env.calls.expire_stale(30) == 1
    assert env.calls.get(call["id"])["status"] == "expired"
    assert status_text(env.calls.get(call["id"])) == "verfallen – nochmal senden?"


def test_call_arriving_during_a_run_is_processed(env):
    started, release = threading.Event(), threading.Event()

    class SlowLLM(ScriptedLLM):
        def chat(self, messages, tools):
            if not started.is_set():
                started.set()
                assert release.wait(5)
            return LLMMessage(role="assistant", content="ok")

    env.llm = SlowLLM([])
    first = env.calls.create("Erster")
    assert env.runner.request("dispatch", "call", condition=env.calls.has_pending) == "started"
    assert started.wait(2)
    second = env.calls.create("Zweiter")
    assert env.runner.request("dispatch", "call", condition=env.calls.has_pending) == "queued"
    release.set()
    assert env.runner.wait_idle("dispatch", 5)
    assert wait_for(lambda: not env.runner.is_running("dispatch"))
    assert [env.calls.get(c["id"])["status"] for c in (first, second)] == ["routed", "routed"]


def test_undo_of_single_actions(env):
    call = env.dispatch("Licht an, als Nächstes Queen, und demnächst Beatles", [
        [("control_hue_lights", {"action": "on"}), ("play_next", {"query": "Queen"}),
         ("add_music_wish", {"text": "Beatles"}), ("note_for_news", {"text": "Sperrmüll"})],
        "ok",
    ])
    kinds = [a["type"] for a in call["actions"]]
    assert kinds == ["plugin", "play_next", "music_wish", "news_note"]

    _, error = env.calls.undo_action(call["id"], 0)
    assert "nicht rückgängig" in error
    item_id = call["actions"][1]["queue_item_id"]
    updated, error = env.calls.undo_action(call["id"], 1)
    assert error is None and env.runner.queue.get(item_id).status == "removed"
    assert updated["actions"][1]["status"] == "removed" and updated["status"] == "routed"
    updated, error = env.calls.undo_action(call["id"], 2)
    assert error is None and env.runner.wishes.get(call["actions"][2]["wish_id"])["status"] == "removed"
    updated, error = env.calls.undo_action(call["id"], 3)
    assert error is None and updated["actions"][3]["status"] == "removed"
    assert not any(a["undo_available"] for a in call_view(updated)["actions"])
    assert env.calls.undo_action(call["id"], 1)[1].startswith("Nicht mehr rückgängig")
    assert env.calls.undo_action(call["id"], 9)[1] == "Aktion nicht gefunden"


def test_off_air_direct_actions_run_and_program_waits(env, monkeypatch):
    start = datetime.now() + timedelta(hours=8)
    monkeypatch.setattr(desk_module, "is_broadcast_time", lambda config: False)
    monkeypatch.setattr(desk_module, "next_broadcast_start", lambda config: start)
    call = env.dispatch("Licht an und als Nächstes Queen", [
        [("control_hue_lights", {"action": "on"}), ("play_next", {"query": "Queen"})], "ok",
    ])
    assert env.plugin_calls("hue") == ["on"]
    [item] = env.runner.queue.items()
    assert datetime.fromisoformat(item.not_before) == start.astimezone(timezone.utc)
    assert datetime.fromisoformat(item.expires_at) == start.astimezone(timezone.utc) + timedelta(minutes=30)
    assert call["actions"][1]["status"] == "held" and call["status"] == "queued"
    assert status_text(call).startswith(f"Sender ruht bis {start:%H:%M}")
    assert "Sendepause" in env.llm.requests[0][-1].content


def test_reply_plays_after_the_current_song_and_airing_updates_the_call(env):
    player_provider = FakeMusicProvider(block=True)
    queue = env.runner.queue
    queue.append(QueueItem.new("program", "music", [
        Segment("track", "a", "u:a", duration_seconds=200), Segment("track", "b", "u:b", duration_seconds=200),
    ]))
    player = QueuePlayer(player_provider, queue, idle_poll_seconds=0.05, jingle_player=lambda path, stop: None,
                         reserve_path=env.root / "data" / "reserve.json")
    player.on_item_event = env.calls.on_queue_event
    env.runner.player = player
    player.start()
    try:
        assert wait_for(lambda: player_provider.playing.is_set())
        call = env.dispatch("Als Nächstes Queen", [[("play_next", {"query": "Queen", "announce_text": "Für Mama"})], "ok"])
        assert call["status"] == "queued"
        # ETA = the rest of the song on air.
        eta = datetime.fromisoformat(call["eta"]) - datetime.now(timezone.utc)
        assert timedelta(seconds=150) < eta <= timedelta(seconds=200)

        player.skip()  # song "a" ends
        assert wait_for(lambda: player_provider.played[-1].uri == "u:queen" and player_provider.playing.is_set())
        assert wait_for(lambda: env.calls.get(call["id"])["actions"][0]["status"] == "playing")
        assert status_text(env.calls.get(call["id"])) == "läuft jetzt"

        player.skip()  # the reply ends
        assert wait_for(lambda: env.calls.get(call["id"])["status"] == "aired")
        aired = env.calls.get(call["id"])
        assert aired["aired_at"] and aired["actions"][0]["status"] == "aired"
        assert status_text(aired).startswith("gesendet ")
        assert wait_for(lambda: player_provider.played[-1].uri == "u:b")
    finally:
        player.stop()
    assert [t.uri for t in player_provider.played] == ["u:a", "u:queen", "u:b"]


def test_withdrawn_call_during_dispatch_commits_nothing(env):
    call = env.calls.create("Als Nächstes Queen")

    class WithdrawingLLM(ScriptedLLM):
        def chat(self, messages, tools):
            if len(self.requests) == 1:
                env.calls.remove_call(call["id"])
            return super().chat(messages, tools)

    env.llm = WithdrawingLLM([[("play_next", {"query": "Queen"})], "ok"])
    assert env.runner.run("dispatch", "call")["error"] is None
    assert env.calls.get(call["id"])["status"] == "removed" and env.runner.queue.items() == []


def test_inbox_migration(config_env: Path):
    from app.program.calls import CallStore

    inbox = config_env / "data" / "inbox"
    inbox.mkdir()
    (inbox / "20260923T071915043891.txt").write_text("Spiel mal Queen", encoding="utf-8")
    (inbox / "20260923T071915043891.webm").write_bytes(b"x")
    (inbox / "20260923T080000000000.txt").write_text("  ", encoding="utf-8")
    calls = CallStore(config_env / "data" / "calls")

    assert migrate_inbox(calls, config_env) == 1
    [call] = calls.all()
    assert (call["text"], call["status"], call["source"]) == ("Spiel mal Queen", "new", "voice")
    assert call["created_at"].startswith("2026-09-23T07:19:15") and call["id"].startswith("20260923T071915-")
    assert not list(inbox.iterdir())
    assert len(list((config_env / "data" / "processed").iterdir())) == 3
    assert migrate_inbox(calls, config_env) == 0 and len(calls.all()) == 1
