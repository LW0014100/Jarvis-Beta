# J.A.R.V.I.S. // Tactical Spatial OS (`PUBLIC BETA v0.1`)

[![Beta Status](https://img.shields.io/badge/Release-Public%20Beta%20v0.1-00F0FF?style=flat-square)](#)
[![Platforms](https://img.shields.io/badge/Platform-Windows%2011%20%7C%20Raspberry%20Pi%205-F8FAFC?style=flat-square)](#)
[![License: MIT](https://img.shields.io/badge/License-MIT-FFB020?style=flat-square)](./LICENSE)

> **Deutsch** | [English](#english)

---

## Deutsch

**J.A.R.V.I.S. Command Center (`PUBLIC BETA v0.1`)** ist ein schlankes, lokales KI-Desktop-Betriebssystem im **Tactical Spatial OS / Precision Cyber-Glass**-Design für **Windows 10/11** und **Raspberry Pi 5 (ARM64)** – ohne Cloud-Zwang, ohne Abo-Gebühren und in dieser Public Beta **komplett ohne Lizenzschlüssel-Sperre**.

### Kern-Features
- **3D Neural Core (`Three.js` + Custom GLSL Shader):** Interaktives Echtzeit-3D-Gehirn mit Cortical-Point-Cloud (bis zu 140k Punkte auf Desktop, automatischer Lite-Modus auf Raspberry Pi 5), 96-Bin Audio-Spektrum-Visualizer, Schockwellen bei Workflow-Spitzen und 3D-Scan-Laserstrahl Richtung Zielmonitor.
- **Multi-Monitor-Vision (1 bis 3 Bildschirme):** Automatische Erkennung von 1, 2 oder 3 Monitoren (`mss` + `screeninfo`), Live-Stream über binäres WebSocket (`/ws/monitors`), **Cursor-Aware Vision** (erkennt aktiv den Bildschirm unter dem Mauszeiger), aufsplittbares **Window 2 (`/window/screen-matrix`)** sowie OCR-/Screen-Analyse & Fenster-Verschiebung per Sprache.
- **Voice-Control & lokale ONNX-Pipeline:** Direkte Mikrofon-Anbindung über `sounddevice` (PortAudio im normalen Benutzerkontext – keine blockierten Dienste), Wake-Word (`openwakeword`), Whisper (`faster-whisper` ONNX/int8) sowie `edge-tts` / `piper-tts`.
- **n8n Bridge & Workflow Architect:** Unabhängige REST-API- und Webhook-Integration für eine selbst gehostete **n8n (Community Edition)** unter `http://localhost:5678` (mit automatischem Webhook-Fallback, Starter-Templates in `n8n_workflows/` und Echtzeit-Pipeline-Karten in **Window 3 `/window/system-telemetry`**).
- **Zero-Config-Fallback:** Funktioniert out-of-the-box auch **ohne** eigenen Gemini-/LLM-Key (nutzt kostenlose Keyless-Online-KI, lokales Ollama oder den integrierten Web-Demo-/Offline-Preview-Modus direkt im Browser).

### 3-Schritte-Schnellstart

#### Windows 10 / 11
1. **Installieren:** Repository herunterladen/entpacken und per Doppelklick **`1_Windows_Installieren.bat`** ausführen *(erstellt `.venv`, installiert ONNX-Pakete und lädt das deutsche Piper-Stimmmodell)*.
2. **Konfigurieren (optional):** `.env.example` nach `.env` kopieren (oder automatisch anlegen lassen) und eigene API-Keys (`GEMINI_API_KEY`, `GROQ_API_KEY`, `N8N_API_KEY`) eintragen – alternativ jederzeit später bequem im HUD unter **Einstellungen (⚙)** möglich.
3. **Starten:** Doppelklick auf **`Jarvis_starten.vbs`** *(startet sauber ohne schwarzes Konsolenfenster; Logs unter `jarvis.log`)* oder **`2_Windows_Jarvis_Starten.bat`** für Konsolen-Diagnose. Optional **`start_n8n.bat`** für lokales n8n ausführen.

#### Raspberry Pi 5 (64-Bit Debian Bookworm / ARM64)
1. **Installieren:**
   ```bash
   chmod +x *.sh
   ./1_Pi_Installieren.sh
   ```
2. **Konfigurieren (optional):** `.env` anpassen (z. B. `GEMINI_API_KEY`, `OLLAMA_TEXT_MODEL=gemma3:4b`, `N8N_API_KEY`) oder alles später direkt in den HUD-Einstellungen speichern.
3. **Starten:**
   ```bash
   ./jarvis_start.sh
   ```
   Optional lokales n8n starten mit `./start_n8n.sh`.

### Nützliche HUD-Hotkeys
- **`F9`:** Clean- / Recording-Modus (blendet Neben-Controls aus – Fokus auf 3D-Core, Live-Monitore & Live-Command-Bar)
- **`F10` (oder `F8`):** Startet die 10-Sekunden-Showcase-Sequenz
- **`ESC` / Maus-Ecke:** Sofortiger Not-Stopp für autonome Aktionen

### Bekannte Einschränkungen (Public Beta v0.1)
- **Erster Start:** Der erstmalige Download des Piper-Stimmmodells sowie optionaler Whisper-/Ollama-Modelle benötigt kurz Internetzugang.
- **Wayland / Pi 5 Screenshots:** Je nach Compositor auf Raspberry Pi OS kann für Vollbild-Screenshots `grim` / `scrot` nötig sein (wird von `1_Pi_Installieren.sh` mitinstalliert).
- **Smart-Home (HTTP / Pi 5 GPIO):** Optionale lokale HTTP-/Shelly-Webhooks oder Pi-5-GPIO-Relais über `devices.json` (siehe `devices.example.json`).
- **Feedback & Bug-Reports:** Bitte Fehler, Hardware-Logs (`jarvis.log`) und Feature-Wünsche direkt über **GitHub Issues** melden!

---

<a id="english"></a>

## English

**J.A.R.V.I.S. Command Center (`PUBLIC BETA v0.1`)** is a lightweight, local-first AI desktop OS & HUD built in **Tactical Spatial OS / Precision Cyber-Glass** style for **Windows 10/11** and **Raspberry Pi 5 (ARM64)** – zero subscriptions, no PyTorch bloat, and **no license-key gate** during this Open Public Beta.

### Key Features
- **3D Neural Core (`Three.js` + Custom GLSL Shader):** Interactive 3D cortex point-cloud (up to 140k micro-points on desktop, auto-scaled lite mode on ARM/Pi 5), 96-bin circular audio spectrum visualizer, compute shockwaves, and a 3D scan laser targeting the active monitor tile.
- **Multi-Monitor Vision (1 to 3 Displays):** Auto-detects 1, 2, or 3 displays (`mss` + `screeninfo`), low-latency binary WebSocket feed (`/ws/monitors`), **Cursor-Aware Vision** tracking the monitor under your mouse, dedicated **Window 2 (`/window/screen-matrix`)**, OCR analysis, and voice-driven window movement.
- **Voice Control & Lightweight ONNX Pipeline:** User-context microphone capture via `sounddevice` (PortAudio), wake-word (`openwakeword`), speech-to-text (`faster-whisper` int8), and neural TTS (`edge-tts` / `piper-tts`).
- **n8n Bridge & Workflow Architect:** Independent REST-API and webhook bridge for a self-hosted **n8n Community Edition** (`http://localhost:5678`), including automatic local webhook fallback, starter templates in `n8n_workflows/`, and live pipeline cards in **Window 3 (`/window/system-telemetry`)**.
- **Out-of-the-Box Fallback:** Works immediately even without API keys via keyless online fallback, local Ollama (`qwen2.5:7b` / `gemma3:4b`), or the built-in browser Web-Demo / Offline-Preview mode.

### 3-Step Quickstart

#### Windows 10 / 11
1. **Install:** Run **`1_Windows_Installieren.bat`** *(creates `.venv`, installs dependencies, and downloads the Piper voice model)*.
2. **Configure (Optional):** Copy `.env.example` to `.env` (or let first launch create it) and optionally add your `GEMINI_API_KEY`, `GROQ_API_KEY`, or `N8N_API_KEY` – or edit everything directly in the HUD **Settings (⚙)** panel.
3. **Launch:** Double-click **`Jarvis_starten.vbs`** *(silent launch, logs to `jarvis.log`)* or **`2_Windows_Jarvis_Starten.bat`** for console diagnostics. Run **`start_n8n.bat`** to start local n8n.

#### Raspberry Pi 5 (64-Bit ARM64 / Debian Bookworm)
1. **Install:**
   ```bash
   chmod +x *.sh
   ./1_Pi_Installieren.sh
   ```
2. **Configure (Optional):** Add optional LLM or n8n keys in `.env` or later inside the HUD settings UI.
3. **Launch:**
   ```bash
   ./jarvis_start.sh
   ```
   Optional local n8n: `./start_n8n.sh`.

### Known Limitations (Public Beta v0.1)
- First-time installation downloads ONNX voice models (`Piper`) and optional `Ollama` / `Whisper` weights.
- Optional generic HTTP/GPIO smart-home webhooks can be configured via `devices.json` (`devices.example.json`).
- Please report bugs, attach sanitized logs, and request features via **GitHub Issues**!

---

### Rechtliche Hinweise & Marken
- **Lizenz:** Dieses Projekt steht unter der [MIT License](./LICENSE) (Bereitstellung „AS IS“ ohne Gewährleistung oder Haftung).
- **Marken- & Unabhängigkeitshinweis (Disclaimer):** *J.A.R.V.I.S. // Tactical Spatial OS* ist ein unabhängiges Open-Source-Softwareprojekt. Es steht in **keinerlei Verbindung, Partnerschaft oder Lizenzbeziehung** zu **Marvel**, **The Walt Disney Company**, **n8n** oder anderen erwähnten Marken und Plattformen (wie Google/Gemini, Groq, OpenAI, DeepSeek, OpenRouter, Ollama oder Raspberry Pi). Die n8n-Workflow-Bridge ist eine unabhängige REST-/Webhook-Anbindung ohne offizielle n8n-Logos.

### Datenschutz (Lokale Verarbeitung)
- **Lokale Audio-, Sprach- & Screen-Pipeline:** Mikrofonaufnahme (`sounddevice`), Wake-Word (`openwakeword`), Spracherkennung (`faster-whisper` ONNX), Sprachausgabe (`piper-tts`), Langzeitgedächtnis und Multi-Monitor-Screenshots werden lokal auf deinem Gerät verarbeitet und **nicht** an Entwickler oder Dritte gesendet.
- **Optionale Cloud-Schnittstellen:** Externe LLM-APIs oder Web-Fonts (`fonts.googleapis.com` / `fonts.gstatic.com`) werden nur aktiv, wenn eigene Keys eingetragen bzw. der Online-Modus genutzt wird; im **OFFLINE MODE** arbeitet das System vollständig lokal.

### Legal Disclaimer & Privacy
- **License & Trademarks:** Distributed under the [MIT License](./LICENSE) ("AS IS", without warranty or liability). *J.A.R.V.I.S. // Tactical Spatial OS* is an independent open-source project **not affiliated with, endorsed by, or associated with** **Marvel**, **The Walt Disney Company**, **n8n**, or any other trademark holders. All trademarks belong to their respective owners.
- **Privacy (Local Processing):** Voice capture, wake-word detection, local ONNX STT/TTS, memory, and multi-monitor vision run locally on your machine with no developer telemetry. Optional cloud LLM endpoints require your own API keys or explicit online mode.
