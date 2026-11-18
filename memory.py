"""Lokales Langzeitgedächtnis (memory.json) für Fakten, Vorlieben und Projekte."""

from __future__ import annotations

from datetime import date, datetime
import json
import os
from pathlib import Path
import re
import threading
from typing import Any, Optional

from config import get_archive_subdir, get_user_data_dir, is_frozen

try:
    from mem0 import Memory as Mem0Memory
except ImportError:  # Lokaler JSON-Fallback
    Mem0Memory = None  # type: ignore[assignment,misc]


class LongTermMemory:
    """Speichert Fakten und Projektnotizen dauerhaft in memory.json (plus optionalem mem0ai)."""

    def __init__(self, path: str | Path = "") -> None:
        raw_path = str(path or "").strip()
        if not raw_path or (raw_path == "memory.json" and is_frozen()):
            self.path = get_user_data_dir() / "memory.json"
        else:
            self.path = Path(raw_path)
        self._lock = threading.Lock()
        self._data: dict[str, Any] = self._load()
        self._mem0 = self._init_mem0()
        if self.path.exists():
            self.backup_to_archive()

    def _init_mem0(self) -> Optional[Any]:
        if Mem0Memory is None or not os.getenv("OPENAI_API_KEY", "").strip():
            return None
        try:
            return Mem0Memory()
        except Exception:
            return None

    def _default_data(self) -> dict[str, Any]:
        return {
            "facts": [],
            "projects": [],
            "last_briefing_date": "",
            "active_projects": [],
            "user_preferences": {
                "preferred_apps": {
                    "cad": "Onshape / SolidCraft",
                    "slicer": "OrcaSlicer",
                    "video": "CapCut",
                    "code": "Visual Studio Code",
                    "browser": "Google Chrome / Edge",
                },
                "preferred_sites": [
                    "https://www.tradingview.com/",
                    "https://www.tagesschau.de/",
                ],
                "working_hours": "08:00 - 22:00",
                "habits": [],
            },
            "open_tasks": [],
        }

    def _load(self) -> dict[str, Any]:
        defaults = self._default_data()
        if not self.path.exists():
            return defaults
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                raw.setdefault("facts", [])
                raw.setdefault("projects", [])
                raw.setdefault("last_briefing_date", "")
                raw.setdefault("active_projects", [])
                prefs = raw.setdefault("user_preferences", {})
                if not isinstance(prefs, dict):
                    raw["user_preferences"] = dict(defaults["user_preferences"])
                else:
                    for pk, pv in defaults["user_preferences"].items():
                        prefs.setdefault(pk, pv)
                raw.setdefault("open_tasks", [])
                return raw
        except Exception:
            pass
        return defaults

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(".tmp")
        tmp_path.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp_path.replace(self.path)
        self._write_backup_locked()

    def _write_backup_locked(self) -> None:
        try:
            backup_dir, _ = get_archive_subdir("memory_backups")
            serialized = json.dumps(self._data, ensure_ascii=False, indent=2)
            (backup_dir / "memory_latest.json").write_text(serialized, encoding="utf-8")
            daily_name = f"memory_{date.today().isoformat()}.json"
            (backup_dir / daily_name).write_text(serialized, encoding="utf-8")
        except Exception:
            pass

    def backup_to_archive(self) -> None:
        """Schreibt ein System-Backup von memory.json in das aktive Archiv (NAS oder Lokal)."""
        with self._lock:
            self._write_backup_locked()

    def remember(self, text: str, category: str = "fact") -> str:
        """Speichert einen neuen Fakt oder eine Projektnotiz dauerhaft."""
        cleaned = re.sub(r"\s+", " ", text).strip()
        if not cleaned:
            return ""

        bucket = "projects" if category == "project" else "facts"
        with self._lock:
            items: list[dict[str, str]] = self._data.setdefault(bucket, [])
            if not any(item.get("text", "").lower() == cleaned.lower() for item in items):
                items.append(
                    {
                        "text": cleaned,
                        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                    }
                )
                self._save()
        if self._mem0 is not None:
            try:
                self._mem0.add(cleaned, user_id="chef", metadata={"category": category})
            except Exception:
                pass
        return cleaned

    def recall(self, query: str = "", limit: int = 8) -> list[str]:
        """Sucht passende Erinnerungen anhand von Stichwörtern oder liefert die neuesten."""
        with self._lock:
            all_entries: list[str] = []
            for item in self._data.get("facts", []):
                if item.get("text"):
                    all_entries.append(item["text"])
            for item in self._data.get("projects", []):
                if item.get("text"):
                    all_entries.append(f"Projektnotiz ({item.get('created_at', '')}): {item['text']}")

        if not all_entries:
            return []

        tokens = [
            tok for tok in re.findall(r"[a-zA-ZäöüÄÖÜß0-9]{3,}", query.lower())
            if tok not in {"was", "weißt", "noch", "über", "alles", "mein", "meine", "jarvis"}
        ]
        if not tokens:
            return all_entries[-limit:]

        scored: list[tuple[int, str]] = []
        for entry in all_entries:
            low = entry.lower()
            score = sum(1 for tok in tokens if tok in low)
            if score > 0:
                scored.append((score, entry))

        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [entry for _, entry in scored[:limit]]

    def get_context_block(self, max_facts: int = 10, max_projects: int = 5) -> str:
        """Formatiert gespeicherte Fakten, Projekte, Präferenzen und Aufgaben als Kontext für brain.py."""
        with self._lock:
            facts = [i["text"] for i in self._data.get("facts", [])[-max_facts:] if i.get("text")]
            projects = [
                f"{i.get('created_at', '')}: {i['text']}"
                for i in self._data.get("projects", [])[-max_projects:]
                if i.get("text")
            ]
            active_projects = list(self._data.get("active_projects", []))[:5]
            open_tasks = [
                t for t in self._data.get("open_tasks", [])
                if str(t.get("status", "open")).lower() != "done"
            ][:5]
            prefs = dict(self._data.get("user_preferences", {}))

        parts: list[str] = []
        if facts:
            parts.append("Bekannte Fakten über den Chef: " + "; ".join(facts) + ".")
        if projects:
            parts.append("Laufende Werkstatt- und Projektnotizen: " + "; ".join(projects) + ".")
        if active_projects:
            proj_desc = ", ".join(
                f"{p.get('name', '?')} ({p.get('category', 'projekt')}, {p.get('path', '')})"
                for p in active_projects
            )
            parts.append("Aktive Desktop-/Second-Brain-Projekte: " + proj_desc + ".")
        if open_tasks:
            task_desc = "; ".join(
                f"[{t.get('priority', 'normal')}] {t.get('title', '')}"
                for t in open_tasks
                if t.get("title")
            )
            if task_desc:
                parts.append("Offene Aufgaben / To-Dos des Chefs: " + task_desc + ".")
        if prefs.get("preferred_apps"):
            apps_str = ", ".join(f"{k}: {v}" for k, v in list(prefs["preferred_apps"].items())[:6])
            parts.append(f"Bevorzugte Programme ({apps_str}).")
        return "\n".join(parts)

    def get_active_projects(self, limit: int = 15) -> list[dict[str, Any]]:
        """Gibt die zuletzt aktiven Projekte sortiert nach Aktualität zurück."""
        with self._lock:
            items = list(self._data.get("active_projects", []))
        return items[: max(1, limit)]

    def record_active_project(
        self,
        *,
        name: str,
        path: str,
        category: str = "general",
        app_hint: str = "",
        modified_at: str = "",
        notes: str = "",
    ) -> dict[str, Any]:
        """Speichert oder aktualisiert ein aktives Projekt in `memory.json`."""
        clean_path = str(path or "").strip()
        clean_name = str(name or Path(clean_path).name or "Unbenanntes Projekt").strip()
        now_stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        entry = {
            "name": clean_name,
            "path": clean_path,
            "category": str(category or "general").strip().lower(),
            "app_hint": str(app_hint or "").strip(),
            "modified_at": str(modified_at or now_stamp).strip(),
            "last_opened": now_stamp,
            "notes": str(notes or "").strip(),
        }
        with self._lock:
            projects: list[dict[str, Any]] = list(self._data.get("active_projects", []))
            projects = [
                p for p in projects
                if str(p.get("path", "")).strip().lower() != clean_path.lower()
                and str(p.get("name", "")).strip().lower() != clean_name.lower()
            ]
            projects.insert(0, entry)
            self._data["active_projects"] = projects[:40]
            self._save()
        return entry

    def get_user_preferences(self) -> dict[str, Any]:
        """Liefert das strukturierte Präferenz- und Gewohnheitsprofil des Nutzers."""
        with self._lock:
            return json.loads(json.dumps(self._data.get("user_preferences", {}), ensure_ascii=False))

    def update_user_preferences(self, updates: dict[str, Any]) -> dict[str, Any]:
        """Aktualisiert Nutzerpräferenzen (Programme, Websites, Arbeitszeiten, Gewohnheiten)."""
        if not isinstance(updates, dict):
            return self.get_user_preferences()
        with self._lock:
            prefs = self._data.setdefault("user_preferences", {})
            for key, val in updates.items():
                if key == "preferred_apps" and isinstance(val, dict):
                    apps = prefs.setdefault("preferred_apps", {})
                    if isinstance(apps, dict):
                        apps.update({str(k): str(v) for k, v in val.items()})
                    else:
                        prefs["preferred_apps"] = dict(val)
                elif key == "preferred_sites" and isinstance(val, list):
                    existing = [str(x) for x in prefs.get("preferred_sites", [])]
                    for site in val:
                        s = str(site).strip()
                        if s and s not in existing:
                            existing.append(s)
                    prefs["preferred_sites"] = existing[:20]
                elif key == "habits" and isinstance(val, list):
                    existing_h = [str(x) for x in prefs.get("habits", [])]
                    for h in val:
                        hs = str(h).strip()
                        if hs and hs not in existing_h:
                            existing_h.append(hs)
                    prefs["habits"] = existing_h[:30]
                else:
                    prefs[str(key)] = val
            self._save()
            return json.loads(json.dumps(prefs, ensure_ascii=False))

    def get_open_tasks(self, include_done: bool = False) -> list[dict[str, Any]]:
        """Gibt alle offenen (oder alle) Aufgaben zurück."""
        with self._lock:
            tasks = list(self._data.get("open_tasks", []))
        if include_done:
            return tasks
        return [t for t in tasks if str(t.get("status", "open")).lower() != "done"]

    def add_open_task(
        self,
        title: str,
        *,
        priority: str = "normal",
        due: str = "",
        category: str = "general",
    ) -> dict[str, Any]:
        """Fügt eine neue Aufgabe / Erinnerung hinzu (duplikatgeschützt)."""
        clean_title = str(title or "").strip()
        if not clean_title:
            raise ValueError("Aufgabentitel darf nicht leer sein.")
        with self._lock:
            tasks = self._data.setdefault("open_tasks", [])
            for existing in tasks:
                if (
                    str(existing.get("title", "")).strip().lower() == clean_title.lower()
                    and str(existing.get("status", "open")).lower() != "done"
                ):
                    return dict(existing)
            item = {
                "id": f"task_{int(datetime.now().timestamp() * 1000)}",
                "title": clean_title,
                "priority": str(priority or "normal").strip().lower(),
                "due": str(due or "").strip(),
                "category": str(category or "general").strip().lower(),
                "status": "open",
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            }
            tasks.insert(0, item)
            self._data["open_tasks"] = tasks[:80]
            self._save()
            return dict(item)

    def complete_open_task(self, task_id_or_title: str) -> bool:
        """Markiert eine Aufgabe als erledigt."""
        needle = str(task_id_or_title or "").strip().lower()
        if not needle:
            return False
        changed = False
        with self._lock:
            for item in self._data.get("open_tasks", []):
                tid = str(item.get("id", "")).lower()
                title = str(item.get("title", "")).lower()
                if needle == tid or needle in title:
                    item["status"] = "done"
                    item["completed_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
                    changed = True
            if changed:
                self._save()
        return changed

    def get_executive_profile(self) -> dict[str, Any]:
        """Liefert das vollständige Second-Brain-Profil (Projekte, Präferenzen, Aufgaben)."""
        return {
            "active_projects": self.get_active_projects(limit=20),
            "user_preferences": self.get_user_preferences(),
            "open_tasks": self.get_open_tasks(include_done=False),
            "facts": list(self._data.get("facts", []))[-15:],
        }

    def was_briefing_done_today(self) -> bool:
        today_str = date.today().isoformat()
        with self._lock:
            return self._data.get("last_briefing_date") == today_str

    def mark_briefing_done_today(self) -> None:
        today_str = date.today().isoformat()
        with self._lock:
            self._data["last_briefing_date"] = today_str
            self._save()

    def get_all_memories(self) -> list[dict[str, Any]]:
        """Liefert alle gespeicherten Fakten und Projektnotizen für den Mobile-Sync."""
        with self._lock:
            result: list[dict[str, Any]] = []
            for item in self._data.get("facts", []):
                if item.get("text"):
                    result.append({
                        "text": item["text"],
                        "category": "fact",
                        "created_at": item.get("created_at", ""),
                    })
            for item in self._data.get("projects", []):
                if item.get("text"):
                    result.append({
                        "text": item["text"],
                        "category": "project",
                        "created_at": item.get("created_at", ""),
                    })
            return result

    def merge_memories(self, incoming: list[dict[str, Any]]) -> tuple[int, int]:
        """Führt mobile Erinnerungen duplikatfrei zusammen und persistiert sie."""
        if not incoming:
            return 0, len(self.get_all_memories())

        added = 0
        with self._lock:
            facts = self._data.setdefault("facts", [])
            projects = self._data.setdefault("projects", [])

            existing_facts = {f.get("text", "").strip().lower() for f in facts if f.get("text")}
            existing_projects = {p.get("text", "").strip().lower() for p in projects if p.get("text")}

            for item in incoming:
                text = str(item.get("text", "")).strip()
                if not text:
                    continue
                low_text = text.lower()
                category = str(item.get("category", "fact")).lower()
                stamp = str(item.get("created_at", "")).strip() or datetime.now().strftime("%Y-%m-%d %H:%M")

                if category == "project":
                    if low_text not in existing_projects:
                        projects.append({"text": text, "created_at": stamp})
                        existing_projects.add(low_text)
                        added += 1
                else:
                    if low_text not in existing_facts:
                        facts.append({"text": text, "created_at": stamp})
                        existing_facts.add(low_text)
                        added += 1

            if added > 0:
                self._save()

        return added, len(self.get_all_memories())
