from __future__ import annotations

import threading

from app.music.spotify_provider import SpotifyMusicProvider
from tests.conftest import track


class FakeSpotify:
    """Stands in for spotipy.Spotify: current_playback() replays `states` (an Exception is raised)."""

    def __init__(self, states):
        self.states = list(states)
        self.polls = 0

    def current_playback(self, **kwargs):
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


def test_episode_search_and_playback_poll():
    provider = _provider([])
    calls = []

    def search(q, type, limit, market=None):
        calls.append((q, type, market))
        return {"episodes": {"items": [None, {"id": "e1", "name": "Folge 12", "uri": "spotify:episode:e1",
                                              "duration_ms": 1_800_000, "release_date": "2026-09-20"}]}}

    provider.sp.search = search
    [episode] = provider.search_episodes("Lage der Nation")
    assert calls == [("Lage der Nation", "episode", "from_token")]
    assert (episode.uri, episode.title, episode.duration_seconds) == ("spotify:episode:e1", "Folge 12", 1800)
    assert provider.supports_episodes

    # The status poll asks for episodes too - otherwise a playing podcast reads as "stopped".
    seen = []
    provider.sp.current_playback = lambda **kw: seen.append(kw) or None
    provider.play_until(episode, threading.Event())
    assert seen == [{"additional_types": "track,episode"}]
