from __future__ import annotations

import os

from app.config import resolve_path
from app.music.base import MusicProvider


def create_music_provider(config: dict) -> MusicProvider:
    music_cfg = config.get("music", {})
    provider = music_cfg.get("provider", "local")

    if provider == "spotify":
        from app.music.spotify_provider import SpotifyMusicProvider

        spotify_cfg = music_cfg.get("spotify", {})
        return SpotifyMusicProvider(
            client_id=os.environ["SPOTIFY_CLIENT_ID"],
            client_secret=os.environ["SPOTIFY_CLIENT_SECRET"],
            redirect_uri=os.environ.get(
                "SPOTIFY_REDIRECT_URI", "http://localhost:8000/api/music/spotify/callback"
            ),
            device_name=spotify_cfg.get("device_name", "open-home-fm"),
        )

    if provider == "local":
        from app.music.local_provider import LocalMusicProvider

        local_cfg = music_cfg.get("local", {})
        audio_cfg = config.get("audio", {})
        return LocalMusicProvider(
            library_path=resolve_path(local_cfg.get("library_path", "data/library")),
            output_device=audio_cfg.get("output_device", "default"),
        )

    raise ValueError(f"Unknown music provider: {provider}")
