"""Visueller Ghost-Cursor (Always-on-Top Sci-Fi HUD Overlay, flüssige Gleitbewegungen, Klick-Ripple, Wisch-Spur & UI-Erkennung)."""

from __future__ import annotations

import ctypes
import json
import math
import os
import platform
import re
import shutil
import subprocess
import threading
import time
from typing import Any, Callable, Optional

import pyautogui

# Starrer Bildschirmecken-FailSafe deaktiviert (ersetzt durch intelligenten SmartMouseGuard + ESC)
pyautogui.FAILSAFE = False
pyautogui.PAUSE = 0.01

VK_ESCAPE = 0x1B


class GhostCursorInterrupted(RuntimeError):
    """Wird ausgelöst, wenn der Nutzer die Maussteuerung per ESC oder starker Mausbewegung abbricht."""


def _is_escape_pressed() -> bool:
    """Prüft unter Windows per GetAsyncKeyState, ob ESC gedrückt wurde."""
    if platform.system() != "Windows":
        return False
    try:
        state = ctypes.windll.user32.GetAsyncKeyState(VK_ESCAPE)
        return bool(state & 0x8000)
    except Exception:
        return False


def get_cursor_pos() -> tuple[int, int]:
    """Liest die aktuelle Mausposition schnell und plattformunabhängig aus."""
    if platform.system() == "Windows":
        try:
            class _POINT(ctypes.Structure):
                _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

            pt = _POINT()
            if ctypes.windll.user32.GetCursorPos(ctypes.byref(pt)):
                return int(pt.x), int(pt.y)
        except Exception:
            pass
    pos = pyautogui.position()
    return int(pos[0]), int(pos[1])


def check_user_interrupt(
    expected_pos: Optional[tuple[int, int]] = None,
    tolerance_px: int = 220,
) -> None:
    """Prüft ESC-Taste (0x1B) und deutliche manuelle Übernahme (>220 px; leichtes Wackeln <=140 px wird toleriert)."""
    if _is_escape_pressed():
        raise GhostCursorInterrupted(
            "[STOP] Abbruch durch ESC-Taste – automatische Maussteuerung sofort gestoppt."
        )

    if expected_pos is not None:
        cx, cy = get_cursor_pos()
        ex, ey = expected_pos
        dist = math.hypot(cx - ex, cy - ey)
        if dist > max(141, int(tolerance_px)):
            raise GhostCursorInterrupted(
                "Du hast die Maus übernommen, ich pausiere."
            )


class GhostCursorOverlay:
    """
    Durchklickbares Always-on-Top Sci-Fi-Overlay (WS_EX_LAYERED | WS_EX_TRANSPARENT).
    Zeigt:
    - Rotierenden Cyan/Gold-Zielring um den Mauszeiger
    - Live-Aktions-Label (z. B. 'Suche: Xbox App', 'Klicke: Bibliothek', 'Wische nach unten...')
    - Aufblitzende Ripple-Wellen bei jedem Klick
    - Nachleuchtende Motion-Trail-Linie bei Gleit- und Wischbewegungen
    """

    _CHROMA_BG = "#010203"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._active = False
        self._stealth_for_capture = False
        self._label = "JARVIS // GHOST-CURSOR"
        self._sub_label = "ESC oder Maus stark bewegen = Stopp"
        self._mode = "move"  # 'move' | 'click' | 'swipe' | 'search'
        self._trail: list[tuple[int, int, float, str]] = []
        self._ripples: list[dict[str, Any]] = []
        self._highlights: list[dict[str, Any]] = []
        self._highlight_only_until = 0.0
        self._angle_outer = 0.0
        self._angle_inner = 0.0
        self._on_event: Optional[Callable[[dict[str, Any]], None]] = None
        self._ready_event = threading.Event()

    def set_event_callback(self, cb: Optional[Callable[[dict[str, Any]], None]]) -> None:
        self._on_event = cb

    def _emit_event(self, payload: dict[str, Any]) -> None:
        cb = self._on_event
        if cb is not None:
            try:
                cb(payload)
            except Exception:
                pass

    def start(self, label: str = "JARVIS übernimmt Maussteuerung...") -> None:
        """Aktiviert das sichtbare Ghost-Cursor-Overlay auf dem Desktop."""
        with self._lock:
            self._active = True
            self._stealth_for_capture = False
            if label:
                self._label = label
            cx, cy = get_cursor_pos()
            now = time.monotonic()
            self._trail.append((cx, cy, now, self._mode))

        if self._thread is None or not self._thread.is_alive():
            self._ready_event.clear()
            self._thread = threading.Thread(target=self._run_tk_overlay, daemon=True)
            self._thread.start()
            self._ready_event.wait(timeout=1.0)

        self._emit_event({"type": "ghost_cursor", "active": True, "label": self._label, "mode": self._mode})

    def stop(self) -> None:
        """Blendet das Ghost-Cursor-Overlay nach Abschluss der Aktion wieder aus."""
        with self._lock:
            self._active = False
            self._highlight_only_until = 0.0
            self._trail.clear()
            self._ripples.clear()
            self._highlights.clear()
        self._emit_event({"type": "ghost_cursor", "active": False, "label": "", "mode": "idle"})

    def add_highlight_box(
        self,
        x: int,
        y: int,
        width: int,
        height: int,
        label: str = "JARVIS SCAN",
        color: str = "#f59e0b",
        duration: float = 2.4,
    ) -> None:
        """Zeichnet einen pulsierenden Region-Highlight-Rahmen über Monitor 1, 2 oder 3."""
        now = time.monotonic()
        dur = max(0.5, min(10.0, float(duration)))
        with self._lock:
            self._highlights.append(
                {
                    "x": int(x),
                    "y": int(y),
                    "w": max(20, int(width)),
                    "h": max(20, int(height)),
                    "label": str(label or "JARVIS SCAN")[:48],
                    "color": color,
                    "start": now,
                    "duration": dur,
                }
            )
            if not self._active:
                self._active = True
                self._highlight_only_until = max(self._highlight_only_until, now + dur + 0.15)
        if self._thread is None or not self._thread.is_alive():
            self._ready_event.clear()
            self._thread = threading.Thread(target=self._run_tk_overlay, daemon=True)
            self._thread.start()
            self._ready_event.wait(timeout=0.9)

    def set_status(self, label: str, mode: str = "move") -> None:
        """Aktualisiert den angezeigten Aktionstext neben dem Mauszeiger."""
        with self._lock:
            self._active = True
            self._label = label
            self._mode = mode
        if self._thread is None or not self._thread.is_alive():
            self.start(label=label)
        else:
            self._emit_event({"type": "ghost_cursor", "active": True, "label": label, "mode": mode})

    def record_point(self, x: int, y: int, mode: Optional[str] = None) -> None:
        now = time.monotonic()
        with self._lock:
            m = mode or self._mode
            if not self._trail or math.hypot(x - self._trail[-1][0], y - self._trail[-1][1]) >= 3:
                self._trail.append((int(x), int(y), now, m))
                if len(self._trail) > 36:
                    self._trail = self._trail[-36:]

    def add_ripple(
        self,
        x: int,
        y: int,
        color: str = "#ff9d2e",
        secondary_color: str = "#00f0ff",
        label: str = "",
    ) -> None:
        """Erzeugt eine aufblitzende Klick-Ripple-Welle an der Klick-Koordinate."""
        now = time.monotonic()
        with self._lock:
            if label:
                self._label = label
                self._mode = "click"
            self._ripples.append(
                {
                    "x": int(x),
                    "y": int(y),
                    "start": now,
                    "duration": 0.52,
                    "color": color,
                    "secondary": secondary_color,
                    "label": label,
                }
            )
        self._emit_event(
            {
                "type": "ghost_cursor_click",
                "x": int(x),
                "y": int(y),
                "label": label or self._label,
            }
        )

    def set_capture_stealth(self, stealth: bool) -> None:
        """Blendet das Overlay für ~40ms während eines Vision-Screenshots unsichtbar, damit Icons frei sichtbar sind."""
        with self._lock:
            self._stealth_for_capture = stealth
        if stealth:
            time.sleep(0.04)

    @staticmethod
    def _apply_windows_clickthrough(hwnd: int) -> None:
        """Setzt WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE."""
        if platform.system() != "Windows" or not hwnd:
            return
        try:
            user32 = ctypes.windll.user32
            GWL_EXSTYLE = -20
            WS_EX_LAYERED = 0x00080000
            WS_EX_TRANSPARENT = 0x00000020
            WS_EX_TOPMOST = 0x00000008
            WS_EX_TOOLWINDOW = 0x00000080
            WS_EX_NOACTIVATE = 0x08000000

            current_style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            new_style = (
                current_style
                | WS_EX_LAYERED
                | WS_EX_TRANSPARENT
                | WS_EX_TOPMOST
                | WS_EX_TOOLWINDOW
                | WS_EX_NOACTIVATE
            )
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, new_style)
            # SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_FRAMECHANGED
            user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010 | 0x0020)
        except Exception:
            pass

    def _run_tk_overlay(self) -> None:
        try:
            import tkinter as tk
        except Exception as exc:
            print(f"[Ghost-Cursor] Tkinter nicht verfügbar ({exc}) – fahre ohne Overlay fort.")
            self._ready_event.set()
            return

        if platform.system() == "Linux" and not (os.getenv("DISPLAY") or os.getenv("WAYLAND_DISPLAY")):
            self._ready_event.set()
            return

        try:
            root = tk.Tk()
            root.withdraw()
            root.overrideredirect(True)

            vx, vy = 0, 0
            sw = root.winfo_screenwidth()
            sh = root.winfo_screenheight()
            if platform.system() == "Windows":
                try:
                    u32 = ctypes.windll.user32
                    v_x = int(u32.GetSystemMetrics(76))
                    v_y = int(u32.GetSystemMetrics(77))
                    v_w = int(u32.GetSystemMetrics(78))
                    v_h = int(u32.GetSystemMetrics(79))
                    if v_w > 0 and v_h > 0:
                        vx, vy, sw, sh = v_x, v_y, v_w, v_h
                except Exception:
                    pass
            pos_str = f"{vx:+d}{vy:+d}"
            root.geometry(f"{sw}x{sh}{pos_str}")
            root.configure(bg=self._CHROMA_BG)
            root.attributes("-topmost", True)

            if platform.system() == "Windows":
                root.attributes("-transparentcolor", self._CHROMA_BG)
            else:
                try:
                    root.wait_visibility(root)
                    root.attributes("-alpha", 0.85)
                except Exception:
                    pass

            canvas = tk.Canvas(
                root,
                width=sw,
                height=sh,
                bg=self._CHROMA_BG,
                highlightthickness=0,
                bd=0,
            )
            canvas.pack(fill="both", expand=True)

            root.deiconify()
            root.update_idletasks()

            if platform.system() == "Windows":
                hwnd = ctypes.windll.user32.GetParent(root.winfo_id()) or root.winfo_id()
                self._apply_windows_clickthrough(hwnd)

            self._ready_event.set()
            visible_state = True

            def _render_tick() -> None:
                nonlocal visible_state
                now = time.monotonic()
                with self._lock:
                    if self._highlight_only_until > 0 and now >= self._highlight_only_until and not self._highlights:
                        self._active = False
                        self._highlight_only_until = 0.0
                    active = self._active
                    stealth = self._stealth_for_capture
                    label = self._label
                    sub_label = self._sub_label
                    mode = self._mode
                    highlight_only = self._highlight_only_until > now
                    # Alte Trail-Punkte (> 0.65s), Ripples und abgelaufene Highlights entfernen
                    self._trail = [pt for pt in self._trail if (now - pt[2]) <= 0.65]
                    self._ripples = [rp for rp in self._ripples if (now - rp["start"]) <= rp["duration"]]
                    self._highlights = [hl for hl in self._highlights if (now - hl["start"]) <= hl["duration"]]
                    trail_snap = list(self._trail)
                    ripples_snap = list(self._ripples)
                    highlights_snap = list(self._highlights)

                canvas.delete("all")

                if not active or stealth:
                    if visible_state and not active:
                        root.withdraw()
                        visible_state = False
                    root.after(24, _render_tick)
                    return

                if not visible_state:
                    root.deiconify()
                    if platform.system() == "Windows":
                        hwnd_now = ctypes.windll.user32.GetParent(root.winfo_id()) or root.winfo_id()
                        self._apply_windows_clickthrough(hwnd_now)
                    visible_state = True

                # 0. Multi-Monitor Region-Highlight-Rahmen (falls aktiv)
                for hl in highlights_snap:
                    hx1 = hl["x"] - vx
                    hy1 = hl["y"] - vy
                    hx2 = hx1 + hl["w"]
                    hy2 = hy1 + hl["h"]
                    col = hl["color"]
                    canvas.create_rectangle(hx1 + 3, hy1 + 3, hx2 - 3, hy2 - 3, outline=col, width=4)
                    # Eck-Winkel in Eis-Cyan
                    c_len = min(38, max(16, hl["w"] // 10))
                    for (px, py, dx, dy) in ((hx1 + 3, hy1 + 3, 1, 1), (hx2 - 3, hy1 + 3, -1, 1), (hx1 + 3, hy2 - 3, 1, -1), (hx2 - 3, hy2 - 3, -1, -1)):
                        canvas.create_line(px, py, px + dx * c_len, py, fill="#38bdf8", width=4)
                        canvas.create_line(px, py, px, py + dy * c_len, fill="#38bdf8", width=4)
                    lbl_txt = hl["label"]
                    lw = max(160, len(lbl_txt) * 8 + 22)
                    canvas.create_rectangle(hx1 + 10, hy1 + 10, hx1 + 10 + lw, hy1 + 34, fill="#07080b", outline=col, width=2)
                    canvas.create_text(hx1 + 20, hy1 + 22, text=lbl_txt, anchor="w", fill="#f4f7fb", font=("Consolas", 10, "bold"))

                if highlight_only and not ripples_snap:
                    root.after(32, _render_tick)
                    return

                raw_mx, raw_my = get_cursor_pos()
                self.record_point(raw_mx, raw_my, mode=mode)
                mx, my = raw_mx - vx, raw_my - vy

                # 1. Nachleuchtende Motion-Trail-Linie hinter dem Zeiger
                if len(trail_snap) >= 2:
                    for i in range(1, len(trail_snap)):
                        x0, y0, _, _ = trail_snap[i - 1]
                        x1, y1, t1, pt_mode = trail_snap[i]
                        x0, y0, x1, y1 = x0 - vx, y0 - vy, x1 - vx, y1 - vy
                        age = max(0.0, now - t1)
                        life = max(0.0, 1.0 - (age / 0.65))
                        if life <= 0.05:
                            continue
                        if pt_mode == "swipe":
                            col = "#ffbe3d" if life > 0.55 else ("#ff7b29" if life > 0.25 else "#8a3c0e")
                            width = max(2, int(7 * life))
                        else:
                            col = "#00f0ff" if life > 0.6 else ("#00a8cc" if life > 0.3 else "#06546b")
                            width = max(1, int(4 * life))
                        canvas.create_line(
                            x0,
                            y0,
                            x1,
                            y1,
                            fill=col,
                            width=width,
                            capstyle="round",
                        )

                # 2. Aufblitzende Klick-Ripple-Wellen
                for rp in ripples_snap:
                    progress = min(1.0, max(0.0, (now - rp["start"]) / rp["duration"]))
                    r_outer = 10 + int(62 * progress)
                    r_inner = 6 + int(38 * progress)
                    w_outer = max(1, int(4 * (1.0 - progress)))
                    w_inner = max(1, int(3 * (1.0 - progress)))
                    rx, ry = rp["x"] - vx, rp["y"] - vy
                    canvas.create_oval(
                        rx - r_outer,
                        ry - r_outer,
                        rx + r_outer,
                        ry + r_outer,
                        outline=rp["color"],
                        width=w_outer,
                    )
                    canvas.create_oval(
                        rx - r_inner,
                        ry - r_inner,
                        rx + r_inner,
                        ry + r_inner,
                        outline=rp["secondary"],
                        width=w_inner,
                    )
                    if progress < 0.45:
                        cross = 14
                        canvas.create_line(rx - cross, ry, rx + cross, ry, fill="#ffffff", width=2)
                        canvas.create_line(rx, ry - cross, rx, ry + cross, fill="#ffffff", width=2)

                # 3. Sci-Fi HUD Target-Ring (Mehrschichtig rotierend) um die Mausposition
                self._angle_outer = (self._angle_outer + 5.5) % 360.0
                self._angle_inner = (self._angle_inner - 8.0) % 360.0

                primary_col = "#ff9d2e" if mode in ("click", "swipe") else "#00f0ff"
                secondary_col = "#00f0ff" if mode in ("click", "swipe") else "#ff7b29"

                # Äußerer taktischer Kompass-Ring (4 Segmente)
                r1 = 29
                for base_ang in (0, 90, 180, 270):
                    start_a = (base_ang + self._angle_outer) % 360
                    canvas.create_arc(
                        mx - r1,
                        my - r1,
                        mx + r1,
                        my + r1,
                        start=start_a,
                        extent=54,
                        style="arc",
                        outline=primary_col,
                        width=2,
                    )

                # Innerer gegenläufiger Arc-Ring
                r2 = 18
                for base_ang in (45, 165, 285):
                    start_b = (base_ang + self._angle_inner) % 360
                    canvas.create_arc(
                        mx - r2,
                        my - r2,
                        mx + r2,
                        my + r2,
                        start=start_b,
                        extent=72,
                        style="arc",
                        outline=secondary_col,
                        width=2,
                    )

                # Fadenkreuz-Ticks (lassen das exakte Zentrum frei für freie Sicht & Klicks)
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    canvas.create_line(
                        mx + dx * 7,
                        my + dy * 7,
                        mx + dx * 14,
                        my + dy * 14,
                        fill="#ffffff",
                        width=1,
                    )

                # 4. Kompaktes Sci-Fi HUD-Label neben dem Cursor
                text_main = (label or "JARVIS // GHOST-CURSOR")[:52]
                box_w = max(210, min(390, len(text_main) * 8 + 36))
                box_h = 44

                # Automatisch nach links/unten spiegeln, falls nahe am Bildschirmrand
                bx = mx + 36 if (mx + 36 + box_w) < (sw - 12) else max(12, mx - 36 - box_w)
                by = my - 24 if (my - 24) > 12 else min(sh - box_h - 12, my + 24)

                # Verbindungs-Linie vom Zielring zum HUD-Tag
                anchor_x = bx if bx > mx else bx + box_w
                canvas.create_line(
                    mx + (22 if bx > mx else -22),
                    my - 8,
                    anchor_x,
                    by + 14,
                    fill=primary_col,
                    width=2,
                )

                # HUD-Box Hintergrund + Rahmen + Akzent-Balken
                canvas.create_rectangle(
                    bx,
                    by,
                    bx + box_w,
                    by + box_h,
                    fill="#060b14",
                    outline=primary_col,
                    width=2,
                )
                canvas.create_rectangle(
                    bx,
                    by,
                    bx + 5,
                    by + box_h,
                    fill=secondary_col,
                    outline=secondary_col,
                )
                canvas.create_text(
                    bx + 13,
                    by + 14,
                    text=text_main,
                    anchor="w",
                    fill="#f8fafc",
                    font=("Consolas", 10, "bold"),
                )
                canvas.create_text(
                    bx + 13,
                    by + 31,
                    text=sub_label,
                    anchor="w",
                    fill="#38bdf8",
                    font=("Consolas", 8),
                )

                root.after(16, _render_tick)

            root.after(16, _render_tick)
            root.mainloop()
        except Exception as exc:
            print(f"[Ghost-Cursor Overlay-Hinweis] {exc}")
            self._ready_event.set()


# Globale Singleton-Instanz
GHOST_OVERLAY = GhostCursorOverlay()


def smooth_move_cursor(
    x: int,
    y: int,
    duration: float = 0.7,
    label: str = "",
    mode: str = "move",
) -> tuple[int, int]:
    """
    Bewegt den Mauszeiger flüssig und menschlich mit easeInOutQuad zum Ziel (x, y),
    zeichnet die Leuchtspur im Ghost-Cursor-Overlay und prüft bei jedem Schritt den Fail-Safe.
    """
    sw, sh = pyautogui.size()
    target_x = max(3, min(int(x), sw - 4))
    target_y = max(3, min(int(y), sh - 4))

    start_x, start_y = get_cursor_pos()
    dist = math.hypot(target_x - start_x, target_y - start_y)

    # Dynamische Mindestdauer für gut sichtbare, natürliche Bewegung
    eff_duration = max(0.28, float(duration))
    if dist < 25:
        eff_duration = 0.18
    elif dist > 900:
        eff_duration = max(eff_duration, 0.85)

    GHOST_OVERLAY.set_status(label or f"Gleite zu ({target_x}, {target_y})", mode=mode)
    check_user_interrupt()

    # Wir führen die Bewegung mit pyautogui.easeInOutQuad in 60-Hz-Schritten aus,
    # damit die Leuchtspur exakt synchronisiert ist und ESC / manuelles Eingreifen sofort greift.
    steps = max(12, int(eff_duration * 60))
    step_dt = eff_duration / steps
    last_expected = (start_x, start_y)

    for i in range(1, steps + 1):
        check_user_interrupt(expected_pos=last_expected, tolerance_px=220)
        t = i / steps
        eased = float(pyautogui.easeInOutQuad(t))
        nx = int(round(start_x + (target_x - start_x) * eased))
        ny = int(round(start_y + (target_y - start_y) * eased))
        pyautogui.moveTo(nx, ny, duration=0.0, tween=pyautogui.easeInOutQuad, _pause=False)
        GHOST_OVERLAY.record_point(nx, ny, mode=mode)
        last_expected = (nx, ny)
        time.sleep(step_dt)

    pyautogui.moveTo(target_x, target_y, duration=0.0, tween=pyautogui.easeInOutQuad, _pause=False)
    GHOST_OVERLAY.record_point(target_x, target_y, mode=mode)
    return target_x, target_y


def ghost_click(
    x: Optional[int] = None,
    y: Optional[int] = None,
    button: str = "left",
    clicks: int = 1,
    duration: float = 0.7,
    label: str = "",
) -> str:
    """Fährt flüssig zur Zielkoordinate, zeigt die Klick-Ripple-Animation und klickt."""
    if x is None or y is None:
        cx, cy = get_cursor_pos()
    else:
        cx, cy = smooth_move_cursor(x, y, duration=duration, label=label or f"Ziel: ({x}, {y})", mode="move")

    check_user_interrupt(expected_pos=(cx, cy), tolerance_px=220)
    click_label = label or f"Klicke ({cx}, {cy})"
    GHOST_OVERLAY.add_ripple(cx, cy, label=click_label)
    time.sleep(0.08)

    if button == "right":
        pyautogui.rightClick(cx, cy)
    elif clicks >= 2:
        pyautogui.doubleClick(cx, cy)
    else:
        pyautogui.click(cx, cy, clicks=clicks, button=button)

    time.sleep(0.32)
    return f"Klick auf ({cx}, {cy}) [{click_label}]"


def ghost_drag(
    start_x: int,
    start_y: int,
    end_x: int,
    end_y: int,
    duration: float = 0.75,
    label: str = "",
) -> str:
    """Fährt sichtbar zum Startpunkt, hält die Maustaste und zieht mit leuchtender Spur zum Zielpunkt."""
    sx, sy = smooth_move_cursor(
        start_x,
        start_y,
        duration=0.65,
        label=label or f"Greife Element ({start_x}, {start_y})",
        mode="move",
    )
    GHOST_OVERLAY.add_ripple(sx, sy, color="#00f0ff", secondary_color="#ff9d2e", label=label or "Halte gedrückt...")
    pyautogui.mouseDown(sx, sy, button="left")
    try:
        ex, ey = smooth_move_cursor(
            end_x,
            end_y,
            duration=max(0.45, duration),
            label=label or f"Ziehe nach ({end_x}, {end_y})...",
            mode="swipe",
        )
        GHOST_OVERLAY.add_ripple(ex, ey, color="#ff9d2e", secondary_color="#00f0ff", label="Losgelassen")
    finally:
        pyautogui.mouseUp(button="left")
    time.sleep(0.28)
    return f"Drag-and-Drop von ({sx}, {sy}) nach ({end_x}, {end_y})"


def ghost_swipe_scroll(
    amount: int = -400,
    x: Optional[int] = None,
    y: Optional[int] = None,
    direction: str = "",
    label: str = "",
) -> str:
    """
    Führt eine sichtbare Wisch-/Scroll-Bewegung mit leuchtender Spur (Motion-Trail) aus:
    Der Zeiger gleitet zur Zielstelle und vollführt eine fließende Swipe-Geste, während er scrollt.
    """
    sw, sh = pyautogui.size()
    cx = int(x) if (x and int(x) > 0) else sw // 2
    cy = int(y) if (y and int(y) > 0) else int(sh * 0.54)

    dir_low = direction.strip().lower()
    if not dir_low:
        dir_low = "down" if amount < 0 else "up"

    if dir_low in ("down", "runter", "unten"):
        action_text = label or "Wische nach unten..."
        start_pt = (cx, max(120, min(sh - 160, cy + 75)))
        end_pt = (cx, max(120, min(sh - 160, cy - 85)))
        scroll_total = -abs(int(amount or -420))
        horizontal = False
    elif dir_low in ("up", "hoch", "oben"):
        action_text = label or "Wische nach oben..."
        start_pt = (cx, max(120, min(sh - 160, cy - 75)))
        end_pt = (cx, max(120, min(sh - 160, cy + 85)))
        scroll_total = abs(int(amount or 420))
        horizontal = False
    elif dir_low in ("right", "rechts"):
        action_text = label or "Wische nach rechts..."
        start_pt = (max(120, min(sw - 160, cx + 95)), cy)
        end_pt = (max(120, min(sw - 160, cx - 95)), cy)
        scroll_total = -abs(int(amount or -400))
        horizontal = True
    else:
        action_text = label or "Wische nach links..."
        start_pt = (max(120, min(sw - 160, cx - 95)), cy)
        end_pt = (max(120, min(sw - 160, cx + 95)), cy)
        scroll_total = abs(int(amount or 400))
        horizontal = True

    # 1. Sichtbar zum Startpunkt der Wisch-Geste gleiten
    sx, sy = smooth_move_cursor(start_pt[0], start_pt[1], duration=0.55, label=action_text, mode="move")
    GHOST_OVERLAY.add_ripple(sx, sy, color="#00f0ff", secondary_color="#ffbe3d", label=action_text)

    # 2. Fließende Wisch-Bewegung mit Swipe-Leuchtspur + synchronem Scroll-Impuls
    steps = 16
    swipe_duration = 0.52
    step_dt = swipe_duration / steps
    per_step_scroll = int(round(scroll_total / steps)) or (-25 if scroll_total < 0 else 25)
    last_expected = (sx, sy)

    for i in range(1, steps + 1):
        check_user_interrupt(expected_pos=last_expected, tolerance_px=160)
        t = i / steps
        eased = float(pyautogui.easeInOutQuad(t))
        nx = int(round(sx + (end_pt[0] - sx) * eased))
        ny = int(round(sy + (end_pt[1] - sy) * eased))
        pyautogui.moveTo(nx, ny, duration=0.0, tween=pyautogui.easeInOutQuad, _pause=False)
        GHOST_OVERLAY.record_point(nx, ny, mode="swipe")
        if horizontal and hasattr(pyautogui, "hscroll"):
            pyautogui.hscroll(per_step_scroll, x=nx, y=ny, _pause=False)
        else:
            pyautogui.scroll(per_step_scroll, x=nx, y=ny, _pause=False)
        last_expected = (nx, ny)
        time.sleep(step_dt)

    time.sleep(0.22)
    return f"Visuelle Wisch-/Scroll-Bewegung ausgeführt ({action_text}, Delta={scroll_total})"


# ============================================================
# Intelligente Windows UI- & Taskleisten-Koordinaten-Erkennung
# ============================================================

_UIA_PS_SCRIPT = r"""
param(
    [string]$TargetName = "",
    [int]$TaskbarOnly = 0
)
$ErrorActionPreference = "SilentlyContinue"
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes

$root = [System.Windows.Automation.AutomationElement]::RootElement
if (-not $root) {
    Write-Output '{"found":false}'
    exit 0
}

$q = $TargetName.Trim().ToLowerInvariant()
$best = $null
$bestScore = -1

function Score-Element($el, [string]$query, [bool]$isTaskbar) {
    try {
        $rect = $el.Current.BoundingRectangle
        if ($rect.IsEmpty -or $rect.Width -lt 8 -or $rect.Height -lt 8 -or [double]::IsInfinity($rect.Left)) {
            return -1
        }
        $name = [string]$el.Current.Name
        $aid  = [string]$el.Current.AutomationId
        $cls  = [string]$el.Current.ClassName
        $help = [string]$el.Current.HelpText
        $hay  = ("$name $aid $help").ToLowerInvariant()
        if ([string]::IsNullOrWhiteSpace($hay)) { return -1 }

        $score = 0
        $nLow = $name.Trim().ToLowerInvariant()
        if ($nLow -eq $query) { $score = 120 }
        elseif ($nLow.StartsWith($query)) { $score = 95 }
        elseif ($nLow.Contains($query)) { $score = 80 }
        elseif ($hay.Contains($query)) { $score = 65 }
        else { return -1 }

        if ($isTaskbar) { $score += 20 }
        return $score
    } catch {
        return -1
    }
}

$condTypes = New-Object System.Windows.Automation.OrCondition(
    (New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ControlTypeProperty, [System.Windows.Automation.ControlType]::Button)),
    (New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ControlTypeProperty, [System.Windows.Automation.ControlType]::TabItem)),
    (New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ControlTypeProperty, [System.Windows.Automation.ControlType]::MenuItem)),
    (New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ControlTypeProperty, [System.Windows.Automation.ControlType]::ListItem)),
    (New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ControlTypeProperty, [System.Windows.Automation.ControlType]::Hyperlink)),
    (New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ControlTypeProperty, [System.Windows.Automation.ControlType]::Edit))
)

# 1. Taskleiste (Shell_TrayWnd) prüfen
$trayCond = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ClassNameProperty, "Shell_TrayWnd")
$tray = $root.FindFirst([System.Windows.Automation.TreeScope]::Children, $trayCond)
if ($tray) {
    $items = $tray.FindAll([System.Windows.Automation.TreeScope]::Descendants, $condTypes)
    foreach ($el in $items) {
        $sc = Score-Element $el $q $true
        if ($sc -gt $bestScore) {
            $bestScore = $sc
            $best = $el
        }
    }
}

# 2. Offene Anwendungsfenster prüfen (falls nicht nur Taskleiste gewünscht)
if ($TaskbarOnly -eq 0 -and $bestScore -lt 100) {
    $winCond = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ControlTypeProperty, [System.Windows.Automation.ControlType]::Window)
    $windows = $root.FindAll([System.Windows.Automation.TreeScope]::Children, $winCond)
    $count = 0
    foreach ($w in $windows) {
        if ($count -ge 8) { break }
        $wName = ([string]$w.Current.Name).ToLowerInvariant()
        if ($wName.Contains("jarvis // command center")) { continue }
        $count++
        $els = $w.FindAll([System.Windows.Automation.TreeScope]::Descendants, $condTypes)
        foreach ($el in $els) {
            $sc = Score-Element $el $q $false
            if ($sc -gt $bestScore) {
                $bestScore = $sc
                $best = $el
            }
        }
    }
}

if ($best -ne $null -and $bestScore -gt 0) {
    $r = $best.Current.BoundingRectangle
    $cx = [int][Math]::Round($r.Left + ($r.Width / 2.0))
    $cy = [int][Math]::Round($r.Top + ($r.Height / 2.0))
    $out = @{
        found = $true
        x = $cx
        y = $cy
        width = [int]$r.Width
        height = [int]$r.Height
        name = [string]$best.Current.Name
        score = $bestScore
        source = "uiautomation"
    }
    $out | ConvertTo-Json -Compress
} else {
    Write-Output '{"found":false}'
}
"""


def find_ui_element_screen_coords(
    target_name: str,
    taskbar_only: bool = False,
    timeout_s: float = 3.5,
) -> Optional[dict[str, Any]]:
    """
    Ermittelt per Windows UIAutomation (oder Linux xdotool/wmctrl) die exakte Bildschirm-Koordinate (X/Y)
    eines Icons in der Taskleiste oder eines Buttons/Menüpunkts im offenen Anwendungsfenster.
    """
    cleaned = re.sub(r'["\'`$]', "", (target_name or "").strip())
    if not cleaned:
        return None

    if platform.system() != "Windows":
        return None

    ps_exe = shutil.which("powershell") or shutil.which("pwsh")
    if not ps_exe:
        return None

    try:
        proc = subprocess.run(
            [
                ps_exe,
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                _UIA_PS_SCRIPT,
                "-TargetName",
                cleaned,
                "-TaskbarOnly",
                "1" if taskbar_only else "0",
            ],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        raw = (proc.stdout or "").strip()
        if not raw:
            return None
        lines = [ln.strip() for ln in raw.splitlines() if ln.strip().startswith("{")]
        if not lines:
            return None
        data = json.loads(lines[-1])
        if isinstance(data, dict) and data.get("found") and int(data.get("x", 0)) > 0 and int(data.get("y", 0)) > 0:
            return data
    except Exception:
        pass
    return None


def find_windows_start_or_search_coords() -> tuple[int, int, str]:
    """Ermittelt die Koordinate des Windows-Suche- oder Start-Buttons in der Taskleiste."""
    for candidate in ("Suche", "Suchen", "Search", "Start"):
        hit = find_ui_element_screen_coords(candidate, taskbar_only=True, timeout_s=2.5)
        if hit:
            return int(hit["x"]), int(hit["y"]), str(hit.get("name") or candidate)

    sw, sh = pyautogui.size()
    # Windows 11 zentrierte Taskleiste / Startbutton Fallback
    return int(sw * 0.42), max(10, sh - 24), "Windows-Taskleiste (Start/Suche)"
