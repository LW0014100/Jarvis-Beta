# -*- coding: utf-8 -*-
"""
Aktions- & Makro-Engine sowie durchgehender Ereignis-Ticker (Live-Telemetry-Feed)
für das Multi-Monitor- und Multi-Window-System.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime
import os
import platform
import shutil
import subprocess
import threading
import time
from typing import Any, Callable, Optional


class EventTicker:
    """Thread-sicherer Echtzeit-Ereignis-Ticker für Voice, Screen-Scans, Fensterbefehle & n8n."""

    def __init__(self, maxlen: int = 120) -> None:
        self._lock = threading.Lock()
        self._seq = 0
        self._events: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self._listeners: list[Callable[[dict[str, Any]], None]] = []

    def subscribe(self, fn: Callable[[dict[str, Any]], None]) -> None:
        with self._lock:
            if fn not in self._listeners:
                self._listeners.append(fn)

    def unsubscribe(self, fn: Callable[[dict[str, Any]], None]) -> None:
        with self._lock:
            self._listeners = [l for l in self._listeners if l is not fn]

    def emit(
        self,
        category: str,
        source: str,
        summary: str,
        *,
        monitor: Any = None,
        data: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        with self._lock:
            self._seq += 1
            ev = {
                "seq": self._seq,
                "ts": datetime.now().strftime("%H:%M:%S.%f")[:-3],
                "category": str(category or "system")[:24],
                "source": str(source or "jarvis")[:40],
                "summary": str(summary or "")[:240],
                "monitor": monitor,
                "data": data or {},
            }
            self._events.append(ev)
            listeners = list(self._listeners)

        for fn in listeners:
            try:
                fn(ev)
            except Exception:
                pass
        return ev

    def recent(self, limit: int = 40) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._events)[-max(1, int(limit)) :]


EVENT_TICKER = EventTicker()


def open_window(window_name: str = "screen-matrix", monitor: Any = None) -> dict[str, Any]:
    """
    Öffnet ein synchronisiertes Zusatzfenster ('screen-matrix' -> Fenster 2,
    'system-telemetry' -> Fenster 3) optional direkt auf dem gewünschten Monitor.
    """
    from gui import get_active_gui

    raw = (window_name or "screen-matrix").strip().lower()
    if raw in ("neural-core", "neural", "brain", "gehirn", "3d-gehirn", "3d", "neural-brain"):
        panel_key = "neural-core"
        default_mon = 2
    elif raw in ("system-telemetry", "telemetry", "logs", "neural-logs", "fenster3", "window3", "3"):
        panel_key = "system-telemetry"
        default_mon = 3
    else:
        panel_key = "screen-matrix"
        default_mon = 2

    target_mon = monitor if monitor is not None else default_mon
    gui_inst = get_active_gui()
    EVENT_TICKER.emit("window", "open_window", f"Öffne {panel_key} auf Monitor {target_mon}", monitor=target_mon)
    if gui_inst is not None:
        return gui_inst.open_subwindow_on_monitor(panel=panel_key, monitor_target=target_mon)
    return {
        "ok": False,
        "panel": panel_key,
        "monitor": target_mon,
        "message": "Das Web-HUD läuft aktuell nicht im Grafikmodus.",
    }


def read_monitor_ocr(
    monitor_target: Any = "cursor",
    question: str = "Lies den sichtbaren Text, geöffnete Programme und wichtige Hinweise knapp auf Deutsch aus.",
    brain: Any = None,
) -> dict[str, Any]:
    """Erfasst den gewählten (bzw. Maus-)Monitor in voller Auflösung und liest Inhalte via Vision/LLM aus."""
    from gui import get_active_gui
    from multi_monitor import capture_monitor_jpeg, highlight_monitor_region, resolve_monitor_selector

    gui_inst = get_active_gui()
    resolved_tgt, _ = resolve_monitor_selector(monitor_target)
    hl = highlight_monitor_region(monitor=resolved_tgt, label=f"OCR / VISION M{resolved_tgt}", duration=2.2)
    cap = capture_monitor_jpeg(
        target=resolved_tgt,
        quality=88,
        max_width=0,  # Volle Originalauflösung für maximale Lesbarkeit
        with_grid=False,
    )
    if not cap.get("ok") or not cap.get("jpeg_bytes"):
        return {
            "ok": False,
            "target": monitor_target,
            "highlight": hl,
            "message": cap.get("summary") or "Screenshot konnte nicht erstellt werden.",
        }

    EVENT_TICKER.emit(
        "vision",
        "read_monitor_ocr",
        f"Vollauflösungs-Scan Monitor {cap.get('target')} ({cap.get('width')}x{cap.get('height')})",
        monitor=cap.get("target"),
    )

    if brain is None and gui_inst is not None:
        brain = getattr(gui_inst, "_brain", None)

    analysis_text = cap.get("summary", "Monitor erfasst.")
    if brain is not None and hasattr(brain, "ask"):
        try:
            prompt = (
                f"[VOLLAUFLÖSUNGS-MONITOR-ANALYSE: Monitor {cap.get('target')} ({cap.get('width')}x{cap.get('height')})]\n"
                f"{question}"
            )
            res = brain.ask(prompt, image_bytes=bytes(cap["jpeg_bytes"]), use_memory=False)
            analysis_text = str(getattr(res, "text", res) or analysis_text).strip()
        except Exception as exc:
            analysis_text = f"{analysis_text} (Vision-Hinweis: {exc})"

    if gui_inst is not None:
        gui_inst.emit_neural_thought(
            category="vision",
            tool="read_monitor_ocr",
            summary=analysis_text[:280],
            monitor=cap.get("target"),
        )

    return {
        "ok": True,
        "target": cap.get("target"),
        "width": cap.get("width"),
        "height": cap.get("height"),
        "analysis": analysis_text,
        "message": analysis_text,
    }


class MacroEngine:
    """Aktions- & Makro-Engine für Multi-Monitor-Workflows."""

    def start_work_mode(self) -> dict[str, Any]:
        """
        Makro 'Arbeitsmodus starten': Öffnet/verteilt Browser, Code-Editor und Zusatzfenster
        sowie System-Fenster über alle angeschlossenen Monitore (1, 2, 3).
        """
        from gui import get_active_gui
        from multi_monitor import get_connected_monitors, list_open_windows, move_window_to_monitor

        mons = get_connected_monitors()
        EVENT_TICKER.emit("macro", "work_mode", f"Starte Arbeitsmodus auf {len(mons)} Monitor(en)")
        actions_done: list[str] = []

        # 1. Code-Editor auf Hauptmonitor (M2 Mitte, bzw. M1 falls nur 1 Monitor)
        mid_idx = 2 if len(mons) >= 3 else 1
        right_idx = 3 if len(mons) >= 3 else (2 if len(mons) == 2 else 1)

        wins = list_open_windows()
        titles_low = [w["title"].lower() for w in wins]
        if not any("visual studio code" in t or "cursor" in t for t in titles_low):
            code_cmd = shutil.which("code")
            if code_cmd:
                try:
                    kwargs = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)} if platform.system() == "Windows" else {}
                    subprocess.Popen([code_cmd], **kwargs)
                    actions_done.append("VS Code gestartet")
                except Exception:
                    pass

        for query, target_m, layout in (
            ("visual studio code", mid_idx, "maximize"),
            ("code", mid_idx, "maximize"),
            ("chrome", 1, "maximize"),
            ("vivaldi", 1, "maximize"),
            ("edge", 1, "maximize"),
            ("powershell", right_idx, "left_half"),
            ("terminal", right_idx, "left_half"),
        ):
            if any(query in t for t in titles_low):
                res = move_window_to_monitor(query, target_m, layout=layout)
                if res.get("ok"):
                    actions_done.append(f"{query} -> M{target_m}")

        # 2. Multi-Window-System: Screen-Matrix (Fenster 2) & System-Telemetry (Fenster 3) positionieren
        gui_inst = get_active_gui()
        if gui_inst is not None:
            if len(mons) >= 2:
                gui_inst.open_subwindow_on_monitor("screen-matrix", monitor_target=1 if len(mons) >= 3 else 2)
                actions_done.append("Fenster 2 (Screen-Matrix) geöffnet")
            if len(mons) >= 3:
                gui_inst.open_subwindow_on_monitor("system-telemetry", monitor_target=3)
                actions_done.append("Fenster 3 (System-Telemetry) auf M3 geöffnet")
            gui_inst.emit_neural_thought(
                category="tool",
                tool="work_mode_macro",
                summary=f"Arbeitsmodus aktiv: {', '.join(actions_done) or 'Fenster ausgerichtet'}",
            )

        summary = "Arbeitsmodus aktiviert" + (f": {', '.join(actions_done)}." if actions_done else ".")
        EVENT_TICKER.emit("macro", "work_mode", summary)
        return {"ok": True, "actions": actions_done, "monitor_count": len(mons), "message": summary}

    def focus_monitor(self, monitor_target: Any = 2, brain: Any = None) -> dict[str, Any]:
        """Makro 'Fokus auf Monitor X': hebt den Monitor hervor und analysiert ihn in voller Auflösung."""
        EVENT_TICKER.emit("macro", "focus_monitor", f"Fokus auf Monitor {monitor_target}", monitor=monitor_target)
        res = read_monitor_ocr(
            monitor_target=monitor_target,
            question="Fasse präzise in 2–3 Sätzen zusammen, was aktuell auf diesem Bildschirm zu sehen ist und welche App im Fokus steht.",
            brain=brain,
        )
        return res


MACRO_ENGINE = MacroEngine()
