"""FastAPI entrypoint: serves the web UI, the config/inbox/plugin/status API, and wires up the
background agent scheduler + deterministic script player.

Run with: uvicorn app.main:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.agent.loop import AgentLoop
from app.api.routes_config import router as config_router
from app.api.routes_inbox import router as inbox_router
from app.api.routes_plugins import router as plugins_router
from app.api.routes_status import router as status_router
from app.api.routes_transcripts import router as transcripts_router
from app.api.routes_voice import router as voice_router
from app.audio.player import ScriptPlayer
from app.audio.stt import create_stt_engine
from app.config import ROOT_DIR, load_config
from app.music import create_music_provider
from app.scheduler import AgentScheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = load_config()

    agent_loop = AgentLoop(ROOT_DIR)
    scheduler = AgentScheduler(agent_loop)

    # The player owns its own MusicProvider instance/connection, separate from the one(s) the
    # agent loop creates per-tick, so playback is never blocked on a generation run.
    player = ScriptPlayer(
        provider=create_music_provider(config),
        script_path=agent_loop.script_path,
        play_history_path=agent_loop.play_history_path,
    )

    app.state.agent_loop = agent_loop
    app.state.scheduler = scheduler
    app.state.script_player = player
    app.state.stt_engine = create_stt_engine(config)

    scheduler.start()
    player.start()
    logger.info("open-home-fm started")

    yield

    scheduler.stop()
    player.stop()
    logger.info("open-home-fm stopped")


app = FastAPI(title="open home fm", lifespan=lifespan)

app.include_router(config_router)
app.include_router(inbox_router)
app.include_router(plugins_router)
app.include_router(status_router)
app.include_router(transcripts_router)
app.include_router(voice_router)

app.mount("/", StaticFiles(directory=str(ROOT_DIR / "web"), html=True), name="web")
