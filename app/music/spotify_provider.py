"""Spotify provider, driving playback on the local raspotify Spotify Connect device.

Requires a Spotify Premium account (Web API playback control needs Premium) and an app
registered at developer.spotify.com with SPOTIFY_CLIENT_ID/SECRET in .env. raspotify exposes
this Pi as a Connect device named `music.spotify.device_name` in config.yaml - that's the
device we default to when no explicit device is requested.
"""
from __future__ import annotations

import logging
import time

import spotipy
from spotipy.exceptions import SpotifyOauthError
from spotipy.oauth2 import SpotifyOAuth

from app.config import ROOT_DIR
from app.music.base import Device, MusicProvider, Playlist, Track

logger = logging.getLogger(__name__)

SCOPE = "user-read-playback-state user-modify-playback-state playlist-read-private"
TOKEN_CACHE_PATH = ROOT_DIR / ".spotify_token_cache"
LOGIN_HINT = "Run `.venv/bin/python scripts/spotify_login.py` once to log in to Spotify."


class NonInteractiveSpotifyOAuth(SpotifyOAuth):
    """SpotifyOAuth that fails fast instead of starting an interactive login.

    Plain SpotifyOAuth starts a browser/local-server login whenever no cached token exists,
    which blocks forever on a headless Pi running as a service. The login is done once,
    interactively, via scripts/spotify_login.py; afterwards the cached refresh token is enough.
    """

    def get_auth_response(self, open_browser=None):
        raise SpotifyOauthError(f"No Spotify login stored. {LOGIN_HINT}")


def create_spotify_oauth(
    client_id: str, client_secret: str, redirect_uri: str, interactive: bool = False
) -> SpotifyOAuth:
    oauth_cls = SpotifyOAuth if interactive else NonInteractiveSpotifyOAuth
    return oauth_cls(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        scope=SCOPE,
        cache_path=str(TOKEN_CACHE_PATH),
        open_browser=False,
    )


class SpotifyMusicProvider(MusicProvider):
    name = "spotify"

    def __init__(self, client_id: str, client_secret: str, redirect_uri: str, device_name: str):
        self.device_name = device_name
        auth_manager = create_spotify_oauth(client_id, client_secret, redirect_uri)
        if auth_manager.cache_handler.get_cached_token() is None:
            logger.error("No Spotify login stored - Spotify calls will fail. %s", LOGIN_HINT)
        self.sp = spotipy.Spotify(auth_manager=auth_manager)

    def _resolve_device_id(self, device: Device | None) -> str | None:
        if device is not None:
            return device.id
        for d in self.list_devices():
            if d.name == self.device_name:
                return d.id
        devices = self.list_devices()
        return devices[0].id if devices else None

    def search_tracks(self, query: str, limit: int = 10) -> list[Track]:
        result = self.sp.search(q=query, type="track", limit=limit)
        items = result.get("tracks", {}).get("items", [])
        return [self._to_track(item) for item in items]

    def list_playlists(self) -> list[Playlist]:
        result = self.sp.current_user_playlists(limit=50)
        return [
            Playlist(id=p["id"], name=p["name"], track_count=p["tracks"]["total"])
            for p in result.get("items", [])
        ]

    def get_playlist_tracks(self, playlist_id: str) -> list[Track]:
        result = self.sp.playlist_items(playlist_id, additional_types=["track"])
        tracks = []
        for item in result.get("items", []):
            track = item.get("track")
            if track:
                tracks.append(self._to_track(track))
        return tracks

    def get_track_by_uri(self, uri: str) -> Track | None:
        try:
            item = self.sp.track(uri)
        except Exception:
            logger.warning("Could not resolve Spotify track uri '%s'", uri, exc_info=True)
            return None
        return self._to_track(item)

    def list_devices(self) -> list[Device]:
        result = self.sp.devices()
        return [
            Device(id=d["id"], name=d["name"], is_active=d["is_active"])
            for d in result.get("devices", [])
        ]

    def play(self, track: Track, device: Device | None = None) -> None:
        device_id = self._resolve_device_id(device)
        if device_id is None:
            raise RuntimeError("No Spotify Connect device available (is raspotify running?)")
        logger.info("Playing on Spotify device %s: %s - %s", device_id, track.artist, track.title)
        self.sp.start_playback(device_id=device_id, uris=[track.uri])

    def play_and_wait(self, track: Track, device: Device | None = None) -> None:
        self.play(track, device)
        duration = track.duration_seconds or 180.0
        deadline = time.time() + duration + 2
        # Poll playback state so we notice early skips/failures instead of always sleeping the
        # full nominal duration.
        while time.time() < deadline:
            time.sleep(5)
            state = self.sp.current_playback()
            if not state or not state.get("is_playing"):
                break
            if state.get("item", {}).get("uri") != track.uri:
                break

    def stop(self, device: Device | None = None) -> None:
        device_id = self._resolve_device_id(device)
        if device_id:
            self.sp.pause_playback(device_id=device_id)

    @staticmethod
    def _to_track(item: dict) -> Track:
        return Track(
            id=item["id"],
            title=item["name"],
            artist=", ".join(a["name"] for a in item.get("artists", [])),
            uri=item["uri"],
            duration_seconds=item.get("duration_ms", 0) / 1000 if item.get("duration_ms") else None,
            album=item.get("album", {}).get("name"),
        )
