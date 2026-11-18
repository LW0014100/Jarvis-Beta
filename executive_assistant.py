"""
Executive Assistant & Second Brain (`executive_assistant.py`) für J.A.R.V.I.S.
==============================================================================
Implementiert 4 autonome KI-Mitarbeiter-Module nach dem "Jarvis-Playbook / CEO-GPT"-Prinzip:
1. MODUL 1 ("Second Brain"): Permanentes Projekt-, Datei- & Persönlichkeits-Gedächtnis
   (`ProjectKnowledgeGraph` + Desktop/Dokumente/Downloads/NAS-Indizierung + sofortiges Fortsetzen)
2. MODUL 2 ("Morning Briefing & Cockpit"): Proaktives CEO-Cockpit mit Browser-Tabs + 45s-Briefing
3. MODUL 3 ("Multi-App End-to-End Pipelines"):
   - Pipeline A: Live Browser-Recherche -> formatierte Datei/Tabelle direkt auf dem Desktop öffnen
   - Pipeline B: Projekt-Setup auf Zuruf (Ordnerstruktur + Hauptprogramm + Referenz-Website)
4. MODUL 4 ("Proaktiver Mitdenk-Modus"): Smart Suggestion Engine (HUD-Banner + Overlay-Pill + Sprache)
"""

from __future__ import annotations

import csv
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import threading
import time
from typing import Any, Callable, Optional
import webbrowser

IS_WINDOWS = platform.system().lower() == "windows"

FILE_CATEGORY_RULES: dict[str, tuple[str, str]] = {
    ".stl": ("cad", "OrcaSlicer"),
    ".3mf": ("cad", "OrcaSlicer"),
    ".step": ("cad", "SolidCraft / Onshape"),
    ".stp": ("cad", "SolidCraft / Onshape"),
    ".fcstd": ("cad", "FreeCAD"),
    ".f3d": ("cad", "Fusion 360"),
    ".obj": ("cad", "Blender"),
    ".blend": ("cad", "Blender"),
    ".scad": ("cad", "OpenSCAD"),
    ".mp4": ("video", "CapCut"),
    ".mov": ("video", "CapCut"),
    ".mkv": ("video", "CapCut"),
    ".avi": ("video", "CapCut"),
    ".prproj": ("video", "Premiere Pro"),
    ".drp": ("video", "DaVinci Resolve"),
    ".py": ("code", "Visual Studio Code"),
    ".js": ("code", "Visual Studio Code"),
    ".ts": ("code", "Visual Studio Code"),
    ".tsx": ("code", "Visual Studio Code"),
    ".html": ("code", "Visual Studio Code"),
    ".css": ("code", "Visual Studio Code"),
    ".cpp": ("code", "Visual Studio Code"),
    ".ino": ("code", "Arduino IDE"),
    ".xlsx": ("table", "Excel"),
    ".csv": ("table", "Excel"),
    ".docx": ("document", "Word"),
    ".pdf": ("document", "Browser / PDF"),
    ".md": ("document", "Editor"),
    ".txt": ("document", "Editor"),
}

IGNORED_DIR_NAMES = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    "build",
    "dist",
    "site-packages",
    ".gradle",
    ".idea",
}

CONFIRM_SUGGESTION_RE = re.compile(
    r"^\s*(?:ja|jup|klar|gerne|genau|mach\s+das|ja\s+mach\s+das|ja\s+bitte|"
    r"los|ausführen|öffne\s+es|mach\s+weiter|ok|okay|einverstanden|yes)\b",
    flags=re.IGNORECASE,
)

DISMISS_SUGGESTION_RE = re.compile(
    r"^\s*(?:nein|nee|später|nicht\s+jetzt|verwerfen|abbruch|ignore|ignorieren|weg\s+damit)\b",
    flags=re.IGNORECASE,
)


def _safe_filename(name: str, default: str = "Jarvis_Projekt") -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\r\n]+', "_", str(name or "").strip())
    cleaned = re.sub(r"\s+", "_", cleaned).strip("._ ")
    return cleaned[:64] or default


def _desktop_dir() -> Path:
    home = Path.home()
    for candidate in (home / "Desktop", home / "Schreibtisch", home):
        if candidate.exists() and candidate.is_dir():
            return candidate
    return home


def _candidate_scan_roots(extra_roots: Optional[list[Path]] = None) -> list[Path]:
    home = Path.home()
    roots: list[Path] = []
    candidates = [
        home / "Desktop",
        home / "Schreibtisch",
        home / "Documents",
        home / "Dokumente",
        home / "Downloads",
    ]
    if extra_roots:
        candidates.extend(extra_roots)
    try:
        from nas_brain import NAS_BRAIN

        candidates.extend([NAS_BRAIN._hot_cache_dir, NAS_BRAIN._active_dir])
    except Exception:
        pass

    seen: set[str] = set()
    for p in candidates:
        try:
            if not p or not p.exists() or not p.is_dir():
                continue
            key = str(p.resolve()).lower()
            if key not in seen:
                seen.add(key)
                roots.append(p)
        except Exception:
            pass
    return roots


def _open_path_visibly(target: Path, app_hint: str = "") -> str:
    """Öffnet eine Datei oder einen Ordner sichtbar auf dem Desktop."""
    resolved = Path(target)
    if app_hint:
        try:
            from app_indexer import APP_INDEXER

            lower_hint = app_hint.lower()
            if any(k in lower_hint for k in ("orcaslicer", "capcut", "davinci", "blender", "code", "editor", "notepad", "excel")):
                if IS_WINDOWS:
                    exe_path = APP_INDEXER.find_app(app_hint.split("/")[0].strip())
                    if exe_path:
                        subprocess.Popen([exe_path, str(resolved)])
                        return f"{resolved.name} sichtbar mit {app_hint} geöffnet."
        except Exception:
            pass

    if IS_WINDOWS:
        os.startfile(str(resolved))  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open", str(resolved)])
    return f"{resolved.name} sichtbar auf dem Desktop geöffnet."


class ProjectKnowledgeGraph:
    """
    MODUL 1: Leichtgewichtiger Second-Brain-Indexer.
    Scannt Desktop, Dokumente, Downloads und NAS/HotCache mit niedriger CPU-Priorität,
    klassifiziert Projekte und aktualisiert `LongTermMemory` + `NAS_BRAIN`.
    """

    def __init__(self, memory: Any = None) -> None:
        self._lock = threading.RLock()
        self._memory = memory
        self._recent_files: list[dict[str, Any]] = []
        self._last_scan_ts: float = 0.0
        self._last_scan_str: str = ""
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._seen_recent_stl: set[str] = set()
        self._on_new_export_callback: Optional[Callable[[dict[str, Any]], None]] = None

    def attach_memory(self, memory: Any) -> None:
        self._memory = memory

    def register_export_callback(self, cb: Callable[[dict[str, Any]], None]) -> None:
        self._on_new_export_callback = cb

    def scan_once(
        self,
        *,
        extra_roots: Optional[list[Path]] = None,
        max_depth: int = 3,
        max_files_per_root: int = 160,
        memory: Any = None,
        sync_nas: bool = True,
    ) -> dict[str, Any]:
        mem = memory or self._memory
        roots = _candidate_scan_roots(extra_roots=extra_roots)
        collected: list[dict[str, Any]] = []
        now_ts = time.time()

        for root in roots:
            root_count = 0
            stack: list[tuple[Path, int]] = [(root, 0)]
            while stack and root_count < max_files_per_root:
                current, depth = stack.pop()
                try:
                    entries = list(os.scandir(current))
                except Exception:
                    continue
                for entry in entries:
                    if root_count >= max_files_per_root:
                        break
                    name = entry.name
                    if name.startswith(".") and name != ".git":
                        continue
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if name.lower() in IGNORED_DIR_NAMES:
                                continue
                            if depth < max_depth:
                                stack.append((Path(entry.path), depth + 1))
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        ext = Path(name).suffix.lower()
                        if ext not in FILE_CATEGORY_RULES:
                            continue
                        st = entry.stat(follow_symlinks=False)
                        category, app_hint = FILE_CATEGORY_RULES[ext]
                        collected.append(
                            {
                                "name": name,
                                "path": entry.path,
                                "ext": ext,
                                "category": category,
                                "app_hint": app_hint,
                                "mtime": float(st.st_mtime),
                                "modified_at": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                                "size_bytes": int(st.st_size),
                                "parent": str(Path(entry.path).parent.name),
                            }
                        )
                        root_count += 1
                    except Exception:
                        continue
                time.sleep(0.002)

        collected.sort(key=lambda item: item["mtime"], reverse=True)
        top_files = collected[:80]
        scanned_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Neue STL/3MF-Exporte (jünger als 180s) für Smart Suggestion erkennen
        fresh_exports: list[dict[str, Any]] = []
        for f in top_files[:20]:
            if f["ext"] in (".stl", ".3mf") and (now_ts - f["mtime"]) <= 180.0:
                key = str(f["path"]).lower()
                if key not in self._seen_recent_stl:
                    self._seen_recent_stl.add(key)
                    fresh_exports.append(f)

        with self._lock:
            self._recent_files = top_files
            self._last_scan_ts = time.monotonic()
            self._last_scan_str = scanned_at

        if mem is not None and hasattr(mem, "record_active_project"):
            for item in reversed(top_files[:6]):
                try:
                    mem.record_active_project(
                        name=item["name"],
                        path=item["path"],
                        category=item["category"],
                        app_hint=item["app_hint"],
                        modified_at=item["modified_at"],
                    )
                except Exception:
                    pass

        snapshot = {
            "scanned_at": scanned_at,
            "root_count": len(roots),
            "file_count": len(top_files),
            "recent_files": top_files,
            "active_projects": mem.get_active_projects(limit=15) if (mem is not None and hasattr(mem, "get_active_projects")) else top_files[:10],
        }

        if sync_nas:
            try:
                from nas_brain import NAS_BRAIN

                NAS_BRAIN.sync_project_knowledge_graph(snapshot)
            except Exception:
                pass

        if fresh_exports and self._on_new_export_callback is not None:
            for exp in fresh_exports[:1]:
                try:
                    self._on_new_export_callback(exp)
                except Exception:
                    pass

        return snapshot

    def start_background_scanner(self, memory: Any = None, interval_sec: float = 120.0) -> threading.Thread:
        if memory is not None:
            self._memory = memory
        if self._thread is not None and self._thread.is_alive():
            return self._thread
        self._stop_event.clear()

        def _loop() -> None:
            # Kurze Initialpause nach HUD-Start, damit Turbo-Start < 1,5s unberührt bleibt
            if self._stop_event.wait(3.0):
                return
            while not self._stop_event.is_set():
                try:
                    self.scan_once(memory=self._memory, sync_nas=True)
                except Exception:
                    pass
                if self._stop_event.wait(max(20.0, float(interval_sec))):
                    break

        self._thread = threading.Thread(target=_loop, name="JarvisSecondBrainScanner", daemon=True)
        self._thread.start()
        return self._thread

    def stop(self) -> None:
        self._stop_event.set()

    def get_recent_files(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            files = list(self._recent_files)
        if not files:
            try:
                self.scan_once(sync_nas=False)
                with self._lock:
                    files = list(self._recent_files)
            except Exception:
                files = []
        return files[: max(1, limit)]

    def resolve_project_request(self, query: str, memory: Any = None) -> Optional[dict[str, Any]]:
        """
        Findet ohne Rückfragen das passende Projekt/die passende Datei zu Anfragen wie
        'Mach an meinem letzten Projekt weiter' oder 'Öffne die Datei von gestern'.
        """
        mem = memory or self._memory
        low = str(query or "").lower()
        want_yesterday = any(k in low for k in ("gestern", "yesterday", "letztes mal", "vorhin"))

        want_cat = ""
        if any(k in low for k in ("cad", "3d", "stl", "slic", "druck")):
            want_cat = "cad"
        elif any(k in low for k in ("video", "schnitt", "capcut", "davinci", "clip", "film")):
            want_cat = "video"
        elif any(k in low for k in ("code", "python", "skript", "programm", "repo", "vs code")):
            want_cat = "code"
        elif any(k in low for k in ("tabelle", "excel", "csv")):
            want_cat = "table"
        elif any(k in low for k in ("dokument", "brief", "notiz", "text", "pdf", "word")):
            want_cat = "document"

        candidates: list[dict[str, Any]] = []
        if mem is not None and hasattr(mem, "get_active_projects"):
            for p in mem.get_active_projects(limit=25):
                p_path = str(p.get("path", "")).strip()
                if p_path and Path(p_path).exists():
                    candidates.append(dict(p))

        for f in self.get_recent_files(limit=40):
            if not any(str(c.get("path", "")).lower() == str(f.get("path", "")).lower() for c in candidates):
                candidates.append(dict(f))

        if not candidates:
            return None

        # Stichwort-Match auf Namen/Pfad prüfen
        tokens = [
            tok for tok in re.findall(r"[a-zA-ZäöüÄÖÜß0-9_-]{3,}", low)
            if tok not in {
                "mach", "weiter", "mein", "meinem", "letzten", "letztes", "letzte",
                "projekt", "datei", "öffne", "oeffne", "bitte", "gestern", "heute", "jarvis",
            }
        ]
        if tokens:
            for item in candidates:
                hay = f"{item.get('name', '')} {item.get('path', '')} {item.get('notes', '')}".lower()
                if any(tok in hay for tok in tokens):
                    return item

        if want_cat:
            cat_matches = [c for c in candidates if str(c.get("category", "")).lower() == want_cat]
            if cat_matches:
                candidates = cat_matches

        if want_yesterday:
            yesterday_date = (datetime.now() - timedelta(days=1)).date()
            y_matches = []
            for c in candidates:
                mod = str(c.get("modified_at", ""))[:10]
                if mod == yesterday_date.isoformat():
                    y_matches.append(c)
            if y_matches:
                return y_matches[0]

        return candidates[0]


class SmartSuggestionEngine:
    """
    MODUL 4: Proaktiver Mitdenk-Modus.
    Erkennt relevante Desktop-/Export-/Fehler-Situationen und bietet eine subtile
    1-Klick- oder Sprach-Bestätigung ('Ja, mach das') an.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._current_suggestion: Optional[dict[str, Any]] = None
        self._cooldown_by_key: dict[str, float] = {}
        self._callbacks: list[Callable[[Optional[dict[str, Any]]], None]] = []

    def register_callback(self, cb: Callable[[Optional[dict[str, Any]]], None]) -> None:
        with self._lock:
            if cb not in self._callbacks:
                self._callbacks.append(cb)

    def _notify(self, suggestion: Optional[dict[str, Any]]) -> None:
        try:
            from multi_cursor_overlay import MULTI_CURSOR_OVERLAY

            if suggestion:
                MULTI_CURSOR_OVERLAY.set_status_pill(
                    str(suggestion.get("pill_text") or suggestion.get("text") or ""),
                    visible=True,
                )
            else:
                MULTI_CURSOR_OVERLAY.clear_status_pill()
        except Exception:
            pass

        with self._lock:
            cbs = list(self._callbacks)
        for cb in cbs:
            try:
                cb(suggestion)
            except Exception:
                pass

    def get_current_suggestion(self) -> Optional[dict[str, Any]]:
        with self._lock:
            return dict(self._current_suggestion) if self._current_suggestion else None

    def propose_suggestion(
        self,
        *,
        dedupe_key: str,
        text: str,
        action_type: str,
        goal: str,
        target_path: str = "",
        app_name: str = "",
        cooldown_sec: float = 300.0,
    ) -> Optional[dict[str, Any]]:
        now = time.monotonic()
        with self._lock:
            last = self._cooldown_by_key.get(dedupe_key, 0.0)
            if (now - last) < cooldown_sec:
                return None
            self._cooldown_by_key[dedupe_key] = now
            suggestion = {
                "id": f"sug_{int(time.time() * 1000)}",
                "key": dedupe_key,
                "text": text.strip(),
                "pill_text": f"💡 VORSCHLAG: {text.strip()}",
                "action_type": action_type,
                "goal": goal.strip(),
                "target_path": target_path,
                "app_name": app_name,
                "created_at": datetime.now().strftime("%H:%M:%S"),
            }
            self._current_suggestion = suggestion

        self._notify(suggestion)
        return dict(suggestion)

    def evaluate_context(
        self,
        *,
        event_hint: str = "",
        target_path: str = "",
        screen_summary: str = "",
    ) -> Optional[dict[str, Any]]:
        """Bewertet neue Dateien, Browser-Kontexte oder Fehler und erzeugt ggf. 1 smarten Vorschlag."""
        hint_low = f"{event_hint} {target_path} {screen_summary}".lower()

        if target_path and Path(target_path).suffix.lower() in (".stl", ".3mf"):
            fname = Path(target_path).name
            return self.propose_suggestion(
                dedupe_key=f"stl:{target_path.lower()}",
                text=f"Soll ich die STL-Datei '{fname}' direkt in OrcaSlicer öffnen und für den 3D-Druck vorbereiten?",
                action_type="open_in_app",
                goal=f"Öffne {fname} in OrcaSlicer und bereite den Druck vor.",
                target_path=target_path,
                app_name="OrcaSlicer",
            )

        if any(err in hint_low for err in ("traceback", "syntaxerror", "modulenotfounderror", "fehlermeldung", "exception")):
            return self.propose_suggestion(
                dedupe_key="error_popup_research",
                text="Ich sehe eine Fehlermeldung. Soll ich im Browser sofort eine Lösung recherchieren?",
                action_type="browser_research",
                goal=f"Recherchiere im Browser eine Lösung für diesen Fehler: {(screen_summary or event_hint)[:120]}",
                app_name="Browser",
                cooldown_sec=240.0,
            )

        if any(k in hint_low for k in ("preisvergleich", "recherche", "vergleich", "goldpreis", "aktie", "bitcoin")) and (
            "fertig" in hint_low or "ergebnis" in hint_low
        ):
            return self.propose_suggestion(
                dedupe_key="research_to_desktop",
                text="Soll ich die Ergebnisse unserer Browser-Recherche als strukturierte Datei auf deinem Desktop speichern?",
                action_type="research_to_desktop",
                goal=event_hint or screen_summary or "Aktuelle Recherche-Zusammenfassung",
                cooldown_sec=300.0,
            )

        return None

    def dismiss_suggestion(self) -> dict[str, Any]:
        with self._lock:
            had = self._current_suggestion is not None
            self._current_suggestion = None
        self._notify(None)
        return {"ok": True, "dismissed": had}

    def confirm_suggestion(
        self,
        *,
        assistant: "ExecutiveAssistant",
        brain: Any = None,
        on_status: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        with self._lock:
            sug = dict(self._current_suggestion) if self._current_suggestion else None
            self._current_suggestion = None
        self._notify(None)

        if not sug:
            return {"ok": False, "spoken": "Chef, es liegt aktuell kein offener Vorschlag vor."}

        act = sug.get("action_type", "")
        target_path = str(sug.get("target_path", "")).strip()
        app_name = str(sug.get("app_name", "")).strip()
        goal = str(sug.get("goal", "")).strip()

        if act == "open_in_app" and target_path:
            opened_msg = _open_path_visibly(Path(target_path), app_hint=app_name)
            try:
                from agent_hand import VISUAL_HAND_AGENT

                if app_name and brain is not None:
                    VISUAL_HAND_AGENT.visual_desktop_app_operator(
                        goal=goal,
                        app_name=app_name,
                        brain=brain,
                        on_status=on_status,
                        max_steps=6,
                    )
            except Exception:
                pass
            return {"ok": True, "spoken": f"Erledigt, Chef. {opened_msg}", "suggestion": sug}

        if act == "browser_research":
            try:
                from agent_hand import VISUAL_HAND_AGENT

                res = VISUAL_HAND_AGENT.visual_browser_search_and_read(
                    query=goal,
                    brain=brain,
                    on_status=on_status,
                    interactive_click=True,
                )
                return {
                    "ok": True,
                    "spoken": res.get("summary") or "Ich habe die Live-Lösung im Browser für dich geöffnet, Chef.",
                    "suggestion": sug,
                }
            except Exception as exc:
                return {"ok": False, "spoken": f"Browser-Ausführung fehlgeschlagen: {exc}", "suggestion": sug}

        if act == "research_to_desktop":
            return assistant.run_research_to_desktop_pipeline(query=goal, brain=brain, on_status=on_status)

        return {"ok": True, "spoken": f"Vorschlag '{sug.get('text', '')}' ausgeführt, Chef.", "suggestion": sug}


class ExecutiveAssistant:
    """Zentraler KI-Mitarbeiter-Koordinator für Second Brain, Cockpit-Briefing & Workflow-Pipelines."""

    def __init__(self, memory: Any = None) -> None:
        self._memory = memory
        self.knowledge_graph = ProjectKnowledgeGraph(memory=memory)
        self.suggestions = SmartSuggestionEngine()
        self.knowledge_graph.register_export_callback(self._on_new_export_detected)
        self._briefing_callbacks: list[Callable[[str], None]] = []

    def attach_memory(self, memory: Any) -> None:
        self._memory = memory
        self.knowledge_graph.attach_memory(memory)

    def start(self, memory: Any = None) -> None:
        if memory is not None:
            self.attach_memory(memory)
        self.knowledge_graph.start_background_scanner(memory=self._memory, interval_sec=120.0)

    def _on_new_export_detected(self, file_info: dict[str, Any]) -> None:
        self.suggestions.evaluate_context(
            event_hint=f"Neue Export-Datei {file_info.get('name', '')}",
            target_path=str(file_info.get("path", "")),
        )

    @staticmethod
    def is_confirm_reply(text: str) -> bool:
        return bool(CONFIRM_SUGGESTION_RE.search(str(text or "").strip()))

    @staticmethod
    def is_dismiss_reply(text: str) -> bool:
        return bool(DISMISS_SUGGESTION_RE.search(str(text or "").strip()))

    # ------------------------------------------------------------------
    # MODUL 1: Sofortiges Fortsetzen / Öffnen bekannter Projekte
    # ------------------------------------------------------------------
    def resume_project_or_file(
        self,
        query: str,
        *,
        memory: Any = None,
        brain: Any = None,
        on_status: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        mem = memory or self._memory
        match = self.knowledge_graph.resolve_project_request(query, memory=mem)
        if not match or not match.get("path"):
            return {
                "ok": False,
                "spoken": "Chef, im Second-Brain-Gedächtnis wurde noch keine aktuelle Projektdatei gefunden.",
            }

        target = Path(str(match["path"]))
        if not target.exists():
            return {
                "ok": False,
                "spoken": f"Chef, die gespeicherte Datei {target.name} existiert unter diesem Pfad nicht mehr.",
            }

        if on_status:
            on_status(f"📂 Second Brain: Öffne '{target.name}' ({match.get('app_hint', 'Desktop')})...")

        opened_msg = _open_path_visibly(target, app_hint=str(match.get("app_hint", "")))
        if mem is not None and hasattr(mem, "record_active_project"):
            mem.record_active_project(
                name=target.name,
                path=str(target),
                category=str(match.get("category", "general")),
                app_hint=str(match.get("app_hint", "")),
                modified_at=str(match.get("modified_at", "")),
            )

        if str(match.get("category", "")).lower() == "cad" and target.suffix.lower() in (".stl", ".3mf"):
            self.suggestions.evaluate_context(
                event_hint=f"CAD-Projekt geöffnet: {target.name}",
                target_path=str(target),
            )

        return {
            "ok": True,
            "project": match,
            "spoken": (
                f"Chef, ich habe dein Projekt {target.name} aus deinem Second Brain geladen "
                f"und sichtbar auf dem Desktop geöffnet. {opened_msg}"
            ),
        }

    # ------------------------------------------------------------------
    # MODUL 2: Proaktives Morning Briefing & Desktop-Cockpit
    # ------------------------------------------------------------------
    def run_executive_briefing(
        self,
        *,
        brain: Any = None,
        memory: Any = None,
        alarm_manager: Any = None,
        tts: Any = None,
        on_status: Optional[Callable[[str], None]] = None,
        open_browser_cockpit: bool = True,
    ) -> dict[str, Any]:
        mem = memory or self._memory
        if on_status:
            on_status("⚡ Executive Briefing gestartet: Richte Desktop-Cockpit ein...")

        # 1. Aktives Second-Brain-Update (schnell im Thread)
        snapshot = self.knowledge_graph.scan_once(memory=mem, sync_nas=False)
        recent_projects = snapshot.get("active_projects") or snapshot.get("recent_files") or []
        top_project = recent_projects[0] if recent_projects else None

        # 2. Offene Aufgaben & Wecker sammeln
        open_tasks = mem.get_open_tasks(include_done=False) if (mem is not None and hasattr(mem, "get_open_tasks")) else []
        prefs = mem.get_user_preferences() if (mem is not None and hasattr(mem, "get_user_preferences")) else {}
        alarms_active: list[str] = []
        if alarm_manager is not None and hasattr(alarm_manager, "_alarms"):
            try:
                alarms_active = [
                    f"{a.get('time', '?')} Uhr ({a.get('label', 'Wecker')})"
                    for a in getattr(alarm_manager, "_alarms", [])
                    if a.get("enabled", True)
                ]
            except Exception:
                alarms_active = []

        # 3. Sichtbare Browser-Cockpit-Choreografie
        opened_tabs: list[str] = []
        if open_browser_cockpit:
            cockpit_urls = list(prefs.get("preferred_sites") or [])[:2]
            if not cockpit_urls:
                cockpit_urls = ["https://www.tradingview.com/", "https://www.tagesschau.de/"]
            for url in cockpit_urls:
                try:
                    webbrowser.open_new_tab(url)
                    opened_tabs.append(url)
                    time.sleep(0.25)
                except Exception:
                    pass
            try:
                from agent_hand import VISUAL_HAND_AGENT

                if brain is not None and VISUAL_HAND_AGENT.is_available():
                    VISUAL_HAND_AGENT.visual_browser_search_and_read(
                        query="Goldpreis Bitcoin Dax Wetter heute",
                        brain=brain,
                        on_status=on_status,
                        interactive_click=False,
                    )
            except Exception:
                pass

        # 4. System- & Schwarm-Status sammeln
        nas_label = "NVMe HotCache bereit"
        try:
            from nas_brain import NAS_BRAIN

            nas_label = NAS_BRAIN.get_status().get("status_label", nas_label)
        except Exception:
            pass

        task_part = (
            f"Du hast {len(open_tasks)} offene Aufgaben, zuerst: {open_tasks[0].get('title', '')}. "
            if open_tasks
            else "Es stehen keine dringenden offenen To-Dos an. "
        )
        alarm_part = (
            f"Aktive Wecker: {', '.join(alarms_active[:2])}. "
            if alarms_active
            else ""
        )
        proj_part = (
            f"Dein aktives Projekt {top_project.get('name', 'Projekt')} liegt bereit. "
            if top_project
            else "Dein Workspace ist sauber vorbereitet. "
        )

        briefing_text = (
            "Guten Morgen. Hier ist dein Status-Update: Wetter und Systeme sind online, "
            f"der Speicher meldet {nas_label}. "
            "Deine Märkte zeigen heute folgende Bewegung auf dem geöffneten Cockpit-Tab. "
            f"{task_part}{alarm_part}{proj_part}"
            "Womit starten wir?"
        )

        if mem is not None and hasattr(mem, "mark_briefing_done_today"):
            try:
                mem.mark_briefing_done_today()
            except Exception:
                pass

        if tts is not None and hasattr(tts, "speak"):
            try:
                threading.Thread(target=tts.speak, args=(briefing_text,), daemon=True).start()
            except Exception:
                pass

        return {
            "ok": True,
            "briefing_text": briefing_text,
            "spoken": briefing_text,
            "opened_tabs": opened_tabs,
            "open_tasks_count": len(open_tasks),
            "active_alarms_count": len(alarms_active),
            "top_project": top_project,
        }

    # ------------------------------------------------------------------
    # MODUL 3: Multi-App End-to-End Pipelines
    # ------------------------------------------------------------------
    def run_research_to_desktop_pipeline(
        self,
        query: str,
        *,
        brain: Any = None,
        memory: Any = None,
        on_status: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        """
        Pipeline A: Recherche im echten Desktop-Browser -> Strukturierte Datei/Tabelle auf Desktop.
        """
        mem = memory or self._memory
        low = str(query or "").lower()
        want_table = any(k in low for k in ("tabelle", "csv", "excel", "preise", "vergleich", "kurse"))

        if on_status:
            on_status("🌐 Schritt 1/2: Führe sichtbare Live-Browser-Recherche aus...")

        summary_text = ""
        search_topic = re.sub(
            r"(?:recherchiere(?:\s+mir)?|im\s+browser|schreib(?:\s+mir)?|speichere|"
            r"zusammenfassung|tabelle|auf\s+den\s+desktop|als\s+datei)",
            " ",
            query,
            flags=re.IGNORECASE,
        ).strip()
        search_topic = re.sub(r"\s+", " ", search_topic) or query

        try:
            from agent_hand import VISUAL_HAND_AGENT

            browser_res = VISUAL_HAND_AGENT.visual_browser_search_and_read(
                query=search_topic,
                brain=brain,
                on_status=on_status,
                interactive_click=True,
            )
            summary_text = str(browser_res.get("summary") or browser_res.get("raw_extract") or "").strip()
        except Exception as exc:
            summary_text = f"Live-Browser-Recherche zu '{search_topic}' gestartet ({exc})."

        if not summary_text:
            summary_text = f"Live-Recherche zu '{search_topic}' im Browser geöffnet am {datetime.now().strftime('%d.%m.%Y %H:%M')}."

        if on_status:
            on_status("📝 Schritt 2/2: Erstelle Ergebnis-Datei auf dem Desktop & öffne sie sichtbar...")

        desk = _desktop_dir()
        stamp = datetime.now().strftime("%Y%m%d_%H%M")
        slug = _safe_filename(search_topic[:32], default="Recherche")

        if want_table:
            out_path = desk / f"JARVIS_Tabelle_{slug}_{stamp}.csv"
            rows = [
                ["Thema", "Wert / Zusammenfassung", "Quelle", "Zeitstempel"],
                [search_topic, summary_text.replace("\n", " "), "Live Desktop Browser", datetime.now().strftime("%Y-%m-%d %H:%M")],
            ]
            with out_path.open("w", encoding="utf-8-sig", newline="") as f:
                writer = csv.writer(f, delimiter=";")
                writer.writerows(rows)
            category, app_hint = "table", "Excel"
        else:
            out_path = desk / f"JARVIS_Recherche_{slug}_{stamp}.txt"
            content = (
                f"J.A.R.V.I.S. EXECUTIVE RECHERCHE-BERICHT\n"
                f"========================================\n"
                f"Thema     : {search_topic}\n"
                f"Erstellt  : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                f"ERGEBNIS AUS LIVE-BROWSER:\n"
                f"{summary_text}\n"
            )
            out_path.write_text(content, encoding="utf-8")
            category, app_hint = "document", "Editor"

        if mem is not None and hasattr(mem, "record_active_project"):
            mem.record_active_project(
                name=out_path.name,
                path=str(out_path),
                category=category,
                app_hint=app_hint,
            )

        _open_path_visibly(out_path, app_hint=app_hint)

        return {
            "ok": True,
            "output_path": str(out_path),
            "summary": summary_text,
            "spoken": (
                f"Chef, ich habe zu {search_topic} live im Browser recherchiert und die fertige "
                f"{'Tabelle' if want_table else 'Zusammenfassung'} als {out_path.name} auf deinem Desktop erstellt und geöffnet."
            ),
        }

    def run_project_setup_pipeline(
        self,
        request_text: str,
        *,
        project_name: str = "",
        category: str = "",
        brain: Any = None,
        memory: Any = None,
        on_status: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        """
        Pipeline B: Projekt-Setup auf Zuruf (Ordnerstruktur + Hauptprogramm + Asset-/Referenz-Tab).
        """
        mem = memory or self._memory
        low = str(request_text or "").lower()

        resolved_cat = category.lower().strip()
        if not resolved_cat:
            if any(k in low for k in ("video", "schnitt", "capcut", "davinci", "youtube", "reel")):
                resolved_cat = "video"
            elif any(k in low for k in ("cad", "3d", "druck", "konstruktion", "solidcraft", "onshape", "orcaslicer")):
                resolved_cat = "cad"
            else:
                resolved_cat = "code"

        if not project_name:
            name_m = re.search(
                r"(?:namens|name|projekt|für)\s+[\"']?([a-zA-ZäöüÄÖÜß0-9 _-]{2,40})[\"']?",
                request_text,
                flags=re.IGNORECASE,
            )
            if name_m:
                cand = name_m.group(1).strip()
                cand = re.sub(
                    r"\b(?:ein|eine|neues|projekt|videoschnitt|cad|programmier|setup|vor)\b",
                    "",
                    cand,
                    flags=re.IGNORECASE,
                ).strip()
                project_name = cand
        clean_name = _safe_filename(project_name, default=f"Jarvis_{resolved_cat.upper()}_Projekt")

        if on_status:
            on_status(f"🗂️ Richte neues {resolved_cat.upper()}-Projekt '{clean_name}' ein...")

        project_root = _desktop_dir() / "JARVIS_Projekte" / clean_name
        subfolder_map = {
            "video": ["01_Rohmaterial", "02_Musik_SFX", "03_Projektdateien", "04_Fertige_Exporte"],
            "cad": ["01_Skizzen_Referenzen", "02_CAD_Modelle", "03_STL_Exporte", "04_Druckprofile"],
            "code": ["src", "tests", "docs", "assets"],
        }
        for sub in subfolder_map.get(resolved_cat, ["assets", "work", "exports"]):
            (project_root / sub).mkdir(parents=True, exist_ok=True)

        readme = project_root / "README_Projekt.md"
        readme.write_text(
            f"# {clean_name}\n"
            f"- Kategorie: {resolved_cat.upper()}\n"
            f"- Erstellt durch J.A.R.V.I.S.: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n",
            encoding="utf-8",
        )

        domain_config = {
            "video": {
                "app": "CapCut",
                "fallback_app": "DaVinci Resolve",
                "url": "https://pixabay.com/music/",
            },
            "cad": {
                "app": "OrcaSlicer",
                "fallback_app": "Blender",
                "url": "https://www.printables.com/",
            },
            "code": {
                "app": "Visual Studio Code",
                "fallback_app": "Notepad",
                "url": "https://github.com/",
            },
        }[resolved_cat]

        launched_app = domain_config["app"]
        try:
            from app_indexer import APP_INDEXER

            ok_launch, _ = APP_INDEXER.launch_app(domain_config["app"])
            if not ok_launch:
                ok_fb, _ = APP_INDEXER.launch_app(domain_config["fallback_app"])
                launched_app = domain_config["fallback_app"] if ok_fb else domain_config["app"]
        except Exception:
            pass

        try:
            webbrowser.open_new_tab(domain_config["url"])
        except Exception:
            pass

        try:
            _open_path_visibly(project_root)
        except Exception:
            pass

        if mem is not None and hasattr(mem, "record_active_project"):
            mem.record_active_project(
                name=clean_name,
                path=str(project_root),
                category=resolved_cat,
                app_hint=launched_app,
                notes=f"Auto-Setup inkl. Ordner & Referenz-Tab ({domain_config['url']})",
            )

        return {
            "ok": True,
            "project_root": str(project_root),
            "category": resolved_cat,
            "launched_app": launched_app,
            "reference_url": domain_config["url"],
            "spoken": (
                f"Chef, das komplette Projekt-Setup für {clean_name} steht bereit. "
                f"Ordnerstruktur auf dem Desktop angelegt, {launched_app} gestartet "
                f"und passende Referenzen im Browser geöffnet."
            ),
        }


EXECUTIVE_ASSISTANT = ExecutiveAssistant()
