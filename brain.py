"""Multi-Provider-KI-Router: Gemini -> Groq -> DeepSeek -> OpenRouter -> Ollama."""

from __future__ import annotations

import base64
from collections import deque
from dataclasses import dataclass
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import threading
import time
from typing import TYPE_CHECKING, Callable, Iterator, Literal, Optional
import urllib.error
import urllib.parse
import urllib.request

from dotenv import load_dotenv

from google import genai
from google.genai import types
from openai import OpenAI

try:
    from groq import Groq
except ImportError:
    Groq = None  # type: ignore[assignment,misc]

from config import (
    clean_api_key,
    default_ollama_text_model,
    get_active_provider,
    get_env_path,
    get_is_online,
    get_model_priority,
    register_online_mode_listener,
    sanitize_gemini_model,
    sanitize_groq_model,
    set_active_provider,
    set_online_mode,
)

# .env direkt aus dem portablen Nutzerdaten-Verzeichnis (%LOCALAPPDATA%\JarvisCommandCenter im Release) laden
_PORTABLE_ENV_PATH = get_env_path()
if _PORTABLE_ENV_PATH.exists():
    load_dotenv(dotenv_path=_PORTABLE_ENV_PATH, override=True)

if TYPE_CHECKING:
    from memory import LongTermMemory


AIMode = Literal["online", "offline"]
KEYLESS_ONLINE_BASE_URL = "https://text.pollinations.ai/"
DEFAULT_KEYLESS_ONLINE_MODEL = "openai"
FORCE_VISUAL_BROWSER_SHOW: bool = True

_BROWSER_SHOW_GUARD = threading.local()

_PURE_SMALLTALK_REGEX = re.compile(
    r"^\s*(?:(?:hey|hi|hallo|servus|moin|guten\s+(?:morgen|tag|abend))(?:\s*,?\s*jarvis)?[\s,!.]*)?"
    r"(?:hey|hi|hallo|servus|moin|guten\s+(?:morgen|tag|abend)|"
    r"gute\s+nacht|wie\s+geht(?:'?s|\s+es\s+dir)(?:\s+jarvis|\s+heute)?|"
    r"alles\s+(?:klar|gut)(?:\s+bei\s+dir)?|danke(?:\s+dir|\s+sehr|\s+schön)?|"
    r"vielen\s+dank|tschüss|ciao|bis\s+(?:bald|später|gleich)|"
    r"wer\s+bist\s+du|was\s+kannst\s+du|gut\s+gemacht|super|perfekt|cool|nice)"
    r"(?:\s*,?\s*jarvis)?\s*[!?.]*\s*$",
    flags=re.IGNORECASE,
)

_BROWSER_SHOW_FORCE_TOPIC_REGEX = re.compile(
    r"\b(?:gold|goldpreis|silber|silberpreis|ölpreis|xauusd|aktie\w*|kurs\w*|preis\w*|kosten?|"
    r"bitcoin|btc|ethereum|eth|krypto\w*|nvidia|nvda|apple|tesla|rheinmetall|microsoft|amazon|"
    r"dax|nasdaq|s&p|chart\w*|tradingview|finanzen|"
    r"wetter|vorhersage|temperatur|regen|schnee|nachrichten|news|tagesschau|"
    r"such\w*|recherchier\w*|produkt\w*|kaufen|vergleich\w*|testbericht|rezension|"
    r"video\w*|youtube|wiki\w*|ort\w*|stadt|land|entfernung|route|"
    r"wer\s+(?:ist|war|hat|gewann)|was\s+(?:ist|sind|bedeutet|kostet)|"
    r"wie\s+(?:sieht\s+es|steht|wird|hoch|viel|alt|weit|lange)|"
    r"wann\s+(?:ist|war|startet|erscheint)|wo\s+(?:ist|liegt|findet)|warum)\b",
    flags=re.IGNORECASE,
)

_LOCAL_CONTROL_EXCLUSION_REGEX = re.compile(
    r"^\s*(?:ja|nein|abbrechen|stopp|ok|okay|mach\s+das|starte)\s*[!.]*\s*$|"
    r"\b(?:schalte\s+(?:das|die|den)|steckdose|relais|wecker|weck\s+mich|"
    r"lauter|leiser|ton\s+aus|stumm|merk(?:e)?\s+dir|systemstatus|depotstatus|portfolio\s*status|"
    r"executive[\s\-]*briefing|starte\s+mein\s+briefing|morgen[\s\-]*briefing|"
    r"letzten\s+projekt\s+weiter|datei\s+von\s+gestern|projekt[\s\-]*setup|"
    r"öffne\s+(?:rechner|taschenrechner|editor|notepad|explorer|einstellungen|steam|discord|spotify|orcaslicer|solid\s*craft)|"
    r"klick\w*\s+auf|doppelklick|maus|auf\s+dem\s+desktop)\b",
    flags=re.IGNORECASE,
)


def is_smalltalk_only(user_text: str) -> bool:
    """
    Prüft streng, ob die Eingabe ausschließlich reiner Smalltalk/Begrüßung ohne
    Wissens-, Preis-, Kurs-, Wetter-, Produkt-, Orts-, Personen-, Video- oder Suchbezug ist.
    """
    clean = (user_text or "").strip()
    if not clean:
        return True
    if _BROWSER_SHOW_FORCE_TOPIC_REGEX.search(clean):
        return False
    if _PURE_SMALLTALK_REGEX.match(clean):
        return True
    return False


def should_force_browser_show(
    user_text: str,
    system_prompt: Optional[str] = None,
    use_history: bool = True,
    image_bytes: Optional[bytes] = None,
    bypass_browser_show: bool = False,
) -> bool:
    """
    Harte 'Browser-Show-First'-Weiche in `brain.py`:
    Wenn aktiv (`FORCE_VISUAL_BROWSER_SHOW = True`), erzwingt sie bei jeder Nutzerfrage
    außer reinem Smalltalk (oder rein lokalen OS-/Smart-Home-Befehlen) die sichtbare
    Ausführung über `VISUAL_HAND_AGENT.visual_browser_search_and_read`.
    Interne System-Prompts (`system_prompt is not None` / `use_history=False` / Vision-Loops)
    werden über `_BROWSER_SHOW_GUARD` sauber ausgeschlossen.
    """
    if not FORCE_VISUAL_BROWSER_SHOW or bypass_browser_show:
        return False
    if getattr(_BROWSER_SHOW_GUARD, "active", False):
        return False
    if system_prompt is not None or not use_history or image_bytes is not None:
        return False
    if not get_is_online():
        return False
    clean = (user_text or "").strip()
    if not clean or is_smalltalk_only(clean):
        return False
    if _LOCAL_CONTROL_EXCLUSION_REGEX.search(clean) and not _BROWSER_SHOW_FORCE_TOPIC_REGEX.search(clean):
        return False
    return True

JARVIS_SYSTEM_PROMPT = (
    "Du bist Jarvis, ein smarter, schlagfertiger und entspannt-professioneller KI-Assistent für Alltag und Technik "
    "nach dem Grundgesetz '100% VISIBLE DESKTOP & APP OPERATOR'. "
    "PERMANENTE LIVE-DESKTOP-SICHT: Du erhältst bei jeder Anfrage automatisch einen aktuellen Desktop-Screenshot "
    "mit normiertem 0-1000-Koordinatengitter und rotem Maus-Fadenkreuz, sodass du jederzeit siehst, was auf dem Bildschirm "
    "und in geöffneten Apps passiert. "
    "VOLLE KONTROLLE ÜBER JEDE APP: Du steuerst jede sichtbare Windows-App, jedes Spiel, jeden Launcher (z. B. ArcCraft, FilmCraft, "
    "Solid Craft 3D / CAD, OrcaSlicer, Creality Print, CapCut, DaVinci Resolve, Premiere, VS Code, Steam, Discord, Spotify, Explorer, Editoren, Webbrowser) "
    "vollständig sichtbar über Maus und Tastatur (`click`, `double_click`, `right_click`, `drag_and_drop`, `click_and_type`, `scroll`, `hotkey`). "
    "STRIKT VERBOTEN: Sage NIEMALS 'Ich kann diese App nicht steuern' oder 'Ich habe keinen Zugriff' – "
    "wenn der Nutzer etwas auf dem Desktop oder in einer App will, übernimm die sichtbare Maus-/Tastatur-Steuerung über `visual_desktop_app_operator`! "
    "STRIKTES VERBOT UNSICHTBARER HINTERGRUND-SUCHEN: Du darfst NIEMALS im Hintergrund still über Python, "
    "requests, DuckDuckGo, Wikipedia-APIs oder yfinance Webseiten auslesen oder Aufgaben unsichtbar im Code lösen, "
    "wenn es dafür den Webbrowser oder eine installierte App auf dem Laptop gibt. "
    "PFLICHT-ROUTING: "
    "1) Sobald der Nutzer eine Suchanfrage stellt, nach aktuellen Infos, Preisen, Wetter, Nachrichten oder Fakten fragt, "
    "starte zwingend das sichtbare Browser- & Maus-Tool `visual_browser_search_and_read`, damit alles live auf dem Monitor geöffnet, gesucht, angeklickt und direkt vom Screenshot abgelesen wird. "
    "2) Sobald der Nutzer eine lokale Aktion will (CAD / Solid Craft 3D-Konstruktion, 3D-Druck / OrcaSlicer, Videoschnitt, Programmieren einer App/eines Spiels, Rechnen, Musik, Einstellungen, Dateien, Notizen, Launcher), "
    "gehe zwingend sichtbar über den Desktop oder die installierten Laptop-Apps via `visual_desktop_app_operator`. "
    "Du sprichst mit dem Nutzer ('Chef') stets auf Augenhöhe — natürlich, direkt, hilfreich und mit feinem Humor, wenn es passt. "
    "Antworte immer auf echtem, flüssigem Deutsch. "
    "KRITISCH FÜR DIE SPRACHAUSGABE: Gib ausschließlich reinen, gut vorlesbaren Fließtext aus — "
    "kein Markdown (keine Sternchen, Rauten oder Backticks), keine Aufzählungszeichen, keine Emojis und keine URLs. "
    "Wenn ein Desktop-Screenshot oder Kamerabild dabei ist, beziehe das Sichtbare direkt und präzise auf Deutsch ein."
)

WORKBENCH_VISION_SYSTEM_PROMPT = (
    "Du bist Jarvis in der Rolle eines erfahrenen, praxisnahen Elektronik-, Mechanik- und 3D-Druck-Partners. "
    "Die Kamera blickt auf die Werkbank des Nutzers ('Chef'). "
    "Beschreibe in lockerem, klarem Deutsch genau, welche Bauteile, Platinen, Kabel, Werkzeuge oder 3D-Drucke zu sehen sind, "
    "erkenne mögliche Fehler (lose Kabel, falsche Polung, fehlende Teile) und gib sofort umsetzbare Tipps. "
    "KRITISCH FÜR DIE SPRACHAUSGABE: Nur reiner Fließtext auf Deutsch, kein Markdown, keine Aufzählungen, keine Emojis, keine URLs."
)

OFFLINE_UNAVAILABLE_MESSAGE = (
    "Gerade ist leider weder ein Online-Dienst noch der lokale Ollama-Server erreichbar. "
    "Schau bitte kurz nach der Internetverbindung oder ob Ollama läuft."
)


def _clean_key(raw: Optional[str]) -> str:
    return clean_api_key(raw)


GEMINI_FALLBACK_MODELS: tuple[str, ...] = (
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.8-flash",
    "gemini-3.1-flash-lite",
)

GROQ_FALLBACK_MODELS: tuple[str, ...] = (
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
    "qwen/qwen3.8-27b",
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
)


def _build_gemini_model_candidates(primary_model: str, preferred_working: Optional[str] = None) -> list[str]:
    ordered: list[str] = []
    if preferred_working:
        ordered.append(sanitize_gemini_model(preferred_working))
    cleaned_primary = sanitize_gemini_model(primary_model)
    if cleaned_primary and cleaned_primary not in ordered:
        ordered.append(cleaned_primary)
    for m in GEMINI_FALLBACK_MODELS:
        if m not in ordered:
            ordered.append(m)
    return ordered


def _build_groq_model_candidates(primary_model: str, preferred_working: Optional[str] = None) -> list[str]:
    ordered: list[str] = []
    if preferred_working:
        ordered.append(preferred_working.strip())
    cleaned_primary = sanitize_groq_model(primary_model)
    if cleaned_primary and cleaned_primary not in ordered:
        ordered.append(cleaned_primary)
    for m in GROQ_FALLBACK_MODELS:
        if m not in ordered:
            ordered.append(m)
    return ordered


def _classify_api_error(exc: Exception) -> str:
    """Klassifiziert API-Fehler mit klarer deutscher Ursache + Original-Details für das Terminal."""
    raw = str(exc).strip()
    lower = raw.lower()
    if isinstance(exc, (ImportError, ModuleNotFoundError)):
        return f"Paket fehlt ({raw})"
    if (
        "401" in lower
        or "403" in lower
        or "api_key_invalid" in lower
        or "invalid_api_key" in lower
        or "invalid api key" in lower
        or "authentication" in lower
        or "permission_denied" in lower
    ):
        return f"Falscher API-Key / Authentifizierung fehlgeschlagen ({raw})"
    if (
        "404" in lower
        or "not_found" in lower
        or "model_not_found" in lower
        or "does not exist" in lower
        or "no longer available" in lower
        or "decommissioned" in lower
    ):
        return f"Modell existiert nicht oder wurde abgeschaltet ({raw})"
    if "429" in lower or "resource_exhausted" in lower or "rate_limit" in lower or "quota" in lower:
        return f"Rate-Limit / Kontingent erschöpft ({raw})"
    if "503" in lower or "unavailable" in lower or "overloaded" in lower or "high demand" in lower:
        return f"Server aktuell überlastet ({raw})"
    if "timed out" in lower or "timeout" in lower or "connection" in lower:
        return f"Zeitüberschreitung / Verbindungsfehler ({raw})"
    return f"API-Fehler ({raw})"


def _strip_reasoning_tags(text: str) -> str:
    """Entfernt optionale <think>...</think>-Blöcke von Reasoning-Modellen für saubere Sprachausgabe."""
    if "<think>" in text:
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    return text


def verify_provider_key(provider: str, api_key: str) -> dict[str, object]:
    """Testet einen API-Key mit minimalem Token-Aufruf (Timeout 6s) und misst die Latenz."""
    p = str(provider).strip().lower()
    key = _clean_key(api_key)

    if p != "pollinations" and not key:
        return {"valid": False, "latency_ms": None, "error": "Kein API-Key angegeben."}

    t0 = time.perf_counter()
    try:
        if p == "gemini":
            client = genai.Client(api_key=key, http_options=types.HttpOptions(timeout=6000))
            last_err: Optional[Exception] = None
            configured_gemini = sanitize_gemini_model(os.getenv("GEMINI_MODEL", "gemini-2.5-flash"))
            for cand in _build_gemini_model_candidates(configured_gemini):
                try:
                    client.models.generate_content(
                        model=cand,
                        contents="ping",
                        config=types.GenerateContentConfig(max_output_tokens=16),
                    )
                    last_err = None
                    break
                except Exception as e:
                    last_err = e
                    err_l = str(e).lower()
                    if "401" in err_l or "api_key_invalid" in err_l or "invalid api key" in err_l:
                        raise
            if last_err is not None:
                raise last_err
        elif p == "groq":
            configured_groq = sanitize_groq_model(os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile"))
            last_err = None
            for cand in _build_groq_model_candidates(configured_groq):
                try:
                    if Groq is not None:
                        gclient = Groq(api_key=key, timeout=6.0)
                        gclient.chat.completions.create(
                            model=cand,
                            messages=[{"role": "user", "content": "ping"}],
                            max_tokens=16,
                        )
                    else:
                        client_oai = OpenAI(api_key=key, base_url="https://api.groq.com/openai/v1", timeout=6.0)
                        client_oai.chat.completions.create(
                            model=cand,
                            messages=[{"role": "user", "content": "ping"}],
                            max_tokens=16,
                        )
                    last_err = None
                    break
                except Exception as e:
                    last_err = e
                    err_l = str(e).lower()
                    if "401" in err_l or "invalid_api_key" in err_l or "invalid api key" in err_l:
                        raise
            if last_err is not None:
                raise last_err
        elif p == "openrouter":
            client = OpenAI(
                api_key=key,
                base_url="https://openrouter.ai/api/v1",
                timeout=6.0,
                default_headers={"X-Title": "Jarvis Desktop Assistent"},
            )
            client.chat.completions.create(
                model="openrouter/free",
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=16,
            )
        elif p == "deepseek":
            client = OpenAI(api_key=key, base_url="https://api.deepseek.com", timeout=6.0)
            client.chat.completions.create(
                model="deepseek-chat",
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=16,
            )
        elif p == "pollinations":
            req = urllib.request.Request(
                "https://text.pollinations.ai/ping?model=openai",
                headers={"User-Agent": "Jarvis-Desktop/2.0"},
            )
            with urllib.request.urlopen(req, timeout=6.0) as resp:
                _ = resp.read()
        else:
            return {"valid": False, "latency_ms": None, "error": f"Unbekannter Anbieter: {provider}"}

        latency_ms = max(1, int((time.perf_counter() - t0) * 1000))
        return {"valid": True, "latency_ms": latency_ms, "error": None}
    except Exception as exc:
        latency_ms = int((time.perf_counter() - t0) * 1000)
        err_msg = _classify_api_error(exc)
        return {"valid": False, "latency_ms": latency_ms, "error": err_msg}


def _is_vision_capable_groq(model: str) -> bool:
    lowered = model.lower()
    return any(tag in lowered for tag in ("scout", "maverick", "vision", "llava"))


_VISION_ONLY_OLLAMA_TOKENS = ("moondream", "llava", "bakllava", "cogvlm", "embed")
_PREFERRED_TEXT_MODEL_PREFIXES = ("qwen2.5:7b", "gemma3:4b", "qwen2.5:3b", "qwen2.5", "gemma3", "llama", "mistral", "phi")
_PULL_LOCK = threading.Lock()
_PULLING_MODELS: set[str] = set()


def _normalize_ollama_base_url(host: str) -> str:
    base = (host or "http://127.0.0.1:11434").strip().rstrip("/")
    if not base:
        return "http://127.0.0.1:11434"
    # Vermeidet IPv6-::1-Auflösungsverzögerungen unter Windows
    base = base.replace("://localhost:", "://127.0.0.1:").replace("://localhost", "://127.0.0.1")
    return base


def _is_ollama_reachable(base_url: str, timeout: float = 1.0) -> bool:
    req = urllib.request.Request(f"{base_url}/api/tags", method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(getattr(resp, "status", 200)) == 200
    except Exception:
        return False


def _find_ollama_executable() -> Optional[str]:
    if platform.system() == "Windows":
        candidate = os.path.expandvars(r"%LOCALAPPDATA%\Programs\Ollama\ollama.exe")
        if candidate and os.path.isfile(candidate):
            return candidate
    return shutil.which("ollama")


def _popen_hidden(args: list[str]) -> Optional[subprocess.Popen[bytes]]:
    if platform.system() == "Windows":
        flags = (
            getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0
        return subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
            startupinfo=startupinfo,
        )
    return subprocess.Popen(
        args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _ensure_ollama_running(base_url: str, wait_seconds: float = 8.0) -> bool:
    if _is_ollama_reachable(base_url, timeout=0.9):
        return True

    exe = _find_ollama_executable()
    if not exe:
        return False

    try:
        print(f"[Ollama] Dienst unter {base_url} nicht erreichbar – starte '{exe} serve' im Hintergrund...")
        _popen_hidden([exe, "serve"])
    except Exception:
        return False

    # Mindestens 2 Sekunden auf Server-Initialisierung warten, danach kurz weiter pollen
    time.sleep(2.0)
    deadline = time.monotonic() + max(1.0, wait_seconds - 2.0)
    while True:
        if _is_ollama_reachable(base_url, timeout=0.9):
            return True
        if time.monotonic() >= deadline:
            break
        time.sleep(0.4)
    return _is_ollama_reachable(base_url, timeout=0.9)


def _fetch_ollama_models(base_url: str, timeout: float = 2.0) -> list[str]:
    req = urllib.request.Request(f"{base_url}/api/tags", method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        data = json.loads(raw)
        models = data.get("models") if isinstance(data, dict) else None
        if not isinstance(models, list):
            return []
        names: list[str] = []
        for item in models:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or item.get("model") or "").strip()
            if name:
                names.append(name)
        return names
    except Exception:
        return []


def _is_vision_only_ollama_model(model_name: str) -> bool:
    lowered = model_name.strip().lower()
    return any(tok in lowered for tok in _VISION_ONLY_OLLAMA_TOKENS)


def _match_installed_model(installed: list[str], preferred: str) -> Optional[str]:
    target = preferred.strip().lower()
    if not target:
        return None
    for name in installed:
        if name.lower() == target:
            return name
    if ":" not in target:
        with_latest = f"{target}:latest"
        for name in installed:
            if name.lower() == with_latest:
                return name
    return None


def _select_fallback_text_model(installed: list[str]) -> Optional[str]:
    text_candidates = [m for m in installed if not _is_vision_only_ollama_model(m)]
    if not text_candidates:
        return None
    for prefix in _PREFERRED_TEXT_MODEL_PREFIXES:
        for candidate in text_candidates:
            if candidate.lower().startswith(prefix):
                return candidate
    return text_candidates[0]


def _start_ollama_pull_background(model: str) -> bool:
    exe = _find_ollama_executable()
    if not exe:
        return False
    target = model.strip() or default_ollama_text_model()
    with _PULL_LOCK:
        if target in _PULLING_MODELS:
            return True
        _PULLING_MODELS.add(target)

    def _runner() -> None:
        try:
            print(f"[Ollama] Starte automatischen Hintergrund-Download: ollama pull {target}")
            proc = _popen_hidden([exe, "pull", target])
            if proc is not None:
                proc.wait()
        except Exception as exc:
            print(f"[Ollama] Hintergrund-Pull für {target} fehlgeschlagen: {exc}")
        finally:
            with _PULL_LOCK:
                _PULLING_MODELS.discard(target)

    threading.Thread(target=_runner, daemon=True).start()
    return True


def _warmup_ollama_model(base_url: str, model: str, timeout: float = 25.0) -> bool:
    """Lädt das Ollama-Modell still in RAM/VRAM vor (keep_alive=30m)."""
    target = model.strip()
    if not target:
        return False
    payload = json.dumps({"model": target, "keep_alive": "30m"}).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(getattr(resp, "status", 200)) == 200
    except Exception:
        return False


@dataclass
class BrainResponse:
    """Ergebnis einer KI-Anfrage inklusive genutztem Anbieter und Modell."""

    text: str
    provider: str
    model: str
    already_streamed: bool = False


@dataclass
class _ProviderStep:
    name: str
    model: str
    supports_vision: bool
    stream_call: Callable[[str, Optional[bytes], str, bool], Iterator[str]]


class JarvisBrain:
    """Routet Anfragen durch die kostenlose 4-Stufen-KI-Kaskade bis zum lokalen Ollama."""

    def __init__(
        self,
        gemini_api_key: Optional[str] = None,
        gemini_model: str = "gemini-2.5-flash",
        groq_api_key: Optional[str] = None,
        groq_model: str = "llama-3.3-70b-versatile",
        deepseek_api_key: Optional[str] = None,
        deepseek_model: str = "deepseek-chat",
        openrouter_api_key: Optional[str] = None,
        openrouter_model: str = "openrouter/free",
        ollama_host: str = "http://127.0.0.1:11434",
        ollama_text_model: Optional[str] = None,
        ollama_vision_model: str = "moondream",
        timeout_seconds: float = 20.0,
        max_history_turns: int = 4,
        memory: Optional[LongTermMemory] = None,
        on_log: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.memory = memory
        self._on_log = on_log

        self.gemini_model = sanitize_gemini_model(gemini_model)
        self.groq_model = sanitize_groq_model(groq_model)
        self.deepseek_model = _clean_key(deepseek_model) or "deepseek-chat"
        self.openrouter_model = _clean_key(openrouter_model) or "openrouter/free"
        self._working_gemini_model: Optional[str] = None
        self._working_groq_model: Optional[str] = None
        self.keyless_model = (
            os.getenv("KEYLESS_ONLINE_MODEL", DEFAULT_KEYLESS_ONLINE_MODEL).strip()
            or DEFAULT_KEYLESS_ONLINE_MODEL
        )
        self.ollama_host = _normalize_ollama_base_url(ollama_host)
        self.ollama_text_model = (ollama_text_model or default_ollama_text_model()).strip() or default_ollama_text_model()
        self.ollama_vision_model = ollama_vision_model
        self._active_ollama_model = self.ollama_text_model

        self._history: deque[tuple[str, str]] = deque(maxlen=max_history_turns * 2)
        self.is_busy: bool = False
        self._passive_screen_lock = threading.Lock()
        self._passive_screen_bytes: bytes = b""
        self._passive_screen_at: float = 0.0
        self._mode: AIMode = "online" if get_is_online() else "offline"
        self._init_clients(
            gemini_api_key=gemini_api_key,
            groq_api_key=groq_api_key,
            deepseek_api_key=deepseek_api_key,
            openrouter_api_key=openrouter_api_key,
        )
        register_online_mode_listener(self._on_online_mode_changed)
        self._schedule_initial_ollama_check()

    def set_log_callback(self, callback: Optional[Callable[[str], None]]) -> None:
        self._on_log = callback

    def _emit_log(self, message: str) -> None:
        print(f"[KI] {message}")
        if self._on_log is not None:
            try:
                self._on_log(message)
            except Exception:
                pass

    def _schedule_initial_ollama_check(self) -> None:
        def _boot_check() -> None:
            time.sleep(1.5)
            base_url = _normalize_ollama_base_url(self.ollama_host)
            if not _is_ollama_reachable(base_url, timeout=1.0):
                started = _ensure_ollama_running(base_url, wait_seconds=10.0)
                if started:
                    self._emit_log("Ollama-Server automatisch im Hintergrund gestartet.")
            if not _is_ollama_reachable(base_url, timeout=1.0):
                return

            installed = _fetch_ollama_models(base_url)
            matched_text = _match_installed_model(installed, self.ollama_text_model)
            if not matched_text:
                if _start_ollama_pull_background(self.ollama_text_model):
                    self._emit_log(
                        f"Ollama: Lade Standardmodell '{self.ollama_text_model}' im Hintergrund herunter..."
                    )
            else:
                if _warmup_ollama_model(base_url, matched_text):
                    self._emit_log(f"Ollama: Modell '{matched_text}' im Speicher vorgewärmt.")

            if self.ollama_vision_model and not _match_installed_model(installed, self.ollama_vision_model):
                if _start_ollama_pull_background(self.ollama_vision_model):
                    self._emit_log(
                        f"Ollama: Lade Vision-Modell '{self.ollama_vision_model}' im Hintergrund herunter..."
                    )

        threading.Thread(target=_boot_check, daemon=True).start()

    def _init_clients(
        self,
        gemini_api_key: Optional[str] = None,
        groq_api_key: Optional[str] = None,
        deepseek_api_key: Optional[str] = None,
        openrouter_api_key: Optional[str] = None,
    ) -> None:
        env_p = get_env_path()
        if env_p.exists():
            load_dotenv(dotenv_path=env_p, override=True)
        gemini_key = _clean_key(gemini_api_key if gemini_api_key is not None else os.getenv("GEMINI_API_KEY"))
        groq_key = _clean_key(groq_api_key if groq_api_key is not None else os.getenv("GROQ_API_KEY"))
        deepseek_key = _clean_key(deepseek_api_key if deepseek_api_key is not None else os.getenv("DEEPSEEK_API_KEY"))
        openrouter_key = _clean_key(openrouter_api_key if openrouter_api_key is not None else os.getenv("OPENROUTER_API_KEY"))

        self._gemini_key = gemini_key
        self._groq_key = groq_key
        self._gemini_client = (
            genai.Client(
                api_key=gemini_key,
                http_options=types.HttpOptions(timeout=max(5000, int(self.timeout_seconds * 1000))),
            )
            if gemini_key
            else None
        )
        if groq_key:
            if Groq is not None:
                self._groq_client: object = Groq(api_key=groq_key, timeout=self.timeout_seconds)
            else:
                self._groq_client = OpenAI(
                    api_key=groq_key,
                    base_url="https://api.groq.com/openai/v1",
                    timeout=self.timeout_seconds,
                )
        else:
            self._groq_client = None
        self._deepseek_client = (
            OpenAI(api_key=deepseek_key, base_url="https://api.deepseek.com", timeout=self.timeout_seconds)
            if deepseek_key
            else None
        )
        self._openrouter_client = (
            OpenAI(
                api_key=openrouter_key,
                base_url="https://openrouter.ai/api/v1",
                timeout=self.timeout_seconds,
                default_headers={"X-Title": "Jarvis Desktop Assistent"},
            )
            if openrouter_key
            else None
        )
        self._ollama_base_url = _normalize_ollama_base_url(self.ollama_host)

        has_cloud = any(
            client is not None
            for client in (
                self._gemini_client,
                self._groq_client,
                self._deepseek_client,
                self._openrouter_client,
            )
        )
        self._mode = "online" if get_is_online() else "offline"
        if self._mode == "online" and not has_cloud:
            print(
                f"[Online-KI] Keine Cloud-API-Keys gesetzt – nutze kostenlose Online-KI (Pollinations) + Ollama-Fallback."
            )
        elif self._mode == "offline":
            print(f"[Offline-Modus] Nutze ausschließlich lokales Ollama ({self.ollama_text_model}).")

        # Startet beim Systemstart im Hintergrund den Tiefen-App-Scanner (apps_cache.json)
        try:
            from app_indexer import APP_INDEXER

            APP_INDEXER.start_background_scan()
        except Exception:
            pass

    def run_startup_api_check(self) -> None:
        """Prüft beim Start Gemini und Groq und gibt bei Fehlern die exakte Ursache im Terminal aus."""
        if self._mode == "offline":
            print("[API-Check] Offline-Modus aktiv – überspringe Cloud-API-Check (nutze Ollama).")
            return

        print("[API-Check] Prüfe Cloud-APIs (Google Gemini & Groq)...")

        # 1. Google Gemini Verbindungs-Check
        if not self._gemini_key:
            print("[API-Check] Google Gemini: Kein GEMINI_API_KEY in .env gesetzt – überspringe Gemini.")
        elif self._gemini_client is None:
            print("[API-Check] Google Gemini FEHLER: Paket 'google-genai' fehlt oder Client konnte nicht initialisiert werden.")
        else:
            gemini_ok = False
            candidates = _build_gemini_model_candidates(self.gemini_model, self._working_gemini_model)
            for idx, cand in enumerate(candidates):
                try:
                    self._gemini_client.models.generate_content(
                        model=cand,
                        contents="Antworte mit OK",
                        config=types.GenerateContentConfig(max_output_tokens=16),
                    )
                    self._working_gemini_model = cand
                    gemini_ok = True
                    if cand != self.gemini_model:
                        print(
                            f"[API-Check] Google Gemini: OK (automatischer Modell-Wechsel von '{self.gemini_model}' auf aktives Modell '{cand}')."
                        )
                    else:
                        print(f"[API-Check] Google Gemini: OK (Modell '{cand}' verbunden).")
                    break
                except Exception as exc:
                    reason = _classify_api_error(exc)
                    if idx == 0:
                        print(f"[API-Check] Google Gemini Hinweis für Modell '{cand}': {reason}")
                    err_l = str(exc).lower()
                    if "401" in err_l or "api_key_invalid" in err_l or "invalid api key" in err_l:
                        break
            if not gemini_ok:
                print(
                    "[API-Check] Google Gemini FEHLER: Alle getesteten Gemini-Modelle nicht erreichbar – wechsle bei Anfragen auf Groq / Ollama."
                )

        # 2. Groq Verbindungs-Check
        if not self._groq_key:
            print("[API-Check] Groq: Kein GROQ_API_KEY in .env gesetzt – überspringe Groq.")
        elif Groq is None and self._groq_client is None:
            print("[API-Check] Groq FEHLER: Paket 'groq' fehlt in der .venv.")
        else:
            groq_ok = False
            candidates = _build_groq_model_candidates(self.groq_model, self._working_groq_model)
            for idx, cand in enumerate(candidates):
                try:
                    self._groq_client.chat.completions.create(  # type: ignore[union-attr]
                        model=cand,
                        messages=[{"role": "user", "content": "Antworte mit OK"}],
                        max_tokens=16,
                    )
                    self._working_groq_model = cand
                    groq_ok = True
                    if cand != self.groq_model:
                        print(
                            f"[API-Check] Groq: OK (automatischer Modell-Wechsel von '{self.groq_model}' auf aktives Modell '{cand}')."
                        )
                    else:
                        print(f"[API-Check] Groq: OK (Modell '{cand}' verbunden).")
                    break
                except Exception as exc:
                    reason = _classify_api_error(exc)
                    if idx == 0:
                        print(f"[API-Check] Groq Hinweis für Modell '{cand}': {reason}")
                    err_l = str(exc).lower()
                    if "401" in err_l or "invalid_api_key" in err_l or "invalid api key" in err_l:
                        break
            if not groq_ok:
                print(
                    "[API-Check] Groq FEHLER: Keine Verbindung zu Groq möglich – wechsle bei Anfragen auf Fallback / Ollama."
                )

    def reload_from_env(self) -> AIMode:
        """Lädt API-Keys und Modellnamen live aus os.environ neu (ohne Neustart)."""
        env_p = get_env_path()
        if env_p.exists():
            load_dotenv(dotenv_path=env_p, override=True)
        self.gemini_model = sanitize_gemini_model(os.getenv("GEMINI_MODEL", self.gemini_model))
        self.groq_model = sanitize_groq_model(os.getenv("GROQ_MODEL", self.groq_model))
        self._working_gemini_model = None
        self._working_groq_model = None
        self.deepseek_model = _clean_key(os.getenv("DEEPSEEK_MODEL", self.deepseek_model)) or self.deepseek_model
        self.openrouter_model = _clean_key(os.getenv("OPENROUTER_MODEL", self.openrouter_model)) or self.openrouter_model
        self.keyless_model = (
            os.getenv("KEYLESS_ONLINE_MODEL", self.keyless_model).strip() or DEFAULT_KEYLESS_ONLINE_MODEL
        )
        self.ollama_host = _normalize_ollama_base_url(
            os.getenv("OLLAMA_HOST", self.ollama_host).strip() or self.ollama_host
        )
        self.ollama_text_model = os.getenv("OLLAMA_TEXT_MODEL", self.ollama_text_model).strip() or self.ollama_text_model
        self.ollama_vision_model = os.getenv("OLLAMA_VISION_MODEL", self.ollama_vision_model).strip() or self.ollama_vision_model
        self._init_clients()
        return self._mode

    def _on_online_mode_changed(self, enabled: bool) -> None:
        self._mode = "online" if enabled else "offline"

    def active_online_provider(self) -> tuple[str, str]:
        """Liefert den priorisierten aktiven Online-Anbieter samt Modellnamen."""
        active = get_active_provider()
        if active == "ollama":
            return ("Ollama (Lokal)", self.ollama_text_model)
        if active == "gemini" and self._gemini_client is not None:
            return ("Google Gemini", self.gemini_model)
        if active == "groq" and self._groq_client is not None:
            return ("Groq", self.groq_model)
        if active == "openrouter" and self._openrouter_client is not None:
            return ("OpenRouter", self.openrouter_model)
        if active == "deepseek" and self._deepseek_client is not None:
            return ("DeepSeek", self.deepseek_model)
        if active == "pollinations":
            return ("Pollinations (Kostenlos)", self.keyless_model)

        prio_list = get_model_priority()
        mapping = {
            "gemini": (self._gemini_client, "Google Gemini", self.gemini_model),
            "groq": (self._groq_client, "Groq", self.groq_model),
            "openrouter": (self._openrouter_client, "OpenRouter", self.openrouter_model),
            "deepseek": (self._deepseek_client, "DeepSeek", self.deepseek_model),
            "pollinations": (True, "Pollinations", self.keyless_model),
        }
        for prov in prio_list:
            if prov in mapping:
                client, name, model = mapping[prov]
                if client is not None:
                    return (f"Auto (#{prov.upper()})", model)
        return ("Pollinations (Kostenlos)", self.keyless_model)

    @property
    def mode(self) -> AIMode:
        return self._mode

    def set_mode(self, mode: AIMode) -> None:
        self._mode = mode
        set_online_mode(mode == "online")

    def _stream_gemini(
        self,
        user_text: str,
        image_bytes: Optional[bytes],
        system_prompt: str,
        use_history: bool,
    ) -> Iterator[str]:
        assert self._gemini_client is not None
        contents: list[types.Content] = []
        if use_history:
            for role, text in self.get_sanitized_history(assistant_role="model"):
                contents.append(types.Content(role=role, parts=[types.Part.from_text(text=text)]))

        current_parts: list[types.Part] = []
        if image_bytes is not None:
            if isinstance(image_bytes, (list, tuple)):
                for img_item in list(image_bytes)[-2:]:
                    if img_item:
                        current_parts.append(types.Part.from_bytes(data=img_item, mime_type="image/jpeg"))
            else:
                current_parts.append(types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"))
        current_parts.append(types.Part.from_text(text=user_text))
        contents.append(types.Content(role="user", parts=current_parts))

        config = types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=0.4 if not use_history else 0.6,
        )
        candidates = _build_gemini_model_candidates(self.gemini_model, self._working_gemini_model)
        last_exc: Optional[Exception] = None
        for cand in candidates:
            emitted = False
            try:
                for chunk in self._gemini_client.models.generate_content_stream(
                    model=cand,
                    contents=contents,
                    config=config,
                ):
                    piece = getattr(chunk, "text", None) or ""
                    if piece:
                        if not emitted:
                            self._working_gemini_model = cand
                        emitted = True
                        yield piece
                if emitted:
                    return
                last_exc = RuntimeError(f"Leere Antwort von Gemini ({cand}) erhalten.")
            except Exception as exc:
                if emitted:
                    raise
                last_exc = exc
                err_l = str(exc).lower()
                if "401" in err_l or "api_key_invalid" in err_l or "invalid api key" in err_l:
                    break
        if last_exc is not None:
            raise RuntimeError(_classify_api_error(last_exc)) from last_exc
        raise RuntimeError("Leere Antwort von Gemini erhalten.")

    def _stream_groq(
        self,
        user_text: str,
        image_bytes: Optional[bytes],
        system_prompt: str,
        use_history: bool,
    ) -> Iterator[str]:
        assert self._groq_client is not None
        candidates = _build_groq_model_candidates(self.groq_model, self._working_groq_model)
        if image_bytes is not None:
            vision_first = [c for c in candidates if _is_vision_capable_groq(c)]
            non_vision = [c for c in candidates if not _is_vision_capable_groq(c)]
            for v_mod in (
                "meta-llama/llama-4-scout-17b-16e-instruct",
                "meta-llama/llama-4-maverick-17b-128e-instruct",
            ):
                if v_mod not in vision_first:
                    vision_first.append(v_mod)
            candidates = vision_first + non_vision
        last_exc: Optional[Exception] = None
        for cand in candidates:
            emitted = False
            effective_img = image_bytes if _is_vision_capable_groq(cand) else None
            try:
                in_think = False
                think_buf = ""
                for piece in self._stream_openai_compatible(
                    self._groq_client,  # type: ignore[arg-type]
                    cand,
                    user_text,
                    effective_img,
                    system_prompt,
                    use_history,
                ):
                    if not piece:
                        continue
                    think_buf += piece
                    if "<think>" in think_buf and "</think>" not in think_buf:
                        in_think = True
                        continue
                    if in_think and "</think>" in think_buf:
                        think_buf = _strip_reasoning_tags(think_buf)
                        in_think = False
                    if not in_think and think_buf:
                        cleaned_piece = _strip_reasoning_tags(think_buf)
                        think_buf = ""
                        if cleaned_piece:
                            if not emitted:
                                self._working_groq_model = cand
                            emitted = True
                            yield cleaned_piece
                if emitted:
                    return
                last_exc = RuntimeError(f"Leere Antwort von Groq ({cand}) erhalten.")
            except Exception as exc:
                if emitted:
                    raise
                last_exc = exc
                err_l = str(exc).lower()
                if "401" in err_l or "invalid_api_key" in err_l or "invalid api key" in err_l:
                    break
        if last_exc is not None:
            raise RuntimeError(_classify_api_error(last_exc)) from last_exc
        raise RuntimeError("Leere Antwort von Groq erhalten.")

    def _stream_openai_compatible(
        self,
        client: OpenAI,
        model: str,
        user_text: str,
        image_bytes: Optional[bytes],
        system_prompt: str,
        use_history: bool,
    ) -> Iterator[str]:
        messages: list[dict[str, object]] = [
            {"role": "system", "content": system_prompt},
        ]
        if use_history:
            for oai_role, text in self.get_sanitized_history(assistant_role="assistant"):
                messages.append({"role": oai_role, "content": text})

        if image_bytes is not None:
            imgs = list(image_bytes)[-2:] if isinstance(image_bytes, (list, tuple)) else [image_bytes]
            img_parts = [
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64.b64encode(im).decode('ascii')}"}}
                for im in imgs
                if im
            ]
            user_content: object = [{"type": "text", "text": user_text}, *img_parts]
        else:
            user_content = user_text

        messages.append({"role": "user", "content": user_content})
        emitted = False
        try:
            stream = client.chat.completions.create(
                model=model,
                messages=messages,  # type: ignore[arg-type]
                temperature=0.4 if not use_history else 0.6,
                stream=True,
            )
            for chunk in stream:
                choices = getattr(chunk, "choices", None)
                if not choices:
                    continue
                delta = getattr(choices[0].delta, "content", None) or ""
                if delta:
                    emitted = True
                    yield delta
        except Exception:
            if emitted:
                raise
            # Fallback auf Non-Streaming für OpenAI-kompatible Endpunkte ohne sauberes SSE
            resp = client.chat.completions.create(
                model=model,
                messages=messages,  # type: ignore[arg-type]
                temperature=0.4 if not use_history else 0.6,
                stream=False,
            )
            content = str((resp.choices[0].message.content if resp.choices else "") or "").strip()
            if content:
                emitted = True
                yield content

        if not emitted:
            raise RuntimeError(f"Leere Antwort von Modell {model} erhalten.")

    def _stream_pollinations(
        self,
        user_text: str,
        image_bytes: Optional[bytes],
        system_prompt: str,
        use_history: bool,
    ) -> Iterator[str]:
        """Direkter HTTP-POST/GET auf https://text.pollinations.ai/ ohne kaputte OpenAI-JSON-Wrapper."""
        del image_bytes  # Reiner Text-Endpunkt; Bildfragen werden in _build_cascade an Vision-Modelle geroutet
        messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
        if use_history:
            for role, text in self.get_sanitized_history(assistant_role="assistant"):
                messages.append({"role": role, "content": text})
        messages.append({"role": "user", "content": user_text})

        timeout = min(14.0, float(self.timeout_seconds))
        model_name = self.keyless_model or "openai"
        text_answer = ""

        # Versuch 1: Direkter JSON-POST an https://text.pollinations.ai/ -> liefert reinen Text zurück
        post_err: Optional[Exception] = None
        try:
            body = json.dumps({"messages": messages, "model": model_name}).encode("utf-8")
            req = urllib.request.Request(
                KEYLESS_ONLINE_BASE_URL,
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "text/plain, */*",
                    "User-Agent": "Jarvis-Desktop/2.0",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace").strip()
            low = raw.lower()
            if raw and not low.startswith("<!doctype") and not low.startswith("<html"):
                text_answer = raw
        except Exception as exc:
            post_err = exc

        # Versuch 2: Direkter GET-Fallback auf https://text.pollinations.ai/<prompt>?system=...
        if not text_answer:
            encoded_prompt = urllib.parse.quote(user_text, safe="")
            encoded_sys = urllib.parse.quote(system_prompt[:600], safe="")
            get_url = (
                f"{KEYLESS_ONLINE_BASE_URL}{encoded_prompt}"
                f"?model={urllib.parse.quote(model_name, safe='')}&system={encoded_sys}"
            )
            try:
                req_get = urllib.request.Request(
                    get_url,
                    headers={"Accept": "text/plain, */*", "User-Agent": "Jarvis-Desktop/2.0"},
                    method="GET",
                )
                with urllib.request.urlopen(req_get, timeout=timeout) as resp:
                    raw = resp.read().decode("utf-8", errors="replace").strip()
                low = raw.lower()
                if not raw or low.startswith("<!doctype") or low.startswith("<html"):
                    raise RuntimeError("Pollinations lieferte keine gültige Textantwort.")
                text_answer = raw
            except Exception as get_exc:
                cause = f"POST={post_err}; GET={get_exc}" if post_err else str(get_exc)
                raise RuntimeError(f"Pollinations Netzwerkfehler ({cause})") from get_exc

        # Token-/Wortweise ausgeben für flüssiges TTS-Streaming
        for chunk in text_answer.split(" "):
            if chunk:
                yield chunk + " "

    def _resolve_ollama_model(
        self,
        base_url: str,
        preferred: str,
        is_vision: bool,
    ) -> tuple[Optional[str], bool]:
        """Prüft /api/tags, lädt fehlendes Zielmodell im Hintergrund und wählt ggf. besten Fallback."""
        installed = _fetch_ollama_models(base_url)
        matched = _match_installed_model(installed, preferred)
        if matched:
            return matched, False

        if not is_vision:
            pull_target = preferred or default_ollama_text_model()
            pull_started = _start_ollama_pull_background(pull_target)
            fallback = _select_fallback_text_model(installed)
            if fallback:
                self._emit_log(
                    f"Ollama: '{pull_target}' wird im Hintergrund geladen – nutze vorübergehend '{fallback}'."
                )
                return fallback, False
            return None, pull_started

        return preferred, False

    def _collect_ollama_chat(
        self,
        base_url: str,
        model: str,
        messages: list[dict[str, object]],
        use_history: bool,
    ) -> str:
        return "".join(self._stream_ollama_chat(base_url, model, messages, use_history)).strip()

    def _stream_ollama_text_only(
        self,
        base_url: str,
        model: str,
        messages: list[dict[str, object]],
        use_history: bool,
        is_vision: bool = False,
    ) -> Iterator[str]:
        emitted = False
        try:
            for token in self._stream_ollama_chat(base_url, model, messages, use_history):
                emitted = True
                yield token
        except urllib.error.HTTPError as exc:
            if exc.code != 404 or emitted:
                raise
            alt_model, pulling = self._resolve_ollama_model(base_url, model, is_vision=is_vision)
            if pulling or not alt_model:
                target_name = self.ollama_text_model or default_ollama_text_model()
                yield (
                    f"Das lokale Sprachmodell {target_name} wird gerade noch heruntergeladen. "
                    "Gib mir bitte kurz einen Moment und frag mich gleich noch einmal."
                )
                return
            if alt_model == model:
                raise
            self._active_ollama_model = alt_model
            for token in self._stream_ollama_chat(base_url, alt_model, messages, use_history):
                yield token
        except (urllib.error.URLError, TimeoutError, OSError):
            if emitted:
                raise
            if not _ensure_ollama_running(base_url):
                raise
            for token in self._stream_ollama_chat(base_url, model, messages, use_history):
                yield token

    def _stream_ollama(
        self,
        user_text: str,
        image_bytes: Optional[bytes],
        system_prompt: str,
        use_history: bool,
    ) -> Iterator[str]:
        base_url = _normalize_ollama_base_url(self.ollama_host)
        if not _ensure_ollama_running(base_url):
            raise RuntimeError(f"Lokaler Ollama-Dienst unter {base_url} nicht erreichbar.")

        # 2-Stufen-Vision-Pipeline (strikte Deutsch-Pflicht: z. B. Moondream -> deutsches Hauptmodell)
        if image_bytes is not None:
            latest_img = (
                list(image_bytes)[-1]
                if isinstance(image_bytes, (list, tuple)) and image_bytes
                else image_bytes
            )
            vision_model = self.ollama_vision_model
            vision_messages: list[dict[str, object]] = [
                {
                    "role": "user",
                    "content": (
                        "Describe everything visible in this webcam image accurately and concretely: "
                        "people, faces, posture, objects on the desk, monitors, cables, tools, text, colors, and actions."
                    ),
                    "images": [base64.b64encode(latest_img).decode("ascii")],
                }
            ]
            raw_vision_desc = self._collect_ollama_chat(base_url, vision_model, vision_messages, False)

            text_model, pull_started = self._resolve_ollama_model(base_url, self.ollama_text_model, is_vision=False)
            if pull_started or not text_model:
                yield (
                    f"Ich lade gerade noch das deutsche Sprachmodell {self.ollama_text_model} herunter, "
                    "damit ich dir die Kameraaufnahme auf sauberem Deutsch beschreiben kann."
                )
                return

            self._active_ollama_model = f"{vision_model} -> {text_model}"
            refined_prompt = (
                f"Live-Beobachtung aus der Webcam (Rohbeschreibung):\n{raw_vision_desc}\n\n"
                f"Beantworte jetzt die Frage des Nutzers direkt in flüssigem, natürlichem Deutsch "
                f"(zum Beispiel 'Ich sehe ...'), ohne englische Rohsätze zu übernehmen:\n{user_text}"
            )
            messages: list[dict[str, object]] = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": refined_prompt},
            ]
            yield from self._stream_ollama_text_only(base_url, text_model, messages, False, is_vision=False)
            return

        model, pull_started = self._resolve_ollama_model(base_url, self.ollama_text_model, is_vision=False)
        if pull_started or not model:
            target_name = self.ollama_text_model or default_ollama_text_model()
            yield (
                f"Das lokale Sprachmodell {target_name} ist noch nicht fertig installiert und lädt gerade im Hintergrund. "
                "Frag mich einfach in einem kurzen Moment noch einmal."
            )
            return

        self._active_ollama_model = model
        messages = [{"role": "system", "content": system_prompt}]
        if use_history:
            for ollama_role, text in self.get_sanitized_history(assistant_role="assistant"):
                messages.append({"role": ollama_role, "content": text})
        messages.append({"role": "user", "content": user_text})

        yield from self._stream_ollama_text_only(base_url, model, messages, use_history, is_vision=False)

    def _build_cascade(self, has_image: bool) -> list[_ProviderStep]:
        steps: list[_ProviderStep] = []
        active_provider = get_active_provider()

        if self._mode == "offline" or not get_is_online() or active_provider == "ollama":
            ollama_model = self.ollama_vision_model if has_image else self.ollama_text_model
            return [_ProviderStep("Ollama (Lokal)", ollama_model, True, self._stream_ollama)]

        available_steps: dict[str, _ProviderStep] = {}

        if self._gemini_client is not None:
            available_steps["gemini"] = _ProviderStep(
                "Google Gemini",
                self._working_gemini_model or self.gemini_model,
                True,
                self._stream_gemini,
            )

        if self._groq_client is not None:
            available_steps["groq"] = _ProviderStep(
                "Groq",
                self._working_groq_model or self.groq_model,
                True,
                self._stream_groq,
            )

        if self._openrouter_client is not None:
            openrouter_client = self._openrouter_client
            available_steps["openrouter"] = _ProviderStep(
                "OpenRouter",
                self.openrouter_model,
                True,
                lambda text, img, sys_p, hist, c=openrouter_client, m=self.openrouter_model: (
                    self._stream_openai_compatible(c, m, text, img, sys_p, hist)
                ),
            )

        if self._deepseek_client is not None:
            deepseek_client = self._deepseek_client
            available_steps["deepseek"] = _ProviderStep(
                "DeepSeek",
                self.deepseek_model,
                False,
                lambda text, img, sys_p, hist, c=deepseek_client, m=self.deepseek_model: (
                    self._stream_openai_compatible(c, m, text, img, sys_p, hist)
                ),
            )

        available_steps["pollinations"] = _ProviderStep(
            "Pollinations (Kostenlos)",
            self.keyless_model,
            False,
            self._stream_pollinations,
        )

        if active_provider != "auto":
            if active_provider in available_steps:
                steps.append(available_steps[active_provider])
            else:
                self._emit_log(f"Anbieter '{active_provider}' nicht verfügbar – nutze Prio-Kaskade.")
                prio_list = get_model_priority()
                for prov in prio_list:
                    if prov in available_steps:
                        steps.append(available_steps[prov])
        else:
            prio_list = get_model_priority()
            for prov in prio_list:
                if prov in available_steps:
                    steps.append(available_steps[prov])

        ollama_model = self.ollama_vision_model if has_image else self.ollama_text_model
        steps.append(_ProviderStep("Ollama (Lokal)", ollama_model, True, self._stream_ollama))

        if has_image:
            vision_steps = [step for step in steps if step.supports_vision]
            if vision_steps:
                return vision_steps
            return [_ProviderStep("Ollama (Lokal)", self.ollama_vision_model, True, self._stream_ollama)]
        return steps

    def get_sanitized_history(
        self,
        max_messages: int = 10,
        assistant_role: str = "model",
    ) -> list[tuple[str, str]]:
        """
        Bereinigt den Gesprächsverlauf vor jedem API-Call (gegen HTTP 400 Bad Request):
        - Verwirft None / leere Strings / defekte Einträge
        - Erzwingt streng abwechselnde Rollen (user -> model/assistant -> user -> ...)
        - Begrenzt auf maximal `max_messages` (Standard: 10) und endet immer auf Assistant/Model,
          damit der nachfolgende aktuelle User-Prompt nie auf einen weiteren User-Turn trifft.
        """
        raw_items = list(self._history)
        cleaned: list[tuple[str, str]] = []
        target_assistant = "assistant" if assistant_role == "assistant" else "model"

        for entry in raw_items:
            if not isinstance(entry, (tuple, list)) or len(entry) < 2:
                continue
            raw_role, raw_text = entry[0], entry[1]
            if raw_text is None:
                continue
            text = str(raw_text).strip()
            if not text:
                continue
            role_low = str(raw_role or "").strip().lower()
            norm_role = target_assistant if role_low in ("model", "assistant", "jarvis") else "user"
            if cleaned and cleaned[-1][0] == norm_role:
                prev_role, prev_text = cleaned[-1]
                cleaned[-1] = (prev_role, f"{prev_text}\n{text}")
            else:
                cleaned.append((norm_role, text))

        if max_messages > 0 and len(cleaned) > max_messages:
            cleaned = cleaned[-max_messages:]
        while cleaned and cleaned[0][0] != "user":
            cleaned.pop(0)
        while cleaned and cleaned[-1][0] == "user":
            cleaned.pop()
        return cleaned

    def _remember_turn(self, user_text: str, answer: str) -> None:
        u_clean = str(user_text or "").strip() if user_text is not None else ""
        a_clean = str(answer or "").strip() if answer is not None else ""
        if not u_clean or not a_clean:
            return
        self._history.append(("user", u_clean))
        self._history.append(("model", a_clean))

    def update_passive_screen_context(self, jpeg_bytes: bytes, timestamp: Optional[float] = None) -> None:
        """Speichert den aktuellen 30s-Desktop-Snapshot im RAM für spontane Bildschirm-Rückfragen."""
        if not jpeg_bytes:
            return
        with self._passive_screen_lock:
            self._passive_screen_bytes = bytes(jpeg_bytes)
            self._passive_screen_at = float(timestamp if timestamp is not None else time.monotonic())

    def get_passive_screen_context(self, max_age_seconds: float = 90.0) -> Optional[bytes]:
        """Liefert den im RAM vorgehaltenen 30s-Desktop-Snapshot (sofern aktuell)."""
        with self._passive_screen_lock:
            if not self._passive_screen_bytes:
                return None
            if max_age_seconds > 0 and self._passive_screen_at > 0:
                if (time.monotonic() - self._passive_screen_at) > max_age_seconds:
                    return None
            return self._passive_screen_bytes

    def _resolve_system_prompt(self, system_prompt: Optional[str]) -> str:
        if system_prompt is not None:
            return system_prompt
        prompt = JARVIS_SYSTEM_PROMPT
        if self.memory is not None:
            mem_block = self.memory.get_context_block()
            if mem_block:
                prompt = f"{prompt}\n\nLangzeitgedächtnis:\n{mem_block}"
        return prompt

    def _resolve_step_active_model(self, step: _ProviderStep) -> str:
        if step.name == "Ollama (Lokal)":
            return self._active_ollama_model
        if step.name == "Google Gemini":
            return self._working_gemini_model or self.gemini_model
        if step.name == "Groq":
            return self._working_groq_model or self.groq_model
        return step.model

    def _resolve_desktop_image_for_turn(
        self,
        clean_user_text: str,
        system_prompt: Optional[str],
        use_history: bool,
        include_desktop_vision: Optional[bool],
    ) -> Optional[bytes]:
        """Liefert bei normalen Befehlen automatisch den Live-Desktop-Screenshot aus DesktopVisionStream."""
        if include_desktop_vision is False:
            return None
        low_text = (clean_user_text or "").lower()
        low_sys = (system_prompt or "").lower()
        if "ohne bild" in low_text or "text-only" in low_sys:
            return None
        is_utility_prompt = (not use_history) or (
            "router" in low_sys or "json" in low_sys or "klassifiziere" in low_sys
        )
        explicit_screen_cue = False
        try:
            from vision import should_capture_screen

            explicit_screen_cue = should_capture_screen(clean_user_text)
        except Exception:
            pass

        if include_desktop_vision is not True and is_utility_prompt and not explicit_screen_cue:
            return None

        try:
            from agent_hand import DESKTOP_VISION_STREAM, VISUAL_HAND_AGENT

            DESKTOP_VISION_STREAM.boost_active_rate(10.0)
            has_vision_provider = any(step.supports_vision for step in self._build_cascade(has_image=False))
            if not has_vision_provider and not explicit_screen_cue and include_desktop_vision is not True:
                return None
            screen_ctx = DESKTOP_VISION_STREAM.get_latest_screen_bytes(
                max_age_seconds=5.0,
                capture_if_missing=True,
            )
            if not screen_ctx:
                screen_ctx = self.get_passive_screen_context(max_age_seconds=20.0)
            if not screen_ctx:
                screen_ctx = VISUAL_HAND_AGENT.get_latest_passive_screen_bytes(
                    max_age_seconds=20.0,
                    capture_if_missing=True,
                )
            return screen_ctx or None
        except Exception:
            return None

    def ask_stream(
        self,
        user_text: str,
        image_bytes: Optional[bytes] = None,
        system_prompt: Optional[str] = None,
        use_history: bool = True,
        on_provider: Optional[Callable[[str, str], None]] = None,
        on_token: Optional[Callable[[str], None]] = None,
        use_memory: Optional[bool] = None,
        include_desktop_vision: Optional[bool] = None,
        **extra_kwargs: Any,
    ) -> BrainResponse:
        """Streamt die Antwort wortweise durch die KI-Kaskade bis zum ersten Erfolg."""
        clean_user_text = str(user_text or "").strip() if user_text is not None else ""
        if not clean_user_text:
            fallback_msg = "Ich habe leider keine Eingabe erhalten, Chef."
            if on_token is not None:
                on_token(fallback_msg)
            return BrainResponse(
                text=fallback_msg,
                provider="System",
                model="-",
                already_streamed=bool(on_token is not None),
            )

        self.is_busy = True
        try:
            bypass_show = bool(extra_kwargs.get("bypass_browser_show", False))
            if should_force_browser_show(
                clean_user_text,
                system_prompt=system_prompt,
                use_history=use_history,
                image_bytes=image_bytes,
                bypass_browser_show=bypass_show,
            ):
                _BROWSER_SHOW_GUARD.active = True
                try:
                    from agent_hand import VISUAL_HAND_AGENT

                    if on_provider is not None:
                        on_provider("1-FPS-Browser-Show", "Visible-Browser-Operator")
                    vis_res = VISUAL_HAND_AGENT.visual_browser_search_and_read(
                        query=clean_user_text,
                        brain=self,
                        interactive_click=True,
                    )
                    answer = str(
                        vis_res.get("summary")
                        or vis_res.get("message")
                        or "Ich habe die Anfrage sichtbar im Desktop-Webbrowser geöffnet."
                    ).strip()
                    try:
                        from executive_assistant import EXECUTIVE_ASSISTANT

                        EXECUTIVE_ASSISTANT.suggestions.evaluate_context(
                            event_hint=f"Browser-Recherche fertig: {clean_user_text}",
                            screen_summary=answer,
                        )
                    except Exception:
                        pass
                    if on_token is not None:
                        on_token(answer)
                    if use_history and answer:
                        self._remember_turn(clean_user_text, answer)
                    return BrainResponse(
                        text=answer,
                        provider="1-FPS-Browser-Show",
                        model="Visible-Browser-Operator",
                        already_streamed=bool(on_token is not None),
                    )
                except Exception as vis_exc:
                    self._emit_log(f"Browser-Show-Operator Hinweis: {vis_exc}")
                finally:
                    _BROWSER_SHOW_GUARD.active = False

            if use_memory is False:
                use_history = False
            if image_bytes is None:
                image_bytes = self._resolve_desktop_image_for_turn(
                    clean_user_text=clean_user_text,
                    system_prompt=system_prompt,
                    use_history=use_history,
                    include_desktop_vision=include_desktop_vision,
                )
            prompt = self._resolve_system_prompt(system_prompt)
            cascade = self._build_cascade(has_image=image_bytes is not None)

            for idx, step in enumerate(cascade):
                chunks: list[str] = []
                provider_notified = False
                try:
                    for token in step.stream_call(clean_user_text, image_bytes, prompt, use_history):
                        if not token:
                            continue
                        active_model = self._resolve_step_active_model(step)
                        if not provider_notified:
                            provider_notified = True
                            if on_provider is not None:
                                on_provider(step.name, active_model)
                        chunks.append(token)
                        if on_token is not None:
                            on_token(token)

                    answer = "".join(chunks).strip()
                    if not answer:
                        raise RuntimeError(f"Leere Antwort von {step.name}.")
                    final_model = self._resolve_step_active_model(step)
                    if use_history:
                        self._remember_turn(clean_user_text, answer)
                    return BrainResponse(
                        text=answer,
                        provider=step.name,
                        model=final_model,
                        already_streamed=bool(on_token is not None and chunks),
                    )
                except Exception as exc:
                    if chunks:
                        # Bereits begonnener Stream wird sauber abgeschlossen statt hart verworfen
                        partial = "".join(chunks).strip()
                        if use_history and partial:
                            self._remember_turn(clean_user_text, partial)
                        return BrainResponse(
                            text=partial,
                            provider=step.name,
                            model=step.model,
                            already_streamed=bool(on_token is not None),
                        )
                    if idx + 1 < len(cascade):
                        next_step = cascade[idx + 1]
                        self._emit_log(f"{step.name} Fehler ({exc}) – Fallback auf {next_step.name}.")
                    else:
                        self._emit_log(f"{step.name} Fehler: {exc}")

            if on_token is not None:
                on_token(OFFLINE_UNAVAILABLE_MESSAGE)
            return BrainResponse(
                text=OFFLINE_UNAVAILABLE_MESSAGE,
                provider="Kein Anbieter",
                model="-",
                already_streamed=bool(on_token is not None),
            )
        finally:
            self.is_busy = False

    def ask(
        self,
        user_text: str,
        image_bytes: Optional[bytes] = None,
        system_prompt: Optional[str] = None,
        use_history: bool = True,
        use_memory: Optional[bool] = None,
        **extra_kwargs: Any,
    ) -> BrainResponse:
        """Sendet eine vollständige Anfrage durch die aktive KI-Kaskade."""
        if use_memory is False:
            use_history = False
        return self.ask_stream(
            user_text=user_text,
            image_bytes=image_bytes,
            system_prompt=system_prompt,
            use_history=use_history,
        )

    def extract_browser_intent(self, user_text: str) -> dict[str, str]:
        """Extrahiert vor dem Browser-Start ein sauberes JSON-Aktionspaket (target_url, clean_search_term, final_goal)."""
        return extract_browser_intent(user_text, brain=self)

    def extract_app_and_subtask_intent(self, user_text: str) -> dict[str, Any]:
        """Extrahiert aus komplexen Desktop-/App-Befehlen Ziel-App, Desktop-Hinweis, Doppelklick-Bedarf und Folgeaktion."""
        return extract_app_and_subtask_intent(user_text, brain=self)

    def check_missing_app_proposal(
        self,
        user_text: str,
        explicit_app: str = "",
    ) -> Optional[dict[str, Any]]:
        """Prüft per `APP_INDEXER`, ob eine benötigte App fehlt, und bereitet `pending_install_task` vor."""
        return detect_missing_app_and_propose(user_text, explicit_app=explicit_app, brain=self)

    def resolve_pending_install(
        self,
        user_text: str,
        on_status: Optional[Callable[[str], None]] = None,
    ) -> Optional[dict[str, Any]]:
        """Reagiert auf Ja/Nein nach einer offenen Installations-Rückfrage."""
        return handle_pending_install_reply(user_text, brain=self, on_status=on_status)


def detect_missing_app_and_propose(
    user_text: str,
    explicit_app: str = "",
    brain: Any = None,
) -> Optional[dict[str, Any]]:
    """Delegiert Bedarfs-Erkennung & Pflicht-Rückfrage an `APP_INSTALLER_AGENT`."""
    try:
        from app_installer_agent import APP_INSTALLER_AGENT

        return APP_INSTALLER_AGENT.detect_missing_app_and_propose(
            user_goal=user_text,
            explicit_app=explicit_app,
            brain=brain,
        )
    except Exception:
        return None


def handle_pending_install_reply(
    user_text: str,
    brain: Any = None,
    on_status: Optional[Callable[[str], None]] = None,
) -> Optional[dict[str, Any]]:
    """Verarbeitet Nutzer-Bestätigung oder Abbruch einer offenen `pending_install_task`."""
    try:
        from app_installer_agent import APP_INSTALLER_AGENT
    except Exception:
        return None

    if APP_INSTALLER_AGENT.get_pending_proposal() is None:
        return None

    if APP_INSTALLER_AGENT.is_confirm(user_text):
        return APP_INSTALLER_AGENT.execute_pending_install(brain=brain, on_status=on_status)

    if APP_INSTALLER_AGENT.is_cancel(user_text):
        APP_INSTALLER_AGENT.clear_pending_proposal()
        return {
            "ok": True,
            "status": "cancelled",
            "message": "Verstanden, Chef. Ich habe die geplante App-Installation abgebrochen.",
        }
    return None


def extract_json_object(raw_text: str) -> Optional[dict[str, Any]]:
    """
    Kugelsicherer JSON-Objekt-Extraktor:
    - Entfernt Markdown-Codeblöcke (```json ... ```) sowie einleitenden/abschließenden Prosa-Text.
    - Extrahiert via `re.search(r"\\{.*\\}", ..., re.DOTALL)` sowie balancierter Klammer-Suche.
    - Toleriert überflüssige Trailing-Commas vor `}` oder `]`.
    """
    if not raw_text:
        return None
    cleaned = str(raw_text).strip()
    cleaned = re.sub(r"```(?:json)?", "", cleaned, flags=re.IGNORECASE).strip()

    candidates: list[str] = [cleaned]
    regex_match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    if regex_match:
        candidates.append(regex_match.group(0))

    # Auch einzelne balancierte {...}-Blöcke sammeln, falls die KI Text mit mehreren Klammern ausgibt
    decoder = json.JSONDecoder()
    for idx, ch in enumerate(cleaned):
        if ch == "{":
            try:
                obj, _ = decoder.raw_decode(cleaned[idx:])
                if isinstance(obj, dict):
                    return obj
            except Exception:
                pass

    for cand in candidates:
        for attempt in (cand, re.sub(r",\s*([}\]])", r"\1", cand)):
            try:
                parsed = json.loads(attempt)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                continue
    return None


def extract_app_and_subtask_intent(user_text: str, brain: Any = None) -> dict[str, Any]:
    """
    Zerlegt zusammengesetzte Desktop-/App-Aufträge wie
    'Geh auf den ArcCraft Launcher auf meinem Desktop, öffne ihn mit einem Doppelklick und öffne dann darin FilmCraft.'
    in ein sauberes Aktionspaket (`app_name`, `in_app_target`, `requires_desktop`, `requires_double_click`, `action_goal`).
    """
    raw = (user_text or "").strip()
    low = raw.lower()
    requires_desktop = any(
        tok in low for tok in ("desktop", "arbeitsfläche", "desktop-icon", "desktop icon", "verknüpfung auf dem desktop")
    )
    requires_double_click = requires_desktop or any(
        tok in low for tok in ("doppelklick", "double click", "double-click", "zweimal klicken")
    )

    # Deterministische Extraktion für App-Name & In-App-Unterziel
    app_name = ""
    in_app_target = ""

    # Muster: "... öffne ... darin <SubTarget>" / "... und klicke auf <SubTarget>"
    sub_m = re.search(
        r"\b(?:und\s+)?(?:öffne|oeffne|starte|klicke?\s+auf|gehe?\s+(?:auf|zu|in)|wähle)\s+(?:dann\s+)?(?:darin\s+|in\s+ihm\s+|in\s+der\s+app\s+|im\s+launcher\s+)?(?:auf\s+)?([a-zA-Z0-9äöüÄÖÜß._ -]{2,60}?)(?:\s*[.!?]|$)",
        raw,
        flags=re.IGNORECASE,
    )
    if sub_m:
        cand_sub = sub_m.group(1).strip(" .,!?\"'")
        if cand_sub.lower() not in ("ihn", "sie", "es", "das", "den", "die", "app", "launcher", "programm"):
            in_app_target = cand_sub

    # Primäre Ziel-App am Satzanfang / vor "auf meinem Desktop" / "mit einem Doppelklick" extrahieren
    head = re.split(r"\b(?:und\s+(?:öffne|oeffne|starte|klicke|gehe)|,\s*(?:öffne|oeffne|starte|und))\b", raw, maxsplit=1, flags=re.IGNORECASE)[0]
    head = re.sub(
        r"(?i)\b(?:hey\s+jarvis|jarvis|bitte|mal|kannst\s+du|geh\w*\s+(?:bitte\s+)?(?:auf|zu)|öffne|oeffne|starte|mach\w*)\b",
        " ",
        head,
    )
    head = re.sub(
        r"(?i)\b(?:auf\s+(?:meinem|dem)\s+desktop|vom\s+desktop|auf\s+desktop|desktop|mit\s+(?:einem\s+)?doppelklick|per\s+doppelklick|ihn|sie|es|den|die|das|ein|eine|einen)\b",
        " ",
        head,
    )
    head = re.sub(r"[?!.,:;\"']+", " ", head)
    app_name = re.sub(r"\s+", " ", head).strip()

    if brain is not None and hasattr(brain, "ask") and (not app_name or len(app_name.split()) > 5):
        try:
            resp = brain.ask(
                f'Befehl: "{raw}"',
                system_prompt=(
                    "Extrahiere aus dem deutschen Desktop-/App-Befehl exakt JSON mit:\n"
                    '{"app_name": "Name des Launchers/Programms", "in_app_target": "Unterpunkt oder leer", '
                    '"requires_desktop": true/false, "requires_double_click": true/false}.\n'
                    "Antworte AUSSCHLIESSLICH mit validem JSON ohne Markdown."
                ),
                use_history=False,
            )
            parsed = extract_json_object(str(getattr(resp, "text", resp) or ""))
            if parsed:
                llm_app = str(parsed.get("app_name") or "").strip()
                llm_sub = str(parsed.get("in_app_target") or "").strip()
                if llm_app and len(llm_app.split()) <= 5:
                    app_name = llm_app
                if llm_sub:
                    in_app_target = llm_sub
                if "requires_desktop" in parsed:
                    requires_desktop = bool(parsed["requires_desktop"]) or requires_desktop
                if "requires_double_click" in parsed:
                    requires_double_click = bool(parsed["requires_double_click"]) or requires_double_click
        except Exception:
            pass

    return {
        "app_name": app_name,
        "in_app_target": in_app_target,
        "requires_desktop": requires_desktop,
        "requires_double_click": requires_double_click,
        "action_goal": raw,
    }


_CATEGORY_URL_MAP: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (
        ("tradingview", "chartseite", "live-chart", "trading chart", "chartanalyse", "analysiere", "xauusd"),
        "https://de.tradingview.com/chart/",
        "TradingView Live-Chart",
    ),
    (
        (
            "goldpreis",
            "gold",
            "silberpreis",
            "silber",
            "ölpreis",
            "rohstoff",
            "aktienwebseite",
            "aktien-webseite",
            "aktien webseite",
            "aktienseite",
            "börsenseite",
            "boersenseite",
            "finanzseite",
            "aktienkurse",
            "finanzen.net",
            "aktie",
            "kurs",
            "nvidia",
            "apple",
            "tesla",
            "rheinmetall",
            "microsoft",
            "dax",
        ),
        "https://www.finanzen.net",
        "Finanzen.net Live-Kurse",
    ),
    (
        ("krypto", "bitcoin", "btc", "coinmarketcap", "ethereum", "eth"),
        "https://de.tradingview.com/chart/",
        "TradingView Krypto-Chart",
    ),
    (
        ("yahoo finance", "yahoo finanzen"),
        "https://de.finance.yahoo.com",
        "Yahoo Finanzen",
    ),
    (
        ("youtube", "musikvideo", "video"),
        "https://www.youtube.com",
        "YouTube",
    ),
    (
        ("amazon", "einkaufen", "bestellen"),
        "https://www.amazon.de",
        "Amazon Deutschland",
    ),
    (
        ("wikipedia", "lexikon", "enzyklopädie"),
        "https://de.wikipedia.org",
        "Wikipedia",
    ),
    (
        ("nachrichten", "news", "tagesschau"),
        "https://www.tagesschau.de",
        "Tagesschau Nachrichten",
    ),
    (
        ("wetter", "wetterbericht", "vorhersage"),
        "https://www.wetter.com",
        "Wetter.com",
    ),
)


def _fallback_extract_browser_intent(user_text: str) -> dict[str, str]:
    """Deterministischer Intent-Analyzer ohne stumpfes Satz-Kopieren."""
    raw = (user_text or "").strip()
    low = raw.lower()

    target_url = ""
    site_label = "Webseite"

    url_match = re.search(
        r"(https?://[^\s\"']+|[a-zA-Z0-9.-]+\.(?:de|com|org|net|io|ai|finance|me|tv|ch|at|eu)(?:/[^\s\"']*)?)",
        raw,
        flags=re.IGNORECASE,
    )
    if url_match:
        u = url_match.group(1).strip(" .,!?\"'")
        target_url = u if u.lower().startswith(("http://", "https://")) else f"https://{u}"
        site_label = target_url
    else:
        for keywords, mapped_url, label in _CATEGORY_URL_MAP:
            if any(k in low for k in keywords):
                target_url = mapped_url
                site_label = label
                break

    if not target_url:
        target_url = "https://www.google.de"
        site_label = "Google"

    # Reinen Suchbegriff extrahieren (niemals den ganzen gesprochenen Satz übernehmen)
    cleaned = raw
    cleaned = re.sub(
        r"(?i)\b(hey\s+jarvis|jarvis|bitte|mal|kannst\s+du|würdest\s+du|geh\w*\s+(?:bitte\s+)?(?:auf|in|zu)|öffne|oeffne|starte|zeig\w*|such\w*|recherchier\w*|find\w*|sag\s+mir|erklär\w*|was\s+(?:ist|sind|kostet|kosten)|wie\s+(?:sieht\s+es(?:\s+mit)?|steht|wird|ist|sind|hoch|viel)|wer\s+ist|im\s+webbrowser|im\s+browser|im\s+internet|im\s+web|auf\s+google|dort|da|nach\s+einer|nach\s+einem|nach\s+dem|nach\s+der|nach\s+den|nach|eine|einer|einem|aktuellen|aktuelle|aktueller|aktuell|aus)\b",
        " ",
        cleaned,
    )
    cleaned = re.sub(
        r"(?i)\b(aktienwebseite|aktien-webseite|aktien\s+webseite|aktienseite|börsenseite|finanzseite|webseite|website|seite|youtube|amazon|wikipedia|google|tradingview|finanzen\.net)\b",
        " ",
        cleaned,
    )
    cleaned = re.sub(r"\b(?:es|und|oder|mir|mich|auf|in|im|zu|für|mit|von|der|die|das|den|dem|des)\b", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"[?!.,:;\"']+", " ", cleaned)
    clean_search_term = re.sub(r"\s+", " ", cleaned).strip()

    # Falls der Nutzer nur "Such im Webbrowser nach einer Aktienwebseite" gesagt hat -> kein Extra-Suchbegriff nötig
    if len(clean_search_term) <= 1 or clean_search_term.lower() in ("web", "browser", "internet"):
        clean_search_term = ""

    if clean_search_term:
        final_goal = (
            f"Auf {target_url} ({site_label}) ist das Suchergebnis oder der Live-Chart für "
            f"'{clean_search_term}' geöffnet und klar auf dem Bildschirm sichtbar."
        )
    else:
        final_goal = (
            f"Die Zielwebseite {target_url} ({site_label}) ist im Vordergrund vollständig geladen, "
            f"ohne störende Cookie-Banner, und interaktiv auf dem Bildschirm sichtbar."
        )

    return {
        "target_url": target_url,
        "clean_search_term": clean_search_term,
        "final_goal": final_goal,
    }


def extract_browser_intent(user_text: str, brain: Any = None) -> dict[str, str]:
    """
    FIX 1: Intelligente Ziel- & Suchbegriff-Extraktion (Intent Analyzer vor dem Browser-Start).
    Extrahiert per LLM (mit deterministischem Fallback) ein sauberes JSON-Aktionspaket:
      - target_url
      - clean_search_term
      - final_goal
    """
    fallback = _fallback_extract_browser_intent(user_text)
    raw = (user_text or "").strip()
    if not raw or brain is None or not hasattr(brain, "ask"):
        return fallback

    sys_prompt = (
        "Du bist der Browser-Intent-Analyzer von J.A.R.V.I.S. "
        "Extrahiere aus dem gesprochenen Befehl des Nutzers ein sauberes JSON-Objekt mit exakt 3 Feldern:\n"
        '1. "target_url": Direkte URL der Zielseite oder passenden Kategorie-Webseite '
        '(z. B. "Aktienwebseite"/"Aktienseite" -> "https://www.finanzen.net", '
        '"TradingView"/"Chart" -> "https://de.tradingview.com/chart/", '
        '"YouTube" -> "https://www.youtube.com", "Amazon" -> "https://www.amazon.de", '
        '"Krypto" -> "https://coinmarketcap.com", sonst "https://www.google.de"). '
        'NIEMALS "https://www.google.com/search?q=<ganzer Satz>"!\n'
        '2. "clean_search_term": NUR der reine, kurze Suchbegriff für die Webseite '
        '(z. B. "Goldpreis", "Nvidia", "Lofi Beats") oder "" (leer), wenn nur die Webseite geöffnet werden soll. '
        'NIEMALS Füllwörter oder den ganzen gesprochenen Satz!\n'
        '3. "final_goal": Kurze Beschreibung, was am Ende sichtbar auf dem Monitor geöffnet sein muss.\n'
        "Antworte AUSSCHLIESSLICH mit validem JSON."
    )
    try:
        resp = brain.ask(
            f"Gesprochener Nutzerbefehl: \"{raw}\"",
            system_prompt=sys_prompt,
            use_history=False,
        )
        ans = str(getattr(resp, "text", resp) or "").strip()
        parsed = extract_json_object(ans)
        if parsed:
            if True:
                t_url = str(parsed.get("target_url") or "").strip()
                if t_url and not t_url.lower().startswith(("http://", "https://")):
                    t_url = f"https://{t_url}"
                # Blockiere stumpfe Google-Satz-URLs vom LLM
                if "google.com/search?q=" in t_url.lower() and len(t_url) > 45:
                    t_url = fallback["target_url"]
                c_term = str(parsed.get("clean_search_term") or "").strip()
                # Sicherheits-Check: Falls LLM den ganzen langen Satz zurückgibt, nimm Fallback
                if len(c_term.split()) > 5 and len(c_term) >= len(raw) * 0.7:
                    c_term = fallback["clean_search_term"]
                f_goal = str(parsed.get("final_goal") or "").strip() or fallback["final_goal"]
                return {
                    "target_url": t_url or fallback["target_url"],
                    "clean_search_term": c_term,
                    "final_goal": f_goal,
                }
    except Exception:
        pass
    return fallback
