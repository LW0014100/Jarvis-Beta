#!/usr/bin/env bash
# J.A.R.V.I.S. // Ein-Klick-Starter für Raspberry Pi 5 (Normaler Benutzerkontext)
# Nutzt direkt PortAudio/PipeWire im aktiven User-Kontext (kein Session-0 / systemd-Service),
# startet das schlanke FastAPI-Backend + Systembrowser im App-Modus (--app=) und loggt nach jarvis.log.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
LOG_FILE="$SCRIPT_DIR/jarvis.log"

if [ ! -x "$SCRIPT_DIR/.venv/bin/python" ]; then
    echo "[Fehler] Virtuelle Python-Umgebung (.venv) fehlt. Bitte zuerst './1_Pi_Installieren.sh' ausführen." | tee -a "$LOG_FILE"
    exit 1
fi

# Benutzerkontext für Audio (PipeWire / PulseAudio), GPIO (RP1 lgpio) und Display sicherstellen
USER_ID="$(id -u)"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$USER_ID}"
if [ -z "${DBUS_SESSION_BUS_ADDRESS:-}" ] && [ -S "$XDG_RUNTIME_DIR/bus" ]; then
    export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"
fi
if [ -z "${PULSE_SERVER:-}" ] && [ -S "$XDG_RUNTIME_DIR/pulse/native" ]; then
    export PULSE_SERVER="unix:$XDG_RUNTIME_DIR/pulse/native"
fi
export GPIOZERO_PIN_FACTORY="${GPIOZERO_PIN_FACTORY:-lgpio}"
export OMP_NUM_THREADS="${OMP_NUM_THREADS:-4}"
export OPENBLAS_NUM_THREADS="${OPENBLAS_NUM_THREADS:-4}"
export PYTHONUNBUFFERED=1
export PYTHONUTF8=1
export JARVIS_HUD_HOST="browser"

# Logdatei bei > 2 MB rotieren
if [ -f "$LOG_FILE" ] && [ "$(wc -c < "$LOG_FILE" 2>/dev/null || echo 0)" -gt 2097152 ]; then
    mv -f "$LOG_FILE" "$LOG_FILE.old" >/dev/null 2>&1 || true
fi

{
    echo "============================================================"
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starte J.A.R.V.I.S. auf $(uname -m) im Benutzerkontext..."
} >> "$LOG_FILE"

# Ollama lokal im Hintergrund starten (falls installiert & noch nicht aktiv)
if command -v ollama >/dev/null 2>&1; then
    if ! curl -sf --max-time 1 http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
        nohup ollama serve >/tmp/ollama-jarvis.log 2>&1 &
    fi
fi

# Starte Jarvis im normalen Benutzerkontext; bei Terminal-Aufruf auch live ausgeben
if [ -t 1 ]; then
    "$SCRIPT_DIR/.venv/bin/python" "$SCRIPT_DIR/main.py" "$@" 2>&1 | tee -a "$LOG_FILE"
    EXIT_CODE="${PIPESTATUS[0]}"
else
    "$SCRIPT_DIR/.venv/bin/python" "$SCRIPT_DIR/main.py" "$@" >> "$LOG_FILE" 2>&1
    EXIT_CODE=$?
fi

exit "$EXIT_CODE"
