# open home fm

Lokaler, plugin-basierter Radio-Agent für einen von einem Raspberry Pi gehosteten FM-Sender.

Eine KI-Musikredaktion plant das Programm in Blöcken aus Songs und kurzen gesprochenen
Einspielern und hängt sie an eine fortlaufende Warteschlange an - immer dann, wenn das
eingeplante Programm knapp wird. Ein davon unabhängiger, deterministischer Player spielt die
Warteschlange durchgehend ab; fällt das LLM aus, läuft Füllprogramm weiter.

## Architektur

```
Web-UI (web/)  ──┐
                 ├─> FastAPI (app/main.py) ─┬─> DeskScheduler (app/scheduler.py)
Inbox (Text/STT) ┘                          │     Füllstand-Watcher 30 s, Heartbeat 60 min, Sendebeginn
                                             │      │
                                             │      v
                                             │   DeskRunner (app/agent/desk.py): Musikredaktion
                                             │     LLM (Ollama/Anthropic) + Tools (Musik, TTS, Plugins)
                                             │      │ append_program_block / update_reserve
                                             │      v
                                             │   data/queue.json (+ player_cursor.json, reserve.json)
                                             │      │
                                             └─> QueuePlayer (app/audio/player.py)
                                                    spielt Segment für Segment, Füllprogramm, Skip
```

- **Planung** (nicht-deterministisch): Es gibt keinen festen Takt mehr. Der Scheduler prüft
  alle 30 s den Füllstand; sinkt das eingeplante Programm unter
  `desks.music.fill_threshold_minutes` (Standard 10), plant die Musikredaktion einen Block von
  `block_minutes` (20) und hängt ihn hinten an - höchstens bis `max_queued_program_minutes`
  (45). Zusätzlich läuft sie zum Sendebeginn, stündlich (Heartbeat) und bei neuen Wünschen.
  Songs aus den letzten `no_repeat_minutes` und bereits eingeplante Songs werden entfernt,
  Ansagen höchstens alle `songs_per_announcement` Songs, auch über Blockgrenzen hinweg - die
  Redaktion knüpft an statt neu zu begrüßen. Nach Fehlern pausiert sie 1, 2, 5, 10 min.
- **Wiedergabe** (deterministisch): `QueuePlayer` läuft als eigener Hintergrund-Thread und wählt
  vor jedem Segment neu (Spuren `urgent` > `reply` > `news` > `program` > `filler`). Nach einem
  Neustart geht es mit dem nächsten Segment weiter. Songs zählen erst nach 30 s als gespielt.
  Brechen 3 Segmente in Folge nach < 10 s ab (z.B. Spotify-Gerät weg), pausiert der Player 60 s.
- **Füllprogramm**: Ist kein Programm da, spielt der Player die **Reserve** (`data/reserve.json`,
  15-20 Songs, bei jedem Lauf von der Musikredaktion gepflegt), danach zufällige Songs aus den
  **Lieblings-Playlists** (`music.favorite_playlists`, Playlist-IDs bzw. Ordnernamen der
  lokalen Bibliothek) oder - lokal ohne Favoriten - aus der ganzen Bibliothek. Sobald ein
  Programmblock da ist, übernimmt er am nächsten Song-Ende.
- **Ein Prozess, ein Worker**: Locks, Redaktions- und Player-Status liegen im Speicher - uvicorn
  immer mit genau einem Worker starten (Standard; kein `--workers`).

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

Welche Plugins eine Redaktion nutzen darf, steht in `desks.<redaktion>.plugins`; Plugins in
`desks.<redaktion>.context_plugins` werden bei jedem Durchlauf automatisch (mit den
`context_args` aus dem Manifest) ausgeführt und ihr Ergebnis direkt in den Input geschrieben -
sie müssen dafür nicht extra als Tool aufgerufen werden (bleiben aber zusätzlich aufrufbar, z.B.
für abweichende Parameter). Die Musikredaktion lädt standardmäßig das Wetter automatisch; die
Hue-Steuerung ist für sie nicht freigegeben (kommt mit der Leitstelle).

## Lauf-Historie

Jeder Durchlauf speichert seine vollständige Nachrichten-Historie (System-/User-/Assistant-/
Tool-Nachrichten) unter `data/transcripts/`, je Redaktion (`desk`). Die Redaktion bekommt bei
jedem neuen Durchlauf automatisch eine kompakte Zusammenfassung ihrer letzten drei Läufe
(Wünsche, Ansage, eingeplante Segmente) in den Kontext geladen, dazu das Ende der
Warteschlange, sodass sie das Programm fortsetzt statt es zu wiederholen. Sichtbar über den
Tab „Verlauf“ unter „Redaktion“ in der Web-UI (`GET /api/transcripts?desk=music`).

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
Konfiguration ab (LLM-Provider + API-Key, Musikquelle, Stimme, Audio-Ausgang,
Plugins) und schreibt `.env` sowie `data/config.yaml` entsprechend. Danach direkt startklar:

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
`open-home-fm` neu - ohne Rückfragen. Ein normales `./install.sh` bietet bei bestehender
Installation dasselbe Schnell-Update als Standard an.

### Konfiguration

- `config/config.yaml` enthält nur die **Defaults** (im Repo, nicht selbst bearbeiten).
- `data/config.yaml` (nicht im Repo) enthält **nur deine Abweichungen** davon - Web-UI und
  Installer schreiben dorthin. Zur Laufzeit wird beides zusammengeführt; geänderte Defaults eines
  Updates erreichen so alle Werte, die du nie angefasst hast. Von Hand ändern: nur den jeweiligen
  Schlüssel (mit seinen Eltern-Schlüsseln) in `data/config.yaml` eintragen; Schlüssel löschen =
  zurück zum Default.
- Prompt: Standard in `config/desks/music.md`, ein im Web-UI angepasster liegt in
  `data/prompts/music.md` („Auf Standard zurücksetzen“ löscht ihn wieder).
- Ältere Installationen, bei denen das Web-UI noch in `config/config.yaml`/`config/system_prompt.md`
  geschrieben hat, werden beim Update (oder App-Start nach einem manuellen `git pull`) automatisch
  umgezogen: die lokalen Werte landen in `data/`, die Repo-Dateien werden zurückgesetzt. Manuell:
  `.venv/bin/python -m app.migrate`. Beim App-Start werden außerdem `agent.max_tool_iterations`/
  `agent.no_repeat_minutes` nach `desks.music.*` verschoben; `agent.loop_interval_seconds` entfällt.

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
4. **Musikquelle wählen** (`data/config.yaml` → `music.provider`):
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
   die Stimme im Web-UI (Technik → Karte „Stimme“) weiter anpassen: weitere deutsche Stimmen laden,
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
   Für Dauerbetrieb einen systemd-Service einrichten, der diesen Befehl beim Boot startet
   (genau ein Worker, siehe [Architektur](#architektur)).

## Entwicklung

- Manuellen Redaktions-Durchlauf testen (ohne Scheduler, hängt an `data/queue.json` an):
  `python scripts/run_agent_once.py [music]`
- Agent-Verhalten anpassen: Prompt über die Web-UI, Redaktion → Tab „Prompt“ (landet in `data/prompts/music.md`, Standard:
  `config/desks/music.md`)
- Defaults in `config/config.yaml` ändern: vor dem nächsten App-Start/Update committen - eine
  uncommittete Änderung hält die Migration sonst für eine Nutzereinstellung und zieht sie nach
  `data/config.yaml`.
- Tests: `.venv/bin/pip install -e ".[dev]"`, dann `.venv/bin/pytest` (`-m "not shell"` überspringt
  die Update-Simulation mit `install.sh`)
- Neues Tool hinzufügen: neuen Ordner unter `plugins/` mit `manifest.yaml` + `plugin.py` anlegen
