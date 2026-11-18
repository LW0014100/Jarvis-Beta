"""Strikte On-Demand-Webcam & Desktop-Vision (Hardware-Kamera im Ruhemodus komplett aus)."""

from __future__ import annotations

from datetime import datetime
import io
import json
import platform
import re
import shutil
import subprocess
import threading
import time
from typing import Any, Callable, Optional

import cv2
import mss
import numpy as np
from PIL import Image, ImageDraw, ImageFont
import pyautogui

from config import (
    VISION_STATUS_PASSIVE,
    VISION_STATUS_WEBCAM,
    WEBCAM_EXPOSURE_WARMUP_SECONDS,
    get_archive_subdir,
)
from ghost_cursor import GHOST_OVERLAY, find_ui_element_screen_coords


SCREEN_TRIGGER_PATTERN = re.compile(
    r"\b("
    r"bildschirm|desktop|screen|monitor|fenster|fehlermeldung|"
    r"(?:diesem|dem|den)\s+fehler|fehler\s+hier|was\s+steht\s+(?:hier|da|auf\s+dem\s+bildschirm)|"
    r"was\s+siehst\s+du\s+(?:gerade\s+)?(?:auf\s+(?:meinem|dem)\s+(?:bildschirm|screen|desktop|monitor))|"
    r"schau\w*(?:\s+mal)?\s+(?:auf\s+(?:meinen|den)\s+(?:bildschirm|screen|desktop|monitor))"
    r")\b",
    flags=re.IGNORECASE,
)

WEBCAM_TRIGGER_PATTERN = re.compile(
    r"\b("
    r"kamera|webcam|foto|zimmer|raum|tisch|werkbank|"
    r"durch\s+die\s+kamera|in\s+die\s+kamera|vor\s+der\s+kamera|"
    r"in\s+meiner\s+hand|halte\s+ich|vor\s+mir|"
    r"wie\s+sehe\s+ich\s+aus|siehst\s+du\s+mich|"
    r"objekt|gegenstand|werkstück|"
    r"verkabel\w*|platine|schaltung|bauteil\w*|werkzeug\w*|bastel\w*|3d[\s-]*druck\w*"
    r")\b",
    flags=re.IGNORECASE,
)

VISION_TRIGGER_PATTERN = WEBCAM_TRIGGER_PATTERN

WORKBENCH_TRIGGER_PATTERN = re.compile(
    r"\b("
    r"tisch|werkbank|arbeitsplatz|bastel\w*|mechanik|elektronik|"
    r"platine|schaltung|verkabel\w*|kabel|anschluss|anschlüsse|pin\w*|gpio|"
    r"löt\w*|bauteil\w*|widerstand|kondensator|sensor|arduino|esp\w*|raspberry|"
    r"3d[\s-]*druck\w*|druckteil\w*|werkzeug\w*|schraub\w*|"
    r"was\s+fehlt|richtig\s+aus|richtig\s+angeschlossen"
    r")\b",
    flags=re.IGNORECASE,
)


def should_capture_screen(query: str) -> bool:
    """Prüft, ob der Nutzer eine Frage zum aktuellen Desktop-/Bildschirminhalt stellt."""
    if not query:
        return False
    return bool(SCREEN_TRIGGER_PATTERN.search(query)) and not bool(
        re.search(r"\b(?:kamera|webcam|in\s+meiner\s+hand|halte\s+ich|tisch|werkbank)\b", query, flags=re.IGNORECASE)
    )


def should_capture_webcam(query: str) -> bool:
    """Prüft, ob die physische Laptop-Webcam explizit angefordert wurde."""
    if not query:
        return False
    if should_capture_screen(query):
        return False
    return bool(WEBCAM_TRIGGER_PATTERN.search(query))


def should_capture_image(query: str) -> bool:
    """Prüft anhand expliziter Schlüsselwörter, ob ein Webcam-Snapshot benötigt wird."""
    return should_capture_webcam(query)


def is_workbench_query(query: str) -> bool:
    """Erkennt Fragen zur Werkbank, Elektronik, Mechanik, 3D-Druck oder Verkabelung."""
    if not query:
        return False
    return bool(WORKBENCH_TRIGGER_PATTERN.search(query))


class CameraVision:
    """
    Strikte On-Demand Hardware-Webcam:
    - Standard-Zustand beim Start = KOMPLETT AUS (cap.release(), Kamera-LED aus, 0 % CPU).
    - Öffnet die Webcam NUR bei expliziter Anfrage (`capture_on_demand_snapshot`) oder
      manuellem Klick auf den HUD-Kamera-Button (`toggle_paused`).
    """

    def __init__(
        self,
        camera_index: int = 0,
        width: int = 1280,
        height: int = 720,
        jpeg_quality: int = 85,
        fps: float = 15.0,
        on_demand: bool = True,
        on_status_change: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.camera_index = camera_index
        self.width = width
        self.height = height
        self.jpeg_quality = jpeg_quality
        self.frame_interval = 1.0 / max(1.0, fps)
        self.on_demand = bool(on_demand)
        self.on_status_change = on_status_change

        self._frame_lock = threading.Lock()
        self._snapshot_lock = threading.Lock()
        self._latest_frame: Optional[np.ndarray] = None
        self._latest_snapshot_jpeg: Optional[bytes] = None
        self.snapshot_active: bool = False
        self._paused = threading.Event()
        if self.on_demand:
            # Standard-Zustand = KOMPLETT AUS (Kamera-LED bleibt beim Start aus)
            self._paused.set()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _emit_status(
        self,
        status_text: str,
        on_status: Optional[Callable[[str], None]] = None,
    ) -> None:
        for cb in (on_status, self.on_status_change):
            if cb is not None:
                try:
                    cb(status_text)
                except Exception:
                    pass

    def _open_picamera2(self) -> Optional[object]:
        """Öffnet offizielle Raspberry-Pi-CSI-Kameras über Picamera2/libcamera (falls vorhanden)."""
        if platform.system() != "Linux":
            return None
        try:
            from picamera2 import Picamera2  # type: ignore[import-not-found]
        except Exception:
            return None

        class _PicamAdapter:
            def __init__(self, width: int, height: int, index: int) -> None:
                self._picam = Picamera2(camera_num=max(0, index))
                cfg = self._picam.create_video_configuration(
                    main={"size": (width, height), "format": "RGB888"}
                )
                self._picam.configure(cfg)
                self._picam.start()
                self._opened = True

            def isOpened(self) -> bool:
                return self._opened

            def read(self) -> tuple[bool, Optional[np.ndarray]]:
                if not self._opened:
                    return False, None
                try:
                    rgb = self._picam.capture_array("main")
                    if rgb is None:
                        return False, None
                    bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
                    return True, bgr
                except Exception:
                    return False, None

            def release(self) -> None:
                if self._opened:
                    self._opened = False
                    try:
                        self._picam.stop()
                    except Exception:
                        pass
                    try:
                        self._picam.close()
                    except Exception:
                        pass

        try:
            adapter = _PicamAdapter(self.width, self.height, self.camera_index)
            print("[Vision] Raspberry Pi CSI-Kamera über Picamera2/libcamera aktiv.")
            return adapter
        except Exception:
            return None

    def _open_capture(self) -> object:
        system = platform.system()
        cap: Optional[cv2.VideoCapture] = None
        if system == "Windows":
            cap = cv2.VideoCapture(self.camera_index, cv2.CAP_DSHOW)
            if cap.isOpened():
                self._configure_capture(cap)
                return cap
            cap.release()
        elif system == "Linux":
            cap = cv2.VideoCapture(self.camera_index, cv2.CAP_V4L2)
            if cap.isOpened():
                self._configure_capture(cap)
                ok, test_frame = cap.read()
                if ok and test_frame is not None:
                    return cap
            cap.release()

            picam = self._open_picamera2()
            if picam is not None:
                return picam

        cap = cv2.VideoCapture(self.camera_index)
        if cap.isOpened():
            self._configure_capture(cap)
        return cap

    def _configure_capture(self, cap: cv2.VideoCapture) -> None:
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    @property
    def is_paused(self) -> bool:
        return self._paused.is_set()

    @property
    def is_active(self) -> bool:
        return not self._paused.is_set()

    def set_paused(self, paused: bool) -> None:
        """Pausiert/aktiviert die Kameraaufnahme (bei Pause sofortiges cap.release(), 0% CPU)."""
        if paused:
            self._paused.set()
            self._stop_event.set()
            if self._thread is not None and self._thread.is_alive():
                self._thread.join(timeout=1.5)
            self._thread = None
            with self._frame_lock:
                self._latest_frame = None
        else:
            self._paused.clear()
            self._stop_event.clear()
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._capture_loop, daemon=True)
                self._thread.start()

    def toggle_paused(self) -> bool:
        """Schaltet den Kamera-Privatsphäre-Modus um und gibt True zurück, wenn pausiert (AUS)."""
        next_paused = not self.is_paused
        self.set_paused(next_paused)
        return next_paused

    def start(self) -> None:
        """
        Im On-Demand-Standardmodus bleibt die Hardware-Kamera beim Start komplett AUS (`cap.release()`).
        Der Hintergrund-Stream startet nur, wenn der Nutzer ihn manuell über den HUD-Button aktiviert.
        """
        if self._paused.is_set():
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Beendet den Stream und schließt das Gerät."""
        self.close()

    def close(self) -> None:
        """Beendet den Hintergrund-Stream und gibt die Webcam beim Programmende frei."""
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._thread = None
        with self._frame_lock:
            self._latest_frame = None

    def _capture_loop(self) -> None:
        cap: Optional[cv2.VideoCapture] = None
        fail_count = 0

        try:
            while not self._stop_event.is_set():
                if self._paused.is_set():
                    if cap is not None:
                        cap.release()
                        cap = None
                    time.sleep(0.15)
                    continue

                if cap is None or not cap.isOpened():
                    if cap is not None:
                        cap.release()
                    cap = self._open_capture()
                    fail_count = 0
                    if not cap.isOpened():
                        cap.release()
                        cap = None
                        time.sleep(1.5)
                        continue
                    # Kurzer Warmup nach (Re-)Connect
                    for _ in range(3):
                        cap.read()

                loop_start = time.monotonic()
                ok, frame = cap.read()
                if ok and frame is not None:
                    fail_count = 0
                    with self._frame_lock:
                        self._latest_frame = frame
                else:
                    fail_count += 1
                    if fail_count >= 10:
                        cap.release()
                        cap = None
                        with self._frame_lock:
                            self._latest_frame = None
                        time.sleep(1.0)
                        continue

                elapsed = time.monotonic() - loop_start
                sleep_left = self.frame_interval - elapsed
                if sleep_left > 0:
                    time.sleep(sleep_left)
        finally:
            if cap is not None:
                cap.release()

    def get_latest_bgr(self) -> Optional[np.ndarray]:
        """Liefert verzögerungsfrei eine Kopie des aktuellen Live-Frames (BGR) für das HUD."""
        if self._paused.is_set():
            return None
        with self._frame_lock:
            if self._latest_frame is None:
                return None
            return self._latest_frame.copy()

    def capture_on_demand_snapshot(
        self,
        archive: bool = True,
        warmup_seconds: float = WEBCAM_EXPOSURE_WARMUP_SECONDS,
        on_status: Optional[Callable[[str], None]] = None,
    ) -> Optional[bytes]:
        """
        Strikte On-Demand-Aufnahme (AUFGABE 2):
        1. Meldet Status `WEBCAM: SNAPSHOT AKTIV`.
        2. Öffnet die physische Kamera kurzzeitig (`cv2.VideoCapture(index, cv2.CAP_DSHOW)`),
           wartet `0.4` Sekunden für Belichtung/Weißabgleich, nimmt exakt 1 scharfes Foto auf
           und schaltet sie sofort wieder per `cap.release()` komplett ab (LED aus).
        """
        self.snapshot_active = True
        self._emit_status(VISION_STATUS_WEBCAM, on_status=on_status)
        frame: Optional[np.ndarray] = None
        try:
            with self._snapshot_lock:
                if not self._paused.is_set():
                    live = self.get_latest_bgr()
                    if live is not None:
                        frame = live
                if frame is None:
                    cap: Optional[object] = None
                    try:
                        cap = self._open_capture()
                        if cap is not None and getattr(cap, "isOpened", lambda: False)():
                            warmup_end = time.monotonic() + max(0.1, float(warmup_seconds))
                            while time.monotonic() < warmup_end:
                                cap.read()
                                time.sleep(0.05)
                            ok, captured = cap.read()
                            if ok and captured is not None:
                                frame = captured
                    finally:
                        if cap is not None:
                            try:
                                cap.release()
                            except Exception:
                                pass

            if frame is None:
                return None

            ok, encoded = cv2.imencode(
                ".jpg",
                frame,
                [int(cv2.IMWRITE_JPEG_QUALITY), self.jpeg_quality],
            )
            if not ok:
                return None

            data = encoded.tobytes()
            with self._frame_lock:
                self._latest_snapshot_jpeg = data
            if archive:
                self._archive_jpeg(data)
            return data
        finally:
            self.snapshot_active = False
            self._emit_status(VISION_STATUS_PASSIVE, on_status=on_status)

    def get_current_frame(
        self,
        archive: bool = False,
        on_status: Optional[Callable[[str], None]] = None,
    ) -> Optional[bytes]:
        """
        Liefert ein aktuelles Webcam-Foto als JPEG-Bytes:
        - Wenn der manuelle Live-Stream läuft: sofort aus dem Stream.
        - Im On-Demand-Standardzustand: exakt 1 Snapshot (0.4s Belichtung) mit sofortigem `cap.release()`.
        """
        if not self._paused.is_set() and self._thread is not None and self._thread.is_alive():
            frame = self.get_latest_bgr()
            if frame is not None:
                ok, encoded = cv2.imencode(
                    ".jpg",
                    frame,
                    [int(cv2.IMWRITE_JPEG_QUALITY), self.jpeg_quality],
                )
                if ok:
                    data = encoded.tobytes()
                    if archive:
                        self._archive_jpeg(data)
                    return data

        return self.capture_on_demand_snapshot(archive=archive, on_status=on_status)

    def capture_jpeg(self, archive: bool = True) -> Optional[bytes]:
        """Abwärtskompatibler Alias für den sofortigen Abruf eines Live-Frames."""
        return self.get_current_frame(archive=archive)

    @staticmethod
    def _archive_jpeg(jpeg_bytes: bytes) -> None:
        try:
            cam_dir, _ = get_archive_subdir("kamera_fotos")
            stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S_%f")[:-3]
            (cam_dir / f"cam_{stamp}.jpg").write_bytes(jpeg_bytes)
        except Exception:
            pass


# ============================================================
# Desktop-Vision & Intelligente UI- / Taskleisten-Erkennung
# ============================================================

_UI_SYNONYMS: dict[str, list[str]] = {
    "xbox": ["Xbox", "Xbox App", "Gaming"],
    "bibliothek": ["Meine Bibliothek", "Bibliothek", "My Library", "Library"],
    "meine bibliothek": ["Meine Bibliothek", "Bibliothek", "My Library", "Library"],
    "game pass": ["Game Pass", "PC Game Pass"],
    "cloud gaming": ["Cloud Gaming", "Cloud"],
    "store": ["Store", "Microsoft Store", "Shop"],
    "suche": ["Suche", "Suchen", "Search"],
    "startseite": ["Startseite", "Home"],
    "einstellungen": ["Einstellungen", "Settings"],
    "explorer": ["Explorer", "Datei-Explorer", "File Explorer"],
    "browser": ["Microsoft Edge", "Google Chrome", "Firefox", "Browser"],
}

UI_LOCATOR_SYSTEM_PROMPT = """Du bist das präzise Desktop-Vision-Modul von Jarvis.
Du erhältst einen Screenshot des Bildschirms mit einem eingeblendeten Koordinaten-Raster (X-Achse horizontal, Y-Achse vertikal; in der Taskleiste unten zusätzlich mit 50-Pixel-Markierungen).
Finde das gesuchte Icon, den Button oder den Menüpunkt (z. B. das Xbox-App-Symbol in der Taskleiste oder einen Menüpunkt wie 'Meine Bibliothek' / 'Game Pass' im Fenster) und gib die exakte Mittelpunkt-Koordinate (x, y) zurück.

Antworte AUSSCHLIESSLICH mit genau einem JSON-Objekt ohne Markdown:
{
  "found": true,
  "x": 540,
  "y": 1055,
  "label": "Erkannter Name des Icons/Buttons"
}
Falls das Element absolut nicht auf dem Screenshot sichtbar ist, antworte mit:
{"found": false, "x": 0, "y": 0, "label": ""}
"""


def expand_ui_synonyms(target: str) -> list[str]:
    """Erweitert deutsche/englische UI-Begriffe für zuverlässige Taskleisten- und Menü-Erkennung."""
    clean = re.sub(
        r"^(?:bitte|mal|auf|zu|zur|zum|in\s+der|in\s+die|den|die|das|der|dem)\s+",
        "",
        (target or "").strip(),
        flags=re.IGNORECASE,
    ).strip(" ?!.,\"'")
    low = clean.lower()
    candidates = [clean] if clean else []
    for key, syns in _UI_SYNONYMS.items():
        if key == low or key in low:
            for s in syns:
                if s not in candidates:
                    candidates.append(s)
    return candidates or [target.strip()]


def draw_precision_coordinate_grid(img: Image.Image, step: int = 100) -> Image.Image:
    """Zeichnet ein feines Koordinaten-Raster inkl. 50px-Taskleisten-Skala für präzise Maus-Zielerfassung."""
    grid_img = img.convert("RGBA")
    overlay = Image.new("RGBA", grid_img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    font = ImageFont.load_default()
    width, height = grid_img.size

    line_col = (0, 212, 255, 88)
    taskbar_col = (255, 155, 45, 150)
    label_bg = (6, 11, 20, 185)
    label_fg = (255, 225, 130, 245)

    for x in range(step, width, step):
        draw.line([(x, 0), (x, height)], fill=line_col, width=1)
        tag = str(x)
        draw.rectangle([x + 2, 2, x + 34, 16], fill=label_bg)
        draw.text((x + 4, 3), tag, fill=label_fg, font=font)

    # Feinere 50px-Markierungen im unteren Taskleisten-Bereich (letzte 80 Pixel)
    tb_top = max(0, height - 80)
    draw.line([(0, tb_top), (width, tb_top)], fill=taskbar_col, width=1)
    for x in range(50, width, 50):
        draw.line([(x, tb_top), (x, height)], fill=taskbar_col, width=1)
        if x % 100 != 0:
            draw.rectangle([x - 14, tb_top - 14, x + 16, tb_top], fill=label_bg)
            draw.text((x - 12, tb_top - 13), str(x), fill=(0, 240, 255, 245), font=font)

    for y in range(step, height, step):
        draw.line([(0, y), (width, y)], fill=line_col, width=1)
        tag = str(y)
        draw.rectangle([2, y + 2, 36, y + 16], fill=label_bg)
        draw.text((4, y + 3), tag, fill=label_fg, font=font)

    return Image.alpha_composite(grid_img, overlay).convert("RGB")


def capture_desktop_screenshot(
    quality: int = 85,
    with_grid: bool = True,
) -> tuple[bytes, int, int]:
    """Erstellt einen hochauflösenden Desktop-Screenshot (blendet das Ghost-Overlay kurzzeitig aus)."""
    logical_w, logical_h = pyautogui.size()
    img: Optional[Image.Image] = None

    GHOST_OVERLAY.set_capture_stealth(True)
    try:
        try:
            with mss.mss() as sct:
                monitor = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
                raw = sct.grab(monitor)
                img = Image.frombytes("RGB", raw.size, raw.rgb)
        except Exception:
            if platform.system() == "Linux" and shutil.which("grim"):
                proc = subprocess.run(
                    ["grim", "-t", "jpeg", "-q", str(quality), "-"],
                    capture_output=True,
                    timeout=4.0,
                    check=False,
                )
                if proc.returncode == 0 and proc.stdout:
                    img = Image.open(io.BytesIO(proc.stdout)).convert("RGB")
            if img is None:
                raise
    finally:
        GHOST_OVERLAY.set_capture_stealth(False)

    if logical_w > 0 and logical_h > 0 and img.size != (logical_w, logical_h):
        img = img.resize((logical_w, logical_h), Image.Resampling.LANCZOS)

    if with_grid:
        img = draw_precision_coordinate_grid(img, step=100)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue(), logical_w, logical_h


def locate_ui_element_visually(
    target_description: str,
    ask_vision_fn: Optional[Callable[[str, bytes, str], str]] = None,
    taskbar_only: bool = False,
) -> Optional[dict[str, Any]]:
    """
    Sucht ein UI-Element (z. B. Taskleisten-Icon 'Xbox' oder Button 'Bibliothek')
    zuerst über Windows UIAutomation und anschließend über Screenshot-Vision-Analyse.
    """
    candidates = expand_ui_synonyms(target_description)

    # Stufe 1: Exakte Windows UIAutomation-Koordinaten (Taskleiste & Fenster)
    for cand in candidates:
        hit = find_ui_element_screen_coords(cand, taskbar_only=taskbar_only, timeout_s=3.2)
        if hit and hit.get("found"):
            return hit

    # Stufe 2: Multimodale Screenshot-Analyse mit Präzisions-Koordinatenraster
    if ask_vision_fn is not None:
        try:
            screen_bytes, sw, sh = capture_desktop_screenshot(quality=86, with_grid=True)
            region_hint = (
                f"Suche gezielt unten in der Windows-Taskleiste (Y zwischen {max(0, sh - 75)} und {sh})."
                if taskbar_only
                else f"Suche im geöffneten Anwendungsfenster oder auf dem Bildschirm ({sw}x{sh} Pixel)."
            )
            prompt = (
                f"Gesuchtes UI-Element / Icon: '{target_description}' (Synonyme: {', '.join(candidates)})\n"
                f"Bildschirmgröße: {sw}x{sh} Pixel. {region_hint}\n"
                "Lies die exakte X- und Y-Koordinate der Mitte dieses Elements am Raster ab und gib das JSON zurück."
            )
            raw = ask_vision_fn(prompt, screen_bytes, UI_LOCATOR_SYSTEM_PROMPT)
            match = re.search(r"\{.*\}", (raw or "").strip(), flags=re.DOTALL)
            if match:
                parsed = json.loads(match.group(0))
                if isinstance(parsed, dict) and parsed.get("found"):
                    px = int(parsed.get("x") or 0)
                    py = int(parsed.get("y") or 0)
                    if 2 <= px < sw and 2 <= py < sh:
                        return {
                            "found": True,
                            "x": px,
                            "y": py,
                            "name": str(parsed.get("label") or target_description),
                            "source": "vision",
                        }
        except Exception as exc:
            print(f"[Vision-UI-Locator Hinweis] {exc}")

    return None

