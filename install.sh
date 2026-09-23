#!/usr/bin/env bash
# open home fm - interactive installer.
#
# Sets up the Python environment, asks for the configuration needed to actually run (LLM
# provider + credentials, music source, TTS/STT), and writes .env + config/config.yaml
# accordingly. Safe to re-run - existing answers are offered as defaults.
#
# Usage (already cloned):  ./install.sh
# Usage (one-liner):       bash -c "$(curl -fsSL https://raw.githubusercontent.com/LeonNonnast/open_home_fm/main/install.sh)"
#
# Note: use `bash -c "$(curl ...)"`, not `curl ... | bash` - piping into bash consumes stdin
# with the script itself, which breaks the interactive prompts below.
set -euo pipefail

REPO_URL="https://github.com/LeonNonnast/open_home_fm.git"

# No local checkout yet (e.g. fetched standalone via the one-liner above)? Clone one and
# re-exec this same script from inside it, so the rest of the installer can assume it's
# running from within the repo.
if [ ! -f "pyproject.toml" ]; then
  if ! command -v git >/dev/null 2>&1; then
    echo "git fehlt - zuerst installieren: sudo apt-get install git" >&2
    exit 1
  fi
  TARGET_DIR="${OPEN_HOME_FM_DIR:-$PWD/open_home_fm}"
  if [ -d "$TARGET_DIR/.git" ]; then
    echo "==> Bestehende Installation in $TARGET_DIR gefunden, aktualisiere..."
    git -C "$TARGET_DIR" pull --ff-only
  else
    echo "==> Klone open home fm nach $TARGET_DIR ..."
    git clone "$REPO_URL" "$TARGET_DIR"
  fi
  cd "$TARGET_DIR"
  exec bash install.sh "$@"
fi

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

BOLD="$(tput bold 2>/dev/null || true)"
DIM="$(tput dim 2>/dev/null || true)"
RESET="$(tput sgr0 2>/dev/null || true)"

info()  { echo "${BOLD}==>${RESET} $1"; }
note()  { echo "    ${DIM}$1${RESET}"; }

ask() {
  # ask <prompt> <default> -> echoes the answer
  local prompt="$1" default="${2:-}" answer
  if [ -n "$default" ]; then
    read -r -p "$prompt [$default]: " answer || true
    echo "${answer:-$default}"
  else
    read -r -p "$prompt: " answer || true
    echo "$answer"
  fi
}

ask_secret() {
  # ask_secret <prompt> <existing value> -> echoes the answer; empty input keeps the existing value
  local prompt="$1" existing="${2:-}" answer
  if [ -n "$existing" ]; then
    prompt="$prompt [Enter = bisherigen Wert behalten]"
  fi
  read -r -s -p "$prompt: " answer || true
  echo >&2  # newline after the hidden input - to stderr, so it's not captured as part of the value
  echo "${answer:-$existing}"
}

ask_yes_no() {
  # ask_yes_no <prompt> <default: y|n> -> "true" or "false"
  local prompt="$1" default="$2" answer
  read -r -p "$prompt [${default}]: " answer || true
  answer="${answer:-$default}"
  [[ "$answer" =~ ^[yY] ]] && echo "true" || echo "false"
}

echo ""
echo "${BOLD}open home fm - Setup${RESET}"
echo "Beantworte ein paar Fragen, danach ist das System startklar."
echo ""

# ---------------------------------------------------------------------------
# 1. Prerequisites
# ---------------------------------------------------------------------------
# Checked before anything is installed, so a missing tool fails here with a clear hint instead
# of halfway through with a cryptic error. Each entry: name | check command | apt package | hint.
PREREQS=(
  "git|command -v git|git|https://git-scm.com/downloads"
  "curl|command -v curl|curl|https://curl.se/download.html"
  "python3 >= 3.11|python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))'|python3|Raspberry Pi OS Bookworm oder neuer liefert Python 3.11"
  "python3-venv|python3 -c 'import venv, ensurepip'|python3-venv|Paket python3-venv installieren"
  "ffplay (ffmpeg)|command -v ffplay|ffmpeg|https://ffmpeg.org/download.html"
)

check_prereqs() {
  # check_prereqs -> prints a status line per prerequisite, fills MISSING_* arrays
  MISSING_NAMES=(); MISSING_PKGS=(); MISSING_HINTS=()
  local entry name check pkg hint
  for entry in "${PREREQS[@]}"; do
    IFS='|' read -r name check pkg hint <<<"$entry"
    if bash -c "$check" >/dev/null 2>&1; then
      echo "    ${BOLD}[ok]${RESET}     $name"
    else
      echo "    ${BOLD}[fehlt]${RESET}  $name"
      MISSING_NAMES+=("$name"); MISSING_PKGS+=("$pkg"); MISSING_HINTS+=("$hint")
    fi
  done
}

info "Voraussetzungen prüfen"
check_prereqs
if [ "${#MISSING_NAMES[@]}" -gt 0 ] && command -v apt-get >/dev/null 2>&1; then
  if [ "$(ask_yes_no "Fehlende Pakete via apt installieren (${MISSING_PKGS[*]}, braucht sudo)?" y)" = "true" ]; then
    sudo apt-get update -y
    sudo apt-get install -y "${MISSING_PKGS[@]}"
    info "Erneut prüfen"
    check_prereqs
  fi
fi
if [ "${#MISSING_NAMES[@]}" -gt 0 ]; then
  echo ""
  echo "${BOLD}Installation abgebrochen - folgende Voraussetzungen fehlen:${RESET}"
  for i in "${!MISSING_NAMES[@]}"; do
    echo "  - ${MISSING_NAMES[$i]}: sudo apt-get install ${MISSING_PKGS[$i]}  (${MISSING_HINTS[$i]})"
  done
  echo "Danach ./install.sh erneut ausführen. Details: README.md -> Voraussetzungen."
  exit 1
fi

# ---------------------------------------------------------------------------
# 2. Python environment
# ---------------------------------------------------------------------------
info "Python-Umgebung"
if [ ! -d ".venv" ]; then
  python3 -m venv .venv
  note "Virtualenv angelegt unter .venv/"
else
  note "Bestehendes .venv/ gefunden, wird wiederverwendet."
fi
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -e .
note "Python-Abhängigkeiten installiert."

# ---------------------------------------------------------------------------
# 3. .env
# ---------------------------------------------------------------------------
ENV_FILE=".env"
if [ -f "$ENV_FILE" ]; then
  # shellcheck disable=SC1090
  source "$ENV_FILE"
fi

info "LLM-Provider"
echo "  1) Ollama (lokal oder Ollama Cloud) - Standard"
echo "  2) Anthropic (Claude API)"
LLM_CHOICE="$(ask "Auswahl" "1")"

if [ "$LLM_CHOICE" = "2" ]; then
  LLM_PROVIDER="anthropic"
  ANTHROPIC_MODEL="$(ask "Anthropic-Modell" "${ANTHROPIC_MODEL:-claude-sonnet-5}")"
  ANTHROPIC_API_KEY="$(ask_secret "Anthropic API-Key" "${ANTHROPIC_API_KEY:-}")"
else
  LLM_PROVIDER="ollama"
  OLLAMA_HOST="$(ask "Ollama-Host (leer = lokaler Daemon, sonst z.B. https://ollama.com)" "${OLLAMA_HOST:-https://ollama.com}")"
  OLLAMA_MODEL="$(ask "Ollama-Modell" "${OLLAMA_MODEL:-gpt-oss:120b-cloud}")"
  if [[ "$OLLAMA_HOST" == *"ollama.com"* ]]; then
    OLLAMA_API_KEY="$(ask_secret "Ollama Cloud API-Key" "${OLLAMA_API_KEY:-}")"
  else
    OLLAMA_API_KEY="${OLLAMA_API_KEY:-}"
  fi
fi

echo ""
info "Musikquelle"
echo "  1) Lokale Dateisammlung - Standard"
echo "  2) Spotify (via raspotify)"
MUSIC_CHOICE="$(ask "Auswahl" "1")"

if [ "$MUSIC_CHOICE" = "2" ]; then
  MUSIC_PROVIDER="spotify"
  note "Spotify-App unter https://developer.spotify.com/dashboard anlegen und dort als"
  note "Redirect-URI eintragen: http://127.0.0.1:8888/callback"
  # Credentials + one-time login in a loop: a failed login (wrong ID/secret, redirect URI
  # mismatch, ...) goes straight back to re-entering the credentials.
  while true; do
    SPOTIFY_CLIENT_ID="$(ask "Spotify Client-ID" "${SPOTIFY_CLIENT_ID:-}")"
    SPOTIFY_CLIENT_SECRET="$(ask_secret "Spotify Client-Secret" "${SPOTIFY_CLIENT_SECRET:-}")"
    SPOTIFY_REDIRECT_URI="$(ask "Spotify Redirect-URI" "${SPOTIFY_REDIRECT_URI:-http://127.0.0.1:8888/callback}")"
    if [ -z "$SPOTIFY_CLIENT_ID" ] || [ -z "$SPOTIFY_CLIENT_SECRET" ]; then
      note "Client-ID und Client-Secret dürfen nicht leer sein."
      continue
    fi
    if SPOTIFY_CLIENT_ID="$SPOTIFY_CLIENT_ID" SPOTIFY_CLIENT_SECRET="$SPOTIFY_CLIENT_SECRET" \
       SPOTIFY_REDIRECT_URI="$SPOTIFY_REDIRECT_URI" .venv/bin/python scripts/spotify_login.py; then
      break
    fi
    echo ""
    if [ "$(ask_yes_no "Zugangsdaten neu eingeben und erneut versuchen? (n = Login später nachholen)" y)" = "false" ]; then
      note "Login später nachholen mit: .venv/bin/python scripts/spotify_login.py"
      note "Bis dahin schlagen alle Spotify-Aufrufe fehl."
      break
    fi
  done
  SPOTIFY_DEVICE_NAME="$(ask "Name des raspotify Connect-Geräts" "${SPOTIFY_DEVICE_NAME:-open-home-fm}")"
  # The name above only reaches config.yaml - raspotify announces itself as
  # "raspotify (<hostname>)" unless LIBRESPOT_NAME is set in its own config, so the device
  # would never show up under this name in the Spotify app.
  RASPOTIFY_CONF="/etc/raspotify/conf"
  if [ "$(ask_yes_no "raspotify installieren/konfigurieren (braucht sudo)?" y)" = "true" ]; then
    if [ ! -f "$RASPOTIFY_CONF" ]; then
      curl -sL https://dtcooper.github.io/raspotify/install.sh | sh
    fi
    if sudo grep -qE '^#?\s*LIBRESPOT_NAME=' "$RASPOTIFY_CONF"; then
      sudo sed -i -E "s|^#?\s*LIBRESPOT_NAME=.*|LIBRESPOT_NAME=\"$SPOTIFY_DEVICE_NAME\"|" "$RASPOTIFY_CONF"
    else
      echo "LIBRESPOT_NAME=\"$SPOTIFY_DEVICE_NAME\"" | sudo tee -a "$RASPOTIFY_CONF" >/dev/null
    fi
    sudo systemctl restart raspotify
    note "raspotify läuft als '$SPOTIFY_DEVICE_NAME' - in der Spotify-App (gleiches WLAN) auswählbar."
  else
    note "Übersprungen - setze LIBRESPOT_NAME=\"$SPOTIFY_DEVICE_NAME\" in $RASPOTIFY_CONF selbst."
  fi
else
  MUSIC_PROVIDER="local"
  LIBRARY_PATH="$(ask "Pfad zur lokalen Musikbibliothek" "${LIBRARY_PATH:-data/library}")"
  mkdir -p "$LIBRARY_PATH"
fi

echo ""
info "Sprachausgabe (TTS)"
TTS_CHOICE="$(ask "Piper-Binary im PATH verfügbar? (n = TTS vorerst deaktivieren)" "y")"
if [[ "$TTS_CHOICE" =~ ^[yY] ]]; then
  TTS_ENGINE="piper"
  PIPER_VOICE_MODEL="$(ask "Pfad zum Piper-Stimmmodell (.onnx)" "${PIPER_VOICE_MODEL:-models/tts/de_DE-thorsten-medium.onnx}")"
  if [ ! -f "$PIPER_VOICE_MODEL" ]; then
    note "Hinweis: $PIPER_VOICE_MODEL existiert noch nicht - Modell von"
    note "https://github.com/rhasspy/piper/releases herunterladen und dort ablegen."
  fi
else
  TTS_ENGINE="none"
fi

echo ""
info "Audio-Ausgang"
AUDIO_OUTPUT_DEVICE="$(ask "ALSA/Pulse-Ausgabegerät (füttert die FM-Sendekette)" "${AUDIO_OUTPUT_DEVICE:-default}")"

echo ""
info "Agent-Loop"
LOOP_INTERVAL="$(ask "Intervall zwischen Durchläufen in Sekunden" "${LOOP_INTERVAL:-300}")"

# ---------------------------------------------------------------------------
# 4. Write .env
# ---------------------------------------------------------------------------
info "Schreibe $ENV_FILE"
cat > "$ENV_FILE" <<EOF
OLLAMA_API_KEY=${OLLAMA_API_KEY:-}
ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY:-}
SPOTIFY_CLIENT_ID=${SPOTIFY_CLIENT_ID:-}
SPOTIFY_CLIENT_SECRET=${SPOTIFY_CLIENT_SECRET:-}
SPOTIFY_REDIRECT_URI=${SPOTIFY_REDIRECT_URI:-http://127.0.0.1:8888/callback}
WEATHER_API_KEY=
EOF
chmod 600 "$ENV_FILE"

# ---------------------------------------------------------------------------
# 5. Write config/config.yaml
# ---------------------------------------------------------------------------
info "Schreibe config/config.yaml"
.venv/bin/python3 - "$LLM_PROVIDER" "${OLLAMA_MODEL:-}" "${OLLAMA_HOST:-}" "${ANTHROPIC_MODEL:-claude-sonnet-5}" \
  "$MUSIC_PROVIDER" "${SPOTIFY_DEVICE_NAME:-open-home-fm}" "${LIBRARY_PATH:-data/library}" \
  "$AUDIO_OUTPUT_DEVICE" "$TTS_ENGINE" "${PIPER_VOICE_MODEL:-models/tts/de_DE-thorsten-medium.onnx}" \
  "$LOOP_INTERVAL" <<'PYEOF'
import sys
import yaml
from pathlib import Path

(llm_provider, ollama_model, ollama_host, anthropic_model,
 music_provider, spotify_device, library_path,
 audio_output, tts_engine, piper_voice_model, loop_interval) = sys.argv[1:]

config_path = Path("config/config.yaml")
config = yaml.safe_load(config_path.read_text(encoding="utf-8"))

config["llm"]["provider"] = llm_provider
config["llm"]["ollama"]["model"] = ollama_model or config["llm"]["ollama"]["model"]
config["llm"]["ollama"]["host"] = ollama_host
config["llm"]["anthropic"]["model"] = anthropic_model

config["music"]["provider"] = music_provider
config["music"]["spotify"]["device_name"] = spotify_device
config["music"]["local"]["library_path"] = library_path

config["audio"]["output_device"] = audio_output

config["tts"]["engine"] = tts_engine
config["tts"]["piper"]["voice_model"] = piper_voice_model

config["agent"]["loop_interval_seconds"] = int(loop_interval)

config_path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")
print("config/config.yaml aktualisiert.")
PYEOF

# ---------------------------------------------------------------------------
# 6. Feature-specific checks
# ---------------------------------------------------------------------------
# Things only needed for the chosen options. Not fatal - the config is written either way - but
# listed explicitly, since e.g. a missing raspotify only shows up later as "no device in the app".
OPEN_ITEMS=()
if [ "$MUSIC_PROVIDER" = "spotify" ]; then
  if ! systemctl cat raspotify >/dev/null 2>&1; then
    OPEN_ITEMS+=("raspotify fehlt: curl -sL https://dtcooper.github.io/raspotify/install.sh | sh")
  else
    if ! sudo grep -q "^LIBRESPOT_NAME=\"$SPOTIFY_DEVICE_NAME\"" /etc/raspotify/conf 2>/dev/null; then
      OPEN_ITEMS+=("raspotify-Name passt nicht: LIBRESPOT_NAME=\"$SPOTIFY_DEVICE_NAME\" in /etc/raspotify/conf setzen, dann sudo systemctl restart raspotify")
    fi
    if ! systemctl is-active --quiet raspotify; then
      OPEN_ITEMS+=("raspotify läuft nicht: sudo systemctl restart raspotify, Log: journalctl -u raspotify -n 50")
    fi
  fi
fi
if [ "$TTS_ENGINE" = "piper" ]; then
  if ! command -v piper >/dev/null 2>&1; then
    OPEN_ITEMS+=("piper nicht im PATH: Binary von https://github.com/rhasspy/piper/releases installieren")
  fi
  if [ ! -f "$PIPER_VOICE_MODEL" ] || [ ! -f "$PIPER_VOICE_MODEL.json" ]; then
    OPEN_ITEMS+=("Piper-Stimmmodell fehlt: $PIPER_VOICE_MODEL + .onnx.json von https://github.com/rhasspy/piper/releases ablegen")
  fi
fi

echo ""
if [ "${#OPEN_ITEMS[@]}" -gt 0 ]; then
  info "Noch offen - ohne diese Punkte funktionieren die gewählten Optionen nicht:"
  for item in "${OPEN_ITEMS[@]}"; do
    echo "  - $item"
  done
  echo ""
fi
info "Fertig!"
note "Start: .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000"
note "Web-UI dann unter http://<host>:8000/"
if [ "$MUSIC_PROVIDER" = "spotify" ]; then
  note "Spotify-Login erneuern (z.B. anderer Account): .venv/bin/python scripts/spotify_login.py --force"
fi
