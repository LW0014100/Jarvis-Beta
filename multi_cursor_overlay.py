"""Transparent 60-FPS 4-Cursor Sci-Fi Overlay for JARVIS Multi-Agent Swarm."""

from __future__ import annotations

import ctypes
import math
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any


AGENT_CURSOR_SPECS: dict[int, dict[str, str]] = {
    1: {
        "slot": "1",
        "key": "overmind",
        "role": "OVERMIND",
        "badge": "[A1: OVERMIND]",
        "color": "#ff7b29",
        "glow": "#ffb27a",
    },
    2: {
        "slot": "2",
        "key": "desktop_video",
        "role": "CREATIVE / VIDEO",
        "badge": "[A2: CUTTING]",
        "color": "#00f0ff",
        "glow": "#8cf8ff",
    },
    3: {
        "slot": "3",
        "key": "web_ghost",
        "role": "WEB GHOST",
        "badge": "[A3: BROWSER]",
        "color": "#b829ff",
        "glow": "#e0a3ff",
    },
    4: {
        "slot": "4",
        "key": "finance_quant",
        "role": "DATA / QUANT",
        "badge": "[A4: DATA]",
        "color": "#00ff66",
        "glow": "#9effc6",
    },
}

_AGENT_ALIASES: dict[str, int] = {
    "1": 1,
    "a1": 1,
    "overmind": 1,
    "agent_1": 1,
    "coordinator": 1,
    "2": 2,
    "a2": 2,
    "desktop_video": 2,
    "desktop": 2,
    "video": 2,
    "cutting": 2,
    "creative": 2,
    "agent_2": 2,
    "3": 3,
    "a3": 3,
    "web_ghost": 3,
    "web": 3,
    "browser": 3,
    "ghost": 3,
    "agent_3": 3,
    "4": 4,
    "a4": 4,
    "finance_quant": 4,
    "finance": 4,
    "quant": 4,
    "data": 4,
    "files": 4,
    "agent_4": 4,
}


def resolve_agent_slot(agent_id: int | str) -> int:
    """Resolve any numeric or string agent identifier to slot 1..4."""
    if isinstance(agent_id, int) and agent_id in AGENT_CURSOR_SPECS:
        return agent_id
    raw = str(agent_id or "").strip().lower()
    if raw in _AGENT_ALIASES:
        return _AGENT_ALIASES[raw]
    for prefix, slot in _AGENT_ALIASES.items():
        if prefix in raw:
            return slot
    return 1


def _clamp_norm(val: float) -> float:
    return max(0.0, min(1000.0, float(val)))


def _get_screen_size() -> tuple[int, int]:
    if sys.platform == "win32":
        try:
            user32 = ctypes.windll.user32
            w = int(user32.GetSystemMetrics(0))
            h = int(user32.GetSystemMetrics(1))
            if w > 0 and h > 0:
                return w, h
        except Exception:
            pass
    return 1920, 1080


@dataclass
class VirtualCursorState:
    """Mutable RAM state for one virtual AI cursor."""

    slot: int
    key: str
    role: str
    badge: str
    color: str
    glow: str
    visible: bool = False
    x_norm: float = 500.0
    y_norm: float = 500.0
    target_x_norm: float = 500.0
    target_y_norm: float = 500.0
    start_x_norm: float = 500.0
    start_y_norm: float = 500.0
    glide_elapsed: float = 0.0
    glide_duration: float = 0.0
    active: bool = False
    last_updated: float = 0.0


@dataclass
class ClickRipple:
    """Animated click ripple wave for a virtual cursor."""

    slot: int
    x_norm: float
    y_norm: float
    color: str
    created_at: float
    duration: float = 0.42
    double: bool = False


class MultiCursorOverlay:
    """Lightweight transparent 60-FPS click-through Windows overlay for 4 AI cursors."""

    TRANSPARENT_KEY = "#010203"
    FRAME_MS = 16  # ~60 FPS
    AUTO_HIDE_IDLE_SEC = 18.0

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._enabled: bool = True
        self._running: bool = False
        self._tk_ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._headless_fallback: bool = False
        self._last_tick_ts: float = time.perf_counter()

        defaults = {
            1: (220.0, 220.0),
            2: (760.0, 240.0),
            3: (240.0, 740.0),
            4: (760.0, 740.0),
        }
        self._cursors: dict[int, VirtualCursorState] = {}
        for slot, spec in AGENT_CURSOR_SPECS.items():
            dx, dy = defaults[slot]
            self._cursors[slot] = VirtualCursorState(
                slot=slot,
                key=spec["key"],
                role=spec["role"],
                badge=spec["badge"],
                color=spec["color"],
                glow=spec["glow"],
                visible=False,
                x_norm=dx,
                y_norm=dy,
                target_x_norm=dx,
                target_y_norm=dy,
                start_x_norm=dx,
                start_y_norm=dy,
            )
        self._ripples: list[ClickRipple] = []
        self._status_pill_text: str = ""
        self._status_pill_visible: bool = False
        self._status_pill_updated_at: float = 0.0

        # Tk GUI thread objects (only touched on the dedicated GUI thread)
        self._root: Any = None
        self._canvas: Any = None
        self._screen_w: int = 1920
        self._screen_h: int = 1080
        self._is_mapped: bool = False

    def is_enabled(self) -> bool:
        with self._lock:
            return self._enabled

    def set_enabled(self, enabled: bool) -> bool:
        want = bool(enabled)
        with self._lock:
            self._enabled = want
            if not want:
                for cur in self._cursors.values():
                    cur.visible = False
                    cur.active = False
                self._ripples.clear()
        if want:
            self._ensure_gui_started()
        return want

    def _can_spawn_tk(self) -> bool:
        if os.getenv("JARVIS_DISABLE_OVERLAY", "").strip() == "1":
            return False
        if sys.platform != "win32" and not os.getenv("DISPLAY"):
            return False
        return True

    def _ensure_gui_started(self) -> None:
        with self._lock:
            if not self._enabled or self._headless_fallback:
                return
            if self._thread is not None and self._thread.is_alive():
                return
            if not self._can_spawn_tk():
                self._headless_fallback = True
                return
            self._running = True
            self._tk_ready.clear()
            self._thread = threading.Thread(
                target=self._gui_mainloop,
                name="JarvisMultiCursorOverlay",
                daemon=True,
            )
            self._thread.start()
        self._tk_ready.wait(timeout=1.5)

    def _apply_click_through_win32(self, root: Any) -> None:
        if sys.platform != "win32":
            return
        try:
            user32 = ctypes.windll.user32
            hwnd = int(root.winfo_id())
            parent_hwnd = int(user32.GetParent(hwnd) or hwnd)
            target_hwnd = parent_hwnd if parent_hwnd else hwnd

            GWL_EXSTYLE = -20
            WS_EX_TRANSPARENT = 0x00000020
            WS_EX_LAYERED = 0x00080000
            WS_EX_TOPMOST = 0x00000008
            WS_EX_NOACTIVATE = 0x08000000
            WS_EX_TOOLWINDOW = 0x00000080

            HWND_TOPMOST = -1
            SWP_NOMOVE = 0x0002
            SWP_NOSIZE = 0x0001
            SWP_NOACTIVATE = 0x0010
            SWP_SHOWWINDOW = 0x0040

            for h in {hwnd, target_hwnd}:
                style = int(user32.GetWindowLongW(h, GWL_EXSTYLE))
                new_style = (
                    style
                    | WS_EX_LAYERED
                    | WS_EX_TRANSPARENT
                    | WS_EX_TOPMOST
                    | WS_EX_NOACTIVATE
                    | WS_EX_TOOLWINDOW
                )
                user32.SetWindowLongW(h, GWL_EXSTYLE, new_style)
                user32.SetWindowPos(
                    h,
                    HWND_TOPMOST,
                    0,
                    0,
                    0,
                    0,
                    SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW,
                )
            # Exclude virtual AI cursors from screen capture so agents see clean UI targets
            try:
                WDA_EXCLUDEFROMCAPTURE = 0x00000011
                user32.SetWindowDisplayAffinity(target_hwnd, WDA_EXCLUDEFROMCAPTURE)
            except Exception:
                pass
        except Exception:
            pass

    def _gui_mainloop(self) -> None:
        try:
            import tkinter as tk
        except Exception:
            with self._lock:
                self._headless_fallback = True
                self._running = False
            self._tk_ready.set()
            return

        root = None
        try:
            root = tk.Tk()
            self._screen_w, self._screen_h = _get_screen_size()
            root.withdraw()
            root.overrideredirect(True)
            root.attributes("-topmost", True)
            try:
                root.attributes("-transparentcolor", self.TRANSPARENT_KEY)
            except Exception:
                pass
            root.configure(bg=self.TRANSPARENT_KEY)
            root.geometry(f"{self._screen_w}x{self._screen_h}+0+0")

            canvas = tk.Canvas(
                root,
                width=self._screen_w,
                height=self._screen_h,
                bg=self.TRANSPARENT_KEY,
                highlightthickness=0,
                bd=0,
            )
            canvas.pack(fill="both", expand=True)
            root.update_idletasks()

            self._root = root
            self._canvas = canvas
            self._is_mapped = False
            self._apply_click_through_win32(root)
            self._tk_ready.set()
            root.after(self.FRAME_MS, self._render_frame)
            root.mainloop()
        except Exception:
            with self._lock:
                self._headless_fallback = True
                self._running = False
            self._tk_ready.set()
        finally:
            try:
                if root is not None:
                    root.destroy()
            except Exception:
                pass
            self._root = None
            self._canvas = None
            self._is_mapped = False

    def activate_agent(
        self,
        agent_id: int | str,
        x_norm: float | None = None,
        y_norm: float | None = None,
        badge: str | None = None,
    ) -> None:
        slot = resolve_agent_slot(agent_id)
        now = time.perf_counter()
        with self._lock:
            cur = self._cursors[slot]
            if not self._enabled:
                cur.visible = False
                cur.active = False
                return
            if x_norm is not None and y_norm is not None:
                cx = _clamp_norm(x_norm)
                cy = _clamp_norm(y_norm)
                cur.x_norm = cx
                cur.y_norm = cy
                cur.start_x_norm = cx
                cur.start_y_norm = cy
                cur.target_x_norm = cx
                cur.target_y_norm = cy
                cur.glide_duration = 0.0
                cur.glide_elapsed = 0.0
            if badge:
                cur.badge = str(badge)[:28]
            else:
                cur.badge = AGENT_CURSOR_SPECS[slot]["badge"]
            cur.visible = True
            cur.active = True
            cur.last_updated = now
        self._ensure_gui_started()

    def deactivate_agent(self, agent_id: int | str) -> None:
        slot = resolve_agent_slot(agent_id)
        with self._lock:
            cur = self._cursors[slot]
            cur.visible = False
            cur.active = False
            cur.glide_duration = 0.0

    def move_cursor(
        self,
        agent_id: int | str,
        x_norm: float,
        y_norm: float,
        badge: str | None = None,
        active: bool = True,
        duration: float = 0.14,
    ) -> None:
        """Move one agent cursor independently in 0..1000 coordinates."""
        slot = resolve_agent_slot(agent_id)
        nx = _clamp_norm(x_norm)
        ny = _clamp_norm(y_norm)
        now = time.perf_counter()
        with self._lock:
            cur = self._cursors[slot]
            if not self._enabled:
                cur.visible = False
                cur.active = False
                return
            if badge:
                cur.badge = str(badge)[:28]
            cur.start_x_norm = cur.x_norm
            cur.start_y_norm = cur.y_norm
            cur.target_x_norm = nx
            cur.target_y_norm = ny
            if duration <= 0.005:
                cur.x_norm = nx
                cur.y_norm = ny
                cur.glide_duration = 0.0
                cur.glide_elapsed = 0.0
            else:
                cur.glide_duration = float(duration)
                cur.glide_elapsed = 0.0
            cur.visible = True
            cur.active = bool(active)
            cur.last_updated = now
        self._ensure_gui_started()

    def move_cursor_smooth(
        self,
        agent_id: int | str,
        x_norm: float,
        y_norm: float,
        badge: str | None = None,
        duration: float = 0.22,
        wait: bool = False,
    ) -> None:
        """Initiate smooth ease-out glide on the overlay without blocking other cursors."""
        dur = max(0.05, min(0.8, float(duration)))
        self.move_cursor(
            agent_id,
            x_norm,
            y_norm,
            badge=badge,
            active=True,
            duration=dur,
        )
        if wait:
            end_ts = time.perf_counter() + dur
            while time.perf_counter() < end_ts:
                with self._lock:
                    if not self._enabled or not self._cursors[resolve_agent_slot(agent_id)].visible:
                        break
                self.tick_physics(0.016)
                time.sleep(0.014)

    def trigger_click(
        self,
        agent_id: int | str,
        x_norm: float | None = None,
        y_norm: float | None = None,
        double: bool = False,
        badge: str | None = None,
    ) -> None:
        """Spawn a glowing click ripple ring on the virtual cursor's target."""
        slot = resolve_agent_slot(agent_id)
        now = time.perf_counter()
        with self._lock:
            if not self._enabled:
                return
            cur = self._cursors[slot]
            if x_norm is not None and y_norm is not None:
                cx = _clamp_norm(x_norm)
                cy = _clamp_norm(y_norm)
                cur.x_norm = cx
                cur.y_norm = cy
                cur.target_x_norm = cx
                cur.target_y_norm = cy
                cur.glide_duration = 0.0
            if badge:
                cur.badge = str(badge)[:28]
            cur.visible = True
            cur.active = True
            cur.last_updated = now
            self._ripples.append(
                ClickRipple(
                    slot=slot,
                    x_norm=cur.x_norm,
                    y_norm=cur.y_norm,
                    color=cur.color,
                    created_at=now,
                    duration=0.45 if double else 0.36,
                    double=bool(double),
                )
            )
            if len(self._ripples) > 24:
                self._ripples = self._ripples[-24:]
        self._ensure_gui_started()

    def stop_all(self) -> None:
        """Instant 1-ms emergency stop: hides all 4 cursors, status pill and clears animations."""
        with self._lock:
            for slot, cur in self._cursors.items():
                cur.visible = False
                cur.active = False
                cur.glide_duration = 0.0
                cur.glide_elapsed = 0.0
                cur.badge = AGENT_CURSOR_SPECS[slot]["badge"]
            self._ripples.clear()
            self._status_pill_visible = False
            self._status_pill_text = ""

    def set_status_pill(self, text: str, visible: bool = True) -> None:
        """Zeigt die schmale, durchklickbare Sci-Fi-Status-Pille oben mittig am Bildschirmrand."""
        clean = " ".join(str(text or "").split())[:130]
        now = time.perf_counter()
        want_show = bool(visible and clean)
        with self._lock:
            if not self._enabled:
                self._status_pill_visible = False
                self._status_pill_text = ""
                return
            self._status_pill_text = clean
            self._status_pill_visible = want_show
            self._status_pill_updated_at = now
        if want_show:
            self._ensure_gui_started()

    def clear_status_pill(self) -> None:
        """Blendet die Status-Pille am oberen Bildschirmrand sofort wieder aus."""
        with self._lock:
            self._status_pill_visible = False
            self._status_pill_text = ""

    def tick_physics(self, dt: float | None = None) -> None:
        """Advance smooth interpolation and expire old ripples/idle cursors."""
        now = time.perf_counter()
        with self._lock:
            if dt is None:
                dt = max(0.001, min(0.1, now - self._last_tick_ts))
            self._last_tick_ts = now

            for cur in self._cursors.values():
                if not cur.visible:
                    continue
                if cur.glide_duration > 0.0:
                    cur.glide_elapsed += dt
                    t = min(1.0, cur.glide_elapsed / cur.glide_duration)
                    # Cubic ease-out for crisp robotic glide
                    eased = 1.0 - (1.0 - t) ** 3
                    cur.x_norm = cur.start_x_norm + (cur.target_x_norm - cur.start_x_norm) * eased
                    cur.y_norm = cur.start_y_norm + (cur.target_y_norm - cur.start_y_norm) * eased
                    if t >= 1.0:
                        cur.x_norm = cur.target_x_norm
                        cur.y_norm = cur.target_y_norm
                        cur.glide_duration = 0.0
                if cur.last_updated > 0.0 and (now - cur.last_updated) > self.AUTO_HIDE_IDLE_SEC:
                    cur.visible = False
                    cur.active = False

            self._ripples = [
                rp for rp in self._ripples if (now - rp.created_at) < rp.duration
            ]
            if self._status_pill_visible and self._status_pill_updated_at > 0.0 and (now - self._status_pill_updated_at) > 45.0:
                self._status_pill_visible = False
                self._status_pill_text = ""

    def _render_frame(self) -> None:
        root = self._root
        canvas = self._canvas
        if root is None or canvas is None:
            return

        self.tick_physics()
        now = time.perf_counter()
        with self._lock:
            enabled = self._enabled
            visible_cursors = [
                VirtualCursorState(**vars(c))
                for c in self._cursors.values()
                if enabled and c.visible
            ]
            ripples = list(self._ripples) if enabled else []
            pill_visible = bool(enabled and self._status_pill_visible and self._status_pill_text)
            pill_text = self._status_pill_text

        try:
            canvas.delete("all")
            if not enabled or (not visible_cursors and not ripples and not pill_visible):
                if self._is_mapped:
                    root.withdraw()
                    self._is_mapped = False
                root.after(40, self._render_frame)
                return

            if not self._is_mapped:
                root.deiconify()
                self._apply_click_through_win32(root)
                self._is_mapped = True

            sw = max(1, self._screen_w)
            sh = max(1, self._screen_h)

            # 1. Expanding click ripples
            for rp in ripples:
                progress = max(0.0, min(1.0, (now - rp.created_at) / rp.duration))
                px = (rp.x_norm / 1000.0) * sw
                py = (rp.y_norm / 1000.0) * sh
                radius = 6.0 + progress * 34.0
                width = 3 if progress < 0.5 else 2
                canvas.create_oval(
                    px - radius,
                    py - radius,
                    px + radius,
                    py + radius,
                    outline=rp.color,
                    width=width,
                )
                if rp.double and progress > 0.18:
                    r2 = 4.0 + max(0.0, (progress - 0.18) / 0.82) * 24.0
                    canvas.create_oval(
                        px - r2,
                        py - r2,
                        px + r2,
                        py + r2,
                        outline="#ffffff",
                        width=1,
                    )

            # 2. Geklonte Windows-Standard-Mauszeiger (weiß mit schwarzem Rand) + Agenten-Farbpunkt (A1-A4)
            for cur in visible_cursors:
                px = max(4.0, min(sw - 6.0, (cur.x_norm / 1000.0) * sw))
                py = max(4.0, min(sh - 6.0, (cur.y_norm / 1000.0) * sh))

                # Dezenter Agenten-Ring an der Klickspitze
                pulse = 7.0 + 2.0 * math.sin(now * 6.5 + cur.slot)
                canvas.create_oval(
                    px - pulse,
                    py - pulse,
                    px + pulse,
                    py + pulse,
                    outline=cur.color,
                    width=1,
                )

                # Exakt geklonter Windows-Standard-Pfeil (weiß #ffffff mit schwarzem Rand #000000)
                arrow_pts = [
                    px, py,
                    px, py + 19,
                    px + 4.5, py + 14.5,
                    px + 8.5, py + 23,
                    px + 11.5, py + 21.5,
                    px + 7.5, py + 13,
                    px + 14, py + 13,
                ]
                canvas.create_polygon(
                    arrow_pts,
                    fill="#ffffff",
                    outline="#000000",
                    width=2,
                )

                # Kleiner farbiger Agenten-Dot (A1-A4) + kompaktes Neon-Tag
                badge_text = cur.badge or AGENT_CURSOR_SPECS[cur.slot]["badge"]
                dot_x = px + 16
                dot_y = py + 15
                canvas.create_oval(
                    dot_x - 5,
                    dot_y - 5,
                    dot_x + 5,
                    dot_y + 5,
                    fill=cur.color,
                    outline="#000000",
                    width=1,
                )
                canvas.create_text(
                    dot_x,
                    dot_y,
                    text=str(cur.slot),
                    fill="#07101c",
                    font=("Consolas", 7, "bold"),
                )

                bx = px + 24
                by = py + 16
                if bx + 150 > sw:
                    bx = max(6.0, px - 156)
                if by + 24 > sh:
                    by = max(6.0, py - 26)

                pad_w = max(84, len(badge_text) * 7 + 14)
                canvas.create_rectangle(
                    bx,
                    by,
                    bx + pad_w,
                    by + 20,
                    fill="#07101c",
                    outline=cur.color,
                    width=1,
                )
                canvas.create_text(
                    bx + 7,
                    by + 10,
                    anchor="w",
                    text=badge_text,
                    fill=cur.color,
                    font=("Consolas", 9, "bold"),
                )

            # 3. Transparente / schwebende Mini-Status-Pille oben mittig am Bildschirmrand
            if pill_visible and pill_text:
                pill_w = max(460, min(sw - 60, len(pill_text) * 8 + 56))
                pill_h = 34
                px0 = (sw - pill_w) / 2.0
                py0 = 14.0
                px1 = px0 + pill_w
                py1 = py0 + pill_h
                chamfer = 10.0
                pill_pts = [
                    px0 + chamfer, py0,
                    px1 - chamfer, py0,
                    px1, py0 + chamfer,
                    px1, py1 - chamfer,
                    px1 - chamfer, py1,
                    px0 + chamfer, py1,
                    px0, py1 - chamfer,
                    px0, py0 + chamfer,
                ]
                canvas.create_polygon(
                    pill_pts,
                    fill="#070b14",
                    outline="#00f0ff",
                    width=2,
                )
                # Orange/Cyan Akzent-Striche links & rechts + pulsierender Status-Punkt
                pulse_on = (int(now * 4.0) % 2) == 0
                dot_col = "#00f0ff" if pulse_on else "#ff7b29"
                canvas.create_line(px0 + 16, py0 + 2, px0 + 48, py0 + 2, fill="#ff7b29", width=2)
                canvas.create_line(px1 - 48, py1 - 2, px1 - 16, py1 - 2, fill="#ff7b29", width=2)
                canvas.create_oval(
                    px0 + 16,
                    py0 + 12,
                    px0 + 26,
                    py0 + 22,
                    fill=dot_col,
                    outline="#070b14",
                    width=1,
                )
                canvas.create_text(
                    px0 + 34,
                    py0 + pill_h / 2.0,
                    anchor="w",
                    text=pill_text,
                    fill="#e6fbff",
                    font=("Consolas", 10, "bold"),
                )

            root.after(self.FRAME_MS, self._render_frame)
        except Exception:
            try:
                root.after(50, self._render_frame)
            except Exception:
                pass

    def get_snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": self._enabled,
                "headless_fallback": self._headless_fallback,
                "status_pill_visible": self._status_pill_visible,
                "status_pill_text": self._status_pill_text,
                "cursors": {
                    str(slot): {
                        "slot": cur.slot,
                        "key": cur.key,
                        "role": cur.role,
                        "badge": cur.badge,
                        "color": cur.color,
                        "visible": cur.visible,
                        "active": cur.active,
                        "x_norm": round(cur.x_norm, 1),
                        "y_norm": round(cur.y_norm, 1),
                        "target_x_norm": round(cur.target_x_norm, 1),
                        "target_y_norm": round(cur.target_y_norm, 1),
                    }
                    for slot, cur in self._cursors.items()
                },
                "ripple_count": len(self._ripples),
            }


MULTI_CURSOR_OVERLAY = MultiCursorOverlay()
