"""Leichtgewichtiger Beta-Selbsttest für Jarvis-Beta-v1 (ohne private Daten)."""

from __future__ import annotations

import py_compile
import sys
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# 1. Syntax-Check über alle Python-Dateien
py_files = sorted(ROOT.glob("*.py"))
for p in py_files:
    py_compile.compile(str(p), doraise=True)
print(f"[OK] {len(py_files)} Python-Dateien kompiliert.")

# 2. Offene Public Beta ohne Lizenzsperre prüfen
from license_manager import LICENSE_MANAGER

lic = LICENSE_MANAGER.get_status()
assert lic["activated"] is True and lic["needs_activation"] is False, f"Key-Sperre aktiv: {lic}"
assert "PUBLIC BETA" in lic["beta_version"].upper()
print(f"[OK] Public Beta Status offen: {lic['beta_version']} ({lic['tier']})")

# 3. Monitor-Erkennung (1-3 Monitore) prüfen
import multi_monitor

mons = multi_monitor.get_connected_monitors()
assert len(mons) >= 1
print(f"[OK] Monitor-Auto-Erkennung: {len(mons)} Bildschirm(e)")

# 4. FastAPI HUD & Multi-Window Routen prüfen
from gui import JarvisGUI

gui = JarvisGUI(fullscreen=False)
client = TestClient(gui.app)
for route in ("/", "/window/screen-matrix", "/window/system-telemetry", "/static/tactical-os.css", "/api/monitors"):
    r = client.get(route)
    assert r.status_code == 200, f"GET {route} -> {r.status_code}"

root_html = client.get("/").text
assert "PUBLIC BETA v0.1" in root_html
assert "LIZENZ AKTIVIEREN" not in root_html
assert "7-TAGE-TESTMODUS" not in root_html
print("[OK] Alle HUD-Routen, Tactical OS Assets & Open-Beta UI geprüft.")

gui._stop_event.set()
print("BETA_SELF_CHECK_PASSED")
