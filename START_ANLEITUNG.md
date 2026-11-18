# J.A.R.V.I.S. // Start- & n8n-Anleitung (PUBLIC BETA v0.1)

Dieses Projekt läuft vollständig lokal im normalen Benutzerkontext (**0 € Abo**, kein PyTorch, kein App-Wrapper, keine Lizenzsperre in der Public Beta).

---

## 1. Jarvis installieren & starten

### Windows 10 / 11 (1 bis 3 Monitore)
1. **Einmalig einrichten:** Doppelklick auf `1_Windows_Installieren.bat` (erstellt `.venv`, installiert die schlanke ONNX-Umgebung + `screeninfo` und lädt das deutsche Piper-Stimmmodell).
2. **Optional Keys eintragen:** In `.env` (aus `.env.example`) oder direkt im HUD unter **Einstellungen (⚙)** eigene Keys (`GEMINI_API_KEY`, `GROQ_API_KEY`, `N8N_API_KEY`) ergänzen. Ohne eigene Keys startet Jarvis automatisch im kostenlosen Online-/Offline-Fallback.
3. **Normal starten (empfohlen, ohne schwarzes CMD-Fenster):** Doppelklick auf **`Jarvis_starten.vbs`**.
   - Öffnet das Command Center im App-Modus (`--app=`) des Systembrowsers.
   - Startet das Standard-Mikrofon direkt über PortAudio (`sounddevice`).
   - Alle Ausgaben landen sauber in **`jarvis.log`**.
4. **Diagnose-Start mit Konsole:** `2_Windows_Jarvis_Starten.bat`.

### Raspberry Pi 5 (ARM64 / Debian Bookworm)
1. Einmalig Rechte vergeben & installieren:
   ```bash
   chmod +x *.sh
   ./1_Pi_Installieren.sh
   ```
2. Jarvis im Benutzerkontext starten (PipeWire/PulseAudio + RP1 `lgpio`):
   ```bash
   ./jarvis_start.sh
   ```

---

## 2. Multi-Monitor (1–3 Bildschirme) & Hotkeys im HUD

- **Aufteilbare Fenster:** Oben rechts über den Monitor-Wähler (`M1`, `M2`, `M3`) mit **`⧉ TELEMETRY (F3)`** (Window 3: Sparklines, Prozesse & n8n-Pipelines) oder **`⧉ MATRIX (F2)`** (Window 2: 3-Monitor-Kontrollpult & OCR).
- **Sprachbefehle:**
  - *„Schau auf meinen linken / mittleren / rechten Bildschirm“* oder *„Schau auf alle drei Bildschirme“*
  - *„Öffne Analyse-Fenster auf Monitor 2“* / *„Verschiebe [Fenster] auf Monitor 3“*
- **Hotkeys:**
  - **`F9` (`CLEAN (F9)`):** Clean- / Recording-Modus (lässt 3D-Core, Live-Monitore, Telemetrie & Command-Bar sichtbar).
  - **`F10` / `F8` (`◈`):** Löst die 10-sekündige Showcase-Sequenz aus.

---

## 3. Kostenloses lokales n8n + Starter-Templates (`n8n_workflows/`)

### Lokales n8n starten (0 € Community Edition)
- **Windows:** Doppelklick auf **`start_n8n.bat`**
- **Raspberry Pi 5:** `./start_n8n.sh`

Startet n8n unter **`http://localhost:5678`** mit lokaler Speicherung in `n8n_data/`. Im Ordner `n8n_workflows/` liegen fertige Starter-Templates (`jarvis_quickstart_webhook.json`, `jarvis_system_briefing.json`).

### Verbindung mit Jarvis
1. In den HUD-Einstellungen → Tab **Kommunikation** optional deinen **n8n REST-API-Key** eintragen.
2. **Ohne API-Key (Webhook-Fallback):** Jarvis generiert valide Workflow-JSONs in `n8n_workflows/` und triggert Webhooks direkt über `http://localhost:5678/webhook/...`.
