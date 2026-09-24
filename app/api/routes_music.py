"""Music source helpers for the settings page: the playlists to choose favorites from."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Request

from app.config import load_config
from app.music import create_music_provider

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/music", tags=["music"])


@router.get("/playlists")
def get_playlists(request: Request) -> dict:
    """Playlists of the configured music source (Spotify playlists or local library folders).

    Reuses the player's provider when it's the configured one (no second library scan or Spotify
    login); a source that can't be reached yields an empty list plus `error` instead of a 500."""
    config = load_config()
    wanted = config.get("music", {}).get("provider", "local")
    player = getattr(request.app.state, "player", None)
    provider = getattr(player, "provider", None)
    try:
        if provider is None or getattr(provider, "name", None) != wanted:
            provider = create_music_provider(config)
        playlists = provider.list_playlists()
    except Exception as exc:  # noqa: BLE001 - shown in the UI, e.g. Spotify not logged in
        logger.warning("Listing playlists failed", exc_info=True)
        return {"provider": wanted, "playlists": [], "error": str(exc) or exc.__class__.__name__}
    return {
        "provider": wanted,
        "playlists": [{"id": p.id, "name": p.name, "track_count": p.track_count} for p in playlists],
        "error": None,
    }
