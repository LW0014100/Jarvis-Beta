#!/usr/bin/env bash
# J.A.R.V.I.S. // Lokales n8n Community Edition für Raspberry Pi 5 (ARM64) & Linux (0 € Abo)
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
N8N_DATA_DIR="$SCRIPT_DIR/n8n_data"
mkdir -p "$N8N_DATA_DIR"

echo "============================================================"
echo "  J.A.R.V.I.S. // LOKALES N8N (http://localhost:5678)"
echo "============================================================"
echo "Datenordner: $N8N_DATA_DIR"

if curl -sf --max-time 2 http://127.0.0.1:5678/healthz >/dev/null 2>&1; then
    echo "[OK] n8n läuft bereits unter http://localhost:5678"
    exit 0
fi

# Option 1: Docker (offizielles ARM64/AMD64 Multi-Arch Image)
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    echo "[Docker] Starte kostenlose n8n Community Edition..."
    if docker inspect jarvis-n8n >/dev/null 2>&1; then
        docker start jarvis-n8n >/dev/null
    else
        docker run -d \
            --name jarvis-n8n \
            --restart unless-stopped \
            -p 5678:5678 \
            -v "$N8N_DATA_DIR:/home/node/.n8n" \
            -e N8N_SECURE_COOKIE=false \
            -e GENERIC_TIMEZONE=Europe/Berlin \
            -e TZ=Europe/Berlin \
            docker.n8n.io/n8nio/n8n
    fi

    for _i in $(seq 1 25); do
        sleep 1
        if curl -sf --max-time 1 http://127.0.0.1:5678/healthz >/dev/null 2>&1; then
            echo "[OK] Lokales n8n bereit unter http://localhost:5678"
            exit 0
        fi
    done
    echo "[Hinweis] n8n Container gestartet – Initialisierung dauert noch einige Sekunden."
    exit 0
fi

# Option 2: Fallback über Node.js / npx n8n
if command -v npx >/dev/null 2>&1; then
    echo "[Fallback: npx] Starte n8n im Hintergrund mit $N8N_DATA_DIR..."
    export N8N_USER_FOLDER="$N8N_DATA_DIR"
    export N8N_SECURE_COOKIE=false
    export N8N_HOST="0.0.0.0"
    export N8N_PORT=5678
    nohup npx --yes n8n start >> "$SCRIPT_DIR/n8n.log" 2>&1 &
    echo "[OK] n8n startet im Hintergrund (Log: n8n.log) -> http://localhost:5678"
    exit 0
fi

echo "[Fehler] Weder Docker noch npx/Node.js gefunden."
echo "Tipp für Pi 5: 'curl -fsSL https://get.docker.com | sh && sudo usermod -aG docker \$USER'"
exit 1
