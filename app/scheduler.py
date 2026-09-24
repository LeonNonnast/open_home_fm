"""Decides when desks run. Replaces the old fixed interval (agent.loop_interval_seconds).

Music desk:
- fill-level watcher every 30 s: program left < `fill_threshold_minutes` ⇒ run,
- heartbeat every 60 min (picks up wishes, refreshes the reserve),
- right at the broadcast start.
The fill watcher holds off while the player's circuit breaker is tripped (source unreachable).

News desk: an APScheduler cron job per slot, `lead_minutes` before it (re-planned when the
slots change), for slots inside the broadcast window. The watcher catches up on a slot whose
preparation was missed (app started late, the run failed - gated by the desk's backoff) while
the slot is still ahead; a manual run ("Jetzt vorbereiten") prepares the next slot again and
replaces what was prepared for it.

Dispatch desk: right away for every new call (the calls API asks), plus a watcher every 10 s
that syncs the calls with the queue, expires calls nobody could handle and re-requests a run
while calls are open (which also retries after an LLM failure, gated by the desk's backoff of
10 s / 30 s / 1 min). It ignores the broadcast window.
While the station is stopped (Stop button, `station.stopped`) no desk runs at all - the
dispatch desk included; calls wait (or expire) until Play. Play starts the music desk right away
like a broadcast start.
Every trigger respects the broadcast window and `max_queued_program_minutes` - neither the
watcher nor "run now" stacks the queue beyond that. Locking, follow-up runs and backoff are
the DeskRunner's job (app/agent/desk.py).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler

from app.agent.desk import DESK_LABELS, DESKS, DeskConfig, DeskRunner
from app.config import is_broadcast_time, is_stopped, load_config
from app.program.calls import STUCK_PROCESSING_MINUTES
from app.program.news import FORMAT_LABELS, cron_minutes, next_slot, normalize_slots

logger = logging.getLogger(__name__)

WATCH_SECONDS = 30
HEARTBEAT_MINUTES = 60
CALLS_WATCH_SECONDS = 10
STOPPED_REASON = "Sender gestoppt – erst Play startet die Redaktionen wieder."


class DeskScheduler:
    def __init__(self, runner: DeskRunner):
        self.runner = runner
        self._scheduler = BackgroundScheduler()
        self._heartbeat_job = None
        self._watch_job = None
        self._calls_job = None
        self._was_on_air: bool | None = None
        self._was_stopped = False
        self._news_jobs: list = []
        self._news_signature: tuple | None = None

    def start(self) -> None:
        # next_run_time=None would add the job *paused* in APScheduler 3.x - check right away.
        self._watch_job = self._scheduler.add_job(self.watch, "interval", seconds=WATCH_SECONDS,
                                                  next_run_time=datetime.now(), max_instances=1, coalesce=True)
        self._heartbeat_job = self._scheduler.add_job(self.heartbeat, "interval", minutes=HEARTBEAT_MINUTES,
                                                      max_instances=1, coalesce=True)
        self._calls_job = self._scheduler.add_job(self.watch_calls, "interval", seconds=CALLS_WATCH_SECONDS,
                                                  next_run_time=datetime.now(), max_instances=1, coalesce=True)
        self.plan_news_jobs()
        self._scheduler.start()
        logger.info("Desk scheduler started (fill watcher every %ds, heartbeat every %d min, calls every %ds)",
                    WATCH_SECONDS, HEARTBEAT_MINUTES, CALLS_WATCH_SECONDS)

    def stop(self) -> None:
        self._scheduler.shutdown(wait=False)

    def wake(self) -> None:
        """Runs the fill and calls watchers now instead of in up to 30/10 s (Play: the music desk
        plans and waiting calls go to the dispatch desk at once)."""
        for job in (self._watch_job, self._calls_job):
            try:
                if job is not None:
                    job.modify(next_run_time=datetime.now())
            except Exception:
                logger.warning("Could not wake a watcher", exc_info=True)

    # ---------- music desk conditions ----------

    @staticmethod
    def _music() -> DeskConfig:
        return DeskConfig.from_config("music", load_config())

    def _remaining_minutes(self) -> float:
        return self.runner.remaining_program_seconds() / 60

    def below_threshold(self) -> bool:
        return self._remaining_minutes() < self._music().settings["fill_threshold_minutes"]

    def under_cap(self) -> bool:
        return self._remaining_minutes() < self._music().settings["max_queued_program_minutes"]

    def _music_may_run(self) -> bool:
        return is_broadcast_time(load_config()) and self.under_cap()

    # ---------- news desk ----------

    @staticmethod
    def _news() -> DeskConfig:
        return DeskConfig.from_config("news", load_config())

    def plan_news_jobs(self) -> list[int]:
        """(Re-)creates the cron jobs: one per distinct minute of the hour at which a slot is
        prepared. Cheap when nothing changed, so the watcher calls it every time."""
        settings = self._news().settings
        minutes = cron_minutes(settings)
        signature = (tuple(minutes),)
        if signature == self._news_signature:
            return minutes
        for job in self._news_jobs:
            try:
                job.remove()
            except Exception:
                pass
        self._news_jobs = [
            self._scheduler.add_job(self.news_trigger, "cron", minute=minute, second=0, id=f"news-{minute:02d}",
                                    replace_existing=True, max_instances=1, coalesce=True, misfire_grace_time=120)
            for minute in minutes
        ]
        self._news_signature = signature
        logger.info("News desk: preparing at minute(s) %s of every hour", ", ".join(f"{m:02d}" for m in minutes) or "-")
        return minutes

    def _news_target(self) -> tuple[datetime, str] | None:
        config = load_config()
        return next_slot(config, DeskConfig.from_config("news", config).settings)

    def _news_due(self, slot: datetime) -> bool:
        """Within the lead time before `slot` and nothing prepared for it yet."""
        lead = self._news().settings["lead_minutes"]
        slot = slot.astimezone(timezone.utc)  # UTC: same-tzinfo datetimes compare by wall clock
        now = datetime.now(timezone.utc)
        return slot - timedelta(minutes=lead, seconds=30) <= now < slot and not self.runner.news_prepared(slot)

    def news_trigger(self) -> str | None:
        """The cron job: prepares the slot `lead_minutes` ahead (if it's inside the broadcast window)."""
        try:
            target = self._news_target()
            if target is None or not self._news_due(target[0]):
                logger.info("News cron: no slot to prepare right now (next: %s)", target[0] if target else "-")
                return None
            return self.request_run("news", "slot")["status"]
        except Exception:
            logger.exception("News trigger failed")
            return None

    def watch_news(self) -> None:
        try:
            self.plan_news_jobs()
            target = self._news_target()
            if target is None or self.runner.is_running("news") or not self._news_due(target[0]):
                return
            status = self.request_run("news", "catch_up")["status"]
            if status == "started":
                logger.info("News for %s not prepared yet - news desk started", target[0].strftime("%H:%M"))
        except Exception:
            logger.exception("News watcher failed")

    # ---------- triggers ----------

    def request_run(self, name: str, trigger: str, force: bool = False) -> dict:
        """Runs desk `name` through its lock; for the API and all timed triggers."""
        if name not in DESKS:
            raise KeyError(name)
        if is_stopped(load_config()):
            return {"status": "skipped", "reason": STOPPED_REASON}
        if name == "music":
            if not is_broadcast_time(load_config()):
                return {"status": "skipped", "reason": "Sendepause – die Musikredaktion plant erst zum Sendebeginn."}
            if not self.under_cap():
                cap = self._music().settings["max_queued_program_minutes"]
                return {"status": "skipped", "reason": f"Warteschlange voll (Obergrenze {cap} Minuten Programm)."}
            condition = self._music_may_run
        elif name == "news":
            target = self._news_target()
            if target is None:
                return {"status": "skipped", "reason": "Keine Ausgabe im Sendefenster der nächsten 48 Stunden."}
            # Timed runs only while the slot still needs one; "Jetzt vorbereiten" prepares it again.
            condition = None if force else (lambda: (t := self._news_target()) is not None and self._news_due(t[0]))
        elif name == "dispatch":
            condition = self.runner.calls.has_pending
        else:
            condition = None
        status = self.runner.request(name, trigger, condition=self._unless_stopped(condition), force=force)
        reasons = {
            "queued": "läuft gerade – ein weiterer Durchlauf ist vorgemerkt",
            "backoff": "nach Fehlern kurz pausiert",
            "disabled": "Redaktion ist ausgeschaltet",
        }
        return {"status": status, "reason": reasons.get(status)}

    @staticmethod
    def _unless_stopped(condition):
        """Re-checked right before the run and a queued follow-up: a Stop meanwhile cancels them."""
        return lambda: not is_stopped(load_config()) and (condition is None or condition())

    def watch(self) -> None:
        self.watch_news()  # also outside the broadcast window: a slot at its start is prepared before
        try:
            config = load_config()
            on_air = is_broadcast_time(config)  # never while stopped
            was_on_air, self._was_on_air = self._was_on_air, on_air
            was_stopped, self._was_stopped = self._was_stopped, is_stopped(config)
            if not on_air:
                return
            if was_on_air is False:
                logger.info("%s - music desk runs right away", "Sender gestartet" if was_stopped else "Broadcast start")
                self.request_run("music", "broadcast_start")
                return
            player = self.runner.player
            if player is not None and player.breaker_active():
                return  # the program can't be played right now - planning more won't help
            if self.below_threshold():
                # Re-checked before the run and before a follow-up: a block appended meanwhile
                # makes the queued run unnecessary.
                if self.under_cap():
                    status = self.runner.request(
                        "music", "fill", condition=lambda: self._music_may_run() and self.below_threshold()
                    )
                    if status == "started":
                        logger.info("Program below %s min - music desk started",
                                    self._music().settings["fill_threshold_minutes"])
        except Exception:
            logger.exception("Fill-level watcher failed")

    def watch_calls(self) -> None:
        try:
            calls = self.runner.calls
            minutes = DeskConfig.from_config("dispatch", load_config()).settings["reply_expires_minutes"]
            calls.expire_stale(int(minutes))
            if not self.runner.is_running("dispatch"):
                # Left `processing` by an error nobody caught - back to the dispatch desk.
                calls.recover_processing(older_than_minutes=STUCK_PROCESSING_MINUTES)
            calls.sync()
            if is_stopped(load_config()):
                return  # calls wait for Play (or expire above)
            if calls.has_pending() and not self.runner.is_running("dispatch"):
                self.runner.request("dispatch", "poll", condition=self._unless_stopped(calls.has_pending))
        except Exception:
            logger.exception("Calls watcher failed")

    def heartbeat(self) -> None:
        try:
            self.request_run("music", "heartbeat")
        except Exception:
            logger.exception("Heartbeat trigger failed")

    # ---------- status ----------

    def desk_status(self, name: str) -> dict:
        config = load_config()
        desk = DeskConfig.from_config(name, config)
        status = self.runner.status(name)
        data = {"name": name, "label": DESK_LABELS.get(name, name), "enabled": desk.enabled, **status}
        if name == "music":
            s = desk.settings
            heartbeat_at = self._heartbeat_job.next_run_time if self._heartbeat_job is not None else None
            data["next_trigger_at"] = status["backoff_until"] or (heartbeat_at.isoformat() if heartbeat_at else None)
            data["next_trigger"] = (
                "nächster Versuch nach Fehler" if status["backoff_until"]
                else f"wenn das Programm unter {s['fill_threshold_minutes']} min fällt, spätestens zum Heartbeat"
            )
            data["fill"] = {
                "remaining_seconds": round(self.runner.remaining_program_seconds()),
                "threshold_minutes": s["fill_threshold_minutes"],
                "cap_minutes": s["max_queued_program_minutes"],
                "block_minutes": s["block_minutes"],
            }
        elif name == "news":
            s = desk.settings
            target = next_slot(config, s)
            slot, fmt = target if target else (None, None)
            prepare_at = (slot.astimezone(timezone.utc) - timedelta(minutes=s["lead_minutes"])).astimezone() \
                if slot else None
            prepared = bool(slot) and self.runner.news_prepared(slot)
            data["next_slot_at"] = slot.isoformat() if slot else None
            data["next_slot_format"] = fmt
            data["prepare_at"] = prepare_at.isoformat() if prepare_at else None
            data["prepared"] = prepared
            data["last_bulletin"] = self.runner.last_bulletin()
            data["open_notes"] = len(self.runner.news_notes.open())
            data["slots"] = normalize_slots(s["slots"]) or []
            data["next_trigger_at"] = status["backoff_until"] or (
                None if prepared or prepare_at is None else prepare_at.isoformat())
            if status["backoff_until"]:
                data["next_trigger"] = "nächster Versuch nach Fehler"
            elif slot is None:
                data["next_trigger"] = "keine Ausgabe im Sendefenster"
            elif prepared:
                data["next_trigger"] = f"Ausgabe {slot.strftime('%H:%M')} ist vorbereitet"
            else:
                data["next_trigger"] = (f"bereitet {slot.strftime('%H:%M')} ({FORMAT_LABELS[fmt]}) um "
                                        f"{prepare_at.strftime('%H:%M')} vor")
        elif name == "dispatch":
            calls = self.runner.calls.all()
            data["open_calls"] = sum(c["status"] in ("new", "retrying", "processing") for c in calls)
            data["queued_calls"] = sum(c["status"] == "queued" for c in calls)
            data["last_call_at"] = calls[0]["created_at"] if calls else None
            data["next_trigger_at"] = status["backoff_until"]
            data["next_trigger"] = (
                "nächster Versuch nach Fehler" if status["backoff_until"] else "sofort bei jedem Zwischenruf"
            )
        if is_stopped(config):
            data["next_trigger_at"] = None
            data["next_trigger"] = "Sender gestoppt – erst nach Play"
        return data

    def desks_status(self) -> list[dict]:
        return [self.desk_status(name) for name in DESKS]
