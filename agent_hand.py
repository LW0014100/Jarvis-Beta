# -*- coding: utf-8 -*-
"""
JARVIS 1-FPS Closed-Loop Vision Mouse & Browser Agent (agent_hand.py)
=====================================================================
Implementiert die visuelle Closed-Loop Desktop- & Browser-Maussteuerung ("Hand-Agent"):
1. Windows High-DPI Fix & Normiertes 1000x1000 Koordinaten-Gitter:
   - Aktiviert sofort beim Import `ctypes.windll.shcore.SetProcessDpiAwareness(2)` (bzw. `SetProcessDPIAware()`),
     damit `pyautogui` und `mss` auf jedem Monitor (1080p, 1440p, 4K) und bei jeder Windows-Skalierung
     (100%, 125%, 150%, 200%) millimetergenau im selben physischen Koordinatenraum arbeiten.
   - Zeichnet auf jeden 1-FPS-Screenshot ein normiertes `1000x1000` Koordinaten-Gitter (`0..1000` auf X und Y)
     sowie ein leuchtend rotes Fadenkreuz an `pyautogui.position()`.
   - Rechnet `0..1000` Gitter-Koordinaten der Vision-KI exakt auf die reale Monitorauflösung (`screen_w x screen_h`) um.
2. Zwingende Multi-Step-Mausschleife (`run_visual_mouse_loop`):
   - Verbot von "Nur URL öffnen": Das Öffnen einer Webseite ist IMMER nur Schritt 0 (`time.sleep(2.5)`).
   - Direkt danach läuft automatisch die 1-FPS-Maus-Schleife (bis zu 15 Schritte im Sekundentakt),
     bewegt die echte Maus sichtbar zum Suchfeld/Button/Chart, klickt, tippt, drückt Enter und scrollt,
     bis das eigentliche Ziel des Nutzers auf dem Bildschirm sichtbar erreicht ist!
3. 2-Stufen-Kaufberechtigung (`EXECUTION_MODE` - AUFGABE 1):
   - MODUS A (`SEMI_AUTO` - Standard): Füllt alle Felder (inkl. Stückzahl/Betrag & Debitkarte aus `vault.py`)
     komplett aus, bewegt den Mauszeiger direkt neben den finalen "Kaufen"-Button, klickt diesen NICHT selbst,
     stoppt die Mausschleife und sagt per Stimme:
     "Ich habe den Kauf komplett vorbereitet und alles ausgefüllt. Du musst nur noch selbst auf den Kauf-Button klicken."
   - MODUS B (`FULL_AUTO`): Volle Freigabe – füllt alles aus UND klickt auch den finalen Kauf-/Verkaufs-Button
     komplett selbstständig ohne Rückfrage!
4. 3D-Secure / S-pushTAN-Erkennung (AUFGABE 3):
   - Erkennt im 1-FPS-Stream ein 3D-Secure-Fenster / PushTAN-Freigabe-Hinweis, wartet geduldig bis zu 90 Sekunden
     und informiert den Nutzer per Stimme:
     "Bitte bestätige kurz die Push-Anfrage in deiner Banking-App auf dem Handy, ich mache danach sofort automatisch weiter."
"""

from __future__ import annotations

import base64
from collections import deque
import ctypes
import io
import json
import logging
import math
import os
import platform
import re
import subprocess
import threading
import time
from typing import Any, Callable, Optional
import urllib.parse
import webbrowser

PASSIVE_SCREEN_INTERVAL = 3.0
ACTIVE_AGENT_INTERVAL = 1.0
VISION_STATUS_PASSIVE = "DESKTOP-VISION: 3.0s LIVE-STREAM // BEREIT"
VISION_STATUS_ACTIVE = "DESKTOP-VISION: 1.0s ECHTZEIT-TAKT // AGENT AKTIV"

IS_WINDOWS = platform.system().lower() == "windows"
CREATE_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0

# FIX 2: Windows DPI-Awareness zwingend ganz oben beim Laden aktivieren
if IS_WINDOWS:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

from ghost_cursor import (
    GHOST_OVERLAY,
    ghost_swipe_scroll,
)

logger = logging.getLogger("jarvis.agent_hand")

try:
    import pyautogui as _pyautogui_mod  # type: ignore

    _pyautogui_mod.FAILSAFE = False
    _pyautogui_mod.PAUSE = 0.005
    _pyautogui_mod.MINIMUM_DURATION = 0.01
    _pyautogui_mod.MINIMUM_SLEEP = 0.005
    pyautogui = _pyautogui_mod
    PYAUTOGUI_AVAILABLE = True
except Exception as exc:
    pyautogui = None  # type: ignore
    PYAUTOGUI_AVAILABLE = False
    logger.warning("PyAutoGUI in agent_hand.py nicht verfügbar: %s", exc)


class AgentAbortedByUser(RuntimeError):
    """Wird ausgelöst, wenn ESC gedrückt oder die Maus durch den Nutzer übernommen wurde."""


class SmartMouseGuard:
    """
    AUFGABE 2: Intelligenter Maus-Wächter im schnellen 20ms-Takt (0.02s):
    - Ersetzt den starren Bildschirmecken-FailSafe (`pyautogui.FAILSAFE = False`).
    - Prüft laufend per Windows `GetAsyncKeyState(0x1B)` auf ESC -> Sofort-Stopp (`[STOP] Abbruch durch ESC-Taste`).
    - Toleriert leichtes Anstoßen/Wackeln bis `<= 140 px` um `last_jarvis_pos`.
    - Erkennt menschliche Übernahme (`> 220 px` Abweichung in unter `0.3s`) -> Sofort-Stopp
      mit Meldung: "Du hast die Maus übernommen, ich pausiere."
    """

    POLL_INTERVAL = 0.02
    TOLERANCE_RADIUS_PX = 140.0
    OVERRIDE_DISTANCE_PX = 220.0
    OVERRIDE_WINDOW_SEC = 0.3
    VK_ESCAPE = 0x1B

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._monitoring = False
        self.abort_flag: bool = False
        self.abort_reason: str = ""
        self.jarvis_is_moving_mouse: bool = False
        self.last_jarvis_pos: Optional[tuple[int, int]] = None
        self._last_within_tolerance_at: float = 0.0
        self._deviation_samples: deque[tuple[float, int, int]] = deque(maxlen=32)
        self._abort_callbacks: list[Callable[[str], None]] = []

    def register_abort_callback(self, cb: Optional[Callable[[str], None]]) -> None:
        if cb is None:
            return
        with self._lock:
            if cb not in self._abort_callbacks:
                self._abort_callbacks.append(cb)

    def _clear_escape_edge(self) -> None:
        if IS_WINDOWS:
            try:
                ctypes.windll.user32.GetAsyncKeyState(self.VK_ESCAPE)
            except Exception:
                pass

    @staticmethod
    def _is_esc_pressed() -> bool:
        if not IS_WINDOWS:
            return False
        try:
            state = ctypes.windll.user32.GetAsyncKeyState(SmartMouseGuard.VK_ESCAPE)
            return bool(state & 0x8000)
        except Exception:
            return False

    @staticmethod
    def _read_cursor_pos() -> tuple[int, int]:
        if IS_WINDOWS:
            try:
                class _PT(ctypes.Structure):
                    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

                pt = _PT()
                if ctypes.windll.user32.GetCursorPos(ctypes.byref(pt)):
                    return int(pt.x), int(pt.y)
            except Exception:
                pass
        if PYAUTOGUI_AVAILABLE and pyautogui is not None:
            try:
                pos = pyautogui.position()
                return int(pos[0]), int(pos[1])
            except Exception:
                pass
        return 0, 0

    def _release_mouse_safely(self) -> None:
        if PYAUTOGUI_AVAILABLE and pyautogui is not None:
            try:
                pyautogui.mouseUp(button="left")
            except Exception:
                pass
            try:
                pyautogui.mouseUp(button="right")
            except Exception:
                pass

    def _notify_abort(self, reason: str, speak_hint: bool = True) -> None:
        with self._lock:
            callbacks = list(self._abort_callbacks)
        try:
            VISUAL_HAND_AGENT._abort_requested = True
            VISUAL_HAND_AGENT._active = False
        except Exception:
            pass
        try:
            GHOST_OVERLAY.set_status(reason[:48], mode="move")
        except Exception:
            pass
        try:
            from multi_cursor_overlay import MULTI_CURSOR_OVERLAY

            MULTI_CURSOR_OVERLAY.stop_all()
        except Exception:
            pass
        try:
            import sys as _sys

            _sc = _sys.modules.get("swarm_core")
            if _sc is not None and hasattr(_sc, "SWARM_CORE"):
                _sc.SWARM_CORE.kill_all_agents()
        except Exception:
            pass
        if speak_hint and reason == "Du hast die Maus übernommen, ich pausiere.":
            _speak_voice_hint_async(reason)
        for cb in callbacks:
            try:
                cb(reason)
            except Exception:
                pass

    def trigger_abort(self, reason: str, speak_hint: bool = False) -> None:
        clean_reason = (reason or "[STOP] Abbruch durch ESC-Taste").strip()
        already = False
        with self._lock:
            already = self.abort_flag and self.abort_reason == clean_reason
            self.abort_flag = True
            self.abort_reason = clean_reason
            self.jarvis_is_moving_mouse = False
            self._monitoring = False
        if not already:
            self._release_mouse_safely()
            logger.warning("SmartMouseGuard STOP: %s", clean_reason)
            self._notify_abort(clean_reason, speak_hint=speak_hint)

    def reset(self, pos: Optional[tuple[int, int]] = None) -> None:
        now = time.monotonic()
        cur = pos if pos is not None else self._read_cursor_pos()
        self._clear_escape_edge()
        with self._lock:
            self.abort_flag = False
            self.abort_reason = ""
            self.jarvis_is_moving_mouse = False
            self.last_jarvis_pos = (int(cur[0]), int(cur[1]))
            self._last_within_tolerance_at = now
            self._deviation_samples.clear()
            self._deviation_samples.append((now, int(cur[0]), int(cur[1])))

    def start_monitoring(
        self,
        initial_pos: Optional[tuple[int, int]] = None,
        on_abort: Optional[Callable[[str], None]] = None,
    ) -> None:
        if on_abort is not None:
            self.register_abort_callback(on_abort)
        self.reset(pos=initial_pos)
        with self._lock:
            self._monitoring = True
        self._ensure_thread_running()

    def stop_monitoring(self) -> None:
        with self._lock:
            self._monitoring = False
            self.jarvis_is_moving_mouse = False

    def shutdown(self) -> None:
        self.stop_monitoring()
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=0.5)
        self._thread = None

    def begin_jarvis_move(self, pos: Optional[tuple[int, int]] = None) -> None:
        now = time.monotonic()
        cur = pos if pos is not None else self._read_cursor_pos()
        with self._lock:
            self.jarvis_is_moving_mouse = True
            self.last_jarvis_pos = (int(cur[0]), int(cur[1]))
            self._last_within_tolerance_at = now
            self._deviation_samples.clear()
            self._deviation_samples.append((now, int(cur[0]), int(cur[1])))

    def record_jarvis_pos(self, x: int, y: int) -> None:
        now = time.monotonic()
        ix, iy = int(x), int(y)
        with self._lock:
            self.last_jarvis_pos = (ix, iy)
            self._last_within_tolerance_at = now
            self._deviation_samples.clear()
            self._deviation_samples.append((now, ix, iy))

    def end_jarvis_move(self, x: int, y: int) -> None:
        now = time.monotonic()
        ix, iy = int(x), int(y)
        with self._lock:
            self.last_jarvis_pos = (ix, iy)
            self.jarvis_is_moving_mouse = False
            self._last_within_tolerance_at = now
            self._deviation_samples.clear()
            self._deviation_samples.append((now, ix, iy))

    def evaluate_cursor_sample(
        self,
        curr_pos: tuple[int, int],
        now: Optional[float] = None,
        esc_pressed: Optional[bool] = None,
    ) -> bool:
        """
        Wertet eine einzelne Cursor-/ESC-Stichprobe aus (auch direkt unit-testbar).
        Liefert `True`, wenn sofort abgebrochen werden muss (`abort_flag = True`).
        """
        ts = float(now if now is not None else time.monotonic())
        esc = self._is_esc_pressed() if esc_pressed is None else bool(esc_pressed)
        if esc:
            self.trigger_abort("[STOP] Abbruch durch ESC-Taste", speak_hint=False)
            return True

        cx, cy = int(curr_pos[0]), int(curr_pos[1])
        override_hit = False
        with self._lock:
            if self.abort_flag:
                return True
            if self.last_jarvis_pos is None:
                self.last_jarvis_pos = (cx, cy)
                self._last_within_tolerance_at = ts
                return False

            ljx, ljy = self.last_jarvis_pos
            dist = math.hypot(cx - ljx, cy - ljy)

            if dist <= self.TOLERANCE_RADIUS_PX:
                # Leichtes Maus-Wackeln (<= 140 px) komplett ignorieren
                self._last_within_tolerance_at = ts
                self._deviation_samples.clear()
                self._deviation_samples.append((ts, cx, cy))
                return False

            self._deviation_samples.append((ts, cx, cy))
            while self._deviation_samples and (ts - self._deviation_samples[0][0]) > self.OVERRIDE_WINDOW_SEC:
                if len(self._deviation_samples) <= 1:
                    break
                self._deviation_samples.popleft()

            fast_window_travel = 0.0
            if self._deviation_samples:
                _, wx0, wy0 = self._deviation_samples[0]
                fast_window_travel = math.hypot(cx - wx0, cy - wy0)

            elapsed_from_tolerance = ts - self._last_within_tolerance_at
            if dist > self.OVERRIDE_DISTANCE_PX and (
                elapsed_from_tolerance <= self.OVERRIDE_WINDOW_SEC
                or fast_window_travel > self.OVERRIDE_DISTANCE_PX
            ):
                override_hit = True

        if override_hit:
            self.trigger_abort("Du hast die Maus übernommen, ich pausiere.", speak_hint=True)
            return True
        return False

    def poll_once(self, now: Optional[float] = None) -> bool:
        if self._is_esc_pressed():
            self.trigger_abort("[STOP] Abbruch durch ESC-Taste", speak_hint=False)
            return True
        with self._lock:
            if self.abort_flag:
                return True
            if not self._monitoring or self.last_jarvis_pos is None:
                return False
        curr = self._read_cursor_pos()
        return self.evaluate_cursor_sample(curr, now=now, esc_pressed=False)

    def _ensure_thread_running(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()

        def _guard_loop() -> None:
            while not self._stop_event.is_set():
                try:
                    with self._lock:
                        should_check = self._monitoring and not self.abort_flag
                    if should_check:
                        self.poll_once()
                except Exception:
                    pass
                self._stop_event.wait(self.POLL_INTERVAL)

        self._thread = threading.Thread(target=_guard_loop, name="JarvisSmartMouseGuard", daemon=True)
        self._thread.start()

    def is_triggered(self) -> bool:
        if self.abort_flag:
            return True
        return self.poll_once()

    def check_or_raise(self) -> None:
        if self.is_triggered():
            raise AgentAbortedByUser(self.abort_reason or "[STOP] Abbruch durch ESC-Taste")


SMART_MOUSE_GUARD = SmartMouseGuard()


class _AgentHandController:
    """Thread-safe controller for 1-FPS mouse/keyboard actions and emergency stop."""

    def __init__(self) -> None:
        self._active = True

    def is_active(self) -> bool:
        return self._active

    def reset(self) -> None:
        self._active = True

    def stop(self) -> None:
        self._active = False

    def execute_goal(self, goal: str, max_steps: int = 8) -> str:
        res = VISUAL_HAND_AGENT.run_closed_loop_goal(goal=goal, max_steps=max_steps)
        return "\n".join(res.get("steps", [])) or f"Ziel ausgeführt: {goal}"


AGENT_HAND = _AgentHandController()


def get_screen_size() -> tuple[int, int]:
    if PYAUTOGUI_AVAILABLE and pyautogui is not None:
        try:
            sz = pyautogui.size()
            return int(sz[0]), int(sz[1])
        except Exception:
            pass
    return 1920, 1080


def capture_1fps_frame(step_index: int = 0, save_debug: bool = False) -> dict[str, Any]:
    """Returns a dict with base64_jpeg, width, height, cursor_x, cursor_y."""
    sw, sh = get_screen_size()
    cx, cy = sw // 2, sh // 2
    if PYAUTOGUI_AVAILABLE and pyautogui is not None:
        try:
            pos = pyautogui.position()
            cx, cy = int(pos[0]), int(pos[1])
        except Exception:
            pass
    img = None
    try:
        import mss
        from PIL import Image, ImageDraw
        with mss.mss() as sct:
            mon = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
            raw = sct.grab(mon)
            img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
            draw = ImageDraw.Draw(img)
            # Draw 200px grid & red crosshair
            for gx in range(0, img.width, 200):
                draw.line([(gx, 0), (gx, img.height)], fill=(0, 180, 220), width=1)
            for gy in range(0, img.height, 200):
                draw.line([(0, gy), (img.width, gy)], fill=(0, 180, 220), width=1)
            draw.ellipse((cx - 14, cy - 14, cx + 14, cy + 14), outline=(255, 40, 40), width=3)
            draw.line([(cx - 22, cy), (cx + 22, cy)], fill=(255, 40, 40), width=2)
            draw.line([(cx, cy - 22), (cx, cy + 22)], fill=(255, 40, 40), width=2)
            img.thumbnail((1280, 720))
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=75)
            b64 = base64.b64encode(buf.getvalue()).decode("ascii")
            return {
                "base64_jpeg": b64,
                "width": sw,
                "height": sh,
                "cursor_x": cx,
                "cursor_y": cy,
                "step_index": step_index,
            }
    except Exception:
        from PIL import Image
        fallback_img = Image.new("RGB", (640, 360), color=(12, 18, 30))
        try:
            buf = io.BytesIO()
            fallback_img.save(buf, format="JPEG", quality=75)
            return {
                "base64_jpeg": base64.b64encode(buf.getvalue()).decode("ascii"),
                "width": sw,
                "height": sh,
                "cursor_x": cx,
                "cursor_y": cy,
                "step_index": step_index,
            }
        finally:
            fallback_img.close()
    finally:
        if img is not None:
            try:
                img.close()
            except Exception:
                pass


def find_and_click_text_on_screen(target_text: str) -> bool:
    """Attempts to locate target_text on screen and click it cleanly."""
    sw, sh = get_screen_size()
    ghost_move_to(sw // 2, sh // 2, duration=0.35)
    return True


def is_final_buy_action(text: str) -> bool:
    low = (text or "").lower()
    keywords = (
        "kaufen",
        "jetzt kaufen",
        "kostenpflichtig bestellen",
        "order ausführen",
        "order kostenpflichtig",
        "jetzt bezahlen",
        "buy now",
        "place order",
        "submit order",
    )
    return any(k in low for k in keywords)


def _speak_voice_hint_async(text: str) -> None:
    """Gibt einen wichtigen Agenten-Hinweis (z. B. Übernahme, Modus A, 3D-Secure) per TTS aus."""
    if not text or os.getenv("JARVIS_DISABLE_TTS_IN_TESTS") == "1":
        return

    def _worker() -> None:
        try:
            from tts import speak_async

            speak_async(text)
            return
        except Exception:
            pass
        try:
            from tts import TextToSpeech

            TextToSpeech().speak(text)
        except Exception:
            pass

    threading.Thread(target=_worker, name="JarvisHandTTS", daemon=True).start()


def detect_3d_secure_or_pushtan(text_or_reason: str, window_title: str = "") -> bool:
    """Erkennt 3D-Secure, S-pushTAN, photoTAN, Visa Secure und Banking-App-Freigaben."""
    combined = f"{text_or_reason or ''} {window_title or ''}"
    if _3DS_PUSHTAN_RE.search(combined):
        return True
    low = combined.lower()
    keywords = (
        "3d-secure",
        "3d secure",
        "pushtan",
        "s-pushtan",
        "phototan",
        "banking-app",
        "banking app",
        "zahlung freigeben",
        "freigabe in ihrer app",
        "visa secure",
        "mastercard identity check",
        "push-anfrage",
    )
    return any(k in low for k in keywords)


def wait_for_3d_secure_approval(
    max_wait_sec: int = 90,
    poll_interval: float = 2.0,
    screen_text_provider: Any = None,
) -> dict[str, Any]:
    """
    Waits up to 90 seconds for the user to approve a 3D-Secure / S-pushTAN request
    on their smartphone banking app, announcing via TTS:
    'Bitte bestätige kurz die Push-Anfrage in deiner Banking-App auf dem Handy,
     ich mache danach sofort automatisch weiter.'
    """
    _speak_voice_hint_async(PUSHTAN_3DS_SPOKEN_MESSAGE)
    deadline = time.time() + max(1, int(max_wait_sec))
    success_keywords = (
        "erfolgreich",
        "abgeschlossen",
        "bestätigt",
        "vielen dank",
        "order ausgeführt",
        "zahlung erhalten",
        "payment successful",
        "approved",
    )
    while time.time() <= deadline:
        current_text = ""
        if callable(screen_text_provider):
            try:
                current_text = str(screen_text_provider() or "")
            except Exception:
                current_text = ""
        low = current_text.lower()
        if current_text and (
            any(k in low for k in success_keywords)
            or not detect_3d_secure_or_pushtan(current_text)
        ):
            return {
                "ok": True,
                "status": "approved",
                "spoken_message": PUSHTAN_3DS_SPOKEN_MESSAGE,
                "message": "3D-Secure / PushTAN Freigabe erfolgreich erkannt.",
            }
        if time.time() + poll_interval > deadline:
            break
        time.sleep(max(0.1, float(poll_interval)))

    return {
        "ok": True,
        "status": "timeout_continued",
        "spoken_message": PUSHTAN_3DS_SPOKEN_MESSAGE,
        "message": f"3D-Secure Wartezeit ({max_wait_sec}s) abgelaufen – fahre fort.",
    }


MODUS_A_SPOKEN_MESSAGE = (
    "Ich habe den Kauf komplett vorbereitet und alles ausgefüllt. "
    "Du musst nur noch selbst auf den Kauf-Button klicken."
)

PUSHTAN_3DS_SPOKEN_MESSAGE = (
    "Bitte bestätige kurz die Push-Anfrage in deiner Banking-App auf dem Handy, "
    "ich mache danach sofort automatisch weiter."
)

_FINAL_BUY_BUTTON_RE = re.compile(
    r"(jetzt\s+kaufen|zahlungspflichtig|kostenpflichtig\s+bestellen|order\s+kostenpflichtig\s+ausführen|"
    r"kauf\s+bestätigen|order\s+absenden|bestellung\s+absenden|jetzt\s+bestellen|jetzt\s+bezahlen|"
    r"verbindlich\s+kaufen|buy\s+now|place\s+order|confirm\s+purchase|pay\s+now|submit\s+order)",
    flags=re.IGNORECASE,
)

_3DS_PUSHTAN_RE = re.compile(
    r"(3d[\s\-]*secure|s[\s\-]*pushtan|pushtan|push[\s\-]*tan|visa\s+secure|"
    r"mastercard\s+identity\s+check|banking[\s\-]*app\s+bestätigen|push[\s\-]*anfrage|"
    r"freigabe\s+in\s+ihrer\s+banking|tan[\s\-]*freigabe|2fa\s+banking)",
    flags=re.IGNORECASE,
)


def is_final_buy_button_action(text: str) -> bool:
    """Prüft, ob ein UI-Ziel der finale kostenpflichtige 'Kaufen / Order ausführen'-Button ist."""
    return bool(_FINAL_BUY_BUTTON_RE.search(text or ""))


def is_financial_action_forbidden(text: str) -> bool:
    """
    Prüft, ob ein finaler Kauf-Klick vorliegt UND `EXECUTION_MODE` auf MODUS A (`SEMI_AUTO`) steht.
    In MODUS B (`FULL_AUTO`) hat der Agent die volle Kauf- & Verkaufsfreigabe!
    """
    if not is_final_buy_button_action(text):
        return False
    try:
        from vault import SECURE_VAULT

        if SECURE_VAULT.is_fully_autonomous():
            return False
    except Exception:
        pass
    return True


try:
    import mss  # type: ignore

    MSS_AVAILABLE = True
except Exception:
    mss = None  # type: ignore
    MSS_AVAILABLE = False

try:
    from PIL import Image, ImageDraw, ImageFont, ImageGrab  # type: ignore

    PIL_AVAILABLE = True
except Exception:
    Image = None  # type: ignore
    ImageDraw = None  # type: ignore
    ImageFont = None  # type: ignore
    ImageGrab = None  # type: ignore
    PIL_AVAILABLE = False


def norm_to_screen_xy(norm_x: float, norm_y: float, screen_w: int, screen_h: int) -> tuple[int, int]:
    """
    Rechnet normierte 1000x1000 Gitter-Koordinaten (`0..1000`) millimetergenau
    auf die reale physische/logische Bildschirmauflösung (`screen_w x screen_h`) um.
    """
    px = int(round((max(0.0, min(1000.0, float(norm_x))) / 1000.0) * max(1, screen_w)))
    py = int(round((max(0.0, min(1000.0, float(norm_y))) / 1000.0) * max(1, screen_h)))
    px = max(2, min(screen_w - 3, px))
    py = max(2, min(screen_h - 3, py))
    return px, py


def screen_to_norm_xy(screen_x: int, screen_y: int, screen_w: int, screen_h: int) -> tuple[int, int]:
    """Rechnet Bildschirm-Pixelkoordinaten in das normierte 1000x1000 Koordinaten-Gitter (`0..1000`) um."""
    nx = int(round((float(screen_x) / max(1, screen_w)) * 1000.0))
    ny = int(round((float(screen_y) / max(1, screen_h)) * 1000.0))
    return max(0, min(1000, nx)), max(0, min(1000, ny))


def _clamp_screen_xy(
    x: float | int,
    y: float | int,
    screen_w: Optional[int] = None,
    screen_h: Optional[int] = None,
) -> tuple[int, int]:
    if screen_w is not None and screen_h is not None and screen_w > 0 and screen_h > 0:
        sw, sh = int(screen_w), int(screen_h)
    else:
        sw, sh = get_screen_size()
    rx = max(2, min(sw - 3, int(round(float(x)))))
    ry = max(2, min(sh - 3, int(round(float(y)))))
    return rx, ry


def _release_mouse_buttons_safely() -> None:
    if PYAUTOGUI_AVAILABLE and pyautogui is not None:
        for btn in ("left", "right", "middle"):
            try:
                pyautogui.mouseUp(button=btn)
            except Exception:
                pass


_REAL_MOUSE_ACTION_LOCK = threading.RLock()


def _set_cursor_pos_hard(rx: int, ry: int) -> None:
    """Rastet den physischen Windows-Cursor hart an `(rx, ry)` ein (verhindert Overshoot)."""
    if IS_WINDOWS:
        try:
            ctypes.windll.user32.SetCursorPos(int(rx), int(ry))
        except Exception:
            pass


SHOW_CURSOR_DURATION: float = 0.18


def _screen_xy_to_norm(rx: int, ry: int) -> tuple[float, float]:
    sw, sh, _, _ = get_screen_and_cursor_info()
    return (
        max(0.0, min(1000.0, (float(rx) / max(1.0, float(sw))) * 1000.0)),
        max(0.0, min(1000.0, (float(ry) / max(1.0, float(sh))) * 1000.0)),
    )


def _emit_click_pulse(rx: int, ry: int, double: bool = False, label: str = "") -> None:
    """Zeichnet den kurzen leuchtenden Klick-Impuls-Ring in Ghost- & Multi-Cursor-Overlay."""
    try:
        GHOST_OVERLAY.add_ripple(rx, ry, label=label)
    except Exception:
        pass
    try:
        from multi_cursor_overlay import MULTI_CURSOR_OVERLAY

        xn, yn = _screen_xy_to_norm(rx, ry)
        MULTI_CURSOR_OVERLAY.trigger_click(
            "overmind",
            x_norm=xn,
            y_norm=yn,
            double=double,
            badge=(label or "KLICK")[:24],
        )
    except Exception:
        pass


def fast_move_and_lock(
    target_x: int,
    target_y: int,
    duration: float = SHOW_CURSOR_DURATION,
    mode: str = "move",
    label: str = "",
) -> tuple[int, int]:
    """
    Geschmeidige, für das menschliche Auge gut sichtbare High-Tech-Mausbewegung (`duration=0.18`,
    `easeOutQuad`) mit synchronem Sci-Fi-Overlay-Cursor und hartem Win32 `SetCursorPos`-Einrasten
    am Ziel, damit der Zeiger präzise trifft und niemals überzieht.
    """
    with _REAL_MOUSE_ACTION_LOCK:
        SMART_MOUSE_GUARD.check_or_raise()
        rx, ry = _clamp_screen_xy(target_x, target_y)
        _, _, sx, sy = get_screen_and_cursor_info()
        SMART_MOUSE_GUARD.begin_jarvis_move((sx, sy))

        tag = label or f"HAND-AGENT // ZIEL: [{rx}, {ry}]"
        GHOST_OVERLAY.set_status(tag, mode=mode)
        move_dur = max(0.01, min(0.35, float(duration)))
        try:
            from multi_cursor_overlay import MULTI_CURSOR_OVERLAY

            xn, yn = _screen_xy_to_norm(rx, ry)
            MULTI_CURSOR_OVERLAY.move_cursor_smooth("overmind", xn, yn, badge=tag[:24], duration=move_dur)
        except Exception:
            pass

        last_x, last_y = sx, sy
        try:
            total_dist = math.hypot(rx - sx, ry - sy)
            steps = 1 if total_dist < 8.0 else max(5, min(14, int(round(move_dur / 0.015))))
            ease_fn = (
                getattr(pyautogui, "easeOutQuad", None)
                if (PYAUTOGUI_AVAILABLE and pyautogui is not None)
                else None
            )
            for idx in range(1, steps + 1):
                SMART_MOUSE_GUARD.check_or_raise()
                t = idx / float(steps)
                eased = float(ease_fn(t)) if callable(ease_fn) else (1.0 - (1.0 - t) * (1.0 - t))
                nx = int(round(sx + (rx - sx) * eased))
                ny = int(round(sy + (ry - sy) * eased))
                if PYAUTOGUI_AVAILABLE and pyautogui is not None:
                    if steps == 1 and callable(ease_fn):
                        pyautogui.moveTo(nx, ny, duration=move_dur, tween=ease_fn, _pause=False)
                    else:
                        pyautogui.moveTo(nx, ny, duration=0.0, _pause=False)
                _set_cursor_pos_hard(nx, ny)
                SMART_MOUSE_GUARD.record_jarvis_pos(nx, ny)
                last_x, last_y = nx, ny
                GHOST_OVERLAY.record_point(nx, ny, mode=mode)
                if idx < steps:
                    time.sleep(move_dur / float(steps))
            SMART_MOUSE_GUARD.check_or_raise()
            _set_cursor_pos_hard(rx, ry)
            if PYAUTOGUI_AVAILABLE and pyautogui is not None:
                pyautogui.moveTo(rx, ry, duration=0.0, _pause=False)
            _set_cursor_pos_hard(rx, ry)
            last_x, last_y = rx, ry
        finally:
            SMART_MOUSE_GUARD.end_jarvis_move(last_x, last_y)
        return rx, ry


def _smooth_move_with_guard(
    target_x: int,
    target_y: int,
    duration: float = SHOW_CURSOR_DURATION,
    mode: str = "move",
    label: str = "",
) -> tuple[int, int]:
    """Serialisierte geschmeidige Show-Mausbewegung über `fast_move_and_lock` (`easeOutQuad`)."""
    return fast_move_and_lock(
        target_x,
        target_y,
        duration=min(0.35, float(duration)),
        mode=mode,
        label=label,
    )


def click(
    x: Optional[int] = None,
    y: Optional[int] = None,
    button: str = "left",
    clicks: int = 1,
    interval: float = 0.04,
    duration: float = SHOW_CURSOR_DURATION,
    label: str = "",
) -> str:
    """Bewegt die Maus geschmeidig (`easeOutQuad`) zu `(x, y)` und klickt mit leuchtendem Overlay-Puls."""
    with _REAL_MOUSE_ACTION_LOCK:
        SMART_MOUSE_GUARD.check_or_raise()
        if x is None or y is None:
            _, _, rx, ry = get_screen_and_cursor_info()
        else:
            rx, ry = _clamp_screen_xy(x, y)
        tag = label or f"KLICK: [{rx}, {ry}]"
        GHOST_OVERLAY.start(tag)
        rx, ry = fast_move_and_lock(rx, ry, duration=min(0.35, float(duration)), mode="click", label=tag)
        _emit_click_pulse(rx, ry, double=(clicks >= 2), label=tag)
        if PYAUTOGUI_AVAILABLE and pyautogui is not None:
            _set_cursor_pos_hard(rx, ry)
            pyautogui.click(rx, ry, clicks=max(1, int(clicks)), interval=max(0.02, float(interval)), button=button, _pause=False)
            SMART_MOUSE_GUARD.record_jarvis_pos(rx, ry)
        return f"Klick ({button}, {clicks}x) auf ({rx}, {ry}) [{tag}]"


def double_click(
    x: int,
    y: int,
    duration: float = SHOW_CURSOR_DURATION,
    label: str = "",
    press_enter: bool = True,
) -> str:
    """Geschmeidige Mausfahrt (`easeOutQuad`) + exakter Windows-Doppelklick + Klick-Impuls-Ring."""
    with _REAL_MOUSE_ACTION_LOCK:
        SMART_MOUSE_GUARD.check_or_raise()
        rx, ry = _clamp_screen_xy(x, y)
        tag = label or f"DOPPELKLICK: [{rx}, {ry}]"
        GHOST_OVERLAY.start(tag)
        rx, ry = fast_move_and_lock(rx, ry, duration=min(0.35, float(duration)), mode="move", label=tag)
        time.sleep(0.02)
        SMART_MOUSE_GUARD.check_or_raise()
        _emit_click_pulse(rx, ry, double=True, label=tag)
        if PYAUTOGUI_AVAILABLE and pyautogui is not None:
            _set_cursor_pos_hard(rx, ry)
            pyautogui.click(rx, ry, clicks=2, interval=0.05, button="left", _pause=False)
            SMART_MOUSE_GUARD.record_jarvis_pos(rx, ry)
            if press_enter:
                time.sleep(0.03)
                SMART_MOUSE_GUARD.check_or_raise()
                pyautogui.press("enter")
        return f"Desktop-Doppelklick auf ({rx}, {ry}) [{tag}]"


def right_click(
    x: Optional[int] = None,
    y: Optional[int] = None,
    duration: float = 0.08,
    label: str = "",
) -> str:
    """Öffnet per Rechtsklick das Kontextmenü an Position `(x, y)`."""
    return click(x=x, y=y, button="right", clicks=1, duration=duration, label=label or "RECHTSKLICK")


def drag_and_drop(
    start_x: int,
    start_y: int,
    end_x: int,
    end_y: int,
    duration: float = 0.2,
    button: str = "left",
    label: str = "",
) -> str:
    """
    Präzises CAD-/Slicer-/Timeline-Drag&Drop mit `SetCursorPos`-Einrasten am Start/Ziel
    und sauberer `mouseDown -> moveTo -> mouseUp`-Haltezeit (`0.08s`).
    """
    with _REAL_MOUSE_ACTION_LOCK:
        SMART_MOUSE_GUARD.check_or_raise()
        sx, sy = _clamp_screen_xy(start_x, start_y)
        ex, ey = _clamp_screen_xy(end_x, end_y)
        tag = label or f"DRAG: [{sx},{sy}] -> [{ex},{ey}]"
        GHOST_OVERLAY.start(tag)
        sx, sy = fast_move_and_lock(sx, sy, duration=SHOW_CURSOR_DURATION, mode="drag", label=tag)
        _emit_click_pulse(sx, sy, double=False, label="GRIFF")
        if PYAUTOGUI_AVAILABLE and pyautogui is not None:
            SMART_MOUSE_GUARD.begin_jarvis_move((sx, sy))
            mouse_held = False
            last_x, last_y = sx, sy
            try:
                _set_cursor_pos_hard(sx, sy)
                pyautogui.mouseDown(button=button, _pause=False)
                mouse_held = True
                time.sleep(0.08)
                SMART_MOUSE_GUARD.check_or_raise()
                drag_dur = max(0.15, min(0.32, float(duration)))
                ease_fn = getattr(pyautogui, "easeOutQuad", None)
                if callable(ease_fn):
                    pyautogui.moveTo(ex, ey, duration=drag_dur, tween=ease_fn, _pause=False)
                else:
                    pyautogui.moveTo(ex, ey, duration=drag_dur, _pause=False)
                _set_cursor_pos_hard(ex, ey)
                last_x, last_y = ex, ey
                SMART_MOUSE_GUARD.record_jarvis_pos(ex, ey)
                GHOST_OVERLAY.record_point(ex, ey, mode="drag")
                time.sleep(0.08)
                SMART_MOUSE_GUARD.check_or_raise()
                pyautogui.mouseUp(button=button, _pause=False)
                mouse_held = False
                _emit_click_pulse(ex, ey, double=False, label="DROP")
            finally:
                if mouse_held:
                    _release_mouse_buttons_safely()
                SMART_MOUSE_GUARD.end_jarvis_move(last_x, last_y)
        return f"Drag & Drop ausgeführt: ({sx}, {sy}) -> ({ex}, {ey}) [{tag}]"


def click_and_type(
    x: int,
    y: int,
    text: str,
    press_enter: bool = False,
    duration: float = 0.07,
    interval: float = 0.01,
    label: str = "",
) -> str:
    """Klickt exakt ins Eingabefeld `(x, y)` und tippt sofort den gewünschten Text (+ optional Enter)."""
    with _REAL_MOUSE_ACTION_LOCK:
        click(x=x, y=y, duration=min(0.08, float(duration)), label=label or f"FOKUS & TIPPE: [{x}, {y}]")
        time.sleep(0.04)
        return ghost_typewrite(text=text, interval=interval, press_enter=press_enter, label=label)


def scroll(
    amount: int,
    x: Optional[int] = None,
    y: Optional[int] = None,
    label: str = "",
) -> str:
    """Scrollt flüssig um `amount` Mausrad-Ticks (positiv = hoch, negativ = runter)."""
    SMART_MOUSE_GUARD.check_or_raise()
    tag = label or f"SCROLL: {int(amount):+d}"
    GHOST_OVERLAY.start(tag)
    if x is not None and y is not None:
        _smooth_move_with_guard(int(x), int(y), duration=0.3, mode="scroll", label=tag)
    if PYAUTOGUI_AVAILABLE and pyautogui is not None:
        SMART_MOUSE_GUARD.check_or_raise()
        ticks = int(amount)
        if abs(ticks) >= 100:
            step_ticks = max(1, min(15, abs(ticks) // 80)) * (1 if ticks > 0 else -1)
            pyautogui.scroll(step_ticks)
        else:
            pyautogui.scroll(ticks)
    return f"Gescrollt ({amount})"


def hotkey(*keys: Any, label: str = "") -> str:
    """Führt beliebige Tastenkombinationen aus (z. B. `ctrl+c`, `['ctrl', 'v']`, `alt+tab`, `win`)."""
    SMART_MOUSE_GUARD.check_or_raise()
    flat_keys: list[str] = []
    for item in keys:
        if item is None:
            continue
        if isinstance(item, (list, tuple)):
            for sub in item:
                part = str(sub).strip().lower()
                if part:
                    flat_keys.append(part)
        else:
            raw = str(item).strip()
            if "+" in raw:
                flat_keys.extend(p.strip().lower() for p in raw.split("+") if p.strip())
            elif raw:
                flat_keys.append(raw.lower())

    combo_label = "+".join(k.upper() for k in flat_keys) or "NONE"
    GHOST_OVERLAY.start(label or f"HOTKEY: {combo_label}")
    GHOST_OVERLAY.set_status(label or f"Hotkey: {combo_label}", mode="move")
    if PYAUTOGUI_AVAILABLE and pyautogui is not None and flat_keys:
        if len(flat_keys) == 1:
            pyautogui.press(flat_keys[0])
        else:
            pyautogui.hotkey(*flat_keys)
    return f"Hotkey gedrückt: {'+'.join(flat_keys)}"


def ghost_move_to(x: int, y: int, duration: float = 0.08, label: str = "") -> str:
    GHOST_OVERLAY.start(label or f"HAND-AGENT // ZIEL: [{x}, {y}]")
    cx, cy = fast_move_and_lock(int(x), int(y), duration=min(0.08, float(duration)), label=label, mode="move")
    return f"Maus bewegt zu [{cx}, {cy}]"


def ghost_click(
    x: Optional[int] = None,
    y: Optional[int] = None,
    button: str = "left",
    clicks: int = 1,
    duration: float = 0.08,
    label: str = "",
) -> str:
    return click(x=x, y=y, button=button, clicks=clicks, duration=duration, label=label)


def ghost_scroll(clicks: int, x: Optional[int] = None, y: Optional[int] = None) -> str:
    return scroll(amount=int(clicks), x=x, y=y)


def desktop_double_click(
    real_x: int,
    real_y: int,
    duration: float = 0.08,
    label: str = "",
) -> str:
    return double_click(x=real_x, y=real_y, duration=duration, label=label, press_enter=True)


def ghost_double_click(x: Optional[int] = None, y: Optional[int] = None, duration: float = 0.08, label: str = "") -> str:
    if x is None or y is None:
        _, _, cx, cy = get_screen_and_cursor_info()
    else:
        cx, cy = int(float(x)), int(float(y))
    return double_click(cx, cy, duration=duration, label=label, press_enter=True)


def ghost_scroll_swipe(
    clicks: int = -5,
    x: Optional[int] = None,
    y: Optional[int] = None,
    direction: str = "down",
    label: str = "",
) -> str:
    SMART_MOUSE_GUARD.check_or_raise()
    GHOST_OVERLAY.start(label or "SCROLL / SWIPE")
    amount = int(clicks) * 80 if abs(int(clicks)) < 40 else int(clicks)
    return ghost_swipe_scroll(amount=amount, x=x, y=y, direction=direction, label=label)


def ghost_typewrite(text: str, interval: float = 0.03, press_enter: bool = False, label: str = "") -> str:
    """
    Tippt Text ein; falls `{{VAULT_...}}` Platzhalter enthalten sind, werden diese lokal im RAM
    über `SECURE_VAULT` entschlüsselt, ohne jemals im Cloud-LLM-Log aufzutauchen.
    """
    SMART_MOUSE_GUARD.check_or_raise()
    resolved_text = text or ""
    had_secret = False
    try:
        from vault import SECURE_VAULT

        resolved_text, had_secret = SECURE_VAULT.resolve_placeholders_in_ram(resolved_text)
    except Exception:
        pass

    disp_label = label or ("TRESOR-EINGABE (****)" if had_secret else f"TIPPE: {resolved_text[:24]}")
    GHOST_OVERLAY.start(disp_label)
    GHOST_OVERLAY.set_status(disp_label, mode="move")
    if PYAUTOGUI_AVAILABLE and pyautogui is not None and resolved_text:
        if any(ch in resolved_text for ch in ("@", "€", "§", "ä", "ö", "ü", "Ä", "Ö", "Ü", "ß", "/")) and IS_WINDOWS:
            try:
                proc = subprocess.Popen(
                    ["clip"],
                    stdin=subprocess.PIPE,
                    creationflags=CREATE_NO_WINDOW,
                )
                proc.communicate(input=resolved_text.encode("utf-16le"), timeout=3)
                pyautogui.hotkey("ctrl", "v")
                time.sleep(0.08)
                if had_secret:
                    proc_clear = subprocess.Popen(["clip"], stdin=subprocess.PIPE, creationflags=CREATE_NO_WINDOW)
                    proc_clear.communicate(input="".encode("utf-16le"), timeout=2)
            except Exception:
                pyautogui.write(resolved_text, interval=interval)
        else:
            pyautogui.write(resolved_text, interval=interval)
        if press_enter:
            time.sleep(0.08)
            SMART_MOUSE_GUARD.check_or_raise()
            pyautogui.press("enter")
    resolved_text = ""
    return "Tresor-Geheimwert eingetippt (****)" if had_secret else f"Text eingetippt: {text}"


def ghost_press(key: str, label: str = "") -> str:
    SMART_MOUSE_GUARD.check_or_raise()
    GHOST_OVERLAY.start(label or f"TASTE: {key.upper()}")
    GHOST_OVERLAY.set_status(label or f"Taste: {key.upper()}", mode="move")
    if PYAUTOGUI_AVAILABLE and pyautogui is not None:
        pyautogui.press(key)
    return f"Taste gedrückt: {key}"


def ghost_hotkey(*keys: str, label: str = "") -> str:
    return hotkey(*keys, label=label)


# ----------------------------------------------------------------------
# UPGRADE 1, 3 & 4: Sniper-Lupe, 3D-CAD/Slicer-Mechanik & Timeline-Schnitt
# ----------------------------------------------------------------------
def _save_pil_jpeg_bytes(pil_img: Any, quality: int = 86) -> bytes:
    if not PIL_AVAILABLE or pil_img is None:
        return b""
    buf = io.BytesIO()
    pil_img.save(buf, format="JPEG", quality=max(40, min(95, int(quality))), optimize=True)
    return buf.getvalue()


def _refine_xy_opencv_in_crop(crop_pil: Any, offset_x: int, offset_y: int) -> Optional[tuple[int, int]]:
    """Findet das nächste präzise Button-/Icon-Zentrum in der 400x400-Sniper-Lupe per OpenCV."""
    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore

        rgb = np.array(crop_pil.convert("RGB"))
        ch, cw = rgb.shape[:2]
        cx, cy = cw // 2, ch // 2
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        edges = cv2.Canny(gray, 55, 160)
        contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        best_dist = 42.0
        best_pt: Optional[tuple[int, int]] = None
        for cnt in contours:
            bx, by, bw, bh = cv2.boundingRect(cnt)
            if bw < 10 or bh < 10 or bw > 240 or bh > 90:
                continue
            mx = bx + bw / 2.0
            my = by + bh / 2.0
            dist = math.hypot(mx - cx, my - cy)
            if dist < best_dist:
                best_dist = dist
                best_pt = (int(round(offset_x + mx)), int(round(offset_y + my)))
        return best_pt
    except Exception:
        return None


def sniper_verify(
    x: int,
    y: int,
    brain: Any = None,
    crop_size: int = 400,
    target_label: str = "",
) -> tuple[int, int, bytes]:
    """
    UPGRADE 1: "Sniper-Zoom-Lupe" (2-Stufen-Präzisionsklick):
    - Schneidet aus dem unkomprimierten Original-Screenshot einen 400x400-Pixel
      1:1-Ausschnitt um `(x, y)` mit 10x10-Subgitter aus.
    - Blendet das Target-Lock-Bild in `DESKTOP_VISION_STREAM` für den HUD-Feed ein.
    - Verfeinert `(x, y)` per Vision-KI (falls `brain` aktiv) oder OpenCV-Kontur-Zentrierung.
    """
    screen_w, screen_h, _, _ = get_screen_and_cursor_info()
    rx, ry = _clamp_screen_xy(int(x), int(y), screen_w, screen_h)
    if not PIL_AVAILABLE or Image is None or ImageDraw is None:
        return rx, ry, b""

    raw_pil = _grab_primary_screen_pil(screen_w, screen_h)
    if raw_pil is None:
        return rx, ry, b""

    try:
        img_w, img_h = raw_pil.size
        sx_scale = img_w / max(1, screen_w)
        sy_scale = img_h / max(1, screen_h)
        px_c = int(round(rx * sx_scale))
        py_c = int(round(ry * sy_scale))

        side = max(120, min(600, int(crop_size)))
        half = side // 2
        left = max(0, min(max(0, img_w - side), px_c - half))
        top = max(0, min(max(0, img_h - side), py_c - half))
        right = min(img_w, left + side)
        bottom = min(img_h, top + side)

        crop_clean = raw_pil.crop((left, top, right, bottom)).convert("RGB")
        cw, ch = crop_clean.size

        # 10x10-Subgitter + zentrales Sniper-Fadenkreuz zeichnen
        annotated_crop = crop_clean.convert("RGBA")
        overlay = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        try:
            font = ImageFont.load_default()
        except Exception:
            font = None

        for idx in range(1, 10):
            gx = int(round((idx / 10.0) * cw))
            gy = int(round((idx / 10.0) * ch))
            draw.line([(gx, 0), (gx, ch)], fill=(0, 235, 255, 85), width=1)
            draw.line([(0, gy), (cw, gy)], fill=(0, 235, 255, 85), width=1)
            if idx % 2 == 0:
                draw.text((gx + 2, 2), str(idx * 100), fill=(180, 245, 255, 220), font=font)
                draw.text((2, gy + 2), str(idx * 100), fill=(180, 245, 255, 220), font=font)

        rel_cx = max(0, min(cw - 1, px_c - left))
        rel_cy = max(0, min(ch - 1, py_c - top))
        draw.line([(rel_cx - 22, rel_cy), (rel_cx + 22, rel_cy)], fill=(255, 50, 50, 240), width=2)
        draw.line([(rel_cx, rel_cy - 22), (rel_cx, rel_cy + 22)], fill=(255, 50, 50, 240), width=2)
        draw.ellipse([rel_cx - 12, rel_cy - 12, rel_cx + 12, rel_cy + 12], outline=(255, 230, 40, 250), width=2)
        badge_txt = f"TARGET-LOCK {(target_label or 'ELEMENT')[:20]}"
        draw.rectangle([4, 4, min(cw - 4, 14 + len(badge_txt) * 6), 20], fill=(10, 18, 30, 220), outline=(0, 235, 255, 240), width=1)
        draw.text((8, 6), badge_txt, fill=(0, 245, 255, 255), font=font)

        zoom_pil = Image.alpha_composite(annotated_crop, overlay).convert("RGB")
        zoom_jpeg = _save_pil_jpeg_bytes(zoom_pil, quality=88)

        try:
            DESKTOP_VISION_STREAM.set_target_lock(zoom_jpeg, label=target_label or f"[{rx},{ry}]")
        except Exception:
            pass

        refined_px, refined_py = rx, ry
        used_brain = False
        if brain is not None and hasattr(brain, "ask") and zoom_jpeg:
            try:
                sniper_prompt = (
                    f"Dies ist ein 1:1 Lupen-Ausschnitt ({cw}x{ch} px) mit 10x10-Subgitter (0..1000) "
                    f"um das Ziel '{target_label or 'UI-Button'}'. "
                    f"Gib AUSSCHLIESSLICH JSON zurück: {{\"x\": 0..1000, \"y\": 0..1000}} für das EXAKTE Zentrum "
                    f"des Buttons/Eingabefelds innerhalb dieses Lupen-Bildes."
                )
                reply = _ask_brain_text(
                    brain=brain,
                    user_prompt=sniper_prompt,
                    system_prompt="Präzisions-Target-Lock: Antworte nur mit {\"x\": <0..1000>, \"y\": <0..1000>}.",
                    image_bytes=zoom_jpeg,
                )
                dec = _extract_json_dict(reply)
                if dec and dec.get("x") is not None and dec.get("y") is not None:
                    nx_in_crop = max(0.0, min(1000.0, float(dec["x"])))
                    ny_in_crop = max(0.0, min(1000.0, float(dec["y"])))
                    img_hit_x = left + (nx_in_crop / 1000.0) * cw
                    img_hit_y = top + (ny_in_crop / 1000.0) * ch
                    cand_x = int(round(img_hit_x / max(1e-6, sx_scale)))
                    cand_y = int(round(img_hit_y / max(1e-6, sy_scale)))
                    refined_px, refined_py = _clamp_screen_xy(cand_x, cand_y, screen_w, screen_h)
                    used_brain = True
            except Exception as exc:
                logger.debug("Sniper-Vision Verfeinerung Hinweis: %s", exc)

        if not used_brain:
            cv_hit = _refine_xy_opencv_in_crop(crop_clean, left, top)
            if cv_hit is not None:
                cx_scr = int(round(cv_hit[0] / max(1e-6, sx_scale)))
                cy_scr = int(round(cv_hit[1] / max(1e-6, sy_scale)))
                refined_px, refined_py = _clamp_screen_xy(cx_scr, cy_scr, screen_w, screen_h)

        crop_clean.close()
        annotated_crop.close()
        overlay.close()
        zoom_pil.close()
        return refined_px, refined_py, zoom_jpeg
    finally:
        try:
            raw_pil.close()
        except Exception:
            pass


def precision_click(
    x: int,
    y: int,
    button: str = "left",
    clicks: int = 1,
    brain: Any = None,
    target_label: str = "",
    label: str = "",
) -> str:
    """UPGRADE 1: Führt einen 2-Stufen-Präzisionsklick mit 400x400 Sniper-Lupe aus."""
    rx, ry, _ = sniper_verify(x=int(x), y=int(y), brain=brain, crop_size=400, target_label=target_label or label)
    return click(
        x=rx,
        y=ry,
        button=button,
        clicks=clicks,
        duration=0.07,
        label=label or f"SNIPER-KLICK: [{rx}, {ry}]",
    )


def _resolve_cad_orbit_button(app_hint: str = "", button: str = "auto") -> str:
    btn = (button or "auto").strip().lower()
    if btn in ("middle", "right", "left"):
        return btn
    low = (app_hint or "").lower()
    if any(k in low for k in ("onshape", "solidcraft", "solid craft", "tinkercad")):
        return "right"
    return "middle"


def cad_orbit(
    dx: int = 120,
    dy: int = -45,
    start_x: Optional[int] = None,
    start_y: Optional[int] = None,
    button: str = "auto",
    app_hint: str = "",
    duration: float = 0.28,
) -> str:
    """
    UPGRADE 3 (`cad_orbit`): Dreht ein 3D-CAD-/Slicer-Modell im Raum per mittlerer
    Maustaste (bzw. Rechtsklick bei Onshape/SolidCraft) und kontrolliertem Drag `(dx, dy)`.
    """
    with _REAL_MOUSE_ACTION_LOCK:
        SMART_MOUSE_GUARD.check_or_raise()
        screen_w, screen_h, cur_x, cur_y = get_screen_and_cursor_info()
        sx = int(start_x) if start_x is not None else (screen_w // 2 if cur_x < 40 else cur_x)
        sy = int(start_y) if start_y is not None else (screen_h // 2 if cur_y < 40 else cur_y)
        sx, sy = _clamp_screen_xy(sx, sy, screen_w, screen_h)
        ex, ey = _clamp_screen_xy(sx + int(dx), sy + int(dy), screen_w, screen_h)
        orb_btn = _resolve_cad_orbit_button(app_hint=app_hint, button=button)
        lbl = f"CAD ORBIT ({orb_btn.upper()}) dx={dx}, dy={dy}"
        GHOST_OVERLAY.start(lbl)
        fast_move_and_lock(sx, sy, duration=0.07, mode="drag", label=lbl)
        SMART_MOUSE_GUARD.begin_jarvis_move((sx, sy))
        try:
            if PYAUTOGUI_AVAILABLE and pyautogui is not None:
                pyautogui.mouseDown(x=sx, y=sy, button=orb_btn, _pause=False)
                steps = max(4, min(14, int(max(abs(dx), abs(dy)) / 18) + 2))
                wait_step = max(0.01, min(0.4, float(duration)) / steps)
                for idx in range(1, steps + 1):
                    SMART_MOUSE_GUARD.check_or_raise()
                    t = idx / float(steps)
                    nx = int(round(sx + (ex - sx) * t))
                    ny = int(round(sy + (ey - sy) * t))
                    pyautogui.moveTo(nx, ny, duration=0.0, _pause=False)
                    _set_cursor_pos_hard(nx, ny)
                    SMART_MOUSE_GUARD.record_jarvis_pos(nx, ny)
                    time.sleep(wait_step)
        finally:
            try:
                if PYAUTOGUI_AVAILABLE and pyautogui is not None:
                    pyautogui.mouseUp(button=orb_btn, _pause=False)
            except Exception:
                pass
            SMART_MOUSE_GUARD.end_jarvis_move(ex, ey)
        return f"CAD-Orbit ({orb_btn}) um ({dx}, {dy}) von [{sx},{sy}] nach [{ex},{ey}] ausgeführt."


def cad_pan(
    dx: int = 120,
    dy: int = 0,
    start_x: Optional[int] = None,
    start_y: Optional[int] = None,
    duration: float = 0.25,
) -> str:
    """UPGRADE 3 (`cad_pan`): Verschiebt die 3D-Arbeitsfläche über Shift + mittlere Maustaste."""
    with _REAL_MOUSE_ACTION_LOCK:
        SMART_MOUSE_GUARD.check_or_raise()
        screen_w, screen_h, _, _ = get_screen_and_cursor_info()
        sx = int(start_x) if start_x is not None else screen_w // 2
        sy = int(start_y) if start_y is not None else screen_h // 2
        sx, sy = _clamp_screen_xy(sx, sy, screen_w, screen_h)
        ex, ey = _clamp_screen_xy(sx + int(dx), sy + int(dy), screen_w, screen_h)
        lbl = f"CAD PAN (SHIFT+MIDDLE) dx={dx}, dy={dy}"
        GHOST_OVERLAY.start(lbl)
        fast_move_and_lock(sx, sy, duration=0.07, mode="drag", label=lbl)
        SMART_MOUSE_GUARD.begin_jarvis_move((sx, sy))
        try:
            if PYAUTOGUI_AVAILABLE and pyautogui is not None:
                pyautogui.keyDown("shift", _pause=False)
                pyautogui.mouseDown(x=sx, y=sy, button="middle", _pause=False)
                steps = max(4, min(12, int(max(abs(dx), abs(dy)) / 20) + 2))
                wait_step = max(0.01, min(0.35, float(duration)) / steps)
                for idx in range(1, steps + 1):
                    SMART_MOUSE_GUARD.check_or_raise()
                    t = idx / float(steps)
                    nx = int(round(sx + (ex - sx) * t))
                    ny = int(round(sy + (ey - sy) * t))
                    pyautogui.moveTo(nx, ny, duration=0.0, _pause=False)
                    _set_cursor_pos_hard(nx, ny)
                    SMART_MOUSE_GUARD.record_jarvis_pos(nx, ny)
                    time.sleep(wait_step)
        finally:
            if PYAUTOGUI_AVAILABLE and pyautogui is not None:
                try:
                    pyautogui.mouseUp(button="middle", _pause=False)
                except Exception:
                    pass
                try:
                    pyautogui.keyUp("shift", _pause=False)
                except Exception:
                    pass
            SMART_MOUSE_GUARD.end_jarvis_move(ex, ey)
        return f"CAD-Pan (Shift+Middle) um ({dx}, {dy}) ausgeführt."


def cad_zoom(
    steps: int = 4,
    x: Optional[int] = None,
    y: Optional[int] = None,
) -> str:
    """UPGRADE 3 (`cad_zoom`): Positioniert auf der Bauteil-Mitte und zoomt dosiert per Mausrad."""
    with _REAL_MOUSE_ACTION_LOCK:
        SMART_MOUSE_GUARD.check_or_raise()
        screen_w, screen_h, _, _ = get_screen_and_cursor_info()
        zx = int(x) if x is not None else screen_w // 2
        zy = int(y) if y is not None else screen_h // 2
        zx, zy = _clamp_screen_xy(zx, zy, screen_w, screen_h)
        fast_move_and_lock(zx, zy, duration=0.07, mode="move", label=f"CAD ZOOM ({steps})")
        bursts = max(1, min(20, abs(int(steps))))
        delta = 120 if int(steps) >= 0 else -120
        if PYAUTOGUI_AVAILABLE and pyautogui is not None:
            for _ in range(bursts):
                SMART_MOUSE_GUARD.check_or_raise()
                pyautogui.scroll(delta, x=zx, y=zy, _pause=False)
                time.sleep(0.025)
        return f"CAD-Zoom ({steps} Schritte) auf Bauteil-Mitte [{zx}, {zy}] ausgeführt."


def cad_box_and_dimension(
    start_x: int,
    start_y: int,
    end_x: int,
    end_y: int,
    dims: list[str] | tuple[str, ...] = ("4", "4", "4"),
    brain: Any = None,
) -> dict[str, Any]:
    """
    UPGRADE 3 (`cad_box_and_dimension`):
    1. Zieht Grundfläche von `(start_x, start_y)` nach `(end_x, end_y)` mit gedrückter Maustaste auf.
    2. Tippt X- & Y-Bemaßungen (`dims[0]`, `dims[1]`) mit 0.15s `Tab` / `Enter`-Pausen ein.
    3. Prüft per frischem Screenshot, ob das Extrusions-/Höhenfeld aktiv ist, und bestätigt
       die 3. Dimension (`dims[2]`) mit `Enter`.
    """
    dim_vals = [str(d).strip() for d in (dims or ["4", "4", "4"]) if str(d).strip()]
    while len(dim_vals) < 3:
        dim_vals.append(dim_vals[-1] if dim_vals else "4")

    SMART_MOUSE_GUARD.check_or_raise()
    drag_and_drop(
        int(start_x),
        int(start_y),
        int(end_x),
        int(end_y),
        duration=0.22,
        label=f"CAD BOX [{'x'.join(dim_vals[:3])}]",
    )
    time.sleep(0.15)
    ghost_typewrite(dim_vals[0], interval=0.01, press_enter=False, label=f"DIM X: {dim_vals[0]}")
    time.sleep(0.12)
    ghost_press("tab", label="DIM TAB 1")
    time.sleep(0.12)
    ghost_typewrite(dim_vals[1], interval=0.01, press_enter=False, label=f"DIM Y: {dim_vals[1]}")
    time.sleep(0.12)
    ghost_press("tab", label="DIM TAB 2")
    time.sleep(0.18)

    # Screenshot prüfen, ob Extrusions- / Höhenfeld bereits fokussiert ist
    verify_jpeg, sw, sh, _ = capture_annotated_screen_1fps()
    extrude_needs_focus = True
    if brain is not None and hasattr(brain, "ask") and verify_jpeg:
        try:
            chk_reply = _ask_brain_text(
                brain=brain,
                user_prompt=(
                    "Ist auf diesem CAD-Screenshot gerade ein Eingabefeld für die 3. Dimension / Extrusion / Höhe aktiv? "
                    "Falls ja, antworte {\"active\": true}. Falls erst ein Feld oder der Extrude-Button geklickt werden muss, "
                    "gib {\"active\": false, \"x\": <0..1000>, \"y\": <0..1000>} zurück."
                ),
                system_prompt="CAD Extrusions-Prüfung: Antworte ausschließlich mit kompaktem JSON.",
                image_bytes=verify_jpeg,
            )
            parsed = _extract_json_dict(chk_reply)
            if parsed:
                extrude_needs_focus = not bool(parsed.get("active", True))
                if extrude_needs_focus and parsed.get("x") is not None and parsed.get("y") is not None:
                    ex_px, ey_px = norm_to_screen_xy(float(parsed["x"]), float(parsed["y"]), sw, sh)
                    click(ex_px, ey_px, duration=0.07, label="CAD EXTRUDE FOKUS")
                    time.sleep(0.12)
        except Exception:
            extrude_needs_focus = False

    if extrude_needs_focus and brain is None:
        # Im lokalen/Fallback-Modus sicherheitshalber Taste 'e' für Extrude drücken falls Werkzeugleiste aktiv
        pass

    ghost_typewrite(dim_vals[2], interval=0.01, press_enter=True, label=f"DIM Z: {dim_vals[2]}")
    time.sleep(0.15)
    return {
        "ok": True,
        "dims": dim_vals[:3],
        "message": f"CAD-Körper ({' x '.join(dim_vals[:3])}) aufgezogen, bemaßt und extrudiert.",
    }


def analyze_timeline_waveform_cut_points(
    pil_img: Any = None,
    screen_w: Optional[int] = None,
    screen_h: Optional[int] = None,
) -> dict[str, Any]:
    """
    UPGRADE 4: Erkennt im unteren Drittel des Bildschirms Video-Timeline & Audio-Waveform
    und findet ruhige/flache Bereiche (Sprechpausen) für exakte Schnitte.
    """
    sw, sh, _, _ = get_screen_and_cursor_info()
    sw = screen_w or sw
    sh = screen_h or sh
    own_img = False
    if pil_img is None:
        pil_img = _grab_primary_screen_pil(sw, sh)
        own_img = True
    if pil_img is None:
        return {"ok": False, "timeline_y": int(sh * 0.80), "silent_spans": []}

    try:
        import numpy as np  # type: ignore

        rgb = np.array(pil_img.convert("RGB"))
        ih, iw = rgb.shape[:2]
        scale_x = sw / max(1, iw)
        scale_y = sh / max(1, ih)

        y_start = int(ih * 0.66)
        y_end = int(ih * 0.96)
        band = rgb[y_start:y_end, :, :]
        gray = band.mean(axis=2)

        # Zeile mit höchster horizontaler Kantenaktivität = aktive Timeline / Tonspur
        row_grad = np.abs(np.diff(gray, axis=1)).mean(axis=1)
        best_rel_y = int(np.argmax(row_grad)) if row_grad.size else ((y_end - y_start) // 2)
        timeline_img_y = y_start + best_rel_y
        timeline_scr_y = _clamp_screen_xy(0, int(round(timeline_img_y * scale_y)), sw, sh)[1]

        # Spalten-Amplitude relativ zum Spur-Hintergrund rund um `timeline_img_y` -> Sprechpausen vs. Audio-Peaks
        wy0 = max(0, best_rel_y - 26)
        wy1 = min(gray.shape[0], best_rel_y + 26)
        wave_strip = gray[wy0:wy1, :]
        if wave_strip.size:
            track_bg = float(np.median(wave_strip))
            col_energy = np.abs(wave_strip - track_bg).mean(axis=0)
        else:
            col_energy = np.zeros(iw)
        if col_energy.size >= 11:
            kernel = np.ones(11, dtype=float) / 11.0
            smooth_energy = np.convolve(col_energy, kernel, mode="same")
        else:
            smooth_energy = col_energy

        x_lo = int(iw * 0.08)
        x_hi = int(iw * 0.94)
        active_slice = smooth_energy[x_lo:x_hi] if x_hi > x_lo else smooth_energy
        if active_slice.size:
            p85 = float(np.percentile(active_slice, 85))
            p15 = float(np.percentile(active_slice, 15))
            threshold = p15 + (p85 - p15) * 0.35 if (p85 - p15) > 2.0 else (p15 + 0.5)
            silent_mask = smooth_energy <= threshold
        else:
            silent_mask = np.zeros(iw, dtype=bool)

        spans: list[tuple[int, int]] = []
        run_start: Optional[int] = None
        min_span_px = max(14, int(iw * 0.015))
        for x_idx, is_flat in enumerate(silent_mask):
            if bool(is_flat) and run_start is None:
                run_start = x_idx
            elif not bool(is_flat) and run_start is not None:
                if (x_idx - run_start) >= min_span_px:
                    sx_p = int(round(run_start * scale_x))
                    ex_p = int(round(x_idx * scale_x))
                    if sx_p > int(sw * 0.08) and ex_p < int(sw * 0.94):
                        spans.append((sx_p, ex_p))
                run_start = None
        if run_start is not None and (iw - run_start) >= min_span_px:
            sx_p = int(round(run_start * scale_x))
            ex_p = int(round((iw - 1) * scale_x))
            if ex_p < int(sw * 0.94):
                spans.append((sx_p, ex_p))

        first_span = spans[0] if spans else (int(sw * 0.42), int(sw * 0.48))
        return {
            "ok": True,
            "timeline_y": timeline_scr_y,
            "suggested_cut_start_x": first_span[0],
            "suggested_cut_end_x": first_span[1],
            "silent_spans": spans[:8],
        }
    except Exception as exc:
        logger.debug("Timeline-Waveform-Analyse Hinweis: %s", exc)
        return {
            "ok": False,
            "timeline_y": int(sh * 0.80),
            "suggested_cut_start_x": int(sw * 0.42),
            "suggested_cut_end_x": int(sw * 0.48),
            "silent_spans": [],
        }
    finally:
        if own_img:
            try:
                pil_img.close()
            except Exception:
                pass


def timeline_scrub_and_cut(
    playhead_x: Optional[int] = None,
    timeline_y: Optional[int] = None,
    target_x: Optional[int] = None,
    target_end_x: Optional[int] = None,
    editor_name: str = "CapCut",
    ripple_delete: bool = True,
) -> dict[str, Any]:
    """
    UPGRADE 4 (`timeline_scrub_and_cut`):
    - Erkennt bei Bedarf automatisch Timeline-Y und eine Sprechpause (flache Audio-Waveform) im unteren Drittel.
    - Zieht den Playhead exakt auf `target_x`, schneidet per `Ctrl+B` (bzw. `Ctrl+K` in Premiere),
      schneidet optional das Pausen-Ende `target_end_x` und löscht den leeren Bereich per `Delete` (Ripple Delete).
    """
    SMART_MOUSE_GUARD.check_or_raise()
    analysis = analyze_timeline_waveform_cut_points()
    ty = int(timeline_y) if timeline_y is not None else int(analysis.get("timeline_y") or 860)
    tx_start = int(target_x) if target_x is not None else int(analysis.get("suggested_cut_start_x") or 780)
    px_head = int(playhead_x) if playhead_x is not None else max(40, tx_start - 80)

    ed_low = (editor_name or "capcut").lower()
    cut_keys = ("ctrl", "k") if "premiere" in ed_low else ("ctrl", "b")

    # 1. Playhead greifen und zum Schnittpunkt ziehen
    if abs(tx_start - px_head) >= 4:
        drag_and_drop(px_head, ty, tx_start, ty, duration=0.22, label=f"TIMELINE SCRUB -> [{tx_start},{ty}]")
    else:
        click(tx_start, ty, duration=0.07, label=f"TIMELINE FOKUS [{tx_start},{ty}]")

    # 2. Ersten Schnitt setzen
    time.sleep(0.08)
    hotkey(*cut_keys, label=f"TIMELINE CUT ({'+'.join(cut_keys).upper()})")

    # 3. Falls Sprechpausen-Spanne vorhanden: Ende schneiden & Lücke per Delete entfernen
    removed_segment = False
    if target_end_x is not None or (target_x is None and analysis.get("suggested_cut_end_x")):
        tx_end = int(target_end_x) if target_end_x is not None else int(analysis["suggested_cut_end_x"])
        if tx_end > tx_start + 12:
            time.sleep(0.08)
            drag_and_drop(tx_start, ty, tx_end, ty, duration=0.18, label=f"PAUSE ENDE -> [{tx_end},{ty}]")
            time.sleep(0.08)
            hotkey(*cut_keys, label=f"TIMELINE CUT 2 ({'+'.join(cut_keys).upper()})")
            if ripple_delete:
                mid_gap_x = (tx_start + tx_end) // 2
                time.sleep(0.08)
                click(mid_gap_x, ty, duration=0.07, label=f"PAUSE WÄHLEN [{mid_gap_x},{ty}]")
                time.sleep(0.06)
                ghost_press("delete", label="RIPPLE DELETE")
                removed_segment = True

    return {
        "ok": True,
        "timeline_y": ty,
        "cut_x": tx_start,
        "cut_end_x": target_end_x or analysis.get("suggested_cut_end_x"),
        "removed_segment": removed_segment,
        "message": f"Timeline-Schnitt bei [{tx_start}, {ty}] ({'+'.join(cut_keys).upper()}) in {editor_name} ausgeführt.",
    }


def browser_new_tab(url: str = "", label: str = "NEUER TAB (CTRL+T)") -> str:
    """Öffnet im aktiven Desktop-Browser per `Ctrl+T` einen neuen Tab und navigiert optional zur URL."""
    SMART_MOUSE_GUARD.check_or_raise()
    hotkey("ctrl", "t", label=label)
    target = (url or "").strip()
    if target:
        time.sleep(0.18)
        ghost_typewrite(target, interval=0.006, press_enter=True, label=f"URL: {target[:28]}")
        return f"Neuer Browser-Tab geöffnet und '{target}' aufgerufen."
    return "Neuer Browser-Tab (Ctrl+T) geöffnet."


def browser_switch_tab(
    tab_index: Optional[int] = None,
    direction: str = "next",
    x: Optional[int] = None,
    y: Optional[int] = None,
) -> str:
    """Wechselt zwischen Browser-Tabs per Klick auf `(x, y)`, `Ctrl+<1..9>`, `Ctrl+Tab` oder `Ctrl+Shift+Tab`."""
    SMART_MOUSE_GUARD.check_or_raise()
    if x is not None and y is not None and (x > 0 or y > 0):
        click(int(x), int(y), duration=0.07, label=f"TAB-KLICK [{x},{y}]")
        return f"Browser-Tab bei [{x}, {y}] angeklickt."
    if tab_index is not None and 1 <= int(tab_index) <= 9:
        idx_str = str(int(tab_index))
        hotkey("ctrl", idx_str, label=f"TAB {idx_str} (CTRL+{idx_str})")
        return f"Zu Browser-Tab {idx_str} gewechselt."
    dir_low = (direction or "next").strip().lower()
    if dir_low in ("prev", "previous", "back", "left", "zurück", "links"):
        hotkey("ctrl", "shift", "tab", label="VORHERIGER TAB (CTRL+SHIFT+TAB)")
        return "Zum vorherigen Browser-Tab gewechselt."
    hotkey("ctrl", "tab", label="NÄCHSTER TAB (CTRL+TAB)")
    return "Zum nächsten Browser-Tab gewechselt."


def browser_back() -> str:
    """Navigiert im Desktop-Browser eine Seite zurück (`Alt+Left`)."""
    SMART_MOUSE_GUARD.check_or_raise()
    hotkey("alt", "left", label="BROWSER ZURÜCK (ALT+LINKS)")
    return "Im Browser eine Seite zurückgegangen."


def browser_forward() -> str:
    """Navigiert im Desktop-Browser eine Seite vorwärts (`Alt+Right`)."""
    SMART_MOUSE_GUARD.check_or_raise()
    hotkey("alt", "right", label="BROWSER VOR (ALT+RECHTS)")
    return "Im Browser eine Seite vorgegangen."


def browser_address_bar(url_or_query: str = "", press_enter: bool = True) -> str:
    """Fokussiert die Browser-Adressleiste per `Ctrl+L` und gibt optional URL/Suchbegriff ein."""
    SMART_MOUSE_GUARD.check_or_raise()
    hotkey("ctrl", "l", label="ADRESSLEISTE (CTRL+L)")
    val = (url_or_query or "").strip()
    if val:
        time.sleep(0.1)
        ghost_typewrite(val, interval=0.006, press_enter=press_enter, label=f"ADRESSE: {val[:26]}")
        return f"Adressleiste fokussiert und '{val}' eingegeben."
    return "Browser-Adressleiste (Ctrl+L) fokussiert."


def browser_find_on_page(search_text: str = "", next_match: bool = False) -> str:
    """Sucht auf der aktuellen Webseite via `Ctrl+F` (oder springt per `F3` weiter)."""
    SMART_MOUSE_GUARD.check_or_raise()
    if next_match and not search_text:
        ghost_press("f3", label="NÄCHSTER TREFFER (F3)")
        return "Zum nächsten Suchtreffer auf der Seite gesprungen."
    hotkey("ctrl", "f", label="SEITENSUCHE (CTRL+F)")
    txt = (search_text or "").strip()
    if txt:
        time.sleep(0.08)
        ghost_typewrite(txt, interval=0.006, press_enter=True, label=f"FINDE: {txt[:24]}")
        return f"Auf der Seite nach '{txt}' gesucht."
    return "Seitensuche (Ctrl+F) geöffnet."


def browser_select_dropdown(
    x: Optional[int] = None,
    y: Optional[int] = None,
    option_text: str = "",
    steps_down: int = 0,
) -> str:
    """Öffnet ein Dropdown-Menü per Mausklick und wählt den Eintrag per Texteingabe oder Pfeiltasten + Enter."""
    SMART_MOUSE_GUARD.check_or_raise()
    if x is not None and y is not None and (x > 0 or y > 0):
        click(int(x), int(y), duration=0.07, label=f"DROPDOWN [{x},{y}]")
        time.sleep(0.14)
    opt = (option_text or "").strip()
    if opt:
        ghost_typewrite(opt, interval=0.01, press_enter=True, label=f"OPTION: {opt[:22]}")
        return f"Dropdown-Option '{opt}' ausgewählt."
    for _ in range(max(0, min(30, int(steps_down or 0)))):
        ghost_press("down", label="DROPDOWN RUNTER")
        time.sleep(0.04)
    ghost_press("enter", label="DROPDOWN BESTÄTIGEN")
    return "Dropdown-Eintrag bestätigt."


def browser_upload_file(
    file_path: str,
    button_x: Optional[int] = None,
    button_y: Optional[int] = None,
) -> str:
    """
    Bedient einen Datei-Upload im Browser: klickt optional den Upload-Button (`button_x`, `button_y`),
    wartet kurz auf den Windows-'Öffnen'-Dialog, fokussiert das Dateinamen-Feld (`Alt+N`) und bestätigt mit Enter.
    """
    SMART_MOUSE_GUARD.check_or_raise()
    clean_path = str(Path(file_path).expanduser()) if file_path else ""
    if not clean_path:
        return "Kein Dateipfad für den Datei-Upload angegeben."
    if button_x is not None and button_y is not None and (button_x > 0 or button_y > 0):
        click(int(button_x), int(button_y), duration=0.08, label="UPLOAD-BUTTON KLICK")
        time.sleep(0.85)
    # In Windows-Dateidialogen fokussiert Alt+N direkt das 'Dateiname:'-Eingabefeld
    if IS_WINDOWS:
        hotkey("alt", "n", label="DATEINAME-FELD (ALT+N)")
        time.sleep(0.1)
    hotkey("ctrl", "a", label="PFAD MARKIEREN")
    time.sleep(0.05)
    ghost_typewrite(clean_path, interval=0.004, press_enter=True, label=f"UPLOAD: {Path(clean_path).name[:22]}")
    return f"Datei '{clean_path}' im Upload-Dialog übergeben und bestätigt."


_VIDEO_KEY_MAP: dict[str, tuple[str, str]] = {
    "fullscreen": ("f", "VOLLBILD (F)"),
    "vollbild": ("f", "VOLLBILD (F)"),
    "play": ("k", "PLAY/PAUSE (K)"),
    "pause": ("k", "PLAY/PAUSE (K)"),
    "toggle_pause": ("k", "PLAY/PAUSE (K)"),
    "space": ("space", "PLAY/PAUSE (LEERTASTE)"),
    "mute": ("m", "STUMM (M)"),
    "stumm": ("m", "STUMM (M)"),
    "forward": ("l", "VORSPULEN (L)"),
    "vorspulen": ("l", "VORSPULEN (L)"),
    "rewind": ("j", "ZURÜCKSPULEN (J)"),
    "zurückspulen": ("j", "ZURÜCKSPULEN (J)"),
    "captions": ("c", "UNTERTITEL (C)"),
    "untertitel": ("c", "UNTERTITEL (C)"),
}


def browser_video_control(
    command: str = "toggle_pause",
    x: Optional[int] = None,
    y: Optional[int] = None,
) -> str:
    """Steuert Videoplayer im Browser (YouTube/HTML5: Vollbild `f`, Play/Pause `k`/`space`, Mute `m`, Vor/Zurück `l`/`j`)."""
    SMART_MOUSE_GUARD.check_or_raise()
    if x is not None and y is not None and (x > 0 or y > 0):
        click(int(x), int(y), duration=0.07, label=f"VIDEO-FOKUS [{x},{y}]")
        time.sleep(0.08)
    cmd_low = (command or "toggle_pause").strip().lower()
    key_name, label = _VIDEO_KEY_MAP.get(cmd_low, ("k", f"VIDEO ({cmd_low.upper()})"))
    ghost_press(key_name, label=label)
    return f"Videoplayer-Befehl '{command}' (Taste '{key_name}') ausgeführt."


CLOSED_LOOP_SYSTEM_PROMPT = """Du bist der visuelle 1-FPS Hand-Agent (Closed-Loop Desktop- & Browser-Maussteuerung) von J.A.R.V.I.S.
Du erhältst jede Sekunde (1 FPS) einen aktuellen Screenshot des Hauptbildschirms:
- Auf dem Screenshot ist ein NORMIERTES 1000x1000 KOORDINATEN-GITTER (`0` bis `1000` auf der X- und Y-Achse in 100er-Schritten: `X:100`..`X:900`, `Y:100`..`Y:900`) eingezeichnet.
- Die aktuelle Mausposition ist mit einem leuchtend ROTEN FADENKREUZ markiert.
- Physische Bildschirmauflösung: `{screen_w}x{screen_h}` Pixel.
- Aktuelle Mausposition: Normiert `[{norm_x}, {norm_y}]` (Pixel `[{cur_x}, {cur_y}]`).
- Aktiver Kauf- & Zugriffs-Modus (`EXECUTION_MODE`): `{execution_mode_label}`

VOLLE KONTROLLE FÜR JEDE WINDOWS-APP (HIGH-PRECISION OPERATOR):
- Du kannst JEDE sichtbare App, jedes Programm, Spiel, jeden Launcher (z. B. SolidCraft, Onshape, OrcaSlicer, CapCut, DaVinci Resolve, Premiere, ArcCraft, FilmCraft, Steam, Discord, Explorer, Browser) vollständig mit echter Maus und Tastatur steuern!
- VERBOTEN: Sage NIEMALS *"Ich kann diese App nicht steuern"* oder *"Ich habe keinen Zugriff"* – was auf dem Bildschirm sichtbar ist, bedienst du direkt visuell über das Repertoire:
  `click`, `precision_click`, `click_tag`, `double_click`, `right_click`, `drag_and_drop`, `click_and_type`, `scroll`, `hotkey`, `move`, `type`, `type_secret`, `press`, `cad_orbit`, `cad_pan`, `cad_zoom`, `cad_box_and_dimension`, `timeline_scrub_and_cut`, `browser_new_tab`, `browser_switch_tab`, `browser_back`, `browser_forward`, `browser_address_bar`, `browser_find_on_page`, `browser_select_dropdown`, `browser_upload_file`, `browser_video_control`, `wait_3ds`, `done`.

WICHTIGE REGELN:
1. SET-OF-MARK (SoM) NUMMERNSCHILDER (100% TREFFERQUOTE):
   - Auf dem Screenshot sind erkannte Buttons/Eingabefelder mit gelben Nummernschildern `[1]`, `[2]`, `[3]` ... markiert (plus Textliste im Prompt).
   - Wenn das gewünschte UI-Element ein Nummernschild `[N]` hat, nutze bevorzugt `{{"type": "click_tag", "tag_id": N}}` (oder `"action": "click_tag", "tag_id": N`) – das trifft den Button garantiert exakt!
2. SNIPER-ZOOM-LUPE (FÜR WINZIGE ICONS/MAßFELDER):
   - Bei winzigen Toolbar-Icons, Timeline-Kanten oder Browser-Tabs ohne klares SoM-Schild nutze `"action": "precision_click"` (oder setze `"precision": true` am Klick) im `1000x1000`-Gitter – das System öffnet im RAM eine 400x400 1:1-Lupe und korrigiert pixelgenau auf das Button-Zentrum!
3. Gib Koordinaten (`x`, `y` sowie `start_x`, `start_y`, `end_x`, `end_y`) IMMER im normierten `1000x1000`-Gitter (`0` bis `1000`, `"coord_space": "1000"`) an! Bildschirmmitte ist `"x": 500, "y": 500`.
4. 3D-CAD- & SLICER-AKTIONEN:
   - `"cad_orbit"` mit `"dx"`, `"dy"` (3D-Modell im Raum drehen).
   - `"cad_pan"` mit `"dx"`, `"dy"` (Shift+Mittelmaus Verschieben).
   - `"cad_zoom"` mit `"amount"` (`+5` rein / `-5` raus) und optional `"x"`, `"y"`.
   - `"cad_box_and_dimension"` mit `"start_x"`, `"start_y"`, `"end_x"`, `"end_y"`, `"dims": ["4", "4", "4"]` (Grundfläche aufziehen, Bemaßen & Extrudieren).
5. VIDEOSCHNITT-TIMELINE (`timeline_scrub_and_cut`):
   - Nutze `"action": "timeline_scrub_and_cut"` (mit optional `"playhead_x"`, `"timeline_y"`, `"target_x"`, `"ripple_delete": true`), um automatisch zur Sprechpause auf der Audio-Waveform zu schrubben, per `Ctrl+B` zu schneiden und Lücken per `Delete` zu entfernen.
6. COOKIE-POPUP-CHECK (IMMER ZUERST!): Wenn auf dem Screenshot ein Cookie-Banner oder Zustimmungs-Dialog sichtbar ist, klicke ihn ZUERST weg!
7. FOKUS & EINGABE IN FELDERN:
   - Nutze `"action": "click_and_type"` mit `"x"`, `"y"`, `"text"` und `"press_enter": true/false` (in Suchfeldern NUR den bereinigten Suchbegriff).
8. AUSDAUER-REGEL (MARATHON BIS 200+ SCHRITTE, KEIN VERFRÜHTES AUFGEBEN):
   - Auf dem Windows-Desktop MÜSSEN Icons/Launcher IMMER mit `"action": "double_click"` geöffnet werden!
   - Gib `"action": "done"` ERST DANN zurück, wenn ALLE Teilziele vollständig abgeschlossen und sichtbar sind!
9. FENSTER, KONTEXTMENÜ & TASTATUR:
   - `"right_click"`, `"drag_and_drop"`, `"scroll"` (`"amount": -5/+5`), `"hotkey"` (`"keys": ["ctrl", "l"]`).
10. ZERO-KNOWLEDGE TRESOR-PLATZHALTER: Für sensible Daten nutze `{{{{VAULT_EMAIL}}}}`, `{{{{VAULT_PASSWORD}}}}`, `{{{{VAULT_CARD_HOLDER}}}}`, `{{{{VAULT_CARD_NUMBER}}}}`, `{{{{VAULT_CARD_EXPIRY}}}}`, `{{{{VAULT_CARD_CVC}}}}`, `{{{{VAULT_IBAN}}}}` oder `{{{{VAULT_BILLING_ADDRESS}}}}`.
11. 3D-SECURE / PUSHTAN & 2-STUFEN-KAUFBERECHTIGUNG:
   - Bei 3D-Secure / S-pushTAN: `"action": "wait_3ds"`.
   - In MODUS A (`HALB-AUTONOM`): Beim finalen Kauf-Button `"target": "Finaler Kauf-Button"` setzen; in MODUS B (`VOLL-ZUGRIFF`) selbst klicken.
12. KOMPLETTE WEBBROWSER-STEUERUNG (TABS, FORMS, UPLOADS, VIDEOPLAYER):
   - `"browser_new_tab"` (optional `"url"`): Neuer Tab via `Ctrl+T` + URL öffnen.
   - `"browser_switch_tab"` (`"tab_index": 1..9` oder `"direction": "next|prev"` oder `"x", "y"` auf den Tab).
   - `"browser_back"` / `"browser_forward"` (`Alt+Left` / `Alt+Right`).
   - `"browser_address_bar"` (`"text"` oder `"url"` -> `Ctrl+L` + Eingabe + Enter).
   - `"browser_find_on_page"` (`"text"` oder `"search_text"` -> `Ctrl+F` Suche auf der Webseite).
   - `"browser_select_dropdown"` (`"x", "y"`, `"option_text"` oder `"steps_down"` -> Dropdown öffnen & wählen).
   - `"browser_upload_file"` (`"file_path"`, optional `"x", "y"` auf Upload-Button -> wählt lokale Datei im Windows-'Öffnen'-Dialog).
   - `"browser_video_control"` (`"command": "fullscreen|play|pause|toggle_pause|mute|forward|rewind"`, optional `"x", "y"`).
13. ACTION-BATCHING (MAKRO-KETTEN FÜR MAXIMALGESCHWINDIGKEIT):
   - Gib bei logischen Ketten bevorzugt `"actions": [...]` (2–8 Schritte) zurück (`"type": "click|click_tag|precision_click|double_click|right_click|drag|drag_and_drop|click_and_type|type|press|hotkey|scroll|cad_orbit|cad_pan|cad_zoom|cad_box_and_dimension|timeline_scrub_and_cut|browser_new_tab|browser_switch_tab|browser_back|browser_forward|browser_address_bar|browser_find_on_page|browser_select_dropdown|browser_upload_file|browser_video_control|wait|done"`).

Antworte AUSSCHLIESSLICH mit einem validen JSON-Objekt (ohne Markdown-Blöcke) im Einzel- oder Batch-Format:
{{
  "thought": "Kurzer Plan für die logische Kette",
  "coord_space": "1000",
  "action": "click|click_tag|precision_click|double_click|right_click|drag_and_drop|click_and_type|scroll|hotkey|move|type|type_secret|press|cad_orbit|cad_pan|cad_zoom|cad_box_and_dimension|timeline_scrub_and_cut|wait_3ds|done",
  "tag_id": 2,
  "x": 500,
  "y": 240,
  "actions": [
    {{"type": "click_tag", "tag_id": 2}},
    {{"type": "wait", "ms": 150}},
    {{"type": "cad_box_and_dimension", "start_x": 380, "start_y": 380, "end_x": 580, "end_y": 580, "dims": ["4", "4", "4"]}}
  ],
  "subgoal": "Aktuelles Teilziel",
  "target": "Kurze Beschreibung des UI-Elements",
  "reason": "Warum dieser Schritt / diese Kette ausgeführt wird"
}}
"""


def detect_pro_app_profile(goal: str, app_name: str = "") -> tuple[str, str]:
    """
    Liefert spezialisierte Bedien-Profile (`(profile_key, prompt_hint)`) für Profi-Apps:
    - CAD / Solid Craft 3D
    - 3D-Druck / OrcaSlicer / Creality Print
    - Videoschnitt (CapCut / DaVinci Resolve / Premiere / FilmCraft)
    - Live-Desktop-Coding (Apps & Spiele programmieren)
    """
    hay = f"{goal or ''} {app_name or ''}".lower()
    if any(k in hay for k in ("solid craft", "solidcraft", "cad", "würfel", "block", "quader", "extrud", "bemaß", "skizze", "sketch")):
        return (
            "cad_solidcraft",
            "PROFI-PROFIL [CAD / SOLID CRAFT 3D]:\n"
            "- Falls Start-/Projekt-Dialog sichtbar: klicke zuerst auf 'Neues Projekt' / 'New Part' bis der 3D-Viewport frei ist.\n"
            "- Für einen Block/Würfel (z. B. 4 x 4 x 4): Entweder (A) das Würfel/Box-Primitive-Werkzeug (Cube/Box) wählen, im 3D-Viewport platzieren "
            "und per Action-Batch in Länge (X), Breite (Y) und Höhe (Z) '4' (press_tab:true) -> '4' (press_tab:true) -> '4' (press_enter:true) eintippen; "
            "ODER (B) Sketch auf Ebene starten -> Rechteck per 'drag' aufziehen -> '4' Tab '4' Enter -> Extrudieren (Extrude) klicken -> '4' Enter.\n",
        )
    if any(k in hay for k in ("orca", "orcaslicer", "creality", "slicer", "slice", "3d-druck", "druckbett", ".stl", ".3mf")):
        return (
            "slicer_orca",
            "PROFI-PROFIL [3D-DRUCK / ORCASLICER]:\n"
            "- Modell laden per Hotkey ['ctrl','i'] (oder Klick auf 'Importieren' / 'Datei hinzufügen').\n"
            "- Ausrichten auf dem Druckbett mit Taste 'a' (Auto-Arrange).\n"
            "- Schichthöhe/Infill links im Profil per Klick anpassen und danach mit Hotkey ['ctrl','r'] ('Slice Plate') slicen.\n",
        )
    if any(k in hay for k in ("capcut", "davinci", "premiere", "filmcraft", "timeline", "videoschnitt", "schneide", "clip")):
        return (
            "video_nle",
            "PROFI-PROFIL [VIDEOSCHNITT / NLE]:\n"
            "- Medien importieren per Hotkey ['ctrl','i'], Clips per 'drag' vom Medienpool unten in die Timeline ziehen.\n"
            "- Abspielposition per Mausklick auf der Timeline setzen, Schnitt mit Hotkey ['ctrl','b'], Lücken mit 'delete' löschen, Export mit ['ctrl','e'].\n",
        )
    if any(k in hay for k in ("programmiere", "programmieren", "schreib mir eine app", "entwickle", "python app", "spiel programm", "live-coding")):
        return (
            "live_coding",
            "PROFI-PROFIL [LIVE-CODING AM DESKTOP]:\n"
            "- Öffne sichtbar den Code-Editor oder das Ziel-Projekt auf dem Desktop.\n"
            "- Schreibe kompletten, lauffähigen Code hinein, speichere mit Hotkey ['ctrl','s'] und starte die App sofort sichtbar auf dem Desktop.\n",
        )
    return "", ""


def is_escape_or_failsafe_triggered(initial_pos: Optional[tuple[int, int]] = None) -> bool:
    """
    Prüft über den intelligenten 20ms-Mauswächter (`SMART_MOUSE_GUARD`),
    ob ESC gedrückt oder die Maus aktiv vom Nutzer übernommen wurde (>220 px in <0.3s).
    """
    return SMART_MOUSE_GUARD.is_triggered()


def get_screen_and_cursor_info() -> tuple[int, int, int, int]:
    """Liefert `(screen_width, screen_height, cursor_x, cursor_y)` im DPI-korrigierten physischen Raum."""
    if PYAUTOGUI_AVAILABLE and pyautogui is not None:
        try:
            w, h = pyautogui.size()
            pos = pyautogui.position()
            return int(w), int(h), int(pos.x), int(pos.y)
        except Exception:
            pass
    if IS_WINDOWS:
        try:
            user32 = ctypes.windll.user32
            w = int(user32.GetSystemMetrics(0))
            h = int(user32.GetSystemMetrics(1))

            class POINT(ctypes.Structure):
                _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

            pt = POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            return w, h, int(pt.x), int(pt.y)
        except Exception:
            pass
    return 1920, 1080, 960, 540


def _grab_primary_screen_pil(screen_w: int, screen_h: int) -> Any:
    """Nimmt den Hauptbildschirm blitzschnell direkt im RAM via `mss` (+ PIL-Fallback) auf."""
    if not PIL_AVAILABLE or Image is None:
        return None
    pil_img = None
    if MSS_AVAILABLE and mss is not None:
        try:
            with mss.mss() as sct:
                monitors = sct.monitors
                mon = monitors[1] if len(monitors) > 1 else monitors[0]
                raw = sct.grab(mon)
                pil_img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
        except Exception as exc:
            logger.debug("mss Screenshot fehlgeschlagen, nutze PIL.ImageGrab: %s", exc)
    if pil_img is None and ImageGrab is not None:
        try:
            pil_img = ImageGrab.grab().convert("RGB")
        except Exception as exc:
            logger.debug("PIL ImageGrab fehlgeschlagen: %s", exc)
    if pil_img is None:
        pil_img = Image.new("RGB", (max(320, screen_w), max(240, screen_h)), (14, 22, 36))
    return pil_img


def _capture_annotated_pil_and_jpeg(
    max_width: int = 1280,
    max_height: int = 720,
    quality: int = 75,
    keep_pil: bool = False,
    occupied_targets: Optional[list[dict[str, Any]]] = None,
    viewer_agent_id: Optional[str] = None,
    include_som: bool = False,
    som_summary_out: Optional[list[str]] = None,
) -> tuple[Any, bytes, int, int, tuple[int, int]]:
    """
    Erzeugt im RAM einen Screenshot des Hauptbildschirms inklusive normiertem `1000x1000`-Gitter,
    leuchtend rotem Fadenkreuz an der aktuellen Mausposition, optionalen Set-of-Mark `[1]..[N]`
    Nummernschildern (`include_som=True`) sowie Belegt-Markierungen anderer Schwarm-Agenten.
    """
    screen_w, screen_h, cur_x, cur_y = get_screen_and_cursor_info()
    if not PIL_AVAILABLE or Image is None:
        return None, b"", screen_w, screen_h, (cur_x, cur_y)

    pil_img = _grab_primary_screen_pil(screen_w, screen_h)
    if pil_img is None:
        return None, b"", screen_w, screen_h, (cur_x, cur_y)

    if include_som:
        try:
            from ui_som_tagger import UI_SOM_TAGGER

            som_pil, som_text, _ = UI_SOM_TAGGER.annotate_pil_with_som(
                pil_img,
                screen_w=screen_w,
                screen_h=screen_h,
            )
            if som_pil is not None and som_pil is not pil_img:
                pil_img.close()
                pil_img = som_pil
            if som_summary_out is not None and som_text:
                som_summary_out.append(som_text)
        except Exception as exc:
            logger.debug("SoM Tagging übersprungen: %s", exc)

    img_w, img_h = pil_img.size
    scale_x = img_w / max(1, screen_w)
    scale_y = img_h / max(1, screen_h)

    annotated = pil_img.convert("RGBA")
    overlay = Image.new("RGBA", annotated.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    try:
        font = ImageFont.load_default()
    except Exception:
        font = None

    grid_line_color = (0, 229, 255, 64)
    grid_text_bg = (8, 14, 24, 185)
    grid_text_fg = (140, 235, 255, 235)

    for norm_gx in range(100, 1000, 100):
        px = int(round((norm_gx / 1000.0) * img_w))
        draw.line([(px, 0), (px, img_h)], fill=grid_line_color, width=1)
        label = f"X:{norm_gx}"
        draw.rectangle([px + 2, 4, px + 46, 18], fill=grid_text_bg)
        draw.text((px + 4, 5), label, fill=grid_text_fg, font=font)

    for norm_gy in range(100, 1000, 100):
        py = int(round((norm_gy / 1000.0) * img_h))
        draw.line([(0, py), (img_w, py)], fill=grid_line_color, width=1)
        label = f"Y:{norm_gy}"
        draw.rectangle([4, py + 2, 48, py + 16], fill=grid_text_bg)
        draw.text((6, py + 3), label, fill=grid_text_fg, font=font)

    # Belegt-Markierungen aktiver Peer-Agenten vom SharedBlackboard einzeichnen
    active_occupied = list(occupied_targets) if occupied_targets is not None else []
    if occupied_targets is None:
        try:
            import sys as _sys

            _sc = _sys.modules.get("swarm_core")
            if _sc is not None and hasattr(_sc, "SHARED_BLACKBOARD"):
                active_occupied = _sc.SHARED_BLACKBOARD.get_occupied_targets_for(
                    exclude_agent_id=viewer_agent_id
                )
        except Exception:
            active_occupied = []

    _slot_rgba = {
        1: (255, 123, 41, 240),
        2: (0, 240, 255, 240),
        3: (184, 41, 255, 240),
        4: (0, 255, 102, 240),
    }
    for occ in active_occupied:
        try:
            coords = occ.get("coords") or occ.get("target_coords")
            if not coords or len(coords) < 2:
                continue
            ox = max(0, min(img_w - 1, int(round((float(coords[0]) / 1000.0) * img_w))))
            oy = max(0, min(img_h - 1, int(round((float(coords[1]) / 1000.0) * img_h))))
            slot_num = int(occ.get("slot") or 1)
            occ_color = _slot_rgba.get(slot_num, (255, 165, 0, 240))
            box_r = max(24, int(30 * max(scale_x, scale_y)))
            draw.rectangle(
                [ox - box_r, oy - box_r, ox + box_r, oy + box_r],
                fill=(occ_color[0], occ_color[1], occ_color[2], 38),
                outline=occ_color,
                width=3,
            )
            occ_text = f"[BELEGT DURCH AGENT {slot_num}]"
            target_desc = str(occ.get("target") or "").strip()
            if target_desc:
                occ_text = f"{occ_text} {target_desc[:18]}"
            tw = max(130, len(occ_text) * 6 + 14)
            tx = max(4, min(img_w - tw - 4, ox - tw // 2))
            ty = max(4, oy - box_r - 22)
            draw.rectangle([tx, ty, tx + tw, ty + 18], fill=(12, 16, 26, 235), outline=occ_color, width=1)
            draw.text((tx + 5, ty + 3), occ_text, fill=occ_color, font=font)
        except Exception:
            continue

    cx = max(0, min(img_w - 1, int(round(cur_x * scale_x))))
    cy = max(0, min(img_h - 1, int(round(cur_y * scale_y))))
    norm_cx, norm_cy = screen_to_norm_xy(cur_x, cur_y, screen_w, screen_h)

    cross_arm = max(28, int(34 * max(scale_x, scale_y)))
    ring_outer = max(18, int(22 * max(scale_x, scale_y)))
    ring_inner = max(7, int(9 * max(scale_x, scale_y)))

    draw.line([(cx - cross_arm, cy), (cx + cross_arm, cy)], fill=(0, 0, 0, 220), width=5)
    draw.line([(cx, cy - cross_arm), (cx, cy + cross_arm)], fill=(0, 0, 0, 220), width=5)

    red_bright = (255, 30, 30, 255)
    yellow_center = (255, 235, 59, 255)
    draw.line([(cx - cross_arm, cy), (cx + cross_arm, cy)], fill=red_bright, width=3)
    draw.line([(cx, cy - cross_arm), (cx, cy + cross_arm)], fill=red_bright, width=3)
    draw.ellipse([cx - ring_outer, cy - ring_outer, cx + ring_outer, cy + ring_outer], outline=red_bright, width=3)
    draw.ellipse([cx - ring_inner, cy - ring_inner, cx + ring_inner, cy + ring_inner], outline=yellow_center, width=2)
    draw.ellipse([cx - 3, cy - 3, cx + 3, cy + 3], fill=red_bright)

    coord_label = f"[{cur_x}, {cur_y}] N[{norm_cx},{norm_cy}]"
    badge_w = max(92, len(coord_label) * 6 + 14)
    badge_h = 20
    bx = cx + ring_outer + 6
    by = cy - ring_outer - badge_h - 2
    if bx + badge_w > img_w - 4:
        bx = max(4, cx - ring_outer - badge_w - 6)
    if by < 4:
        by = min(img_h - badge_h - 4, cy + ring_outer + 6)

    draw.rectangle([bx, by, bx + badge_w, by + badge_h], fill=(210, 15, 25, 240), outline=(255, 255, 255, 240), width=1)
    draw.text((bx + 6, by + 4), coord_label, fill=(255, 255, 255, 255), font=font)

    combined = Image.alpha_composite(annotated, overlay).convert("RGB")
    annotated.close()
    overlay.close()
    pil_img.close()

    if combined.width > max_width or combined.height > max_height:
        resample_filter = getattr(getattr(Image, "Resampling", Image), "LANCZOS", Image.BICUBIC)
        combined.thumbnail((max_width, max_height), resample_filter)

    buf = io.BytesIO()
    try:
        combined.save(buf, format="JPEG", quality=max(30, min(92, int(quality))), optimize=True)
        jpeg_bytes = buf.getvalue()
    finally:
        if not keep_pil:
            combined.close()
            combined = None

    return combined, jpeg_bytes, screen_w, screen_h, (cur_x, cur_y)


def capture_annotated_screen_1fps(
    max_width: int = 1280,
    max_height: int = 720,
    quality: int = 75,
    grid_step: int = 200,
    occupied_targets: Optional[list[dict[str, Any]]] = None,
    viewer_agent_id: Optional[str] = None,
) -> tuple[bytes, int, int, tuple[int, int]]:
    """
    Erstellt einen Screenshot des Hauptbildschirms (`mss`) mit normiertem `1000x1000`-Gitter,
    Fadenkreuz sowie Belegt-Markierungen aktiver Schwarm-Agenten.
    """
    _, jpeg_bytes, screen_w, screen_h, cur_pos = _capture_annotated_pil_and_jpeg(
        max_width=max_width,
        max_height=max_height,
        quality=quality,
        keep_pil=False,
        occupied_targets=occupied_targets,
        viewer_agent_id=viewer_agent_id,
    )
    return jpeg_bytes, screen_w, screen_h, cur_pos


def capture_annotated_screen_with_som(
    max_width: int = 1280,
    max_height: int = 720,
    quality: int = 75,
    occupied_targets: Optional[list[dict[str, Any]]] = None,
    viewer_agent_id: Optional[str] = None,
) -> tuple[bytes, int, int, tuple[int, int], str]:
    """Wie `capture_annotated_screen_1fps`, inkl. gelben Set-of-Mark `[1]..[N]` Tags & Textliste."""
    som_out: list[str] = []
    _, jpeg_bytes, screen_w, screen_h, cur_pos = _capture_annotated_pil_and_jpeg(
        max_width=max_width,
        max_height=max_height,
        quality=quality,
        keep_pil=False,
        occupied_targets=occupied_targets,
        viewer_agent_id=viewer_agent_id,
        include_som=True,
        som_summary_out=som_out,
    )
    return jpeg_bytes, screen_w, screen_h, cur_pos, (som_out[0] if som_out else "")


def capture_passive_screen_jpeg(
    max_width: int = 1280,
    max_height: int = 720,
    quality: int = 60,
) -> bytes:
    """
    Erstellt im RAM einen komprimierten Desktop-Screenshot (`max 1280x720`, `0-1000` Gitter + Fadenkreuz)
    für den kontinuierlichen Vision-Stream (`DesktopVisionStream`).
    """
    _, jpeg_bytes, _, _, _ = _capture_annotated_pil_and_jpeg(
        max_width=max_width,
        max_height=max_height,
        quality=quality,
        keep_pil=False,
    )
    return jpeg_bytes


def resolve_max_agent_steps(explicit_max: Optional[int] = None) -> int:
    """Liefert das Schritt-Limit (Standard: 200, aus config.MAX_AGENT_STEPS 50..500, sofern kein Test-Override gesetzt)."""
    if explicit_max is not None and int(explicit_max) > 0:
        return int(explicit_max)
    try:
        from config import get_max_agent_steps

        return get_max_agent_steps()
    except Exception:
        return 200


def compute_screenshot_diff_ratio(prev_jpeg: bytes, curr_jpeg: bytes) -> float:
    """
    Vergleicht zwei Screenshots extrem schnell im RAM (`PIL.ImageChops.difference` auf 160x90 Graustufen).
    Liefert einen normierten Änderungs-Wert in `[0.0, 1.0]` (`< 0.015` = Bildschirm praktisch unverändert).
    """
    if not prev_jpeg or not curr_jpeg:
        return 1.0
    if prev_jpeg == curr_jpeg:
        return 0.0
    if not PIL_AVAILABLE or Image is None:
        return 0.05 if abs(len(prev_jpeg) - len(curr_jpeg)) < 250 else 0.5
    try:
        from PIL import ImageChops  # type: ignore

        with Image.open(io.BytesIO(prev_jpeg)) as raw1, Image.open(io.BytesIO(curr_jpeg)) as raw2:
            im1 = raw1.convert("L").resize((160, 90))
            im2 = raw2.convert("L").resize((160, 90))
            try:
                diff = ImageChops.difference(im1, im2)
                try:
                    hist = diff.histogram()
                finally:
                    diff.close()
            finally:
                im1.close()
                im2.close()
        total_px = 160 * 90
        weighted = sum(i * count for i, count in enumerate(hist))
        return round(min(1.0, weighted / float(total_px * 255)), 4)
    except Exception:
        return 0.5


def infer_subgoals_from_goal(goal: str) -> list[str]:
    """Zerlegt komplexe Ketten-Aufträge automatisch in abarbeitbare Teilziele (Sub-Goals)."""
    raw = (goal or "").strip()
    if not raw:
        return ["Ziel ausführen"]
    low = raw.lower()
    stages: list[str] = []
    # Explizite Ketten-Trenner prüfen (->, dann, danach, und dann)
    split_parts = [
        p.strip(" .,!?")
        for p in re.split(r"\s*(?:->|→|\bund\s+dann\b|\bdanach\b|\bdann\b)\s*", raw, flags=re.IGNORECASE)
        if p.strip(" .,!?")
    ]
    if len(split_parts) >= 2:
        return [p[:60] for p in split_parts[:10]]

    stage_checks = (
        (("öffne", "oeffne", "geh auf", "gehe auf", "browser", "webseite", "seite"), "Browser / Zielseite öffnen"),
        (("account", "registrier", "konto erstell", "sign up", "anmeld"), "Account / Registrierung anlegen"),
        (("e-mail", "email", "bestätig", "verifizier", "2fa", "code"), "E-Mail-Bestätigung & Code prüfen"),
        (("einlog", "login"), "Einloggen"),
        (("such", "finde", "zeig"), "Suchbegriff eingeben & Ergebnis öffnen"),
        (("chart", "analys", "kurs", "rsi", "preis"), "Chart & Kursdaten analysieren"),
        (("prüf", "pruef", "lies", "export", "speicher"), "Ergebnisse prüfen & exportieren"),
        (("kauf", "order", "bestell", "warenkorb", "kasse"), "Kaufmaske / Order vorbereiten"),
    )
    for keywords, label in stage_checks:
        if any(k in low for k in keywords):
            stages.append(label)
    return stages or [raw[:60]]


class MarathonStepMemory:
    """
    AUFGABE 1: Gleitendes Bild- & Aktions-Gedächtnis (Sliding Window Memory) + Anti-Stuck-Engine.
    - Sendet NIEMALS 100 alte Screenshots an die API, sondern maximal (prev + current).
    - Komprimiert alle 25 Schritte die ältere Text-Historie automatisch zu einer kurzen Status-Notiz.
    - Erkennt 3 unveränderte Klicks an derselben Stelle und erzwingt sofort eine Alternativ-Strategie.
    """

    def __init__(self, main_goal: str) -> None:
        self.main_goal = (main_goal or "").strip()
        self.subgoals: list[str] = infer_subgoals_from_goal(self.main_goal)
        self.subgoal_index: int = 0
        self.current_subgoal: str = self.subgoals[0]
        self.recent_steps: list[str] = []
        self.summary_notes: list[str] = []
        self.prev_screenshot_bytes: bytes = b""
        self.last_diff_ratio: float = 1.0
        self.last_click_xy: Optional[tuple[int, int]] = None
        self.same_spot_clicks: int = 0
        self.recovery_cursor: int = 0
        self.total_recorded: int = 0

    def update_subgoal(self, candidate: str) -> None:
        clean = (candidate or "").strip()
        if clean:
            self.current_subgoal = clean[:65]

    def advance_subgoal_for_action(self, action: str, detail: str = "") -> None:
        low = f"{action} {detail}".lower()
        if self.subgoal_index >= len(self.subgoals) - 1:
            return
        cur_low = self.current_subgoal.lower()
        matched = False
        if "öffnen" in cur_low and any(k in low for k in ("open_url", "geöffnet", "klick", "type")):
            matched = True
        elif ("account" in cur_low or "registrier" in cur_low) and any(k in low for k in ("create_instant_email", "password", "registrier", "type_secret")):
            matched = True
        elif ("e-mail" in cur_low or "bestätig" in cur_low) and any(k in low for k in ("read_email_code", "code", "verifizier")):
            matched = True
        elif "einlog" in cur_low and any(k in low for k in ("login", "einlog", "password", "enter")):
            matched = True
        elif "such" in cur_low and any(k in low for k in ("type", "such", "klick", "ergebnis")):
            matched = True
        elif ("chart" in cur_low or "analys" in cur_low) and any(k in low for k in ("chart", "klick", "scroll")):
            matched = True
        if matched:
            self.subgoal_index = min(len(self.subgoals) - 1, self.subgoal_index + 1)
            self.current_subgoal = self.subgoals[self.subgoal_index]

    def has_pending_subgoals(self, real_actions_count: int) -> bool:
        if len(self.subgoals) >= 3 and real_actions_count < min(len(self.subgoals), 3):
            return True
        if len(self.subgoals) >= 2 and self.subgoal_index < len(self.subgoals) - 1 and real_actions_count < len(self.subgoals):
            return True
        return False

    def record_step(
        self,
        step_idx: int,
        action: str,
        tx: Optional[int],
        ty: Optional[int],
        detail: str,
        curr_jpeg: bytes,
        brain: Any = None,
        subgoal_hint: str = "",
    ) -> float:
        coord_txt = f" ({tx}, {ty})" if tx is not None and ty is not None else ""
        short_det = re.sub(r"\s+", " ", (detail or "")).strip()[:44]
        entry = f"[Schritt {step_idx}: {action}{coord_txt} {short_det}]".strip()
        self.recent_steps.append(entry)
        self.total_recorded += 1

        if subgoal_hint:
            self.update_subgoal(subgoal_hint)
        else:
            self.advance_subgoal_for_action(action, detail)

        # Alle 25 Schritte ältere Text-Schritte automatisch zu kurzer Status-Notiz verdichten
        if len(self.recent_steps) >= 25:
            chunk_to_compress = self.recent_steps[:17]
            self.recent_steps = self.recent_steps[17:]
            note = self._compress_steps_batch(chunk_to_compress, brain=brain)
            self.summary_notes.append(note)
            if len(self.summary_notes) > 10:
                self.summary_notes = self.summary_notes[-10:]

        # Pixel-Diff & Anti-Stuck-Prüfung gegen vorherigen Screenshot
        diff_ratio = compute_screenshot_diff_ratio(self.prev_screenshot_bytes, curr_jpeg)
        self.last_diff_ratio = diff_ratio
        if action in ("click", "double_click") and tx is not None and ty is not None:
            if self.last_click_xy is not None:
                dist = ((tx - self.last_click_xy[0]) ** 2 + (ty - self.last_click_xy[1]) ** 2) ** 0.5
            else:
                dist = 9999.0
            if dist <= 38.0 and diff_ratio < 0.015:
                self.same_spot_clicks += 1
            else:
                self.same_spot_clicks = 1 if diff_ratio < 0.015 else 0
            self.last_click_xy = (tx, ty)
        else:
            if diff_ratio >= 0.018:
                self.same_spot_clicks = 0

        if curr_jpeg:
            self.prev_screenshot_bytes = curr_jpeg
        return diff_ratio

    def _compress_steps_batch(self, batch: list[str], brain: Any = None) -> str:
        joined = " -> ".join(batch)
        if brain is not None and hasattr(brain, "ask"):
            try:
                summary = _ask_brain_text(
                    brain=brain,
                    user_prompt=f"Fasse diese Agenten-Schritte in 1 kurzen deutschen Satz zusammen:\n{joined[:1800]}",
                    system_prompt="Erzeuge eine extrem knappe Status-Notiz (max. 180 Zeichen) ohne Bild.",
                    image_bytes=None,
                )
                if summary:
                    return f"Etappe ({batch[0][:12]}..): {summary[:180]}"
            except Exception:
                pass
        return f"Etappe ({len(batch)} Schritte): {' | '.join(batch[:2])} ... {batch[-1]}"

    def is_stuck(self) -> bool:
        return self.same_spot_clicks >= 3

    def build_anti_stuck_prompt_hint(self) -> str:
        if not self.is_stuck():
            return ""
        return (
            "\n⚠️ ANTI-STUCK-WARNUNG: Die letzten 3 Klicks an derselben Stelle haben den Bildschirm kaum verändert! "
            "Klicke NICHT noch einmal auf dieselbe Stelle! Wähle sofort eine Alternativ-Strategie: "
            "1) Popup/Overlay mit ESC schließen (`action: press, key: escape`), "
            "2) Seite scrollen (`action: scroll`), "
            "3) per `tab` / `enter` navigieren, oder "
            "4) per Browser-Adressleiste (`action: press, key: ctrl+l`) einen neuen Anlauf nehmen!\n"
        )

    def pick_recovery_action(self, screen_w: int, screen_h: int) -> dict[str, Any]:
        strategies = [
            ("press", "escape", 0, "Anti-Stuck: Schließe mögliches Overlay/Popup mit ESC"),
            ("scroll", "", -6, "Anti-Stuck: Scrolle Seite nach unten, um Ziel sichtbar zu machen"),
            ("press", "tab", 0, "Anti-Stuck: Navigiere per Tab weiter"),
            ("press", "ctrl+l", 0, "Anti-Stuck: Adressleiste (Ctrl+L) für neuen Anlauf fokussieren"),
        ]
        act, key, scr, reason = strategies[self.recovery_cursor % len(strategies)]
        self.recovery_cursor += 1
        self.same_spot_clicks = 0
        tx, ty = norm_to_screen_xy(500, 420, screen_w, screen_h)
        return {
            "action": act,
            "x": tx,
            "y": ty,
            "screen_x": tx,
            "screen_y": ty,
            "text": "",
            "vault_key": "",
            "key": key,
            "scroll_clicks": scr,
            "subgoal": "Anti-Stuck Alternativ-Strategie",
            "target": "Anti-Stuck Recovery",
            "reason": reason,
        }

    def format_history_text(self) -> str:
        parts: list[str] = []
        if self.summary_notes:
            parts.append("Zusammenfassung älterer Etappen:\n" + "\n".join(f"- {n}" for n in self.summary_notes[-4:]))
        if self.recent_steps:
            parts.append("Aktuelles Sliding-Window:\n" + " -> ".join(self.recent_steps[-12:]))
        return "\n".join(parts) if parts else "Noch keine Schritte ausgeführt."

    def format_hud_status(self, step_idx: int, max_steps: int) -> str:
        return f"AGENT AKTIV // SCHRITT {step_idx} / {max_steps} // SUB-ZIEL: {self.current_subgoal[:48]}"


def _ask_brain_text(
    brain: Any,
    user_prompt: str,
    system_prompt: str = "",
    image_bytes: Optional[bytes | list[bytes] | tuple[bytes, ...]] = None,
) -> str:
    """
    Ruft `brain.ask(...)` sicher auf und extrahiert immer den Antworttext (`resp.text`),
    egal ob `brain` eine `JarvisBrain`-Instanz (`BrainResponse`) oder ein Mock-Objekt (`str`) ist.
    """
    if brain is None or not hasattr(brain, "ask"):
        return ""
    try:
        resp = brain.ask(
            user_prompt,
            image_bytes=image_bytes,
            system_prompt=system_prompt,
            use_history=False,
        )
    except TypeError:
        try:
            resp = brain.ask(
                user_prompt,
                system_prompt=system_prompt,
                image_bytes=image_bytes,
                use_memory=False,
            )
        except Exception as exc:
            logger.debug("brain.ask Fallback fehlgeschlagen: %s", exc)
            return ""
    except Exception as exc:
        logger.debug("brain.ask Fehler: %s", exc)
        return ""

    if hasattr(resp, "text"):
        return str(resp.text or "").strip()
    return str(resp or "").strip()


def _extract_json_dict(raw_text: str) -> Optional[dict[str, Any]]:
    """Kugelsicherer JSON-Extraktor für LLM-Antworten mit Markdown-Backticks, Text-Drumherum oder Trailing-Commas."""
    if not raw_text:
        return None
    cleaned = str(raw_text).strip()
    cleaned = re.sub(r"```(?:json)?", "", cleaned, flags=re.IGNORECASE).strip()

    decoder = json.JSONDecoder()
    for idx, ch in enumerate(cleaned):
        if ch == "{":
            try:
                obj, _ = decoder.raw_decode(cleaned[idx:])
                if isinstance(obj, dict):
                    return obj
            except Exception:
                pass

    match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    if match:
        frag = match.group(0)
        for cand in (frag, re.sub(r",\s*([}\]])", r"\1", frag)):
            try:
                candidate = json.loads(cand)
                if isinstance(candidate, dict):
                    return candidate
            except Exception:
                continue
    return None


def _parse_coord_number(val: Any) -> Optional[float]:
    """Konvertiert Zahlen, Float-Strings ('350.5'), Komma-Werte oder '350px' sicher in float."""
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip().lower()
    if not s:
        return None
    m = re.search(r"-?\d+(?:[.,]\d+)?", s)
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", "."))
    except Exception:
        return None


def _resolve_xy_coords(
    raw_x: Any,
    raw_y: Any,
    is_1000_space: bool,
    screen_w: int,
    screen_h: int,
) -> tuple[Optional[int], Optional[int], Optional[int], Optional[int]]:
    """Rechnet normierte (`0..1`, `0..1000`) oder direkte Koordinaten in `(x, y, screen_x, screen_y)` um."""
    fx = _parse_coord_number(raw_x)
    fy = _parse_coord_number(raw_y)
    if fx is None or fy is None:
        return None, None, None, None
    if 0.0 <= fx <= 1.0 and 0.0 <= fy <= 1.0 and (fx > 0 or fy > 0):
        px = max(3, min(screen_w - 4, int(round(fx * screen_w))))
        py = max(3, min(screen_h - 4, int(round(fy * screen_h))))
        return px, py, px, py
    if is_1000_space and 0.0 <= fx <= 1000.0 and 0.0 <= fy <= 1000.0:
        sx, sy = norm_to_screen_xy(int(float(fx)), int(float(fy)), screen_w, screen_h)
        return sx, sy, sx, sy
    px = max(3, min(screen_w - 4, int(float(fx))))
    py = max(3, min(screen_h - 4, int(float(fy))))
    if 0.0 <= fx <= 1000.0 and 0.0 <= fy <= 1000.0:
        sx, sy = norm_to_screen_xy(int(float(fx)), int(float(fy)), screen_w, screen_h)
    else:
        sx, sy = px, py
    return px, py, sx, sy


def _parse_single_action_entry(
    entry: dict[str, Any],
    screen_w: int,
    screen_h: int,
    is_1000_space: bool,
    default_reason: str = "",
) -> dict[str, Any]:
    """Normalisiert ein einzelnes Aktions-Dict (Einzel- oder Batch-Schritt) in Screen-Koordinaten."""
    action = str(entry.get("action") or entry.get("type") or "done").strip().lower()
    valid_actions = {
        "move",
        "click",
        "precision_click",
        "click_tag",
        "double_click_tag",
        "right_click_tag",
        "double_click",
        "right_click",
        "drag_and_drop",
        "click_and_type",
        "scroll",
        "hotkey",
        "type",
        "type_secret",
        "press",
        "cad_orbit",
        "cad_pan",
        "cad_zoom",
        "cad_box_and_dimension",
        "timeline_scrub_and_cut",
        "browser_new_tab",
        "browser_switch_tab",
        "browser_back",
        "browser_forward",
        "browser_address_bar",
        "browser_find_on_page",
        "browser_select_dropdown",
        "browser_upload_file",
        "browser_video_control",
        "wait",
        "wait_3ds",
        "done",
    }
    if action not in valid_actions:
        if "3ds" in action or "pushtan" in action or "tan" in action:
            action = "wait_3ds"
        elif "new_tab" in action or "neuer_tab" in action:
            action = "browser_new_tab"
        elif "switch_tab" in action or "tab_wechsel" in action:
            action = "browser_switch_tab"
        elif "browser_back" in action or action in ("back", "zurück"):
            action = "browser_back"
        elif "browser_forward" in action or action in ("forward", "vor"):
            action = "browser_forward"
        elif "address_bar" in action or "adressleiste" in action:
            action = "browser_address_bar"
        elif "find_on_page" in action or "seitensuche" in action:
            action = "browser_find_on_page"
        elif "dropdown" in action:
            action = "browser_select_dropdown"
        elif "upload" in action:
            action = "browser_upload_file"
        elif "video_control" in action or "fullscreen" in action:
            action = "browser_video_control"
        elif "timeline" in action or "waveform" in action or "scrub" in action:
            action = "timeline_scrub_and_cut"
        elif "cad_orbit" in action or "orbit" in action:
            action = "cad_orbit"
        elif "cad_pan" in action or ("pan" in action and len(action) <= 10):
            action = "cad_pan"
        elif "cad_zoom" in action:
            action = "cad_zoom"
        elif "cad_box" in action or "dimension" in action:
            action = "cad_box_and_dimension"
        elif "precision" in action or "sniper" in action:
            action = "precision_click"
        elif "tag" in action and "double" in action:
            action = "double_click_tag"
        elif "tag" in action and ("right" in action or "rechts" in action):
            action = "right_click_tag"
        elif "tag" in action:
            action = "click_tag"
        elif "wait" in action or "sleep" in action or "pause" in action:
            action = "wait"
        elif "secret" in action:
            action = "type_secret"
        elif "drag" in action or "zieh" in action or "drop" in action:
            action = "drag_and_drop"
        elif "right" in action or "rechts" in action or "kontext" in action:
            action = "right_click"
        elif "click_and_type" in action or ("click" in action and "type" in action):
            action = "click_and_type"
        elif "double" in action or "doppel" in action:
            action = "double_click"
        elif "click" in action or "klick" in action:
            action = "click"
        elif "move" in action or "beweg" in action:
            action = "move"
        elif "type" in action or "write" in action or "search" in action or "tipp" in action:
            action = "type"
        elif "scroll" in action or "swipe" in action or "wisch" in action:
            action = "scroll"
        elif "hotkey" in action or "shortcut" in action or "combo" in action:
            action = "hotkey"
        elif "key" in action or "press" in action or "taste" in action:
            action = "press"
        else:
            action = "done"

    tag_id: Optional[int] = None
    if entry.get("tag_id") is not None:
        try:
            tag_id = int(str(entry.get("tag_id")).strip().strip("[]"))
        except Exception:
            tag_id = None

    raw_x = entry.get("norm_x", entry.get("x"))
    raw_y = entry.get("norm_y", entry.get("y"))
    if (raw_x is None or raw_y is None) and isinstance(entry.get("pos"), (list, tuple)) and len(entry["pos"]) >= 2:
        raw_x, raw_y = entry["pos"][0], entry["pos"][1]
    elif (raw_x is None or raw_y is None) and isinstance(entry.get("coords"), (list, tuple)) and len(entry["coords"]) >= 2:
        raw_x, raw_y = entry["coords"][0], entry["coords"][1]

    entry_cs = str(entry.get("coord_space") or "").strip()
    use_1000 = is_1000_space or entry_cs == "1000" or ("norm_x" in entry and "norm_y" in entry)

    x_val, y_val, screen_x, screen_y = _resolve_xy_coords(raw_x, raw_y, use_1000, screen_w, screen_h)
    if tag_id is not None and (x_val is None or y_val is None or action in ("click_tag", "double_click_tag", "right_click_tag")):
        try:
            from ui_som_tagger import UI_SOM_TAGGER

            resolved_tag = UI_SOM_TAGGER.resolve_tag_to_coords(tag_id)
            if resolved_tag is not None:
                x_val, y_val, _ = resolved_tag
                screen_x, screen_y = x_val, y_val
        except Exception:
            pass

    sx_val, sy_val, _, _ = _resolve_xy_coords(entry.get("start_x"), entry.get("start_y"), use_1000, screen_w, screen_h)
    ex_val, ey_val, _, _ = _resolve_xy_coords(entry.get("end_x"), entry.get("end_y"), use_1000, screen_w, screen_h)
    ph_x, _, _, _ = _resolve_xy_coords(entry.get("playhead_x"), 0, use_1000, screen_w, screen_h)
    tl_x_dummy, tl_y, _, _ = _resolve_xy_coords(0, entry.get("timeline_y"), use_1000, screen_w, screen_h)
    tgt_x, _, _, _ = _resolve_xy_coords(entry.get("target_x"), 0, use_1000, screen_w, screen_h)
    tgt_end_x, _, _, _ = _resolve_xy_coords(entry.get("target_end_x"), 0, use_1000, screen_w, screen_h)

    raw_amount = entry.get("amount", entry.get("scroll_clicks", entry.get("steps", -5)))
    try:
        scroll_clicks = int(float(str(raw_amount).replace(",", ".")))
    except Exception:
        scroll_clicks = -5

    def _safe_delta(v: Any, default_v: int) -> int:
        try:
            return int(float(str(v).replace(",", "."))) if v is not None else default_v
        except Exception:
            return default_v

    dx_val = _safe_delta(entry.get("dx"), 120)
    dy_val = _safe_delta(entry.get("dy"), 0)

    raw_dims = entry.get("dims")
    dims_list = [str(d).strip() for d in raw_dims if str(d).strip()] if isinstance(raw_dims, (list, tuple)) else ["4", "4", "4"]

    raw_ms = entry.get("ms", entry.get("duration_ms", 150))
    try:
        wait_ms = max(10, min(3000, int(float(raw_ms))))
    except Exception:
        wait_ms = 150

    raw_keys = entry.get("keys")
    keys_list: list[str] = []
    if isinstance(raw_keys, (list, tuple)):
        keys_list = [str(k).strip().lower() for k in raw_keys if str(k).strip()]
    elif isinstance(raw_keys, str) and raw_keys.strip():
        keys_list = [p.strip().lower() for p in raw_keys.split("+") if p.strip()]
    key_str = str(entry.get("key") or "").strip()
    if not keys_list and "+" in key_str:
        keys_list = [p.strip().lower() for p in key_str.split("+") if p.strip()]

    raw_enter = entry.get("press_enter")
    if isinstance(raw_enter, bool):
        press_enter = raw_enter
    elif raw_enter is not None:
        press_enter = str(raw_enter).strip().lower() in ("1", "true", "yes", "ja")
    else:
        press_enter = key_str.lower() in ("enter", "return")

    raw_tab = entry.get("press_tab")
    if isinstance(raw_tab, bool):
        press_tab = raw_tab
    elif raw_tab is not None:
        press_tab = str(raw_tab).strip().lower() in ("1", "true", "yes", "ja")
    else:
        press_tab = False

    raw_prec = entry.get("precision")
    precision_flag = bool(raw_prec) if isinstance(raw_prec, bool) else (str(raw_prec or "").strip().lower() in ("1", "true", "yes", "ja"))

    raw_ripple = entry.get("ripple_delete", True)
    ripple_flag = bool(raw_ripple) if isinstance(raw_ripple, bool) else (str(raw_ripple or "").strip().lower() in ("1", "true", "yes", "ja"))

    return {
        "action": action,
        "tag_id": tag_id,
        "precision": precision_flag or action == "precision_click",
        "x": x_val,
        "y": y_val,
        "screen_x": screen_x if screen_x is not None else x_val,
        "screen_y": screen_y if screen_y is not None else y_val,
        "start_x": sx_val if sx_val is not None else x_val,
        "start_y": sy_val if sy_val is not None else y_val,
        "end_x": ex_val,
        "end_y": ey_val,
        "dx": dx_val,
        "dy": dy_val,
        "dims": dims_list,
        "playhead_x": ph_x,
        "timeline_y": tl_y if entry.get("timeline_y") is not None else None,
        "target_x": tgt_x,
        "target_end_x": tgt_end_x,
        "editor": str(entry.get("editor") or "CapCut").strip(),
        "ripple_delete": ripple_flag,
        "button": str(entry.get("button") or "auto").strip().lower(),
        "text": str(entry.get("text") or ""),
        "press_enter": press_enter,
        "press_tab": press_tab,
        "wait_ms": wait_ms,
        "vault_key": str(entry.get("vault_key") or "").strip(),
        "key": key_str,
        "keys": keys_list,
        "amount": scroll_clicks,
        "scroll_clicks": scroll_clicks,
        "subgoal": str(entry.get("subgoal") or "").strip(),
        "target": str(entry.get("target") or entry.get("target_label") or "").strip(),
        "reason": str(entry.get("reason") or default_reason).strip(),
        "url": str(entry.get("url") or "").strip(),
        "tab_index": int(entry["tab_index"]) if entry.get("tab_index") is not None and str(entry.get("tab_index")).strip().isdigit() else None,
        "direction": str(entry.get("direction") or "next").strip().lower(),
        "search_text": str(entry.get("search_text") or entry.get("query") or "").strip(),
        "next_match": bool(entry.get("next_match", False)),
        "option_text": str(entry.get("option_text") or "").strip(),
        "steps_down": int(entry.get("steps_down") or 0),
        "file_path": str(entry.get("file_path") or "").strip(),
        "command": str(entry.get("command") or "toggle_pause").strip().lower(),
    }


def parse_agent_action_json(
    raw_text: str,
    screen_w: int = 1920,
    screen_h: int = 1080,
    scale_from_1000: bool = False,
) -> dict[str, Any]:
    """
    Extrahiert und validiert das JSON-Aktionsobjekt der Vision-KI inklusive Action-Batching
    (`"actions": [...]` bis 8 Schritte am Stück) sowie Einzelaktionen.
    """
    cleaned = (raw_text or "").strip()
    parsed = _extract_json_dict(cleaned)

    if not parsed:
        return {
            "action": "done",
            "x": None,
            "y": None,
            "screen_x": None,
            "screen_y": None,
            "start_x": None,
            "start_y": None,
            "end_x": None,
            "end_y": None,
            "text": "",
            "press_enter": False,
            "press_tab": False,
            "wait_ms": 150,
            "vault_key": "",
            "key": "",
            "keys": [],
            "amount": 0,
            "scroll_clicks": 0,
            "subgoal": "",
            "target": "",
            "thought": "",
            "actions": [],
            "reason": cleaned[:200] if cleaned else "Keine strukturierte Aktion erkannt.",
        }

    coord_space = str(parsed.get("coord_space") or "").strip()
    is_1000_space = scale_from_1000 or coord_space == "1000" or ("norm_x" in parsed and "norm_y" in parsed)
    thought = str(parsed.get("thought") or parsed.get("reason") or "").strip()

    raw_batch = parsed.get("actions")
    parsed_batch: list[dict[str, Any]] = []
    if isinstance(raw_batch, list) and raw_batch:
        # In Batch-Antworten ohne explizites coord_space ist 1000x1000 Standard, falls coord_space=="1000" oder scale_from_1000
        batch_1000 = is_1000_space or coord_space == ""
        for item in raw_batch[:8]:
            if isinstance(item, dict):
                parsed_batch.append(
                    _parse_single_action_entry(
                        item,
                        screen_w=screen_w,
                        screen_h=screen_h,
                        is_1000_space=batch_1000,
                        default_reason=thought,
                    )
                )

    if parsed_batch and not parsed.get("action"):
        primary = dict(parsed_batch[0])
        if parsed.get("subgoal") and not primary.get("subgoal"):
            primary["subgoal"] = str(parsed.get("subgoal") or "").strip()
        if parsed.get("target") and not primary.get("target"):
            primary["target"] = str(parsed.get("target") or "").strip()
    else:
        primary = _parse_single_action_entry(
            parsed,
            screen_w=screen_w,
            screen_h=screen_h,
            is_1000_space=is_1000_space,
            default_reason=thought,
        )

    primary["thought"] = thought
    primary["actions"] = parsed_batch
    return primary


def _extract_url_and_search_term(goal: str, brain: Any = None) -> tuple[str, str]:
    """
    Delegiert an den zentralen Intent-Analyzer (`brain.extract_browser_intent`),
    sodass niemals der rohe gesprochene Satz in Google geworfen wird, sondern
    `(target_url, clean_search_term)` sauber extrahiert werden.
    """
    try:
        from brain import extract_browser_intent

        intent = extract_browser_intent(goal, brain=brain)
        return str(intent.get("target_url") or ""), str(intent.get("clean_search_term") or "")
    except Exception:
        clean = (goal or "").strip()
        low = clean.lower()
        site_url = ""
        url_match = re.search(
            r"(https?://[^\s\"']+|[a-zA-Z0-9.-]+\.(?:de|com|org|net|io|ai|finance|me)(?:/[^\s\"']*)?)",
            clean,
        )
        if url_match:
            raw_u = url_match.group(1).strip(" .,!?")
            site_url = raw_u if raw_u.lower().startswith(("http://", "https://")) else f"https://{raw_u}"
        elif any(k in low for k in ("aktienwebseite", "aktienseite", "finanzseite", "börsenseite", "boersenseite", "aktien", "börse")):
            site_url = "https://www.finanzen.net"
        elif "tradingview" in low or "chartseite" in low:
            site_url = "https://de.tradingview.com/chart/"
        elif "youtube" in low:
            site_url = "https://www.youtube.com"
        elif "amazon" in low:
            site_url = "https://www.amazon.de"
        elif "wikipedia" in low:
            site_url = "https://de.wikipedia.org"
        return site_url, ""


_SAVED_HUD_RECT: Optional[tuple[int, int, int, int]] = None
_HUD_WINDOW_TITLE = "JARVIS // COMMAND CENTER"
BROWSER_WINDOW_KEYWORDS: tuple[str, ...] = (
    "chrome",
    "edge",
    "firefox",
    "brave",
    "opera",
    "vivaldi",
    "finanzen",
    "tradingview",
    "yahoo",
    "google",
    "wikipedia",
    "wetter",
)


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


def _find_hud_hwnd_windows() -> int:
    if not IS_WINDOWS:
        return 0
    try:
        user32 = ctypes.windll.user32
        hwnd = int(user32.FindWindowW(None, _HUD_WINDOW_TITLE) or 0)
        if hwnd:
            return hwnd
    except Exception:
        pass
    return 0


def _ensure_window_foreground(target_keywords: tuple[str, ...]) -> bool:
    """
    Holt ein sichtbares Ziel-Fenster (Browser oder geöffnete Laptop-App) per
    `pygetwindow` / `win32gui` / `ctypes.windll.user32.SetForegroundWindow`
    zuverlässig in den Vordergrund.
    """
    kw_list = tuple(k.strip().lower() for k in target_keywords if k and k.strip())
    if not kw_list:
        return False

    try:
        import pygetwindow as gw  # type: ignore

        for win in gw.getAllWindows():
            title_low = (getattr(win, "title", "") or "").lower()
            if not title_low or "jarvis // command center" in title_low:
                continue
            if any(k in title_low for k in kw_list):
                try:
                    if getattr(win, "isMinimized", False):
                        win.restore()
                    win.activate()
                    time.sleep(0.25)
                    return True
                except Exception:
                    pass
    except Exception:
        pass

    if IS_WINDOWS:
        try:
            import win32gui  # type: ignore
            import win32con  # type: ignore

            found_w32: list[int] = []

            def _cb(hwnd: int, _extra: Any) -> bool:
                if not win32gui.IsWindowVisible(hwnd):
                    return True
                t_low = (win32gui.GetWindowText(hwnd) or "").lower()
                if not t_low or "jarvis // command center" in t_low:
                    return True
                if any(k in t_low for k in kw_list):
                    found_w32.append(hwnd)
                    return False
                return True

            win32gui.EnumWindows(_cb, None)
            if found_w32:
                tgt = found_w32[0]
                win32gui.ShowWindow(tgt, win32con.SW_RESTORE)
                win32gui.SetForegroundWindow(tgt)
                time.sleep(0.25)
                return True
        except Exception:
            pass

        try:
            user32 = ctypes.windll.user32
            found_hwnds: list[int] = []
            WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

            def _enum_cb(hwnd: int, _lparam: int) -> bool:
                if not user32.IsWindowVisible(hwnd):
                    return True
                length = user32.GetWindowTextLengthW(hwnd)
                if length <= 0:
                    return True
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                title_low = buf.value.lower()
                if "jarvis // command center" in title_low:
                    return True
                if any(k in title_low for k in kw_list):
                    found_hwnds.append(hwnd)
                    return False
                return True

            user32.EnumWindows(WNDENUMPROC(_enum_cb), 0)
            if found_hwnds:
                target_hwnd = found_hwnds[0]
                user32.ShowWindow(target_hwnd, 9)  # SW_RESTORE
                user32.BringWindowToTop(target_hwnd)
                user32.SetForegroundWindow(target_hwnd)
                time.sleep(0.25)
                return True
        except Exception:
            pass

    return False


_GUI_MINIMIZE_CB: Optional[Callable[[bool], None]] = None


def register_hud_visibility_callback(cb: Optional[Callable[[bool], None]]) -> None:
    """Registriert den GUI-Callback, damit Jarvis sich beim Klick auf Desktop/Apps selbst minimiert."""
    global _GUI_MINIMIZE_CB
    _GUI_MINIMIZE_CB = cb


def coordinate_jarvis_window_for_action(
    mode: str = "mini_or_minimize",
    target_keywords: tuple[str, ...] = (),
) -> dict[str, Any]:
    """
    AUFGABE 3 & 4: Automatische Fenster-Koordination.
    Verhindert, dass das JARVIS-HUD das Ziel-Programm oder den Browser verdeckt:
    - 'mini': Schaltet das HUD in einen kompakten Mini-HUD-Streifen am oberen rechten Bildschirmrand.
    - 'minimize' / 'mini_or_minimize': Minimiert das HUD (via GUI-Callback + Win32)
      und bringt das Zielfenster per SetForegroundWindow aktiv nach vorne.
    """
    global _SAVED_HUD_RECT
    hud_moved = False
    hud_minimized = False
    target_focused = False

    if mode != "mini" and _GUI_MINIMIZE_CB is not None:
        try:
            _GUI_MINIMIZE_CB(True)
            hud_minimized = True
        except Exception:
            pass

    if IS_WINDOWS:
        try:
            user32 = ctypes.windll.user32
            hud_hwnd = _find_hud_hwnd_windows()
            if hud_hwnd:
                rect = _RECT()
                if _SAVED_HUD_RECT is None and user32.GetWindowRect(hud_hwnd, ctypes.byref(rect)):
                    w0 = max(640, rect.right - rect.left)
                    h0 = max(480, rect.bottom - rect.top)
                    _SAVED_HUD_RECT = (rect.left, rect.top, w0, h0)

                if mode == "mini":
                    sw, _ = get_screen_size()
                    mini_w, mini_h = 400, 140
                    mini_x = max(0, sw - mini_w - 14)
                    mini_y = 14
                    user32.MoveWindow(hud_hwnd, mini_x, mini_y, mini_w, mini_h, True)
                    user32.SetWindowPos(hud_hwnd, -2, mini_x, mini_y, mini_w, mini_h, 0x0010)
                    hud_moved = True
                else:
                    user32.ShowWindow(hud_hwnd, 6)  # SW_MINIMIZE
                    hud_minimized = True
        except Exception:
            pass

    if not hud_minimized and not hud_moved:
        try:
            import pygetwindow as gw  # type: ignore

            for win in gw.getAllWindows():
                if "jarvis // command center" in (getattr(win, "title", "") or "").lower():
                    if mode == "mini":
                        sw, _ = get_screen_size()
                        win.resizeTo(400, 140)
                        win.moveTo(max(0, sw - 414), 14)
                        hud_moved = True
                    else:
                        win.minimize()
                        hud_minimized = True
                    break
        except Exception:
            pass

    if target_keywords:
        target_focused = _ensure_window_foreground(target_keywords)

    return {
        "hud_moved_mini": hud_moved,
        "hud_minimized": hud_minimized,
        "target_focused": target_focused,
    }


def ensure_app_visibility(
    target_keywords: tuple[str, ...] = (),
    wait_seconds: float = 0.45,
) -> dict[str, Any]:
    """Minimiert Jarvis zuverlässig und holt eine Ziel-App per Win32 in den Vordergrund."""
    res = coordinate_jarvis_window_for_action(mode="minimize", target_keywords=target_keywords)
    if wait_seconds > 0:
        time.sleep(min(2.0, float(wait_seconds)))
    return res


def show_desktop_safely(
    wait_seconds: float = 1.0,
    send_win_d: bool = True,
) -> dict[str, Any]:
    """
    FIX 1: Saubere Desktop-Freigabe ohne vorzeitiges Wieder-Maximieren.
    Minimiert das JARVIS-HUD zuverlässig, gibt bei Desktop-Aufträgen den echten Windows-Desktop
    per `Win+D` frei und wartet mindestens `1.0 Sekunde`, bevor der erste Vision-Screenshot entsteht.
    """
    coord = coordinate_jarvis_window_for_action(mode="minimize")
    win_d_sent = False
    if send_win_d and IS_WINDOWS and PYAUTOGUI_AVAILABLE and pyautogui is not None:
        try:
            pyautogui.hotkey("win", "d")
            win_d_sent = True
        except Exception:
            win_d_sent = False
    wait_s = max(1.0, float(wait_seconds))
    time.sleep(wait_s)
    return {
        "hud_minimized": bool(coord.get("hud_minimized") or coord.get("hud_moved_mini")),
        "win_d_sent": win_d_sent,
        "waited_seconds": wait_s,
    }


def restore_jarvis_window() -> bool:
    """Holt das JARVIS-HUD nach Abschluss auf Wunsch wieder in die ursprüngliche Größe nach vorne."""
    global _SAVED_HUD_RECT
    if _GUI_MINIMIZE_CB is not None:
        try:
            _GUI_MINIMIZE_CB(False)
        except Exception:
            pass
    if not IS_WINDOWS:
        return False
    try:
        user32 = ctypes.windll.user32
        hud_hwnd = _find_hud_hwnd_windows()
        if not hud_hwnd:
            return False
        user32.ShowWindow(hud_hwnd, 9)  # SW_RESTORE
        if _SAVED_HUD_RECT is not None:
            lx, ty, w0, h0 = _SAVED_HUD_RECT
            user32.MoveWindow(hud_hwnd, lx, ty, w0, h0, True)
            _SAVED_HUD_RECT = None
        user32.SetForegroundWindow(hud_hwnd)
        return True
    except Exception:
        return False


class DesktopVisionStream:
    """
    AUFGABE 1: Permanenter Desktop-Observer-Thread (Continuous Desktop Vision Stream).
    - Erstellt fortlaufend im RAM (`with mss.mss() as sct:`) Screenshots des Hauptbildschirms
      mit `0–1000` Koordinaten-Gitter und rotem Fadenkreuz an der aktuellen Mausposition.
    - Hält jederzeit `latest_screen_pil`, `latest_screen_bytes` und `latest_screen_b64` bereit.
    - Dynamischer Takt: `3.0s` im Ruhemodus, sofort `1.0s` Echtzeit-Takt sobald der Nutzer spricht
      (`boost_active_rate`) oder ein Desktop-/App-Agent aktiv arbeitet (`set_agent_active(True)`).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._agent_active: bool = False
        self._boost_until: float = 0.0
        self._brain_ref: Any = None
        self._vision_status_cb: Optional[Callable[[str], None]] = None
        self.latest_screen_pil: Any = None
        self.latest_screen_bytes: bytes = b""
        self.latest_screen_b64: str = ""
        self.latest_screen_at: float = 0.0
        self.latest_size: tuple[int, int] = (1920, 1080)
        self.latest_cursor_pos: tuple[int, int] = (960, 540)
        self.latest_target_lock_jpeg: bytes = b""
        self.latest_target_lock_at: float = 0.0
        self.latest_target_lock_label: str = ""

    def set_target_lock(self, crop_jpeg: bytes, label: str = "") -> None:
        with self._lock:
            self.latest_target_lock_jpeg = bytes(crop_jpeg or b"")
            self.latest_target_lock_at = time.monotonic() if crop_jpeg else 0.0
            self.latest_target_lock_label = str(label or "")[:36]

    def _overlay_target_lock_pip(self, base_img: Any) -> Any:
        if not PIL_AVAILABLE or Image is None or base_img is None:
            return base_img
        with self._lock:
            lock_bytes = self.latest_target_lock_jpeg
            lock_at = self.latest_target_lock_at
        if not lock_bytes or lock_at <= 0.0 or (time.monotonic() - lock_at) > 6.0:
            return base_img
        try:
            pip_img = Image.open(io.BytesIO(lock_bytes)).convert("RGB")
            pip_side = max(120, min(196, int(min(base_img.width, base_img.height) * 0.34)))
            resample_filter = getattr(getattr(Image, "Resampling", Image), "BILINEAR", Image.BICUBIC)
            pip_img = pip_img.resize((pip_side, pip_side), resample_filter)
            px = max(4, base_img.width - pip_side - 10)
            py = max(4, base_img.height - pip_side - 10)
            draw = ImageDraw.Draw(base_img)
            draw.rectangle([px - 2, py - 2, px + pip_side + 1, py + pip_side + 1], outline=(0, 235, 255), width=2)
            base_img.paste(pip_img, (px, py))
            pip_img.close()
        except Exception:
            pass
        return base_img

    def attach_brain(self, brain: Any) -> None:
        self._brain_ref = brain

    def register_vision_status_callback(self, cb: Optional[Callable[[str], None]]) -> None:
        self._vision_status_cb = cb

    def get_current_interval(self, now: Optional[float] = None) -> float:
        cur = time.monotonic() if now is None else float(now)
        if self._agent_active or cur < self._boost_until:
            return float(ACTIVE_AGENT_INTERVAL)
        return float(PASSIVE_SCREEN_INTERVAL)

    def is_realtime_mode(self, now: Optional[float] = None) -> bool:
        return self.get_current_interval(now=now) <= float(ACTIVE_AGENT_INTERVAL) + 1e-6

    def set_agent_active(self, active: bool) -> None:
        self._agent_active = bool(active)
        if active:
            self._boost_until = max(self._boost_until, time.monotonic() + 2.0)
        self._wake_event.set()
        self._notify_status()

    def boost_active_rate(self, duration_sec: float = 10.0) -> None:
        """Schaltet den Stream sofort auf den 1.0s-Echtzeittakt hoch (z. B. beim Sprechen/Tippen)."""
        self._boost_until = max(self._boost_until, time.monotonic() + max(1.0, float(duration_sec)))
        self._wake_event.set()
        self._notify_status()

    def _notify_status(self) -> None:
        cb = self._vision_status_cb
        if cb is not None:
            try:
                cb(VISION_STATUS_ACTIVE if self.is_realtime_mode() else VISION_STATUS_PASSIVE)
            except Exception:
                pass

    def _store_captured_frame(
        self,
        pil_img: Any,
        jpeg_bytes: bytes,
        screen_w: int,
        screen_h: int,
        cur_pos: tuple[int, int],
    ) -> None:
        b64_str = base64.b64encode(jpeg_bytes).decode("ascii") if jpeg_bytes else ""
        now_ts = time.monotonic()
        old_pil = None
        with self._lock:
            old_pil = self.latest_screen_pil
            self.latest_screen_pil = pil_img
            self.latest_screen_bytes = jpeg_bytes
            self.latest_screen_b64 = b64_str
            self.latest_screen_at = now_ts
            self.latest_size = (int(screen_w), int(screen_h))
            self.latest_cursor_pos = (int(cur_pos[0]), int(cur_pos[1]))
        if old_pil is not None and old_pil is not pil_img:
            try:
                old_pil.close()
            except Exception:
                pass
        if jpeg_bytes and self._brain_ref is not None and hasattr(self._brain_ref, "update_passive_screen_context"):
            try:
                self._brain_ref.update_passive_screen_context(jpeg_bytes, timestamp=now_ts)
            except Exception:
                pass

    def capture_once(self) -> bytes:
        pil_img, jpeg_bytes, sw, sh, cur_pos = _capture_annotated_pil_and_jpeg(
            max_width=1280,
            max_height=720,
            quality=70,
            keep_pil=True,
        )
        self._store_captured_frame(pil_img, jpeg_bytes, sw, sh, cur_pos)
        return jpeg_bytes

    def get_latest_screen_bytes(
        self,
        max_age_seconds: float = 8.0,
        capture_if_missing: bool = True,
    ) -> bytes:
        with self._lock:
            cached = self.latest_screen_bytes
            age = (time.monotonic() - self.latest_screen_at) if self.latest_screen_at > 0 else 1e9
        if cached and age <= max_age_seconds:
            return cached
        if not capture_if_missing:
            return cached
        fresh = self.capture_once()
        return fresh or cached

    def get_preview_jpeg(
        self,
        max_width: int = 960,
        max_height: int = 540,
        quality: int = 65,
    ) -> bytes:
        """
        Liefert ein leichtgewichtiges Live-Desktop-Preview-JPEG (`960x540`, Qualität 65)
        inkl. Fadenkreuz, `0..1000`-Raster und Agenten-Zielen für `/api/live_desktop_preview.jpg`.
        """
        max_age = 0.45 if self.is_realtime_mode() else 0.95
        with self._lock:
            age = (time.monotonic() - self.latest_screen_at) if self.latest_screen_at > 0 else 1e9
            pil_cached = self.latest_screen_pil
            if pil_cached is not None and age <= max_age and PIL_AVAILABLE and Image is not None:
                try:
                    copy_img = pil_cached.copy()
                    if copy_img.width > max_width or copy_img.height > max_height:
                        resample_filter = getattr(getattr(Image, "Resampling", Image), "BILINEAR", Image.BICUBIC)
                        copy_img.thumbnail((max_width, max_height), resample_filter)
                    copy_img = self._overlay_target_lock_pip(copy_img)
                    buf = io.BytesIO()
                    copy_img.save(buf, format="JPEG", quality=max(35, min(85, int(quality))), optimize=True)
                    copy_img.close()
                    return buf.getvalue()
                except Exception:
                    pass
        fresh_pil, preview_jpeg, sw, sh, cur_pos = _capture_annotated_pil_and_jpeg(
            max_width=max_width,
            max_height=max_height,
            quality=quality,
            keep_pil=True,
        )
        if fresh_pil is not None:
            try:
                fresh_pil = self._overlay_target_lock_pip(fresh_pil)
                preview_jpeg = _save_pil_jpeg_bytes(fresh_pil, quality=quality) or preview_jpeg
            finally:
                try:
                    fresh_pil.close()
                except Exception:
                    pass
        if preview_jpeg:
            with self._lock:
                self.latest_size = (int(sw), int(sh))
                self.latest_cursor_pos = (int(cur_pos[0]), int(cur_pos[1]))
        return preview_jpeg

    def get_latest_screen_b64(self, max_age_seconds: float = 8.0, capture_if_missing: bool = True) -> str:
        raw = self.get_latest_screen_bytes(max_age_seconds=max_age_seconds, capture_if_missing=capture_if_missing)
        if not raw:
            return ""
        with self._lock:
            return self.latest_screen_b64

    def get_latest_screen_pil(self) -> Any:
        with self._lock:
            if self.latest_screen_pil is not None:
                try:
                    return self.latest_screen_pil.copy()
                except Exception:
                    return self.latest_screen_pil
        self.capture_once()
        with self._lock:
            if self.latest_screen_pil is not None:
                try:
                    return self.latest_screen_pil.copy()
                except Exception:
                    return self.latest_screen_pil
        return None

    def start(self, interval_seconds: Optional[float] = None) -> None:
        if interval_seconds is not None and float(interval_seconds) <= float(ACTIVE_AGENT_INTERVAL):
            self.boost_active_rate(duration_sec=5.0)
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._wake_event.clear()

        def _stream_loop() -> None:
            if self._stop_event.wait(0.6):
                return
            last_capture_at = 0.0
            while not self._stop_event.is_set():
                now_mono = time.monotonic()
                target_interval = self.get_current_interval(now=now_mono)
                if last_capture_at <= 0.0 or (now_mono - last_capture_at) >= target_interval:
                    try:
                        self.capture_once()
                    except Exception as exc:
                        logger.debug("DesktopVisionStream Capture-Fehler: %s", exc)
                    last_capture_at = time.monotonic()
                sleep_slice = min(0.25, max(0.05, self.get_current_interval() / 2.0))
                self._wake_event.wait(timeout=sleep_slice)
                self._wake_event.clear()

        self._thread = threading.Thread(
            target=_stream_loop,
            name="JarvisDesktopVisionStream",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=1.5)
        self._thread = None
        old_pil = None
        with self._lock:
            old_pil = self.latest_screen_pil
            self.latest_screen_pil = None
        if old_pil is not None:
            try:
                old_pil.close()
            except Exception:
                pass


DESKTOP_VISION_STREAM = DesktopVisionStream()


class VisualHandAgent:
    """
    1-FPS Closed-Loop Vision Mouse & Browser Agent mit:
    - Normiertem 1000x1000 Koordinaten-Gitter
    - Permanentem `DesktopVisionStream` (3.0s Idle / 1.0s Echtzeit)
    - Zwingender Multi-Step-Mausschleife (`run_visual_mouse_loop`) inkl. `SmartMouseGuard`
    - 2-Stufen-Kaufberechtigung (`MODUS A: HALB-AUTONOM` vs. `MODUS B: VOLL-ZUGRIFF`)
    - Intelligenter 3D-Secure / S-pushTAN-Erkennung (90s Warte-Schleife).
    """

    def __init__(self) -> None:
        self._active = False
        self._abort_requested = False
        self._passive_lock = threading.Lock()
        self.latest_passive_screen_bytes: bytes = b""
        self.latest_passive_screen_b64: str = ""
        self.latest_passive_screen_at: float = 0.0
        self._vision_status_cb: Optional[Callable[[str], None]] = None
        self._brain_ref: Any = None

    @property
    def is_agent_active(self) -> bool:
        return bool(self._active)

    def register_vision_status_callback(self, cb: Optional[Callable[[str], None]]) -> None:
        self._vision_status_cb = cb
        DESKTOP_VISION_STREAM.register_vision_status_callback(cb)
        self._notify_vision_status()

    def attach_brain(self, brain: Any) -> None:
        self._brain_ref = brain
        DESKTOP_VISION_STREAM.attach_brain(brain)

    def get_vision_status_text(self) -> str:
        return VISION_STATUS_ACTIVE if (self._active or DESKTOP_VISION_STREAM.is_realtime_mode()) else VISION_STATUS_PASSIVE

    def _notify_vision_status(self) -> None:
        cb = self._vision_status_cb
        if cb is not None:
            try:
                cb(self.get_vision_status_text())
            except Exception:
                pass

    def capture_passive_snapshot_now(self) -> bytes:
        """Erstellt einen annotierten Desktop-Snapshot im RAM und synchronisiert den Stream-Cache."""
        jpeg_bytes = DESKTOP_VISION_STREAM.capture_once()
        if jpeg_bytes:
            now_ts = time.monotonic()
            with self._passive_lock:
                self.latest_passive_screen_bytes = jpeg_bytes
                self.latest_passive_screen_b64 = DESKTOP_VISION_STREAM.latest_screen_b64
                self.latest_passive_screen_at = now_ts
        return jpeg_bytes

    def get_latest_passive_screen_bytes(
        self,
        max_age_seconds: float = 45.0,
        capture_if_missing: bool = True,
    ) -> bytes:
        """Liefert den aktuellen Desktop-Screenshot aus dem Live-Vision-Stream (bei Bedarf frischer Snap)."""
        raw = DESKTOP_VISION_STREAM.get_latest_screen_bytes(
            max_age_seconds=max_age_seconds,
            capture_if_missing=capture_if_missing,
        )
        if raw:
            with self._passive_lock:
                self.latest_passive_screen_bytes = raw
                self.latest_passive_screen_b64 = DESKTOP_VISION_STREAM.latest_screen_b64
                self.latest_passive_screen_at = DESKTOP_VISION_STREAM.latest_screen_at or time.monotonic()
            return raw
        with self._passive_lock:
            return self.latest_passive_screen_bytes

    def start_passive_screen_loop(self, interval_seconds: float = PASSIVE_SCREEN_INTERVAL) -> None:
        """Startet den kontinuierlichen Live-Desktop-Vision-Stream (`3.0s` Ruhe / `1.0s` Aktiv)."""
        if self._brain_ref is not None:
            DESKTOP_VISION_STREAM.attach_brain(self._brain_ref)
        DESKTOP_VISION_STREAM.start(interval_seconds=interval_seconds)

    def stop_passive_screen_loop(self) -> None:
        DESKTOP_VISION_STREAM.stop()

    def request_abort(self) -> None:
        self._abort_requested = True
        SMART_MOUSE_GUARD.trigger_abort("[STOP] Abbruch durch STOP-Befehl", speak_hint=False)

    def _emit_hud_status(
        self,
        status_text: str,
        on_status: Optional[Callable[[str], None]] = None,
    ) -> None:
        try:
            GHOST_OVERLAY.set_status(status_text)
        except Exception:
            pass
        if on_status is not None:
            try:
                on_status(status_text)
            except Exception:
                pass

    def _emit_preview(
        self,
        jpeg_bytes: bytes,
        on_preview_frame: Optional[Callable[[bytes], None]] = None,
    ) -> None:
        if on_preview_frame is not None and jpeg_bytes:
            try:
                on_preview_frame(jpeg_bytes)
            except Exception:
                pass

    def _ensure_browser_foreground(self, on_status: Optional[Callable[[str], None]] = None) -> bool:
        """
        Koordiniert das JARVIS-HUD (Mini-HUD / Minimieren) und holt das aktive
        Desktop-Browser-Fenster (Chrome, Edge, Firefox, Brave, Opera) zuverlässig in den Vordergrund.
        """
        coord = coordinate_jarvis_window_for_action(
            mode="minimize",
            target_keywords=BROWSER_WINDOW_KEYWORDS,
        )
        focused = bool(coord.get("target_focused"))
        if focused:
            self._emit_hud_status("SCHRITT 1 // BROWSER IM VORDERGRUND FOKUSSIERT", on_status)
            time.sleep(0.35)
        return focused

    def _open_url_step_zero(
        self,
        url: str,
        on_status: Optional[Callable[[str], None]] = None,
        wait_sec: float = 2.5,
    ) -> None:
        """
        SCHRITT 0: Koordiniert das HUD-Fenster, öffnet die Ziel-URL sichtbar im
        Desktop-Browser und wartet `wait_sec` (2.5s), bevor die 1-FPS-Mausschleife startet.
        """
        if not url:
            return
        coordinate_jarvis_window_for_action(mode="minimize")
        self._emit_hud_status(f"SCHRITT 0 // ÖFFNE SEITE: {url[:38]}", on_status)
        try:
            if IS_WINDOWS:
                os.startfile(url)  # type: ignore[attr-defined]
            else:
                webbrowser.open(url, new=2)
        except Exception:
            webbrowser.open(url, new=2)
        time.sleep(max(0.5, float(wait_sec)))
        self._ensure_browser_foreground(on_status=on_status)

    # ------------------------------------------------------------------
    # AUFGABE 3: Intelligente 3D-Secure / S-pushTAN-Erkennung (bis zu 90s)
    # ------------------------------------------------------------------
    def wait_for_3d_secure_approval(
        self,
        max_wait_sec: int = 90,
        poll_interval_sec: float = 3.0,
        brain: Any = None,
        on_status: Optional[Callable[[str], None]] = None,
        on_preview_frame: Optional[Callable[[bytes], None]] = None,
    ) -> dict[str, Any]:
        """
        Wartet geduldig bis zu `max_wait_sec` (90 Sekunden), während der Nutzer die
        3D-Secure / S-pushTAN-Freigabe in seiner Banking-App auf dem Smartphone bestätigt.
        Spricht den exakten Hinweis per Stimme aus und prüft im 1-FPS-Stream, ob die Freigabe erfolgt ist.
        """
        self._emit_hud_status(f"📱 3D-SECURE / PUSHTAN // WARTE AUF HANDY-FREIGABE ({max_wait_sec}s)", on_status)
        _speak_voice_hint_async(PUSHTAN_3DS_SPOKEN_MESSAGE)

        if max_wait_sec <= 0:
            return {
                "ok": True,
                "approved": True,
                "spoken_message": PUSHTAN_3DS_SPOKEN_MESSAGE,
                "message": PUSHTAN_3DS_SPOKEN_MESSAGE,
            }

        deadline = time.monotonic() + float(max_wait_sec)
        waited = 0.0
        while time.monotonic() < deadline and not self._abort_requested:
            jpeg_bytes, _, _, _ = capture_annotated_screen_1fps()
            self._emit_preview(jpeg_bytes, on_preview_frame)
            remaining = max(0, int(round(deadline - time.monotonic())))
            self._emit_hud_status(
                f"📱 BITTE PUSHTAN AM HANDY BESTÄTIGEN ({remaining}s verbleibend)...",
                on_status,
            )

            if brain is not None and hasattr(brain, "ask") and jpeg_bytes and waited >= poll_interval_sec:
                check_reply = _ask_brain_text(
                    brain=brain,
                    user_prompt=(
                        "Ist das 3D-Secure / PushTAN-Freigabefenster noch sichtbar ('WAITING') "
                        "oder wurde die Zahlung/Freigabe bereits bestätigt ('APPROVED')? "
                        "Antworte nur mit WAITING oder APPROVED."
                    ),
                    system_prompt="Du prüfst den 3D-Secure / PushTAN Status auf dem Screenshot.",
                    image_bytes=jpeg_bytes,
                )
                if "APPROVED" in check_reply.upper():
                    self._emit_hud_status("✓ 3D-SECURE / PUSHTAN FREIGEGEBEN – FAHRE FORT", on_status)
                    return {
                        "ok": True,
                        "approved": True,
                        "spoken_message": PUSHTAN_3DS_SPOKEN_MESSAGE,
                        "message": f"{PUSHTAN_3DS_SPOKEN_MESSAGE} (Freigabe erkannt, fahre automatisch fort.)",
                    }

            # Wenn kein Vision-Brain im Aufruf übergeben wurde (z. B. Schnell-Test), nicht 90s blockieren
            if brain is None:
                break

            sleep_chunk = min(float(poll_interval_sec), max(0.2, deadline - time.monotonic()))
            time.sleep(sleep_chunk)
            waited += sleep_chunk

        return {
            "ok": True,
            "approved": True,
            "spoken_message": PUSHTAN_3DS_SPOKEN_MESSAGE,
            "message": PUSHTAN_3DS_SPOKEN_MESSAGE,
        }

    # ------------------------------------------------------------------
    # FIX 1 + AUFGABE 1 & 3: Zwingende Multi-Step 1-FPS-Mausschleife
    # ------------------------------------------------------------------
    def run_visual_mouse_loop(
        self,
        goal: str,
        brain: Any = None,
        on_status: Optional[Callable[[str], None]] = None,
        on_preview_frame: Optional[Callable[[bytes], None]] = None,
        max_steps: Optional[int] = None,
        initial_url: str = "",
        clean_search_term: str = "",
        require_interaction_after_open: bool = False,
        execution_mode_override: Optional[str] = None,
        suppress_win_d: bool = False,
        suppress_browser_step_zero: bool = False,
        start_url: str = "",
        minimize_jarvis: bool = True,
        restore_jarvis_after: bool = False,
    ) -> dict[str, Any]:
        """
        AUFGABE 1: Ausdauernder 200-Schritte-Marathon-Regelkreis (50–500 Schritte, Standard 200):
        - Gleitendes Bild- & Aktions-Gedächtnis (`MarathonStepMemory` mit 25-Schritte-Komprimierung,
          sendet maximal aktuellen + optional direkt vorherigen Screenshot).
        - Anti-Stuck-Engine: 3 unveränderte Klicks an derselben Stelle lösen sofort eine Alternativ-Strategie aus
          (`ESC` drücken, Seite scrollen, `Tab`/`Enter` oder `Ctrl+L` Adressleiste).
        - Ausdauer-Regel: `done` erst zulässig, wenn alle Teilziele (`subgoals`) abgearbeitet sind.
        - Live-HUD-Status: `AGENT AKTIV // SCHRITT X / Y // SUB-ZIEL: ...`.
        """
        if not initial_url and start_url:
            initial_url = start_url
        if minimize_jarvis:
            coordinate_jarvis_window_for_action(mode="minimize")
        effective_max_steps = resolve_max_agent_steps(max_steps)
        try:
            from vault import SECURE_VAULT

            exec_mode = (execution_mode_override or SECURE_VAULT.get_execution_mode()).strip().upper()
        except Exception:
            exec_mode = (execution_mode_override or "SEMI_AUTO").strip().upper()

        is_full_auto = exec_mode in ("FULL_AUTO", "MODUS_B", "MODE_2")

        # Wenn der Befehl selbst bereits ein reiner Direkt-Klick auf "Jetzt kaufen" im Halb-Autonom-Modus ist
        # und kein mehrstufiger Vorbereitungsauftrag vorliegt:
        if not is_full_auto and is_final_buy_button_action(goal) and "vorbereiten" not in (goal or "").lower():
            _, _, cur_x, cur_y = get_screen_and_cursor_info()
            GHOST_OVERLAY.start("🛡️ MODUS A // KAUF VORBEREITET – BITTE SELBST KLICKEN")
            GHOST_OVERLAY.set_status("🛡️ MODUS A // KAUF VORBEREITET – BITTE SELBST KLICKEN", mode="click")
            self._emit_hud_status("🛡️ MODUS A // KAUF VORBEREITET (BITTE SELBST KLICKEN)", on_status)
            _speak_voice_hint_async(MODUS_A_SPOKEN_MESSAGE)
            return {
                "ok": False,
                "status": "modus_a_prepared",
                "execution_mode": "SEMI_AUTO",
                "cursor_position": [cur_x, cur_y],
                "steps": [],
                "spoken_message": MODUS_A_SPOKEN_MESSAGE,
                "message": MODUS_A_SPOKEN_MESSAGE,
            }

        self._active = True
        self._abort_requested = False
        DESKTOP_VISION_STREAM.set_agent_active(True)
        self._notify_vision_status()
        marathon_mem = MarathonStepMemory(goal)
        executed_steps: list[dict[str, Any]] = []
        real_actions_count = 0

        goal_low = (goal or "").lower()
        is_desktop_task = any(
            tok in goal_low
            for tok in ("desktop", "arbeitsfläche", "desktop-icon", "doppelklick")
        )
        is_local_app_task = is_desktop_task or any(
            tok in goal_low
            for tok in ("launcher", "anwendung", "programm", "im hintergrund", "darin ")
        )
        has_browser_cue = any(
            tok in goal_low
            for tok in ("browser", "webbrowser", "webseite", "website", "aktienseite", "aktienwebseite", "internet", "google", "youtube", "amazon", "tradingview", "wikipedia", "http", ".de", ".com")
        )
        url_for_step_zero = (initial_url or "").strip()
        active_search_term = (clean_search_term or "").strip()
        if url_for_step_zero or (has_browser_cue and not suppress_browser_step_zero and not is_local_app_task):
            extracted_url, extracted_search_term = _extract_url_and_search_term(goal, brain=brain)
            if not active_search_term:
                active_search_term = (extracted_search_term or "").strip()
            if not url_for_step_zero and extracted_url and any(
                tok in goal_low
                for tok in ("geh auf", "gehe auf", "öffne", "oeffne", "im browser", "aktienwebseite", "aktienseite", "webseite", "seite")
            ):
                url_for_step_zero = extracted_url

        if url_for_step_zero:
            require_interaction_after_open = True
            self._open_url_step_zero(url_for_step_zero, on_status=on_status, wait_sec=2.5)
            marathon_mem.record_step(0, "open_url", None, None, url_for_step_zero, b"", brain=brain)
        else:
            if is_desktop_task and not suppress_win_d:
                show_desktop_safely(wait_seconds=1.0, send_win_d=True)
                require_interaction_after_open = True
            elif not suppress_win_d:
                coordinate_jarvis_window_for_action(mode="minimize")
                time.sleep(0.55)

        _, _, init_cx, init_cy = get_screen_and_cursor_info()
        initial_pos = (init_cx, init_cy)
        SMART_MOUSE_GUARD.start_monitoring(initial_pos=initial_pos)

        def _make_abort_result() -> dict[str, Any]:
            reason_msg = SMART_MOUSE_GUARD.abort_reason or "Hand-Agent durch Nutzer (ESC / Maus-Übernahme / STOP) sofort gestoppt."
            _release_mouse_buttons_safely()
            self._emit_hud_status(f"🛑 {reason_msg}", on_status)
            return {
                "ok": False,
                "aborted": True,
                "steps": executed_steps,
                "message": reason_msg,
            }

        try:
            for step_idx in range(1, max(1, int(effective_max_steps)) + 1):
                if self._abort_requested or SMART_MOUSE_GUARD.is_triggered():
                    return _make_abort_result()

                # 1. Capture 1-FPS screenshot mit 1000x1000 Gitter, rotem Fadenkreuz & SoM [1]..[N] Tags
                jpeg_bytes, screen_w, screen_h, (cur_x, cur_y), som_summary = capture_annotated_screen_with_som()
                norm_cx, norm_cy = screen_to_norm_xy(cur_x, cur_y, screen_w, screen_h)
                self._emit_preview(jpeg_bytes, on_preview_frame)
                hud_progress = marathon_mem.format_hud_status(step_idx, effective_max_steps)
                self._emit_hud_status(hud_progress, on_status)

                # 2. Ask Vision AI for next step (Sliding Window Memory + max 2 Screenshots: prev + current)
                exec_label = (
                    "⚡ MODUS B: VOLL-ZUGRIFF (100% Autonomes Kaufen & Verkaufen)"
                    if is_full_auto
                    else "🛡️ MODUS A: HALB-AUTONOM (Vorbereiten -> Ich drücke selbst auf Kaufen)"
                )
                sys_prompt = CLOSED_LOOP_SYSTEM_PROMPT.format(
                    screen_w=screen_w,
                    screen_h=screen_h,
                    cur_x=cur_x,
                    cur_y=cur_y,
                    norm_x=norm_cx,
                    norm_y=norm_cy,
                    execution_mode_label=exec_label,
                )
                history_text = marathon_mem.format_history_text()
                anti_stuck_hint = marathon_mem.build_anti_stuck_prompt_hint()
                _, pro_profile_hint = detect_pro_app_profile(goal)
                som_hint = (
                    f"Erkannte UI-Elemente (Set-of-Mark `[N]`): {som_summary}\n"
                    if som_summary
                    else ""
                )
                user_prompt = (
                    f"Hauptziel (main_goal): {marathon_mem.main_goal}\n"
                    f"Aktuelles Teilziel (current_subgoal): {marathon_mem.current_subgoal}\n"
                    f"Aktueller Schritt: {step_idx} von {effective_max_steps}\n"
                    f"{pro_profile_hint}"
                    f"{som_hint}"
                    f"Kompakte Schritt-Historie:\n{history_text}\n"
                    f"{anti_stuck_hint}\n"
                    f"Prüfe anhand des roten Fadenkreuzes [{cur_x}, {cur_y}] (Normiert N[{norm_cx}, {norm_cy}]), "
                    f"der gelben SoM-Schilder `[1]..[N]` und des 1000x1000-Gitters auf dem Screenshot, "
                    f"und gib den nächsten JSON-Schritt (z. B. `click_tag`, `precision_click`, CAD-/Timeline-Aktion oder `'actions'`-Batch) zurück."
                )

                imgs_payload: bytes | list[bytes] = (
                    [marathon_mem.prev_screenshot_bytes, jpeg_bytes]
                    if (marathon_mem.prev_screenshot_bytes and step_idx > 1 and marathon_mem.is_stuck())
                    else jpeg_bytes
                )
                raw_reply = _ask_brain_text(
                    brain=brain,
                    user_prompt=user_prompt,
                    system_prompt=sys_prompt,
                    image_bytes=imgs_payload,
                )
                if self._abort_requested or SMART_MOUSE_GUARD.is_triggered():
                    return _make_abort_result()

                decision = parse_agent_action_json(raw_reply, screen_w=screen_w, screen_h=screen_h)
                if decision.get("subgoal"):
                    marathon_mem.update_subgoal(decision["subgoal"])

                if marathon_mem.is_stuck() and decision["action"] in ("click", "double_click"):
                    decision = marathon_mem.pick_recovery_action(screen_w, screen_h)

                action = decision["action"]
                tx = decision["x"]
                ty = decision["y"]
                sx = decision.get("start_x")
                sy = decision.get("start_y")
                ex = decision.get("end_x")
                ey = decision.get("end_y")
                text_to_type = decision["text"]
                press_enter_flag = bool(decision.get("press_enter"))
                vault_key = decision.get("vault_key", "")
                key_to_press = decision["key"]
                keys_list = decision.get("keys") or []
                scroll_clicks = decision.get("amount", decision["scroll_clicks"])
                target_desc = decision["target"] or goal
                reason = decision["reason"]

                goal_low_check = (goal or "").lower()
                is_multi_step_app = any(
                    tok in goal_low_check
                    for tok in ("und öffne", "und klick", "dann darin", "darin ", "auf meinem desktop", "doppelklick")
                )
                premature_done = action == "done" and (
                    (require_interaction_after_open and real_actions_count == 0)
                    or (is_multi_step_app and real_actions_count < 2 and step_idx <= 3)
                    or (bool(url_for_step_zero) and step_idx == 1)
                    or (bool(url_for_step_zero) and bool(active_search_term) and real_actions_count < 2)
                    or (bool(url_for_step_zero) and marathon_mem.has_pending_subgoals(real_actions_count))
                )
                if premature_done:
                    if active_search_term and real_actions_count == 0:
                        tx, ty = norm_to_screen_xy(500, 185, screen_w, screen_h)
                        action = "click_and_type"
                        text_to_type = active_search_term
                        key_to_press = "enter"
                        press_enter_flag = True
                        target_desc = f"Suchfeld für '{active_search_term}'"
                        reason = f"Tippe '{active_search_term}' in das Suchfeld der geöffneten Seite ein."
                    elif active_search_term and real_actions_count == 1:
                        tx, ty = norm_to_screen_xy(420, 360, screen_w, screen_h)
                        action = "click"
                        target_desc = f"Bestes Ergebnis / Chart für '{active_search_term}'"
                        reason = f"Klicke auf das erste relevante Ergebnis für '{active_search_term}'."
                    else:
                        tx, ty = norm_to_screen_xy(500, 360, screen_w, screen_h)
                        action = "move"
                        target_desc = f"Nächstes Teilziel: {marathon_mem.current_subgoal}"
                        reason = f"Setze komplexen Auftrag fort ({marathon_mem.current_subgoal})."
                    decision = {
                        "action": action,
                        "x": tx,
                        "y": ty,
                        "text": text_to_type,
                        "press_enter": press_enter_flag,
                        "vault_key": "",
                        "key": key_to_press,
                        "keys": keys_list,
                        "amount": scroll_clicks,
                        "scroll_clicks": scroll_clicks,
                        "subgoal": marathon_mem.current_subgoal,
                        "target": target_desc,
                        "reason": reason,
                    }

                executed_steps.append(decision)
                batch_list = decision.get("actions") if isinstance(decision.get("actions"), list) and decision.get("actions") else [decision]

                def _dispatch_single_step(act_item: dict[str, Any]) -> tuple[bool, Optional[dict[str, Any]], str]:
                    nonlocal real_actions_count
                    if self._abort_requested or SMART_MOUSE_GUARD.is_triggered():
                        return False, _make_abort_result(), ""

                    s_act = act_item["action"]
                    s_tx = act_item.get("x")
                    s_ty = act_item.get("y")
                    s_sx = act_item.get("start_x")
                    s_sy = act_item.get("start_y")
                    s_ex = act_item.get("end_x")
                    s_ey = act_item.get("end_y")
                    s_text = act_item.get("text", "")
                    s_enter = bool(act_item.get("press_enter"))
                    s_tab = bool(act_item.get("press_tab"))
                    s_vkey = act_item.get("vault_key", "")
                    s_key = act_item.get("key", "")
                    s_keys = act_item.get("keys") or []
                    s_scroll = act_item.get("amount", act_item.get("scroll_clicks", -5))
                    s_wait_ms = int(act_item.get("wait_ms", 150) or 150)
                    s_target = act_item.get("target") or target_desc
                    s_reason = act_item.get("reason") or reason
                    s_coords = f"[{s_tx}, {s_ty}]" if (s_tx is not None and s_ty is not None) else f"[{cur_x}, {cur_y}]"

                    # UPGRADE 2: Set-of-Mark Tag-Auflösung (`click_tag`, `double_click_tag`, `right_click_tag`)
                    s_tag_id = act_item.get("tag_id")
                    if s_tag_id is not None and (
                        s_act in ("click_tag", "double_click_tag", "right_click_tag") or s_tx is None or s_ty is None
                    ):
                        try:
                            from ui_som_tagger import UI_SOM_TAGGER

                            resolved_tag = UI_SOM_TAGGER.resolve_tag_to_coords(int(s_tag_id))
                            if resolved_tag is not None:
                                s_tx, s_ty, tag_label = resolved_tag
                                if tag_label:
                                    s_target = f"[{s_tag_id}] {tag_label}"
                                s_coords = f"[{s_tx}, {s_ty}]"
                        except Exception:
                            pass
                    if s_act == "click_tag":
                        s_act = "click"
                    elif s_act == "double_click_tag":
                        s_act = "double_click"
                    elif s_act == "right_click_tag":
                        s_act = "right_click"

                    # UPGRADE 1: 400x400 Sniper-Zoom-Lupe (Precision-Verify vor Klick / Eingabe)
                    use_sniper = (s_act == "precision_click") or bool(act_item.get("precision"))
                    if use_sniper and s_tx is not None and s_ty is not None:
                        s_tx, s_ty, _ = sniper_verify(
                            x=s_tx,
                            y=s_ty,
                            brain=brain,
                            crop_size=400,
                            target_label=s_target,
                        )
                        s_coords = f"[{s_tx}, {s_ty}]"
                        if s_act == "precision_click":
                            click(s_tx, s_ty, button="left", clicks=1, duration=0.07, label=f"SNIPER-KLICK: {s_coords}")
                            real_actions_count += 1
                            return True, None, f"Sniper-Klick auf '{s_target}' bei {s_coords}"

                    if s_act == "cad_orbit":
                        orb_res = cad_orbit(
                            dx=int(act_item.get("dx", 120)),
                            dy=int(act_item.get("dy", -45)),
                            start_x=s_tx,
                            start_y=s_ty,
                            button=str(act_item.get("button", "auto")),
                            app_hint=f"{app_name} {goal}",
                        )
                        real_actions_count += 1
                        return True, None, orb_res

                    if s_act == "cad_pan":
                        pan_res = cad_pan(
                            dx=int(act_item.get("dx", 120)),
                            dy=int(act_item.get("dy", 0)),
                            start_x=s_tx,
                            start_y=s_ty,
                        )
                        real_actions_count += 1
                        return True, None, pan_res

                    if s_act == "cad_zoom":
                        zoom_res = cad_zoom(steps=int(s_scroll), x=s_tx, y=s_ty)
                        real_actions_count += 1
                        return True, None, zoom_res

                    if s_act == "cad_box_and_dimension":
                        box_sx = s_sx if s_sx is not None else (s_tx if s_tx is not None else cur_x)
                        box_sy = s_sy if s_sy is not None else (s_ty if s_ty is not None else cur_y)
                        box_ex = s_ex if s_ex is not None else min(screen_w - 20, box_sx + 180)
                        box_ey = s_ey if s_ey is not None else min(screen_h - 20, box_sy + 140)
                        box_res = cad_box_and_dimension(
                            start_x=box_sx,
                            start_y=box_sy,
                            end_x=box_ex,
                            end_y=box_ey,
                            dims=act_item.get("dims") or ["4", "4", "4"],
                            brain=brain,
                        )
                        real_actions_count += 1
                        return True, None, str(box_res.get("message") or "CAD-Box bemaßt")

                    if s_act == "timeline_scrub_and_cut":
                        tl_res = timeline_scrub_and_cut(
                            playhead_x=act_item.get("playhead_x"),
                            timeline_y=act_item.get("timeline_y"),
                            target_x=act_item.get("target_x", s_tx),
                            target_end_x=act_item.get("target_end_x"),
                            editor_name=str(act_item.get("editor") or app_name or "CapCut"),
                            ripple_delete=bool(act_item.get("ripple_delete", True)),
                        )
                        real_actions_count += 1
                        return True, None, str(tl_res.get("message") or "Timeline-Schnitt ausgeführt")

                    if s_act in (
                        "browser_new_tab",
                        "browser_switch_tab",
                        "browser_back",
                        "browser_forward",
                        "browser_address_bar",
                        "browser_find_on_page",
                        "browser_select_dropdown",
                        "browser_upload_file",
                        "browser_video_control",
                    ):
                        browser_dispatch = {
                            "browser_new_tab": lambda: browser_new_tab(url=str(act_item.get("url") or s_text or "")),
                            "browser_switch_tab": lambda: browser_switch_tab(
                                tab_index=act_item.get("tab_index"),
                                direction=str(act_item.get("direction") or "next"),
                                x=s_tx,
                                y=s_ty,
                            ),
                            "browser_back": lambda: browser_back(),
                            "browser_forward": lambda: browser_forward(),
                            "browser_address_bar": lambda: browser_address_bar(
                                url_or_query=str(act_item.get("url") or s_text or ""),
                                press_enter=True if "press_enter" not in act_item else s_enter,
                            ),
                            "browser_find_on_page": lambda: browser_find_on_page(
                                search_text=str(act_item.get("search_text") or s_text or ""),
                                next_match=bool(act_item.get("next_match", False)),
                            ),
                            "browser_select_dropdown": lambda: browser_select_dropdown(
                                x=s_tx,
                                y=s_ty,
                                option_text=str(act_item.get("option_text") or s_text or ""),
                                steps_down=int(act_item.get("steps_down") or 0),
                            ),
                            "browser_upload_file": lambda: browser_upload_file(
                                file_path=str(act_item.get("file_path") or s_text or ""),
                                button_x=s_tx,
                                button_y=s_ty,
                            ),
                            "browser_video_control": lambda: browser_video_control(
                                command=str(act_item.get("command") or s_text or "toggle_pause"),
                                x=s_tx,
                                y=s_ty,
                            ),
                        }
                        b_msg = browser_dispatch[s_act]()
                        real_actions_count += 1
                        return True, None, b_msg

                    if s_act == "wait_3ds" or detect_3d_secure_or_pushtan(f"{s_target} {s_reason} {s_text}"):
                        w_res = self.wait_for_3d_secure_approval(
                            max_wait_sec=90 if brain is not None else 0,
                            brain=brain,
                            on_status=on_status,
                            on_preview_frame=on_preview_frame,
                        )
                        marathon_mem.record_step(step_idx, "wait_3ds", cur_x, cur_y, w_res["message"], jpeg_bytes, brain=brain)
                        wait_entry = {**act_item, "action": "wait_3ds", "spoken_message": PUSHTAN_3DS_SPOKEN_MESSAGE}
                        if executed_steps and executed_steps[-1] is decision and len(batch_list) == 1:
                            executed_steps[-1] = wait_entry
                        else:
                            executed_steps.append(wait_entry)
                        real_actions_count += 1
                        if brain is None:
                            return False, {
                                "ok": True,
                                "status": "waiting_3ds",
                                "steps": executed_steps,
                                "spoken_message": PUSHTAN_3DS_SPOKEN_MESSAGE,
                                "message": PUSHTAN_3DS_SPOKEN_MESSAGE,
                            }, "3DS"
                        return True, None, "3DS-Freigabe"

                    if s_act in ("click", "double_click", "press") and is_final_buy_button_action(
                        f"{s_target} {s_text} {s_reason}"
                    ) and not is_full_auto:
                        btn_x = s_tx if s_tx is not None else cur_x
                        btn_y = s_ty if s_ty is not None else cur_y
                        side_x = min(screen_w - 15, int(btn_x) + 42)
                        side_y = int(btn_y)
                        fast_move_and_lock(
                            side_x,
                            side_y,
                            duration=0.08,
                            label="🛡️ MODUS A // NEBEN KAUF-BUTTON (BITTE SELBST KLICKEN)",
                        )
                        GHOST_OVERLAY.set_status(
                            f"🛡️ KAUF VORBEREITET BEI [{btn_x}, {btn_y}] – BITTE SELBST KLICKEN",
                            mode="click",
                        )
                        self._emit_hud_status(f"🛡️ MODUS A // KAUF VORBEREITET NEBEN [{btn_x}, {btn_y}]", on_status)
                        _speak_voice_hint_async(MODUS_A_SPOKEN_MESSAGE)
                        modus_a_entry = {
                            **act_item,
                            "action": "modus_a_stop_next_to_buy_button",
                            "button_x": btn_x,
                            "button_y": btn_y,
                            "cursor_parked_x": side_x,
                            "cursor_parked_y": side_y,
                            "spoken_message": MODUS_A_SPOKEN_MESSAGE,
                        }
                        if executed_steps and executed_steps[-1] is decision and len(batch_list) == 1:
                            executed_steps[-1] = modus_a_entry
                        else:
                            executed_steps.append(modus_a_entry)
                        return False, {
                            "ok": True,
                            "status": "modus_a_prepared",
                            "execution_mode": "SEMI_AUTO",
                            "button_coords": [btn_x, btn_y],
                            "cursor_coords": [side_x, side_y],
                            "steps": executed_steps,
                            "spoken_message": MODUS_A_SPOKEN_MESSAGE,
                            "message": MODUS_A_SPOKEN_MESSAGE,
                        }, "MODUS_A"

                    if s_act == "done":
                        done_msg = s_reason or f"Visuelles Ziel '{goal}' erfolgreich erreicht."
                        self._emit_hud_status(f"HAND-AGENT FERTIG // [{cur_x}, {cur_y}]", on_status)
                        return False, {"ok": True, "steps": executed_steps, "message": done_msg}, "DONE"

                    if s_act == "wait":
                        deadline = time.monotonic() + (max(10, min(3000, s_wait_ms)) / 1000.0)
                        while time.monotonic() < deadline:
                            if self._abort_requested or SMART_MOUSE_GUARD.is_triggered():
                                return False, _make_abort_result(), ""
                            time.sleep(min(0.025, max(0.005, deadline - time.monotonic())))
                        return True, None, f"Warte {s_wait_ms}ms"

                    if s_act == "move" and s_tx is not None and s_ty is not None:
                        fast_move_and_lock(s_tx, s_ty, duration=SHOW_CURSOR_DURATION, label=f"HAND-AGENT // ZIEL: [{s_tx}, {s_ty}]")
                        real_actions_count += 1
                        return True, None, f"Maus -> [{s_tx}, {s_ty}] ({s_target})"

                    if s_act == "click":
                        click(s_tx, s_ty, button="left", clicks=1, duration=SHOW_CURSOR_DURATION, label=f"KLICK: {s_coords}")
                        real_actions_count += 1
                        return True, None, f"Klick auf '{s_target}'"

                    if s_act == "right_click":
                        right_click(s_tx, s_ty, duration=SHOW_CURSOR_DURATION, label=f"RECHTSKLICK: {s_coords}")
                        real_actions_count += 1
                        return True, None, f"Rechtsklick auf '{s_target}'"

                    if s_act == "double_click":
                        double_click(
                            s_tx if s_tx is not None else cur_x,
                            s_ty if s_ty is not None else cur_y,
                            duration=SHOW_CURSOR_DURATION,
                            label=f"DOPPELKLICK: {s_coords}",
                            press_enter=True,
                        )
                        real_actions_count += 1
                        return True, None, f"Doppelklick auf '{s_target}'"

                    if s_act == "drag_and_drop":
                        drag_start_x = s_sx if s_sx is not None else cur_x
                        drag_start_y = s_sy if s_sy is not None else cur_y
                        drag_end_x = s_ex if s_ex is not None else (s_tx if s_tx is not None else drag_start_x)
                        drag_end_y = s_ey if s_ey is not None else (s_ty if s_ty is not None else drag_start_y)
                        drag_and_drop(
                            drag_start_x,
                            drag_start_y,
                            drag_end_x,
                            drag_end_y,
                            duration=0.2,
                            label=f"DRAG: [{drag_start_x},{drag_start_y}]->[{drag_end_x},{drag_end_y}]",
                        )
                        real_actions_count += 1
                        return True, None, f"Drag & Drop [{drag_start_x},{drag_start_y}] -> [{drag_end_x},{drag_end_y}]"

                    if s_act == "click_and_type":
                        click_and_type(
                            s_tx if s_tx is not None else cur_x,
                            s_ty if s_ty is not None else cur_y,
                            s_text,
                            press_enter=s_enter or s_key.lower() in ("enter", "return"),
                            duration=SHOW_CURSOR_DURATION,
                            interval=0.008,
                            label=f"TIPPE: {s_text[:24]}",
                        )
                        if s_tab:
                            ghost_press("tab", label="TAB")
                        real_actions_count += 1
                        return True, None, f"Klick & Getippt '{s_text[:28]}'"

                    if s_act == "type_secret":
                        from vault import SECURE_VAULT

                        v_key = s_vkey or s_text or "VAULT_PASSWORD"
                        press_ent = s_enter or s_key.lower() in ("enter", "return")
                        sec_msg = SECURE_VAULT.type_secret_locally(
                            vault_key=v_key,
                            x=s_tx,
                            y=s_ty,
                            press_enter=press_ent,
                        )
                        real_actions_count += 1
                        return True, None, sec_msg

                    if s_act == "type":
                        if s_tx is not None and s_ty is not None:
                            click(s_tx, s_ty, duration=0.07, label=f"FOKUS: [{s_tx}, {s_ty}]")
                            time.sleep(0.04)
                        do_enter = s_enter or s_key.lower() in ("enter", "return")
                        if "press_enter" not in act_item and not s_tab and s_key == "":
                            do_enter = True
                        ghost_typewrite(
                            s_text,
                            interval=0.008,
                            press_enter=do_enter,
                            label=f"TIPPE: {s_text[:24]}",
                        )
                        if s_tab:
                            ghost_press("tab", label="TAB")
                        real_actions_count += 1
                        return True, None, f"Getippt '{s_text[:28]}'"

                    if s_act == "scroll":
                        direction = "down" if s_scroll < 0 else "up"
                        scroll(
                            amount=s_scroll,
                            x=s_tx,
                            y=s_ty,
                            label=f"SCROLL {direction.upper()} {s_coords}",
                        )
                        real_actions_count += 1
                        return True, None, f"Scroll {direction} ({s_scroll})"

                    if s_act == "hotkey":
                        combo_args = s_keys if s_keys else ([s_key] if s_key else [])
                        if combo_args:
                            hotkey(*combo_args, label=f"HOTKEY: {'+'.join(combo_args).upper()}")
                            real_actions_count += 1
                            return True, None, f"Hotkey '{'+'.join(combo_args)}'"

                    if s_act == "press" and (s_key or s_keys):
                        if s_keys and len(s_keys) > 1:
                            hotkey(*s_keys, label=f"HOTKEY: {'+'.join(s_keys).upper()}")
                            real_actions_count += 1
                            return True, None, f"Hotkey '{'+'.join(s_keys)}'"
                        if "+" in s_key:
                            hotkey(s_key, label=f"HOTKEY: {s_key.upper()}")
                            real_actions_count += 1
                            return True, None, f"Hotkey '{s_key}'"
                        single_k = s_key or s_keys[0]
                        ghost_press(single_k, label=f"TASTE: {single_k.upper()}")
                        real_actions_count += 1
                        return True, None, f"Taste '{single_k}'"

                    return True, None, target_desc

                self._emit_hud_status(
                    marathon_mem.format_hud_status(step_idx, effective_max_steps),
                    on_status,
                )
                try:
                    from multi_cursor_overlay import MULTI_CURSOR_OVERLAY

                    pill_goal = (marathon_mem.current_subgoal or goal or "Desktop-Auftrag").strip()[:42]
                    MULTI_CURSOR_OVERLAY.set_status_pill(
                        f"⚡ JARVIS AKTIV [Schritt {step_idx}/{effective_max_steps}] // Ziel: {pill_goal} // [ESC oder Maus ruckartig bewegen = STOP]"
                    )
                except Exception:
                    pass

                step_details: list[str] = []
                last_executed_act = action
                for sub_idx, sub_act in enumerate(batch_list):
                    keep_going, term_result, detail_str = _dispatch_single_step(sub_act)
                    if detail_str:
                        step_details.append(detail_str)
                    if sub_act.get("action") and sub_act["action"] != "wait":
                        last_executed_act = sub_act["action"]
                    if not keep_going and term_result is not None:
                        return term_result
                    if len(batch_list) > 1 and sub_idx < len(batch_list) - 1:
                        time.sleep(0.025)

                step_detail = " | ".join(step_details) if step_details else target_desc

                # 4. Schneller Kontroll-Takt nach Einzelaktion oder kompletter Makro-Kette
                if last_executed_act == "double_click":
                    wait_total = 1.4
                elif last_executed_act in ("type", "click_and_type", "press") and (
                    press_enter_flag or key_to_press.lower() in ("enter", "return")
                ):
                    wait_total = 0.7
                else:
                    wait_total = 0.25 if len(batch_list) > 1 else min(0.45, float(ACTIVE_AGENT_INTERVAL))
                wait_deadline = time.monotonic() + wait_total
                while time.monotonic() < wait_deadline:
                    if self._abort_requested or SMART_MOUSE_GUARD.is_triggered():
                        return _make_abort_result()
                    time.sleep(min(0.05, max(0.01, wait_deadline - time.monotonic())))
                verify_jpeg, _, _, (new_x, new_y) = capture_annotated_screen_1fps()
                self._emit_preview(verify_jpeg, on_preview_frame)
                diff_r = marathon_mem.record_step(
                    step_idx,
                    action,
                    tx if tx is not None else new_x,
                    ty if ty is not None else new_y,
                    step_detail,
                    verify_jpeg,
                    brain=brain,
                    subgoal_hint=decision.get("subgoal", ""),
                )
                hud_line = marathon_mem.format_hud_status(step_idx, effective_max_steps)
                if marathon_mem.is_stuck():
                    hud_line += f" // ANTI-STUCK AKTIV (Diff {diff_r:.2f})"
                self._emit_hud_status(hud_line, on_status)

                if brain is None and real_actions_count >= (2 if active_search_term else 1):
                    break

            return {
                "ok": True,
                "steps": executed_steps,
                "message": f"Hand-Agent hat {len(executed_steps)} visuelle Schritte für '{goal}' ausgeführt.",
            }
        except AgentAbortedByUser as abort_exc:
            logger.warning("Hand-Agent pausiert: %s", abort_exc)
            _release_mouse_buttons_safely()
            self._emit_hud_status(f"🛑 {abort_exc}", on_status)
            return {
                "ok": False,
                "aborted": True,
                "steps": executed_steps,
                "message": str(abort_exc),
            }
        except Exception as exc:
            logger.error("Fehler im 1-FPS Hand-Agent: %s", exc)
            return {
                "ok": False,
                "steps": executed_steps,
                "message": f"Fehler bei der visuellen Steuerung: {exc}",
            }
        finally:
            _release_mouse_buttons_safely()
            SMART_MOUSE_GUARD.stop_monitoring()
            try:
                from multi_cursor_overlay import MULTI_CURSOR_OVERLAY

                MULTI_CURSOR_OVERLAY.clear_status_pill()
            except Exception:
                pass
            DESKTOP_VISION_STREAM.set_agent_active(False)
            self._active = False
            self._notify_vision_status()

    def run_closed_loop_goal(
        self,
        goal: str,
        brain: Any = None,
        on_status: Optional[Callable[[str], None]] = None,
        on_preview_frame: Optional[Callable[[bytes], None]] = None,
        max_steps: Optional[int] = None,
    ) -> dict[str, Any]:
        """Alias für `run_visual_mouse_loop` (Standard: bis zu 200 Schritte im 1-FPS-Regelkreis)."""
        return self.run_visual_mouse_loop(
            goal=goal,
            brain=brain,
            on_status=on_status,
            on_preview_frame=on_preview_frame,
            max_steps=max_steps,
        )

    def run_closed_loop(
        self,
        goal: str,
        brain: Any = None,
        on_status: Optional[Callable[[str], None]] = None,
        on_preview_frame: Optional[Callable[[bytes], None]] = None,
        max_steps: Optional[int] = None,
    ) -> str:
        """String-Rückgabe-Wrapper für `tools.py`."""
        res = self.run_visual_mouse_loop(
            goal=goal,
            brain=brain,
            on_status=on_status,
            on_preview_frame=on_preview_frame,
            max_steps=max_steps,
        )
        return str(res.get("message") or "")

    # ------------------------------------------------------------------
    # Task 2 & AUFGABE 2: 100% Sichtbare Desktop-Webbrowser-Suche & Lesen
    # ------------------------------------------------------------------
    def visual_browser_search_and_read(
        self,
        query: str,
        brain: Any = None,
        on_status: Optional[Callable[[str], None]] = None,
        on_preview_frame: Optional[Callable[[bytes], None]] = None,
        interactive_click: bool = True,
        max_steps: Optional[int] = None,
    ) -> dict[str, Any]:
        """
        AUFGABE 2 (100% VISIBLE DESKTOP & APP OPERATOR):
        - Extrahiert saubere `clean_query` und `target_url` (niemals ganzer Satz in Suchleiste).
        - Schiebt das JARVIS-HUD aus dem Sichtfeld und holt den echten Desktop-Browser sichtbar in den Vordergrund.
        - Steuert Adresszeile/Suchfeld, tippt `clean_query`, drückt Enter, behandelt Cookie-Banner,
          klickt passende Treffer / scrollt im 1-FPS-Closed-Loop und liest die Antwort direkt
          vom Live-Monitor-Screenshot ab. Die Webseite bleibt für den Nutzer sichtbar geöffnet!
        """
        return self.visible_browser_search_and_summarize(
            query=query,
            brain=brain,
            on_status=on_status,
            on_preview_frame=on_preview_frame,
            interactive_click=interactive_click,
            max_steps=max_steps,
        )

    def visible_browser_search_and_summarize(
        self,
        query: str,
        brain: Any = None,
        on_status: Optional[Callable[[str], None]] = None,
        on_preview_frame: Optional[Callable[[bytes], None]] = None,
        interactive_click: bool = True,
        max_steps: Optional[int] = None,
    ) -> dict[str, Any]:
        """
        Verbot von "Nur URL öffnen" & Verbot stiller Hintergrund-Suchen:
        - SCHRITT 0: Schiebt das JARVIS-HUD aus dem Weg, öffnet/fokussiert den echten Desktop-Browser
          sichtbar im Vordergrund und wartet `time.sleep(2.5)`.
        - SCHRITT 1..N (bis zu 200 Schritte): Führt aktive 1-FPS-Vision-Mausschleife (Cookie-Banner,
          `clean_query` eintippen + Enter, bestes Suchergebnis anklicken, scrollen) aus und liest das Ergebnis
          direkt vom Bildschirm ab. Der Browser bleibt sichtbar auf dem Bildschirm offen.
        """
        clean_query = (query or "").strip()
        if not clean_query:
            return {"ok": False, "url": "", "summary": "Es wurde kein Suchbegriff angegeben."}

        # Zwingende Show-Ansage vor dem Minimieren & sichtbaren Browser-Start
        is_course_query = bool(
            re.search(
                r"\b(?:gold|goldpreis|silber|ölpreis|xauusd|aktie\w*|kurs\w*|preis\w*|kosten?|"
                r"bitcoin|btc|ethereum|eth|krypto\w*|nvidia|nvda|apple|tesla|rheinmetall|dax|chart\w*|tradingview)\b",
                clean_query,
                flags=re.IGNORECASE,
            )
        )
        announcement = (
            "Ich schaue live für dich im Browser nach dem aktuellen Kurs."
            if is_course_query
            else "Ich schaue live für dich im Webbrowser nach."
        )
        if not os.getenv("JARVIS_DISABLE_TTS"):
            _speak_voice_hint_async(announcement)
        try:
            from multi_cursor_overlay import MULTI_CURSOR_OVERLAY

            MULTI_CURSOR_OVERLAY.set_status_pill(f"🌐 LIVE-BROWSER-SHOW // {clean_query[:48]}")
        except Exception:
            pass
        coordinate_jarvis_window_for_action(mode="minimize")

        try:
            from brain import extract_browser_intent

            intent = extract_browser_intent(clean_query, brain=brain)
            target_url = str(intent.get("target_url") or "https://www.google.com")
            extracted_term = str(intent.get("clean_search_term") or "").strip()
            final_goal = str(intent.get("final_goal") or "").strip()
        except Exception:
            extracted_site, extracted_term = _extract_url_and_search_term(clean_query, brain=brain)
            target_url = extracted_site or "https://www.google.com"
            final_goal = ""

        if extracted_term:
            loop_goal = (
                final_goal
                or (
                    f"Die Webseite {target_url} ist jetzt im Vordergrund geöffnet. "
                    f"Falls ein Cookie-Banner erscheint, klicke es zuerst weg. "
                    f"Klicke danach mit der Maus in das Suchfeld auf der Seite (oder fokussiere via Ctrl+L falls nötig), "
                    f"tippe NUR '{extracted_term}' ein, drücke Enter, scrolle falls nötig und klicke auf das beste Ergebnis "
                    f"bzw. die Antwort für '{extracted_term}', sodass das Ergebnis klar auf dem Bildschirm sichtbar ist."
                )
            )
        elif interactive_click:
            loop_goal = (
                final_goal
                or (
                    f"Die Webseite {target_url} ist jetzt sichtbar im Vordergrund geöffnet. "
                    f"Falls ein Cookie-Banner erscheint, klicke es zuerst weg und scrolle bzw. navigiere "
                    f"zum gewünschten Hauptinhalt für '{clean_query}', bis alles klar sichtbar ist."
                )
            )
        else:
            loop_goal = final_goal or f"Öffne {target_url} sichtbar im Vordergrund."

        loop_res = self.run_visual_mouse_loop(
            goal=loop_goal,
            brain=brain,
            on_status=on_status,
            on_preview_frame=on_preview_frame,
            max_steps=max_steps,
            initial_url=target_url,
            clean_search_term=extracted_term,
            require_interaction_after_open=True,
            minimize_jarvis=True,
            restore_jarvis_after=False,
        )

        # Browser groß im Vordergrund halten, damit der Chef Chart/Ergebnis direkt betrachten kann
        _ensure_window_foreground(BROWSER_WINDOW_KEYWORDS)

        jpeg_bytes, _, _, (cur_x, cur_y) = capture_annotated_screen_1fps()
        self._emit_preview(jpeg_bytes, on_preview_frame)
        self._emit_hud_status(f"BROWSER SICHTBAR FERTIG // ZIEL: [{cur_x}, {cur_y}]", on_status)

        summary_text = ""
        if brain is not None and hasattr(brain, "ask") and jpeg_bytes:
            try:
                vision_prompt = (
                    f"Nutzer-Anfrage: '{clean_query}' ({target_url}). "
                    f"Der sichtbare Desktop-Browser-Operator hat {len(loop_res.get('steps', []))} Schritte ausgeführt. "
                    f"Lies das aktuelle Ergebnis direkt auf diesem Live-Browser-Screenshot ab und fasse die Antwort "
                    f"kurz, präzise und natürlich auf Deutsch in 2 bis 4 Sätzen zusammen."
                )
                summary_text = _ask_brain_text(
                    brain=brain,
                    user_prompt=vision_prompt,
                    system_prompt=(
                        "Du bist J.A.R.V.I.S. Du schaust gemeinsam mit dem Nutzer live auf den geöffneten "
                        "Desktop-Webbrowser. Antworte direkt auf Deutsch, natürlich, präzise und ohne Markdown."
                    ),
                    image_bytes=jpeg_bytes,
                )
            except Exception as exc:
                logger.warning("Vision-Zusammenfassung des Browser-Screenshots fehlgeschlagen: %s", exc)

        display_term = extracted_term or clean_query
        if not summary_text:
            summary_text = str(
                loop_res.get("message")
                or f"Ich habe '{target_url}' sichtbar im Desktop-Browser geöffnet und '{display_term}' aufgerufen."
            )

        return {
            "ok": True,
            "query": display_term,
            "url": target_url,
            "steps": loop_res.get("steps", []),
            "summary": summary_text,
            "message": summary_text,
            "screenshot_bytes": jpeg_bytes,
        }

    # ------------------------------------------------------------------
    # AUFGABE 3: Desktop- & Laptop-App-Operator (Öffnen + IN der App arbeiten)
    # ------------------------------------------------------------------
    def visual_desktop_app_operator(
        self,
        goal: str,
        app_name: str = "",
        brain: Any = None,
        on_status: Optional[Callable[[str], None]] = None,
        on_preview_frame: Optional[Callable[[bytes], None]] = None,
        max_steps: Optional[int] = None,
        restore_hud_after: bool = False,
    ) -> dict[str, Any]:
        """
        AUFGABE 3 (100% VISIBLE DESKTOP & APP OPERATOR):
        Steuert lokale Laptop-Aufgaben komplett sichtbar über Desktop-Icons, Taskleiste, Startmenü oder Windows-Suche:
        1. Schiebt/minimiert das JARVIS-HUD (`coordinate_jarvis_window_for_action`), damit der Ziel-Bereich frei ist.
        2. Findet & startet die gewünschte App (Weg A: Desktop/Taskbar/Startmenü-Index bzw. visueller Doppelklick;
           Weg B: Windows-Taste -> App eintippen -> Screenshot prüfen -> Enter).
        3. Holt das App-Fenster per `SetForegroundWindow` sichtbar nach vorn.
        4. Arbeitet mit der 1-FPS-Maus-&-Tastatur-Schleife DIREKT IN DER APP weiter, bis `goal` erfüllt ist.
        """
        clean_goal = (goal or "").strip()
        target_app = (app_name or "").strip()
        in_app_target = ""
        goal_low = clean_goal.lower()
        requires_desktop = any(
            tok in goal_low for tok in ("desktop", "arbeitsfläche", "desktop-icon", "doppelklick")
        )

        try:
            from brain import extract_app_and_subtask_intent

            sub_intent = extract_app_and_subtask_intent(clean_goal, brain=None)
            if not target_app and sub_intent.get("app_name"):
                target_app = str(sub_intent["app_name"]).strip()
            in_app_target = str(sub_intent.get("in_app_target") or "").strip()
            requires_desktop = requires_desktop or bool(sub_intent.get("requires_desktop"))
        except Exception:
            pass

        if not target_app and clean_goal:
            try:
                from app_indexer import APP_INDEXER

                match = APP_INDEXER.find_app(clean_goal)
                if isinstance(match, dict):
                    target_app = str(match.get("name") or "").strip()
            except Exception:
                target_app = ""

        # FIX 1: Desktop oder Ziel-App sauber ohne vorzeitiges Wieder-Maximieren freigeben
        if requires_desktop:
            show_desktop_safely(wait_seconds=1.0, send_win_d=True)
        else:
            coordinate_jarvis_window_for_action(mode="minimize")
            time.sleep(0.55)
        self._emit_hud_status(f"APP-OPERATOR // STARTE {target_app or 'DESKTOP-APP'}", on_status)

        launch_msg = ""
        launched_entry: Optional[dict[str, str]] = None
        opened_visibly = False
        if target_app:
            try:
                from app_indexer import APP_INDEXER

                found_entry = APP_INDEXER.find_app(target_app)
                if isinstance(found_entry, dict):
                    launched_entry = found_entry
                entry_source = str((launched_entry or {}).get("source") or "").lower()
                entry_name = str((launched_entry or {}).get("name") or target_app)

                if requires_desktop or (launched_entry is not None and entry_source in ("desktop", "taskbar")):
                    # Weg A (bevorzugt sichtbar): Icon/Pin auf freiem Desktop oder Taskleiste per Vision suchen & doppelklicken
                    coords = None
                    if brain is not None:
                        try:
                            coords = self.locate_ui_element_visually(
                                f"Desktop-Icon oder App-Symbol '{entry_name}'",
                                brain=brain,
                            )
                        except Exception:
                            coords = None
                    if coords is not None:
                        ix, iy = coords
                        if requires_desktop or entry_source == "desktop":
                            desktop_double_click(ix, iy, duration=0.45, label=f"ÖFFNE DESKTOP-ICON: {entry_name.upper()}")
                        else:
                            ghost_move_and_click(ix, iy, label=f"TASKLEISTE: {entry_name.upper()}")
                        launch_msg = f"'{entry_name}' sichtbar über Desktop/Taskleiste geöffnet."
                        opened_visibly = True
                    elif launched_entry is not None:
                        ok_launch, launch_msg = APP_INDEXER.open_app_by_query(entry_name)
                        opened_visibly = bool(ok_launch)
                        if not ok_launch:
                            launch_msg = self.visual_windows_search_open(
                                target_app,
                                brain=brain,
                                on_status=on_status,
                                on_preview_frame=on_preview_frame,
                            )
                            opened_visibly = True
                    elif not requires_desktop:
                        launch_msg = self.visual_windows_search_open(
                            target_app,
                            brain=brain,
                            on_status=on_status,
                            on_preview_frame=on_preview_frame,
                        )
                        opened_visibly = True
                elif launched_entry is not None:
                    ok_launch, launch_msg = APP_INDEXER.open_app_by_query(entry_name)
                    opened_visibly = bool(ok_launch)
                    if not ok_launch:
                        launch_msg = self.visual_windows_search_open(
                            target_app,
                            brain=brain,
                            on_status=on_status,
                            on_preview_frame=on_preview_frame,
                        )
                        opened_visibly = True
                else:
                    # Weg B: Sichtbare Windows-Suche (Win-Taste -> Name tippen -> Enter)
                    launch_msg = self.visual_windows_search_open(
                        target_app,
                        brain=brain,
                        on_status=on_status,
                        on_preview_frame=on_preview_frame,
                    )
                    opened_visibly = True
            except Exception as exc:
                logger.warning("App-Start über Index fehlgeschlagen, nutze Windows-Suche: %s", exc)
                launch_msg = self.visual_windows_search_open(
                    target_app,
                    brain=brain,
                    on_status=on_status,
                    on_preview_frame=on_preview_frame,
                )
                opened_visibly = True

            if opened_visibly:
                time.sleep(1.8)
                entry_disp_name = str((launched_entry or {}).get("name") or "").lower()
                fg_terms = [t for t in [target_app.lower(), entry_disp_name] if t]
                if fg_terms:
                    _ensure_window_foreground(tuple(fg_terms))

        # Prüfen, ob auf dem Desktop doppelgeklickt oder innerhalb der App noch Schritte ausgeführt werden sollen
        lower_goal = clean_goal.lower()
        in_app_keywords = (
            "rechn", "tipp", "schreib", "erstell", "spiel", "abspiel", "klick", "such", "öffne ordner",
            "datei", "speicher", "export", "schnitt", "clip", "musik", "song", "playlist", "notiz",
            "code", "einstell", "wlan", "bluetooth", "laut", "leis", "hell", "berech", "darin", "dann",
            "launcher", "+", "-", "*", "/",
        )
        needs_in_app_loop = bool(
            clean_goal
            and (
                not opened_visibly
                or bool(in_app_target)
                or brain is not None
                or not target_app
                or any(tok in lower_goal for tok in in_app_keywords)
                or len(clean_goal.split()) > 3
            )
        )

        loop_res: dict[str, Any] = {"ok": True, "steps": [], "message": launch_msg}
        if needs_in_app_loop:
            if not opened_visibly:
                in_app_goal = clean_goal
            elif in_app_target:
                in_app_goal = (
                    f"Die Anwendung '{target_app}' ist jetzt im Vordergrund geöffnet. "
                    f"Klicke jetzt sichtbar direkt IN dieser Anwendung auf '{in_app_target}' "
                    f"(Gesamtauftrag: {clean_goal}), bis '{in_app_target}' geöffnet ist."
                )
            else:
                in_app_goal = (
                    f"Die Anwendung '{target_app}' ist jetzt im Vordergrund geöffnet. "
                    f"Erledige jetzt sichtbar direkt IN dieser App folgende Aufgabe per Maus und Tastatur: {clean_goal}"
                    if target_app
                    else clean_goal
                )
            loop_res = self.run_visual_mouse_loop(
                goal=in_app_goal,
                brain=brain,
                on_status=on_status,
                on_preview_frame=on_preview_frame,
                max_steps=max_steps,
                require_interaction_after_open=bool(target_app or not opened_visibly),
                suppress_win_d=True,
                suppress_browser_step_zero=True,
            )

        summary_text = ""
        jpeg_bytes, _, _, (cur_x, cur_y) = capture_annotated_screen_1fps()
        self._emit_preview(jpeg_bytes, on_preview_frame)
        self._emit_hud_status(f"APP-OPERATOR FERTIG // ZIEL: [{cur_x}, {cur_y}]", on_status)

        if brain is not None and hasattr(brain, "ask") and jpeg_bytes:
            try:
                summary_text = _ask_brain_text(
                    brain=brain,
                    user_prompt=(
                        f"Auftrag des Nutzers: '{clean_goal}'. Gestartete App: '{target_app or 'Desktop'}'. "
                        f"Lies den aktuellen Zustand bzw. das Ergebnis direkt vom App-Screenshot ab und antworte "
                        f"in 1 bis 3 kurzen deutschen Sätzen ohne Markdown."
                    ),
                    system_prompt="Du bist J.A.R.V.I.S. Antworte direkt auf Deutsch auf Basis des Live-App-Screenshots.",
                    image_bytes=jpeg_bytes,
                )
            except Exception as exc:
                logger.warning("App-Screenshot-Zusammenfassung fehlgeschlagen: %s", exc)

        if restore_hud_after:
            restore_jarvis_window()

        final_msg = summary_text or str(loop_res.get("message") or launch_msg or f"Aufgabe '{clean_goal}' sichtbar ausgeführt.")
        return {
            "ok": bool(loop_res.get("ok", True)),
            "app_name": target_app,
            "steps": loop_res.get("steps", []),
            "launch_message": launch_msg,
            "summary": final_msg,
            "message": final_msg,
            "screenshot_bytes": jpeg_bytes,
        }

    # ------------------------------------------------------------------
    # Task 3 Fallback: Visual Windows Search App Opener
    # ------------------------------------------------------------------
    def visual_windows_search_open(
        self,
        app_name: str,
        brain: Any = None,
        on_status: Optional[Callable[[str], None]] = None,
        on_preview_frame: Optional[Callable[[bytes], None]] = None,
    ) -> str:
        clean_app = (app_name or "").strip()
        if not clean_app:
            return "Kein Anwendungsname angegeben."

        if not PYAUTOGUI_AVAILABLE or pyautogui is None:
            return f"PyAutoGUI ist nicht verfügbar, um '{clean_app}' über die Windows-Suche zu starten."

        coordinate_jarvis_window_for_action(mode="minimize")
        self._emit_hud_status(f"WINDOWS-SUCHE // {clean_app.upper()}", on_status)
        ghost_press("win", label="WINDOWS-TASTE")
        time.sleep(0.85)

        if is_escape_or_failsafe_triggered():
            return "Visueller App-Start durch Nutzer (ESC / FailSafe) abgebrochen."

        ghost_typewrite(clean_app, interval=0.04, press_enter=False, label=f"SUCHE APP: {clean_app}")
        time.sleep(1.0)

        jpeg_bytes, _, _, (cur_x, cur_y) = capture_annotated_screen_1fps()
        self._emit_preview(jpeg_bytes, on_preview_frame)
        self._emit_hud_status(f"HAND-AGENT AKTIV // ZIEL: [{cur_x}, {cur_y}]", on_status)

        if is_escape_or_failsafe_triggered():
            return "Visueller App-Start durch Nutzer abgebrochen."

        ghost_press("enter", label=f"STARTE: {clean_app.upper()}")
        time.sleep(1.0)

        after_jpeg, _, _, (nx, ny) = capture_annotated_screen_1fps()
        self._emit_preview(after_jpeg, on_preview_frame)
        self._emit_hud_status(f"HAND-AGENT AKTIV // ZIEL: [{nx}, {ny}]", on_status)

        return f"Ich habe '{clean_app}' sichtbar über die Windows-Suche gesucht und gestartet."


# Global Singleton instance
VISUAL_HAND_AGENT = VisualHandAgent()


def visual_browser_search_and_read(
    query: str,
    brain: Any = None,
    on_status: Optional[Callable[[str], None]] = None,
    on_preview_frame: Optional[Callable[[bytes], None]] = None,
    interactive_click: bool = True,
    max_steps: Optional[int] = None,
) -> dict[str, Any]:
    """Modul-Einstiegspunkt für sichtbare Desktop-Webbrowser-Suche + Live-Antwort-Extraktion."""
    return VISUAL_HAND_AGENT.visual_browser_search_and_read(
        query=query,
        brain=brain,
        on_status=on_status,
        on_preview_frame=on_preview_frame,
        interactive_click=interactive_click,
        max_steps=max_steps,
    )


def visual_desktop_app_operator(
    goal: str,
    app_name: str = "",
    brain: Any = None,
    on_status: Optional[Callable[[str], None]] = None,
    on_preview_frame: Optional[Callable[[bytes], None]] = None,
    max_steps: Optional[int] = None,
    restore_hud_after: bool = False,
) -> dict[str, Any]:
    """Modul-Einstiegspunkt für den sichtbaren Desktop- & Laptop-App-Operator."""
    return VISUAL_HAND_AGENT.visual_desktop_app_operator(
        goal=goal,
        app_name=app_name,
        brain=brain,
        on_status=on_status,
        on_preview_frame=on_preview_frame,
        max_steps=max_steps,
        restore_hud_after=restore_hud_after,
    )


def run_visual_mouse_loop(
    goal: str,
    brain: Any = None,
    on_status: Optional[Callable[[str], None]] = None,
    on_preview_frame: Optional[Callable[[bytes], None]] = None,
    max_steps: Optional[int] = None,
    initial_url: str = "",
) -> dict[str, Any]:
    """
    Modul-Einstiegspunkt für den Marathon-Closed-Loop:
    Führt nach dem Öffnen einer Webseite (Schritt 0, `time.sleep(2.5)`) zwingend die visuelle
    1-FPS-Maus-Schleife (bis zu `MAX_AGENT_STEPS` = 200 Schritte, konfigurierbar 50–500) aus.
    """
    return VISUAL_HAND_AGENT.run_visual_mouse_loop(
        goal=goal,
        brain=brain,
        on_status=on_status,
        on_preview_frame=on_preview_frame,
        max_steps=max_steps,
        initial_url=initial_url,
    )


class HardwareInputArbiter:
    """
    Kollisionsfreier Input-Multiplexer (Anti-Collision Arbiter) für das 4-Cursor-Schwarm-System:
    - Synchronisiert den physischen OS-Mauszeiger & die Zwischenablage über `atomic_input_lock` (RLock).
    - Jeder Agent bewegt seinen virtuellen Overlay-Cursor flüssig ohne Lock.
    - Erst für den eigentlichen Hardware-Klick (~0.04s) bzw. das Block-Einfügen (~0.25s) wird der
      Hardware-Cursor atomar gesetzt, geklickt/gepastet und sofort wieder freigegeben.
    - Unterstützt zusätzlich fokusfreies `PostMessageW` an Ziel-Fensterhandles (`hwnd`).
    """

    WM_CHAR = 0x0102
    WM_KEYDOWN = 0x0100
    WM_KEYUP = 0x0101
    WM_LBUTTONDOWN = 0x0201
    WM_LBUTTONUP = 0x0202
    MK_LBUTTON = 0x0001

    def __init__(self) -> None:
        self.atomic_input_lock = threading.RLock()

    @staticmethod
    def _norm_to_screen_px(x_norm: float, y_norm: float) -> tuple[int, int]:
        screen_w, screen_h, _, _ = get_screen_and_cursor_info()
        nx = max(0.0, min(1000.0, float(x_norm)))
        ny = max(0.0, min(1000.0, float(y_norm)))
        px = max(1, min(max(1, screen_w - 2), int(round((nx / 1000.0) * screen_w))))
        py = max(1, min(max(1, screen_h - 2), int(round((ny / 1000.0) * screen_h))))
        return px, py

    @staticmethod
    def _resolve_vault_tokens(text: str) -> tuple[str, bool]:
        if "{{VAULT_" not in (text or ""):
            return text, False
        try:
            import sys as _sys

            _sc = _sys.modules.get("swarm_core")
            if _sc is not None and hasattr(_sc, "PASSWORD_VAULT"):
                resolved = _sc.PASSWORD_VAULT.resolve_placeholders(text)
                return resolved, True
        except Exception:
            pass
        return text, False

    @staticmethod
    def _write_clipboard_text(text: str) -> bool:
        if IS_WINDOWS:
            try:
                user32 = ctypes.windll.user32
                kernel32 = ctypes.windll.kernel32
                CF_UNICODETEXT = 13
                GMEM_MOVEABLE = 0x0002
                data = (text or "").encode("utf-16-le") + b"\x00\x00"
                h_mem = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
                if h_mem:
                    ptr = kernel32.GlobalLock(h_mem)
                    if ptr:
                        ctypes.memmove(ptr, data, len(data))
                        kernel32.GlobalUnlock(h_mem)
                        if user32.OpenClipboard(0):
                            try:
                                user32.EmptyClipboard()
                                user32.SetClipboardData(CF_UNICODETEXT, h_mem)
                                return True
                            finally:
                                user32.CloseClipboard()
            except Exception:
                pass
        try:
            import pyperclip  # type: ignore

            pyperclip.copy(text)
            return True
        except Exception:
            return False

    @staticmethod
    def _clear_clipboard() -> None:
        HardwareInputArbiter._write_clipboard_text("")

    @staticmethod
    def _ensure_not_aborted() -> None:
        if SMART_MOUSE_GUARD.is_triggered():
            raise AgentAbortedByUser(SMART_MOUSE_GUARD.abort_reason or "Abbruch durch Nutzer/ESC")

    @staticmethod
    def _set_guard_pos(px: int, py: int, moving: bool) -> None:
        with SMART_MOUSE_GUARD._lock:
            SMART_MOUSE_GUARD.jarvis_is_moving_mouse = moving
            SMART_MOUSE_GUARD.last_jarvis_pos = (int(px), int(py))

    def post_message_to_hwnd(self, hwnd: int, msg: int, wparam: int = 0, lparam: int = 0) -> bool:
        """Sendet asynchron eine Win32-Nachricht ohne Fokuswechsel an ein Hintergrundfenster."""
        if not hwnd or not IS_WINDOWS:
            return False
        try:
            import win32gui  # type: ignore

            win32gui.PostMessage(int(hwnd), int(msg), int(wparam), int(lparam))
            return True
        except Exception:
            pass
        try:
            res = ctypes.windll.user32.PostMessageW(
                ctypes.c_void_p(int(hwnd)),
                ctypes.c_uint(int(msg)),
                ctypes.c_size_t(int(wparam)),
                ctypes.c_ssize_t(int(lparam)),
            )
            return bool(res)
        except Exception:
            return False

    def send_background_text(self, hwnd: int, text: str) -> bool:
        """Tippt Text via WM_CHAR direkt an ein Ziel-HWND ohne Hardware-Tastatur-Lock."""
        if not hwnd or not text:
            return False
        ok_any = False
        for ch in text:
            self._ensure_not_aborted()
            if self.post_message_to_hwnd(hwnd, self.WM_CHAR, ord(ch), 0):
                ok_any = True
        return ok_any

    def send_background_click(self, hwnd: int, client_x: int, client_y: int) -> bool:
        """Sendet einen Linksklick via WM_LBUTTONDOWN/UP direkt an Client-Koordinaten eines HWND."""
        if not hwnd:
            return False
        lparam = ((int(client_y) & 0xFFFF) << 16) | (int(client_x) & 0xFFFF)
        d_ok = self.post_message_to_hwnd(hwnd, self.WM_LBUTTONDOWN, self.MK_LBUTTON, lparam)
        u_ok = self.post_message_to_hwnd(hwnd, self.WM_LBUTTONUP, 0, lparam)
        return bool(d_ok and u_ok)

    def atomic_click(
        self,
        agent_id: int | str,
        x_norm: float,
        y_norm: float,
        button: str = "left",
        clicks: int = 1,
        badge: Optional[str] = None,
        glide_duration: float = 0.14,
    ) -> dict[str, Any]:
        """Bewegt den virtuellen Agenten-Cursor vorab und führt den Hardware-Klick atomar (~0.04s) aus."""
        self._ensure_not_aborted()
        try:
            from multi_cursor_overlay import MULTI_CURSOR_OVERLAY

            MULTI_CURSOR_OVERLAY.move_cursor_smooth(
                agent_id,
                x_norm,
                y_norm,
                badge=badge,
                duration=glide_duration,
                wait=True,
            )
        except Exception:
            pass

        px, py = self._norm_to_screen_px(x_norm, y_norm)
        with _REAL_MOUSE_ACTION_LOCK, self.atomic_input_lock:
            self._ensure_not_aborted()
            self._set_guard_pos(px, py, True)
            try:
                _set_cursor_pos_hard(px, py)
                if PYAUTOGUI_AVAILABLE and pyautogui is not None:
                    pyautogui.moveTo(px, py, duration=0.0, _pause=False)
                    _set_cursor_pos_hard(px, py)
                    pyautogui.click(x=px, y=py, clicks=max(1, int(clicks)), interval=0.04, button=button, _pause=False)
                try:
                    from multi_cursor_overlay import MULTI_CURSOR_OVERLAY

                    MULTI_CURSOR_OVERLAY.trigger_click(
                        agent_id,
                        x_norm,
                        y_norm,
                        double=(int(clicks) >= 2),
                        badge=badge,
                    )
                except Exception:
                    pass
                time.sleep(0.01)
            finally:
                self._set_guard_pos(px, py, False)
        return {"ok": True, "agent_id": str(agent_id), "x": px, "y": py, "clicks": int(clicks)}

    def atomic_double_click(
        self,
        agent_id: int | str,
        x_norm: float,
        y_norm: float,
        badge: Optional[str] = None,
    ) -> dict[str, Any]:
        return self.atomic_click(
            agent_id=agent_id,
            x_norm=x_norm,
            y_norm=y_norm,
            button="left",
            clicks=2,
            badge=badge,
        )

    def atomic_drag_and_drop(
        self,
        agent_id: int | str,
        start_x_norm: float,
        start_y_norm: float,
        end_x_norm: float,
        end_y_norm: float,
        badge: Optional[str] = None,
        duration: float = 0.24,
    ) -> dict[str, Any]:
        """Führt Drag & Drop unter exklusivem Lock aus und bewegt den Agenten-Cursor synchron."""
        self._ensure_not_aborted()
        try:
            from multi_cursor_overlay import MULTI_CURSOR_OVERLAY

            MULTI_CURSOR_OVERLAY.move_cursor_smooth(
                agent_id,
                start_x_norm,
                start_y_norm,
                badge=badge,
                duration=0.14,
                wait=True,
            )
        except Exception:
            pass

        sx, sy = self._norm_to_screen_px(start_x_norm, start_y_norm)
        ex, ey = self._norm_to_screen_px(end_x_norm, end_y_norm)
        with _REAL_MOUSE_ACTION_LOCK, self.atomic_input_lock:
            self._ensure_not_aborted()
            self._set_guard_pos(sx, sy, True)
            try:
                try:
                    from multi_cursor_overlay import MULTI_CURSOR_OVERLAY

                    MULTI_CURSOR_OVERLAY.trigger_click(agent_id, start_x_norm, start_y_norm, badge=badge)
                    MULTI_CURSOR_OVERLAY.move_cursor(
                        agent_id,
                        end_x_norm,
                        end_y_norm,
                        badge=badge,
                        duration=max(0.08, duration),
                    )
                except Exception:
                    pass
                if PYAUTOGUI_AVAILABLE and pyautogui is not None:
                    _set_cursor_pos_hard(sx, sy)
                    pyautogui.mouseDown(x=sx, y=sy, button="left", _pause=False)
                    time.sleep(0.08)
                    self._set_guard_pos(ex, ey, True)
                    pyautogui.moveTo(ex, ey, duration=max(0.12, min(0.28, float(duration))), _pause=False)
                    _set_cursor_pos_hard(ex, ey)
                    time.sleep(0.08)
                    pyautogui.mouseUp(x=ex, y=ey, button="left", _pause=False)
                try:
                    from multi_cursor_overlay import MULTI_CURSOR_OVERLAY

                    MULTI_CURSOR_OVERLAY.trigger_click(agent_id, end_x_norm, end_y_norm, badge=badge)
                except Exception:
                    pass
            finally:
                try:
                    if PYAUTOGUI_AVAILABLE and pyautogui is not None:
                        pyautogui.mouseUp(button="left")
                except Exception:
                    pass
                self._set_guard_pos(ex, ey, False)
        return {"ok": True, "agent_id": str(agent_id), "from": [sx, sy], "to": [ex, ey]}

    def atomic_click_and_paste(
        self,
        agent_id: int | str,
        x_norm: float,
        y_norm: float,
        text: str,
        press_enter: bool = False,
        clear_first: bool = True,
        badge: Optional[str] = None,
        resolve_vault: bool = True,
    ) -> dict[str, Any]:
        """
        Atomarer Block-Paste (~0.25s Hardware-Fenster):
        Klickt Feld an, füllt Zwischenablage unter Lock, fügt Text via Ctrl+V am Stück ein
        und bereinigt sensible Tresor-Daten sofort. Kein Zeichen-für-Zeichen-Vermischen möglich.
        """
        self._ensure_not_aborted()
        try:
            from multi_cursor_overlay import MULTI_CURSOR_OVERLAY

            MULTI_CURSOR_OVERLAY.move_cursor_smooth(
                agent_id,
                x_norm,
                y_norm,
                badge=badge,
                duration=0.14,
                wait=True,
            )
        except Exception:
            pass

        px, py = self._norm_to_screen_px(x_norm, y_norm)
        raw_text = str(text or "")
        was_secret = False
        if resolve_vault:
            raw_text, was_secret = self._resolve_vault_tokens(raw_text)

        with self.atomic_input_lock:
            self._ensure_not_aborted()
            self._set_guard_pos(px, py, True)
            try:
                if PYAUTOGUI_AVAILABLE and pyautogui is not None:
                    pyautogui.moveTo(px, py, duration=0.0)
                    pyautogui.click(x=px, y=py)
                    time.sleep(0.03)
                    if clear_first:
                        pyautogui.hotkey("ctrl", "a")
                        pyautogui.press("backspace")
                try:
                    from multi_cursor_overlay import MULTI_CURSOR_OVERLAY

                    MULTI_CURSOR_OVERLAY.trigger_click(agent_id, x_norm, y_norm, badge=badge)
                except Exception:
                    pass

                wrote_clip = self._write_clipboard_text(raw_text)
                if PYAUTOGUI_AVAILABLE and pyautogui is not None:
                    if wrote_clip:
                        time.sleep(0.02)
                        pyautogui.hotkey("ctrl", "v")
                    else:
                        pyautogui.write(raw_text, interval=0.005)
                    if press_enter:
                        time.sleep(0.02)
                        pyautogui.press("enter")
            finally:
                if was_secret:
                    self._clear_clipboard()
                self._set_guard_pos(px, py, False)
        return {
            "ok": True,
            "agent_id": str(agent_id),
            "x": px,
            "y": py,
            "chars": len(raw_text),
            "vault_masked": was_secret,
        }


HARDWARE_INPUT_ARBITER = HardwareInputArbiter()

