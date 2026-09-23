"""Wakes the agent loop up on a fixed interval (agent.loop_interval_seconds in config.yaml)."""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler

from app.agent.loop import AgentLoop
from app.config import is_broadcast_time, load_config

logger = logging.getLogger(__name__)


class AgentScheduler:
    def __init__(self, agent_loop: AgentLoop):
        self.agent_loop = agent_loop
        self._scheduler = BackgroundScheduler()
        self._job = None

    def _tick(self) -> None:
        config = load_config()
        if not is_broadcast_time(config):
            logger.info("Outside configured broadcast hours, skipping agent loop tick")
            return
        try:
            logger.info("Agent loop tick starting")
            self.agent_loop.run_once()
            logger.info("Agent loop tick finished")
        except Exception:
            logger.exception("Agent loop tick failed")

    def start(self) -> None:
        interval = load_config().get("agent", {}).get("loop_interval_seconds", 300)
        self._job = self._scheduler.add_job(self._tick, "interval", seconds=interval, next_run_time=None)
        self._scheduler.start()
        logger.info("Agent scheduler started, interval=%ds", interval)

    def reschedule(self, interval_seconds: int) -> None:
        if self._job is not None:
            self._job.reschedule(trigger="interval", seconds=interval_seconds)
            logger.info("Agent scheduler rescheduled, interval=%ds", interval_seconds)

    def stop(self) -> None:
        self._scheduler.shutdown(wait=False)
