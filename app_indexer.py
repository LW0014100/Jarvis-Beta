# -*- coding: utf-8 -*-
r"""
JARVIS Deep Windows App Scanner & Fuzzy Launcher (app_indexer.py)
=================================================================
Scans the entire Windows system in the background at startup and builds
a fast persistent index (`apps_cache.json`) covering:
1. Start Menu shortcuts (.lnk in %ProgramData% & %APPDATA%)
2. Desktop shortcuts & icons (.lnk, .url & .exe in %USERPROFILE%\Desktop & Public\Desktop)
3. Taskbar pinned shortcuts (.lnk & .url in User Pinned\TaskBar)
4. Windows UWP / Store Apps via `Get-StartApps | ConvertTo-Json` (CREATE_NO_WINDOW)
5. Windows Registry (`HKLM` & `HKCU` ...\CurrentVersion\App Paths)
6. Standard Program Folders (`C:\Program Files`, `C:\Program Files (x86)`, `%LOCALAPPDATA%\Programs`)

Supports fuzzy matching (`difflib` + token/substring matching) for natural voice
commands ("Öffne Xbox", "Starte Steam", "Mach den Rechner auf", "Öffne OrcaSlicer")
and a visual Windows-Search fallback (`Win` -> visible typing -> 1-FPS screenshot check -> `Enter`)
when an application is not found in the local cache.
"""

from __future__ import annotations

import difflib
import json
import logging
import os
import platform
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from config import get_base_dir, get_user_data_dir, is_frozen

logger = logging.getLogger("jarvis.app_indexer")
BASE_DIR = get_base_dir()
IS_WINDOWS = platform.system().lower() == "windows"
CREATE_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0

if is_frozen():
    APPS_CACHE_FILE = get_user_data_dir() / "apps_cache.json"
    if not APPS_CACHE_FILE.exists():
        _bundled_cache = BASE_DIR / "apps_cache.json"
        if _bundled_cache.is_file():
            try:
                APPS_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
                APPS_CACHE_FILE.write_bytes(_bundled_cache.read_bytes())
            except Exception:
                pass
else:
    APPS_CACHE_FILE = BASE_DIR / "apps_cache.json"

# Binaries that should be ignored when scanning Program Files directories
IGNORED_EXE_PATTERNS = (
    "unins",
    "uninstall",
    "crashpad",
    "crashhandler",
    "crashreporter",
    "updater",
    "update",
    "setup",
    "installer",
    "vc_redist",
    "vcredist",
    "dxsetup",
    "helper",
    "elevate",
    "elevation",
    "notification_helper",
    "unitycrashhandler",
    "QtWebEngineProcess",
    "createdump",
    "izzi",
    "7za",
    "bsdtar",
)

# German/English synonym & system app map mapped to canonical search keys or direct targets
BUILTIN_APP_SYNONYMS: dict[str, list[str]] = {
    "rechner": ["rechner", "calculator", "taschenrechner", "calc"],
    "taschenrechner": ["rechner", "calculator", "taschenrechner", "calc"],
    "calculator": ["calculator", "rechner", "calc"],
    "editor": ["editor", "notepad", "texteditor"],
    "notepad": ["notepad", "editor"],
    "explorer": ["explorer", "datei-explorer", "file explorer", "ordner"],
    "datei explorer": ["explorer", "datei-explorer", "file explorer"],
    "xbox": ["xbox", "xbox app", "microsoft.gamingapp"],
    "xbox app": ["xbox", "xbox app", "microsoft.gamingapp"],
    "steam": ["steam"],
    "orcaslicer": ["orcaslicer", "orca slicer", "orca-slicer"],
    "orca slicer": ["orcaslicer", "orca slicer", "orca-slicer"],
    "bambulab": ["bambu studio", "bambustudio"],
    "bambu studio": ["bambu studio", "bambustudio"],
    "cura": ["ultimaker cura", "cura"],
    "prusaslicer": ["prusaslicer", "prusa slicer"],
    "vscode": ["visual studio code", "code"],
    "visual studio code": ["visual studio code", "code"],
    "chrome": ["google chrome", "chrome"],
    "google chrome": ["google chrome", "chrome"],
    "edge": ["microsoft edge", "edge", "msedge"],
    "firefox": ["mozilla firefox", "firefox"],
    "brave": ["brave", "brave browser"],
    "opera": ["opera", "opera gx"],
    "spotify": ["spotify"],
    "discord": ["discord"],
    "vlc": ["vlc media player", "vlc"],
    "obs": ["obs studio", "obs64", "obs"],
    "blender": ["blender"],
    "gimp": ["gimp"],
    "word": ["word", "winword", "microsoft word"],
    "excel": ["excel", "microsoft excel"],
    "powerpoint": ["powerpoint", "powerpnt", "microsoft powerpoint"],
    "outlook": ["outlook", "microsoft outlook"],
    "teams": ["microsoft teams", "teams", "ms-teams"],
    "onenote": ["onenote"],
    "paint": ["paint", "mspaint"],
    "cmd": ["eingabeaufforderung", "command prompt", "cmd"],
    "eingabeaufforderung": ["eingabeaufforderung", "command prompt", "cmd"],
    "terminal": ["terminal", "windows terminal", "wt"],
    "powershell": ["windows powershell", "powershell"],
    "taskmanager": ["task-manager", "task manager", "taskmgr"],
    "task manager": ["task-manager", "task manager", "taskmgr"],
    "einstellungen": ["einstellungen", "settings", "ms-settings"],
    "settings": ["settings", "einstellungen", "ms-settings"],
    "systemsteuerung": ["systemsteuerung", "control panel", "control"],
    "snipping tool": ["snipping tool", "ausschneiden und skizzieren"],
}

# Guaranteed fast-path fallbacks for built-in Windows apps & URI protocols
DIRECT_WINDOWS_TARGETS: dict[str, dict[str, str]] = {
    "rechner": {
        "name": "Rechner (Calculator)",
        "target": "calc.exe",
        "kind": "exe",
        "source": "builtin",
    },
    "taschenrechner": {
        "name": "Rechner (Calculator)",
        "target": "calc.exe",
        "kind": "exe",
        "source": "builtin",
    },
    "calculator": {
        "name": "Rechner (Calculator)",
        "target": "calc.exe",
        "kind": "exe",
        "source": "builtin",
    },
    "calc": {
        "name": "Rechner (Calculator)",
        "target": "calc.exe",
        "kind": "exe",
        "source": "builtin",
    },
    "editor": {
        "name": "Editor (Notepad)",
        "target": "notepad.exe",
        "kind": "exe",
        "source": "builtin",
    },
    "notepad": {
        "name": "Editor (Notepad)",
        "target": "notepad.exe",
        "kind": "exe",
        "source": "builtin",
    },
    "explorer": {
        "name": "Windows Datei-Explorer",
        "target": "explorer.exe",
        "kind": "exe",
        "source": "builtin",
    },
    "datei-explorer": {
        "name": "Windows Datei-Explorer",
        "target": "explorer.exe",
        "kind": "exe",
        "source": "builtin",
    },
    "paint": {
        "name": "Paint",
        "target": "mspaint.exe",
        "kind": "exe",
        "source": "builtin",
    },
    "cmd": {
        "name": "Eingabeaufforderung",
        "target": "cmd.exe",
        "kind": "exe",
        "source": "builtin",
    },
    "taskmanager": {
        "name": "Task-Manager",
        "target": "taskmgr.exe",
        "kind": "exe",
        "source": "builtin",
    },
    "task-manager": {
        "name": "Task-Manager",
        "target": "taskmgr.exe",
        "kind": "exe",
        "source": "builtin",
    },
    "einstellungen": {
        "name": "Windows Einstellungen",
        "target": "ms-settings:",
        "kind": "uri",
        "source": "builtin",
    },
    "settings": {
        "name": "Windows Einstellungen",
        "target": "ms-settings:",
        "kind": "uri",
        "source": "builtin",
    },
    "systemsteuerung": {
        "name": "Systemsteuerung",
        "target": "control.exe",
        "kind": "exe",
        "source": "builtin",
    },
}


def normalize_app_key(text: str) -> str:
    """Normalizes an app name or user query for comparison."""
    cleaned = (text or "").strip().lower()
    cleaned = re.sub(r"\.(lnk|url|exe|bat|cmd|appref-ms)$", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*[-–]\s*(?:verknüpfung|shortcut)$", "", cleaned, flags=re.IGNORECASE)
    # Remove common German/English request prefixes & suffixes
    cleaned = re.sub(
        r"^(?:(?:hey\s+)?jarvis[\s,]+)?(bitte\s+)?(geh\w*\s+(?:auf|zu)|öffne|oeffne|starte|start|open|launch|run|mach|mache|zeig|zeige)\s+(mir\s+)?(mal\s+)?(bitte\s+)?(den|die|das|ein|eine|einen|meinen|meine|mein)?\s*",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"\s+(auf\s+(?:meinem|dem)\s+desktop|vom\s+desktop|mit\s+(?:einem\s+)?doppelklick|auf|öffnen|oeffnen|starten|bitte|jetzt|mal|app|application|programm|anwendung).*$",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"[_/\-]+", " ", cleaned)
    cleaned = re.sub(r"[?!.,:;\"']+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


class AppIndexer:
    """
    Deep Windows Application Scanner & Fuzzy Matcher.
    Scans Start Menu, Desktop, UWP/Store Apps, Registry App Paths, and Program Files,
    persisting the unified index to `apps_cache.json`.
    """

    def __init__(self, cache_path: Path = APPS_CACHE_FILE) -> None:
        self.cache_path = cache_path
        self._lock = threading.RLock()
        self._apps: dict[str, dict[str, str]] = {}
        self._scan_in_progress = False
        self._last_scan_ts: float = 0.0
        self._load_cache_from_disk()

    # ------------------------------------------------------------------
    # Cache Persistence
    # ------------------------------------------------------------------
    def _load_cache_from_disk(self) -> None:
        if not self.cache_path.exists():
            return
        try:
            raw = json.loads(self.cache_path.read_text(encoding="utf-8"))
            apps_data = raw.get("apps", {}) if isinstance(raw, dict) else {}
            if isinstance(apps_data, dict) and apps_data:
                with self._lock:
                    self._apps = apps_data
                    self._last_scan_ts = float(raw.get("timestamp", 0.0))
                logger.info("AppIndexer: %d Apps aus %s geladen.", len(self._apps), self.cache_path.name)
        except Exception as exc:
            logger.debug("AppIndexer Cache konnte nicht geladen werden: %s", exc)

    def _save_cache_to_disk(self) -> None:
        try:
            with self._lock:
                payload = {
                    "version": 2,
                    "timestamp": time.time(),
                    "platform": platform.system(),
                    "count": len(self._apps),
                    "apps": self._apps,
                }
            self.cache_path.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.debug("AppIndexer Cache konnte nicht gespeichert werden: %s", exc)

    # ------------------------------------------------------------------
    # Background & Synchronous Scanning (AUFGABE 1: 0.3s Schnell-Scan!)
    # ------------------------------------------------------------------
    def fast_startup_scan(self, force: bool = False) -> dict[str, dict[str, str]]:
        """
        AUFGABE 1 (Turbo-Start Stufe 3):
        Scannt beim ersten Start in <0.3s NUR Startmenü-`.lnk`, Desktop-Shortcuts & Taskleiste
        (kein langsamer Rekursiv-Scan durch C:\\Program Files!) und speichert sofort `apps_cache.json`.
        """
        with self._lock:
            if not force and self._apps:
                return dict(self._apps)

        start_t = time.perf_counter()
        discovered: dict[str, dict[str, str]] = {}

        def _register(name: str, target: str, kind: str, source: str, priority: int = 5) -> None:
            clean_name = (name or "").strip()
            clean_target = (target or "").strip()
            if not clean_name or not clean_target:
                return
            norm_key = normalize_app_key(clean_name)
            if not norm_key or len(norm_key) < 2:
                return
            existing = discovered.get(norm_key)
            if existing is not None and int(existing.get("priority", 0)) >= priority:
                return
            discovered[norm_key] = {
                "name": clean_name,
                "key": norm_key,
                "target": clean_target,
                "kind": kind,
                "source": source,
                "priority": str(priority),
            }

        try:
            for key, entry in DIRECT_WINDOWS_TARGETS.items():
                _register(
                    name=entry["name"],
                    target=entry["target"],
                    kind=entry["kind"],
                    source=entry["source"],
                    priority=4,
                )
                discovered[key] = {
                    "name": entry["name"],
                    "key": key,
                    "target": entry["target"],
                    "kind": entry["kind"],
                    "source": entry["source"],
                    "priority": "4",
                }

            if IS_WINDOWS:
                self._scan_start_menu(_register)
                self._scan_desktop_shortcuts(_register)
                self._scan_taskbar_shortcuts(_register)
            else:
                self._scan_linux_desktop_entries(_register)

            with self._lock:
                self._apps = discovered
                self._last_scan_ts = time.time()

            self._save_cache_to_disk()
            elapsed = (time.perf_counter() - start_t) * 1000.0
            logger.info(
                "AppIndexer: Schnell-Scan abgeschlossen — %d Startmenü-/Desktop-Apps in %.0f ms (%s).",
                len(discovered),
                elapsed,
                self.cache_path.name,
            )
        except Exception as exc:
            logger.warning("AppIndexer Schnell-Scan Warnung: %s", exc)

        return discovered

    def start_background_scan(
        self,
        force: bool = False,
        delay_sec: float = 0.0,
        low_priority: bool = True,
    ) -> threading.Thread:
        """
        Starts the deep system scan in a low-priority background daemon thread.
        Wenn noch kein Cache vorhanden ist, läuft sofort vorab der <0.3s Schnell-Scan.
        """
        if not self._apps:
            self.fast_startup_scan(force=False)

        def _bg_worker() -> None:
            if delay_sec > 0:
                time.sleep(delay_sec)
            if low_priority and IS_WINDOWS:
                try:
                    import ctypes

                    ctypes.windll.kernel32.SetThreadPriority(
                        ctypes.windll.kernel32.GetCurrentThread(),
                        -1,  # THREAD_PRIORITY_BELOW_NORMAL
                    )
                except Exception:
                    pass
            self.scan_all_apps(force=force)

        thread = threading.Thread(
            target=_bg_worker,
            name="jarvis-app-indexer",
            daemon=True,
        )
        thread.start()
        return thread

    def get_all_apps(self) -> dict[str, dict[str, str]]:
        with self._lock:
            return dict(self._apps)

    def scan_all_apps(self, force: bool = False) -> dict[str, dict[str, str]]:
        """
        Executes a comprehensive scan of all installed applications and saves `apps_cache.json`.
        """
        with self._lock:
            if self._scan_in_progress:
                return dict(self._apps)
            if not force and self._apps and (time.time() - self._last_scan_ts) < 300:
                return dict(self._apps)
            self._scan_in_progress = True

        start_t = time.perf_counter()
        discovered: dict[str, dict[str, str]] = {}

        def _register(name: str, target: str, kind: str, source: str, priority: int = 5) -> None:
            clean_name = (name or "").strip()
            clean_target = (target or "").strip()
            if not clean_name or not clean_target:
                return
            norm_key = normalize_app_key(clean_name)
            if not norm_key or len(norm_key) < 2:
                return
            existing = discovered.get(norm_key)
            if existing is not None and int(existing.get("priority", 0)) >= priority:
                return
            discovered[norm_key] = {
                "name": clean_name,
                "key": norm_key,
                "target": clean_target,
                "kind": kind,
                "source": source,
                "priority": str(priority),
            }

        try:
            # 0. Built-in Windows system tools
            for key, entry in DIRECT_WINDOWS_TARGETS.items():
                _register(
                    name=entry["name"],
                    target=entry["target"],
                    kind=entry["kind"],
                    source=entry["source"],
                    priority=4,
                )
                discovered[key] = {
                    "name": entry["name"],
                    "key": key,
                    "target": entry["target"],
                    "kind": entry["kind"],
                    "source": entry["source"],
                    "priority": "4",
                }

            if IS_WINDOWS:
                # 1. Start Menu shortcuts (.lnk / .appref-ms)
                self._scan_start_menu(_register)

                # 2. Desktop shortcuts & icons (.lnk / .url / .exe in User + Public Desktop)
                self._scan_desktop_shortcuts(_register)

                # 3. Taskbar pinned shortcuts (.lnk / .url)
                self._scan_taskbar_shortcuts(_register)

                # 4. Windows UWP / Store Apps via Get-StartApps | ConvertTo-Json
                self._scan_uwp_start_apps(_register)

                # 5. Windows Registry (HKLM & HKCU App Paths)
                self._scan_registry_app_paths(_register)

                # 6. Standard Program Folders (Program Files, Program Files (x86), %LOCALAPPDATA%\Programs)
                self._scan_program_directories(_register)
            else:
                # Linux / Raspberry Pi .desktop entries
                self._scan_linux_desktop_entries(_register)

            with self._lock:
                self._apps = discovered
                self._last_scan_ts = time.time()

            self._save_cache_to_disk()
            elapsed = (time.perf_counter() - start_t) * 1000.0
            logger.info(
                "AppIndexer: Tiefenscan abgeschlossen — %d Anwendungen in %.0f ms indexiert (%s).",
                len(discovered),
                elapsed,
                self.cache_path.name,
            )
        except Exception as exc:
            logger.warning("AppIndexer Scan-Warnung: %s", exc)
        finally:
            with self._lock:
                self._scan_in_progress = False

        return discovered

    def rescan_apps(self, deep: bool = True) -> dict[str, dict[str, str]]:
        """
        Erzwingt einen vollständigen Neu-Scan aller installierten Apps (z. B. direkt nach einer
        autonomen Software-Installation) und aktualisiert `apps_cache.json` sofort.
        """
        if deep:
            return self.scan_all_apps(force=True)
        return self.fast_startup_scan(force=True)
    # ------------------------------------------------------------------
    def _scan_start_menu(self, register_fn: Callable[[str, str, str, str, int], None]) -> None:
        """Scans %ProgramData% and %APPDATA% Start Menu Programs for .lnk shortcuts."""
        roots: list[Path] = []
        program_data = os.environ.get("ProgramData", r"C:\ProgramData")
        appdata = os.environ.get("APPDATA", "")
        if program_data:
            roots.append(Path(program_data) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
        if appdata:
            roots.append(Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs")

        for root in roots:
            if not root.exists():
                continue
            try:
                for path in root.rglob("*"):
                    if not path.is_file():
                        continue
                    suffix = path.suffix.lower()
                    if suffix not in (".lnk", ".appref-ms", ".url"):
                        continue
                    stem_lower = path.stem.lower()
                    if any(bad in stem_lower for bad in ("uninstall", "deinstall", "readme", "help", "handbuch", "website")):
                        continue
                    register_fn(path.stem, str(path), "shortcut", "start_menu", 9)
            except Exception as exc:
                logger.debug("Startmenü-Scan Fehler in %s: %s", root, exc)

    def _scan_desktop_shortcuts(self, register_fn: Callable[[str, str, str, str, int], None]) -> None:
        """Scans User Desktop and Public Desktop for .lnk, .url, and .exe items."""
        roots: list[Path] = []
        user_profile = os.environ.get("USERPROFILE", "")
        public_dir = os.environ.get("PUBLIC", r"C:\Users\Public")
        if user_profile:
            roots.append(Path(user_profile) / "Desktop")
            onedrive_desktop = Path(user_profile) / "OneDrive" / "Desktop"
            if onedrive_desktop.exists():
                roots.append(onedrive_desktop)
        if public_dir:
            roots.append(Path(public_dir) / "Desktop")

        for root in roots:
            if not root.exists():
                continue
            try:
                for path in root.iterdir():
                    if not path.is_file():
                        continue
                    suffix = path.suffix.lower()
                    if suffix in (".lnk", ".url", ".appref-ms"):
                        register_fn(path.stem, str(path), "shortcut", "desktop", 9)
                    elif suffix == ".exe":
                        register_fn(path.stem, str(path), "exe", "desktop", 9)
            except Exception as exc:
                logger.debug("Desktop-Scan Fehler in %s: %s", root, exc)

    def _scan_taskbar_shortcuts(self, register_fn: Callable[[str, str, str, str, int], None]) -> None:
        """Scans Windows Taskbar pinned shortcuts (.lnk / .url) so taskbar apps have top priority."""
        appdata = os.environ.get("APPDATA", "")
        if not appdata:
            return
        taskbar_dir = Path(appdata) / "Microsoft" / "Internet Explorer" / "Quick Launch" / "User Pinned" / "TaskBar"
        if not taskbar_dir.exists():
            return
        try:
            for path in taskbar_dir.iterdir():
                if not path.is_file():
                    continue
                suffix = path.suffix.lower()
                if suffix in (".lnk", ".url", ".appref-ms"):
                    register_fn(path.stem, str(path), "shortcut", "taskbar", 10)
        except Exception as exc:
            logger.debug("Taskbar-Scan Fehler in %s: %s", taskbar_dir, exc)

    def _scan_uwp_start_apps(self, register_fn: Callable[[str, str, str, str, int], None]) -> None:
        """Scans Windows UWP / Store and registered Start Apps via PowerShell Get-StartApps."""
        try:
            ps_cmd = (
                "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
                "Get-StartApps | Select-Object Name, AppID | ConvertTo-Json -Compress"
            )
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", ps_cmd],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                timeout=10,
                creationflags=CREATE_NO_WINDOW,
            )
            stdout = (proc.stdout or "").strip()
            if not stdout:
                return
            items = json.loads(stdout)
            if isinstance(items, dict):
                items = [items]
            if not isinstance(items, list):
                return
            for item in items:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("Name") or "").strip()
                app_id = str(item.get("AppID") or "").strip()
                if not name or not app_id:
                    continue
                # Launchable via explorer.exe shell:AppsFolder\<AppID>
                target = f"shell:AppsFolder\\{app_id}"
                register_fn(name, target, "uwp", "get_startapps", 10)
        except Exception as exc:
            logger.debug("UWP Get-StartApps Scan Fehler: %s", exc)

    def _scan_registry_app_paths(self, register_fn: Callable[[str, str, str, str, int], None]) -> None:
        """Scans HKLM & HKCU ...\\Windows\\CurrentVersion\\App Paths in Windows Registry."""
        if not IS_WINDOWS:
            return
        try:
            import winreg  # type: ignore

            reg_roots = [
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"),
                (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths"),
            ]
            for hive, subkey_path in reg_roots:
                try:
                    with winreg.OpenKey(hive, subkey_path) as base_key:
                        idx = 0
                        while True:
                            try:
                                sub_name = winreg.EnumKey(base_key, idx)
                                idx += 1
                            except OSError:
                                break
                            try:
                                with winreg.OpenKey(base_key, sub_name) as app_key:
                                    val, _ = winreg.QueryValueEx(app_key, "")
                                    exe_path = str(val or "").strip().strip('"')
                                    if exe_path and Path(exe_path).exists():
                                        display_name = Path(sub_name).stem
                                        register_fn(display_name, exe_path, "exe", "registry_app_paths", 7)
                            except OSError:
                                continue
                except OSError:
                    continue
        except Exception as exc:
            logger.debug("Registry App Paths Scan Fehler: %s", exc)

    def _scan_program_directories(self, register_fn: Callable[[str, str, str, str, int], None]) -> None:
        """
        Scans C:\\Program Files, C:\\Program Files (x86), and %LOCALAPPDATA%\\Programs
        up to depth 2 for main application executables.
        """
        roots: list[Path] = []
        for env_key, fallback in (
            ("ProgramFiles", r"C:\Program Files"),
            ("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        ):
            val = os.environ.get(env_key, fallback)
            if val:
                roots.append(Path(val))

        local_appdata = os.environ.get("LOCALAPPDATA", "")
        if local_appdata:
            roots.append(Path(local_appdata) / "Programs")
            # Also check standard Discord / WhatsApp / Squirrel style folders in LocalAppData
            for extra_folder in ("Discord", "Spotify", "OrcaSlicer", "BambuStudio", "Steam"):
                candidate = Path(local_appdata) / extra_folder
                if candidate.exists():
                    roots.append(candidate)

        for root in roots:
            if not root.exists():
                continue
            try:
                for app_dir in root.iterdir():
                    if not app_dir.is_dir():
                        continue
                    folder_name = app_dir.name
                    self._scan_single_app_folder(app_dir, folder_name, register_fn, max_depth=2)
            except PermissionError:
                continue
            except Exception as exc:
                logger.debug("Program-Folder Scan Fehler in %s: %s", root, exc)

    def _scan_single_app_folder(
        self,
        folder: Path,
        vendor_or_app_name: str,
        register_fn: Callable[[str, str, str, str, int], None],
        max_depth: int = 2,
        current_depth: int = 0,
    ) -> None:
        if current_depth > max_depth:
            return
        try:
            for entry in folder.iterdir():
                if entry.is_file() and entry.suffix.lower() == ".exe":
                    stem_lower = entry.stem.lower()
                    if any(bad in stem_lower for bad in IGNORED_EXE_PATTERNS):
                        continue
                    # Register both by exe stem and by parent folder name if closely related
                    register_fn(entry.stem, str(entry), "exe", "program_files", 6)
                    if current_depth <= 1 and len(vendor_or_app_name) >= 3:
                        register_fn(vendor_or_app_name, str(entry), "exe", "program_files", 5)
                elif entry.is_dir() and current_depth < max_depth:
                    sub_lower = entry.name.lower()
                    if sub_lower in ("locales", "resources", "lib", "include", "doc", "docs", "licenses", "plugins", "shaders"):
                        continue
                    self._scan_single_app_folder(
                        entry,
                        entry.name if current_depth == 0 else vendor_or_app_name,
                        register_fn,
                        max_depth=max_depth,
                        current_depth=current_depth + 1,
                    )
        except (PermissionError, OSError):
            return

    def _scan_linux_desktop_entries(self, register_fn: Callable[[str, str, str, str, int], None]) -> None:
        """Fallback scanner for Raspberry Pi / Linux .desktop entries."""
        desktop_dirs = [
            Path("/usr/share/applications"),
            Path("/usr/local/share/applications"),
            Path.home() / ".local" / "share" / "applications",
        ]
        for d_dir in desktop_dirs:
            if not d_dir.exists():
                continue
            try:
                for path in d_dir.glob("*.desktop"):
                    try:
                        content = path.read_text(encoding="utf-8", errors="ignore")
                    except Exception:
                        continue
                    name_m = re.search(r"^Name=(.+)$", content, flags=re.MULTILINE)
                    exec_m = re.search(r"^Exec=(.+)$", content, flags=re.MULTILINE)
                    if name_m and exec_m:
                        app_name = name_m.group(1).strip()
                        exec_cmd = re.sub(r"\s+%[a-zA-Z]", "", exec_m.group(1).strip())
                        register_fn(app_name, exec_cmd, "linux_desktop", "applications", 8)
            except Exception:
                continue

    # ------------------------------------------------------------------
    # Fuzzy Search & App Matching
    # ------------------------------------------------------------------
    def find_app(self, query: str) -> Optional[dict[str, str]]:
        """
        Matches a natural user query (e.g. 'Öffne Xbox', 'Starte Steam',
        'Mach den Rechner auf', 'Öffne OrcaSlicer') against `apps_cache.json`.
        Uses:
          1. Exact normalized match
          2. Synonym expansion (`BUILTIN_APP_SYNONYMS`)
          3. Word-token & substring match
          4. Fuzzy matching via `difflib.SequenceMatcher` / `get_close_matches`
        """
        norm_query = normalize_app_key(query)
        raw_norm = re.sub(r"[_/\-?!.,:;\"']+", " ", (query or "").strip().lower())
        raw_norm = re.sub(r"\s+", " ", raw_norm).strip()
        if not norm_query and not raw_norm:
            return None
        prefer_desktop = any(tok in raw_norm for tok in ("desktop", "arbeitsfläche", "doppelklick"))

        # Ensure cache is populated
        with self._lock:
            if not self._apps:
                pass
        if not self._apps:
            self.scan_all_apps()

        with self._lock:
            apps_snapshot = dict(self._apps)

        # Candidate search terms (original query + extracted clauses + known synonyms)
        candidate_terms: list[str] = []
        for cand in (norm_query, raw_norm):
            if cand and cand not in candidate_terms:
                candidate_terms.append(cand)
        if norm_query in BUILTIN_APP_SYNONYMS:
            for syn in BUILTIN_APP_SYNONYMS[norm_query]:
                norm_syn = normalize_app_key(syn)
                if norm_syn and norm_syn not in candidate_terms:
                    candidate_terms.append(norm_syn)

        # 1. Exact key match in index
        for term in candidate_terms:
            if term in apps_snapshot:
                return apps_snapshot[term]

        # 2. Direct built-in Windows target check
        for term in candidate_terms:
            if term in DIRECT_WINDOWS_TARGETS:
                return DIRECT_WINDOWS_TARGETS[term]

        # 3. Substring / Phrase-in-Sentence / Token match (prefer Desktop when requested + higher priority)
        best_substring_entry: Optional[dict[str, str]] = None
        best_substring_score: float = 0.0

        for key, entry in apps_snapshot.items():
            if len(key) < 3:
                continue
            entry_name_norm = normalize_app_key(entry.get("name", ""))
            src_low = str(entry.get("source") or "").lower()
            desktop_bonus = 0.25 if (prefer_desktop and src_low == "desktop") else 0.0
            for term in candidate_terms:
                if len(term) < 2:
                    continue
                if term == key or term == entry_name_norm:
                    return entry

                if term in key or key in term:
                    # Wenn der App-Schlüssel komplett als Wort/Phrase im längeren Sprachbefehl vorkommt -> hoher Treffer!
                    in_sentence = len(key) >= 4 and bool(re.search(rf"\b{re.escape(key)}\b", term))
                    len_ratio = 0.85 if in_sentence else (min(len(term), len(key)) / max(len(term), len(key), 1))
                    word_boundary_bonus = 0.20 if (in_sentence or re.search(rf"\b{re.escape(term)}\b", key)) else 0.0
                    prio_bonus = int(entry.get("priority", "5")) * 0.02
                    score = len_ratio + word_boundary_bonus + prio_bonus + desktop_bonus
                    if score > best_substring_score:
                        best_substring_score = score
                        best_substring_entry = entry

        if best_substring_entry is not None and best_substring_score >= 0.48:
            return best_substring_entry

        # 4. Fuzzy match via difflib
        all_keys = list(apps_snapshot.keys())
        best_fuzzy_entry: Optional[dict[str, str]] = None
        best_fuzzy_ratio: float = 0.0

        for term in candidate_terms:
            close_keys = difflib.get_close_matches(term, all_keys, n=5, cutoff=0.58)
            for c_key in close_keys:
                entry = apps_snapshot[c_key]
                ratio = difflib.SequenceMatcher(None, term, c_key).ratio()
                prio_bonus = int(entry.get("priority", "5")) * 0.01
                total = ratio + prio_bonus
                if total > best_fuzzy_ratio:
                    best_fuzzy_ratio = total
                    best_fuzzy_entry = entry

        if best_fuzzy_entry is not None and best_fuzzy_ratio >= 0.60:
            return best_fuzzy_entry

        return None

    def is_app_installed(
        self,
        app_name: str,
        aliases: Optional[list[str]] = None,
    ) -> Optional[dict[str, str]]:
        """
        Prüft streng (exakte/starke Übereinstimmung ohne schwache False-Positives),
        ob eine gewünschte Anwendung tatsächlich bereits auf dem System installiert ist.
        """
        if not self._apps:
            self.fast_startup_scan(force=False)

        candidates: list[str] = []
        for raw_item in [app_name, *(aliases or [])]:
            norm = normalize_app_key(raw_item)
            if norm and norm not in candidates:
                candidates.append(norm)
            if norm in BUILTIN_APP_SYNONYMS:
                for syn in BUILTIN_APP_SYNONYMS[norm]:
                    sn = normalize_app_key(syn)
                    if sn and sn not in candidates:
                        candidates.append(sn)

        if not candidates:
            return None

        with self._lock:
            apps_snapshot = dict(self._apps)

        for cand in candidates:
            if cand in apps_snapshot:
                return apps_snapshot[cand]
            if cand in DIRECT_WINDOWS_TARGETS:
                return DIRECT_WINDOWS_TARGETS[cand]
            for key, entry in apps_snapshot.items():
                entry_name_norm = normalize_app_key(entry.get("name", ""))
                if cand == key or cand == entry_name_norm:
                    return entry
                if len(cand) >= 4 and (
                    re.search(rf"\b{re.escape(cand)}\b", key)
                    or re.search(rf"\b{re.escape(cand)}\b", entry_name_norm)
                ):
                    return entry
        return None

    # ------------------------------------------------------------------
    # Native App Launch + Visual Fallback
    # ------------------------------------------------------------------
    def launch_app(
        self,
        query: str,
        visual_fallback_fn: Optional[Callable[[str], str]] = None,
    ) -> dict[str, Any]:
        """
        Finds and launches an application natively.
        If not found in `apps_cache.json` (or if launching fails), executes the
        visual Windows Search fallback (`Win` -> type name -> 1-FPS screenshot check -> `Enter`).
        """
        cleaned_query = normalize_app_key(query) or query.strip()
        matched = self.find_app(cleaned_query)

        if matched is not None:
            app_name = matched.get("name", cleaned_query)
            target = matched.get("target", "")
            kind = matched.get("kind", "exe")
            try:
                from multi_monitor import snapshot_open_windows, verify_launched_app_across_monitors

                before_snap = snapshot_open_windows() if IS_WINDOWS else None
                self._launch_target(target, kind)
                logger.info("AppIndexer hat '%s' gestartet (%s -> %s).", app_name, kind, target)
                ver_msg = f"Ich habe {app_name} geöffnet."
                if IS_WINDOWS:
                    ver = verify_launched_app_across_monitors(app_name, before_snap, cursor_timeout=1.5, all_timeout=1.2)
                    if ver.get("found") and ver.get("message"):
                        ver_msg = str(ver["message"])
                return {
                    "ok": True,
                    "method": "index",
                    "app_name": app_name,
                    "target": target,
                    "kind": kind,
                    "message": ver_msg,
                }
            except Exception as exc:
                logger.warning("Direktstart von '%s' (%s) fehlgeschlagen: %s", app_name, target, exc)

        # Visual Windows Search Fallback when not in index or direct launch failed
        if visual_fallback_fn is not None:
            try:
                msg = visual_fallback_fn(cleaned_query)
                return {
                    "ok": True,
                    "method": "visual_windows_search",
                    "app_name": cleaned_query,
                    "target": "windows_search",
                    "kind": "visual_fallback",
                    "message": msg,
                }
            except Exception as exc:
                logger.warning("Visueller Windows-Suche-Fallback fehlgeschlagen: %s", exc)

        return {
            "ok": False,
            "method": "none",
            "app_name": cleaned_query,
            "target": "",
            "kind": "none",
            "message": f"Die Anwendung '{cleaned_query}' konnte nicht gefunden werden.",
        }

    def get_desktop_and_taskbar_apps(self) -> list[dict[str, str]]:
        """Gibt alle erkannten Desktop-Icons und Taskleisten-Verknüpfungen zurück."""
        with self._lock:
            if not self._apps:
                pass
        if not self._apps:
            self.scan_all_apps()
        with self._lock:
            seen: set[str] = set()
            items: list[dict[str, str]] = []
            for entry in self._apps.values():
                if entry.get("source") in ("desktop", "taskbar"):
                    tgt = entry.get("target", "")
                    if tgt and tgt not in seen:
                        seen.add(tgt)
                        items.append(dict(entry))
            return items

    def open_app_by_query(
        self,
        query: str,
        visual_fallback_fn: Optional[Callable[[str], str]] = None,
    ) -> tuple[bool, str]:
        """Öffnet eine App per Query und liefert (ok, message) zurück."""
        res = self.launch_app(query, visual_fallback_fn=visual_fallback_fn)
        return bool(res.get("ok")), str(res.get("message") or "")

    def _launch_target(self, target: str, kind: str) -> None:
        """Executes the matched target cleanly without spawning a black CMD window."""
        if IS_WINDOWS:
            if kind == "uwp" or target.startswith("shell:AppsFolder\\"):
                subprocess.Popen(
                    ["explorer.exe", target],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=CREATE_NO_WINDOW,
                )
                return

            if kind == "uri" or (":" in target and not Path(target).drive):
                os.startfile(target)  # type: ignore[attr-defined]
                return

            target_path = Path(target)
            if kind == "shortcut" or target_path.suffix.lower() in (".lnk", ".url", ".appref-ms"):
                os.startfile(str(target_path))  # type: ignore[attr-defined]
                return

            if target_path.exists():
                os.startfile(str(target_path))  # type: ignore[attr-defined]
                return

            # System executable in PATH (e.g. calc.exe, notepad.exe, mspaint.exe)
            try:
                os.startfile(target)  # type: ignore[attr-defined]
            except Exception:
                subprocess.Popen(
                    [target],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=CREATE_NO_WINDOW,
                )
        else:
            subprocess.Popen(
                target.split(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )


# Singleton instance used across JARVIS
APP_INDEXER = AppIndexer()


def rescan_apps(deep: bool = True) -> dict[str, dict[str, str]]:
    """Modulweiter Shortcut auf `APP_INDEXER.rescan_apps(deep=deep)`."""
    return APP_INDEXER.rescan_apps(deep=deep)
