# open home fm

Lokaler, plugin-basierter Radio-Agent für einen von einem Raspberry Pi gehosteten FM-Sender.

Ein Agent-Loop läuft in regelmäßigen Abständen, liest Hörerwünsche (Text oder per Sprache
eingereicht), nutzt Tools (Musiksuche, Wetter, News, eigene Plugins, ...) und baut daraus ein
Sendeprogramm ("Script") aus Songs und kurzen gesprochenen Einspielern. Ein davon unabhängiger,
deterministischer Player spielt dieses Script strikt der Reihe nach ab.

## Architektur

```
Web-UI (web/)  ──┐
                 ├─> FastAPI (app/main.py) ─┬─> AgentScheduler ─> AgentLoop (app/agent/loop.py)
Inbox (Text/STT) ┘                          │      │                 │
                                             │      │   LLM (Ollama/Anthropic) + Tools
                                             │      │   (Musik-Tools, TTS, Plugins aus ./plugins)
                                             │      │                 │
                                             │      │                 v
                                             │      │   data/playlists/current_script.json
                                             │      │                 │
                                             └─> ScriptPlayer (app/audio/player.py) <──┘
                                                    spielt das Script deterministisch ab
```

- **Generierung** (nicht-deterministisch): `AgentScheduler` weckt `AgentLoop.run_once()` alle
  `agent.loop_interval_seconds` (config.yaml). Der Loop liest neue Wünsche aus `data/inbox/`,
  lässt das LLM mit Tools arbeiten und ruft am Ende `set_playback_script` auf, was
  `data/playlists/current_script.json` schreibt.
- **Wiedergabe** (deterministisch): `ScriptPlayer` läuft als eigener Hintergrund-Thread, pollt
  diese Datei und spielt neue Scripts Segment für Segment ab - unabhängig davon, ob das LLM
  gerade läuft, langsam ist oder fehlschlägt.

## Plugin-System

Jedes Verzeichnis unter `plugins/<name>/` mit einer `manifest.yaml` (Tool-Name, Beschreibung,
JSON-Schema der Parameter) und einer `plugin.py` mit einer `execute(**kwargs) -> str` Funktion
wird automatisch als Tool für den Agenten registriert. Siehe `plugins/weather`, `plugins/news`,
`plugins/favorites`, `plugins/hue` als Beispiele. Plugins lassen sich einzeln über die Web-UI (oder
`plugins.disabled` in config.yaml) deaktivieren.

Braucht ein Plugin Einstellungen (Ort, Zugangsdaten, ...), definiert es zusätzlich
`install(setup) -> dict`: Der Installer fragt pro Plugin, ob es aktiv sein soll, und ruft dann
dessen `install()` auf, das über `setup.ask(...)`/`ask_choice`/`ask_yes_no`/`ask_secret` eigene
Fragen stellt. Das zurückgegebene Dict landet in `config.yaml` unter
`plugins.settings.<plugin-ordner>` und wird zur Laufzeit mit
`app.config.load_plugin_settings("<plugin-ordner>")` gelesen; Secrets gehören per
`setup.set_env(...)` in die `.env`. Details: `app/agent/plugin_setup.py`. Einzelne Plugins später
neu einrichten: `.venv/bin/python scripts/setup_plugins.py weather hue`.

Mitgelieferte Plugins mit Einrichtung:
- `weather`: fragt den Standort des Senders ab (mit Auswahl bei mehrdeutigen Orten wie
  "Hude") und speichert die Koordinaten.
- `hue`: Philips Hue - findet die Bridge im Netz, koppelt per Link-Taste (App-Key als
  `HUE_APP_KEY` in `.env`) und fragt einen Standard-Raum ab. Der Agent kann damit Räume/Lampen
  ein-/ausschalten, dimmen und färben (z.B. Stimmung passend zur Musik). Standardmäßig aus, bis
  es eingerichtet ist.

Mit `context: true` (+ optional `context_args`) im Manifest wird ein Plugin bei jedem
Durchlauf automatisch ausgeführt und sein Ergebnis direkt in den Input des Agenten geschrieben -
es muss dafür nicht extra als Tool aufgerufen werden (bleibt aber zusätzlich aufrufbar, z.B. für
abweichende Parameter). `plugins/weather` und `plugins/news` nutzen das bereits.

## Lauf-Historie

Jeder Durchlauf speichert seine vollständige Nachrichten-Historie (System-/User-/Assistant-/
Tool-Nachrichten) unter `data/transcripts/`. Der Agent bekommt bei jedem neuen Durchlauf
automatisch eine kompakte Zusammenfassung der letzten drei Läufe (Wünsche, Ansage, gebautes
Script) in den Kontext geladen, sodass er bei unveränderter Lage dasselbe Programm beibehalten
oder es gezielt um Neues ergänzen kann. Sichtbar über den "Verlauf"-Bereich der Web-UI.

## Setup (Raspberry Pi)

### Voraussetzungen

Der Installer prüft diese Punkte als Erstes, zeigt pro Punkt `[ok]`/`[fehlt]` an und bietet an,
Fehlendes per apt nachzuinstallieren. Fehlt danach noch etwas, bricht er mit einer Anleitung ab.

| Voraussetzung | Wofür | Installation |
| --- | --- | --- |
| git | Klonen/Aktualisieren des Repos | `sudo apt-get install git` |
| curl | One-Liner-Install, raspotify-Installer | `sudo apt-get install curl` |
| Python ≥ 3.11 | App | Raspberry Pi OS Bookworm oder neuer (`python3 --version`) |
| python3-venv | Virtualenv unter `.venv/` | `sudo apt-get install python3-venv` |
| ffplay (ffmpeg) | Lokales Playback + Jingles | `sudo apt-get install ffmpeg` |

Nur für die jeweils gewählte Option (prüft der Installer am Ende und listet offene Punkte auf):

| Option | Voraussetzung | Installation |
| --- | --- | --- |
| Spotify | Spotify Premium + raspotify mit `LIBRESPOT_NAME` = `music.spotify.device_name` | `curl -sL https://dtcooper.github.io/raspotify/install.sh \| sh`, Name in `/etc/raspotify/conf` setzen, `sudo systemctl restart raspotify` (übernimmt der Installer) |
| Piper-TTS | `piper` + Stimmmodell (`.onnx` + `.onnx.json`) | Der Installer installiert `piper-tts` in die venv, lässt eine deutsche Stimme auswählen (mit Hörprobe) und lädt sie nach `models/tts/` |
| Hue-Plugin | Philips Hue Bridge im selben Netz | Installer koppelt per Link-Taste |

### Schnellstart

Ein einziger Befehl - klont das Repo (falls noch nicht vorhanden) und startet den Installer:

```
bash -c "$(curl -fsSL https://raw.githubusercontent.com/LeonNonnast/open_home_fm/main/install.sh)"
```

(Bewusst `bash -c "$(curl ...)"` statt `curl ... | bash` - bei einer Pipe würde bash die
interaktiven Rückfragen nicht mehr stellen können, weil die Pipe bereits vom Herunterladen des
Scripts belegt ist.) Landet standardmäßig in `./open_home_fm` im aktuellen Verzeichnis; ein
anderer Zielordner lässt sich per `OPEN_HOME_FM_DIR=/pfad bash -c "..."` vorgeben.

Alternativ manuell:

```
git clone https://github.com/LeonNonnast/open_home_fm.git
cd open_home_fm
./install.sh
```

Das Script prüft die [Voraussetzungen](#voraussetzungen), installiert die Python-Abhängigkeiten, fragt interaktiv die nötige
Konfiguration ab (LLM-Provider + API-Key, Musikquelle, Stimme, Audio-Ausgang, Loop-Intervall,
Plugins) und schreibt `.env` sowie `config/config.yaml` entsprechend. Danach direkt startklar:

```
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Web-UI dann unter `http://<pi-ip>:8000/` erreichbar. Der Installer ist beliebig oft erneut
ausführbar (z.B. um die Musikquelle später zu wechseln) und schlägt dabei die zuletzt
eingetragenen Werte als Default vor.

### Update

```
./install.sh --update
```

Holt den neuen Code, aktualisiert die Python-Abhängigkeiten und startet den systemd-Service
`open-home-fm` neu - ohne Rückfragen. Über das Web-UI geänderte Einstellungen in
`config/config.yaml` bleiben erhalten, neu hinzugekommene Optionen werden mit ihren Defaults
ergänzt. Ein normales `./install.sh` bietet bei bestehender Installation dasselbe Schnell-Update
als Standard an.

### Manuelles Setup / Details

1. **System-Pakete**: `sudo apt install python3-venv ffmpeg` (ffmpeg liefert `ffplay`, das
   lokale Playback + Jingle-Wiedergabe nutzen).
2. **Python-Umgebung**:
   ```
   python3 -m venv .venv
   .venv/bin/pip install -e .
   ```
3. **Konfiguration**: `cp .env.example .env` und Zugangsdaten eintragen, die du nutzen willst
   (Ollama Cloud API-Key, Anthropic-Key, Spotify Client-ID/Secret).
4. **Musikquelle wählen** (`config/config.yaml` → `music.provider`):
   - `local`: Audiodateien nach `data/library/<Playlist-Ordner>/*.mp3` legen.
   - `spotify`: [raspotify](https://github.com/dtcooper/raspotify) installieren (macht den Pi zu
     einem Spotify-Connect-Gerät) und in `/etc/raspotify/conf` `LIBRESPOT_NAME="open-home-fm"`
     setzen (muss zu `music.spotify.device_name` passen), Spotify-App unter developer.spotify.com anlegen (Redirect-URI:
     `http://127.0.0.1:8888/callback`), Zugangsdaten in `.env` eintragen und einmalig
     `.venv/bin/python scripts/spotify_login.py` ausführen (funktioniert auch headless: Link auf
     PC/Handy öffnen, weitergeleitete URL zurück ins Terminal kopieren). Der Installer erledigt
     das automatisch. Spotify Premium nötig für Playback-Steuerung über die Web API.
5. **TTS (Piper, lokal)**: `.venv/bin/pip install piper-tts` und ein Stimmmodell (`.onnx` +
   `.onnx.json`) von [rhasspy/piper-voices](https://huggingface.co/rhasspy/piper-voices) nach
   `models/tts/` legen, Pfade in `config.yaml` (`tts.piper.binary`, `tts.piper.voice_model`)
   eintragen. Der Installer erledigt beides inkl. Stimmauswahl und Hörprobe. Danach lässt sich
   die Stimme im Web-UI (Karte „Stimme“) weiter anpassen: weitere deutsche Stimmen laden,
   Sprecher (bei Mehrsprecher-Modellen), Tempo, Tonhöhe und Hall - mit Hörprobe im Browser und
   Voreinstellungen wie „Jarvis“. Tonhöhe und Hall rechnet `ffmpeg` nach.
6. **STT (faster-whisper, lokal)**: Kein manueller Download nötig - das Modell wird beim ersten
   Transkriptions-Aufruf automatisch heruntergeladen (Internetverbindung beim ersten Mal nötig).
7. **Audio-Ausgang**: `audio.output_device` in config.yaml auf den ALSA/Pulse-Sink zeigen lassen,
   der die eigentliche FM-Sendekette (z.B. ein PiFmRds-Setup) speist.
8. **Starten**:
   ```
   .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
   ```
   Für Dauerbetrieb einen systemd-Service einrichten, der diesen Befehl beim Boot startet.

## Entwicklung

- Manuellen Loop-Durchlauf testen (ohne Scheduler): `python scripts/run_agent_once.py`
- Agent-Verhalten anpassen: `config/system_prompt.md` (auch über die Web-UI editierbar)
- Neues Tool hinzufügen: neuen Ordner unter `plugins/` mit `manifest.yaml` + `plugin.py` anlegen
