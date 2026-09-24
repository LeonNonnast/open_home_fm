"""Spotify provider, driving playback on the local raspotify Spotify Connect device.

Requires a Spotify Premium account (Web API playback control needs Premium) and an app
registered at developer.spotify.com with SPOTIFY_CLIENT_ID/SECRET in .env. raspotify exposes
this Pi as a Connect device named `music.spotify.device_name` in config.yaml - that's the
device we default to when no explicit device is requested.
"""
from __future__ import annotations

import logging
import threading
import time

import spotipy
from spotipy.exceptions import SpotifyOauthError
from spotipy.oauth2 import SpotifyOAuth

from app.config import ROOT_DIR
from app.music.base import Device, MusicProvider, PlaybackResult, Playlist, Track

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
    supports_episodes = True

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        device_name: str,
        volume_percent: int | None = 90,
    ):
        self.device_name = device_name
        self.volume_percent = volume_percent
        auth_manager = create_spotify_oauth(client_id, client_secret, redirect_uri)
        if auth_manager.cache_handler.get_cached_token() is None:
            logger.error("No Spotify login stored - Spotify calls will fail. %s", LOGIN_HINT)
        self.sp = spotipy.Spotify(auth_manager=auth_manager)

    def _resolve_device(self, device: Device | None) -> Device | None:
        devices = self.list_devices()
        if device is not None:
            # The agent sometimes passes a device name instead of the id from find_devices.
            wanted = device.id.casefold()
            for d in devices:
                if d.id == device.id or d.name.casefold() == wanted:
                    return d
            logger.warning("Requested Spotify device '%s' not found, falling back", device.id)
        configured = self.device_name.casefold()
        for d in devices:
            if d.name.casefold() == configured:
                return d
        if devices:
            logger.warning(
                "Spotify device '%s' not found (available: %s) - using the active device instead",
                self.device_name,
                ", ".join(d.name for d in devices),
            )
        # Prefer whatever the user is currently listening on over an arbitrary first device.
        for d in devices:
            if d.is_active:
                return d
        return devices[0] if devices else None

    def search_tracks(self, query: str, limit: int = 10) -> list[Track]:
        result = self.sp.search(q=query, type="track", limit=limit)
        items = result.get("tracks", {}).get("items", [])
        return [self._to_track(item) for item in items]

    def search_episodes(self, query: str, limit: int = 5) -> list[Track]:
        # Without a market Spotify answers episode searches with null items.
        result = self.sp.search(q=query, type="episode", limit=limit, market="from_token")
        items = (result.get("episodes") or {}).get("items") or []
        return [self._episode_to_track(item) for item in items if item]

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
        if uri.startswith("spotify:episode:"):
            try:
                return self._episode_to_track(self.sp.episode(uri, market="from_token"))
            except Exception:
                logger.warning("Could not resolve Spotify episode uri '%s'", uri, exc_info=True)
                return None
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
        target = self._resolve_device(device)
        if target is None:
            raise RuntimeError("No Spotify Connect device available (is raspotify running?)")
        if not target.is_active:
            # start_playback on an idle Connect device (e.g. raspotify after a restart) is often
            # ignored or answered with 404 - activating it via transfer first makes it reliable.
            self.sp.transfer_playback(device_id=target.id, force_play=False)
        logger.info("Playing on Spotify device %s: %s - %s", target.name, track.artist, track.title)
        self.sp.start_playback(device_id=target.id, uris=[track.uri])
        self._apply_volume(target)

    def _apply_volume(self, target: Device) -> None:
        # Set on every track, not just once: the moderator's TTS plays at a fixed level via ffplay,
        # so the music has to come back to the same level for the two to stay balanced.
        if self.volume_percent is None:
            return
        try:
            self.sp.volume(self.volume_percent, device_id=target.id)
        except Exception:
            logger.warning("Could not set Spotify volume to %d%%", self.volume_percent, exc_info=True)

    # Playback-state poll interval: tight enough to notice a song ending early, loose enough to
    # stay far away from the Web API rate limits. A stop request doesn't wait for it.
    POLL_SECONDS = 5
    # A failing status poll (network blip, 5xx/429, token refresh) doesn't end the song: keep
    # waiting until the nominal end, give up only after this many failed polls in a row (~30 s).
    MAX_POLL_ERRORS = 6

    def play_until(self, track: Track, stop_event: threading.Event, device: Device | None = None) -> PlaybackResult:
        self.play(track, device)
        started = time.monotonic()
        duration = track.duration_seconds or 180.0
        deadline = started + duration + 2
        position = 0.0
        poll_errors = 0
        # Poll playback state so we notice early skips/failures instead of always sleeping the
        # full nominal duration.
        while time.monotonic() < deadline:
            if stop_event.wait(self.POLL_SECONDS):
                try:
                    self.stop(device)
                except Exception:
                    logger.warning("Could not pause Spotify playback", exc_info=True)
                return PlaybackResult(finished=False, position_seconds=time.monotonic() - started)
            try:
                # additional_types: without "episode" a playing podcast shows up as item=None.
                state = self.sp.current_playback(additional_types="track,episode")
            except Exception as exc:
                poll_errors += 1
                if poll_errors >= self.MAX_POLL_ERRORS:
                    logger.error("Spotify playback state unavailable %d times in a row, giving up on '%s': %s",
                                 poll_errors, track.title, exc)
                    break
                logger.warning("Could not read Spotify playback state (%d/%d): %s",
                               poll_errors, self.MAX_POLL_ERRORS, exc)
                continue
            poll_errors = 0
            if not state or not state.get("is_playing"):
                break
            if (state.get("item") or {}).get("uri") != track.uri:
                break
            position = (state.get("progress_ms") or 0) / 1000
        return PlaybackResult(finished=True, position_seconds=max(position, time.monotonic() - started))

    def stop(self, device: Device | None = None) -> None:
        target = self._resolve_device(device)
        if target:
            self.sp.pause_playback(device_id=target.id)

    @staticmethod
    def _episode_to_track(item: dict) -> Track:
        show = (item.get("show") or {}).get("name") or ""
        return Track(
            id=item["id"],
            title=item["name"],
            artist=show,
            uri=item["uri"],
            duration_seconds=item.get("duration_ms", 0) / 1000 if item.get("duration_ms") else None,
            album=item.get("release_date"),
        )

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
