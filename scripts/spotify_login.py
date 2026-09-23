#!/usr/bin/env python3
"""One-time interactive Spotify login - stores the token the (headless) service then refreshes.

Works without a browser on the Pi: open the printed link on any device, log in, and paste the
URL you get redirected to (the page itself won't load - that's expected) back into the terminal.

Credentials come from SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET / SPOTIFY_REDIRECT_URI (the
installer passes them as env vars, otherwise they're read from .env).

Usage: .venv/bin/python scripts/spotify_login.py [--force]
  --force  log in again even if a valid login is already stored

Exit code 0 = logged in, 1 = login failed (message says why), 2 = credentials missing.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import spotipy  # noqa: E402
from spotipy.exceptions import SpotifyException, SpotifyOauthError  # noqa: E402

from app.music.spotify_provider import TOKEN_CACHE_PATH, create_spotify_oauth  # noqa: E402

DEFAULT_REDIRECT_URI = "http://127.0.0.1:8888/callback"


def fail(message: str) -> int:
    print(f"\n[FEHLER] {message}", file=sys.stderr)
    return 1


def describe_oauth_error(err: SpotifyOauthError) -> str:
    if err.error == "invalid_client":
        return "Spotify lehnt Client-ID oder Client-Secret ab - bitte im Developer-Dashboard prüfen."
    if err.error == "invalid_grant":
        return (
            "Der Code ist ungültig oder abgelaufen, oder die Redirect-URI passt nicht exakt zu der "
            "im Dashboard. Bitte erneut versuchen (der Code ist nur einmal und kurz gültig)."
        )
    if err.error == "access_denied":
        return "Der Zugriff wurde im Browser abgelehnt."
    return f"Spotify-Login fehlgeschlagen: {err}"


def verify(auth) -> str | None:
    """Returns an error message, or None if the stored login actually works."""
    try:
        user = spotipy.Spotify(auth_manager=auth).current_user()
    except SpotifyOauthError as err:
        return describe_oauth_error(err)
    except SpotifyException as err:
        if err.http_status == 403:
            return (
                "Spotify verweigert den Zugriff (403). Ist die App im Development Mode, muss dein "
                "Spotify-Account im Dashboard unter 'User Management' eingetragen sein."
            )
        return f"Spotify-API-Fehler: {err}"
    except Exception as err:  # network errors etc.
        return f"Verbindung zu Spotify fehlgeschlagen: {err}"
    print(f"\n[OK] Eingeloggt als {user.get('display_name') or user.get('id')}.")
    print(f"     Token gespeichert unter {TOKEN_CACHE_PATH}")
    return None


def main() -> int:
    force = "--force" in sys.argv[1:]
    client_id = os.environ.get("SPOTIFY_CLIENT_ID", "").strip()
    client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET", "").strip()
    redirect_uri = os.environ.get("SPOTIFY_REDIRECT_URI", "").strip() or DEFAULT_REDIRECT_URI
    if not client_id or not client_secret:
        print("[FEHLER] SPOTIFY_CLIENT_ID und SPOTIFY_CLIENT_SECRET müssen gesetzt sein.", file=sys.stderr)
        return 2

    auth = create_spotify_oauth(client_id, client_secret, redirect_uri, interactive=True)

    if not force and auth.cache_handler.get_cached_token() is not None:
        # Refreshing the stored token also proves the current Client-ID/Secret are valid.
        if verify(auth) is None:
            print("     Bestehender Login ist gültig - kein neuer Login nötig.")
            return 0
        print("Gespeicherter Login ist nicht mehr gültig - neuer Login nötig.")

    print("\nSpotify-Login")
    print(f"  Im Spotify-Dashboard muss als Redirect-URI exakt eingetragen sein: {redirect_uri}")
    print("\n  1. Diesen Link auf einem beliebigen Gerät (PC/Handy) öffnen und einloggen:\n")
    print(f"     {auth.get_authorize_url()}\n")
    print("  2. Danach landest du auf einer Seite, die nicht lädt - das ist richtig so.")
    print("     Die komplette URL aus der Adresszeile kopieren (beginnt mit")
    print(f"     {redirect_uri}?code=...) und hier einfügen.")
    print("  Zeigt Spotify stattdessen 'INVALID_CLIENT' o.ä. an, stimmen Client-ID oder")
    print("  Redirect-URI nicht - dann einfach Enter drücken und die Daten korrigieren.\n")

    try:
        response = input("Weitergeleitete URL: ").strip()
    except (EOFError, KeyboardInterrupt):
        return fail("Login abgebrochen.")
    if not response:
        return fail("Keine URL eingegeben - Login abgebrochen.")

    try:
        _, code = auth.parse_auth_response_url(response)
    except SpotifyOauthError as err:
        return fail(describe_oauth_error(err))
    if not code:
        return fail(
            "In der eingefügten URL steckt kein '?code=...'. Bitte die komplette URL aus der "
            "Adresszeile kopieren, nicht den Link von oben."
        )

    try:
        auth.get_access_token(code, as_dict=False, check_cache=False)
    except SpotifyOauthError as err:
        return fail(describe_oauth_error(err))
    except Exception as err:  # network errors etc.
        return fail(f"Verbindung zu Spotify fehlgeschlagen: {err}")

    error = verify(auth)
    return fail(error) if error else 0


if __name__ == "__main__":
    sys.exit(main())
