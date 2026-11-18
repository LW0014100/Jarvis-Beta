"""Verwaltung der .env-Konfiguration inklusive Live-Reload für das Command Center."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import threading
from typing import Callable, Mapping, Optional

from dotenv import dotenv_values, load_dotenv

def is_frozen() -> bool:
    """Prüft, ob die Anwendung als eigenständige Binärdatei (z. B. PyInstaller .exe) läuft."""
    return getattr(sys, "frozen", False)


def get_base_dir() -> Path:
    """Liefert das Basisverzeichnis für statische Anwendungsdateien (z. B. web_ui)."""
    if is_frozen():
        if hasattr(sys, "_MEIPASS"):
            return Path(sys._MEIPASS)
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


def get_user_data_dir() -> Path:
    """Liefert das beschreibbare Nutzerdatenverzeichnis (%LOCALAPPDATA%\\JarvisCommandCenter im Release-Modus)."""
    if is_frozen():
        if platform.system() == "Windows":
            local_appdata = os.getenv("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
            data_dir = Path(local_appdata) / "JarvisCommandCenter"
        else:
            data_dir = Path.home() / ".local" / "share" / "JarvisCommandCenter"
    else:
        # Entwicklungsmodus: Workspace-Verzeichnis beibehalten
        data_dir = Path(__file__).resolve().parent
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def get_env_path() -> Path:
    return get_user_data_dir() / ".env"


# .env sofort beim Modul-Import aus dem portablen Nutzerdaten-Verzeichnis laden
PROJECT_ENV_PATH = get_env_path()
if PROJECT_ENV_PATH.exists():
    load_dotenv(dotenv_path=PROJECT_ENV_PATH, override=True)


def clean_api_key(raw: Optional[str]) -> str:
    """Bereinigt einen API-Key von Leerzeichen, Anführungszeichen und Platzhaltern."""
    value = (raw or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("\"", "'"):
        value = value[1:-1].strip()
    else:
        value = value.strip("\"' \t\r\n")
    if not value or (value.startswith("dein_") and value.endswith("_hier")):
        return ""
    return value


DEPRECATED_GROQ_MODELS = {
    "llama3-70b-8192",
    "llama3-8b-8192",
    "mixtral-8x7b-32768",
    "gemma-7b-it",
    "gemma2-9b-it",
    "llama-3.2-11b-text-preview",
    "llama-3.2-90b-text-preview",
}


def sanitize_groq_model(raw: Optional[str]) -> str:
    """Stellt sicher, dass kein abgeschaltetes Groq-Modell (400/404) übergeben wird."""
    model = clean_api_key(raw)
    if not model or model.lower() in DEPRECATED_GROQ_MODELS:
        return "llama-3.3-70b-versatile"
    return model


def sanitize_gemini_model(raw: Optional[str]) -> str:
    """Bereinigt den Gemini-Modellnamen (Standard: gemini-2.5-flash)."""
    model = clean_api_key(raw)
    if model.startswith("models/"):
        model = model[len("models/"):]
    if not model or model.lower() in ("gemini-pro", "gemini-1.5-flash", "gemini-1.5-pro"):
        return "gemini-2.5-flash"
    return model


def get_env_example_path() -> Path:
    cand = get_base_dir() / ".env.example"
    if cand.is_file():
        return cand
    cand2 = Path(__file__).resolve().parent / ".env.example"
    if cand2.is_file():
        return cand2
    return get_user_data_dir() / ".env.example"


def get_web_ui_dir() -> Path:
    """Ermittelt das web_ui-Verzeichnis sowohl im Source-Code als auch im PyInstaller sys._MEIPASS Bundle."""
    candidate = get_base_dir() / "web_ui"
    if candidate.is_dir():
        return candidate
    candidate2 = Path(__file__).resolve().parent / "web_ui"
    if candidate2.is_dir():
        return candidate2
    return Path("web_ui").resolve()


def get_subprocess_hide_flags() -> dict[str, object]:
    """Liefert kwargs für subprocess.Popen / subprocess.run unter Windows, damit kein CMD-Fenster aufblitzt."""
    kwargs: dict[str, object] = {}
    if platform.system() == "Windows":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0  # SW_HIDE
        kwargs["startupinfo"] = startupinfo
    return kwargs


def process_installer_choice(env_path: Optional[Path] = None) -> None:
    """Liest die Auswahl aus installer_choice.json (falls vom Inno Setup angelegt) und setzt Initialwerte."""
    p_env = env_path or get_env_path()
    choice_file = get_user_data_dir() / "installer_choice.json"
    if not choice_file.is_file():
        return
    try:
        raw = choice_file.read_text(encoding="utf-8")
        data = json.loads(raw)
        updates: dict[str, str] = {}

        enable_keyless = bool(data.get("enable_free_keyless_ai", True))
        current_prio = get_model_priority()

        if enable_keyless:
            if "pollinations" not in current_prio:
                current_prio.append("pollinations")
            updates["MODEL_PRIORITY"] = ",".join(current_prio)
            updates["KEYLESS_ONLINE_ENABLED"] = "true"
        else:
            prio_filtered = [p for p in current_prio if p != "pollinations"]
            updates["MODEL_PRIORITY"] = ",".join(prio_filtered)
            updates["KEYLESS_ONLINE_ENABLED"] = "false"

        selected_models = data.get("selected_models", [])
        if isinstance(selected_models, list) and selected_models:
            text_models = [
                m for m in selected_models
                if not any(token in m.lower() for token in ("moondream", "llava", "vision", "bakllava"))
            ]
            if text_models:
                updates["OLLAMA_TEXT_MODEL"] = text_models[0]

            vision_models = [
                m for m in selected_models
                if any(token in m.lower() for token in ("moondream", "llava", "vision", "bakllava"))
            ]
            if vision_models:
                updates["OLLAMA_VISION_MODEL"] = vision_models[0]

        if updates:
            save_settings(updates, env_path=p_env)

        processed_file = get_user_data_dir() / "installer_choice.processed"
        try:
            choice_file.replace(processed_file)
        except Exception:
            pass
        print(f"[Installer] Einstellungen aus installer_choice.json erfolgreich geladen: {updates}")
    except Exception as exc:
        print(f"[Installer] Fehler beim Verarbeiten von installer_choice.json: {exc}")


def default_ollama_text_model() -> str:
    """Wählt das optimale lokale Standard-Modell passend zur Hardware-Plattform."""
    if platform.system() == "Windows":
        return "qwen2.5:7b"
    return "gemma3:4b"


ENV_PATH = get_env_path()
ENV_EXAMPLE_PATH = get_env_example_path()
LOCAL_ARCHIVE_FALLBACK = get_user_data_dir() / "projekt_logs"

OFFLINE_MODE_CMD_PATTERN = re.compile(
    r"\b(?:"
    r"geh(?:e)?\s+(?:bitte\s+)?(?:in\s+den\s+)?offline(?:[\s-]*modus)?|"
    r"offline[\s-]*modus(?:\s+aktivieren)?|"
    r"schalte?\s+(?:bitte\s+)?auf\s+(?:offline|lokal)|"
    r"(?:bitte\s+)?nur\s+lokal(?:\s+arbeiten)?|"
    r"internet\s+trennen|"
    r"trenn(?:e)?\s+(?:bitte\s+)?das\s+internet"
    r")\b",
    flags=re.IGNORECASE,
)

ONLINE_MODE_CMD_PATTERN = re.compile(
    r"\b(?:"
    r"geh(?:e)?\s+(?:bitte\s+)?(?:in\s+den\s+)?online(?:[\s-]*modus)?|"
    r"online[\s-]*modus(?:\s+aktivieren)?|"
    r"schalte?\s+(?:bitte\s+)?auf\s+online|"
    r"verbinde\s+dich\s+(?:bitte\s+)?wieder(?:\s+mit\s+dem\s+internet)?|"
    r"internet\s+(?:wieder\s+)?aktivieren"
    r")\b",
    flags=re.IGNORECASE,
)

_ONLINE_LISTENERS_LOCK = threading.Lock()
_ONLINE_LISTENERS: list[Callable[[bool], None]] = []


def detect_online_mode_command(text: str) -> Optional[bool]:
    """Erkennt Modus-Wechsel-Sprachbefehle ohne LLM-Aufruf (< 1 ms): False=Offline, True=Online."""
    cleaned = (text or "").strip()
    if not cleaned:
        return None
    if OFFLINE_MODE_CMD_PATTERN.search(cleaned):
        return False
    if ONLINE_MODE_CMD_PATTERN.search(cleaned):
        return True
    return None


def register_online_mode_listener(callback: Callable[[bool], None]) -> None:
    """Registriert einen Modul-Callback für sofortige Updates durch set_online_mode()."""
    with _ONLINE_LISTENERS_LOCK:
        if callback not in _ONLINE_LISTENERS:
            _ONLINE_LISTENERS.append(callback)


def unregister_online_mode_listener(callback: Callable[[bool], None]) -> None:
    """Entfernt einen zuvor registrierten Online-Modus-Callback."""
    with _ONLINE_LISTENERS_LOCK:
        if callback in _ONLINE_LISTENERS:
            _ONLINE_LISTENERS.remove(callback)


def get_is_online(default: bool = True) -> bool:
    """Liest den globalen Online-/Offline-Status (IS_ONLINE) aus Environment / .env."""
    raw = os.getenv("IS_ONLINE", "").strip().lower()
    if not raw and ENV_PATH.exists():
        try:
            raw = str(dotenv_values(ENV_PATH).get("IS_ONLINE") or "").strip().lower()
        except Exception:
            raw = ""
    if not raw:
        return default
    return raw not in ("0", "false", "no", "off", "offline")


def set_is_online(is_online: bool, env_path: Path = ENV_PATH) -> bool:
    """Setzt IS_ONLINE global im Prozess und speichert den Zustand dauerhaft in .env."""
    val_str = "true" if is_online else "false"
    os.environ["IS_ONLINE"] = val_str
    try:
        ensure_env_file(env_path)
        lines = env_path.read_text(encoding="utf-8").splitlines()
        out_lines: list[str] = []
        found = False
        for line in lines:
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in line and line.split("=", 1)[0].strip() == "IS_ONLINE":
                out_lines.append(f"IS_ONLINE={val_str}")
                found = True
            else:
                out_lines.append(line)
        if not found:
            out_lines.append(f"IS_ONLINE={val_str}")
        tmp_path = env_path.with_suffix(".tmp")
        tmp_path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
        tmp_path.replace(env_path)
    except Exception:
        pass
    return bool(is_online)


def set_online_mode(enabled: bool, env_path: Path = ENV_PATH) -> bool:
    """Zentraler Umschalter: persistiert IS_ONLINE und aktualisiert alle registrierten Module sofort."""
    target = bool(enabled)
    set_is_online(target, env_path=env_path)
    with _ONLINE_LISTENERS_LOCK:
        listeners = list(_ONLINE_LISTENERS)
    for callback in listeners:
        try:
            callback(target)
        except Exception:
            pass
    return target


# ------------------------------------------------------------------
# AUFGABE 1: 2-Stufen-Kaufberechtigung (`EXECUTION_MODE`)
#   - "SEMI_AUTO" (MODUS A - Standard): Halb-Autonom (Vorbereiten -> Ich drücke selbst auf Kaufen)
#   - "FULL_AUTO" (MODUS B): Voll-Zugriff (100% Autonomes Kaufen & Verkaufen)
# ------------------------------------------------------------------
def normalize_execution_mode(raw: Optional[str]) -> str:
    """Normalisiert den Ausführungsmodus auf 'SEMI_AUTO' (Modus A) oder 'FULL_AUTO' (Modus B)."""
    val = (raw or "").strip().upper()
    if val in ("FULL_AUTO", "MODUS_B", "MODE_B", "MODE_2", "MODUS_2", "VOLL_ZUGRIFF", "FULL", "2"):
        return "FULL_AUTO"
    return "SEMI_AUTO"


def get_execution_mode() -> str:
    """Liefert den aktiven Kauf- & Zugriffs-Modus ('SEMI_AUTO' = Modus A oder 'FULL_AUTO' = Modus B)."""
    raw = os.getenv("EXECUTION_MODE", "").strip()
    if not raw and ENV_PATH.exists():
        try:
            raw = str(dotenv_values(ENV_PATH).get("EXECUTION_MODE") or "").strip()
        except Exception:
            raw = ""
    return normalize_execution_mode(raw)


DEFAULT_MAX_AGENT_STEPS = 200
MIN_MAX_AGENT_STEPS = 50
MAX_MAX_AGENT_STEPS = 500
MAX_AGENT_STEPS = DEFAULT_MAX_AGENT_STEPS

# Adaptive Desktop-Vision Takt-Steuerung & On-Demand Webcam
PASSIVE_SCREEN_INTERVAL = 30.0
ACTIVE_AGENT_INTERVAL = 1.2
WEBCAM_EXPOSURE_WARMUP_SECONDS = 0.4
VISION_STATUS_PASSIVE = "DESKTOP-VISION: 30s PULS // WEBCAM: STANDBY (ON-DEMAND)"
VISION_STATUS_ACTIVE = "DESKTOP-VISION: 1.2s LIVE-TRACKING // AGENT AKTIV"
VISION_STATUS_WEBCAM = "WEBCAM: SNAPSHOT AKTIV"


def normalize_max_agent_steps(raw: object) -> int:
    """Normalisiert das maximale Schritt-Limit auf den Bereich 50 bis 500 (Standard: 200)."""
    try:
        val = int(str(raw or "").strip())
    except Exception:
        val = DEFAULT_MAX_AGENT_STEPS
    return max(MIN_MAX_AGENT_STEPS, min(MAX_MAX_AGENT_STEPS, val))


def get_max_agent_steps() -> int:
    """Liefert das konfigurierte Schritt-Limit (50–500, Standard: 200)."""
    raw = os.getenv("MAX_AGENT_STEPS", "").strip()
    if not raw and ENV_PATH.exists():
        try:
            raw = str(dotenv_values(ENV_PATH).get("MAX_AGENT_STEPS") or "").strip()
        except Exception:
            raw = ""
    if not raw:
        cfg_p = get_config_json_path()
        if cfg_p.is_file():
            try:
                cfg_data = json.loads(cfg_p.read_text(encoding="utf-8"))
                if isinstance(cfg_data, dict) and cfg_data.get("MAX_AGENT_STEPS") is not None:
                    raw = str(cfg_data.get("MAX_AGENT_STEPS"))
            except Exception:
                pass
    return normalize_max_agent_steps(raw)


def set_max_agent_steps(steps: int, env_path: Path = ENV_PATH) -> int:
    """Speichert MAX_AGENT_STEPS (50–500) dauerhaft in os.environ, .env und config.json."""
    norm = normalize_max_agent_steps(steps)
    os.environ["MAX_AGENT_STEPS"] = str(norm)
    try:
        save_settings({"MAX_AGENT_STEPS": str(norm)}, env_path=env_path)
    except Exception:
        pass
    return norm


def get_config_json_path() -> Path:
    """Liefert den portablen Pfad zu config.json (%LOCALAPPDATA%\\JarvisCommandCenter\\config.json im Release)."""
    return get_user_data_dir() / "config.json"


_CONFIG_JSON_KEYS = (
    "MODEL_PRIORITY",
    "ACTIVE_PROVIDER",
    "EXECUTION_MODE",
    "MAX_AGENT_STEPS",
    "AUTOSTART",
    "OLLAMA_TEXT_MODEL",
    "OLLAMA_VISION_MODEL",
    "WAKE_WORD",
    "WHISPER_MODEL",
    "PIPER_MODEL_PATH",
    "CAMERA_INDEX",
    "WATCHLIST",
    "CITY",
    "NAS_MEMORY_PATH",
    "ARCHIVE_PATH",
    "N8N_BASE_URL",
)


def sync_config_json(values: Optional[Mapping[str, str]] = None) -> Path:
    """Hält config.json (ohne geheime API-Keys) mit den aktiven Einstellungen synchron."""
    cfg_path = get_config_json_path()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[str, object] = {}
    if cfg_path.is_file():
        try:
            loaded = json.loads(cfg_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = loaded
        except Exception:
            existing = {}

    for k in _CONFIG_JSON_KEYS:
        if values and k in values:
            val = str(values[k])
        else:
            val = str(os.getenv(k) or existing.get(k) or DEFAULT_SETTINGS.get(k, ""))
        if k == "MAX_AGENT_STEPS":
            existing[k] = normalize_max_agent_steps(val)
        elif k == "EXECUTION_MODE":
            existing[k] = normalize_execution_mode(val)
        else:
            existing[k] = val
    existing["IS_ONLINE"] = get_is_online()

    tmp_path = cfg_path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(cfg_path)
    return cfg_path


def set_execution_mode(mode: str, env_path: Path = ENV_PATH) -> str:
    """Speichert EXECUTION_MODE ('SEMI_AUTO' oder 'FULL_AUTO') in os.environ und .env."""
    norm = normalize_execution_mode(mode)
    os.environ["EXECUTION_MODE"] = norm
    try:
        save_settings({"EXECUTION_MODE": norm}, env_path=env_path)
    except Exception:
        pass
    return norm


MODEL_PROVIDERS = ("gemini", "groq", "openrouter", "deepseek", "pollinations")
DEFAULT_MODEL_PRIORITY = "gemini,groq,openrouter,deepseek,pollinations"


def get_model_priority() -> list[str]:
    """Liefert die konfigurierte Reihenfolge der Online-KI-Anbieter (#1 bis #5)."""
    raw = os.getenv("MODEL_PRIORITY", "").strip()
    if not raw and ENV_PATH.exists():
        try:
            raw = str(dotenv_values(ENV_PATH).get("MODEL_PRIORITY") or "").strip()
        except Exception:
            raw = ""
    if not raw:
        raw = DEFAULT_MODEL_PRIORITY
    parts = [p.strip().lower() for p in raw.split(",") if p.strip().lower() in MODEL_PROVIDERS]
    for p in MODEL_PROVIDERS:
        if p not in parts:
            parts.append(p)
    return parts


def set_model_priority(priority: list[str] | str, env_path: Path = ENV_PATH) -> list[str]:
    """Speichert die Prioritätsreihenfolge in os.environ und in .env ab."""
    if isinstance(priority, str):
        priority = [p.strip().lower() for p in priority.split(",") if p.strip()]
    cleaned: list[str] = []
    for p in priority:
        low = str(p).strip().lower()
        if low in MODEL_PROVIDERS and low not in cleaned:
            cleaned.append(low)
    for p in MODEL_PROVIDERS:
        if p not in cleaned:
            cleaned.append(p)
    val = ",".join(cleaned)
    os.environ["MODEL_PRIORITY"] = val
    try:
        save_settings({"MODEL_PRIORITY": val}, env_path=env_path)
    except Exception:
        pass
    return cleaned


def get_active_provider() -> str:
    """Liefert den aktiven Provider ('auto', 'gemini', 'groq', 'openrouter', 'deepseek', 'pollinations', 'ollama')."""
    raw = os.getenv("ACTIVE_PROVIDER", "").strip().lower()
    if not raw and ENV_PATH.exists():
        try:
            raw = str(dotenv_values(ENV_PATH).get("ACTIVE_PROVIDER") or "").strip().lower()
        except Exception:
            raw = ""
    valid = ("auto", "gemini", "groq", "openrouter", "deepseek", "pollinations", "ollama")
    return raw if raw in valid else "auto"


def set_active_provider(provider: str, env_path: Path = ENV_PATH) -> str:
    """Setzt den aktiven Provider und schaltet ggf. Online-/Offline-Modus synchron um."""
    valid = ("auto", "gemini", "groq", "openrouter", "deepseek", "pollinations", "ollama")
    target = provider.strip().lower() if provider.strip().lower() in valid else "auto"
    os.environ["ACTIVE_PROVIDER"] = target
    try:
        save_settings({"ACTIVE_PROVIDER": target}, env_path=env_path)
    except Exception:
        pass
    if target == "ollama":
        set_online_mode(False, env_path=env_path)
    elif target != "auto":
        set_online_mode(True, env_path=env_path)
    return target


def get_autostart_status() -> bool:
    """Prüft plattformübergreifend (Linux/Pi 5 & Windows), ob der Autostart aktiv ist."""
    if sys.platform.startswith("linux"):
        desktop_file = Path.home() / ".config" / "autostart" / "jarvis.desktop"
        return desktop_file.is_file()

    if sys.platform == "win32":
        # 1. Windows Registry Run Key
        try:
            import winreg
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run",
                0,
                winreg.KEY_READ,
            ) as key:
                try:
                    val, _ = winreg.QueryValueEx(key, "JARVIS")
                    if val:
                        return True
                except FileNotFoundError:
                    pass
        except Exception:
            pass

        # 2. Windows Startup-Ordner
        try:
            appdata = os.getenv("APPDATA")
            if appdata:
                startup_dir = Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
                if (startup_dir / "JARVIS.bat").is_file() or (startup_dir / "JARVIS.lnk").is_file():
                    return True
        except Exception:
            pass
        return False

    return False


def set_autostart(enabled: bool) -> bool:
    """Aktiviert oder deaktiviert den Autostart für Raspberry Pi 5 und Windows."""
    success = False
    if sys.platform.startswith("linux"):
        autostart_dir = Path.home() / ".config" / "autostart"
        desktop_file = autostart_dir / "jarvis.desktop"
        if enabled:
            try:
                autostart_dir.mkdir(parents=True, exist_ok=True)
                base_dir = get_base_dir().resolve()
                script_path = (base_dir / "2_Pi_Jarvis_Starten.sh").resolve()
                content = (
                    "[Desktop Entry]\n"
                    "Type=Application\n"
                    "Name=JARVIS Command Center\n"
                    "Comment=Startet JARVIS automatisch nach dem Desktop-Login\n"
                    f'Exec=/bin/bash "{script_path}"\n'
                    f"Path={base_dir}\n"
                    "Terminal=false\n"
                    "X-GNOME-Autostart-enabled=true\n"
                )
                desktop_file.write_text(content, encoding="utf-8")
                try:
                    desktop_file.chmod(0o755)
                    if script_path.is_file():
                        script_path.chmod(0o755)
                except Exception:
                    pass
                success = True
            except Exception as exc:
                print(f"[Autostart Fehler Linux] {exc}", file=sys.stderr)
                success = False
        else:
            try:
                if desktop_file.exists():
                    desktop_file.unlink()
                success = True
            except Exception as exc:
                print(f"[Autostart Fehler Linux] {exc}", file=sys.stderr)
                success = False

    elif sys.platform == "win32":
        try:
            import winreg
            key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                key_path,
                0,
                winreg.KEY_SET_VALUE | winreg.KEY_READ,
            ) as key:
                if enabled:
                    if is_frozen():
                        cmd = f'"{sys.executable}"'
                    else:
                        base_dir = get_base_dir().resolve()
                        bat_file = base_dir / "2_Windows_Jarvis_Starten.bat"
                        if bat_file.exists():
                            cmd = f'"{bat_file}"'
                        else:
                            cmd = f'"{sys.executable}" "{base_dir / "main.py"}"'
                    winreg.SetValueEx(key, "JARVIS", 0, winreg.REG_SZ, cmd)
                    success = True
                else:
                    try:
                        winreg.DeleteValue(key, "JARVIS")
                    except FileNotFoundError:
                        pass
                    success = True
        except Exception as exc:
            print(f"[Autostart Registry Fehler] {exc}", file=sys.stderr)

        # Fallback / Ergänzung: Windows Startup-Ordner
        try:
            appdata = os.getenv("APPDATA")
            if appdata:
                startup_dir = Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
                bat_target = startup_dir / "JARVIS.bat"
                if enabled:
                    startup_dir.mkdir(parents=True, exist_ok=True)
                    if is_frozen():
                        bat_target.write_text(f'@start "" "{sys.executable}"\n', encoding="utf-8")
                    else:
                        base_dir = get_base_dir().resolve()
                        bat_file = base_dir / "2_Windows_Jarvis_Starten.bat"
                        bat_target.write_text(f'@call "{bat_file}"\n', encoding="utf-8")
                    success = True
                else:
                    if bat_target.exists():
                        bat_target.unlink()
                    lnk_target = startup_dir / "JARVIS.lnk"
                    if lnk_target.exists():
                        lnk_target.unlink()
                    success = True
        except Exception as exc:
            print(f"[Autostart Startup-Ordner Fehler] {exc}", file=sys.stderr)

    try:
        val_str = "true" if enabled else "false"
        os.environ["AUTOSTART"] = val_str
    except Exception:
        pass

    return success


# Zuordnung UI-Feldname -> (Primär-Key in .env, kompatibler Alias-Key)
SETTING_ALIASES: dict[str, tuple[str, ...]] = {
    "MODEL_PRIORITY": ("MODEL_PRIORITY",),
    "ACTIVE_PROVIDER": ("ACTIVE_PROVIDER",),
    "EXECUTION_MODE": ("EXECUTION_MODE",),
    "MAX_AGENT_STEPS": ("MAX_AGENT_STEPS",),
    "AUTOSTART": ("AUTOSTART",),
    "GEMINI_API_KEY": ("GEMINI_API_KEY",),
    "GROQ_API_KEY": ("GROQ_API_KEY",),
    "DEEPSEEK_API_KEY": ("DEEPSEEK_API_KEY",),
    "OPENROUTER_API_KEY": ("OPENROUTER_API_KEY",),
    "OLLAMA_TEXT_MODEL": ("OLLAMA_TEXT_MODEL",),
    "OLLAMA_VISION_MODEL": ("OLLAMA_VISION_MODEL",),
    "WAKE_WORD": ("WAKE_WORD",),
    "WHISPER_MODEL": ("WHISPER_MODEL",),
    "PIPER_MODEL_PATH": ("PIPER_MODEL_PATH",),
    "CAMERA_INDEX": ("CAMERA_INDEX",),
    "WATCHLIST": ("WATCHLIST",),
    "CITY": ("CITY", "WEATHER_CITY"),
    "NAS_MEMORY_PATH": ("NAS_MEMORY_PATH",),
    "ARCHIVE_PATH": ("ARCHIVE_PATH",),
    "EMAIL_USER": ("EMAIL_USER", "EMAIL_ADDRESS"),
    "EMAIL_PASS": ("EMAIL_PASS", "EMAIL_PASSWORD"),
    "IMAP_SERVER": ("IMAP_SERVER", "EMAIL_IMAP_SERVER"),
    "SMTP_SERVER": ("SMTP_SERVER", "EMAIL_SMTP_SERVER"),
    "TELEGRAM_BOT_TOKEN": ("TELEGRAM_BOT_TOKEN",),
    "TELEGRAM_ALLOWED_CHAT_ID": ("TELEGRAM_ALLOWED_CHAT_ID",),
    "N8N_BASE_URL": ("N8N_BASE_URL",),
    "N8N_API_KEY": ("N8N_API_KEY",),
    "N8N_WEBHOOK_URL": ("N8N_WEBHOOK_URL",),
}

DEFAULT_SETTINGS: dict[str, str] = {
    "MODEL_PRIORITY": DEFAULT_MODEL_PRIORITY,
    "ACTIVE_PROVIDER": "auto",
    "EXECUTION_MODE": "SEMI_AUTO",
    "MAX_AGENT_STEPS": "200",
    "AUTOSTART": "false",
    "GEMINI_API_KEY": "",
    "GROQ_API_KEY": "",
    "DEEPSEEK_API_KEY": "",
    "OPENROUTER_API_KEY": "",
    "OLLAMA_TEXT_MODEL": default_ollama_text_model(),
    "OLLAMA_VISION_MODEL": "moondream",
    "WAKE_WORD": "jarvis",
    "WHISPER_MODEL": "base",
    "PIPER_MODEL_PATH": "models/de_DE-karlsson-low.onnx",
    "CAMERA_INDEX": "0",
    "WATCHLIST": "NVDA,AAPL,MSFT,GC=F,BTC-USD,^GDAXI",
    "CITY": "",
    "NAS_MEMORY_PATH": "",
    "ARCHIVE_PATH": "./projekt_logs",
    "EMAIL_USER": "",
    "EMAIL_PASS": "",
    "IMAP_SERVER": "",
    "SMTP_SERVER": "",
    "TELEGRAM_BOT_TOKEN": "",
    "TELEGRAM_ALLOWED_CHAT_ID": "",
    "N8N_BASE_URL": "http://localhost:5678",
    "N8N_API_KEY": "",
    "N8N_WEBHOOK_URL": "",
}


def get_hot_cache_dir() -> Path:
    """
    Liefert den lokalen Stufe-1 HOT TIER Ordner auf der Laptop-NVMe-SSD:
    `%LOCALAPPDATA%\\JarvisCommandCenter\\HotCache`.
    Hält `active_brain.db`, den 1-FPS-RAM-Ringpuffer-Spiegel und Video-Proxys für 0ms Echtzeit-Zugriff.
    """
    if platform.system() == "Windows":
        local_appdata = os.getenv("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        hot_dir = Path(local_appdata) / "JarvisCommandCenter" / "HotCache"
    else:
        hot_dir = Path.home() / ".local" / "share" / "JarvisCommandCenter" / "HotCache"
    hot_dir.mkdir(parents=True, exist_ok=True)
    return hot_dir


def get_local_jarvis_brain_dir() -> Path:
    """Liefert den lokalen Fallback-Massenspeicher `%LOCALAPPDATA%\\JarvisBrain`."""
    if platform.system() == "Windows":
        local_appdata = os.getenv("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        d = Path(local_appdata) / "JarvisBrain"
    else:
        d = get_user_data_dir() / "JarvisBrain"
    d.mkdir(parents=True, exist_ok=True)
    return d


def discover_explorer_network_drives() -> list[dict[str, str]]:
    """
    Sucht automatisch nach verbundenen Netzlaufwerken im Windows-Explorer
    (`psutil.disk_partitions(all=True)`, `ctypes.windll.kernel32.GetDriveTypeW` / `win32wnet`).
    Gibt eine Liste gefundener Netzlaufwerke zurück: `[{"mountpoint": "Z:\\", "device": "\\\\NAS\\Share", "fstype": "CIFS"}]`.
    """
    found: list[dict[str, str]] = []
    seen_mounts: set[str] = set()

    # 1. psutil.disk_partitions(all=True) prüfen
    try:
        import psutil  # type: ignore

        for part in psutil.disk_partitions(all=True):
            mp = str(getattr(part, "mountpoint", "") or "").strip()
            dev = str(getattr(part, "device", "") or "").strip()
            fstype = str(getattr(part, "fstype", "") or "").strip()
            opts = str(getattr(part, "opts", "") or "").lower()
            is_net = (
                "remote" in opts
                or fstype.lower() in ("cifs", "smb", "smbfs", "nfs", "ncpfs", "webdav", "davfs")
                or dev.startswith("\\\\")
                or dev.startswith("//")
            )
            if is_net and mp and mp.upper() not in seen_mounts:
                seen_mounts.add(mp.upper())
                found.append({"mountpoint": mp, "device": dev, "fstype": fstype or "SMB/NAS"})
    except Exception:
        pass

    # 2. Windows-Kernel GetDriveTypeW (DRIVE_REMOTE == 4) & WNetGetConnectionW / win32wnet
    if os.name == "nt":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            mpr = getattr(ctypes.windll, "mpr", None)
            DRIVE_REMOTE = 4
            bitmask = kernel32.GetLogicalDrives()
            for idx in range(26):
                if not (bitmask & (1 << idx)):
                    continue
                letter = chr(ord("A") + idx)
                root_path = f"{letter}:\\"
                if root_path.upper() in seen_mounts:
                    continue
                dtype = kernel32.GetDriveTypeW(ctypes.c_wchar_p(root_path))
                if dtype == DRIVE_REMOTE:
                    unc_name = root_path
                    try:
                        import win32wnet  # type: ignore

                        unc_name = str(win32wnet.WNetGetUniversalName(root_path))
                    except Exception:
                        if mpr is not None:
                            try:
                                buf_len = ctypes.c_ulong(512)
                                buf = ctypes.create_unicode_buffer(512)
                                res = mpr.WNetGetConnectionW(
                                    ctypes.c_wchar_p(f"{letter}:"),
                                    buf,
                                    ctypes.byref(buf_len),
                                )
                                if res == 0 and buf.value:
                                    unc_name = buf.value
                            except Exception:
                                pass
                    seen_mounts.add(root_path.upper())
                    found.append({"mountpoint": root_path, "device": unc_name, "fstype": "Explorer-NAS"})
        except Exception:
            pass

    return found


def resolve_nas_memory_path() -> tuple[Path, bool, str]:
    """
    Ermittelt den Cold-Tier-Speicherort für das Hybrid-NAS-Gehirn (`NAS_MEMORY_PATH` / Explorer-NAS).
    Gibt `(cold_nas_path, is_remote_nas, configured_path_str)` zurück.
    """
    local_fallback = get_local_jarvis_brain_dir()
    raw_nas = (os.getenv("NAS_MEMORY_PATH") or "").strip()
    if not raw_nas and ENV_PATH.exists():
        try:
            raw_nas = str(dotenv_values(ENV_PATH).get("NAS_MEMORY_PATH") or "").strip()
        except Exception:
            raw_nas = ""

    if raw_nas:
        candidate = Path(raw_nas).expanduser()
        try:
            if candidate.resolve() == local_fallback.resolve():
                return local_fallback, False, raw_nas
        except Exception:
            pass
        if is_archive_reachable(candidate, timeout_seconds=2.2):
            return candidate, True, raw_nas
        return local_fallback, False, raw_nas

    # Automatische Explorer-Netzlaufwerk-Erkennung (z. B. Z:\Jarvis_Archiv)
    net_drives = discover_explorer_network_drives()
    for drv in net_drives:
        mp = drv.get("mountpoint", "")
        if not mp:
            continue
        candidate = Path(mp) / "Jarvis_Archiv"
        if is_archive_reachable(candidate, timeout_seconds=2.2):
            return candidate, True, str(candidate)

    return local_fallback, False, str(local_fallback)


def _probe_writable_dir(target: Path) -> bool:
    """Prüft Schreibzugriff und Mount-Zustand (verhindert Schreibfehler auf schlafendem NAS)."""
    try:
        if os.name == "nt" and target.drive and not target.drive.startswith("\\\\"):
            if not os.path.exists(f"{target.drive}\\"):
                return False

        posix_parts = target.parts
        if os.name != "nt" and len(posix_parts) >= 3 and posix_parts[1] in ("mnt", "media"):
            mount_candidate = Path("/", posix_parts[1], posix_parts[2])
            if not os.path.ismount(mount_candidate):
                return False

        target.mkdir(parents=True, exist_ok=True)
        probe_file = target / ".jarvis_nas_probe"
        probe_file.write_text("ok", encoding="utf-8")
        probe_file.unlink(missing_ok=True)
        return True
    except Exception:
        return False


def is_archive_reachable(target: Path, timeout_seconds: float = 1.8) -> bool:
    """Prüft mit kurzem Timeout, ob ein NAS-/Archiv-Pfad wach und beschreibbar ist."""
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(_probe_writable_dir, target)
    try:
        return bool(future.result(timeout=timeout_seconds))
    except (FuturesTimeoutError, Exception):
        return False
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def resolve_archive_root() -> tuple[Path, bool]:
    """Liefert (aktives_archiv_verzeichnis, nutzt_nas). Fällt bei NAS-Standby lokal zurück."""
    raw = (os.getenv("ARCHIVE_PATH") or DEFAULT_SETTINGS["ARCHIVE_PATH"]).strip()
    local_root = LOCAL_ARCHIVE_FALLBACK
    if not raw:
        local_root.mkdir(parents=True, exist_ok=True)
        return local_root, False

    candidate = Path(raw).expanduser()
    try:
        if candidate.resolve() == local_root.resolve():
            local_root.mkdir(parents=True, exist_ok=True)
            return local_root, False
    except Exception:
        pass

    if is_archive_reachable(candidate):
        return candidate, True

    local_root.mkdir(parents=True, exist_ok=True)
    return local_root, False


def get_archive_subdir(subdir: str = "") -> tuple[Path, bool]:
    """Gibt einen Unterordner im aktiven Archiv (NAS oder lokaler Fallback) zurück."""
    root, using_nas = resolve_archive_root()
    if not subdir or (not using_nas and subdir == "projekt_logs"):
        root.mkdir(parents=True, exist_ok=True)
        return root, using_nas

    target = root / subdir
    try:
        target.mkdir(parents=True, exist_ok=True)
        return target, using_nas
    except Exception:
        fallback = LOCAL_ARCHIVE_FALLBACK if subdir == "projekt_logs" else (LOCAL_ARCHIVE_FALLBACK / subdir)
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback, False


def ensure_env_file(env_path: Path = ENV_PATH, example_path: Path = ENV_EXAMPLE_PATH) -> Path:
    """Stellt sicher, dass eine .env-Datei und config.json existieren (ohne Konsolen-Abfrage)."""
    if not env_path.exists():
        env_path.parent.mkdir(parents=True, exist_ok=True)
        if example_path.exists():
            env_path.write_text(example_path.read_text(encoding="utf-8"), encoding="utf-8")
        else:
            lines = [f"{key}={default}" for key, default in DEFAULT_SETTINGS.items()]
            env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        process_installer_choice(env_path=env_path)
    else:
        choice_file = get_user_data_dir() / "installer_choice.json"
        if choice_file.is_file():
            process_installer_choice(env_path=env_path)
    try:
        if not get_config_json_path().exists():
            sync_config_json()
    except Exception:
        pass
    return env_path


def load_settings(env_path: Path = ENV_PATH) -> dict[str, str]:
    """Lädt aktuelle Einstellungen aus .env und Umgebungsvariablen."""
    ensure_env_file(env_path)
    file_vals = {k: (v or "") for k, v in dotenv_values(env_path).items()}

    result: dict[str, str] = {}
    for ui_key, keys in SETTING_ALIASES.items():
        val = ""
        for k in keys:
            candidate = clean_api_key(os.getenv(k) or file_vals.get(k) or "")
            if candidate:
                val = candidate
                break
        if not val:
            val = DEFAULT_SETTINGS.get(ui_key, "")
        result[ui_key] = val
    result["AUTOSTART"] = "true" if get_autostart_status() else "false"
    result["EXECUTION_MODE"] = normalize_execution_mode(result.get("EXECUTION_MODE"))
    result["MAX_AGENT_STEPS"] = str(normalize_max_agent_steps(result.get("MAX_AGENT_STEPS")))
    return result


def sync_env_aliases(environ: dict[str, str] | None = None) -> None:
    """Synchronisiert Alias-Variablen und bereinigt API-Keys im Speicher von Anführungszeichen/Leerzeichen."""
    target = environ if environ is not None else os.environ
    for api_var in ("GEMINI_API_KEY", "GROQ_API_KEY", "DEEPSEEK_API_KEY", "OPENROUTER_API_KEY"):
        if api_var in target:
            target[api_var] = clean_api_key(target.get(api_var))
    if "GROQ_MODEL" in target:
        target["GROQ_MODEL"] = sanitize_groq_model(target.get("GROQ_MODEL"))
    if "GEMINI_MODEL" in target:
        target["GEMINI_MODEL"] = sanitize_gemini_model(target.get("GEMINI_MODEL"))

    for ui_key, keys in SETTING_ALIASES.items():
        if len(keys) <= 1:
            continue
        resolved = ""
        for k in keys:
            val = clean_api_key(target.get(k, ""))
            if val:
                resolved = val
                break
        if resolved:
            for k in keys:
                target[k] = resolved


def save_settings(values: Mapping[str, str], env_path: Path = ENV_PATH) -> dict[str, str]:
    """Speichert Einstellungen dauerhaft in .env und übernimmt sie sofort in os.environ."""
    ensure_env_file(env_path)
    existing_lines = env_path.read_text(encoding="utf-8").splitlines()

    key_updates: dict[str, str] = {}
    normalized: dict[str, str] = {}
    for ui_key, keys in SETTING_ALIASES.items():
        if ui_key in values:
            val = str(values.get(ui_key, "")).strip()
        else:
            val = str(os.getenv(keys[0]) or DEFAULT_SETTINGS.get(ui_key, "")).strip()
        if ui_key == "EXECUTION_MODE":
            val = normalize_execution_mode(val)
        elif ui_key == "MAX_AGENT_STEPS":
            val = str(normalize_max_agent_steps(val))
        normalized[ui_key] = val
        for env_key in keys:
            key_updates[env_key] = val

    if "AUTOSTART" in values:
        target_autostart = str(values["AUTOSTART"]).strip().lower() in ("1", "true", "yes", "on")
        set_autostart(target_autostart)

    updated_lines: list[str] = []
    seen_keys: set[str] = set()

    for line in existing_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            updated_lines.append(line)
            continue

        k = line.split("=", 1)[0].strip()
        if k in key_updates:
            updated_lines.append(f"{k}={key_updates[k]}")
            seen_keys.add(k)
        else:
            updated_lines.append(line)

    missing_primary = [
        ui_key for ui_key in SETTING_ALIASES if SETTING_ALIASES[ui_key][0] not in seen_keys
    ]
    if missing_primary:
        if updated_lines and updated_lines[-1].strip():
            updated_lines.append("")
        for ui_key in missing_primary:
            primary_key = SETTING_ALIASES[ui_key][0]
            if primary_key not in seen_keys:
                updated_lines.append(f"{primary_key}={normalized[ui_key]}")
                seen_keys.add(primary_key)

    tmp_path = env_path.with_suffix(".tmp")
    tmp_path.write_text("\n".join(updated_lines) + "\n", encoding="utf-8")
    tmp_path.replace(env_path)

    for env_key, val in key_updates.items():
        os.environ[env_key] = val
    load_dotenv(dotenv_path=env_path, override=True)
    sync_env_aliases()
    try:
        if env_path == ENV_PATH:
            sync_config_json(normalized)
    except Exception:
        pass
    return normalized
