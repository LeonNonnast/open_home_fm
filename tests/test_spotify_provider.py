from __future__ import annotations

import threading

from app.music.spotify_provider import SpotifyMusicProvider
from tests.conftest import track


class FakeSpotify:
    """Stands in for spotipy.Spotify: current_playback() replays `states` (an Exception is raised)."""

    def __init__(self, states):
        self.states = list(states)
        self.polls = 0

    def current_playback(self):
        self.polls += 1
        state = self.states.pop(0) if self.states else None
        if isinstance(state, Exception):
            raise state
        return state


def _provider(states) -> SpotifyMusicProvider:
    provider = SpotifyMusicProvider.__new__(SpotifyMusicProvider)  # no OAuth/token cache
    provider.sp = FakeSpotify(states)
    provider.play = lambda t, device=None: None
    provider.POLL_SECONDS = 0.001
    return provider


def test_status_poll_errors_do_not_end_the_song():
    song = track("Song", "Band", uri="spotify:track:1", duration=60)
    playing = {"is_playing": True, "item": {"uri": song.uri}, "progress_ms": 1000}
    provider = _provider([RuntimeError("502"), RuntimeError("429"), playing, RuntimeError("timeout"), playing, None])
    result = provider.play_until(song, threading.Event())
    # Kept waiting through the errors; only the real "stopped" state (None) ended it.
    assert result.finished and provider.sp.polls == 6


def test_gives_up_after_consecutive_poll_errors():
    song = track("Song", "Band", uri="spotify:track:1", duration=60)
    provider = _provider([RuntimeError("down")] * 20)
    result = provider.play_until(song, threading.Event())
    assert result.finished and provider.sp.polls == SpotifyMusicProvider.MAX_POLL_ERRORS
