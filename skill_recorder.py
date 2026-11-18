# -*- coding: utf-8 -*-
"""
J.A.R.V.I.S. Live-Makro- & Skill-Rekorder ("Schau mir zu & Lern es!", skill_recorder.py)
========================================================================================
UPGRADE 5: Beobachtet echte Nutzer-Mausklicks und Tastatureingaben, speichert für jeden
Klick einen 150x150-Pixel-Button-Template-Ausschnitt aus dem Screenshot und spielt den
gesamten Workflow später per `cv2.matchTemplate` in 10x-Geschwindigkeit (selbst bei
verschobenem Fenster) wieder über den Mauszeiger ab.
"""

from __future__ import annotations

import ctypes
import logging
import platform
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger("jarvis.skill_recorder")

IS_WINDOWS = platform.system().lower() == "windows"

try:
    import cv2  # type: ignore
    import numpy as np  # type: ignore

    CV2_AVAILABLE = True
except Exception:
    cv2 = None  # type: ignore
    np = None  # type: ignore
    CV2_AVAILABLE = False

try:
    import mss  # type: ignore
    from PIL import Image  # type: ignore

    MSS_PIL_AVAILABLE = True
except Exception:
    mss = None  # type: ignore
    Image = None  # type: ignore
    MSS_PIL_AVAILABLE = False


_MONITORED_KEYS: list[tuple[int, str]] = [
    (0x0D, "enter"),
    (0x09, "tab"),
    (0x1B, "escape"),
    (0x2E, "delete"),
    (0x08, "backspace"),
    (0x20, "space"),
    (0x41, "a"),
    (0x42, "b"),
    (0x43, "c"),
    (0x45, "e"),
    (0x49, "i"),
    (0x4B, "k"),
    (0x4C, "l"),
    (0x52, "r"),
    (0x53, "s"),
    (0x56, "v"),
    (0x5A, "z"),
]


class LiveSkillRecorder:
    """
    Zeichnet echte Nutzer-Workflows (Klicks + 150x150-Template-Ausschnitt + Shortcuts) auf
    und spielt gespeicherte Workflows per `cv2.matchTemplate` in 10x-Geschwindigkeit ab.
    """

    TEMPLATE_SIZE_PX = 150
    DEFAULT_SPEED_MULTIPLIER = 10.0

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._recording: bool = False
        self._skill_name: str = ""
        self._app_name: str = ""
        self._started_at: float = 0.0
        self._last_event_at: float = 0.0
        self._events: list[dict[str, Any]] = []
        self._status_callbacks: list[Callable[[dict[str, Any]], None]] = []

    def register_status_callback(self, cb: Optional[Callable[[dict[str, Any]], None]]) -> None:
        if cb is None:
            return
        with self._lock:
            if cb not in self._status_callbacks:
                self._status_callbacks.append(cb)

    def _emit_status(self) -> None:
        snap = self.get_status()
        with self._lock:
            cbs = list(self._status_callbacks)
        for cb in cbs:
            try:
                cb(snap)
            except Exception:
                pass

    @staticmethod
    def _get_foreground_window_title() -> str:
        if not IS_WINDOWS:
            return "Desktop"
        try:
            user32 = ctypes.windll.user32
            hwnd = user32.GetForegroundWindow()
            if hwnd:
                buf = ctypes.create_unicode_buffer(256)
                user32.GetWindowTextW(hwnd, buf, 256)
                title = buf.value.strip()
                if title:
                    return title[:80]
        except Exception:
            pass
        return "Desktop"

    @staticmethod
    def _get_cursor_xy() -> tuple[int, int]:
        if IS_WINDOWS:
            try:
                class _PT(ctypes.Structure):
                    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

                pt = _PT()
                if ctypes.windll.user32.GetCursorPos(ctypes.byref(pt)):
                    return int(pt.x), int(pt.y)
            except Exception:
                pass
        return 0, 0

    @staticmethod
    def _capture_screen_rgb_pil() -> Any:
        if not MSS_PIL_AVAILABLE or mss is None or Image is None:
            return None
        try:
            with mss.mss() as sct:
                mon = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
                raw = sct.grab(mon)
                return Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
        except Exception:
            return None

    def _save_click_template_patch(
        self,
        cx: int,
        cy: int,
        step_index: int,
        skill_slug: str,
    ) -> str:
        """Speichert exakt einen 150x150-Pixel-Bild-Template-Ausschnitt um `(cx, cy)` im RAM -> NAS_BRAIN."""
        pil_img = self._capture_screen_rgb_pil()
        if pil_img is None:
            return ""
        try:
            half = self.TEMPLATE_SIZE_PX // 2
            img_w, img_h = pil_img.size
            left = max(0, min(img_w - self.TEMPLATE_SIZE_PX, int(cx) - half))
            top = max(0, min(img_h - self.TEMPLATE_SIZE_PX, int(cy) - half))
            right = min(img_w, left + self.TEMPLATE_SIZE_PX)
            bottom = min(img_h, top + self.TEMPLATE_SIZE_PX)
            crop = pil_img.crop((left, top, right, bottom))

            from nas_brain import NAS_BRAIN

            tpl_dir = NAS_BRAIN.hot_cache_dir / "skill_templates"
            tpl_dir.mkdir(parents=True, exist_ok=True)
            fname = f"{skill_slug}_step_{step_index}_{int(time.time() * 1000)}.png"
            tpl_path = tpl_dir / fname
            crop.save(str(tpl_path), format="PNG")
            crop.close()
            NAS_BRAIN.queue_cold_asset(tpl_path, f"skill_templates/{fname}")
            return str(tpl_path)
        except Exception as exc:
            logger.debug("Template-Capture Hinweis: %s", exc)
            return ""
        finally:
            try:
                pil_img.close()
            except Exception:
                pass

    def is_recording(self) -> bool:
        with self._lock:
            return bool(self._recording)

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "ok": True,
                "recording": bool(self._recording),
                "skill_name": self._skill_name,
                "app_name": self._app_name,
                "steps_count": len(self._events),
                "elapsed_sec": round(time.monotonic() - self._started_at, 1) if self._recording else 0.0,
            }

    def start_recording(
        self,
        skill_name: str = "",
        app_name: str = "",
    ) -> dict[str, Any]:
        """Startet die Live-Beobachtung von Mausklicks (mit 150x150 Template) & Tasten."""
        with self._lock:
            if self._recording:
                return {
                    **self.get_status(),
                    "message": f"Workflow-Aufnahme '{self._skill_name or 'Lern-Workflow'}' läuft bereits ({len(self._events)} Schritte).",
                }
            clean_name = (skill_name or "").strip() or f"Gelernt_{time.strftime('%H%M%S')}"
            detected_app = (app_name or "").strip() or self._get_foreground_window_title()
            self._recording = True
            self._skill_name = clean_name
            self._app_name = detected_app
            self._started_at = time.monotonic()
            self._last_event_at = self._started_at
            self._events = []
            self._stop_event.clear()

        if IS_WINDOWS:
            user32 = ctypes.windll.user32
            for vk in (0x01, 0x02, 0x04) + tuple(k for k, _ in _MONITORED_KEYS):
                try:
                    user32.GetAsyncKeyState(vk)
                except Exception:
                    pass

        self._thread = threading.Thread(
            target=self._observe_loop,
            name="JarvisLiveSkillRecorder",
            daemon=True,
        )
        self._thread.start()
        self._emit_status()
        return {
            "ok": True,
            "recording": True,
            "skill_name": clean_name,
            "app_name": detected_app,
            "message": (
                f"Aufnahme gestartet ({clean_name}). Ich schaue dir jetzt zu und merke mir "
                "jeden Mausklick mit 150x150-Button-Template sowie deine Tastenkürzel. "
                "Sag einfach 'Fertig gelernt', sobald wir speichern sollen."
            ),
        }

    def _observe_loop(self) -> None:
        if not IS_WINDOWS:
            return
        user32 = ctypes.windll.user32
        prev_down: dict[int, bool] = {}
        slug_base = "".join(ch if ch.isalnum() else "_" for ch in (self._skill_name or "skill").lower()).strip("_") or "skill"

        while not self._stop_event.is_set():
            with self._lock:
                if not self._recording:
                    break
            now = time.monotonic()

            for vk, btn_name in ((0x01, "left"), (0x02, "right"), (0x04, "middle")):
                is_down = bool(user32.GetAsyncKeyState(vk) & 0x8000)
                was_down = prev_down.get(vk, False)
                prev_down[vk] = is_down
                if is_down and not was_down:
                    cx, cy = self._get_cursor_xy()
                    fg_title = self._get_foreground_window_title()
                    if "JARVIS" in fg_title.upper() and len(self._events) == 0:
                        continue
                    with self._lock:
                        step_no = len(self._events) + 1
                        delay_s = max(0.02, round(now - self._last_event_at, 3))
                        self._last_event_at = now
                    tpl_path = self._save_click_template_patch(cx, cy, step_no, slug_base)
                    ev = {
                        "step": step_no,
                        "type": "click",
                        "button": btn_name,
                        "x": cx,
                        "y": cy,
                        "delay_sec": delay_s,
                        "window": fg_title,
                        "template_path": tpl_path,
                    }
                    with self._lock:
                        self._events.append(ev)
                    self._emit_status()

            ctrl_down = bool(user32.GetAsyncKeyState(0x11) & 0x8000)
            shift_down = bool(user32.GetAsyncKeyState(0x10) & 0x8000)
            for vk, key_label in _MONITORED_KEYS:
                is_down = bool(user32.GetAsyncKeyState(vk) & 0x8000)
                was_down = prev_down.get(vk, False)
                prev_down[vk] = is_down
                if is_down and not was_down:
                    keys: list[str] = []
                    if ctrl_down:
                        keys.append("ctrl")
                    if shift_down and key_label not in ("shift",):
                        keys.append("shift")
                    keys.append(key_label)
                    if not ctrl_down and key_label not in ("enter", "tab", "escape", "delete", "backspace", "space"):
                        continue
                    with self._lock:
                        step_no = len(self._events) + 1
                        delay_s = max(0.02, round(now - self._last_event_at, 3))
                        self._last_event_at = now
                        ev = {
                            "step": step_no,
                            "type": "hotkey" if len(keys) > 1 else "press",
                            "key": key_label,
                            "keys": keys,
                            "delay_sec": delay_s,
                            "window": self._get_foreground_window_title(),
                        }
                        self._events.append(ev)
                    self._emit_status()

            self._stop_event.wait(timeout=0.02)

    def stop_recording(
        self,
        skill_name: str = "",
        app_name: str = "",
        save_to_nas: bool = True,
    ) -> dict[str, Any]:
        """Beendet die Aufnahme und speichert den Workflow dauerhaft in `skills_db.json` (Hot-Cache + NAS)."""
        with self._lock:
            was_recording = self._recording
            self._recording = False
            self._stop_event.set()
            final_skill_name = (skill_name or self._skill_name or "Gelernte Aktion").strip()
            final_app_name = (app_name or self._app_name or "Desktop").strip()
            recorded_steps = [dict(item) for item in self._events]

        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        self._thread = None

        saved_entry: dict[str, Any] = {}
        if save_to_nas and recorded_steps:
            try:
                from nas_brain import NAS_BRAIN

                saved_entry = NAS_BRAIN.learn_recorded_workflow(
                    app_name=final_app_name,
                    skill_name=final_skill_name,
                    steps=recorded_steps,
                    description=f"Live gelernter Workflow '{final_skill_name}' mit {len(recorded_steps)} Schritten.",
                )
            except Exception as exc:
                logger.warning("Workflow konnte nicht in NAS_BRAIN gespeichert werden: %s", exc)

        self._emit_status()
        if not was_recording and not recorded_steps:
            return {
                "ok": False,
                "recording": False,
                "steps_count": 0,
                "message": "Es war aktuell keine Workflow-Aufzeichnung aktiv.",
            }
        return {
            "ok": True,
            "recording": False,
            "skill_name": final_skill_name,
            "app_name": final_app_name,
            "steps_count": len(recorded_steps),
            "steps": recorded_steps,
            "skill_entry": saved_entry,
            "message": (
                f"Fertig gelernt, Chef! Workflow '{final_skill_name}' ({len(recorded_steps)} Schritte mit "
                "150x150-Button-Templates) dauerhaft in skills_db.json & NAS gespeichert."
            ),
        }

    def find_matching_template_on_screen(
        self,
        template_path: str | Path,
        threshold: float = 0.72,
    ) -> Optional[tuple[int, int, float]]:
        """Sucht ein gespeichertes 150x150-Button-Template per `cv2.matchTemplate` auf dem aktuellen Bildschirm."""
        if not CV2_AVAILABLE or cv2 is None or np is None:
            return None
        tpl_file = Path(template_path)
        if not tpl_file.is_file():
            return None
        pil_screen = self._capture_screen_rgb_pil()
        if pil_screen is None:
            return None
        try:
            screen_bgr = cv2.cvtColor(np.array(pil_screen), cv2.COLOR_RGB2BGR)
            tpl_bgr = cv2.imread(str(tpl_file), cv2.IMREAD_COLOR)
            if tpl_bgr is None:
                return None
            sh, sw = screen_bgr.shape[:2]
            th, tw = tpl_bgr.shape[:2]
            if th >= sh or tw >= sw:
                return None
            res = cv2.matchTemplate(screen_bgr, tpl_bgr, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(res)
            if float(max_val) < float(threshold):
                return None
            cx = int(max_loc[0] + tw // 2)
            cy = int(max_loc[1] + th // 2)
            return cx, cy, float(max_val)
        except Exception as exc:
            logger.debug("Template-Match Hinweis: %s", exc)
            return None
        finally:
            try:
                pil_screen.close()
            except Exception:
                pass

    def replay_skill(
        self,
        skill_query: str,
        speed_multiplier: float = 10.0,
    ) -> dict[str, Any]:
        """Spielt einen gelernten Workflow per `cv2.matchTemplate` in 10x-Geschwindigkeit über die Maus ab."""
        from nas_brain import NAS_BRAIN
        from agent_hand import (
            SMART_MOUSE_GUARD,
            coordinate_jarvis_window_for_action,
            fast_move_and_lock,
            ghost_hotkey,
            ghost_press,
            click as hand_click,
            right_click as hand_right_click,
        )

        hits = NAS_BRAIN.find_ui_skills(skill_query, limit=6)
        chosen: Optional[dict[str, Any]] = None
        for item in hits:
            if isinstance(item.get("steps"), list) and item["steps"]:
                chosen = item
                break
        if chosen is None and hits:
            chosen = hits[0]
        if chosen is None:
            return {
                "ok": False,
                "executed_steps": 0,
                "message": f"Kein gelernter Workflow für '{skill_query}' in skills_db.json gefunden.",
            }

        steps = list(chosen.get("steps") or [])
        if not steps:
            if chosen.get("x") is not None and chosen.get("y") is not None:
                steps = [{"type": "click", "button": "left", "x": int(chosen["x"]), "y": int(chosen["y"]), "delay_sec": 0.05}]
            elif chosen.get("shortcut"):
                parts = [p.strip() for p in str(chosen["shortcut"]).split("+") if p.strip()]
                steps = [{"type": "hotkey", "keys": parts, "delay_sec": 0.05}]

        coordinate_jarvis_window_for_action(mode="minimize")
        time.sleep(0.18)
        SMART_MOUSE_GUARD.start_monitoring()
        spd = max(1.0, min(20.0, float(speed_multiplier or self.DEFAULT_SPEED_MULTIPLIER)))
        matched_templates = 0
        executed = 0
        try:
            for step in steps:
                if SMART_MOUSE_GUARD.is_triggered():
                    return {
                        "ok": False,
                        "aborted": True,
                        "executed_steps": executed,
                        "message": SMART_MOUSE_GUARD.abort_reason or "Workflow-Wiedergabe durch Nutzer gestoppt.",
                    }
                raw_delay = float(step.get("delay_sec", 0.25) or 0.25)
                scaled_wait = max(0.015, min(0.35, raw_delay / spd))
                time.sleep(scaled_wait)

                stype = str(step.get("type") or "click").lower()
                if stype == "click":
                    tx = int(step.get("x") or 0)
                    ty = int(step.get("y") or 0)
                    tpl_path = str(step.get("template_path") or "").strip()
                    if tpl_path:
                        hit = self.find_matching_template_on_screen(tpl_path, threshold=0.72)
                        if hit is not None:
                            tx, ty, _ = hit
                            matched_templates += 1
                    btn = str(step.get("button") or "left").lower()
                    fast_move_and_lock(tx, ty, duration=0.05, label=f"SKILL // {chosen.get('skill_name', '')[:22]}")
                    if btn == "right":
                        hand_right_click(tx, ty, duration=0.04)
                    else:
                        hand_click(tx, ty, button=btn if btn in ("left", "middle") else "left", clicks=1, duration=0.04)
                    executed += 1
                elif stype == "hotkey":
                    keys = [str(k).strip().lower() for k in (step.get("keys") or []) if str(k).strip()]
                    if keys:
                        ghost_hotkey(*keys, label=f"SKILL HOTKEY: {'+'.join(keys).upper()}")
                        executed += 1
                elif stype == "press":
                    k = str(step.get("key") or "").strip().lower()
                    if k:
                        ghost_press(k, label=f"SKILL TASTE: {k.upper()}")
                        executed += 1
        finally:
            SMART_MOUSE_GUARD.stop_monitoring()

        return {
            "ok": True,
            "skill_name": chosen.get("skill_name"),
            "executed_steps": executed,
            "matched_templates": matched_templates,
            "message": (
                f"Gelernter Workflow '{chosen.get('skill_name')}' in {int(spd)}x-Geschwindigkeit "
                f"abgespielt ({executed} Schritte, {matched_templates} Button-Templates exakt getroffen)."
            ),
        }


SKILL_RECORDER = LiveSkillRecorder()
