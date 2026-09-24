"""Decides when desks run. Replaces the old fixed interval (agent.loop_interval_seconds).

Music desk:
- fill-level watcher every 30 s: program left < `fill_threshold_minutes` ⇒ run,
- heartbeat every 60 min (picks up wishes, refreshes the reserve),
- right at the broadcast start.
The fill watcher holds off while the player's circuit breaker is tripped (source unreachable).

Dispatch desk: right away for every new call (the calls API asks), plus a watcher every 10 s
that syncs the calls with the queue, expires calls nobody could handle and re-requests a run
while calls are open (which also retries after an LLM failure, gated by the desk's backoff of
10 s / 30 s / 1 min). It ignores the broadcast window.
Every trigger respects the broadcast window and `max_queued_program_minutes` - neither the
watcher nor "run now" stacks the queue beyond that. Locking, follow-up runs and backoff are
the DeskRunner's job (app/agent/desk.py).
"""
from __future__ import annotations

import logging
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler

from app.agent.desk import DESK_LABELS, DESKS, DeskConfig, DeskRunner
from app.config import is_broadcast_time, load_config

logger = logging.getLogger(__name__)

WATCH_SECONDS = 30
HEARTBEAT_MINUTES = 60
CALLS_WATCH_SECONDS = 10


class DeskScheduler:
    def __init__(self, runner: DeskRunner):
        self.runner = runner
        self._scheduler = BackgroundScheduler()
        self._heartbeat_job = None
        self._was_on_air: bool | None = None

    def start(self) -> None:
        # next_run_time=None would add the job *paused* in APScheduler 3.x - check right away.
        self._scheduler.add_job(self.watch, "interval", seconds=WATCH_SECONDS, next_run_time=datetime.now(),
                                max_instances=1, coalesce=True)
        self._heartbeat_job = self._scheduler.add_job(self.heartbeat, "interval", minutes=HEARTBEAT_MINUTES,
                                                      max_instances=1, coalesce=True)
        self._scheduler.add_job(self.watch_calls, "interval", seconds=CALLS_WATCH_SECONDS,
                                next_run_time=datetime.now(), max_instances=1, coalesce=True)
        self._scheduler.start()
        logger.info("Desk scheduler started (fill watcher every %ds, heartbeat every %d min, calls every %ds)",
                    WATCH_SECONDS, HEARTBEAT_MINUTES, CALLS_WATCH_SECONDS)

    def stop(self) -> None:
        self._scheduler.shutdown(wait=False)

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

    # ---------- triggers ----------

    def request_run(self, name: str, trigger: str, force: bool = False) -> dict:
        """Runs desk `name` through its lock; for the API and all timed triggers."""
        if name not in DESKS:
            raise KeyError(name)
        if name == "music":
            if not is_broadcast_time(load_config()):
                return {"status": "skipped", "reason": "Sendepause – die Musikredaktion plant erst zum Sendebeginn."}
            if not self.under_cap():
                cap = self._music().settings["max_queued_program_minutes"]
                return {"status": "skipped", "reason": f"Warteschlange voll (Obergrenze {cap} Minuten Programm)."}
            condition = self._music_may_run
        elif name == "dispatch":
            condition = self.runner.calls.has_pending
        else:
            condition = None
        status = self.runner.request(name, trigger, condition=condition, force=force)
        reasons = {
            "queued": "läuft gerade – ein weiterer Durchlauf ist vorgemerkt",
            "backoff": "nach Fehlern kurz pausiert",
            "disabled": "Redaktion ist ausgeschaltet",
        }
        return {"status": status, "reason": reasons.get(status)}

    def watch(self) -> None:
        try:
            config = load_config()
            on_air = is_broadcast_time(config)
            was_on_air, self._was_on_air = self._was_on_air, on_air
            if not on_air:
                return
            if was_on_air is False:
                logger.info("Broadcast start - music desk runs right away")
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
            calls.sync()
            if calls.has_pending() and not self.runner.is_running("dispatch"):
                self.runner.request("dispatch", "poll", condition=calls.has_pending)
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
        elif name == "dispatch":
            calls = self.runner.calls.all()
            data["open_calls"] = sum(c["status"] in ("new", "retrying", "processing") for c in calls)
            data["queued_calls"] = sum(c["status"] == "queued" for c in calls)
            data["last_call_at"] = calls[0]["created_at"] if calls else None
            data["next_trigger_at"] = status["backoff_until"]
            data["next_trigger"] = (
                "nächster Versuch nach Fehler" if status["backoff_until"] else "sofort bei jedem Zwischenruf"
            )
        return data

    def desks_status(self) -> list[dict]:
        return [self.desk_status(name) for name in DESKS]
