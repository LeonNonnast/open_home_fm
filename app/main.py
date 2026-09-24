"""FastAPI entrypoint: serves the web UI and the API, and wires up the program queue, the queue
player and the desks with their scheduler.

Run with: uvicorn app.main:app --host 0.0.0.0 --port 8000
Exactly ONE worker: locks, the desk status and the player status live in this process's memory.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.agent.desk import DeskRunner
from app.api.routes_config import router as config_router
from app.api.routes_desks import router as desks_router
from app.api.routes_inbox import router as inbox_router
from app.api.routes_music import router as music_router
from app.api.routes_plugins import router as plugins_router
from app.api.routes_queue import player_router, router as queue_router
from app.api.routes_status import notices_router, router as status_router
from app.api.routes_transcripts import router as transcripts_router
from app.api.routes_voice import router as voice_router
from app.audio.player import QueuePlayer
from app.audio.stt import create_stt_engine
from app.config import DATA_DIR, ROOT_DIR, load_config
from app.migrate import migrate_agent_settings, migrate_user_data
from app.music import create_music_provider
from app.program.queue import ProgramQueue
from app.scheduler import DeskScheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    notices = []
    # For installations updated with a plain `git pull` instead of install.sh --update.
    try:
        migrate_user_data()
    except Exception:
        logger.exception("Migrating local settings into data/ failed - continuing with what's there")
    try:
        if any(c.startswith("agent.") for c in migrate_agent_settings()):
            notices.append({
                "id": "takt-entfaellt",
                "text": "Der feste Takt entfällt: die Musikredaktion plant jetzt nach, sobald das "
                        "Programm knapp wird.",
            })
    except Exception:
        logger.exception("Migrating agent settings to desks.music failed")
    config = load_config()

    queue = ProgramQueue(DATA_DIR / "queue.json", DATA_DIR / "player_cursor.json")
    # The player owns its own MusicProvider instance/connection, separate from the one(s) the
    # desks create per run, so playback is never blocked on a planning run.
    player = QueuePlayer(
        provider=create_music_provider(config),
        queue=queue,
        play_history_path=DATA_DIR / "playlists" / "play_history.json",
        reserve_path=DATA_DIR / "reserve.json",
    )
    runner = DeskRunner(ROOT_DIR, queue, player=player)
    scheduler = DeskScheduler(runner)

    app.state.queue = queue
    app.state.player = player
    app.state.desk_runner = runner
    app.state.scheduler = scheduler
    app.state.notices = notices
    app.state.stt_engine = create_stt_engine(config)

    player.start()
    scheduler.start()
    logger.info("open-home-fm started")

    yield

    scheduler.stop()
    player.stop()
    logger.info("open-home-fm stopped")


app = FastAPI(title="open home fm", lifespan=lifespan)

app.include_router(config_router)
app.include_router(desks_router)
app.include_router(inbox_router)
app.include_router(music_router)
app.include_router(plugins_router)
app.include_router(queue_router)
app.include_router(player_router)
app.include_router(status_router)
app.include_router(notices_router)
app.include_router(transcripts_router)
app.include_router(voice_router)

app.mount("/", StaticFiles(directory=str(ROOT_DIR / "web"), html=True), name="web")
