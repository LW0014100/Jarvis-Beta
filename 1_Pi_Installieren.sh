#!/usr/bin/env bash
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

TARGET_USER="${SUDO_USER:-$USER}"
ARCH="$(uname -m)"
IS_PI5="no"
if [ -r /proc/device-tree/model ] && grep -qi "Raspberry Pi 5" /proc/device-tree/model 2>/dev/null; then
    IS_PI5="yes"
fi

echo "=================================================="
echo "  Jarvis - Plug & Play Setup (Raspberry Pi 5 ARM64)"
echo "=================================================="
echo "Architektur: $ARCH | Pi 5 erkannt: $IS_PI5"
echo ""

pause_exit() {
    local code="${1:-0}"
    echo ""
    read -rp "Drücke [Enter], um dieses Fenster zu schließen..."
    exit "$code"
}

if [ "$ARCH" != "aarch64" ] && [ "$ARCH" != "arm64" ]; then
    echo "[Warnung] Dieses Skript ist für 64-Bit ARM64 (aarch64 / Raspberry Pi OS Bookworm) optimiert."
    echo "          Gefundene Architektur: $ARCH (fahre trotzdem fort...)"
    echo ""
fi

if [ -r /etc/os-release ]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    echo "Betriebssystem: ${PRETTY_NAME:-Linux} (Codename: ${VERSION_CODENAME:-unbekannt})"
    if [ "${VERSION_CODENAME:-}" != "bookworm" ] && [ "${VERSION_CODENAME:-}" != "trixie" ]; then
        echo "[Hinweis] Empfohlen wird Raspberry Pi OS Bookworm (Debian 12, 64-Bit)."
    fi
fi

has_gui() {
    command -v zenity >/dev/null 2>&1 && { [ -n "${DISPLAY:-}" ] || [ -n "${WAYLAND_DISPLAY:-}" ]; }
}

echo ""
echo "[1/7] Installiere Linux-Systempakete (ARM64 Bookworm + RP1 + PipeWire + Wayland)..."
if ! sudo apt update; then
    echo "[Fehler] 'sudo apt update' ist fehlgeschlagen."
    pause_exit 1
fi

if ! sudo apt install -y \
    python3-venv python3-full python3-dev python3-tk python3-pil.imagetk \
    build-essential pkg-config swig \
    gpiod libgpiod-dev python3-libgpiod i2c-tools \
    portaudio19-dev libportaudio2 libsndfile1 libasound2-dev \
    ffmpeg libopenblas-dev libgl1 libglib2.0-0 \
    v4l-utils scrot xdotool playerctl pulseaudio-utils espeak-ng curl zenity; then
    echo "[Fehler] Basis-Systempakete konnten nicht installiert werden."
    pause_exit 1
fi

# Optionale Bookworm-/Wayland-/PipeWire-/RP1-Pakete (je nach Apt-Quelle)
for opt_pkg in \
    liblgpio-dev \
    pipewire-alsa \
    pipewire-pulse \
    grim \
    wtype \
    wl-clipboard \
    python3-gi \
    python3-gi-cairo \
    gir1.2-gtk-3.0 \
    gir1.2-webkit2-4.1 \
    python3-picamera2 \
    python3-libcamera \
    rpicam-apps; do
    sudo apt install -y "$opt_pkg" >/dev/null 2>&1 || true
done

sudo apt install -y chromium >/dev/null 2>&1 || sudo apt install -y chromium-browser >/dev/null 2>&1 || true

echo ""
echo "[2/7] Konfiguriere Hardware-Gruppen (RP1 GPIO, I2C, SPI, UART, Kamera, Audio)..."
for grp in gpio i2c spi dialout video audio render input; do
    if getent group "$grp" >/dev/null 2>&1; then
        sudo usermod -aG "$grp" "$TARGET_USER" >/dev/null 2>&1 || true
    fi
done
echo "Benutzer '$TARGET_USER' für RP1-Schnittstellen berechtigt (ggf. einmal neu anmelden)."

echo ""
echo "[3/7] Erstelle sauberes Python Virtual Environment (.venv / PEP 668)..."
# Falls der Projektordner inkl. Windows-.venv auf den Pi kopiert wurde, .venv sauber neu aufsetzen
if [ -d "$SCRIPT_DIR/.venv" ] && [ ! -x "$SCRIPT_DIR/.venv/bin/python" ]; then
    echo "[Hinweis] Fremde oder beschädigte .venv erkannt (z. B. von Windows) – erstelle Linux-ARM64 .venv neu..."
    rm -rf "$SCRIPT_DIR/.venv"
fi

if [ ! -d "$SCRIPT_DIR/.venv" ]; then
    if ! python3 -m venv --system-site-packages "$SCRIPT_DIR/.venv"; then
        echo "[Fehler] Konnte .venv nicht erstellen."
        pause_exit 1
    fi
fi

"$SCRIPT_DIR/.venv/bin/python" -m pip install --no-cache-dir --upgrade pip wheel setuptools
if ! "$SCRIPT_DIR/.venv/bin/pip" install --no-cache-dir -r "$SCRIPT_DIR/requirements.txt"; then
    echo "[Fehler] Python-Basisabhängigkeiten konnten nicht installiert werden."
    pause_exit 1
fi

if [ -f "$SCRIPT_DIR/requirements-rpi5.txt" ]; then
    echo "Installiere Raspberry Pi 5 RP1-Hardware-Treiber (gpiod, lgpio, gpiozero, serial, i2c, spi)..."
    if ! "$SCRIPT_DIR/.venv/bin/pip" install --no-cache-dir -r "$SCRIPT_DIR/requirements-rpi5.txt"; then
        echo "[Warnung] Einige optionale RP1-Pakete konnten nicht per pip gebaut werden (System-Pakete bleiben aktiv)."
    fi
fi

echo ""
echo "[4/7] Lade deutsches Piper-Stimmmodell (de_DE-karlsson-low)..."
mkdir -p "$SCRIPT_DIR/models"

if [ ! -f "$SCRIPT_DIR/models/de_DE-karlsson-low.onnx" ]; then
    if ! curl -fL -o "$SCRIPT_DIR/models/de_DE-karlsson-low.onnx" \
        "https://huggingface.co/rhasspy/piper-voices/resolve/main/de/de_DE/karlsson/low/de_DE-karlsson-low.onnx"; then
        echo "[Fehler] Download von de_DE-karlsson-low.onnx fehlgeschlagen."
        pause_exit 1
    fi
fi

if [ ! -f "$SCRIPT_DIR/models/de_DE-karlsson-low.onnx.json" ]; then
    if ! curl -fL -o "$SCRIPT_DIR/models/de_DE-karlsson-low.onnx.json" \
        "https://huggingface.co/rhasspy/piper-voices/resolve/main/de/de_DE/karlsson/low/de_DE-karlsson-low.onnx.json"; then
        echo "[Fehler] Download von de_DE-karlsson-low.onnx.json fehlgeschlagen."
        pause_exit 1
    fi
fi

echo ""
echo "[5/7] Installiere Ollama (ARM64) und lade Pi-5-Modelle (gemma3:4b & moondream)..."
if ! command -v ollama >/dev/null 2>&1; then
    if ! curl -fsSL https://ollama.com/install.sh | sh; then
        echo "[Fehler] Ollama-Installation fehlgeschlagen."
        pause_exit 1
    fi
else
    echo "Ollama ist bereits installiert."
fi

if ! curl -sf http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
    sudo systemctl enable --now ollama >/dev/null 2>&1 || true
    sleep 2
fi

if ! curl -sf http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
    echo "Starte Ollama-Dienst temporär für den Modell-Download..."
    nohup ollama serve >/tmp/ollama-jarvis-setup.log 2>&1 &
    for _i in $(seq 1 12); do
        sleep 1
        if curl -sf http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
            break
        fi
    done
fi

if ! ollama pull gemma3:4b; then
    echo "[Fallback] Modell 'gemma3:4b' nicht geladen - versuche 'qwen2.5:3b'..."
    ollama pull qwen2.5:3b || echo "[Warnung] Sprachmodell konnte nicht geladen werden."
fi

if ! ollama pull moondream; then
    echo "[Warnung] Modell 'moondream' konnte nicht geladen werden."
fi

echo ""
echo "[6/7] Lege Pi-5-Konfiguration (.env) an..."
if [ ! -f "$SCRIPT_DIR/.env" ]; then
    if [ -f "$SCRIPT_DIR/.env.example" ]; then
        cp "$SCRIPT_DIR/.env.example" "$SCRIPT_DIR/.env"
        sed -i 's/^OLLAMA_TEXT_MODEL=.*/OLLAMA_TEXT_MODEL=gemma3:4b/' "$SCRIPT_DIR/.env"
        echo ".env aus .env.example erstellt (Standardmodell für Pi 5: gemma3:4b)."
    else
        cat > "$SCRIPT_DIR/.env" <<'EOF'
IS_ONLINE=true
OLLAMA_HOST=http://127.0.0.1:11434
OLLAMA_TEXT_MODEL=gemma3:4b
OLLAMA_VISION_MODEL=moondream
ALARM_GPIO_PIN=17
GPIOZERO_PIN_FACTORY=lgpio
FULLSCREEN=true
EOF
        echo "Pi-5-Standard-.env erstellt."
    fi
else
    echo "Bestehende .env beibehalten."
fi
echo "API-Keys, E-Mail, Watchlist und Stadt kannst du direkt im HUD unter 'EINSTELLUNGEN' eintragen."

echo ""
echo "[7/7] Desktop-Verknüpfung & Autostart-Werkzeuge einrichten..."
chmod +x "$SCRIPT_DIR/"*.sh

DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
mkdir -p "$DESKTOP_DIR"
DESKTOP_FILE="$DESKTOP_DIR/Jarvis.desktop"

cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Type=Application
Name=Jarvis Assistent
Comment=Autonomer Desktop-KI-Assistent (Raspberry Pi 5)
Exec=/bin/bash "$SCRIPT_DIR/jarvis_start.sh"
Path=$SCRIPT_DIR
Icon=audio-input-microphone
Terminal=false
Categories=Utility;AudioVideo;
EOF

chmod +x "$DESKTOP_FILE"
gio set "$DESKTOP_FILE" metadata::trusted true >/dev/null 2>&1 || true
echo "Desktop-Verknüpfung erstellt: $DESKTOP_FILE"

AUTOSTART_DIR="$HOME/.config/autostart"
mkdir -p "$AUTOSTART_DIR"
cat > "$SCRIPT_DIR/Autostart_Umschalten_Pi.sh" <<EOF
#!/usr/bin/env bash
TARGET="$AUTOSTART_DIR/jarvis.desktop"
if [ -f "\$TARGET" ]; then
    rm -f "\$TARGET"
    echo "Jarvis-Autostart wurde deaktiviert."
else
    cp "$DESKTOP_FILE" "\$TARGET"
    echo "Jarvis startet ab sofort im normalen Benutzerkontext beim Desktop-Login (mit vollem Mikrofon-Zugriff)."
fi
read -rp "Drücke [Enter], um fortzufahren..."
EOF
chmod +x "$SCRIPT_DIR/Autostart_Umschalten_Pi.sh"

echo ""
echo "=================================================="
echo "  Raspberry Pi 5 Installation abgeschlossen!"
echo "=================================================="
echo "Starte Jarvis ab jetzt per Doppelklick auf 'Jarvis Assistent'"
echo "auf deinem Desktop oder über './jarvis_start.sh' (mit Log: jarvis.log)."

exit 0
