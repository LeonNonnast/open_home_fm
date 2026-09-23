# TODO

## Spotify Soloist als Ersatz/Ergänzung für raspotify

https://github.com/spotify/soloist - offizieller, headless Spotify-Connect-Client von Spotify
selbst (Blogpost 2026-08-13), gebaut auf derselben Playback-Engine wie die offiziellen Apps.
Läuft auf ARMv7/AArch64/x86_64, explizit für Raspberry Pi/DIY-Setups gedacht.

**Warum interessant:**
- Lokale WebSocket-API (`--ws ADDR:PORT`) mit Commands (`play`/`pause`/`skip_next`/`skip_prev`/
  `seek`/`set_volume`/`set_shuffle`/`set_repeat_*`/`add_to_queue`/`activate`/`deactivate`) und
  Events (`track_changed`, `playback_state`, `queue_changed`, ...).
- Könnte `SpotifyMusicProvider.play_and_wait` von duration-basiertem Sleep + Web-API-Polling auf
  echte `track_changed`-Events umstellen - genauer für den deterministischen Player.
- Einfacheres Auth-Modell für ein Headless-Gerät: im Spotify-for-Developers-Dashboard erzeugter
  API-Key statt OAuth-Browser-Flow.
- Könnte `raspotify` als Connect-Device-Komponente ersetzen (offizieller Client statt
  Community-Fork von librespot).

**Einschränkungen (Stand jetzt):**
- Keine Suche, keine Playlist-Verwaltung, keine Multi-Device-Koordination über die WebSocket-API
  - `spotipy`/Web API bleibt für `search_songs`/`get_playlists`/`get_playlist_tracks` nötig.
  - Soloist würde also nur die Playback-/Geräte-Ebene übernehmen, nicht den ganzen
    `SpotifyMusicProvider` ersetzen.
- Sehr neu, laut Doku nur auf Raspberry Pi 3 Model A+ getestet.
- WebSocket hat bewusst keine Auth/TLS ("nur lokal nutzen") - passt zu unserem Threat-Model, aber
  im Hinterkopf behalten.
- Binaries laufen nach 90 Tagen ab, müssen neu heruntergeladen werden - zusätzlicher
  Wartungsaufwand (z.B. per Cron/systemd-Timer automatisierbar).

**Nächste Schritte, falls verfolgt:**
1. Spotify-for-Developers-Zugang für Soloist einrichten, Testinstallation auf einem Pi/lokal.
2. Prototyp: `SoloistMusicProvider` (oder Erweiterung von `SpotifyMusicProvider`), die für
   `play`/`play_and_wait`/`stop`/`list_devices` die lokale WebSocket-API nutzt, aber für
   `search_tracks`/`list_playlists`/`get_playlist_tracks`/`get_track_by_uri` weiterhin spotipy
   gegen die Web API spricht.
3. Client für den 90-Tage-Rebuild-Zyklus einplanen (Doku/Reminder oder Automatisierung).

Nicht dringend - der aktuelle raspotify+spotipy-Ansatz funktioniert bereits.
