# Implementierungsplan: Redaktionen, Warteschlange & Zwischenrufe

Status: überarbeitet nach Architektur- und UI-Review, Studio durch Leitstelle ersetzt · Stand 2026-09-24

## 1. Ziel

open home fm soll sich wie ein durchgehender Sender anhören statt wie eine Folge einzelner
Mini-Sendungen. Dafür wird der eine Agent in drei **Redaktionen** mit eigenem Auftrag, eigenem
Auslöser und eigenem Prompt aufgeteilt, der Player spielt eine **fortlaufende Warteschlange**
mit Prioritäten, und Hörer können jederzeit einen **Zwischenruf** schicken. Jeder Zwischenruf
startet sofort die **Leitstelle**: sie schätzt ein, wie eilig er ist, und leitet ihn in den
richtigen Kanal - vom Lichtschalten über den Musikwunsch und den Hinweis für die Nachrichten
bis zur Unwetterwarnung, die den laufenden Song unterbricht. Einfaches erledigt sie selbst.

### Probleme, die das löst (Ist-Zustand)

- Jeder Durchlauf ersetzt das komplette Programm; der Player springt am nächsten Song-Ende um.
  Kurzes Intervall ⇒ nach fast jedem Song eine neue Begrüßung. Langes Intervall ⇒ Stille.
- Moderationshäufigkeit ist implizit an das Generierungsintervall gekoppelt.
- Wünsche warten bis zum nächsten Takt (bis zu 30 min).
- Nachrichten gibt es nur, wenn das LLM gerade daran denkt.
- Web-UI-bearbeitete Dateien (`config.yaml`, `system_prompt.md`) liegen im Repo ⇒ Update-Konflikte.
- Keine automatisierten Tests.

### Leitplanken

- **Der Sender verstummt nie wegen der KI.** Fällt das LLM aus, läuft deterministisches
  Füllprogramm (Abschnitt 3.7).
- Ein uvicorn-Prozess mit **einem Worker** (Annahme für Locks und In-Memory-Status; im
  systemd-Unit und README festhalten).
- Hobby-Projekt: lieber eine Funktion weniger als eine halbfertige. Gestrichen für v1 sind
  in Abschnitt 9 aufgeführt.

## 2. Begriffe

| Begriff | Bedeutung |
|---|---|
| **Redaktion** (desk) | Eine Agent-Konfiguration: Prompt + Tool-Auswahl + Auslöser. Drei Stück: `music`, `news` (geplant) und die Leitstelle `dispatch` (spontan). |
| **Leitstelle** (dispatch) | Kleiner, schneller Agent-Lauf pro Zwischenruf: schätzt die Dringlichkeit ein und leitet weiter bzw. erledigt Einfaches selbst. Macht keine Ansagen auf Vorrat. |
| **Dringlichkeit** (urgency) | `sofort` (jetzt, unterbricht Musik) · `als Nächstes` (nach dem laufenden Song) · `demnächst` (über die Musikredaktion) · `Nachrichten` (nächste Ausgabe). |
| **Wunsch-Postfach** | `data/music_wishes.json`: „demnächst“-Wünsche mit `valid_until`, Kontext für Musikredaktion und Reserve. |
| **Meldungs-Postfach** | `data/news_notes.json`: Hinweise für die nächsten Nachrichten, mit `valid_until`. |
| **Beitrag** (queue item) | Eintrag in der Warteschlange: ein oder mehrere Segmente (Track/Jingle) mit Spur, Herkunft und optionalem Zeitfenster. |
| **Warteschlange** (queue) | Geordnete Liste aller geplanten Beiträge, persistent. Ersetzt `current_script.json`. |
| **Spur** (lane) | Priorität: `urgent` > `reply` > `news` > `program` (> `filler`). |
| **Zwischenruf** (call) | Eingang vom Hörer (Text/Sprache). Ersetzt die heutigen „Wünsche“. |

## 3. Zielarchitektur

```
 Hörer ──► /api/calls (Text/Sprache)
               │  sofortiger Trigger bei jedem Zwischenruf
               ▼
        ┌─────────────┐ ── Plugin-Aktion (Hue …) ──► direkt, Programm bleibt unverändert
        │ Leitstelle  │ ── sofort / als Nächstes ─────────►┌──────────────────────┐
        └─────────────┘                                    │   Warteschlange      │
          │ demnächst          │ Nachrichten               │   data/queue.json    │
          ▼                    ▼                           │   + Cursor           │──► Player
   Wunsch-Postfach      Meldungs-Postfach                  │                      │   (Spuren, Skip,
          │                    │                           │                      │    Preempt, Fade,
          ▼                    ▼                           │                      │    Füllprogramm)
   ┌─────────────┐      ┌─────────────┐                    │                      │
   │ Musik       │      │ Nachrichten │ ── news @ Slot ───►│                      │
   └─────────────┘      └─────────────┘                    │                      │
      │   ▲                                                │                      │
      │   └────────────── Füllstand < Schwelle ────────────┤                      │
      └────────────────── program block + Reserve ────────►│                      │
                                                           └──────────────────────┘
```

Alle drei Redaktionen laufen über **dieselbe** Agent-Schleife (`AgentLoop` wird zu
`DeskRunner`), nur mit unterschiedlicher `DeskConfig`. Musik und Nachrichten laufen nach Plan
(Füllstand bzw. :00 voll / :30 kurz), die Leitstelle spontan und parallel dazu.

### 3.1 Warteschlange (`app/program/queue.py`, neu)

```python
@dataclass
class QueueItem:
    id: str
    lane: Literal["urgent", "reply", "news", "program", "filler"]
    desk: str
    segments: list[Segment]        # Segment bekommt duration_seconds auch für Jingles
    created_at: str
    not_before: str | None = None  # z.B. Nachrichten-Slot 07:00
    expires_at: str | None = None  # Pflicht für news/reply/program (s.u.)
    interrupt: bool = False
    resume_interrupted: bool = True  # breaking: Song fortsetzen; play_now: Song verwerfen
    call_id: str | None = None     # Zwischenruf, aus dem der Beitrag stammt
    status: Literal["queued", "playing", "played", "skipped", "expired", "removed"] = "queued"
```

- **Persistenz**: `data/queue.json`; ein Prozess-Lock, atomares `write tmp → rename`. Nur der
  Player ändert `status`; Redaktionen hängen nur an; die UI entfernt (`removed`, mit Undo).
- **Cursor**: `data/player_cursor.json` = `(item_id, segment_index)`. Nach Neustart geht es mit
  dem *nächsten* Segment weiter - kein Wiederholen ganzer Blöcke.
- **Auswahl** `next_item(now)`: höchste Spur zuerst, in der Spur FIFO; `not_before` in der
  Zukunft überspringen; abgelaufene ⇒ `expired` (mit Log-Zeile, warum).
- **Ablauf**: `program`-Blöcke `expires_at = created_at + 2 h` und verfallen bei Sendeschluss
  (sonst laufen um 06:00 Blöcke von 23:00 mit falschen Zeitbezügen). `reply` 30 min,
  `news` Slot + `max_delay_minutes`.
- **Füllstand** `remaining_program_seconds()`: Summe der Dauer aller `queued`-Segmente der
  Spur `program` + Restlaufzeit des aktuellen Segments. Dauer: Tracks aus dem Provider, sonst
  `TRACK_ESTIMATE_SECONDS`; Jingles aus der WAV (`PiperTTSEngine.duration_seconds`, beim
  Rendern gespeichert). Die Restlaufzeit kommt aus dem Player-Speicher (`started_at`,
  `duration`), nicht aus der Datei.
- **Obergrenze**: `max_queued_program_minutes` (Standard 45) - weder Füllstand-Trigger noch
  „Jetzt ausführen“ stapeln darüber hinaus.
- **Laden tolerant**: unbekannte Felder in Segmenten/Beiträgen werden ignoriert
  (heute crasht `Segment(**s)` bei neuen Feldern).
- Aufräumen: Einträge in Endzustand älter als 24 h. `current_script.json` wird nicht
  migriert, sondern gelöscht (Inhalt ist nach dem Update ohnehin veraltet).

### 3.2 Player (`app/audio/player.py`, Umbau)

- Schleife über `queue.next_item()`, Segment für Segment; zwischen Segmenten wird immer neu
  gewählt ⇒ `reply`/`news`/`urgent` spätestens nach dem laufenden Segment.
- **Unterbrechbare Wiedergabe von Anfang an (Phase 1)**: neue Provider-Methode
  `play_until(track, stop_event) -> PlaybackResult(finished|stopped, position_seconds)`.
  - Spotify: bestehender 5-s-Status-Poll bleibt, gewartet wird aber mit
    `stop_event.wait(5)` ⇒ Stopp sofort, ohne zusätzliche API-Aufrufe.
  - Lokal: `ffplay` per `Popen` (heute `subprocess.run`, dadurch hängt auch der Shutdown),
    Stopp ⇒ `terminate`.
  - Grundlage für „Song überspringen“ (Phase 1) und Preempt (Phase 4).
- **Schutzschalter**: enden 3 Segmente in Folge nach < 10 s (z.B. Spotify-Gerät weg ⇒
  `play_and_wait` bricht nach einem Poll ab und leert sonst die Warteschlange im Sekundentakt),
  pausiert der Player 60 s, setzt einen Hinweis für die UI und versucht es erneut.
- **Spielhistorie**: `record_played` erst nach 30 s Laufzeit (übersprungene Songs zählen nicht
  für die Wiederholungssperre).
- **Preempt (Phase 4)**: `interrupt=True` setzt das `stop_event` - aber nur, wenn gerade
  **Musik** läuft; eine laufende Ansage oder Nachrichten werden zu Ende gespielt. Für `news` mit
  `placement: on_time` stellt der Player einen Timer auf `not_before`, der das Event auslöst.
  Nach dem Beitrag wird der unterbrochene Song fortgesetzt (`resume_interrupted`, z.B.
  Unwetterwarnung) oder verworfen (`play_now`: danach geht das Programm normal weiter).
  - Spotify: Position aus `current_playback()["progress_ms"]`; Ausblenden in 4–6
    `volume()`-Schritten; Pause; Jingle; Fortsetzen per `start_playback(device_id)` ohne
    `uris` (nur wenn das Gerät den Zustand verloren hat: `uris` + `position_ms`); Einblenden.
    Die verbleibende Wartezeit wird aus der Restlaufzeit neu berechnet.
  - Antwortet die Volume-API mit 403 (manche librespot-Mixer), wird das einmal erkannt und
    ohne Fade gearbeitet (harter Schnitt).
  - Lokal: Fortsetzen per `ffplay -ss <pos> -af afade=t=in:d=2`.
  - Schutzabstand `min_minutes_between_interrupts` (Standard 10); die UI kennt den nächsten
    möglichen Zeitpunkt vorab (`interrupt_available_at`).
- **Status im Speicher** für die UI: aktueller Beitrag/Segment, `position`, `duration`,
  `server_time`, Modus mit Klartext (`spielt`, `unterbricht`, `Nachrichten warten auf
  Song-Ende`, `Füllprogramm`, `pausiert: Spotify-Gerät nicht erreichbar`) und ein
  Einzeiler-Log der letzten Entscheidungen.

### 3.3 Redaktions-Framework (`app/agent/desk.py`, aus `loop.py` extrahiert)

```python
@dataclass
class DeskConfig:
    name: str                       # "music" | "news" | "dispatch"
    prompt_path: Path               # Nutzer-Prompt (Abschnitt 4)
    tools: set[str]                 # Builtins der Redaktion + in der UI gewählte Plugins
    context_plugins: set[str]       # automatisch geladene Kontext-Plugins
    history_runs: int
    max_tool_iterations: int
```

- `DeskRunner.run(desk, trigger)`: Prompt, Tools, Verlauf **pro Redaktion**; Ergebnis über
  redaktionsspezifische Tools in die Warteschlange.
- **Nicht blockierender Lock pro Redaktion + „dirty“-Flag**: kommt ein Auslöser, während die
  Redaktion schon läuft, wird *ein* Folge-Lauf vorgemerkt statt zu warten oder zu verwerfen.
  So gehen Zwischenrufe während eines Leitstellen-Laufs nicht verloren, und der
  Füllstand-Watcher erzeugt keinen Thread-Stau.
- **Backoff** nach Fehlern pro Redaktion (1, 2, 5, 10 min; Leitstelle 10 s, 30 s, dann jede
  Minute), damit ein ausgefallenes Ollama nicht alle 30 s angefragt wird.
- **Kein globaler LLM-Semaphore** (Läufe warten fast nur auf das Netz); stattdessen ein
  Lock (1) nur ums Piper-Rendern - das ist die CPU-Last auf dem Pi.
- **Transkripte** bekommen `desk`; Aufräumen (`MAX_TRANSCRIPTS_ON_DISK`) und
  `load_recent_transcripts` **pro Redaktion**, sonst verdrängen Leitstellen-Läufe den
  Musikverlauf.
- Status im Speicher: `idle/running/error`, letzter Erfolg, Anzahl Fehler in Folge, letzter
  Fehler, nächster geplanter Auslöser.
- Der alte `/api/status/trigger` (heute `BackgroundTasks`, umgeht jeden Lock) entfällt zugunsten
  von `POST /api/desks/{name}/run` über den Scheduler.

### 3.4 Auslöser (`app/scheduler.py`, Umbau)

| Redaktion | Auslöser |
|---|---|
| music | Watcher alle 30 s: `remaining_program_seconds < fill_threshold_minutes` und unter Obergrenze ⇒ Lauf. Heartbeat alle 60 min. Zum Sendebeginn sofort. |
| news | APScheduler-`cron` je Slot, `lead_minutes` vorher. |
| dispatch | Sofort bei jedem neuen Zwischenruf; Fallback-Poll alle 60 s auf offene Zwischenrufe (auch nach LLM-Ausfall). |

- Sendefenster gilt für Musik und Nachrichten. Die Leitstelle läuft auch außerhalb: direkte
  Aktionen (Licht) werden ausgeführt, Wünsche und Hinweise abgelegt; alles, was gesendet
  werden soll, wartet auf den Sendebeginn (`hold`, UI sagt das, Abschnitt 5.3).
- `agent.loop_interval_seconds` entfällt.

### 3.5 Die drei Redaktionen

#### Musikredaktion (`music`)
- Plant einen Block von `block_minutes` (Standard 20) als `program`-Beitrag.
- Kontext: Uhrzeit, was läuft, **Ende der Warteschlange** (letzte Titel + letzte Ansage),
  Wiederholungssperre (gespielt **und** bereits eingeplant), **offene Wünsche aus dem
  Wunsch-Postfach** (Text, Absender, `valid_until`).
- Tool `append_program_block(segments)` (Nachfolger von `set_playback_script`): löst Titel auf,
  entfernt Wiederholungen (Historie + Warteschlange + innerhalb des Blocks), prüft den
  **Moderationstakt über die Blockgrenze hinweg** (höchstens 1 Ansage je
  `songs_per_announcement` Songs, Standard 3) und die Mindestlänge; Rückmeldung wie heute.
  Segmente dürfen eine `wish_id` tragen ⇒ der Wunsch gilt als eingeplant, der Zwischenruf
  zeigt „im Block ab 07:43“.
- Prompt-Leitlinie: anknüpfen statt neu begrüßen - außer beim ersten Block nach Sendebeginn;
  offene Wünsche in einem der nächsten Blöcke einbauen, gern mit Gruß an den Absender, und
  auch in der Reserve berücksichtigen. Abgelaufene Wünsche (`valid_until`) fallen still weg.

#### Leitstelle (`dispatch`)
- Wird pro Zwischenruf gestartet, läuft parallel zu den geplanten Redaktionen. Klein und
  schnell: sieht Zwischenruf + Absender, die letzten 5 Zwischenrufe **mit ihren Aktionen**, was
  gerade läuft und was als Nächstes kommt. Keine automatischen Kontext-Plugins (Latenz); Wetter
  & Co. nur als Tools, Ergebnisse 10 min gecacht. `max_tool_iterations` klein (6).
- Aufgabe 1 - **Dringlichkeit einschätzen**: `sofort` / `als Nächstes` / `demnächst` /
  `Nachrichten`. Aufgabe 2 - **weiterleiten** oder selbst erledigen. Ein Zwischenruf kann
  mehrere Aktionen auslösen („Licht an und spiel als Nächstes Queen“).
- Beispiele (stehen auch im Standard-Prompt):

  | Zwischenruf | Dringlichkeit | Weg | Was passiert |
  |---|---|---|---|
  | „Schalte das Licht an“ | sofort | `control_hue_lights` (direkt) | Licht geht an, Programm unverändert, Bestätigung nur in der UI |
  | „Spiele jetzt Podcast xy“ | sofort | `play_now(episode_query=…, announce_text=…)` | Song blendet aus, kurze Ansage, Podcast, danach geht das Programm weiter |
  | „Als Nächstes bitte xy“ | als Nächstes | `play_next(query=…, announce_text=…)` | nach dem laufenden Song, optional mit kurzer Ansage („…auf Wunsch von Mama“) |
  | „Beatles fänd ich demnächst mal top“ | demnächst | `add_music_wish(text, valid_until)` | Wunsch-Postfach; die Musikredaktion baut ihn in einen der nächsten Blöcke ein (auch Reserve) |
  | „Morgen ist Sperrmüll“ | Nachrichten | `note_for_news(text, valid_until)` | Meldungs-Postfach für die nächsten Nachrichten |
  | „Was essen wir heute?“ | als Nächstes | `reply(text, when="next")` | die Leitstelle antwortet selbst, als Ansage nach dem Song |
  | „Unwetterwarnung für heute Abend!“ | sofort | `breaking(text)` | echte Eilmeldung: ausblenden, Ansage, Song fortsetzen |

- Tools:
  - `play_now(query | episode_query, announce_text=None)` ⇒ Spur `urgent`, `interrupt=True`,
    `resume_interrupted=False`.
  - `play_next(query | episode_query, announce_text=None)` ⇒ Spur `reply`.
  - `reply(text, when: "now" | "next")` ⇒ Ansage; `now` ⇒ Spur `urgent` mit Unterbrechung,
    `next` ⇒ Spur `reply`.
  - `breaking(text)` ⇒ Spur `urgent`, `interrupt=True`, `resume_interrupted=True`; gesendete
    Eilmeldungen landen zusätzlich im Meldungs-Postfach.
  - `add_music_wish(text, valid_until)` ⇒ Wunsch-Postfach `data/music_wishes.json`.
  - `note_for_news(text, valid_until)` ⇒ Meldungs-Postfach `data/news_notes.json`.
  - freigegebene Plugin-Tools als **direkte Aktionen** (Hue & Co.) und Info-Plugins (gecacht).
- **Mehrere Aktionen, ein Beitrag**: der `DeskRunner` führt direkte Aktionen sofort aus (das
  Ergebnis braucht das LLM für seine Antwort), **sammelt aber alle Programm-Aktionen eines
  Laufs** und schreibt sie erst am Ende. `play_next` + `reply(when="next")` aus demselben Lauf
  werden zu **einem** `reply`-Beitrag in sinnvoller Reihenfolge (Ansage, dann Song) - nie zwei
  unabhängig einsortierte Beiträge, bei denen der Song vor der Ansage laufen könnte. Gleiches
  für `play_now`/`breaking`/`reply(when="now")` ⇒ ein `urgent`-Beitrag. Wünsche und Hinweise
  werden ebenfalls am Laufende geschrieben; bricht der Lauf ab, bleibt nichts halb eingeplant.
- **Schutz**:
  - `min_minutes_between_interrupts` (Standard 10): eine Unterbrechung innerhalb des Abstands
    wird zu „als Nächstes“ herabgestuft; die UI sagt das („Unterbrechen erst wieder ab 07:24
    möglich – läuft nach diesem Song“).
  - Unterbrochen wird **nur Musik**; laufende Ansagen/Nachrichten werden zu Ende gespielt.
  - Direkte Aktionen (Licht) unterbrechen nie etwas und zählen nicht zum Schutzabstand.
  - Außerhalb des Sendefensters: Programm-Aktionen warten auf den Sendebeginn (`hold`, ihr
    `expires_at` zählt ab Sendebeginn), direkte Aktionen werden trotzdem ausgeführt.
  - `dispatch.allow_interrupt` (Standard `true`): aus ⇒ alles mit `sofort` läuft als
    „als Nächstes“.
- **LLM nicht erreichbar**: der Zwischenruf bleibt offen (`retrying`) und wird mit Backoff
  erneut versucht (Abschnitt 3.3); die UI zeigt „Leitstelle gerade nicht erreichbar – wird
  erneut versucht“. Nach `reply_expires_minutes` ohne Erfolg ⇒ `expired` mit „Nochmal senden“.
- Zwischenruf-Datensatz `data/calls/<id>.json`:
  `{id, created_at, author, text, source, status, reply_text, actions, aired_at, error}` mit
  `actions: [{type, urgency, target|summary, status, eta, note, queue_item_id | wish_id |
  note_id, undo_available}]` (`type`: `plugin`, `play_now`, `play_next`, `reply`, `breaking`,
  `music_wish`, `news_note`; `note` z.B. „herabgestuft: Schutzabstand“).
  Status des Zwischenrufs: `transcribing → new → processing → handled` bzw.
  `retrying / expired / removed`. Status je Aktion: `done` (direkte Aktion), `queued → playing
  → aired`, `noted → used` (Wunsch/Hinweis), `held` (wartet auf Sendebeginn), `removed`,
  `expired`, `failed`.

#### Nachrichtenredaktion (`news`)
- Läuft `lead_minutes` vor jedem Slot. Zwei Ausgabeformate:
  - **volle Stunde = ausführlich** (ca. 2–3 min): mehrere Schlagzeilen mit je 1–2 Sätzen,
    Wetter mit Vorhersage, alle offenen Hinweise aus dem Meldungs-Postfach.
  - **halbe Stunde = kurz** (ca. 30–60 s): 2–3 Schlagzeilen in einem Satz, Wetter jetzt,
    nur neue Hinweise.
  Format und Länge je Slot stehen in der Config (`slots`), der Prompt bekommt das Format als
  Vorgabe.
- Quellen einzeln wählbar: News-Plugin, Wetter-Plugin, Meldungs-Postfach
  (`data/news_notes.json`, mit `valid_until`, befüllt von der Leitstelle). Gesendete
  Eilmeldungen landen automatisch im Postfach und werden in der nächsten Ausgabe wiederholt.
  Verwendete Hinweise werden als `used` markiert (Zwischenruf zeigt „in den Nachrichten 08:00“).
- Tool `schedule_news(text)`: `news`-Beitrag mit `not_before = Slot`,
  `expires_at = Slot + max_delay_minutes` (15). `placement: after_song` (Standard) oder
  `on_time` (erst mit Phase 4).

### 3.6 Leitstelle: Ablauf eines Zwischenrufs

```
 Hörer                 Backend / DeskRunner       Leitstelle (LLM)      Player
  │ „Licht an und spiel  │                          │                    │
  │  jetzt Queen“        │                          │                    │
  ├─────────────────────►│ 1. Zwischenruf speichern, Leitstelle starten   │
  │                      │ ────────────────────────►│                    │
  │                      │                          │ 2. Dringlichkeit    │
  │                      │                          │    je Anliegen      │
  │                      │ ◄── control_hue_lights ──│                    │
  │ ◄── „Licht ✓“        │ 3. direkt ausführen      │                    │
  │                      │ ◄── play_now(Queen, …) ──│                    │
  │                      │ 4. vormerken (staged)    │                    │
  │                      │ 5. am Laufende: Schutzabstand? Sendefenster?   │
  │                      │    allow_interrupt? sonst ⇒ als Nächstes      │
  │                      │ 6. Piper rendert (Vorrang am TTS-Lock)         │
  │                      │ 7. ein urgent-Beitrag, interrupt ─────────────►│
  │                      │                          │   8. Musik? Position
  │                      │                          │      merken, 2 s aus-
  │                      │                          │      blenden, Pause.
  │                      │                          │      Ansage/News? zu
  │                      │                          │      Ende spielen
  │                      │                          │   9. Ansage + Queen
  │                      │                          │  10. Programm weiter
  │                      │                          │      (breaking: Song
  │                      │                          │      fortsetzen)
  │ ◄── „gesendet 07:14“ │ 11. Aktionen im Zwischenruf aktualisieren
```

- **Auslöser**: jeder Zwischenruf, ohne Knopf und ohne Umschalter - die Leitstelle entscheidet
  die Dringlichkeit. Schutz gegen Übereifer: Schutzabstand, nur Musik wird unterbrochen,
  `allow_interrupt`, transparente Aktionen mit Rückgängig (Abschnitt 5.3).
- **Dauer** vom Absenden bis zur Wirkung: direkte Aktion (Licht) typisch 3–10 s (ein
  LLM-Schritt + Plugin); Unterbrechung typisch 10–25 s (LLM ~5–15 s, Piper ~2–5 s,
  Ausblenden 2 s); „als Nächstes“ spätestens am Ende des laufenden Songs.
- **Zweite Unterbrechung** innerhalb `min_minutes_between_interrupts`: wird zu „als Nächstes“;
  `interrupt_available_at` im Status, damit die UI vorab „Unterbrechen erst wieder ab 07:24
  möglich“ zeigen kann.
- **Status im Gespräch** je Aktion: „Leitstelle sortiert ein…“ → „läuft jetzt“ bzw. „läuft nach
  diesem Song (ca. 2 min)“ → „gesendet 07:14“.

### 3.7 Füllprogramm (vom Agenten vorbereitet, ohne LLM abspielbar)

- Die Musikredaktion pflegt bei jedem erfolgreichen Lauf zusätzlich eine **Reserve**
  (`data/reserve.json`, ca. 15–20 Titel): ausgesucht nach denselben Regeln wie das Programm -
  Lieblings-Playlists, Songwünsche aus System- und Redaktions-Prompt, offene Wünsche aus dem
  Wunsch-Postfach, Tageszeit, ohne kürzlich gespielte Titel. Tool `update_reserve(tracks)`; der Prompt fordert das pro Lauf ein.
- Ist die Spur `program` leer und die Musikredaktion liefert nicht (läuft noch, Fehler,
  Backoff), spielt der Player Titel aus der Reserve, gefiltert durch die Wiederholungssperre,
  ohne Ansagen, Spur `filler`.
- Ist auch die Reserve aufgebraucht (oder noch nie gefüllt), zufällige Titel aus den
  **Lieblings-Playlists** (`music.favorite_playlists`, in „Technik“ auswählbar) bzw. bei der
  lokalen Quelle aus der ganzen Bibliothek.
- Sobald ein `program`-Block da ist, übernimmt er am nächsten Song-Ende.
- Die Lieblings-Playlists sind auch **Kontext für die Musikredaktion** (Namen + Stichprobe von
  Titeln), damit Programm und Reserve den Geschmack des Haushalts treffen.
- UI: „Füllprogramm aus der Reserve – Musikredaktion nicht erreichbar“; auf „Redaktion“ ist
  die aktuelle Reserve einsehbar.

### 3.8 Podcasts (Phase 5)
- `Segment.type = "episode"` (Spotify `type=episode`); lokal nicht unterstützt.
- Bis dahin lehnen `play_now`/`play_next` ein `episode_query` mit Rückmeldung ab; die Leitstelle
  antwortet dann per `reply` („Podcasts kann ich noch nicht abspielen“).

## 4. Konfiguration & Nutzerdaten

### 4.1 Dateien
- **Im Repo (nur Vorlagen, bleiben an ihren Pfaden)**: `config/config.yaml` (Defaults),
  `config/desks/{music,news,dispatch}.md` (Standard-Prompts). `config/system_prompt.md` wird zu
  `config/desks/music.md` (bleibt eine Version als Kopie liegen).
- **Nutzerdaten (gitignored)**: `data/config.yaml` enthält **nur Abweichungen** von den
  Defaults; `data/prompts/<desk>.md` nur, wenn der Prompt angepasst wurde.
- `load_config()` = Defaults ⊕ Nutzerdatei (gleiche Merge-Logik wie das Schnell-Update, mit
  kurzem mtime-Cache - es wird pro Player-Segment aufgerufen). `save_config()` schreibt nur
  noch die Differenz ⇒ geänderte Defaults erreichen Nutzer, die den Wert nie angefasst haben.
- Config-API: `PATCH /api/config` mit Teilobjekten; jede Karte sendet nur ihre Felder
  (heute überschreibt jede Karte die ganze Config aus dem beim Laden eingelesenen JSON).

### 4.2 Migration bestehender Installationen
Die Migration darf **nicht** im alten `quick_update` passieren: der ist beim `git pull` schon
im Speicher, und der Pull würde die Dateien anfassen, die er danach liest.
1. `finish_update` (läuft bereits im *neuen* Script): ist `config/config.yaml` lokal geändert,
   wird die Differenz zu HEAD nach `data/config.yaml` geschrieben, danach
   `git checkout -- config/config.yaml`. Gleiches für `system_prompt.md` ⇒
   `data/prompts/music.md`.
2. Dieselbe Migration beim App-Start (für manuelles `git pull`), idempotent.
3. Einmalig beim Start (in `lifespan`, nicht in `load_config`): `agent.max_tool_iterations`,
   `agent.no_repeat_minutes` ⇒ `desks.music.*`; `agent.loop_interval_seconds` entfällt;
   unbearbeitete `data/inbox/*.txt` ⇒ Zwischenrufe (Status `new`, die Leitstelle arbeitet sie ab).
4. Hinweis in der UI (`notices`): „Wünsche heißen jetzt Zwischenruf“, „Takt entfällt“, „dein
   Systemprompt ist jetzt der Musik-Prompt“ - wegklickbar.

### 4.3 Neue Einstellungen (Defaults)

```yaml
desks:
  music:
    enabled: true
    fill_threshold_minutes: 10
    block_minutes: 20
    max_queued_program_minutes: 45
    songs_per_announcement: 3
    no_repeat_minutes: 120
    max_tool_iterations: 20
    plugins: [get_weather, get_favorites]
    context_plugins: [get_weather]
  news:
    enabled: true
    slots:
      - {minute: "00", format: full}    # ausführlich, ca. 2–3 min
      - {minute: "30", format: short}   # kurz, ca. 30–60 s
    lead_minutes: 5
    placement: after_song        # after_song | on_time
    max_delay_minutes: 15
    sources: [news, weather, notes]
    max_tool_iterations: 8
  dispatch:
    enabled: true
    allow_interrupt: true        # darf „sofort“ die Musik unterbrechen? aus ⇒ „als Nächstes“
    min_minutes_between_interrupts: 10
    reply_expires_minutes: 30    # so lange wird bei LLM-Ausfall erneut versucht
    wish_default_valid_hours: 24 # valid_until, wenn der Hörer nichts sagt
    max_tool_iterations: 6
    plugins: [control_hue_lights, get_weather, get_news_headlines, get_favorites]
music:
  favorite_playlists: []         # Playlist-IDs: Kontext für die Musikredaktion + letzter Fallback
audio:
  fade_seconds: 2
```

## 5. Web-UI

### 5.1 Informationsarchitektur
Zwei Zielgruppen, vier Bereiche. Familie: *Sendung*, *Rufen* (Handy). Betreiber: *Redaktion*,
*Technik* (Laptop).

```
Sendung · Rufen · Redaktion · Technik
```
Kurze Labels, damit die Navigation auf 320 px passt (sonst Umbruch erlauben).
`inbox.html` bleibt als Weiterleitung auf `calls.html` (Homescreen-Lesezeichen).

| Bereich | Datei | Inhalt |
|---|---|---|
| **Sendung** | `index.html` | On-Air-Display, Als Nächstes, 24h-Skala, Überspringen/Entfernen |
| **Rufen** | `calls.html` | Zwischenruf senden, Gesprächsverlauf, Mini-„läuft gerade“-Leiste |
| **Redaktion** | `desks.html` | Status aller Redaktionen, Einstellungen/Prompt/Verlauf je Redaktion |
| **Technik** | `settings.html` | Sendezeiten, Stimme, LLM & Musikquelle & Lautstärke, Fade, Plugins (Installation/Aktivierung), Füllprogramm, Erweitert (JSON) |

Die heutige Karte „Redaktion“ (LLM/Musikquelle/Takt) geht in „Technik“ auf; der Name ist dann
für die neue Seite frei.

### 5.2 Sendung
- **On-Air-Display**: aktueller Beitrag, Spur-Chip (Text + Farbe), Fortschrittsbalken, der im
  Browser aus `position/duration/server_time` animiert wird (Polling allein würde springen),
  Modus-Klartext aus dem Player.
- **Als Nächstes**: nächste ~10 Beiträge aus `GET /api/queue`, `program`-Blöcke mit
  aufgeklappten Titeln, geschätzte Startzeiten, Nachrichten mit Slot-Uhrzeit.
- **Aktionen**: „Song überspringen“ (Ghost-Button, Bestätigung); „Block entfernen (7 Titel)“ /
  „Antwort entfernen“ mit 10-s-„Rückgängig“; nach Entfernen eines Blocks plant die
  Musikredaktion nach; entfernte Antworten zeigen im Gespräch „vom Sender entfernt“.
- **24h-Skala**: Nachrichten-Slots als Marker; Unterzeile statt „Neuer Durchlauf alle N min“:
  „Programm bis ~07:43 · Nachrichten :00/:30“.
- **Zustände**: Sendepause („Sender ruht bis 06:30“), Füllprogramm, Schutzschalter
  („pausiert: Spotify-Gerät nicht erreichbar“), leere Warteschlange („Musikredaktion plant
  seit 40 s“).

### 5.3 Rufen (Handy-first)
- **Wer ruft?** Namens-Chip (Mama, Opa, …), im Browser gemerkt, als `author` gespeichert ⇒
  eigene Nachrichten rechts, andere links mit Namen; der Moderator kann mit Namen grüßen.
  Eigene Call-IDs zusätzlich lokal gemerkt („von diesem Gerät“).
- **Einsprechen mit Prüfschritt**: Aufnahme → „Wird transkribiert…“ → Text **editierbar** →
  Senden. Die Transkription läuft asynchron (`POST /api/calls/voice` ⇒ `transcribing`,
  danach `PATCH /api/calls/{id}` zum Bestätigen).
- **Kein Eilmeldungs-Umschalter**: ein Senden-Button für alles; wie eilig es ist, entscheidet
  die Leitstelle. Während des Schutzabstands steht unter dem Eingabefeld dezent
  „Unterbrechen erst wieder ab 07:24 möglich“ (aus `interrupt_available_at`).
- **Gesprächsverlauf** (`role="log"`, Einträge per Schlüssel aktualisiert statt per
  `innerHTML` neu gebaut): unter jedem Zwischenruf das **Ergebnis der Leitstelle** als
  Zeilen/Chips, je Aktion mit Klartext, ETA und - wo möglich - „Rückgängig“ (Beitrag aus der
  Warteschlange entfernen, Wunsch/Hinweis verwerfen):
  - „→ Licht: eingeschaltet ✓“ (kein Rückgängig)
  - „→ Musikredaktion: Beatles vorgemerkt (bis morgen)“ · später „im Block ab 07:43“
  - „→ Nachrichten: Sperrmüll-Hinweis für 08:00 vorgemerkt“
  - „→ als Nächstes: Bohemian Rhapsody (ca. 2 min)“
  - „→ jetzt: unterbricht den Song“ bzw. „→ als Nächstes statt jetzt: Unterbrechen erst wieder
    ab 07:24 möglich“
  - Antwort-Text der Leitstelle, falls sie selbst antwortet.
  Status davor: „Leitstelle sortiert ein…“, „dauert länger als sonst“ (> 60 s), „Leitstelle
  gerade nicht erreichbar – wird erneut versucht“, danach „läuft jetzt“, „gesendet 07:14“,
  „verfallen“ + „Nochmal senden“. Sendepause: „Sender ruht bis 06:30 – dein Zwischenruf kommt
  dann dran“ (direkte Aktionen wie Licht laufen trotzdem sofort).
- Polling 3 s solange etwas offen ist, sonst 15 s (`GET /api/calls?since=`).
- Oben eine schmale „läuft gerade“-Leiste. Antwort im Browser nachhören (Phase 2, optional).

### 5.4 Redaktion
```
┌ Musik ● bereit  [an] ─┐┌ Nachrichten ● 07:30 [an] ┐┌ Leitstelle ● wartet [an] ┐
│ ▓▓▓▓▓░░ 23 min | 10   ││ Vorbereitung 07:25       ││ wartet auf Zwischenrufe  │
│ 2 Wünsche offen       ││ 1 Hinweis im Postfach    ││ zuletzt 07:14 · 0 offen  │
│ [Jetzt planen]        ││ [Jetzt vorbereiten]      ││ [Offene erneut versuchen]│
└───────────────────────┘└──────────────────────────┘└──────────────────────────┘
[ Einstellungen | Prompt | Verlauf ]                   Musikredaktion (#music)
┌──────────────────────────────────────────────────────────────────────────┐
│ Detail der gewählten Redaktion in voller Breite                          │
└──────────────────────────────────────────────────────────────────────────┘
```
- Oben drei kompakte Statuskarten (Lampe, `<meter>` für den Füllstand, Schalter, Aktion);
  darunter das Detail der gewählten Redaktion mit Tabs *Einstellungen | Prompt | Verlauf*,
  verlinkbar (`desks.html#news`) - Fehler und Chips auf anderen Seiten verlinken dorthin.
- Status-Texte: „Musik reicht noch 23 min · plant nach bei 10 min“, „Nächste Ausgabe 07:30 ·
  Vorbereitung 07:25“, „Leitstelle wartet auf Zwischenrufe · zuletzt 07:14“, „Fehler 07:12:
  Ollama 500 · nächster Versuch 07:13“, „Aus – keine Nachrichten“.
- Detail **Musik** zusätzlich: offene Wünsche aus dem Wunsch-Postfach (Text, Absender, gültig
  bis, eingeplant ja/nein, „Verwerfen“) und die aktuelle Reserve. Detail **Nachrichten**: das
  Meldungs-Postfach (Hinweis, gültig bis, verwendet, „Verwerfen“). Detail **Leitstelle**:
  `allow_interrupt`, Schutzabstand, Plugin-Auswahl (welche direkten Aktionen erlaubt sind).
- Einstellungen je Redaktion inkl. **Plugin-Auswahl als Checkboxen** (die globale
  Plugin-Liste in „Technik“ ist nur noch Installation/Aktivierung).
- Prompt: Badge „Standard / angepasst“, „neue Standardversion verfügbar“, „Auf Standard
  zurücksetzen“ mit Bestätigung.
- Mobil: Statuskarten gestapelt, Tippen öffnet das Detail darunter.
- Redaktionen, die es noch nicht gibt, werden nicht angezeigt (keine ausgegrauten
  „kommt bald“-Karten).

### 5.5 Technik
Die bestehenden Karten (Sendezeiten, Stimme, LLM/Musikquelle, Plugins, Erweitert) ziehen hierher,
ergänzt um Lautstärke/Fade und Lieblings-Playlists. Jede Karte speichert
per `PATCH` nur ihre Felder.

### 5.6 Querschnitt
- Design-Tokens weiterverwenden; Spur-Farben als Tokens für hell, dunkel **und** das immer
  dunkle Display. `--signal` nur noch für Unterbrechung („jetzt“)/On-Air (heute auch Jingle-Chips und
  Fortschrittsbalken). Chips tragen immer Text, nie nur Farbe.
- `app.js` ⇒ `common.js` + je Seite eine Datei; Seiten-Boot über `<body data-page="…">`.
- Barrierefreiheit: die Uhr aus dem `role="status"`-Element der Lampe herausnehmen
  (heute wird alle 15 s die Uhrzeit vorgelesen); Listen per Schlüssel aktualisieren, damit
  Fokus und geöffnete `<details>` erhalten bleiben.
- Hinweise (`notices`) als wegklickbares Banner auf allen Seiten (Migration, „keine Stimme
  installiert – Antworten können nicht gesendet werden“, Schutzschalter).
- Polling statt SSE für v1 (wenige Clients).

## 6. API

| Methode & Pfad | Zweck |
|---|---|
| `GET /api/status` | schlank: `on_air`, `next_on_air_at`, `now_playing{…, position, duration, lane, mode, mode_text}`, `desks` (Kurzstatus), `interrupt_available_at`, `notices`, `server_time` |
| `GET /api/queue` · `DELETE /api/queue/{id}` · `POST /api/queue/{id}/restore` | Warteschlange · entfernen · Rückgängig |
| `POST /api/player/skip` | aktuellen Song überspringen |
| `GET /api/desks` · `PATCH /api/desks/{name}` | Status + Einstellungen je Redaktion |
| `GET/PUT /api/desks/{name}/prompt` · `POST …/prompt/reset` | Prompt je Redaktion |
| `POST /api/desks/{name}/run` | Redaktion jetzt ausführen (über Scheduler/Lock) |
| `GET /api/transcripts?desk=` | Verlauf je Redaktion |
| `POST /api/calls/text` · `POST /api/calls/voice` · `PATCH /api/calls/{id}` | Zwischenruf (`author`, `text`); Sprache asynchron + Bestätigen. Kein `urgent` - die Leitstelle entscheidet |
| `GET /api/calls?since=` · `GET /api/calls/{id}/audio` | Gesprächsverlauf inkl. `actions` · Antwort-Audio |
| `POST /api/calls/{id}/retry` | abgelaufenen Zwischenruf erneut an die Leitstelle geben („Nochmal senden“) |
| `GET /api/wishes` · `DELETE /api/wishes/{id}` | Wunsch-Postfach anzeigen · Wunsch verwerfen (Rückgängig) |
| `GET /api/news-notes` · `DELETE /api/news-notes/{id}` | Meldungs-Postfach anzeigen · Hinweis verwerfen (Rückgängig) |
| `PATCH /api/config` | Teil-Update der Nutzer-Config |
| `POST /api/notices/{id}/dismiss` | Hinweis wegklicken |

`/api/inbox/*` entfällt (UI und Backend kommen im selben Update). Rückgängig für
„als Nächstes“/„jetzt“ läuft über `DELETE /api/queue/{id}` (solange der Beitrag noch nicht
spielt).

## 7. Phasen

Jede Phase ist einzeln lauffähig, wird auf dem Pi getestet und dann gepusht.

### Phase 0 - Fundament
- Nutzerdaten-Trennung und Migration (Abschnitt 4.1/4.2), `PATCH /api/config`, Karten senden nur
  eigene Felder.
- Tests: `pytest` (Dev-Abhängigkeit), Fakes (`FakeMusicProvider`, `FakeTTS`, `ScriptedLLM`),
  erste Tests für Wiederholungssperre, Mindestlänge, Config-Merge/-Diff, Migration
  (inkl. Update-Simulation wie beim Schnell-Update-Test), Stimmen-API-Validierung.
- **Audio-Spike** (½ Tag, auf dem Pi): laufen raspotify und ffplay gleichzeitig (ALSA/dmix/
  PipeWire)? Heute schon relevant, weil Jingles direkt nach Songs kommen. Ergebnis
  entscheidet über Ducking (später) und ggf. README-Hinweise.
- **Akzeptanz**: Update auf dem Pi ohne Konflikte, alle Einstellungen erhalten; `pytest` grün.

### Phase 1 - Warteschlange, Redaktions-Framework, Musikredaktion, neue Navigation
- `queue.py` + Cursor, Player auf Warteschlange mit `play_until`/Skip, Schutzschalter,
  Füllprogramm (Reserve + Lieblings-Playlists), Jingle-Dauern.
- `DeskRunner` mit nicht blockierendem Lock + dirty-Flag + Backoff, Transkripte je Redaktion,
  Füllstand-Trigger, `append_program_block` (Duplikate gegen Warteschlange, Moderationstakt
  über Blockgrenzen, Obergrenze, Ablauf).
- UI: Navigation mit vier Bereichen, „Sendung“ (Warteschlange, Skip/Entfernen mit Undo),
  „Redaktion“ (nur Musik), „Technik“ (umgezogene Karten), `inbox.html` vorerst unverändert
  verlinkt als „Rufen“.
- **Akzeptanz**: 2 h Dauerbetrieb ohne Stille, auch mit abgeschaltetem LLM (Füllprogramm);
  keine Doppel-Begrüßung; keine Wiederholung innerhalb 120 min; höchstens 1 Ansage je 3 Songs;
  Neustart mitten im Block setzt mit dem nächsten Segment fort.
- **Tests**: Queue-Reihenfolge, Ablauf, Füllstand, Cursor; Trigger feuert unter Schwelle genau
  einmal und merkt Folge-Lauf vor; Backoff; Schutzschalter.

### Phase 2 - Leitstelle & „Rufen“ (ohne Unterbrechung)
- Zwischenruf-Datensätze mit `actions`, sofortiger Trigger, Leitstellen-Prompt mit
  Beispieltabelle, alle Tools: `play_next`, `reply`, `add_music_wish`, `note_for_news`,
  direkte Plugin-Aktionen (Hue) und Info-Plugins. `play_now`, `breaking` und
  `reply(when="now")` gibt es schon, sie werden aber bis Phase 4 zu „als Nächstes“
  herabgestuft (UI sagt das). Sammeln & Zusammenführen der Programm-Aktionen am Laufende,
  Spur `reply`, Wunsch-Postfach + Kontext für Musikredaktion und Reserve, Meldungs-Postfach
  (wird gespeichert, ab Phase 3 verwendet), Retry mit Backoff bei LLM-Ausfall,
  Inbox-Migration, Sprache asynchron mit Prüfschritt, `author`.
- UI: „Rufen“ mit Gesprächsverlauf, Aktions-Zeilen, Status, ETA, Rückgängig; Leitstellen-Karte
  auf „Redaktion“, offene Wünsche im Musik-Detail.
- **Akzeptanz**: „Licht an“ ⇒ Licht geht binnen ~10 s an, Programm unverändert, Bestätigung in
  der UI; Frage per Text ⇒ Antwort läuft nach dem aktuellen Song, Absenden bis „eingeplant“
  < 30 s; „Licht an und spiel als Nächstes Queen“ ⇒ beide Aktionen, Ansage vor dem Song (ein
  Beitrag); „Beatles demnächst“ ⇒ taucht in einem der nächsten 2 Musikblöcke auf; „jetzt“ ⇒
  läuft als Nächstes mit Hinweis; Zwischenruf während eines laufenden Leitstellen-Laufs wird
  danach bearbeitet; bei abgeschaltetem LLM bleibt der Zwischenruf offen und wird nach
  Wiederkehr abgearbeitet.
- **Tests**: Zusammenführen `reply` + `play_next` zu einem Beitrag, Herabstufung, Wunsch-Ablauf,
  Retry/Backoff, Sendepause (direkte Aktion läuft, Programm wartet) mit `ScriptedLLM`.

### Phase 3 - Nachrichtenredaktion
- Slot-Trigger mit Format voll/kurz, `schedule_news` (`after_song`), Meldungs-Postfach als
  Quelle (Hinweise der Leitstelle aus Phase 2 werden jetzt verwendet, `used`), Slot-Marker auf
  der Skala, News-Redaktion mit Postfach-Ansicht auf „Redaktion“.
- **Akzeptanz**: Nachrichten zu jedem Slot nach Song-Ende; „Morgen ist Sperrmüll“ steht in der
  nächsten Ausgabe und bis `valid_until` in den vollen Ausgaben; verpasste Ausgaben verfallen.

### Phase 4 - Unterbrechen
- `sofort` wird freigeschaltet: `play_now`, `breaking`, `reply(when="now")` mit Preempt,
  Fade/Pause/Fortsetzen bzw. Verwerfen des Songs (Spotify + lokal), 403-Erkennung,
  Schutzabstand + `interrupt_available_at`, nur Musik wird unterbrochen, `hold` außerhalb des
  Sendefensters, `dispatch.allow_interrupt`, `news.placement: on_time` mit Timer.
- **Akzeptanz**: „Unwetterwarnung für heute Abend!“ ⇒ Song blendet binnen ~20 s aus, Meldung,
  Song läuft an gleicher Stelle weiter, Meldung steht in den nächsten Nachrichten; „Spiel jetzt
  Bohemian Rhapsody“ ⇒ ausblenden, kurze Ansage, Song, danach Programm; läuft gerade eine
  Ansage, kommt die Unterbrechung direkt danach; zweite Unterbrechung innerhalb 10 min wird zu
  „als Nächstes“ (UI zeigt ab wann wieder); mit `allow_interrupt: false` wird nie
  unterbrochen.

### Phase 5 - Extras
- Podcasts (auch per `play_now`/`play_next` mit `episode_query`), Ducking (falls der Spike positiv war), Plugin-Ideen (Essensplan/Rezepte),
  Nachrichten-Signation als Audio-Asset, PWA-Manifest für „Rufen“.

## 8. Risiken

| Risiko | Gegenmaßnahme |
|---|---|
| Migration verliert Nutzereinstellungen | Migration im neuen Script + beim Start, idempotent, Test mit Update-Simulation (Phase 0) |
| LLM fällt aus ⇒ Stille | Füllprogramm, Backoff; Zwischenrufe bleiben offen und werden erneut versucht (UI sagt das) |
| raspotify und ffplay teilen sich das Audiogerät nicht | Spike in Phase 0; Ducking erst Phase 5 |
| Spotify-Gerät weg ⇒ Warteschlange leert sich im Sekundentakt | Schutzschalter im Player |
| Spotify-Volume 403 / Rate-Limits | einmalige Erkennung ⇒ ohne Fade; kein 1-s-Polling, `stop_event.wait` |
| Leitstelle stuft zu viel als „sofort“ ein | Schutzabstand mit Herabstufung, nur Musik wird unterbrochen, Beispiele im Prompt, `allow_interrupt` zum Abschalten, jede Aktion sichtbar mit Rückgängig |
| Leitstelle versteht falsch und schaltet z.B. das falsche Licht | nur in der Leitstelle freigegebene Plugins, Bestätigung im Gesprächsverlauf, Plugin-Rückmeldung im Ergebnis |
| Antwort und Wunschsong laufen in falscher Reihenfolge | Programm-Aktionen eines Laufs werden gesammelt und als ein Beitrag geschrieben |
| Tool-Calling des Cloud-Modells unzuverlässig | tolerante Tools, Rückmeldungen im Ergebnis, Tests mit `ScriptedLLM` |
| Pi-Last (STT + Piper + LLM) | Lock ums Piper-Rendern, keine Kontext-Plugins in der Leitstelle, Info-Plugins gecacht |
| Race Conditions Warteschlange | ein Lock, atomare Writes, nur Player ändert Status, ein Worker |

## 9. Bewusst gestrichen für v1

Ducking (⇒ Phase 5), `withdraw` für Redaktionen, persistenter Redaktions-Status,
Migration von `current_script.json`, `/api/inbox`-Aliase, ausgegraute „kommt bald“-Karten,
SSE, globaler LLM-Semaphore, Eilmeldungs-Knopf/-Umschalter (die Leitstelle entscheidet),
wörtliches Vorlesen als Fallback, Rückgängig für direkte Aktionen (Licht wieder aus = neuer
Zwischenruf).

## 10. Entscheidungen & offene Fragen

Entschieden:
- Nachrichten zur vollen (ausführlich) und halben Stunde (kurz).
- Füllprogramm stellt der Agent als Reserve zusammen (gespielte Titel, Lieblings-Playlists,
  Wünsche aus den Prompts und dem Wunsch-Postfach); Lieblings-Playlists sind der letzte
  Fallback.
- Statt Studio eine Leitstelle: sie entscheidet die Dringlichkeit jedes Zwischenrufs, es gibt
  keinen Eilmeldungs-Knopf. Unterbrechungen außerhalb des Sendefensters warten auf den
  Sendebeginn, direkte Aktionen laufen immer.

Offen:
1. Dürfen alle Absender direkte Aktionen auslösen (Licht), oder braucht es je Namens-Chip eine
   Freigabe, z.B. für Kinder (Vorschlag: alle, Plugins je Leitstelle abschaltbar)?
2. Antworten der Leitstelle aus der Sendepause: zum Sendebeginn senden oder nur in der UI
   zeigen, weil sie dann veraltet sind (Vorschlag: nur Musikwünsche/Hinweise übernehmen,
   Antworten nur in der UI)?
