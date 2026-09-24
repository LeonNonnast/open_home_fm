"""The news desk: slots (broadcast window, DST), cron trigger, bulletin formats, schedule_news,
news notes, and how the player airs bulletins."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import app.agent.desk as desk_module
from app import config as cfg
from app.agent.desk import DeskConfig, DeskRunner
from app.audio.player import QueuePlayer
from app.program.calls import follow_news_notes
from app.program.mailboxes import news_mailbox
from app.program.news import (
    NewsSession,
    cron_minutes,
    intro_text,
    next_slot,
    normalize_slots,
    notes_for,
    prepared_for,
    queued_bulletins,
    slot_times,
)
from app.program.queue import ProgramQueue, QueueItem, Segment
from app.scheduler import DeskScheduler
from tests.conftest import FakeMusicProvider, ScriptedLLM

BERLIN = ZoneInfo("Europe/Berlin")
SLOTS = [{"minute": "00", "format": "full"}, {"minute": "30", "format": "short"}]


def wait_for(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def local(*args) -> datetime:
    return datetime(*args, tzinfo=BERLIN)


# ---------- slots ----------

def test_normalize_slots():
    assert normalize_slots([{"minute": "30", "format": "short"}, {"minute": 0, "format": "full"}]) == SLOTS
    for bad in ([], None, [{"minute": "60", "format": "full"}], [{"minute": "00", "format": "long"}],
                [{"minute": "00"}, {"minute": "0"}], [{"minute": True}], ["00"]):
        assert normalize_slots(bad) is None, bad


def _gaps(times) -> set[float]:
    # In UTC: Python subtracts datetimes with the same tzinfo by wall clock.
    utc = [t.astimezone(timezone.utc) for t, _ in times]
    return {(b - a).total_seconds() for a, b in zip(utc, utc[1:])}


def test_slot_times_skip_the_missing_hour_in_spring():
    # 2026-03-29: 02:00 CET -> 03:00 CEST, there is no 02:00/02:30 on the wall clock.
    times = slot_times(SLOTS, local(2026, 3, 29, 0, 50), hours=4, tz=BERLIN)
    walls = [t.strftime("%H:%M") for t, _ in times[:5]]
    assert walls == ["01:00", "01:30", "03:00", "03:30", "04:00"]
    assert _gaps(times) == {1800}  # every 30 real minutes
    assert [f for _, f in times[:3]] == ["full", "short", "full"]


def test_slot_times_repeat_the_doubled_hour_in_autumn():
    # 2026-10-25: 03:00 CEST -> 02:00 CET, the hour 02:xx happens twice.
    times = slot_times(SLOTS, local(2026, 10, 25, 1, 50), hours=4, tz=BERLIN)
    walls = [t.strftime("%H:%M %Z") for t, _ in times[:5]]
    assert walls == ["02:00 CEST", "02:30 CEST", "02:00 CET", "02:30 CET", "03:00 CET"]
    assert _gaps(times) == {1800}


def test_next_slot_respects_the_broadcast_window():
    settings = {"slots": SLOTS}
    window = {"schedule": {"enabled": True, "start_time": "06:00", "end_time": "23:00"}}
    assert next_slot(window, settings, local(2026, 9, 24, 7, 10), tz=BERLIN) == (local(2026, 9, 24, 7, 30), "short")
    # 23:00 is the end of the window - the player would be off air before a song ends.
    assert next_slot(window, settings, local(2026, 9, 24, 22, 40), tz=BERLIN) == (local(2026, 9, 25, 6, 0), "full")
    assert next_slot(window, settings, local(2026, 9, 25, 3, 0), tz=BERLIN)[0] == local(2026, 9, 25, 6, 0)
    overnight = {"schedule": {"enabled": True, "start_time": "22:00", "end_time": "02:00"}}
    assert next_slot(overnight, settings, local(2026, 9, 24, 1, 10), tz=BERLIN)[0] == local(2026, 9, 24, 1, 30)
    assert next_slot(overnight, settings, local(2026, 9, 24, 1, 40), tz=BERLIN)[0] == local(2026, 9, 24, 22, 0)
    always = {"schedule": {"enabled": False}}
    assert next_slot(always, settings, local(2026, 9, 24, 7, 30), tz=BERLIN)[0] == local(2026, 9, 24, 8, 0)
    assert next_slot(always, {"slots": []}, local(2026, 9, 24, 7, 30)) is None


def test_intro_text():
    assert intro_text(local(2026, 9, 24, 7, 0), "full") == "Die Nachrichten um sieben Uhr."
    assert intro_text(local(2026, 9, 24, 7, 30), "short") == "Die Kurznachrichten um halb acht."
    assert intro_text(local(2026, 9, 24, 0, 0), "full") == "Die Nachrichten um Mitternacht."
    assert intro_text(local(2026, 9, 24, 12, 30), "short") == "Die Kurznachrichten um halb eins."
    assert intro_text(local(2026, 9, 24, 1, 0), "full") == "Die Nachrichten um ein Uhr."


# ---------- scheduler ----------

class RecordingRunner(DeskRunner):
    def __init__(self, root: Path):
        super().__init__(root, ProgramQueue(root / "data" / "queue.json", root / "data" / "cursor.json"))
        self.runs: list[tuple[str, str]] = []

    def run(self, name: str, trigger: str = "manual") -> dict:
        self.runs.append((name, trigger))
        return {"desk": name, "trigger": trigger, "error": None, "final_message": "ok"}


@pytest.fixture
def recorder(config_env: Path) -> RecordingRunner:
    cfg.update_config({"desks": {"news": {"enabled": True}}})
    return RecordingRunner(config_env)


def _slot_in(minutes: int, fmt: str = "full", lead: int = 5) -> datetime:
    """Configures one slot `minutes` from now; returns it (local, whole minute)."""
    at = (datetime.now().astimezone() + timedelta(minutes=minutes)).replace(second=0, microsecond=0)
    cfg.update_config({"desks": {"news": {"slots": [{"minute": f"{at.minute:02d}", "format": fmt}],
                                          "lead_minutes": lead}}})
    return at


def test_cron_jobs_fire_lead_minutes_before_each_slot(recorder):
    scheduler = DeskScheduler(recorder)
    assert scheduler.plan_news_jobs() == [25, 55]
    jobs = scheduler._scheduler.get_jobs()
    assert sorted(j.id for j in jobs) == ["news-25", "news-55"]
    tz = jobs[0].trigger.timezone
    start = datetime(2026, 9, 24, 6, 50, tzinfo=tz)
    fires = sorted(j.trigger.get_next_fire_time(None, start) for j in jobs)
    assert [f.strftime("%H:%M") for f in fires] == ["06:55", "07:25"]  # slots 07:00 and 07:30

    cfg.update_config({"desks": {"news": {"lead_minutes": 1, "slots": [{"minute": "00", "format": "full"}]}}})
    assert scheduler.plan_news_jobs() == [59]
    assert [j.id for j in scheduler._scheduler.get_jobs()] == ["news-59"]
    assert cron_minutes({"slots": SLOTS, "lead_minutes": 15}) == [15, 45]


def test_news_trigger_prepares_the_slot_ahead_once(recorder):
    scheduler = DeskScheduler(recorder)
    slot = _slot_in(2)
    assert scheduler.news_trigger() == "started"
    assert recorder.wait_idle("news", 5)
    assert recorder.runs == [("news", "slot")]
    # Prepared already: neither the cron job nor the watcher run again.
    recorder.queue.append(QueueItem.new("news", "news", [Segment("jingle", "N", "/x.wav")],
                                        not_before=slot.astimezone(timezone.utc).isoformat()))
    assert scheduler.news_trigger() is None
    scheduler.watch_news()
    assert recorder.runs == [("news", "slot")]
    # "Jetzt vorbereiten" prepares it again.
    assert scheduler.request_run("news", "manual", force=True)["status"] == "started"
    assert recorder.wait_idle("news", 5)
    assert recorder.runs[-1] == ("news", "manual")


def test_news_trigger_waits_for_the_lead_time_and_the_watcher_catches_up(recorder):
    scheduler = DeskScheduler(recorder)
    _slot_in(12, lead=5)
    assert scheduler.news_trigger() is None
    scheduler.watch_news()
    assert recorder.runs == []
    _slot_in(3, lead=5)  # e.g. the app started after the cron minute
    scheduler.watch_news()
    assert recorder.wait_idle("news", 5)
    assert recorder.runs == [("news", "catch_up")]


def test_news_outside_the_window_and_disabled(recorder):
    scheduler = DeskScheduler(recorder)
    slot = _slot_in(2)
    end = slot.strftime("%H:%M")
    start = (slot - timedelta(hours=3)).strftime("%H:%M")
    cfg.update_config({"schedule": {"enabled": True, "start_time": start, "end_time": end}})
    assert scheduler.news_trigger() is None  # the slot is the end of the window
    cfg.update_config({"schedule": {"enabled": False}, "desks": {"news": {"enabled": False}}})
    assert scheduler.news_trigger() == "disabled"
    assert recorder.runs == []


def test_news_status(recorder):
    scheduler = DeskScheduler(recorder)
    slot = _slot_in(20, fmt="short", lead=5)
    data = scheduler.desk_status("news")
    assert datetime.fromisoformat(data["next_slot_at"]) == slot
    assert data["next_slot_format"] == "short"
    assert datetime.fromisoformat(data["prepare_at"]) == slot - timedelta(minutes=5)
    assert data["prepared"] is False and data["next_trigger_at"] == data["prepare_at"]
    assert data["label"] == "Nachrichtenredaktion"


# ---------- the desk run ----------

NEWS_PLUGIN = '''
def execute(limit: int = 3) -> str:
    return "\\n".join(f"- Schlagzeile {i} (limit={limit})" for i in range(1, limit + 1))
'''
WEATHER_PLUGIN = '''
def execute(location=None, forecast=False) -> str:
    return "Jetzt 18 Grad." + (" Morgen 21 Grad, sonnig." if forecast else "")
'''


def _plugins(root: Path) -> None:
    for folder, name, code in (("news", "get_news_headlines", NEWS_PLUGIN), ("weather", "get_weather", WEATHER_PLUGIN)):
        d = root / "plugins" / folder
        d.mkdir(parents=True)
        (d / "manifest.yaml").write_text(f"name: {name}\ndescription: x\nparameters: {{type: object, properties: {{}}}}\n")
        (d / "plugin.py").write_text(code)


@pytest.fixture
def news_env(config_env, fake_tts, monkeypatch):
    _plugins(config_env)
    cfg.update_config({"desks": {"news": {"enabled": True}}})  # schedule_news re-checks it
    llm = ScriptedLLM([])
    monkeypatch.setattr(desk_module, "create_tts_engine", lambda config, cache_dir: fake_tts)
    monkeypatch.setattr(desk_module, "create_llm_provider", lambda config: llm)
    runner = DeskRunner(config_env, ProgramQueue(config_env / "data" / "queue.json", config_env / "data" / "cursor.json"))
    runner.llm = llm
    runner.tts = fake_tts
    return runner


def _user_message(llm: ScriptedLLM, index: int = 0) -> str:
    return next(m.content for m in llm.requests[index] if m.role == "user")


def _context(llm: ScriptedLLM, index: int = 0) -> str:
    return "\n".join(m.content for m in llm.requests[index] if m.role == "system")


def test_full_bulletin_gets_format_sources_and_all_notes(news_env):
    runner, llm = news_env, news_env.llm
    slot = _slot_in(4, fmt="full")
    new = runner.news_notes.add("Morgen ist Sperrmüll", author="Mama", valid_until=datetime.now(timezone.utc) + timedelta(days=1))
    aired = runner.news_notes.add("Oma kommt am Sonntag", valid_until=datetime.now(timezone.utc) + timedelta(days=2))
    runner.news_notes.mark_in_bulletin([aired["id"]], "old-item", (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat())
    stale = runner.news_notes.add("Gestern war Markt", valid_until=datetime.now(timezone.utc) - timedelta(hours=1))
    llm.turns = [[("schedule_news", {"text": "In Berlin scheint die Sonne. Aus dem Haushalt: morgen ist Sperrmüll."})],
                 "fertig"]

    result = runner.run("news", "slot")
    assert result["error"] is None and result["format"] == "full"
    message, context = _user_message(llm), _context(llm)
    assert "AUSFÜHRLICH" in message and "2-3 Minuten" in message and slot.strftime("%H:%M") in message
    assert "Schlagzeile 8 (limit=8)" in context and "Morgen 21 Grad" in context
    assert new["id"] in message and aired["id"] in message and stale["id"] not in message
    assert "schon einmal gemeldet" in message

    [item] = [i for i in runner.queue.items() if i.lane == "news"]
    assert datetime.fromisoformat(item.not_before) == slot
    assert datetime.fromisoformat(item.expires_at) == slot + timedelta(minutes=15)
    assert item.segments[0].text.startswith(f"Die Nachrichten um ")
    assert runner.tts.texts[-1] == item.segments[0].text
    assert sorted(result["notes_used"]) == sorted([new["id"], aired["id"]])
    assert runner.news_notes.get(new["id"])["status"] == "used"
    assert runner.news_notes.get(new["id"])["news_slot"] == slot.astimezone(timezone.utc).isoformat()
    assert runner.last_bulletin()["text"].startswith("In Berlin")


def test_short_bulletin_reads_only_new_notes_and_the_last_bulletin(news_env):
    runner, llm = news_env, news_env.llm
    until = datetime.now(timezone.utc) + timedelta(days=1)
    aired = runner.news_notes.add("Morgen ist Sperrmüll", valid_until=until)
    _slot_in(4, fmt="full")
    llm.turns = [[("schedule_news", {"text": "Erste Ausgabe."})], "ok"]
    runner.run("news", "slot")
    assert runner.news_notes.get(aired["id"])["status"] == "used"

    new = runner.news_notes.add("Papa kommt später", valid_until=until)
    _slot_in(6, fmt="short")
    llm.turns = [[("schedule_news", {"text": "Kurz: es bleibt sonnig."})], "ok"]
    result = runner.run("news", "slot")
    message, context = _user_message(llm, 2), _context(llm, 2)
    assert "KURZ" in message and "30-60 Sekunden" in message
    assert "Schlagzeile 4 (limit=4)" in context and "Morgen 21 Grad" not in context
    mandatory, optional = message.split("Schon gemeldet")
    assert new["id"] in mandatory and aired["id"] not in mandatory and aired["id"] in optional
    assert "„Erste Ausgabe.“" in message and "nicht wortgleich" in message
    assert result["notes_used"] == [new["id"]]
    assert _run_items(runner)[-1].segments[0].text.startswith("Die Kurznachrichten um")


def _run_items(runner) -> list[QueueItem]:
    return [i for i in runner.queue.items() if i.lane == "news"]


def test_sources_can_be_switched_off(news_env):
    runner, llm = news_env, news_env.llm
    cfg.update_config({"desks": {"news": {"sources": ["weather"], "intro": False}}})
    runner.news_notes.add("Sperrmüll")
    _slot_in(4)
    llm.turns = [[("schedule_news", {"text": "Nur Wetter."})], "ok"]
    result = runner.run("news", "slot")
    assert "Schlagzeile" not in _context(llm) and "Sperrmüll" not in _user_message(llm)
    assert result["notes_used"] == [] and _run_items(runner)[0].segments[0].text == "Nur Wetter."


def test_no_bulletin_counts_as_failure(news_env):
    news_env.llm.turns = ["Ich habe keine Lust."]
    _slot_in(4)
    assert "keine Ausgabe" in news_env.run("news", "slot")["error"]


def test_schedule_news_rejects_a_second_call_late_calls_and_replaces_on_re_prepare(config_env, fake_tts):
    queue = ProgramQueue(config_env / "data" / "queue.json", config_env / "data" / "cursor.json")
    notes = news_mailbox(config_env / "data")
    settings = DeskConfig.from_config("news", cfg.load_config()).settings
    slot = datetime.now(BERLIN).replace(second=0, microsecond=0) + timedelta(minutes=5)
    note = notes.add("Sperrmüll", valid_until=datetime.now(timezone.utc) + timedelta(days=1))
    first = NewsSession(slot, "full", settings, queue, notes, fake_tts, notes_for("full", notes))
    assert "eingeplant" in first.schedule_news("Die Nachrichten um fünf: alles ruhig.")
    assert fake_tts.texts[-1] == "Die Nachrichten um fünf: alles ruhig."  # has its own intro
    assert "schon eingeplant" in first.schedule_news("noch mal")
    assert notes.get(note["id"])["queue_item_id"] == first.item.id

    replacing = queued_bulletins(queue, slot)
    second = NewsSession(slot, "full", settings, queue, notes, fake_tts, notes_for("full", notes, replacing=replacing),
                         replacing=replacing)
    assert "ersetzt" in second.schedule_news("Neue Fassung.", note_ids=[])
    assert queue.get(first.item.id).status == "removed"
    assert queue.get(second.item.id).status == "queued"
    # A note the new version doesn't read isn't attached to it: it goes back to the mailbox.
    assert notes.get(note["id"])["queue_item_id"] == first.item.id
    follow_news_notes(notes, {i.id: i for i in queue.items()})
    assert notes.get(note["id"])["status"] == "noted"

    late = NewsSession(datetime.now(BERLIN) - timedelta(minutes=20), "short", settings, queue, notes, fake_tts)
    assert "Zu spät" in late.schedule_news("egal")


def test_notes_come_back_when_their_bulletin_did_not_air(config_env):
    queue = ProgramQueue(config_env / "data" / "queue.json", config_env / "data" / "cursor.json")
    notes = news_mailbox(config_env / "data")
    until = datetime.now(timezone.utc) + timedelta(days=1)
    missed = notes.add("verpasst", valid_until=until)
    aired = notes.add("gesendet", valid_until=until)
    item_missed = queue.append(QueueItem.new("news", "news", [Segment("jingle", "N", "/a.wav")]))
    item_aired = queue.append(QueueItem.new("news", "news", [Segment("jingle", "N", "/b.wav")]))
    earlier = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
    notes.mark_in_bulletin([missed["id"]], item_missed.id, earlier)
    notes.mark_in_bulletin([aired["id"]], item_aired.id, earlier)
    queue.expire_lanes(("news",), "abgelaufen")
    items = {i.id: i for i in queue.items()}
    items[item_aired.id].status = "played"
    assert follow_news_notes(notes, items) == [missed["id"]]
    assert notes.get(missed["id"])["status"] == "noted" and notes.get(missed["id"])["news_slot"] is None
    assert notes.get(aired["id"])["status"] == "used" and notes.get(aired["id"])["aired_at"]
    # Aired notes stay in the full bulletins until valid_until, not in the short ones.
    mandatory, _ = notes_for("full", notes)
    assert {n["id"] for n in mandatory} == {missed["id"], aired["id"]}
    mandatory, optional = notes_for("short", notes)
    assert [n["id"] for n in mandatory] == [missed["id"]] and [n["id"] for n in optional] == [aired["id"]]
    notes.set_fields(aired["id"], valid_until=(datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat())
    assert aired["id"] not in {n["id"] for n in notes_for("full", notes)[0]}


# ---------- queue & player ----------

def test_start_estimate_of_a_held_bulletin_is_the_first_boundary_after_its_slot(tmp_path):
    queue = ProgramQueue(tmp_path / "q.json", tmp_path / "c.json")
    now = datetime(2026, 9, 24, 6, 57, tzinfo=timezone.utc)
    block = queue.append(QueueItem.new("program", "music", [Segment("track", f"t{i}", f"u{i}", duration_seconds=200)
                                                           for i in range(4)], now=now))
    news = queue.append(QueueItem.new("news", "news", [Segment("jingle", "N", "/n.wav", duration_seconds=120)],
                                      now=now, not_before=(now + timedelta(minutes=3)).isoformat()))
    starts = queue.start_estimates(0, now)
    assert starts[block.id] == now
    # Songs end at +200 s and +400 s: the slot (+180 s) waits for the first song end.
    assert starts[news.id] == now + timedelta(seconds=200)


def _player(config_env: Path, provider, queue, jingles: list) -> QueuePlayer:
    return QueuePlayer(provider, queue, play_history_path=config_env / "data" / "history.json",
                       reserve_path=config_env / "data" / "reserve.json", idle_poll_seconds=0.05,
                       jingle_player=lambda path, stop_event: jingles.append(path))


def _news_item(not_before: datetime, minutes: int = 15) -> QueueItem:
    item = QueueItem.new("news", "news", [Segment("jingle", "Nachrichten 07:00", "/news.wav", duration_seconds=90)],
                         not_before=not_before.isoformat())
    item.expires_at = (not_before + timedelta(minutes=minutes)).isoformat()
    return item


def test_player_airs_news_after_the_current_song_once_the_slot_passed(config_env):
    queue = ProgramQueue(config_env / "data" / "queue.json", config_env / "data" / "cursor.json")
    provider = FakeMusicProvider(block=True)
    queue.append(QueueItem.new("program", "music", [Segment("track", t, f"u:{t}", duration_seconds=200) for t in "abc"]))
    jingles: list[str] = []
    player = _player(config_env, provider, queue, jingles)
    player.start()
    try:
        assert wait_for(lambda: provider.playing.is_set())
        # Slot in the future: song b follows a, the bulletin waits.
        held = queue.append(_news_item(datetime.now(timezone.utc) + timedelta(minutes=10)))
        player.skip()
        assert wait_for(lambda: len(provider.played) == 2 and provider.playing.is_set())
        assert jingles == [] and queue.get(held.id).status == "queued"
        queue.remove(held.id)
        # Slot passed while b plays: the bulletin comes at b's end, before c.
        news = queue.append(_news_item(datetime.now(timezone.utc) - timedelta(seconds=5)))
        assert player.status()["mode_text"] == "Nachrichten warten auf Song-Ende"
        player.skip()
        assert wait_for(lambda: len(provider.played) == 3 and provider.playing.is_set())
        assert jingles == ["/news.wav"] and [t.title for t in provider.played] == ["a", "b", "c"]
        assert queue.get(news.id).status == "played"
        assert any("Nachrichten" in line and "nach dem Slot" in line for line in player.status()["log"])
    finally:
        player.stop()


def test_player_drops_news_after_their_expiry(config_env):
    queue = ProgramQueue(config_env / "data" / "queue.json", config_env / "data" / "cursor.json")
    provider = FakeMusicProvider(block=True)
    news = queue.append(_news_item(datetime.now(timezone.utc) - timedelta(minutes=16)))
    queue.append(QueueItem.new("program", "music", [Segment("track", "a", "u:a", duration_seconds=200)]))
    jingles: list[str] = []
    player = _player(config_env, provider, queue, jingles)
    player.start()
    try:
        assert wait_for(lambda: provider.playing.is_set())
        assert jingles == [] and queue.get(news.id).status == "expired"
        assert any("verfallen" in line for line in player.status()["log"])
    finally:
        player.stop()


# ---------- review fixes ----------

def _bulletin(queue: ProgramQueue, slot: datetime) -> QueueItem:
    return queue.append(QueueItem.new("news", "news", [Segment("jingle", "N", "/x.wav")],
                                      not_before=slot.astimezone(timezone.utc).isoformat()))


def test_a_bulletin_removed_by_hand_counts_as_handled(recorder):
    scheduler = DeskScheduler(recorder)
    slot = _slot_in(3)
    item = _bulletin(recorder.queue, slot)
    recorder.queue.remove(item.id)
    assert prepared_for(recorder.queue, slot) is not None
    scheduler.watch_news()
    assert scheduler.news_trigger() is None and recorder.runs == []
    # An expired one doesn't count (e.g. a slot that was dropped and configured again).
    recorder.queue.restore(item.id)
    recorder.queue.expire_items([item.id], "weg")
    assert prepared_for(recorder.queue, slot) is None


def test_re_prepare_offers_the_replaced_notes_as_new_and_keeps_only_the_named_ones(config_env, fake_tts):
    queue = ProgramQueue(config_env / "data" / "queue.json", config_env / "data" / "cursor.json")
    notes = news_mailbox(config_env / "data")
    settings = DeskConfig.from_config("news", cfg.load_config()).settings
    slot = datetime.now(BERLIN).replace(second=0, microsecond=0) + timedelta(minutes=5)
    once = notes.add("Paket kommt")  # no valid_until: aired once
    lasting = notes.add("Sperrmüll", valid_until=datetime.now(timezone.utc) + timedelta(days=1))
    first = NewsSession(slot, "short", settings, queue, notes, fake_tts, notes_for("short", notes))
    first.schedule_news("Paket kommt heute, und Sperrmüll.")
    assert sorted(first.used_notes) == sorted([once["id"], lasting["id"]])

    replacing = queued_bulletins(queue, slot)
    offered = notes_for("short", notes, replacing=replacing)
    assert {n["id"] for n in offered[0]} == {once["id"], lasting["id"]}  # new again, not one slot late
    second = NewsSession(slot, "short", settings, queue, notes, fake_tts, offered, replacing=replacing)
    second.schedule_news("Nur das Paket.", note_ids=[once["id"]])
    assert second.used_notes == [once["id"]]
    follow_news_notes(notes, {i.id: i for i in queue.items()})
    assert notes.get(once["id"])["queue_item_id"] == second.item.id and notes.get(once["id"])["status"] == "used"
    # Not in the new version: back to the mailbox instead of blindly riding along.
    assert notes.get(lasting["id"])["status"] == "noted"


def test_expiry_is_capped_at_the_next_slot(config_env, fake_tts):
    queue = ProgramQueue(config_env / "data" / "queue.json", config_env / "data" / "cursor.json")
    notes = news_mailbox(config_env / "data")
    slot = local(2026, 9, 24, 7, 0)
    settings = {"slots": [{"minute": "00", "format": "full"}, {"minute": "20", "format": "short"}],
                "max_delay_minutes": 30}
    assert NewsSession(slot, "full", settings, queue, notes, fake_tts).expires_at == local(2026, 9, 24, 7, 20)
    settings["max_delay_minutes"] = 15
    assert NewsSession(slot, "full", settings, queue, notes, fake_tts).expires_at == local(2026, 9, 24, 7, 15)


def test_full_bulletins_cap_and_space_out_repeated_notes(config_env):
    notes = news_mailbox(config_env / "data")
    now = datetime.now(timezone.utc)
    long_ago, just_now = (now - timedelta(hours=5)).isoformat(), (now - timedelta(minutes=30)).isoformat()
    aired = [notes.add(f"alt {i}", valid_until=now + timedelta(days=i + 1)) for i in range(5)]
    for i, note in enumerate(aired):
        notes.mark_in_bulletin([note["id"]], f"old-{i}", just_now if i == 0 else long_ago)
    new = notes.add("neu")
    mandatory, optional = notes_for("full", notes, repeat_hours=3)
    # New first; then the 3 soonest expiring of those not repeated in the last 3 hours.
    assert [n["id"] for n in mandatory] == [new["id"]] + [n["id"] for n in aired[1:4]]
    assert {n["id"] for n in optional} == {aired[0]["id"], aired[4]["id"]}
    assert len(notes_for("full", notes, repeat_hours=0)[0]) == 1 + 3


def test_no_usable_source_and_no_notes_skips_the_llm(news_env):
    runner, llm = news_env, news_env.llm
    for folder, text in (("news", "News konnten nicht geladen werden: timeout"), ("weather", "Wetter derzeit nicht verfügbar.")):
        (runner.plugins_dir / folder / "plugin.py").write_text(f"def execute(*a, **k):\n    return {text!r}\n")
    _slot_in(4)
    result = runner.run("news", "slot")
    assert result["error"] is None and "Keine Nachrichten verfügbar" in result["final_message"]
    assert llm.requests == [] and _run_items(runner) == []
    # A new note is content: the desk runs.
    runner.news_notes.add("Oma kommt")
    llm.turns = [[("schedule_news", {"text": "Oma kommt."})], "ok"]
    assert runner.run("news", "slot")["error"] is None and len(_run_items(runner)) == 1


def test_schedule_news_rechecks_slot_and_desk(news_env):
    runner = news_env
    slot = _slot_in(4)
    llm = runner.llm
    other = f"{(slot.minute + 20) % 60:02d}"

    chat = llm.chat

    def change_slots_then_chat(messages, tools):
        # The slot is removed while the run is going (before the model calls schedule_news).
        cfg.update_config({"desks": {"news": {"slots": [{"minute": other, "format": "full"}]}}})
        return chat(messages, tools)

    llm.chat = change_slots_then_chat
    llm.turns = [[("schedule_news", {"text": "Zu spät geändert."})], "ok"]
    runner.run("news", "slot")
    assert _run_items(runner) == []
    session = NewsSession(slot, "full", {"slots": SLOTS}, runner.queue, runner.news_notes, runner.tts,
                          still_wanted=lambda: False)
    assert "nicht mehr eingeplant" in session.schedule_news("Text") and session.item is None


def test_weather_plugin_reports_api_errors(monkeypatch):
    import importlib.util

    import httpx

    spec = importlib.util.spec_from_file_location("weather_plugin", Path(__file__).parents[1] / "plugins" / "weather" / "plugin.py")
    weather = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(weather)
    monkeypatch.setattr(weather, "load_plugin_settings", lambda name: {"location": "X", "latitude": 1, "longitude": 2})
    answers = []

    real_client = httpx.Client

    def client(**kwargs):
        return real_client(transport=httpx.MockTransport(lambda request: answers[0]))

    monkeypatch.setattr(weather.httpx, "Client", client)
    for answer in (httpx.Response(429, json={"error": True, "reason": "Too many requests"}),
                   httpx.Response(200, json={"error": True}), httpx.Response(200, text="<html>")):
        answers[:] = [answer]
        assert weather.execute() == "Wetter derzeit nicht verfügbar."
    answers[:] = [httpx.Response(200, json={"current_weather": {"temperature": 18.5, "windspeed": 7, "weathercode": 0}})]
    assert weather.execute() == "Aktuelles Wetter in X: 18.5°C, klar, Wind 7 km/h."
