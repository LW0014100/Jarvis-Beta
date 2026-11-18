"""J.A.R.V.I.S. Command Center HUD: FastAPI + WebSockets + HTML5/JS Dashboard über pywebview / --app-Browser."""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime
import io
import json
import os
from pathlib import Path
import platform
import queue
import secrets
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from typing import Any, Callable, Optional
import base64
import ipaddress
import urllib.request
import webbrowser


def enable_windows_high_dpi() -> None:
    """Aktiviert Per-Monitor High-DPI-Scaling unter Windows für gestochen scharfe Browser-App-Darstellung."""
    if platform.system() != "Windows":
        return
    try:
        import ctypes

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
            except Exception:
                ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def _compute_centered_window_geometry(width: int = 1280, height: int = 800) -> tuple[int, int, int, int]:
    """Berechnet die zentrierte Fensterposition (x, y, width, height) für den normalen Fenstermodus."""
    sw, sh = 1920, 1080
    if platform.system() == "Windows":
        try:
            import ctypes

            user32 = ctypes.windll.user32
            sw = max(800, int(user32.GetSystemMetrics(0)))
            sh = max(600, int(user32.GetSystemMetrics(1)))
        except Exception:
            pass
    w = min(width, max(820, sw - 60))
    h = min(height, max(600, sh - 80))
    x = max(20, (sw - w) // 2)
    y = max(20, (sh - h - 40) // 2)
    return x, y, w, h


def _get_jarvis_icon_path(ext: str = "ico") -> Optional[Path]:
    """Sucht das hochauflösende JARVIS Arc-Reactor App-Icon unter assets/jarvis_icon.(ico|png)."""
    fname = f"jarvis_icon.{ext.lstrip('.')}"
    for candidate in (
        get_base_dir() / "assets" / fname,
        Path(__file__).resolve().parent / "assets" / fname,
    ):
        if candidate.is_file():
            return candidate
    return None


def _create_jarvis_tray_icon_image() -> Any:
    """Lädt das hochauflösende JARVIS Arc-Reactor Icon (assets/jarvis_icon.png) für System-Tray & Fenster."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None

    icon_png = _get_jarvis_icon_path("png")
    if icon_png is not None:
        try:
            with Image.open(icon_png) as src:
                return src.convert("RGBA").resize((64, 64), Image.Resampling.LANCZOS)
        except Exception:
            pass

    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    # Dunkler Obsidian-Kreis
    draw.ellipse((2, 2, 62, 62), fill=(6, 11, 20, 255), outline=(0, 212, 255, 255), width=3)
    # Mittlerer Kupfer-/Gold-Ring
    draw.ellipse((12, 12, 52, 52), outline=(255, 145, 40, 255), width=3)
    # Leuchtender Arc-Kern
    draw.ellipse((22, 22, 42, 42), fill=(0, 235, 255, 255), outline=(255, 255, 255, 255), width=2)
    return img


enable_windows_high_dpi()

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

from config import (
    VISION_STATUS_PASSIVE,
    clean_api_key,
    get_active_provider,
    get_autostart_status,
    get_base_dir,
    get_is_online,
    get_model_priority,
    get_user_data_dir,
    get_web_ui_dir,
    load_settings,
    register_online_mode_listener,
    save_settings,
    set_active_provider,
    set_autostart,
    set_model_priority,
    set_online_mode,
    unregister_online_mode_listener,
)

DEFAULT_EDGE_VOICE = "de-DE-ConradNeural"
DEFAULT_EDGE_FALLBACK_VOICE = "de-DE-KillianNeural"
DEFAULT_EDGE_PITCH = "-15Hz"
DEFAULT_EDGE_RATE = "-5%"

_cv2_mod: Any = None
_cv2_loaded = False
_pil_image_cls: Any = None
_pil_loaded = False


def _get_cv2() -> Any:
    global _cv2_mod, _cv2_loaded
    if not _cv2_loaded:
        _cv2_loaded = True
        try:
            import cv2 as _cv2

            _cv2_mod = _cv2
        except ImportError:
            _cv2_mod = None
    return _cv2_mod


def _get_pil_image() -> Any:
    global _pil_image_cls, _pil_loaded
    if not _pil_loaded:
        _pil_loaded = True
        try:
            from PIL import Image as _img_cls

            _pil_image_cls = _img_cls
        except ImportError:
            _pil_image_cls = None
    return _pil_image_cls


def _get_ghost_overlay() -> Any:
    from ghost_cursor import GHOST_OVERLAY

    return GHOST_OVERLAY


def _get_vault() -> Any:
    from vault import SECURE_VAULT

    return SECURE_VAULT


def _get_mail_factory() -> Any:
    from mail_factory import MAIL_FACTORY

    return MAIL_FACTORY


def _get_trading_engine() -> Any:
    from trading_engine import TRADING_ENGINE

    return TRADING_ENGINE


def _get_autonomous_agent() -> Any:
    from autonomous_agent import AUTONOMOUS_AGENT

    return AUTONOMOUS_AGENT


def _get_nas_brain() -> Any:
    from nas_brain import NAS_BRAIN

    return NAS_BRAIN


def _get_swarm_core() -> Any:
    from swarm_core import SWARM_CORE

    return SWARM_CORE


def _is_private_or_local_ip(ip_str: str) -> bool:
    """Prüft, ob eine IP-Adresse aus dem Loopback, privaten LAN oder Link-Local-Bereich stammt."""
    if not ip_str:
        return False
    if ip_str in ("127.0.0.1", "::1", "localhost", "testclient"):
        return True
    try:
        ip = ipaddress.ip_address(ip_str)
        return ip.is_loopback or ip.is_private or ip.is_link_local
    except ValueError:
        return False


def _get_lan_ip() -> str:
    """Ermittelt die primäre lokale LAN-IP-Adresse dieses Rechners."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "127.0.0.1"


def _detect_pairing_host_type() -> str:
    """Erkennt, ob dieser Host ein Raspberry Pi oder ein Windows/Linux-PC ist."""
    try:
        from hardware_pi5 import is_raspberry_pi_5

        if is_raspberry_pi_5():
            return "pi"
    except Exception:
        pass
    mach = platform.machine().lower()
    if platform.system() == "Linux" and ("arm" in mach or "aarch64" in mach):
        return "pi"
    if platform.system() == "Windows":
        return "windows"
    return platform.system().lower() or "windows"


def _get_or_create_pairing_sync_token(settings: Optional[dict[str, str]] = None) -> str:
    """Liefert oder erzeugt einen stabilen JARVIS-Sync-Token für sichere Handy-Kopplung."""
    cfg = settings if settings is not None else load_settings()
    token = clean_api_key(cfg.get("JARVIS_SYNC_TOKEN") or os.getenv("JARVIS_SYNC_TOKEN") or "")
    if not token:
        token = f"JARVIS-SYNC-{secrets.token_hex(4).upper()}"
        try:
            save_settings({"JARVIS_SYNC_TOKEN": token})
        except Exception:
            os.environ["JARVIS_SYNC_TOKEN"] = token
    return token


def _generate_qr_png_b64(payload_text: str, box_size_px: int = 280) -> str:
    """Erzeugt aus beliebigem Text/JSON ein kontrastreiches QR-Code-PNG als Data-URI."""
    if not payload_text:
        return ""
    try:
        import cv2
        import numpy as np
        from PIL import Image

        encoder = cv2.QRCodeEncoder_create()
        matrix = encoder.encode(payload_text)
        # Quiet-Zone (4 Module weißer Rand rundherum für zuverlässiges Kamera-Scanning)
        padded = np.pad(matrix, ((4, 4), (4, 4)), mode="constant", constant_values=255)
        img = Image.fromarray(padded.astype("uint8"), mode="L").convert("RGB")
        target_dim = max(180, int(box_size_px))
        img = img.resize((target_dim, target_dim), Image.Resampling.NEAREST)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        return ""


STATUS_META: dict[str, tuple[str, str, str]] = {
    "Bereit": ("READY", "#38d7ff", "STANDBY"),
    "Höre zu": ("LISTENING", "#46a8ff", "VAD ACTIVE"),
    "Verarbeite Sprache": ("PROCESSING", "#ffb02e", "WHISPER STT"),
    "Kamera aktiv": ("OPTICS", "#ff8a3a", "VISION SCAN"),
    "Denke nach": ("THINKING", "#ffa624", "NEURAL STREAM"),
    "Steuere Bildschirm": ("ACTING", "#ff9830", "GUI AUTONOMY"),
    "Spreche": ("SPEAKING", "#26ee9e", "AUDIO STREAM"),
    "Fehler": ("ALERT", "#ff3e4a", "ATTENTION"),
}


def _pick_free_port(preferred: int = 8765) -> int:
    for port in (preferred, 0):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", port))
                return int(s.getsockname()[1])
        except OSError:
            continue
    return preferred


def _find_app_browser() -> Optional[str]:
    """Sucht Edge, Chrome, Brave, Chromium oder Playwright-Chromium für den --app-Modus."""
    candidates: list[Optional[str]] = []
    if platform.system() == "Windows":
        candidates.extend(
            [
                shutil.which("msedge"),
                shutil.which("chrome"),
                shutil.which("vivaldi"),
                shutil.which("brave"),
                os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
                os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
                os.path.expandvars(r"%LocalAppData%\Microsoft\Edge\Application\msedge.exe"),
                os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
                os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
                os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
                os.path.expandvars(r"%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe"),
                os.path.expandvars(r"%LocalAppData%\BraveSoftware\Brave-Browser\Application\brave.exe"),
                os.path.expandvars(r"%ProgramFiles%\Vivaldi\Application\vivaldi.exe"),
                os.path.expandvars(r"%ProgramFiles(x86)%\Vivaldi\Application\vivaldi.exe"),
                os.path.expandvars(r"%LocalAppData%\Vivaldi\Application\vivaldi.exe"),
                os.path.expandvars(r"%LocalAppData%\Programs\Opera\opera.exe"),
            ]
        )
        # Registry App Paths prüfen
        try:
            import winreg

            for root_hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                for exe_name in ("msedge.exe", "chrome.exe", "brave.exe", "vivaldi.exe"):
                    try:
                        with winreg.OpenKey(
                            root_hive,
                            rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe_name}",
                        ) as k:
                            val, _ = winreg.QueryValueEx(k, "")
                            if val:
                                candidates.append(str(val).strip('"'))
                    except OSError:
                        continue
        except Exception:
            pass
        # Windows EdgeCore fallback (System-Chromium ohne Playwright/WebView2-Wrapper)
        edge_core = Path(os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\EdgeCore"))
        if edge_core.is_dir():
            for exe in sorted(edge_core.glob("*/msedge.exe"), reverse=True):
                candidates.append(str(exe))
    else:
        candidates.extend(
            [
                shutil.which("chromium"),
                shutil.which("chromium-browser"),
                shutil.which("google-chrome"),
                shutil.which("brave-browser"),
                shutil.which("microsoft-edge"),
            ]
        )

    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return None


def launch_browser_app_window(
    url: str,
    *,
    width: int = 1280,
    height: int = 800,
    x: Optional[int] = None,
    y: Optional[int] = None,
    profile_name: str = "jarvis_hud_browser_profile",
) -> Optional[subprocess.Popen[Any]]:
    """Öffnet eine lokale Web-App im App-Modus (--app=) des System-Browsers ohne Electron/WebView2."""
    browser_exe = _find_app_browser()
    if browser_exe is None:
        try:
            webbrowser.open(url, new=1)
        except Exception:
            pass
        return None

    pos_x, pos_y, win_w, win_h = _compute_centered_window_geometry(width, height)
    if x is not None:
        pos_x = int(x)
    if y is not None:
        pos_y = int(y)
    profile_dir = Path(tempfile.gettempdir()) / profile_name
    profile_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        browser_exe,
        f"--app={url}",
        f"--user-data-dir={profile_dir}",
        f"--window-size={win_w},{win_h}",
        f"--window-position={pos_x},{pos_y}",
        "--new-window",
        "--no-first-run",
        "--no-default-browser-check",
        "--high-dpi-support=1",
        "--enable-gpu-rasterization",
        "--disable-background-timer-throttling",
        "--disable-renderer-backgrounding",
        "--disable-backgrounding-occluded-windows",
    ]
    if platform.system() == "Windows":
        cmd.append("--disable-features=msEdgeStartupBoost")
    elif platform.system() == "Linux":
        cmd.append("--disable-dev-shm-usage")
        if os.getenv("WAYLAND_DISPLAY"):
            cmd.append("--ozone-platform-hint=auto")

    popen_kwargs: dict[str, Any] = {}
    if platform.system() == "Windows":
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    return subprocess.Popen(cmd, **popen_kwargs)


def _encode_bgr_jpeg(frame_bgr: Any, quality: int = 78) -> Optional[bytes]:
    if frame_bgr is None:
        return None
    cv2_mod = _get_cv2()
    if cv2_mod is not None:
        ok, encoded = cv2_mod.imencode(".jpg", frame_bgr, [int(cv2_mod.IMWRITE_JPEG_QUALITY), quality])
        if ok:
            return encoded.tobytes()
    img_cls = _get_pil_image()
    if img_cls is not None:
        rgb = frame_bgr[:, :, ::-1]
        buf = io.BytesIO()
        img_cls.fromarray(rgb).save(buf, format="JPEG", quality=quality)
        return buf.getvalue()
    return None


_ACTIVE_GUI_INSTANCE: Optional["JarvisGUI"] = None


def get_active_gui() -> Optional["JarvisGUI"]:
    """Liefert die aktuell aktive JarvisGUI-Instanz (falls im HUD-Modus gestartet)."""
    return _ACTIVE_GUI_INSTANCE


class JarvisGUI:
    """Entkoppeltes FastAPI-/WebSocket-Web-HUD mit Systembrowser --app= Modus und Multi-Monitor-Support."""

    def __init__(
        self,
        fullscreen: bool = False,
        on_manual_trigger: Optional[Callable[[], None]] = None,
        on_camera_check: Optional[Callable[[], None]] = None,
        on_camera_toggle: Optional[Callable[[], bool]] = None,
        on_mic_mute_toggle: Optional[Callable[[], bool]] = None,
        on_quick_command: Optional[Callable[[str], None]] = None,
        frame_provider: Optional[Callable[[], Optional[object]]] = None,
        on_settings_saved: Optional[Callable[[dict[str, str]], None]] = None,
        on_mode_toggle: Optional[Callable[[bool], None]] = None,
        on_close: Optional[Callable[[], None]] = None,
        memory: Optional[Any] = None,
        on_mobile_ask: Optional[Callable[[str, Optional[bytes]], dict[str, Any] | str]] = None,
        tts_engine: Optional[Any] = None,
        on_server_startup: Optional[Callable[[], None]] = None,
    ) -> None:
        # Beim Start immer im normalen, frei beweglichen Fenstermodus öffnen (niemals Fullscreen)
        self._fullscreen = False
        self._window_minimized = False
        self._tray_icon: Any = None
        self._on_manual_trigger = on_manual_trigger
        self._on_camera_check = on_camera_check
        self._on_camera_toggle = on_camera_toggle
        self._on_mic_mute_toggle = on_mic_mute_toggle
        self._on_quick_command = on_quick_command
        self._frame_provider = frame_provider
        self._on_settings_saved = on_settings_saved
        self._on_mode_toggle = on_mode_toggle
        self._on_close = on_close
        self._memory = memory
        self._on_mobile_ask = on_mobile_ask
        self._tts_engine = tts_engine
        self._brain: Optional[Any] = None
        self._alarm_manager: Optional[Any] = None
        self._on_server_startup = on_server_startup

        self._stop_event = threading.Event()
        self._state_lock = threading.Lock()
        self._clients_lock = threading.Lock()
        self._ws_clients: set[tuple[asyncio.AbstractEventLoop, asyncio.Queue[str]]] = set()
        self._active_websockets: set[WebSocket] = set()
        self._ws_event_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=100)
        self._ws_broadcaster_task: Optional[asyncio.Task[Any]] = None
        self._live_state: dict[str, Any] = {
            "audio_level": 0.0,
            "level_pct": 0,
            "vad_active": False,
            "speaking_active": False,
            "status": "Bereit",
        }

        self._is_online = get_is_online()
        self._mode = "online" if self._is_online else "offline"
        self._status = "Bereit"
        self._model_label = "MODEL: INITIALIZING..."
        self._voice_label = (
            "VOICE: edge-tts (Conrad -15Hz)"
            if self._is_online
            else "VOICE: piper-tts (Karlsson Deep)"
        )
        self._response_time_s = 0.0
        self._user_text = 'Warte auf Sprachbefehl („Jarvis …“) …'
        self._jarvis_text = "Systeme werden initialisiert, Chef."
        self._camera_paused = True
        self._vision_status = VISION_STATUS_PASSIVE
        self._mic_muted = False
        self._latest_jpeg: Optional[bytes] = None
        self._audio_level = 0.0
        self._vad_active = False
        self._speaking_active = False
        self._seq = 1
        self._dialog_turn_id = 0
        self._current_jarvis_turn_id: Optional[int] = None
        self._dialog_history: deque[dict[str, Any]] = deque(maxlen=24)
        self._telemetry: dict[str, Any] = {
            "cpu_pct": 0.0,
            "ram_used_gb": 0.0,
            "ram_total_gb": 0.0,
            "ram_pct": 0.0,
            "temp_c": None,
            "disk_free_gb": 0.0,
            "disk_total_gb": 0.0,
            "network": {"hostname": "localhost", "ip": "127.0.0.1", "online": False},
        }
        self._activity_lines: deque[str] = deque(maxlen=30)
        self._brain_activity: dict[str, Any] = {
            "state": "idle",
            "intensity": 0.28,
            "active_monitor": None,
            "detail": "STANDBY",
            "seq": 1,
        }
        self._neural_thoughts: deque[dict[str, Any]] = deque(maxlen=60)
        try:
            from multi_monitor import get_connected_monitors

            self._monitors: list[dict[str, Any]] = get_connected_monitors()
        except Exception:
            self._monitors = []
        self._popout_procs: list[subprocess.Popen[Any]] = []
        self._ticker_cb = lambda ev: self._broadcast({"type": "telemetry_event", "event": ev})
        try:
            from macro_engine import EVENT_TICKER

            EVENT_TICKER.subscribe(self._ticker_cb)
        except Exception:
            pass
        global _ACTIVE_GUI_INSTANCE
        _ACTIVE_GUI_INSTANCE = self

        self._webview_window: Any = None
        self._browser_proc: Optional[subprocess.Popen] = None
        self._server: Optional[uvicorn.Server] = None
        self._server_thread: Optional[threading.Thread] = None

        preferred_port = int(os.getenv("JARVIS_HUD_PORT", "8000"))
        self.port = _pick_free_port(preferred_port)
        self.url = f"http://127.0.0.1:{self.port}"
        self._web_dir = get_web_ui_dir()
        if not self._web_dir.exists():
            raise FileNotFoundError(f"Web-UI-Verzeichnis fehlt: {self._web_dir}")

        self._mobile_dir = get_base_dir() / "mobile_ui"
        if not self._mobile_dir.exists():
            self._mobile_dir = Path(__file__).resolve().parent / "mobile_ui"

        self._online_listener = self._handle_external_online_change
        register_online_mode_listener(self._online_listener)
        self._callbacks_wired = False

        self.app = self._build_fastapi_app()
        self.add_log("Web-HUD Command Center initialisiert (Turbo-Start aktiv).")

    def attach_lazy_callbacks(self) -> None:
        """Bindet Ghost-Overlay-, Autonomie- und Schwarm-Callbacks im Hintergrund nach HUD-Start."""
        if self._callbacks_wired:
            return
        self._callbacks_wired = True
        try:
            _get_ghost_overlay().set_event_callback(self._on_ghost_cursor_event)
        except Exception:
            pass
        try:
            _get_autonomous_agent().register_monitor_callback(self._on_agent_monitor_event)
        except Exception:
            pass
        try:
            _get_swarm_core().register_hud_callback(self._on_agent_monitor_event)
        except Exception:
            pass
        try:
            from agent_hand import (
                DESKTOP_VISION_STREAM,
                SMART_MOUSE_GUARD,
                register_hud_visibility_callback,
            )

            register_hud_visibility_callback(
                lambda minimize: self.minimize_to_background() if minimize else self.restore_from_background()
            )
            SMART_MOUSE_GUARD.register_abort_callback(self._on_smart_mouse_abort)
            DESKTOP_VISION_STREAM.register_vision_status_callback(
                lambda st: self._broadcast({"type": "vision_stream_status", "status": st})
            )
        except Exception:
            pass
        try:
            from executive_assistant import EXECUTIVE_ASSISTANT

            if self._memory is not None:
                EXECUTIVE_ASSISTANT.attach_memory(self._memory)
            EXECUTIVE_ASSISTANT.suggestions.register_callback(
                lambda sug: self._broadcast({"type": "smart_suggestion", "suggestion": sug})
            )
        except Exception:
            pass

    def _on_smart_mouse_abort(self, reason: str) -> None:
        self.add_log(f"🛑 Maus-Wächter: {reason}")
        self.set_status("Bereit")
        self._broadcast({"type": "agent_aborted", "reason": reason})

    def attach_runtime_services(
        self,
        *,
        on_manual_trigger: Optional[Callable[[], None]] = None,
        on_camera_check: Optional[Callable[[], None]] = None,
        on_camera_toggle: Optional[Callable[[], bool]] = None,
        on_mic_mute_toggle: Optional[Callable[[], bool]] = None,
        on_quick_command: Optional[Callable[[str], None]] = None,
        frame_provider: Optional[Callable[[], Optional[object]]] = None,
        on_settings_saved: Optional[Callable[[dict[str, str]], None]] = None,
        on_mode_toggle: Optional[Callable[[bool], None]] = None,
        on_close: Optional[Callable[[], None]] = None,
        memory: Optional[Any] = None,
        on_mobile_ask: Optional[Callable[[str, Optional[bytes]], dict[str, Any] | str]] = None,
        tts_engine: Optional[Any] = None,
        brain: Optional[Any] = None,
        alarm_manager: Optional[Any] = None,
    ) -> None:
        """Verknüpft asynchron nachgeladene Backend-Dienste nach dem sofortigen Öffnen des HUD-Fensters."""
        if on_manual_trigger is not None:
            self._on_manual_trigger = on_manual_trigger
        if on_camera_check is not None:
            self._on_camera_check = on_camera_check
        if on_camera_toggle is not None:
            self._on_camera_toggle = on_camera_toggle
        if on_mic_mute_toggle is not None:
            self._on_mic_mute_toggle = on_mic_mute_toggle
        if on_quick_command is not None:
            self._on_quick_command = on_quick_command
        if frame_provider is not None:
            self._frame_provider = frame_provider
        if on_settings_saved is not None:
            self._on_settings_saved = on_settings_saved
        if on_mode_toggle is not None:
            self._on_mode_toggle = on_mode_toggle
        if on_close is not None:
            self._on_close = on_close
        if memory is not None:
            self._memory = memory
        if on_mobile_ask is not None:
            self._on_mobile_ask = on_mobile_ask
        if tts_engine is not None:
            self._tts_engine = tts_engine
        if brain is not None:
            self._brain = brain
        if alarm_manager is not None:
            self._alarm_manager = alarm_manager
        self.attach_lazy_callbacks()

    def _on_ghost_cursor_event(self, payload: dict[str, Any]) -> None:
        ev_type = str(payload.get("type") or "ghost_cursor")
        lbl = str(payload.get("label") or "").strip()
        if ev_type == "ghost_cursor_click" and lbl:
            self.add_log(f"Ghost-Cursor: {lbl}")
        self._broadcast(payload)

    def _on_agent_monitor_event(self, snap: dict[str, Any]) -> None:
        self._broadcast({"type": "agent_monitor", "agent_monitor": snap})

    @staticmethod
    def _sanitize_ws_value(value: Any, max_str: int = 420, max_items: int = 12, depth: int = 0) -> Any:
        if depth > 4:
            return str(value)[:max_str] if value is not None else ""
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, (bytes, bytearray, memoryview)):
            return f"<{len(value)} bytes omitted>"
        if isinstance(value, str):
            low = value[:64].lower()
            if "data:image/" in low or ";base64," in low:
                return "[image omitted]"
            if len(value) > max_str:
                return value[: max_str - 3] + "..."
            return value
        if isinstance(value, dict):
            cleaned: dict[str, Any] = {}
            for k, v in value.items():
                key_str = str(k)
                key_low = key_str.lower()
                if any(tok in key_low for tok in ("base64", "preview_frame", "jpeg", "screenshot", "raw_image")):
                    continue
                if key_low in ("agents",) and isinstance(v, dict):
                    sub_dict: dict[str, Any] = {}
                    for ak, av in list(v.items())[:8]:
                        if isinstance(av, dict):
                            sub_dict[str(ak)] = {
                                "active": bool(av.get("active", False)),
                                "status": JarvisGUI._sanitize_ws_value(av.get("status", ""), max_str=40, depth=depth + 2),
                                "step": JarvisGUI._sanitize_ws_value(av.get("step", ""), max_str=90, depth=depth + 2),
                            }
                    cleaned[key_str] = sub_dict
                    continue
                cleaned[key_str] = JarvisGUI._sanitize_ws_value(
                    v,
                    max_str=220 if key_low in ("step_label", "text", "line") else 140,
                    max_items=6,
                    depth=depth + 1,
                )
            return cleaned
        if isinstance(value, (list, tuple)):
            return [
                JarvisGUI._sanitize_ws_value(item, max_str=120, max_items=6, depth=depth + 1)
                for item in list(value)[:max_items]
            ]
        return str(value)[:max_str]

    @classmethod
    def _sanitize_ws_payload(cls, payload: dict[str, Any], max_bytes: int = 2000) -> dict[str, Any]:
        """Hält jeden WebSocket-Frame frei von Base64-Bildern und strikt unter 2 KB."""
        if not isinstance(payload, dict):
            return {}
        ev_type = str(payload.get("type") or "")
        max_s = 700 if ev_type in ("jarvis_text", "user_text", "snapshot") else 240
        max_it = 12 if ev_type == "snapshot" else 6
        cleaned = cls._sanitize_ws_value(payload, max_str=max_s, max_items=max_it, depth=0)
        if not isinstance(cleaned, dict):
            return {"type": ev_type}
        try:
            raw = json.dumps(cleaned, ensure_ascii=False).encode("utf-8")
            if len(raw) <= max_bytes:
                return cleaned
        except Exception:
            pass

        # Bei Überschreitung weiter einkürzen (Snapshot oder langer Agent-Monitor/Text)
        if ev_type == "snapshot" and isinstance(cleaned.get("data"), dict):
            data = dict(cleaned["data"])
            data["dialog"] = list(data.get("dialog") or [])[-4:]
            data["logs"] = list(data.get("logs") or [])[-5:]
            data["jarvis_text"] = str(data.get("jarvis_text") or "")[:240]
            data["user_text"] = str(data.get("user_text") or "")[:160]
            if isinstance(data.get("agent_monitor"), dict):
                am = dict(data["agent_monitor"])
                am.pop("explorer_drives", None)
                am.pop("nas_drives", None)
                data["agent_monitor"] = cls._sanitize_ws_value(am, max_str=80, max_items=4, depth=1)
            cleaned["data"] = data
            try:
                if len(json.dumps(cleaned, ensure_ascii=False).encode("utf-8")) <= max_bytes:
                    return cleaned
            except Exception:
                pass
            data.pop("dialog", None)
            data.pop("logs", None)
            data.pop("agent_monitor", None)
            return {"type": "snapshot", "data": data}

        compact: dict[str, Any] = {"type": ev_type}
        for k, v in cleaned.items():
            if k == "type":
                continue
            compact[k] = cls._sanitize_ws_value(v, max_str=120, max_items=4, depth=2)
            try:
                if len(json.dumps(compact, ensure_ascii=False).encode("utf-8")) > max_bytes:
                    compact.pop(k, None)
                    break
            except Exception:
                compact.pop(k, None)
                break
        return compact

    # ------------------------------------------------------------------
    # FastAPI & WebSocket Backend (blockiert niemals bei minimierter UI)
    # ------------------------------------------------------------------
    def _build_fastapi_app(self) -> FastAPI:
        app = FastAPI(title="JARVIS Command Center HUD", docs_url=None, redoc_url=None)

        async def websocket_loop() -> None:
            last_audio = (-1.0, False, False)
            while not self._stop_event.is_set():
                try:
                    await asyncio.sleep(0.15)
                    clients = list(self._active_websockets)
                    if not clients or self._window_minimized:
                        # Verwerfe veraltete Events, falls kein Client verbunden oder HUD minimiert ist
                        while self._ws_event_queue.qsize() > 30:
                            try:
                                self._ws_event_queue.get_nowait()
                            except queue.Empty:
                                break
                        continue

                    packets: list[dict[str, Any]] = []
                    latest_telemetry: Optional[dict[str, Any]] = None
                    latest_agent_monitor: Optional[dict[str, Any]] = None
                    latest_brain_activity: Optional[dict[str, Any]] = None
                    latest_monitor_topology: Optional[dict[str, Any]] = None
                    latest_n8n_status: Optional[dict[str, Any]] = None

                    for _ in range(32):
                        try:
                            ev = self._ws_event_queue.get_nowait()
                        except queue.Empty:
                            break
                        if not isinstance(ev, dict):
                            continue
                        ev_type = ev.get("type")
                        if ev_type == "telemetry":
                            latest_telemetry = ev
                        elif ev_type == "agent_monitor":
                            latest_agent_monitor = ev
                        elif ev_type == "brain_activity":
                            latest_brain_activity = ev
                        elif ev_type == "monitor_topology":
                            latest_monitor_topology = ev
                        elif ev_type == "n8n_status":
                            latest_n8n_status = ev
                        else:
                            packets.append(ev)

                    if latest_telemetry is not None:
                        packets.append(latest_telemetry)
                    if latest_agent_monitor is not None:
                        packets.append(latest_agent_monitor)
                    if latest_brain_activity is not None:
                        packets.append(latest_brain_activity)
                    if latest_monitor_topology is not None:
                        packets.append(latest_monitor_topology)
                    if latest_n8n_status is not None:
                        packets.append(latest_n8n_status)

                    cur_level = float(self._live_state.get("audio_level", 0.0))
                    cur_vad = bool(self._live_state.get("vad_active", False))
                    cur_speak = bool(self._live_state.get("speaking_active", False))
                    audio_snap = (round(cur_level, 3), cur_vad, cur_speak)
                    if audio_snap != last_audio or audio_snap[0] > 0.02 or cur_vad or cur_speak:
                        packets.append(
                            {
                                "type": "audio",
                                "level": cur_level,
                                "level_pct": int(round(cur_level * 100.0)),
                                "vad_active": cur_vad,
                                "speaking_active": cur_speak,
                            }
                        )
                        last_audio = audio_snap

                    if not packets:
                        continue

                    safe_packets = [self._sanitize_ws_payload(pkt, max_bytes=2000) for pkt in packets]

                    dead_clients: list[WebSocket] = []
                    for ws in clients:
                        for pkt in safe_packets:
                            try:
                                await asyncio.wait_for(ws.send_json(pkt), timeout=0.5)
                            except Exception:
                                dead_clients.append(ws)
                                break
                    for ws in dead_clients:
                        self._active_websockets.discard(ws)
                except asyncio.CancelledError:
                    break
                except Exception:
                    await asyncio.sleep(0.15)

        @app.on_event("startup")
        async def _on_fastapi_startup() -> None:
            from beta_guard import BETA_GUARD

            if self._ws_broadcaster_task is None or self._ws_broadcaster_task.done():
                self._ws_broadcaster_task = asyncio.create_task(websocket_loop())
            if BETA_GUARD.is_expired():
                self.add_log("Beta-Phase beendet.")
                BETA_GUARD.announce_expired_once(log_fn=self.add_log)
                return
            if self._on_server_startup is not None:
                def _run_startup_hook() -> None:
                    try:
                        self._on_server_startup()
                    except Exception as exc:
                        self.add_log(f"[MIC-ERROR] Server-Start-Mikrofonfehler: {exc}")

                threading.Thread(
                    target=_run_startup_hook,
                    name="JarvisFastApiMicStartup",
                    daemon=True,
                ).start()

        @app.on_event("shutdown")
        async def _on_fastapi_shutdown() -> None:
            if self._ws_broadcaster_task is not None and not self._ws_broadcaster_task.done():
                self._ws_broadcaster_task.cancel()
                try:
                    await self._ws_broadcaster_task
                except Exception:
                    pass

        # CORS für Android PWA / Capacitor App im Heimnetzwerk
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

        # LAN-Sicherheits-Middleware + Beta-Ablauf-Sperre (Kill-Switch)
        @app.middleware("http")
        async def lan_security_middleware(request: Request, call_next):
            client_host = request.client.host if request.client else "127.0.0.1"
            if not _is_private_or_local_ip(client_host):
                return JSONResponse(
                    {"ok": False, "error": "Access denied: outside LAN"},
                    status_code=403,
                )
            from beta_guard import BETA_GUARD

            if BETA_GUARD.is_expired():
                path = request.url.path or "/"
                allow_ui_shell = path in ("/", "/panel/logs", "/panel/vision", "/window/system-telemetry", "/window/screen-matrix", "/neural-core", "/window/neural-core") or path.startswith("/static/")
                if not allow_ui_shell:
                    return JSONResponse(BETA_GUARD.get_api_expired_payload(), status_code=200)
            return await call_next(request)

        @app.get("/api/beta_status")
        async def api_beta_status() -> JSONResponse:
            from beta_guard import BETA_GUARD

            st = BETA_GUARD.get_status()
            if st.get("expired"):
                return JSONResponse(BETA_GUARD.get_api_expired_payload())
            return JSONResponse(st)

        app.mount("/static", StaticFiles(directory=str(self._web_dir)), name="static")
        if self._mobile_dir.exists():
            app.mount("/mobile", StaticFiles(directory=str(self._mobile_dir), html=True), name="mobile")

        @app.get("/")
        async def index() -> FileResponse:
            return FileResponse(self._web_dir / "index.html")

        @app.get("/panel/logs")
        @app.get("/window/system-telemetry")
        async def panel_logs() -> FileResponse:
            return FileResponse(self._web_dir / "panel_logs.html")

        @app.get("/panel/vision")
        @app.get("/window/screen-matrix")
        async def panel_vision() -> FileResponse:
            return FileResponse(self._web_dir / "panel_vision.html")

        @app.get("/neural-core")
        @app.get("/window/neural-core")
        async def panel_neural_core() -> FileResponse:
            return FileResponse(self._web_dir / "panel_neural_core.html")

        @app.get("/api/state")
        async def api_state() -> JSONResponse:
            snap = await asyncio.to_thread(self._build_snapshot)
            return JSONResponse(snap)

        @app.get("/api/settings")
        async def api_get_settings() -> JSONResponse:
            settings = await asyncio.to_thread(load_settings)
            return JSONResponse(settings)

        @app.post("/api/settings")
        async def api_save_settings(request: Request) -> JSONResponse:
            raw = await request.json()
            if not isinstance(raw, dict):
                return JSONResponse({"ok": False, "error": "Ungültiges JSON"}, status_code=400)
            cleaned = {str(k): str(v or "") for k, v in raw.items()}

            def _apply_settings() -> bool:
                normalized = save_settings(cleaned)
                try:
                    _get_nas_brain().refresh_storage_location()
                    self._broadcast({"type": "agent_monitor", "agent_monitor": _get_swarm_core().get_swarm_state()})
                except Exception:
                    pass
                self.add_log("Einstellungen gespeichert (.env aktualisiert).")
                if self._on_settings_saved is not None:
                    threading.Thread(
                        target=self._on_settings_saved,
                        args=(normalized,),
                        daemon=True,
                    ).start()
                return get_is_online()

            online_state = await asyncio.to_thread(_apply_settings)
            return JSONResponse({"ok": True, "is_online": online_state})

        @app.get("/api/autostart")
        def api_get_autostart() -> JSONResponse:
            return JSONResponse({"ok": True, "enabled": get_autostart_status()})

        @app.post("/api/autostart")
        async def api_set_autostart(request: Request) -> JSONResponse:
            body = await request.json()
            enabled = bool(body.get("enabled", False)) if isinstance(body, dict) else False

            def _apply_autostart() -> tuple[bool, bool]:
                ok = set_autostart(enabled)
                cur = get_autostart_status()
                return ok, cur

            success, current_enabled = await asyncio.to_thread(_apply_autostart)
            status_text = "aktiviert" if enabled else "deaktiviert"
            self.add_log(f"Autostart {status_text} ({'Erfolg' if success else 'Fehler'}).")
            self._broadcast({"type": "autostart", "enabled": current_enabled})
            return JSONResponse({"ok": success, "enabled": current_enabled})

        @app.post("/api/mode")
        async def api_set_mode(request: Request) -> JSONResponse:
            body = await request.json()
            enabled = bool(body.get("enabled", True)) if isinstance(body, dict) else True
            self.update_online_switch(enabled)
            if self._on_mode_toggle is not None:
                threading.Thread(
                    target=self._on_mode_toggle,
                    args=(enabled,),
                    daemon=True,
                ).start()
            else:
                await asyncio.to_thread(set_online_mode, enabled)
            return JSONResponse({"ok": True, "is_online": enabled})

        @app.post("/api/verify_key")
        async def api_verify_key(request: Request) -> JSONResponse:
            from brain import verify_provider_key

            body = await request.json()
            provider = str(body.get("provider", "")).strip().lower()
            api_key = str(body.get("api_key", "")).strip()
            res = await asyncio.to_thread(verify_provider_key, provider, api_key)
            return JSONResponse(res)

        @app.get("/api/model_priority")
        def api_get_model_priority() -> JSONResponse:
            return JSONResponse({
                "priority": get_model_priority(),
                "active_provider": get_active_provider(),
            })

        @app.post("/api/model_priority")
        async def api_set_model_priority(request: Request) -> JSONResponse:
            body = await request.json()
            prio = body.get("priority", [])
            new_prio = await asyncio.to_thread(set_model_priority, prio)
            self.add_log(f"Modell-Priorität aktualisiert: {' -> '.join(p.upper() for p in new_prio)}")
            if self._on_settings_saved is not None:
                threading.Thread(
                    target=self._on_settings_saved,
                    args=({"MODEL_PRIORITY": ",".join(new_prio)},),
                    daemon=True,
                ).start()
            return JSONResponse({"ok": True, "priority": new_prio})

        @app.post("/api/set_active_provider")
        async def api_set_active_provider(request: Request) -> JSONResponse:
            body = await request.json()
            provider = str(body.get("provider", "")).strip().lower()
            target = await asyncio.to_thread(set_active_provider, provider)
            is_online = target != "ollama"
            self.update_online_switch(is_online)
            if self._on_mode_toggle is not None:
                threading.Thread(target=self._on_mode_toggle, args=(is_online,), daemon=True).start()
            if target == "auto":
                prio = await asyncio.to_thread(get_model_priority)
                label = f"AUTO (#{prio[0].upper()})"
            elif target == "ollama":
                label = "OLLAMA LOKAL"
            else:
                label = target.upper()
            with self._state_lock:
                self._model_label = f"MODEL: {label}"
            self.add_log(f"Aktiver KI-Anbieter gewechselt: {label}")
            self._broadcast({"type": "model", "model_label": self._model_label})
            self._broadcast({"type": "active_provider", "active_provider": target, "is_online": is_online})
            return JSONResponse({
                "ok": True,
                "active_provider": target,
                "is_online": is_online,
                "model_label": self._model_label,
            })

        @app.post("/api/camera/toggle")
        def api_camera_toggle_route() -> JSONResponse:
            if self._on_camera_toggle is not None:
                paused = bool(self._on_camera_toggle())
            else:
                paused = not self._camera_paused
            v_status = VISION_STATUS_PASSIVE if paused else "WEBCAM: MANUELLER LIVE-STREAM AKTIV"
            with self._state_lock:
                self._camera_paused = paused
                self._vision_status = v_status
            self.add_log(
                "Webcam im On-Demand-Standby (Sensor physisch AUS, 0% CPU)."
                if paused
                else "Webcam manuell aktiviert (Live-Stream aktiv)."
            )
            self._broadcast(
                {
                    "type": "camera_paused",
                    "paused": paused,
                    "active": not paused,
                    "vision_status": v_status,
                }
            )
            return JSONResponse(
                {
                    "ok": True,
                    "camera_paused": paused,
                    "active": not paused,
                    "vision_status": v_status,
                }
            )

        @app.post("/api/shutdown")
        def api_shutdown_route() -> JSONResponse:
            self.add_log("System-Shutdown eingeleitet...")
            threading.Thread(target=self._handle_close, daemon=True).start()
            return JSONResponse({"ok": True})

        # ------------------------------------------------------------------
        # JARVIS Mobile & Camera Edition API (Heimnetz-Sync & Mobile-Ask)
        # ------------------------------------------------------------------
        @app.get("/mobile", response_model=None)
        async def mobile_index() -> FileResponse | JSONResponse:
            index_file = self._mobile_dir / "index.html"
            if index_file.exists():
                return FileResponse(index_file)
            return JSONResponse({"ok": False, "error": "mobile_ui/index.html nicht gefunden"}, status_code=404)

        @app.api_route("/api/mobile/ping", methods=["GET", "POST"])
        async def api_mobile_ping() -> JSONResponse:
            return JSONResponse({
                "ok": True,
                "status": "online",
                "hostname": platform.node(),
                "model_label": self._model_label,
                "active_provider": get_active_provider(),
                "is_online": self._is_online,
                "tts_voice": DEFAULT_EDGE_VOICE,
                "tts_fallback_voice": DEFAULT_EDGE_FALLBACK_VOICE,
                "tts_pitch": DEFAULT_EDGE_PITCH,
                "tts_rate": DEFAULT_EDGE_RATE,
                "tts_endpoint": "/tts",
                "timestamp": time.time(),
            })

        async def _synth_tts_wav(
            text: str,
            voice: Optional[str] = None,
            pitch: Optional[str] = None,
            rate: Optional[str] = None,
        ) -> bytes:
            cleaned = (text or "").strip()
            if not cleaned:
                return b""
            v = (voice or DEFAULT_EDGE_VOICE).strip()
            p = (pitch or DEFAULT_EDGE_PITCH).strip()
            r = (rate or DEFAULT_EDGE_RATE).strip()
            if self._tts_engine is not None and hasattr(self._tts_engine, "synthesize_wav_bytes"):
                try:
                    wav_bytes = await asyncio.to_thread(
                        self._tts_engine.synthesize_wav_bytes,
                        cleaned,
                        v,
                        p,
                        r,
                    )
                    if wav_bytes:
                        return wav_bytes
                except Exception as exc:
                    self.add_log(f"TTS-Engine Synthese-Warnung: {exc}")
            from tts import synthesize_edge_tts_wav_async

            return await synthesize_edge_tts_wav_async(
                cleaned,
                voice=v,
                fallback_voice=DEFAULT_EDGE_FALLBACK_VOICE,
                pitch=p,
                rate=r,
            )

        @app.api_route("/tts", methods=["GET", "POST"], response_model=None)
        @app.api_route("/api/tts", methods=["GET", "POST"], response_model=None)
        @app.api_route("/api/mobile/tts", methods=["GET", "POST"], response_model=None)
        async def api_tts_endpoint(request: Request) -> Response | JSONResponse:
            text = ""
            voice = DEFAULT_EDGE_VOICE
            pitch = DEFAULT_EDGE_PITCH
            rate = DEFAULT_EDGE_RATE
            if request.method == "POST":
                try:
                    body = await request.json()
                    if isinstance(body, dict):
                        text = str(body.get("text", "")).strip()
                        voice = str(body.get("voice") or voice).strip()
                        pitch = str(body.get("pitch") or pitch).strip()
                        rate = str(body.get("rate") or rate).strip()
                except Exception:
                    pass
            if not text:
                text = str(request.query_params.get("text", "")).strip()
                voice = str(request.query_params.get("voice") or voice).strip()
                pitch = str(request.query_params.get("pitch") or pitch).strip()
                rate = str(request.query_params.get("rate") or rate).strip()

            if not text:
                return JSONResponse({"ok": False, "error": "Kein Text für TTS übergeben."}, status_code=400)

            wav_bytes = await _synth_tts_wav(text[:1200], voice=voice, pitch=pitch, rate=rate)
            if not wav_bytes:
                return JSONResponse(
                    {"ok": False, "error": "TTS-Audio konnte nicht generiert werden."},
                    status_code=503,
                )
            return Response(
                content=wav_bytes,
                media_type="audio/wav",
                headers={
                    "Cache-Control": "public, max-age=3600",
                    "X-Jarvis-Voice": voice,
                    "X-Jarvis-Pitch": pitch,
                    "X-Jarvis-Rate": rate,
                },
            )

        @app.api_route("/api/mobile/voice-pack", methods=["GET", "POST"])
        async def api_mobile_voice_pack(request: Request) -> JSONResponse:
            """Erzeugt ein geklontes Offline-Sprachpaket (de-DE-ConradNeural -15Hz/-5% bzw. Karlsson Deep) für das Smartphone."""
            voice = DEFAULT_EDGE_VOICE
            pitch = DEFAULT_EDGE_PITCH
            rate = DEFAULT_EDGE_RATE
            custom_phrases: list[str] = []
            if request.method == "POST":
                try:
                    body = await request.json()
                    if isinstance(body, dict):
                        voice = str(body.get("voice") or voice).strip()
                        pitch = str(body.get("pitch") or pitch).strip()
                        rate = str(body.get("rate") or rate).strip()
                        if isinstance(body.get("phrases"), list):
                            custom_phrases = [str(x).strip() for x in body["phrases"] if str(x).strip()]
                except Exception:
                    pass

            cache_file = get_user_data_dir() / "mobile_voice_pack_cache.json"
            cached_clips: dict[str, str] = {}
            if cache_file.is_file():
                try:
                    cached_data = json.loads(cache_file.read_text(encoding="utf-8"))
                    if (
                        isinstance(cached_data, dict)
                        and cached_data.get("voice") == voice
                        and cached_data.get("pitch") == pitch
                        and isinstance(cached_data.get("clips"), dict)
                    ):
                        cached_clips = dict(cached_data["clips"])
                except Exception:
                    cached_clips = {}

            from tts import MOBILE_VOICE_PACK_PHRASES

            phrases = list(MOBILE_VOICE_PACK_PHRASES)
            for cp in custom_phrases[:12]:
                if cp not in phrases:
                    phrases.append(cp)

            updated = False
            for phrase in phrases:
                if phrase not in cached_clips:
                    wav_bytes = await _synth_tts_wav(phrase, voice=voice, pitch=pitch, rate=rate)
                    if wav_bytes:
                        cached_clips[phrase] = base64.b64encode(wav_bytes).decode("ascii")
                        updated = True

            if updated and cached_clips:
                try:
                    cache_file.write_text(
                        json.dumps(
                            {
                                "voice": voice,
                                "pitch": pitch,
                                "rate": rate,
                                "updated_at": time.time(),
                                "clips": cached_clips,
                            },
                            ensure_ascii=False,
                        ),
                        encoding="utf-8",
                    )
                except Exception:
                    pass

            return JSONResponse({
                "ok": True,
                "voice": voice,
                "pitch": pitch,
                "rate": rate,
                "count": len(cached_clips),
                "clips": cached_clips,
            })

        @app.post("/api/mobile/sync")
        async def api_mobile_sync(request: Request) -> JSONResponse:
            try:
                body = await request.json()
            except Exception:
                body = {}
            mobile_memories = body.get("mobile_memories", [])

            def _do_sync() -> tuple[int, list[dict[str, Any]], list[str], str]:
                added_cnt = 0
                mems: list[dict[str, Any]] = []
                if self._memory is not None:
                    if isinstance(mobile_memories, list) and mobile_memories:
                        added_cnt, _ = self._memory.merge_memories(mobile_memories)
                    mems = self._memory.get_all_memories()
                return added_cnt, mems, get_model_priority(), get_active_provider()

            added, all_mems, model_prio, act_prov = await asyncio.to_thread(_do_sync)
            if added > 0:
                self.add_log(f"Mobile-Sync: {added} neue Einträge vom Smartphone synchronisiert.")

            return JSONResponse({
                "ok": True,
                "added": added,
                "memories": all_mems,
                "last_sync_timestamp": time.time(),
                "model_priority": model_prio,
                "active_provider": act_prov,
            })

        async def _synth_tts_audio_pack(
            text: str,
            voice: Optional[str] = None,
            pitch: Optional[str] = None,
            rate: Optional[str] = None,
        ) -> tuple[bytes, str]:
            """Erzeugt Mobile-TTS bevorzugt als MP3 (`de-DE-KillianNeural` / `ConradNeural`) mit lokalem Piper-WAV-Fallback."""
            cleaned = (text or "").strip()
            if not cleaned:
                return b"", "audio/mp3"
            v = (voice or DEFAULT_EDGE_FALLBACK_VOICE or "de-DE-KillianNeural").strip()
            p = (pitch or DEFAULT_EDGE_PITCH).strip()
            r = (rate or DEFAULT_EDGE_RATE).strip()
            from tts import synthesize_edge_tts_mp3_async

            audio_bytes, mime = await synthesize_edge_tts_mp3_async(
                cleaned,
                voice=v,
                fallback_voice=DEFAULT_EDGE_VOICE,
                pitch=p,
                rate=r,
                tts_engine=self._tts_engine,
            )
            if audio_bytes:
                return audio_bytes, mime

            wav_bytes = await _synth_tts_wav(cleaned, voice=v, pitch=p, rate=r)
            if wav_bytes:
                return wav_bytes, "audio/wav"
            return b"", "audio/mp3"

        @app.api_route("/api/tts_audio", methods=["GET", "POST"], response_model=None)
        async def api_tts_audio(request: Request) -> Response | JSONResponse:
            """Erzeugt echte Jarvis-Sprache (`de-DE-KillianNeural` / Piper) als Base64-MP3 oder Roh-Stream für Mobile."""
            text = ""
            voice = DEFAULT_EDGE_FALLBACK_VOICE or "de-DE-KillianNeural"
            pitch = DEFAULT_EDGE_PITCH
            rate = DEFAULT_EDGE_RATE
            raw_binary = False
            if request.method == "POST":
                try:
                    body = await request.json()
                    if isinstance(body, dict):
                        text = str(body.get("text", "")).strip()
                        voice = str(body.get("voice") or voice).strip()
                        pitch = str(body.get("pitch") or pitch).strip()
                        rate = str(body.get("rate") or rate).strip()
                        raw_binary = bool(body.get("raw", False))
                except Exception:
                    pass
            if not text:
                text = str(request.query_params.get("text", "")).strip()
                voice = str(request.query_params.get("voice") or voice).strip()
                pitch = str(request.query_params.get("pitch") or pitch).strip()
                rate = str(request.query_params.get("rate") or rate).strip()
                raw_binary = raw_binary or (request.query_params.get("raw", "").lower() in ("1", "true", "yes"))

            if not text:
                return JSONResponse({"ok": False, "error": "Kein Text für TTS übergeben."}, status_code=400)

            audio_bytes, audio_mime = await _synth_tts_audio_pack(text[:1200], voice=voice, pitch=pitch, rate=rate)
            if not audio_bytes:
                return JSONResponse(
                    {"ok": False, "error": "TTS-Audio konnte nicht generiert werden."},
                    status_code=503,
                )
            if raw_binary:
                return Response(
                    content=audio_bytes,
                    media_type=audio_mime,
                    headers={"Cache-Control": "public, max-age=3600", "X-Jarvis-Voice": voice},
                )
            return JSONResponse({
                "ok": True,
                "audio_base64": base64.b64encode(audio_bytes).decode("ascii"),
                "audio_mime": audio_mime,
                "voice": voice,
                "pitch": pitch,
                "rate": rate,
            })

        @app.post("/api/mobile/ask")
        @app.post("/api/mobile_chat")
        async def api_mobile_ask(request: Request) -> JSONResponse:
            try:
                body = await request.json()
            except Exception:
                return JSONResponse({"ok": False, "error": "Ungültiges JSON"}, status_code=400)

            text = str(body.get("text", "")).strip()
            img_b64 = body.get("image_base64")
            include_audio = bool(body.get("include_audio", True))
            req_voice = str(body.get("voice") or DEFAULT_EDGE_FALLBACK_VOICE or "de-DE-KillianNeural").strip()
            req_pitch = str(body.get("pitch") or DEFAULT_EDGE_PITCH).strip()
            req_rate = str(body.get("rate") or DEFAULT_EDGE_RATE).strip()

            image_bytes: Optional[bytes] = None
            if img_b64 and isinstance(img_b64, str):
                try:
                    raw_b64 = img_b64.split(",", 1)[1] if "," in img_b64 else img_b64
                    image_bytes = base64.b64decode(raw_b64)
                except Exception as exc:
                    self.add_log(f"Mobile Bild-Dekodierfehler: {exc}")

            if not text and image_bytes is not None:
                text = "Was siehst du auf diesem Foto? Beschreibe kurz das Wichtigste."

            if not text:
                return JSONResponse({"ok": False, "error": "Kein Text übergeben"}, status_code=400)

            try:
                from agent_hand import DESKTOP_VISION_STREAM

                DESKTOP_VISION_STREAM.boost_active_rate(10.0)
            except Exception:
                pass
            self.add_log(f"Mobile-Anfrage: {text[:50]}...")
            answer_text = ""
            provider = "Jarvis"
            model = "Core"

            if self._on_mobile_ask is not None:
                try:
                    res = await asyncio.to_thread(self._on_mobile_ask, text, image_bytes)
                    if isinstance(res, dict):
                        answer_text = str(res.get("text", ""))
                        provider = str(res.get("provider", provider))
                        model = str(res.get("model", model))
                    elif isinstance(res, str):
                        answer_text = res
                except Exception as exc:
                    answer_text = f"Fehler bei Anfrageverarbeitung: {exc}"
            else:
                answer_text = "Jarvis Backend bereit."

            audio_b64: Optional[str] = None
            audio_mime: Optional[str] = None
            if include_audio and answer_text:
                try:
                    audio_bytes, detected_mime = await _synth_tts_audio_pack(
                        answer_text[:900],
                        voice=req_voice,
                        pitch=req_pitch,
                        rate=req_rate,
                    )
                    if audio_bytes:
                        audio_b64 = base64.b64encode(audio_bytes).decode("ascii")
                        audio_mime = detected_mime
                except Exception:
                    audio_b64 = None
                    audio_mime = None

            return JSONResponse({
                "ok": True,
                "text": answer_text,
                "provider": provider,
                "model": model,
                "audio_base64": audio_b64,
                "audio_mime": audio_mime,
                "tts_voice": req_voice,
                "timestamp": time.time(),
            })

        @app.api_route("/api/pairing_qr", methods=["GET", "POST"])
        async def api_pairing_qr(request: Request) -> JSONResponse:
            """Erzeugt das JARVIS_PAIRING_V1 JSON-Kopplungspaket + QR-Code-Bild für 1-Klick-Kopplung."""
            req_pi_url = str(request.query_params.get("pi_url", "") or "").strip()
            req_lan_url = str(request.query_params.get("lan_url", "") or "").strip()
            if request.method == "POST":
                try:
                    body = await request.json()
                    if isinstance(body, dict):
                        if body.get("pi_url") is not None:
                            req_pi_url = str(body.get("pi_url") or "").strip()
                        if body.get("lan_url") is not None:
                            req_lan_url = str(body.get("lan_url") or "").strip()
                        if req_pi_url:
                            await asyncio.to_thread(save_settings, {"PI_LAN_URL": req_pi_url})
                except Exception:
                    pass

            def _build_pairing_data() -> dict[str, Any]:
                settings = load_settings()
                lan_ip = _get_lan_ip()
                host_type = _detect_pairing_host_type()
                port = int(self.port or 8000)
                local_http = f"http://{lan_ip}:{port}"

                if host_type == "pi":
                    lan_url = req_lan_url or clean_api_key(settings.get("PC_LAN_URL") or os.getenv("PC_LAN_URL") or "") or local_http
                    pi_url = req_pi_url or local_http
                else:
                    lan_url = req_lan_url or local_http
                    pi_url = req_pi_url or clean_api_key(settings.get("PI_LAN_URL") or os.getenv("PI_LAN_URL") or "")

                sync_token = _get_or_create_pairing_sync_token(settings)
                groq_key = clean_api_key(settings.get("GROQ_API_KEY") or os.getenv("GROQ_API_KEY") or "")
                gemini_key = clean_api_key(settings.get("GEMINI_API_KEY") or os.getenv("GEMINI_API_KEY") or "")

                payload = {
                    "type": "JARVIS_PAIRING_V1",
                    "host_type": host_type,
                    "lan_url": lan_url,
                    "pi_url": pi_url,
                    "sync_token": sync_token,
                    "groq_key": groq_key,
                    "gemini_key": gemini_key,
                }
                qr_json = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
                qr_image_base64 = _generate_qr_png_b64(qr_json, box_size_px=300)
                return {
                    "ok": True,
                    **payload,
                    "qr_json": qr_json,
                    "qr_image_base64": qr_image_base64,
                }

            result = await asyncio.to_thread(_build_pairing_data)
            return JSONResponse(result)

        # ------------------------------------------------------------------
        # APK DOWNLOAD & BUILD STATUS ENDPOINTS
        # ------------------------------------------------------------------
        @app.get("/download/jarvis.apk", response_model=None)
        async def download_apk() -> FileResponse | JSONResponse:
            base_dir = get_base_dir()
            candidates = [
                base_dir / "Fertige_Android_APK" / "JARVIS_Mobile.apk",
                base_dir / "Output" / "JARVIS_Mobile.apk",
                self._mobile_dir / "JARVIS_Mobile.apk",
                base_dir / "JARVIS_Mobile.apk",
            ]
            for cand in candidates:
                if cand.exists() and cand.is_file():
                    return FileResponse(
                        str(cand),
                        media_type="application/vnd.android.package-archive",
                        filename="JARVIS_Mobile.apk",
                    )
            lan_ip = _get_lan_ip()
            return JSONResponse(
                {
                    "ok": False,
                    "error": "JARVIS_Mobile.apk wurde noch nicht erstellt. Bitte 4_Build_Android_APK.bat ausführen oder in den Einstellungen auf 'APK JETZT BAUEN' klicken.",
                    "build_script": "4_Build_Android_APK.bat",
                    "lan_ip": lan_ip,
                },
                status_code=404,
            )

        @app.get("/api/apk/status")
        async def api_apk_status() -> JSONResponse:
            base_dir = get_base_dir()
            candidates = [
                base_dir / "Fertige_Android_APK" / "JARVIS_Mobile.apk",
                base_dir / "Output" / "JARVIS_Mobile.apk",
                self._mobile_dir / "JARVIS_Mobile.apk",
                base_dir / "JARVIS_Mobile.apk",
            ]
            found: Optional[Path] = None
            for cand in candidates:
                if cand.exists() and cand.is_file():
                    found = cand
                    break

            lan_ip = _get_lan_ip()
            download_url = f"http://{lan_ip}:{self.port}/download/jarvis.apk"
            if found:
                size_mb = round(found.stat().st_size / (1024 * 1024), 2)
                mtime = found.stat().st_mtime
                return JSONResponse({
                    "ok": True,
                    "available": True,
                    "size_mb": size_mb,
                    "mtime": mtime,
                    "download_url": download_url,
                    "path": str(found),
                    "lan_ip": lan_ip,
                })
            return JSONResponse({
                "ok": True,
                "available": False,
                "size_mb": 0.0,
                "download_url": download_url,
                "lan_ip": lan_ip,
            })

        @app.post("/api/apk/build")
        async def api_apk_build() -> JSONResponse:
            self.add_log("APK-Build-Pipeline im Hintergrund gestartet...")
            def _runner():
                try:
                    import build_apk
                    success = build_apk.run_apk_pipeline()
                    if success:
                        self.add_log("APK-Build erfolgreich! JARVIS_Mobile.apk steht zum Download bereit.")
                    else:
                        self.add_log("APK-Build beendet (Details siehe Konsole/Logs).")
                except Exception as exc:
                    self.add_log(f"Fehler im APK-Build-Prozess: {exc}")

            threading.Thread(target=_runner, daemon=True).start()
            return JSONResponse({"ok": True, "message": "Build gestartet"})

        # ------------------------------------------------------------------
        # Tresor (Vault), Autonomer Agent & Trading-Engine API
        # ------------------------------------------------------------------
        @app.get("/api/execution_mode")
        async def api_get_execution_mode() -> JSONResponse:
            def _read_mode() -> tuple[str, str]:
                v = _get_vault()
                return v.get_execution_mode(), v.get_autonomy_mode()

            exec_mode, auto_mode = await asyncio.to_thread(_read_mode)
            return JSONResponse({
                "ok": True,
                "execution_mode": exec_mode,
                "autonomy_mode": auto_mode,
                "label": (
                    "⚡ MODUS B: VOLL-ZUGRIFF (100% Autonomes Kaufen & Verkaufen)"
                    if exec_mode == "FULL_AUTO"
                    else "🛡️ MODUS A: HALB-AUTONOM (Vorbereiten -> Ich drücke selbst auf Kaufen)"
                ),
            })

        @app.post("/api/execution_mode")
        async def api_set_execution_mode(req: Request) -> JSONResponse:
            try:
                body = await req.json()
            except Exception:
                body = {}
            raw_mode = str(body.get("execution_mode") or body.get("autonomy_mode") or "SEMI_AUTO")

            def _apply_mode() -> tuple[str, str, dict[str, Any]]:
                vault_obj = _get_vault()
                n_mode = vault_obj.set_execution_mode(raw_mode)
                a_mode = vault_obj.get_autonomy_mode()
                st = _get_swarm_core().get_swarm_state()
                return n_mode, a_mode, st

            norm_mode, auto_mode, state = await asyncio.to_thread(_apply_mode)
            self._broadcast({"type": "agent_monitor", "agent_monitor": state})
            return JSONResponse({
                "ok": True,
                "execution_mode": norm_mode,
                "autonomy_mode": auto_mode,
                "state": state,
            })

        @app.post("/api/mail_factory/generate_identity")
        async def api_mail_factory_generate_identity(req: Request) -> JSONResponse:
            try:
                body = await req.json()
            except Exception:
                body = {}
            prefix = str(body.get("prefix") or "jarvis")
            res = await asyncio.to_thread(
                _get_mail_factory().generate_identity_and_password,
                prefix,
                True,
            )
            return JSONResponse(res)

        @app.post("/api/mail_factory/create_instant")
        async def api_mail_factory_create_instant(req: Request) -> JSONResponse:
            try:
                body = await req.json()
            except Exception:
                body = {}
            purpose = str(body.get("purpose") or "Autonome Account-Erstellung")
            res = await asyncio.to_thread(
                _get_mail_factory().create_instant_mailbox_mailtm,
                purpose,
                True,
            )
            return JSONResponse(res)

        @app.post("/api/mail_factory/create_browser")
        async def api_mail_factory_create_browser(req: Request) -> JSONResponse:
            try:
                body = await req.json()
            except Exception:
                body = {}
            provider = str(body.get("provider") or "proton")
            res = await asyncio.to_thread(
                _get_mail_factory().create_browser_mailbox_1fps,
                provider,
                True,
            )
            return JSONResponse(res)

        @app.get("/api/mail_factory/poll")
        async def api_mail_factory_poll() -> JSONResponse:
            res = await asyncio.to_thread(_get_vault().read_latest_verification_email, 4)
            return JSONResponse(res)

        @app.get("/api/trading/scanner")
        async def api_trading_scanner() -> JSONResponse:
            res = await asyncio.to_thread(_get_trading_engine().get_scanner_snapshot)
            return JSONResponse(res)

        @app.post("/api/trading/scan_now")
        async def api_trading_scan_now(req: Request) -> JSONResponse:
            try:
                body = await req.json()
            except Exception:
                body = {}
            interval = str(body.get("interval") or "15m")
            auto_exec = bool(body.get("auto_execute", False))
            budget = float(body.get("trade_budget_eur", 250.0) or 250.0)
            res = await asyncio.to_thread(
                _get_trading_engine().scan_watchlist_once,
                None,
                interval,
                auto_exec,
                budget,
            )
            return JSONResponse(res)

        @app.get("/api/vault")
        async def api_get_vault() -> JSONResponse:
            snap = await asyncio.to_thread(lambda: _get_vault().get_ui_masked_snapshot())
            return JSONResponse(snap)

        @app.post("/api/vault")
        async def api_save_vault(request: Request) -> JSONResponse:
            raw = await request.json()
            if not isinstance(raw, dict):
                return JSONResponse({"ok": False, "error": "Ungültiges JSON"}, status_code=400)

            def _save_v() -> tuple[dict[str, Any], dict[str, Any]]:
                v_snap = _get_vault().update_vault_settings(raw)
                s_snap = _get_swarm_core().get_swarm_state()
                return v_snap, s_snap

            snap, swarm_snap = await asyncio.to_thread(_save_v)
            self.add_log("AES-Tresor & Autonomie-Einstellungen verschlüsselt gespeichert.")
            self._broadcast({"type": "agent_monitor", "agent_monitor": swarm_snap})
            return JSONResponse({"ok": True, "vault": snap})

        @app.get("/api/agent/monitor")
        async def api_agent_monitor() -> JSONResponse:
            state = await asyncio.to_thread(lambda: _get_swarm_core().get_swarm_state())
            return JSONResponse(state)

        @app.post("/api/agent/kill")
        async def api_agent_kill() -> JSONResponse:
            def _abort_all() -> dict[str, Any]:
                try:
                    from multi_cursor_overlay import MULTI_CURSOR_OVERLAY

                    MULTI_CURSOR_OVERLAY.stop_all()
                except Exception:
                    pass
                try:
                    from agent_hand import SMART_MOUSE_GUARD, VISUAL_HAND_AGENT

                    SMART_MOUSE_GUARD.trigger_abort("[STOP] Abbruch durch HUD STOP-Button", speak_hint=False)
                    VISUAL_HAND_AGENT.request_abort()
                except Exception:
                    pass
                return _get_swarm_core().kill_all_agents()

            snap = await asyncio.to_thread(_abort_all)
            self.add_log("🛑 NOT-STOPP: Alle 4 Schwarm-Agenten & 4 Overlay-Cursor sofort beendet!")
            self.set_status("Bereit")
            self._broadcast({"type": "agent_monitor", "agent_monitor": snap})
            return JSONResponse({"ok": True, "agent_monitor": snap})

        @app.get("/api/swarm/cursor_mode")
        async def api_swarm_cursor_mode_get() -> JSONResponse:
            enabled = await asyncio.to_thread(lambda: _get_swarm_core().is_multi_cursor_mode())
            return JSONResponse({"ok": True, "enabled": bool(enabled)})

        @app.post("/api/swarm/cursor_mode")
        async def api_swarm_cursor_mode_post(request: Request) -> JSONResponse:
            try:
                body = await request.json()
            except Exception:
                body = {}
            if isinstance(body, dict) and "enabled" in body:
                want = bool(body.get("enabled"))
            else:
                want = not _get_swarm_core().is_multi_cursor_mode()

            def _toggle() -> tuple[bool, dict[str, Any]]:
                sc = _get_swarm_core()
                res = sc.set_multi_cursor_mode(want)
                return res, sc.get_swarm_state()

            enabled, snap = await asyncio.to_thread(_toggle)
            self.add_log(
                f"⚡ 4-CURSOR SWARM: {'AKTIVIERT (4 sichtbare KI-Cursor)' if enabled else 'DEAKTIVIERT (Klassischer Einzel-Cursor)'}"
            )
            self._broadcast({"type": "swarm_cursor_mode", "enabled": enabled})
            self._broadcast({"type": "agent_monitor", "agent_monitor": snap})
            return JSONResponse({"ok": True, "enabled": enabled, "agent_monitor": snap})

        @app.get("/api/nas/status")
        async def api_nas_status() -> JSONResponse:
            stat = await asyncio.to_thread(lambda: _get_nas_brain().get_status())
            return JSONResponse(stat)

        @app.post("/api/executive/briefing")
        async def api_executive_briefing(request: Request) -> JSONResponse:
            try:
                body = await request.json()
            except Exception:
                body = {}
            open_cockpit = bool(body.get("open_browser_cockpit", True)) if isinstance(body, dict) else True

            def _run_briefing() -> dict[str, Any]:
                from executive_assistant import EXECUTIVE_ASSISTANT

                return EXECUTIVE_ASSISTANT.run_executive_briefing(
                    brain=self._brain,
                    memory=self._memory,
                    alarm_manager=self._alarm_manager,
                    tts=self._tts_engine,
                    on_status=lambda msg: self.add_log(msg),
                    open_browser_cockpit=open_cockpit,
                )

            self.set_status("Denke nach")
            self.add_log("⚡ EXECUTIVE BRIEFING wird gestartet...")
            res = await asyncio.to_thread(_run_briefing)
            brief_text = str(res.get("briefing_text") or "")
            if brief_text:
                self.set_jarvis_text(brief_text)
            self.set_status("Bereit")
            return JSONResponse(res)

        @app.get("/api/executive/profile")
        async def api_executive_profile() -> JSONResponse:
            def _load_prof() -> dict[str, Any]:
                from executive_assistant import EXECUTIVE_ASSISTANT

                prof = (
                    self._memory.get_executive_profile()
                    if (self._memory is not None and hasattr(self._memory, "get_executive_profile"))
                    else {"active_projects": [], "user_preferences": {}, "open_tasks": [], "facts": []}
                )
                prof["smart_suggestion"] = EXECUTIVE_ASSISTANT.suggestions.get_current_suggestion()
                prof["recent_files"] = EXECUTIVE_ASSISTANT.knowledge_graph.get_recent_files(limit=12)
                return {"ok": True, "profile": prof}

            return JSONResponse(await asyncio.to_thread(_load_prof))

        @app.post("/api/executive/tasks")
        async def api_executive_tasks(request: Request) -> JSONResponse:
            try:
                body = await request.json()
            except Exception:
                body = {}
            action_kind = str(body.get("action", "add")).lower() if isinstance(body, dict) else "add"
            title = str(body.get("title", "")).strip() if isinstance(body, dict) else ""
            if self._memory is None:
                return JSONResponse({"ok": False, "error": "Memory nicht verbunden"}, status_code=503)
            if action_kind == "complete":
                ok = await asyncio.to_thread(self._memory.complete_open_task, title)
                return JSONResponse({"ok": bool(ok), "open_tasks": self._memory.get_open_tasks()})
            if not title:
                return JSONResponse({"ok": False, "error": "Titel fehlt"}, status_code=400)
            task = await asyncio.to_thread(self._memory.add_open_task, title)
            return JSONResponse({"ok": True, "task": task, "open_tasks": self._memory.get_open_tasks()})

        @app.get("/api/executive/suggestion")
        async def api_executive_suggestion_get() -> JSONResponse:
            from executive_assistant import EXECUTIVE_ASSISTANT

            return JSONResponse({"ok": True, "suggestion": EXECUTIVE_ASSISTANT.suggestions.get_current_suggestion()})

        @app.post("/api/executive/suggestion/confirm")
        async def api_executive_suggestion_confirm() -> JSONResponse:
            def _confirm() -> dict[str, Any]:
                from executive_assistant import EXECUTIVE_ASSISTANT

                return EXECUTIVE_ASSISTANT.suggestions.confirm_suggestion(
                    assistant=EXECUTIVE_ASSISTANT,
                    brain=self._brain,
                    on_status=lambda m: self.add_log(m),
                )

            res = await asyncio.to_thread(_confirm)
            if res.get("spoken"):
                self.set_jarvis_text(str(res["spoken"]))
            self._broadcast({"type": "smart_suggestion", "suggestion": None})
            return JSONResponse(res)

        @app.post("/api/executive/suggestion/dismiss")
        async def api_executive_suggestion_dismiss() -> JSONResponse:
            from executive_assistant import EXECUTIVE_ASSISTANT

            res = await asyncio.to_thread(EXECUTIVE_ASSISTANT.suggestions.dismiss_suggestion)
            self._broadcast({"type": "smart_suggestion", "suggestion": None})
            return JSONResponse(res)

        @app.get("/api/license/status")
        async def api_license_status() -> JSONResponse:
            from license_manager import LICENSE_MANAGER

            return JSONResponse({"ok": True, "license": LICENSE_MANAGER.get_status()})

        @app.post("/api/license/activate")
        async def api_license_activate(request: Request) -> JSONResponse:
            try:
                body = await request.json()
            except Exception:
                body = {}
            key = str(body.get("key", "")).strip() if isinstance(body, dict) else ""
            user_name = str(body.get("user_name", "")).strip() if isinstance(body, dict) else ""
            from license_manager import LICENSE_MANAGER

            res = await asyncio.to_thread(LICENSE_MANAGER.activate_key, key, user_name)
            if res.get("ok"):
                self.add_log(f"🔐 Lizenz aktiviert: {res['status'].get('tier')} ({res['status'].get('user_name')})")
                self._broadcast({"type": "license_status", "license": res["status"]})
            return JSONResponse(res, status_code=200 if res.get("ok") else 400)

        @app.post("/api/license/trial")
        async def api_license_trial(request: Request) -> JSONResponse:
            try:
                body = await request.json()
            except Exception:
                body = {}
            user_name = str(body.get("user_name", "")).strip() if isinstance(body, dict) else ""
            from license_manager import LICENSE_MANAGER

            res = await asyncio.to_thread(LICENSE_MANAGER.start_trial, user_name)
            if res.get("ok"):
                self.add_log(f"⏱️ 7-Tage-Beta-Testmodus gestartet ({res['status'].get('user_name')})")
                self._broadcast({"type": "license_status", "license": res["status"]})
            return JSONResponse(res, status_code=200 if res.get("ok") else 400)

        @app.post("/api/license/audio_check")
        async def api_license_audio_check() -> JSONResponse:
            def _check_audio() -> dict[str, Any]:
                mic_ok = False
                mic_name = "Kein Eingabegerät erkannt"
                speaker_ok = False
                try:
                    import sounddevice as sd

                    devs = sd.query_devices()
                    in_devs = [d for d in devs if int(d.get("max_input_channels", 0)) > 0]
                    out_devs = [d for d in devs if int(d.get("max_output_channels", 0)) > 0]
                    if in_devs:
                        mic_ok = True
                        mic_name = str(in_devs[0].get("name") or "Standard-Mikrofon")
                    speaker_ok = bool(out_devs)
                except Exception as exc:
                    mic_name = f"Audio-Prüfung Hinweis: {exc}"

                if self._tts_engine is not None and hasattr(self._tts_engine, "speak"):
                    try:
                        threading.Thread(
                            target=self._tts_engine.speak,
                            args=("Audio und Mikrofon-System sind einsatzbereit.",),
                            daemon=True,
                        ).start()
                    except Exception:
                        pass

                return {
                    "ok": True,
                    "mic_ok": mic_ok,
                    "mic_name": mic_name,
                    "speaker_ok": speaker_ok,
                    "message": f"Mikrofon: {'OK' if mic_ok else 'Nicht gefunden'} ({mic_name}) | Lautsprecher: {'OK' if speaker_ok else 'Hinweis'}",
                }

            return JSONResponse(await asyncio.to_thread(_check_audio))

        @app.post("/api/beta/feedback")
        async def api_beta_feedback(request: Request) -> JSONResponse:
            try:
                body = await request.json()
            except Exception:
                body = {}
            category = str(body.get("category", "feedback")).strip() if isinstance(body, dict) else "feedback"
            message = str(body.get("message", "")).strip() if isinstance(body, dict) else ""
            contact = str(body.get("contact", "")).strip() if isinstance(body, dict) else ""
            from license_manager import LICENSE_MANAGER

            res = await asyncio.to_thread(LICENSE_MANAGER.submit_feedback, message, category, contact)
            if res.get("ok"):
                self.add_log(f"💬 Beta-Feedback gespeichert: {res.get('ticket_id')}")
            return JSONResponse(res, status_code=200 if res.get("ok") else 400)

        @app.post("/api/video/auto_edit")
        async def api_video_auto_edit(request: Request) -> JSONResponse:
            try:
                body = await request.json()
            except Exception:
                body = {}
            input_path = str(body.get("input_path", "")).strip() if isinstance(body, dict) else ""
            output_path = str(body.get("output_path", "")).strip() if isinstance(body, dict) else ""
            open_in_editor = str(body.get("open_in_editor", "")).strip() if isinstance(body, dict) else ""
            res = await asyncio.to_thread(
                _get_swarm_core().desktop_video.auto_edit_video,
                input_path,
                output_path,
                -32.0,
                0.35,
                open_in_editor,
            )
            return JSONResponse(res)

        @app.get("/api/trading/portfolio")
        async def api_trading_portfolio() -> JSONResponse:
            res = await asyncio.to_thread(lambda: _get_trading_engine().get_portfolio_status())
            return JSONResponse(res)

        @app.post("/api/trading/analyze")
        async def api_trading_analyze(request: Request) -> JSONResponse:
            body = await request.json()
            symbol = str(body.get("symbol", "NVDA")).strip() if isinstance(body, dict) else "NVDA"
            res = await asyncio.to_thread(_get_trading_engine().analyze_stock, symbol)
            return JSONResponse(res)

        @app.post("/api/trading/order")
        async def api_trading_order(request: Request) -> JSONResponse:
            body = await request.json()
            if not isinstance(body, dict):
                return JSONResponse({"ok": False, "error": "Ungültiges JSON"}, status_code=400)
            symbol = str(body.get("symbol", "")).strip()
            qty_or_amount = float(body.get("qty_or_amount_eur", 100.0))
            side = str(body.get("side", "buy")).strip().lower()
            order_type = str(body.get("order_type", "market")).strip().lower()
            force_confirm = bool(body.get("confirmed_by_user", False))
            auto_ag = _get_autonomous_agent()
            auto_ag.set_external_step(f"[TRADING] {side.upper()} {symbol} ({qty_or_amount}) @ {order_type.upper()}")
            res = await asyncio.to_thread(
                _get_trading_engine().place_order,
                symbol,
                qty_or_amount,
                side,
                order_type,
                None,
                force_confirm,
            )
            auto_ag.clear_external_step(res.get("message", "Order verarbeitet."))
            return JSONResponse(res)

        @app.post("/api/action")
        async def api_action(request: Request) -> JSONResponse:
            body = await request.json()
            action = str(body.get("action", "")).lower() if isinstance(body, dict) else ""

            if action == "speak":
                if self._on_manual_trigger is not None:
                    await asyncio.to_thread(self._on_manual_trigger)
                return JSONResponse({"ok": True})

            if action == "toggle_cam":
                def _do_toggle_cam() -> bool:
                    if self._on_camera_toggle is not None:
                        return bool(self._on_camera_toggle())
                    p = not self._camera_paused
                    if self._on_camera_check is not None:
                        self._on_camera_check()
                    return p

                paused = await asyncio.to_thread(_do_toggle_cam)
                v_status = VISION_STATUS_PASSIVE if paused else "WEBCAM: MANUELLER LIVE-STREAM AKTIV"
                with self._state_lock:
                    self._camera_paused = paused
                    self._vision_status = v_status
                self.add_log(
                    "Webcam im On-Demand-Standby (0% CPU)."
                    if paused
                    else "Kamera-Live-Stream manuell aktiviert."
                )
                self._broadcast(
                    {
                        "type": "camera_paused",
                        "paused": paused,
                        "active": not paused,
                        "vision_status": v_status,
                    }
                )
                return JSONResponse(
                    {
                        "ok": True,
                        "camera_paused": paused,
                        "active": not paused,
                        "vision_status": v_status,
                    }
                )

            if action == "toggle_mic":
                def _do_toggle_mic() -> bool:
                    if self._on_mic_mute_toggle is not None:
                        return bool(self._on_mic_mute_toggle())
                    return not self._mic_muted

                muted = await asyncio.to_thread(_do_toggle_mic)
                with self._state_lock:
                    self._mic_muted = muted
                    if muted:
                        self._audio_level = 0.0
                        self._vad_active = False
                        self._live_state["audio_level"] = 0.0
                        self._live_state["level_pct"] = 0
                        self._live_state["vad_active"] = False
                self.add_log("Mikrofon stummgeschaltet." if muted else "Mikrofon wieder aktiv.")
                self._broadcast({"type": "mic_muted", "muted": muted})
                return JSONResponse({"ok": True, "mic_muted": muted})

            quick_prompts = {
                "check_camera": "Schau bitte durch die Kamera und sag mir kurz, was du auf dem Arbeitstisch siehst.",
                "alarm_status": "Welche Wecker sind aktuell eingestellt?",
                "system_status": "Gib mir einen kurzen Statusbericht zu System, Netzwerk und Aufgaben.",
            }
            if action in quick_prompts:
                cmd_text = quick_prompts[action]
                self.add_log(f"Quick-Action ausgelöst: {action}")
                if self._on_quick_command is not None:
                    threading.Thread(
                        target=self._on_quick_command,
                        args=(cmd_text,),
                        daemon=True,
                    ).start()
                elif self._on_manual_trigger is not None:
                    threading.Thread(target=self._on_manual_trigger, daemon=True).start()
                return JSONResponse({"ok": True, "action": action})

            if action == "minimize":
                await asyncio.to_thread(self.minimize_to_background)
                return JSONResponse({"ok": True, "minimized": True})

            if action == "restore":
                await asyncio.to_thread(self.restore_from_background)
                return JSONResponse({"ok": True, "minimized": False})

            if action == "fullscreen":
                await asyncio.to_thread(self._toggle_fullscreen_window)
                return JSONResponse({"ok": True})

            if action in ("record_workflow_start", "record_workflow_stop", "record_workflow_toggle"):
                from skill_recorder import SKILL_RECORDER

                sk_name = str(body.get("skill_name", "") or "").strip() if isinstance(body, dict) else ""
                ap_name = str(body.get("app_name", "") or "").strip() if isinstance(body, dict) else ""
                if action == "record_workflow_start":
                    rec_res = await asyncio.to_thread(SKILL_RECORDER.start_recording, sk_name, ap_name)
                elif action == "record_workflow_stop":
                    rec_res = await asyncio.to_thread(SKILL_RECORDER.stop_recording, sk_name, ap_name)
                else:
                    if SKILL_RECORDER.is_recording():
                        rec_res = await asyncio.to_thread(SKILL_RECORDER.stop_recording, sk_name, ap_name)
                    else:
                        rec_res = await asyncio.to_thread(SKILL_RECORDER.start_recording, sk_name, ap_name)
                self._broadcast({"type": "skill_recorder_status", **SKILL_RECORDER.get_status()})
                return JSONResponse(rec_res)

            if action == "exit":
                threading.Thread(target=self._handle_close, daemon=True).start()
                return JSONResponse({"ok": True})

            return JSONResponse({"ok": False, "error": f"Unbekannte Aktion: {action}"}, status_code=400)

        @app.get("/api/skill_recorder/status")
        async def api_skill_recorder_status() -> JSONResponse:
            from skill_recorder import SKILL_RECORDER

            return JSONResponse(SKILL_RECORDER.get_status())

        @app.post("/api/skill_recorder/start")
        async def api_skill_recorder_start(request: Request) -> JSONResponse:
            from skill_recorder import SKILL_RECORDER

            try:
                body = await request.json()
            except Exception:
                body = {}
            sk_name = str(body.get("skill_name", "") or "").strip() if isinstance(body, dict) else ""
            ap_name = str(body.get("app_name", "") or "").strip() if isinstance(body, dict) else ""
            res = await asyncio.to_thread(SKILL_RECORDER.start_recording, sk_name, ap_name)
            self._broadcast({"type": "skill_recorder_status", **SKILL_RECORDER.get_status()})
            return JSONResponse(res)

        @app.post("/api/skill_recorder/stop")
        async def api_skill_recorder_stop(request: Request) -> JSONResponse:
            from skill_recorder import SKILL_RECORDER

            try:
                body = await request.json()
            except Exception:
                body = {}
            sk_name = str(body.get("skill_name", "") or "").strip() if isinstance(body, dict) else ""
            ap_name = str(body.get("app_name", "") or "").strip() if isinstance(body, dict) else ""
            res = await asyncio.to_thread(SKILL_RECORDER.stop_recording, sk_name, ap_name)
            self._broadcast({"type": "skill_recorder_status", **SKILL_RECORDER.get_status()})
            return JSONResponse(res)

        @app.post("/api/skill_recorder/replay")
        async def api_skill_recorder_replay(request: Request) -> JSONResponse:
            from skill_recorder import SKILL_RECORDER

            try:
                body = await request.json()
            except Exception:
                body = {}
            query = str(body.get("skill_query", body.get("skill_name", "")) or "").strip() if isinstance(body, dict) else ""
            speed = float(body.get("speed_multiplier", 10.0) or 10.0) if isinstance(body, dict) else 10.0
            res = await asyncio.to_thread(SKILL_RECORDER.replay_skill, query, speed)
            return JSONResponse(res)

        @app.get("/api/monitors")
        async def api_monitors() -> JSONResponse:
            mons = await asyncio.to_thread(self.refresh_monitors)
            return JSONResponse(
                {
                    "ok": True,
                    "count": len(mons),
                    "monitors": mons,
                    "active_monitor": self._brain_activity.get("active_monitor"),
                }
            )

        @app.get("/api/monitor_preview/{target}.jpg")
        async def api_monitor_preview_jpg(target: str) -> Response:
            def _grab() -> bytes:
                try:
                    from multi_monitor import MULTI_MONITOR_STREAMER, capture_monitor_jpeg

                    if str(target).isdigit():
                        cached = MULTI_MONITOR_STREAMER.get_cached_frame(int(target))
                        if cached:
                            return cached

                    res = capture_monitor_jpeg(
                        target=target,
                        quality=64,
                        max_width=1080 if target == "all" else 680,
                        with_grid=False,
                    )
                    return bytes(res.get("jpeg_bytes") or b"")
                except Exception:
                    return b""

            jpeg_bytes = await asyncio.to_thread(_grab)
            if not jpeg_bytes:
                return Response(status_code=204, headers={"Cache-Control": "no-store, max-age=0"})
            return Response(
                content=jpeg_bytes,
                media_type="image/jpeg",
                headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
            )

        @app.get("/api/monitor_stream/{target}.mjpeg")
        async def api_monitor_stream_mjpeg(target: str) -> StreamingResponse:
            from multi_monitor import MULTI_MONITOR_STREAMER, capture_monitor_jpeg

            MULTI_MONITOR_STREAMER.touch_viewer()

            async def _gen():
                boundary = b"--frame\r\nContent-Type: image/jpeg\r\n"
                while not self._stop_event.is_set():
                    if BETA_GUARD.is_expired():
                        break
                    if self._window_minimized:
                        await asyncio.sleep(0.3)
                        continue
                    jpeg: Optional[bytes] = None
                    if str(target).isdigit():
                        jpeg = MULTI_MONITOR_STREAMER.get_cached_frame(int(target))
                    if not jpeg:
                        res = await asyncio.to_thread(capture_monitor_jpeg, target, 62, 680, False)
                        jpeg = bytes(res.get("jpeg_bytes") or b"") if res.get("ok") else None
                    if jpeg:
                        yield boundary + f"Content-Length: {len(jpeg)}\r\n\r\n".encode("ascii") + jpeg + b"\r\n"
                    fps = max(1.0, min(15.0, MULTI_MONITOR_STREAMER.get_fps()))
                    await asyncio.sleep(1.0 / fps)

            return StreamingResponse(_gen(), media_type="multipart/x-mixed-replace; boundary=frame")

        @app.websocket("/ws/monitors")
        async def ws_monitors(websocket: WebSocket) -> None:
            """
            Binärer Low-Latency WebSocket für Live-Frames aller 3 Monitore.
            Aktiver Maus-Monitor läuft mit 15+ FPS, Hintergrund-Monitore gedrosselt mit ~5 FPS.
            Header (6 Bytes): 'JM' | uint8 mon_idx | uint8 flags ((cursor<<1)|scan) | uint16 fps*10
            """
            import struct
            from beta_guard import BETA_GUARD
            from multi_monitor import MULTI_MONITOR_STREAMER

            await websocket.accept()
            if BETA_GUARD.is_expired():
                try:
                    await websocket.send_json(BETA_GUARD.get_api_expired_payload())
                except Exception:
                    pass
                try:
                    await websocket.close()
                except Exception:
                    pass
                return
            MULTI_MONITOR_STREAMER.touch_viewer()
            last_mon_seqs: dict[int, int] = {}
            try:
                while not self._stop_event.is_set():
                    if BETA_GUARD.is_expired():
                        await websocket.send_json(BETA_GUARD.get_api_expired_payload())
                        await websocket.close()
                        break
                    _, frames, mon_seqs, cursor_mon, actual_fps = MULTI_MONITOR_STREAMER.get_latest_bundle()
                    if frames:
                        act_mon = self._brain_activity.get("active_monitor")
                        is_scan = str(self._brain_activity.get("state", "")).lower() == "screen_scan"
                        fps_x10 = int(round(max(0.0, min(650.0, actual_fps)) * 10.0))
                        for mon_idx, jpg in frames.items():
                            m_seq = mon_seqs.get(mon_idx, 0)
                            if last_mon_seqs.get(mon_idx) == m_seq:
                                continue
                            last_mon_seqs[mon_idx] = m_seq
                            scan_flag = 1 if (is_scan and (act_mon == "all" or act_mon == mon_idx)) else 0
                            cursor_flag = 1 if int(cursor_mon) == int(mon_idx) else 0
                            flags = (cursor_flag << 1) | scan_flag
                            hdr = b"JM" + struct.pack("<BBH", int(mon_idx) & 0xFF, flags, fps_x10)
                            await asyncio.wait_for(websocket.send_bytes(hdr + jpg), timeout=0.4)
                    fps = max(1.0, min(20.0, MULTI_MONITOR_STREAMER.get_fps()))
                    await asyncio.sleep(max(0.025, 0.8 / fps))
            except (WebSocketDisconnect, Exception):
                pass

        @app.post("/api/windows/bring_here")
        async def api_windows_bring_here(request: Request) -> JSONResponse:
            payload = await request.json() if request else {}
            from multi_monitor import bring_last_window_to_cursor_monitor

            res = await asyncio.to_thread(
                bring_last_window_to_cursor_monitor,
                str((payload or {}).get("window_title", "") or ""),
            )
            return JSONResponse(res)

        @app.post("/api/windows/open")
        async def api_windows_open(request: Request) -> JSONResponse:
            payload = await request.json()
            if not isinstance(payload, dict):
                return JSONResponse({"ok": False, "error": "Ungültiges JSON"}, status_code=400)
            from macro_engine import open_window

            res = await asyncio.to_thread(
                open_window,
                str(payload.get("window", payload.get("panel", "screen-matrix")) or "screen-matrix"),
                payload.get("monitor"),
            )
            return JSONResponse(res)

        @app.post("/api/monitors/highlight")
        async def api_monitors_highlight(request: Request) -> JSONResponse:
            payload = await request.json()
            if not isinstance(payload, dict):
                return JSONResponse({"ok": False, "error": "Ungültiges JSON"}, status_code=400)
            from multi_monitor import highlight_monitor_region

            res = await asyncio.to_thread(
                highlight_monitor_region,
                payload.get("monitor", 1),
                float(payload.get("x_pct", 0.0)),
                float(payload.get("y_pct", 0.0)),
                float(payload.get("w_pct", 1.0)),
                float(payload.get("h_pct", 1.0)),
                str(payload.get("label", "JARVIS SCAN") or "JARVIS SCAN"),
                str(payload.get("color", "#f59e0b") or "#f59e0b"),
                float(payload.get("duration", 2.4)),
            )
            return JSONResponse(res)

        @app.post("/api/monitors/ocr")
        async def api_monitors_ocr(request: Request) -> JSONResponse:
            payload = await request.json()
            if not isinstance(payload, dict):
                return JSONResponse({"ok": False, "error": "Ungültiges JSON"}, status_code=400)
            from macro_engine import read_monitor_ocr

            res = await asyncio.to_thread(
                read_monitor_ocr,
                payload.get("monitor", 1),
                str(payload.get("prompt", "Lies den sichtbaren Text und die wichtigsten Inhalte aus.") or ""),
                self._brain,
            )
            return JSONResponse(res)

        @app.post("/api/monitors/stream_config")
        async def api_monitors_stream_config(request: Request) -> JSONResponse:
            payload = await request.json()
            from multi_monitor import MULTI_MONITOR_STREAMER

            fps = MULTI_MONITOR_STREAMER.set_fps(float((payload or {}).get("fps", 12.0)))
            return JSONResponse({"ok": True, "fps": fps})

        @app.post("/api/macros/run")
        async def api_macros_run(request: Request) -> JSONResponse:
            payload = await request.json()
            if not isinstance(payload, dict):
                return JSONResponse({"ok": False, "error": "Ungültiges JSON"}, status_code=400)
            from macro_engine import MACRO_ENGINE

            macro = str(payload.get("macro", "work_mode") or "work_mode").strip().lower()
            if macro in ("focus", "focus_monitor"):
                res = await asyncio.to_thread(MACRO_ENGINE.focus_monitor, payload.get("monitor", 2), self._brain)
            else:
                res = await asyncio.to_thread(MACRO_ENGINE.start_work_mode)
            return JSONResponse(res)

        @app.get("/api/system/processes")
        async def api_system_processes() -> JSONResponse:
            from multi_monitor import get_monitor_window_telemetry, get_system_processes

            win_tel = await asyncio.to_thread(get_monitor_window_telemetry, True)
            procs = await asyncio.to_thread(get_system_processes, 10)
            return JSONResponse({"ok": True, **win_tel, "processes": procs})

        @app.get("/api/windows/list")
        async def api_windows_list() -> JSONResponse:
            from multi_monitor import list_open_windows

            wins = await asyncio.to_thread(list_open_windows)
            return JSONResponse({"ok": True, "windows": wins[:30]})

        @app.post("/api/windows/popout")
        async def api_windows_popout(request: Request) -> JSONResponse:
            payload = await request.json()
            if not isinstance(payload, dict):
                return JSONResponse({"ok": False, "error": "Ungültiges JSON"}, status_code=400)
            panel = str(payload.get("panel", "logs") or "logs").strip().lower()
            monitor = payload.get("monitor", 1)
            res = await asyncio.to_thread(self.open_subwindow_on_monitor, panel, monitor)
            return JSONResponse(res)

        @app.post("/api/windows/move")
        async def api_windows_move(request: Request) -> JSONResponse:
            payload = await request.json()
            if not isinstance(payload, dict):
                return JSONResponse({"ok": False, "error": "Ungültiges JSON"}, status_code=400)
            from multi_monitor import move_window_to_monitor

            res = await asyncio.to_thread(
                move_window_to_monitor,
                str(payload.get("window_title", "") or ""),
                payload.get("monitor", 1),
                str(payload.get("layout", "maximize") or "maximize"),
            )
            return JSONResponse(res)

        @app.post("/api/brain/demo")
        async def api_brain_demo(request: Request) -> JSONResponse:
            payload = await request.json()
            if not isinstance(payload, dict):
                return JSONResponse({"ok": False, "error": "Ungültiges JSON"}, status_code=400)
            state = str(payload.get("state", "thinking") or "thinking").strip().lower()
            intensity = float(payload.get("intensity", 0.85) or 0.85)
            mon = payload.get("active_monitor")
            detail = str(payload.get("detail", "") or "").strip()
            self.emit_brain_activity(state=state, intensity=intensity, active_monitor=mon, detail=detail)
            if payload.get("thought"):
                self.emit_neural_thought(
                    category=str(payload.get("category", "thought") or "thought"),
                    tool=str(payload.get("tool", "demo_show") or "demo_show"),
                    summary=str(payload.get("thought")),
                    code=str(payload.get("code", "") or ""),
                    monitor=mon,
                )
            return JSONResponse({"ok": True, "brain_activity": self._brain_activity})

        @app.get("/api/n8n/status")
        async def api_n8n_status() -> JSONResponse:
            from n8n_architect import N8N_ARCHITECT

            return JSONResponse(await asyncio.to_thread(N8N_ARCHITECT.get_status))

        @app.post("/api/n8n/create")
        async def api_n8n_create(request: Request) -> JSONResponse:
            payload = await request.json()
            if not isinstance(payload, dict):
                return JSONResponse({"ok": False, "error": "Ungültiges JSON"}, status_code=400)
            from n8n_architect import N8N_ARCHITECT

            res = await asyncio.to_thread(
                N8N_ARCHITECT.create_workflow,
                str(payload.get("goal", "") or ""),
                str(payload.get("name", "") or ""),
                bool(payload.get("activate", True)),
                self._brain,
            )
            return JSONResponse(res)

        @app.post("/api/n8n/manage")
        async def api_n8n_manage(request: Request) -> JSONResponse:
            payload = await request.json()
            if not isinstance(payload, dict):
                return JSONResponse({"ok": False, "error": "Ungültiges JSON"}, status_code=400)
            from n8n_architect import N8N_ARCHITECT

            res = await asyncio.to_thread(
                N8N_ARCHITECT.list_and_manage,
                str(payload.get("action", "list") or "list"),
                str(payload.get("workflow_id", "") or ""),
            )
            return JSONResponse(res)

        @app.post("/api/n8n/run")
        async def api_n8n_run(request: Request) -> JSONResponse:
            payload = await request.json()
            if not isinstance(payload, dict):
                return JSONResponse({"ok": False, "error": "Ungültiges JSON"}, status_code=400)
            from n8n_architect import N8N_ARCHITECT

            res = await asyncio.to_thread(
                N8N_ARCHITECT.run_workflow,
                str(payload.get("target", "") or ""),
                payload.get("payload") if isinstance(payload.get("payload"), dict) else None,
            )
            return JSONResponse(res)

        @app.get("/api/live_desktop_preview.jpg")
        async def api_live_desktop_preview_jpg() -> Response:
            """Liefert das aktuelle annotierte Desktop-Vision-Bild (960x540, Qualität 65) ohne WebSocket-Ballast."""
            def _grab_preview() -> bytes:
                try:
                    from agent_hand import DESKTOP_VISION_STREAM

                    return DESKTOP_VISION_STREAM.get_preview_jpeg(
                        max_width=960,
                        max_height=540,
                        quality=65,
                    )
                except Exception:
                    return b""

            jpeg_bytes = await asyncio.to_thread(_grab_preview)
            if not jpeg_bytes:
                return Response(
                    status_code=204,
                    headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
                )
            return Response(
                content=jpeg_bytes,
                media_type="image/jpeg",
                headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
            )

        @app.get("/api/camera.mjpeg")
        async def api_camera_mjpeg() -> StreamingResponse:
            async def _frame_gen():
                boundary = b"--frame\r\nContent-Type: image/jpeg\r\n"
                while not self._stop_event.is_set():
                    if BETA_GUARD.is_expired():
                        break
                    if self._camera_paused or self._window_minimized:
                        await asyncio.sleep(0.5)
                        continue
                    jpeg = None
                    if self._frame_provider is not None:
                        try:
                            jpeg = await asyncio.to_thread(
                                lambda: _encode_bgr_jpeg(self._frame_provider(), quality=76)
                            )
                        except Exception:
                            jpeg = None
                    if jpeg is None:
                        with self._state_lock:
                            jpeg = self._latest_jpeg
                    if jpeg:
                        yield boundary + f"Content-Length: {len(jpeg)}\r\n\r\n".encode("ascii") + jpeg + b"\r\n"
                    await asyncio.sleep(0.065)

            return StreamingResponse(
                _frame_gen(),
                media_type="multipart/x-mixed-replace; boundary=frame",
            )

        @app.websocket("/ws")
        async def ws_endpoint(websocket: WebSocket) -> None:
            from beta_guard import BETA_GUARD

            await websocket.accept()
            if BETA_GUARD.is_expired():
                try:
                    await websocket.send_json(BETA_GUARD.get_api_expired_payload())
                except Exception:
                    pass
                try:
                    await websocket.close()
                except Exception:
                    pass
                return
            self._active_websockets.add(websocket)
            try:
                snap = await asyncio.to_thread(self._build_snapshot)
                safe_pkt = self._sanitize_ws_payload({"type": "snapshot", "data": snap}, max_bytes=2000)
                await asyncio.wait_for(
                    websocket.send_json(safe_pkt),
                    timeout=0.5,
                )
                while not self._stop_event.is_set():
                    if BETA_GUARD.is_expired():
                        await websocket.send_json(BETA_GUARD.get_api_expired_payload())
                        await websocket.close()
                        break
                    raw_msg = await websocket.receive_text()
                    if not raw_msg or not raw_msg.strip().startswith("{"):
                        continue
                    try:
                        ws_cmd = json.loads(raw_msg)
                    except Exception:
                        continue
                    if not isinstance(ws_cmd, dict):
                        continue
                    cmd_type = str(ws_cmd.get("type", "") or "").lower()
                    if cmd_type == "brain_activity":
                        self.emit_brain_activity(
                            state=str(ws_cmd.get("state", "idle") or "idle"),
                            intensity=float(ws_cmd.get("intensity", 0.5) or 0.5),
                            active_monitor=ws_cmd.get("active_monitor"),
                            detail=str(ws_cmd.get("detail", "") or ""),
                        )
                    elif cmd_type == "refresh_monitors":
                        await asyncio.to_thread(self.refresh_monitors)
            except (WebSocketDisconnect, Exception):
                pass
            finally:
                self._active_websockets.discard(websocket)

        return app

    def _build_snapshot(self) -> dict[str, Any]:
        import sys as _sys

        agent_monitor_state: dict[str, Any] = {}
        if "swarm_core" in _sys.modules:
            try:
                agent_monitor_state = _get_swarm_core().get_swarm_state()
            except Exception:
                agent_monitor_state = {}
        smart_suggestion = None
        if "executive_assistant" in _sys.modules:
            try:
                smart_suggestion = _sys.modules["executive_assistant"].EXECUTIVE_ASSISTANT.suggestions.get_current_suggestion()
            except Exception:
                smart_suggestion = None
        beta_guard_state = None
        try:
            from beta_guard import BETA_GUARD

            beta_guard_state = BETA_GUARD.get_status()
        except Exception:
            beta_guard_state = None
        license_state = None
        try:
            from license_manager import LICENSE_MANAGER

            license_state = LICENSE_MANAGER.get_status()
        except Exception:
            beta_guard_state = None
        try:
            from beta_guard import BETA_GUARD

            beta_guard_state = BETA_GUARD.get_status()
        except Exception:
            beta_guard_state = None
        license_state = None
        n8n_state = None
        try:
            from n8n_architect import N8N_ARCHITECT

            n8n_state = N8N_ARCHITECT.get_status()
        except Exception:
            n8n_state = None
        with self._state_lock:
            eng, color, sub = STATUS_META.get(self._status, ("READY", "#00d4ff", "STANDBY"))
            return {
                "status": self._status,
                "status_eng": eng,
                "status_color": color,
                "status_sub": sub,
                "is_online": self._is_online,
                "mode": self._mode,
                "active_provider": get_active_provider(),
                "model_priority": get_model_priority(),
                "model_label": self._model_label,
                "voice_label": self._voice_label,
                "response_time_s": self._response_time_s,
                "user_text": self._user_text,
                "jarvis_text": self._jarvis_text,
                "camera_paused": self._camera_paused,
                "camera_active": not self._camera_paused,
                "vision_status": self._vision_status,
                "mic_muted": self._mic_muted,
                "audio_level": self._audio_level,
                "vad_active": self._vad_active,
                "speaking_active": self._speaking_active,
                "telemetry": dict(self._telemetry),
                "seq": self._seq,
                "dialog": list(self._dialog_history),
                "logs": list(self._activity_lines),
                "autostart_enabled": get_autostart_status(),
                "agent_monitor": agent_monitor_state,
                "smart_suggestion": smart_suggestion,
                "license": license_state,
                "beta_guard": beta_guard_state,
                "monitors": list(self._monitors),
                "brain_activity": dict(self._brain_activity),
                "neural_thoughts": list(self._neural_thoughts)[-24:],
                "n8n": n8n_state,
                "telemetry_events": self._get_recent_ticker_events(24),
            }

    @staticmethod
    def _get_recent_ticker_events(limit: int = 24) -> list[dict[str, Any]]:
        try:
            from macro_engine import EVENT_TICKER

            return EVENT_TICKER.recent(limit)
        except Exception:
            return []

    def _broadcast(self, payload: dict[str, Any]) -> None:
        if not isinstance(payload, dict):
            return
        safe_payload = self._sanitize_ws_payload(payload, max_bytes=2000)
        if not safe_payload:
            return
        try:
            self._ws_event_queue.put_nowait(safe_payload)
        except queue.Full:
            try:
                self._ws_event_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._ws_event_queue.put_nowait(safe_payload)
            except queue.Full:
                pass

    # ------------------------------------------------------------------
    # Entkoppelter Telemetrie-Thread (schreibt nur in Queue, niemals direkt in WebSocket)
    # ------------------------------------------------------------------
    def _telemetry_loop(self) -> None:
        from multi_monitor import MULTI_MONITOR_STREAMER, get_monitor_window_telemetry, get_system_processes
        from tools import get_live_telemetry

        proc_tick = 0
        cached_procs: list[dict[str, Any]] = []
        while not self._stop_event.is_set():
            if self._window_minimized:
                self._stop_event.wait(3.0)
                continue
            try:
                data = get_live_telemetry()
                win_tel = get_monitor_window_telemetry()
                proc_tick += 1
                if proc_tick % 2 == 1 or not cached_procs:
                    cached_procs = get_system_processes(6)
                data["mouse"] = win_tel.get("mouse")
                data["active_by_monitor"] = win_tel.get("active_by_monitor")
                data["streamer_fps"] = MULTI_MONITOR_STREAMER.get_fps()
                data["processes"] = cached_procs
                with self._state_lock:
                    self._telemetry = data
                    self._seq += 1
                    seq = self._seq
                self._broadcast({"type": "telemetry", "data": data, "seq": seq})
            except Exception:
                pass
            self._stop_event.wait(1.2)

    def _start_backend_server(self) -> None:
        config = uvicorn.Config(
            self.app,
            host="0.0.0.0",
            port=self.port,
            log_level="warning",
            access_log=False,
            use_colors=False,
        )
        self._server = uvicorn.Server(config)
        self._server_thread = threading.Thread(target=self._server.run, daemon=True)
        self._server_thread.start()

        threading.Thread(target=self._telemetry_loop, daemon=True).start()

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(f"{self.url}/api/state", timeout=0.4) as resp:
                    if int(getattr(resp, "status", 200)) == 200:
                        return
            except Exception:
                time.sleep(0.1)

    # ------------------------------------------------------------------
    # Thread-sichere öffentliche Update-Methoden für main.py & agents.py
    # ------------------------------------------------------------------
    def _handle_external_online_change(self, enabled: bool) -> None:
        self.update_online_switch(enabled)

    def emit_brain_activity(
        self,
        state: str = "idle",
        intensity: float = 0.4,
        active_monitor: Any = None,
        detail: str = "",
    ) -> None:
        """Steuert die 3D-Neural-Gehirn-Animation synchron über WebSocket für alle offenen Fenster."""
        clean_state = str(state or "idle").strip().lower()
        clamped = max(0.0, min(1.0, float(intensity)))
        with self._state_lock:
            self._seq += 1
            pkt = {
                "type": "brain_activity",
                "state": clean_state,
                "intensity": clamped,
                "active_monitor": active_monitor,
                "detail": str(detail or "")[:80],
                "seq": self._seq,
            }
            self._brain_activity = {
                "state": pkt["state"],
                "intensity": pkt["intensity"],
                "active_monitor": pkt["active_monitor"],
                "detail": pkt["detail"],
                "seq": pkt["seq"],
            }
        self._broadcast(pkt)

    def emit_neural_thought(
        self,
        category: str = "thought",
        summary: str = "",
        tool: str = "",
        code: str = "",
        monitor: Any = None,
    ) -> None:
        """Sendet einen kompakten KI-Gedanken-/Tool-Schritt live an Sub-Fenster A (Neural Logs)."""
        ts = datetime.now().strftime("%H:%M:%S")
        entry = {
            "category": str(category or "thought")[:20],
            "tool": str(tool or "")[:40],
            "summary": str(summary or "")[:360],
            "code": str(code or "")[:480],
            "monitor": monitor,
            "ts": ts,
        }
        with self._state_lock:
            self._neural_thoughts.append(entry)
        self._broadcast({"type": "neural_thought", **entry})

    def refresh_monitors(self, active_monitor: Any = None) -> list[dict[str, Any]]:
        """Aktualisiert die erkannten Monitore und informiert alle HUD-Fenster in Echtzeit."""
        try:
            from multi_monitor import get_connected_monitors

            mons = get_connected_monitors()
        except Exception:
            mons = []
        with self._state_lock:
            self._monitors = mons
            act = active_monitor if active_monitor is not None else self._brain_activity.get("active_monitor")
        self._broadcast(
            {
                "type": "monitor_topology",
                "monitors": mons,
                "active_monitor": act,
                "updated_at": datetime.now().strftime("%H:%M:%S"),
            }
        )
        return mons

    def open_subwindow_on_monitor(self, panel: str = "logs", monitor_target: Any = 1) -> dict[str, Any]:
        """Öffnet ein eigenständiges HUD-Subfenster (neural-core | screen-matrix | system-telemetry) gezielt auf Monitor 1, 2 oder 3."""
        low = str(panel or "").lower()
        if any(k in low for k in ("neural", "brain", "gehirn", "3d")):
            clean_panel = "neural-core"
            rel_url = "/neural-core"
            panel_title = "Neural 3D Core Window"
        elif any(k in low for k in ("vis", "radar", "matrix", "screen", "2")):
            clean_panel = "screen-matrix"
            rel_url = "/window/screen-matrix"
            panel_title = "Screen-Matrix (Fenster 2)"
        else:
            clean_panel = "system-telemetry"
            rel_url = "/window/system-telemetry"
            panel_title = "System-Telemetry (Fenster 3)"
        full_url = f"{self.url}{rel_url}"

        mons = self.refresh_monitors()
        try:
            from multi_monitor import compute_window_rect_for_monitor, resolve_monitor_selector

            _, chosen = resolve_monitor_selector(monitor_target, mons)
            chosen_mon = chosen or (mons[0] if mons else {"index": 1, "pos": "mitte", "x": 40, "y": 40, "w": 1280, "h": 800})
            wx, wy, ww, wh = compute_window_rect_for_monitor(chosen_mon, layout="center")
        except Exception:
            chosen_mon = {"index": 1, "pos": "mitte"}
            wx, wy, ww, wh = None, None, 1120, 760

        proc = launch_browser_app_window(
            full_url,
            width=ww,
            height=wh,
            x=wx,
            y=wy,
            profile_name=f"jarvis_panel_{clean_panel}_m{chosen_mon.get('index', 1)}",
        )
        if proc is not None:
            self._popout_procs.append(proc)

        self._broadcast(
            {
                "type": "hud_window_command",
                "panel": clean_panel,
                "monitor": chosen_mon.get("index", 1),
                "url": rel_url,
                "action": "open",
            }
        )
        msg = f"{panel_title} auf Monitor {chosen_mon.get('index', 1)} ({chosen_mon.get('pos', 'mitte')}) geöffnet."
        self.emit_neural_thought(category="system", tool="open_subwindow", summary=msg, monitor=chosen_mon.get("index", 1))
        return {
            "ok": True,
            "panel": clean_panel,
            "monitor": chosen_mon,
            "url": full_url,
            "message": msg,
        }

    def add_log(self, message: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        line = f"[{stamp}] {message}"
        with self._state_lock:
            self._activity_lines.append(line)
        self._broadcast({"type": "log", "line": line})

    def set_status(self, status: str) -> None:
        eng, color, sub = STATUS_META.get(status, ("READY", "#00d4ff", status.upper()))
        with self._state_lock:
            self._status = status
        self._live_state["status"] = status
        self._broadcast(
            {
                "type": "status",
                "status": status,
                "status_eng": eng,
                "status_color": color,
                "status_sub": sub,
            }
        )
        if status != "Bereit":
            self.add_log(f"Status: {status}")

        brain_map = {
            "Bereit": ("idle", 0.28, "STANDBY"),
            "Höre zu": ("listening", 0.72, "AUDIO INPUT"),
            "Verarbeite Sprache": ("thinking", 0.85, "STT ONNX"),
            "Denke nach": ("thinking", 0.92, "NEURAL PROCESSING"),
            "Spreche": ("speaking", 0.78, "TTS STREAM"),
            "Kamera aktiv": ("screen_scan", 0.84, "VISION SCAN"),
            "Fehler": ("error", 0.95, "WARNUNG"),
        }
        b_state, b_int, b_det = brain_map.get(status, ("idle", 0.35, eng))
        self.emit_brain_activity(state=b_state, intensity=b_int, detail=b_det)

    def set_vision_status(self, vision_status: str) -> None:
        """Aktualisiert die kompakte HUD-Vision-Statusanzeige (ohne Bilder über WebSocket)."""
        clean = str(vision_status or VISION_STATUS_PASSIVE).strip()
        if not clean:
            clean = VISION_STATUS_PASSIVE
        with self._state_lock:
            changed = self._vision_status != clean
            self._vision_status = clean
        if changed:
            self._broadcast({"type": "vision_status", "vision_status": clean})

    def update_online_switch(self, enabled: bool) -> None:
        target = bool(enabled)
        voice = (
            "VOICE: edge-tts (Conrad -15Hz)"
            if target
            else "VOICE: piper-tts (Karlsson Deep)"
        )
        with self._state_lock:
            self._is_online = target
            self._mode = "online" if target else "offline"
            self._voice_label = voice
        self._broadcast({"type": "mode", "is_online": target, "voice_label": voice})

    def set_mode_label(self, mode: str) -> None:
        self.update_online_switch(mode == "online")

    def set_model_label(self, provider: str, model: str) -> None:
        p_low = provider.lower()
        if "ollama" in p_low:
            text = f"MODEL: OLLAMA LOCAL ({model})" if model and model != "-" else "MODEL: OLLAMA LOCAL"
        elif "gemini" in p_low:
            text = "MODEL: ONLINE / GEMINI"
        elif "groq" in p_low:
            text = "MODEL: ONLINE / GROQ"
        elif "deepseek" in p_low:
            text = "MODEL: ONLINE / DEEPSEEK"
        elif "openrouter" in p_low:
            text = "MODEL: ONLINE / OPENROUTER"
        elif "pollinations" in p_low or "kostenlos" in p_low or "keyless" in p_low:
            text = "MODEL: POLLINATIONS (FREE)"
        else:
            text = f"MODEL: {model.upper()}" if model and model != "-" else f"MODEL: {provider.upper()}"

        with self._state_lock:
            self._model_label = text
        self._broadcast({"type": "model", "model_label": text})

    def set_response_time(self, seconds: float) -> None:
        val = max(0.0, float(seconds))
        with self._state_lock:
            self._response_time_s = val
        self._broadcast({"type": "response_time", "seconds": val})

    def set_audio_level(self, level: float, vad_active: bool, speaking: bool = False) -> None:
        raw = float(level)
        if raw > 1.0:
            raw = raw / 100.0
        clamped = max(0.0, min(1.0, raw))
        was_vad = self._vad_active
        self._audio_level = clamped
        self._vad_active = bool(vad_active)
        self._speaking_active = bool(speaking)
        self._live_state["audio_level"] = clamped
        self._live_state["level_pct"] = int(round(clamped * 100.0))
        self._live_state["vad_active"] = bool(vad_active)
        self._live_state["speaking_active"] = bool(speaking)
        if vad_active and not was_vad:
            try:
                from agent_hand import DESKTOP_VISION_STREAM

                DESKTOP_VISION_STREAM.boost_active_rate(10.0)
            except Exception:
                pass

    def set_user_text(self, text: str) -> None:
        cleaned = text.strip()
        if cleaned:
            try:
                from agent_hand import DESKTOP_VISION_STREAM

                DESKTOP_VISION_STREAM.boost_active_rate(10.0)
            except Exception:
                pass
        formatted = f'"{cleaned}"' if cleaned else ""
        stamp = datetime.now().strftime("%H:%M:%S")
        with self._state_lock:
            self._user_text = formatted
            self._dialog_turn_id += 1
            turn_id = self._dialog_turn_id
            self._current_jarvis_turn_id = None
            entry = {"id": turn_id, "role": "user", "text": cleaned, "time": stamp}
            self._dialog_history.append(entry)
        self._broadcast({"type": "user_text", "text": formatted, "raw": cleaned, "turn_id": turn_id, "time": stamp})

    def set_jarvis_text(self, text: str) -> None:
        cleaned = text.strip()
        stamp = datetime.now().strftime("%H:%M:%S")
        with self._state_lock:
            self._jarvis_text = cleaned
            if self._current_jarvis_turn_id is None:
                self._dialog_turn_id += 1
                self._current_jarvis_turn_id = self._dialog_turn_id
                entry = {"id": self._current_jarvis_turn_id, "role": "jarvis", "text": cleaned, "time": stamp}
                self._dialog_history.append(entry)
            else:
                for item in reversed(self._dialog_history):
                    if item.get("id") == self._current_jarvis_turn_id:
                        item["text"] = cleaned
                        item["time"] = stamp
                        break
            turn_id = self._current_jarvis_turn_id
        self._broadcast({"type": "jarvis_text", "text": cleaned, "turn_id": turn_id, "time": stamp})

    def update_camera_preview(self, jpeg_bytes: bytes) -> None:
        if jpeg_bytes:
            with self._state_lock:
                self._latest_jpeg = jpeg_bytes

    def _toggle_fullscreen_window(self) -> None:
        self._fullscreen = not self._fullscreen
        if self._webview_window is not None:
            try:
                self._webview_window.toggle_fullscreen()
            except Exception:
                pass

    def _set_minimized_state(self, minimized: bool) -> None:
        """Schaltet das 60-FPS-HUD-Rendering im minimierten Zustand stumm (spart CPU/GPU), während alle Hintergrund-Threads weiterlaufen."""
        changed = False
        with self._state_lock:
            if self._window_minimized != minimized:
                self._window_minimized = minimized
                changed = True
        if changed:
            self._broadcast({"type": "window_state", "minimized": minimized})
            if not minimized:
                self._broadcast({"type": "snapshot", "data": self._build_snapshot()})

    def minimize_to_background(self) -> None:
        """Minimiert das HUD-Fenster in die Taskleiste / den System-Tray; Wake-Word, TTS, Wecker & Maus laufen zu 100% weiter."""
        self._set_minimized_state(True)
        if self._webview_window is not None:
            try:
                if self._fullscreen:
                    self._fullscreen = False
                    self._webview_window.toggle_fullscreen()
                self._webview_window.minimize()
                return
            except Exception:
                pass
        if platform.system() == "Windows":
            try:
                import ctypes

                hud_hwnd = ctypes.windll.user32.FindWindowW(None, "JARVIS // COMMAND CENTER")
                if hud_hwnd:
                    ctypes.windll.user32.ShowWindow(hud_hwnd, 6)  # SW_MINIMIZE
            except Exception:
                pass

    def restore_from_background(self) -> None:
        """Holt das HUD-Fenster aus der Taskleiste / dem System-Tray zurück in den Vordergrund und aktiviert sofort 60 FPS."""
        self._set_minimized_state(False)
        if self._webview_window is not None:
            try:
                self._webview_window.restore()
            except Exception:
                pass
        self._focus_hud_window_windows(minimize_console=False)

    def enter_mini_hud_mode(self, label: str = "JARVIS // DESKTOP-OPERATOR AKTIV") -> None:
        """AUFGABE 4: Verschiebt/minimiert das HUD sauber aus dem Weg, damit Browser oder Ziel-App voll sichtbar bleiben."""
        self.minimize_to_background()
        try:
            from agent_hand import coordinate_jarvis_window_for_action

            coordinate_jarvis_window_for_action(mode="minimize")
        except Exception:
            pass
        try:
            _get_ghost_overlay().start(label)
        except Exception:
            pass

    def exit_mini_hud_mode(self, restore_window: bool = False) -> None:
        """
        FIX 1: Blendet das Ghost-Overlay aus.
        Holt das JARVIS-HUD standardmäßig NICHT wieder in den Vordergrund (`restore_window=False`),
        damit geöffnete Desktop-Apps / Browser / Launcher sichtbar im Vordergrund bleiben.
        """
        try:
            _get_ghost_overlay().stop()
        except Exception:
            pass
        if restore_window:
            try:
                from agent_hand import restore_jarvis_window

                restore_jarvis_window()
            except Exception:
                pass
            self.restore_from_background()

    def enter_screen_control(self, label: str = "JARVIS // GHOST-CURSOR AKTIV") -> None:
        """Aktiviert das Ghost-Cursor-Overlay auf dem Desktop und minimiert das HUD-Fenster für freie Sicht."""
        self.enter_mini_hud_mode(label)

    def exit_screen_control(self, restore_window: bool = False) -> None:
        """Blendet das Ghost-Cursor-Overlay aus; lässt das HUD standardmäßig im Hintergrund (`restore_window=False`)."""
        self.exit_mini_hud_mode(restore_window=restore_window)

    def _window_state_monitor_loop(self) -> None:
        """Überwacht unter Windows den Minimiert-Status (IsIconic), damit auch beim Klick auf '_' das 60-FPS-Rendering pausiert."""
        if platform.system() != "Windows":
            return
        try:
            import ctypes

            user32 = ctypes.windll.user32
        except Exception:
            return

        while not self._stop_event.is_set():
            try:
                hud_hwnd = user32.FindWindowW(None, "JARVIS // COMMAND CENTER")
                if hud_hwnd:
                    is_min = bool(user32.IsIconic(hud_hwnd))
                    self._set_minimized_state(is_min)
            except Exception:
                pass
            self._stop_event.wait(0.4)

    def _start_system_tray(self) -> None:
        """Startet das optionale JARVIS System-Tray-Icon (pystray) im Infobereich neben der Uhr."""
        try:
            import pystray  # type: ignore[import-not-found]
        except Exception as exc:
            print(f"[System-Tray Hinweis] pystray nicht geladen ({exc})")
            return

        icon_img = _create_jarvis_tray_icon_image()
        if icon_img is None:
            return

        def _on_show(_icon: Any, _item: Any) -> None:
            self.restore_from_background()

        def _on_min(_icon: Any, _item: Any) -> None:
            self.minimize_to_background()

        def _on_speak(_icon: Any, _item: Any) -> None:
            if self._on_manual_trigger is not None:
                self._on_manual_trigger()

        def _on_quit(_icon: Any, _item: Any) -> None:
            threading.Thread(target=self._handle_close, daemon=True).start()

        menu = pystray.Menu(
            pystray.MenuItem("HUD-Fenster öffnen / In den Vordergrund", _on_show, default=True),
            pystray.MenuItem("Fenster minimieren (Hintergrundbetrieb)", _on_min),
            pystray.MenuItem("Sprachbefehl starten (Jetzt sprechen)", _on_speak),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("JARVIS komplett beenden", _on_quit),
        )

        try:
            self._tray_icon = pystray.Icon(
                "jarvis_command_center",
                icon_img,
                "J.A.R.V.I.S. // Command Center (Aktiv im Hintergrund)",
                menu,
            )
            threading.Thread(target=self._tray_icon.run, name="JarvisSystemTray", daemon=True).start()
        except Exception as exc:
            print(f"[System-Tray Warnung] Konnte Tray-Icon nicht starten: {exc}")

    def _handle_close(self) -> None:
        if self._stop_event.is_set():
            return
        self._stop_event.set()
        try:
            import sys as _sys

            if "ghost_cursor" in _sys.modules:
                _get_ghost_overlay().stop()
        except Exception:
            pass
        if self._tray_icon is not None:
            try:
                self._tray_icon.stop()
            except Exception:
                pass
            self._tray_icon = None
        unregister_online_mode_listener(self._online_listener)
        if self._on_close is not None:
            try:
                self._on_close()
            except Exception:
                pass
        try:
            from multi_monitor import MULTI_MONITOR_STREAMER

            MULTI_MONITOR_STREAMER.stop()
        except Exception:
            pass
        try:
            from macro_engine import EVENT_TICKER

            EVENT_TICKER.unsubscribe(self._ticker_cb)
        except Exception:
            pass
        if self._server is not None:
            self._server.should_exit = True
        if self._webview_window is not None:
            try:
                if self._fullscreen:
                    try:
                        self._webview_window.toggle_fullscreen()
                    except Exception:
                        pass
                self._webview_window.destroy()
            except Exception:
                pass
        if self._browser_proc is not None and self._browser_proc.poll() is None:
            try:
                self._browser_proc.terminate()
            except Exception:
                pass
        for pop_proc in getattr(self, "_popout_procs", []):
            try:
                if pop_proc is not None and pop_proc.poll() is None:
                    pop_proc.terminate()
            except Exception:
                pass

    @staticmethod
    def _focus_hud_window_windows(minimize_console: bool = True) -> None:
        """Bringt das JARVIS-HUD-Fenster im normalen Fenstermodus aktiv in den Vordergrund."""
        if platform.system() != "Windows":
            return
        try:
            import ctypes

            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            if minimize_console:
                console_hwnd = kernel32.GetConsoleWindow()
                if console_hwnd:
                    user32.ShowWindow(console_hwnd, 6)  # SW_MINIMIZE
            hud_hwnd = user32.FindWindowW(None, "JARVIS // COMMAND CENTER")
            if hud_hwnd:
                ico_path = _get_jarvis_icon_path("ico")
                if ico_path is not None:
                    try:
                        IMAGE_ICON = 1
                        LR_LOADFROMFILE = 0x0010
                        WM_SETICON = 0x0080
                        hicon_big = user32.LoadImageW(0, str(ico_path), IMAGE_ICON, 64, 64, LR_LOADFROMFILE)
                        hicon_small = user32.LoadImageW(0, str(ico_path), IMAGE_ICON, 16, 16, LR_LOADFROMFILE)
                        if hicon_big:
                            user32.SendMessageW(hud_hwnd, WM_SETICON, 1, hicon_big)
                        if hicon_small:
                            user32.SendMessageW(hud_hwnd, WM_SETICON, 0, hicon_small)
                    except Exception:
                        pass
                if user32.IsIconic(hud_hwnd):
                    user32.ShowWindow(hud_hwnd, 9)  # SW_RESTORE (holt aus Minimierung in normalen Fenstermodus)
                else:
                    user32.ShowWindow(hud_hwnd, 1)  # SW_SHOWNORMAL (niemals maximiert/Vollbild erzwingen)
                user32.SetForegroundWindow(hud_hwnd)
        except Exception:
            pass

    def run(self, on_window_ready: Optional[Callable[[], None]] = None) -> None:
        """Startet FastAPI-Backend und öffnet das Web-HUD sofort (<1,5s) im normalen Fenstermodus (1280x800)."""
        enable_windows_high_dpi()
        self._fullscreen = False
        self._start_backend_server()
        threading.Thread(target=self._start_system_tray, name="JarvisTrayStarter", daemon=True).start()
        threading.Thread(target=self._window_state_monitor_loop, daemon=True).start()

        boot_fired = threading.Event()

        def _trigger_window_ready_once() -> None:
            if boot_fired.is_set():
                return
            boot_fired.set()
            if on_window_ready is not None:
                threading.Thread(target=on_window_ready, name="JarvisBootloader", daemon=True).start()

        proc_started = time.monotonic()
        proc = launch_browser_app_window(
            self.url,
            width=1280,
            height=800,
            profile_name="jarvis_hud_browser_profile",
        )
        _trigger_window_ready_once()

        if proc is not None:
            self._browser_proc = proc
            if platform.system() == "Windows":
                threading.Timer(
                    1.2,
                    lambda: self._focus_hud_window_windows(minimize_console=True),
                ).start()
            try:
                while not self._stop_event.is_set():
                    if self._browser_proc.poll() is not None:
                        # Falls existierender Browser-Prozess das Fenster übernommen hat, nicht sofort abbrechen
                        if (time.monotonic() - proc_started) < 2.5:
                            print(f"[Web-HUD] Browser-Fenster an laufende Instanz übergeben ({self.url})")
                            while not self._stop_event.is_set():
                                self._stop_event.wait(0.5)
                        break
                    self._stop_event.wait(0.4)
            except KeyboardInterrupt:
                pass
            self._handle_close()
            return

        print(f"[Web-HUD] Öffne System-Standardbrowser unter {self.url}")
        try:
            while not self._stop_event.is_set():
                self._stop_event.wait(0.5)
        except KeyboardInterrupt:
            pass
        self._handle_close()
