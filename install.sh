#!/usr/bin/env bash
# open home fm - interactive installer.
#
# Sets up the Python environment, asks for the configuration needed to actually run (LLM
# provider + credentials, music source, TTS/STT), and writes .env + config/config.yaml
# accordingly. Safe to re-run - existing answers are offered as defaults.
#
# Usage: ./install.sh
set -euo pipefail

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
  local prompt="$1" answer
  read -r -s -p "$prompt: " answer || true
  echo
  echo "$answer"
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
# 1. System packages
# ---------------------------------------------------------------------------
info "System-Pakete"
if command -v apt-get >/dev/null 2>&1; then
  if [ "$(ask_yes_no "python3-venv und ffmpeg via apt installieren (braucht sudo)?" y)" = "true" ]; then
    sudo apt-get update -y
    sudo apt-get install -y python3-venv ffmpeg
  else
    note "Übersprungen - stelle sicher, dass 'python3 -m venv' und 'ffplay' verfügbar sind."
  fi
else
  note "Kein apt gefunden - stelle sicher, dass Python venv und ffmpeg/ffplay bereits installiert sind."
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
  ANTHROPIC_API_KEY="$(ask_secret "Anthropic API-Key")"
else
  LLM_PROVIDER="ollama"
  OLLAMA_HOST="$(ask "Ollama-Host (leer = lokaler Daemon, sonst z.B. https://ollama.com)" "${OLLAMA_HOST:-https://ollama.com}")"
  OLLAMA_MODEL="$(ask "Ollama-Modell" "${OLLAMA_MODEL:-gpt-oss:120b-cloud}")"
  if [[ "$OLLAMA_HOST" == *"ollama.com"* ]]; then
    OLLAMA_API_KEY="$(ask_secret "Ollama Cloud API-Key")"
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
  SPOTIFY_CLIENT_ID="$(ask "Spotify Client-ID" "${SPOTIFY_CLIENT_ID:-}")"
  SPOTIFY_CLIENT_SECRET="$(ask_secret "Spotify Client-Secret")"
  SPOTIFY_REDIRECT_URI="$(ask "Spotify Redirect-URI" "${SPOTIFY_REDIRECT_URI:-http://localhost:8000/api/music/spotify/callback}")"
  SPOTIFY_DEVICE_NAME="$(ask "Name des raspotify Connect-Geräts" "${SPOTIFY_DEVICE_NAME:-open-home-fm}")"
  note "Vergiss nicht, raspotify auf diesem Gerät zu installieren (siehe README)."
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
SPOTIFY_REDIRECT_URI=${SPOTIFY_REDIRECT_URI:-http://localhost:8000/api/music/spotify/callback}
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

echo ""
info "Fertig!"
note "Start: .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000"
note "Web-UI dann unter http://<host>:8000/"
if [ "$MUSIC_PROVIDER" = "spotify" ]; then
  note "Beim ersten Start öffnet spotipy einen OAuth-Flow im Terminal/Browser."
fi
