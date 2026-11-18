"""
Intelligente 2-Stufen Hybrid-Speicher-Engine (`nas_brain.py`) für Laptop-NVMe/RAM + Explorer-HDD-NAS:
- Stufe 1 (HOT TIER – 0ms Echtzeit-Speed auf Laptop-NVMe & im RAM):
  * `%LOCALAPPDATA%\\JarvisCommandCenter\\HotCache\\active_brain.db` (aktive SQLite- & 256-D-Vektor-Datenbank)
  * 1-FPS-Screenshot-Ringpuffer direkt im RAM (`collections.deque(maxlen=30)`)
  * Lokale Video-Proxy- & Schnitt-Arbeitskopien (`HotCache\\video_workspace` & `HotCache\\video_exports`)
  -> Die 4 Agenten blockieren NIEMALS, selbst wenn die mechanischen HDDs im NAS im Spin-Down/Ruhezustand sind!
- Stufe 2 (COLD TIER – Asynchroner Hintergrund-Sync auf das HDD-NAS im Windows-Explorer):
  * Automatische Erkennung verbundener Netzlaufwerke im Windows-Explorer (`psutil.disk_partitions(all=True)` / `win32wnet` / `GetDriveTypeW`)
  * Eigener Hintergrund-Thread (`NAS_Sync_Worker`) bündelt Erinnerungen, `skills_db.json`, Assets und fertige Video-Renders in großen sequentiellen Blöcken (8 MB Buffer – optimal für mechanische HDDs!) alle 60 Sekunden bzw. sofort nach Aufgabenabschluss.
  * Vorab-Staging (`stage_video_to_hot_cache`): Kopiert Rohvideos vom NAS mit maximaler sequentieller LAN-Rate in den lokalen NVMe-`HotCache`, schneidet dort mit voller SSD-Geschwindigkeit und speichert das fertige `.mp4` zurück auf das NAS im Windows-Explorer.
"""

from __future__ import annotations

from collections import deque
import contextlib
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import threading
import time
from typing import Any, Optional

from config import (
    discover_explorer_network_drives,
    get_hot_cache_dir,
    get_local_jarvis_brain_dir,
    resolve_nas_memory_path,
)

EMBEDDING_DIM = 256
SEQUENTIAL_HDD_BLOCK_SIZE = 8 * 1024 * 1024  # 8 MB sequentielle Schreibblöcke für mechanische NAS-HDDs
COLD_SYNC_INTERVAL_SEC = 60.0

DEFAULT_UI_SKILLS: dict[str, dict[str, Any]] = {
    "capcut_split_clip": {
        "id": "capcut_split_clip",
        "app_name": "CapCut",
        "skill_name": "Timeline Clip teilen (Blade/Cut)",
        "action_type": "hotkey",
        "shortcut": "ctrl+b",
        "x": None,
        "y": None,
        "description": "Teilt den ausgewählten Video-Clip auf der CapCut-Timeline exakt an der Abspielposition.",
        "screenshot": "",
        "updated_at": "2026-01-01 00:00:00",
    },
    "capcut_import_media": {
        "id": "capcut_import_media",
        "app_name": "CapCut",
        "skill_name": "Medien importieren",
        "action_type": "hotkey",
        "shortcut": "ctrl+i",
        "x": None,
        "y": None,
        "description": "Öffnet den Datei-Import-Dialog in CapCut.",
        "screenshot": "",
        "updated_at": "2026-01-01 00:00:00",
    },
    "capcut_export_video": {
        "id": "capcut_export_video",
        "app_name": "CapCut",
        "skill_name": "Video exportieren / rendern",
        "action_type": "hotkey",
        "shortcut": "ctrl+e",
        "x": None,
        "y": None,
        "description": "Öffnet das Export-Fenster in CapCut zum Rendern der fertigen MP4-Datei.",
        "screenshot": "",
        "updated_at": "2026-01-01 00:00:00",
    },
    "davinci_split_clip": {
        "id": "davinci_split_clip",
        "app_name": "DaVinci Resolve",
        "skill_name": "Razor / Blade Cut auf Timeline",
        "action_type": "hotkey",
        "shortcut": "ctrl+b",
        "x": None,
        "y": None,
        "description": "Schneidet den Clip unter dem Playhead in DaVinci Resolve.",
        "screenshot": "",
        "updated_at": "2026-01-01 00:00:00",
    },
    "davinci_import_media": {
        "id": "davinci_import_media",
        "app_name": "DaVinci Resolve",
        "skill_name": "Media Pool Import",
        "action_type": "hotkey",
        "shortcut": "ctrl+i",
        "x": None,
        "y": None,
        "description": "Importiert Videodateien in den DaVinci Resolve Media Pool.",
        "screenshot": "",
        "updated_at": "2026-01-01 00:00:00",
    },
    "premiere_razor_cut": {
        "id": "premiere_razor_cut",
        "app_name": "Adobe Premiere Pro",
        "skill_name": "Add Edit (Schnitt setzen)",
        "action_type": "hotkey",
        "shortcut": "ctrl+k",
        "x": None,
        "y": None,
        "description": "Fügt in Adobe Premiere Pro an der aktuellen Zeitmarke einen Schnitt ein.",
        "screenshot": "",
        "updated_at": "2026-01-01 00:00:00",
    },
    "explorer_focus_address": {
        "id": "explorer_focus_address",
        "app_name": "Windows Explorer",
        "skill_name": "Explorer Pfadleiste fokussieren",
        "action_type": "hotkey",
        "shortcut": "alt+d",
        "x": None,
        "y": None,
        "description": "Fokussiert die Adressleiste im Windows-Explorer zum direkten Springen auf NAS-Ordner.",
        "screenshot": "",
        "updated_at": "2026-01-01 00:00:00",
    },
    "browser_focus_url": {
        "id": "browser_focus_url",
        "app_name": "Browser",
        "skill_name": "Adresszeile fokussieren",
        "action_type": "hotkey",
        "shortcut": "ctrl+l",
        "x": None,
        "y": None,
        "description": "Markiert die URL-Adressleiste im Desktop-Webbrowser.",
        "screenshot": "",
        "updated_at": "2026-01-01 00:00:00",
    },
}


def compute_semantic_embedding(text: str, dim: int = EMBEDDING_DIM) -> list[float]:
    """
    Berechnet einen deterministischen, L2-normalisierten 256-D Semantik-Vektor
    über Wort-Unigramme, Wort-Bigramme und Zeichen-Trigramme.
    """
    vec = [0.0] * dim
    cleaned = re.sub(r"\s+", " ", (text or "").lower()).strip()
    if not cleaned:
        return vec

    words = re.findall(r"[a-z0-9äöüß]{2,}", cleaned)
    features: list[tuple[str, float]] = []
    for w in words:
        features.append((f"w:{w}", 2.0))
    for i in range(len(words) - 1):
        features.append((f"b:{words[i]}_{words[i + 1]}", 1.5))
    for tok in words:
        padded = f"#{tok}#"
        for i in range(max(0, len(padded) - 2)):
            features.append((f"c:{padded[i : i + 3]}", 0.6))

    for feat, weight in features:
        digest = hashlib.blake2b(feat.encode("utf-8", errors="ignore"), digest_size=8).digest()
        idx = int.from_bytes(digest[:4], "little") % dim
        sign = 1.0 if (digest[4] & 1) == 0 else -1.0
        vec[idx] += sign * weight

    norm = math.sqrt(sum(v * v for v in vec))
    if norm > 1e-9:
        vec = [round(v / norm, 6) for v in vec]
    return vec


def cosine_similarity(vec_a: list[float], vec_b: list[float]) -> float:
    """Berechnet die Kosinus-Ähnlichkeit zweier Vektoren."""
    if not vec_a or not vec_b or len(vec_a) != len(vec_b):
        return 0.0
    return float(sum(a * b for a, b in zip(vec_a, vec_b)))


def copy_file_sequential_hdd(src: Path, dst: Path, block_size: int = SEQUENTIAL_HDD_BLOCK_SIZE) -> Path:
    """
    Kopiert eine Datei in großen sequentiellen Blöcken (Standard: 8 MB Buffer).
    Verhindert Kopf-Springen (Head-Thrashing) auf mechanischen NAS-HDDs und nutzt die volle LAN-Bandbreite.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp_dst = dst.with_suffix(dst.suffix + ".part")
    with open(src, "rb", buffering=block_size) as f_in, open(tmp_dst, "wb", buffering=block_size) as f_out:
        shutil.copyfileobj(f_in, f_out, length=block_size)
    tmp_dst.replace(dst)
    return dst


class UnlimitedNASBrain:
    """
    2-Stufen Hybrid-Speicher-Engine (`UnlimitedNASBrain`):
    - STUFE 1 (HOT TIER): `%LOCALAPPDATA%\\JarvisCommandCenter\\HotCache` auf Laptop-NVMe + RAM-Ringpuffer (`0ms` Latenz).
    - STUFE 2 (COLD TIER): Asynchroner Hintergrund-Worker (`NAS_Sync_Worker`) synchronisiert gebündelt
      auf das Windows-Explorer-NAS (`Z:\\Jarvis_Archiv` / `\\\\NAS\\...` bzw. Cold-Archiv).
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._sync_event = threading.Event()
        self._stop_event = threading.Event()

        # Stufe 1: HOT TIER auf Laptop-NVMe SSD (%LOCALAPPDATA%\JarvisCommandCenter\HotCache)
        self._hot_cache_dir: Path = get_hot_cache_dir()
        self._hot_video_workspace: Path = self._hot_cache_dir / "video_workspace"
        self._hot_video_exports: Path = self._hot_cache_dir / "video_exports"
        self._hot_assets_dir: Path = self._hot_cache_dir / "assets"
        self._hot_screenshots_dir: Path = self._hot_cache_dir / "skill_screenshots"
        for d in (
            self._hot_cache_dir,
            self._hot_video_workspace,
            self._hot_video_exports,
            self._hot_assets_dir,
            self._hot_screenshots_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)

        # 1-FPS-Screenshot-Ringpuffer direkt im RAM (0ms Echtzeit-Zugriff für alle 4 Agenten)
        self._ram_screenshot_ring: deque[dict[str, Any]] = deque(maxlen=30)

        # Stufe 2: COLD TIER (Windows-Explorer-NAS mit HDDs + lokaler Cold-Fallback)
        self._local_dir: Path = get_local_jarvis_brain_dir()
        self._active_dir: Path = self._local_dir
        self._is_remote_nas: bool = False
        self._configured_path: str = ""
        self._explorer_drives: list[dict[str, str]] = []

        # Warteschlange für gebündelten Cold-Tier-Sync auf die NAS-HDDs
        self._dirty_cold_sync: bool = False
        self._sync_in_progress: bool = False
        self._last_cold_sync_ts: float = time.time()
        self._last_cold_sync_str: str = datetime.now().strftime("%H:%M:%S")
        self._pending_cold_files: list[tuple[Path, str]] = []
        self._chroma_client: Any = None
        self._chroma_collection: Any = None
        self._last_nas_probe_ts: float = 0.0
        self._cached_episodes_count: int = 0
        self._cached_knowledge_count: int = 0
        self._cached_skills_count: int = len(DEFAULT_UI_SKILLS)
        self._cached_free_gb: float = 0.0
        self._last_metrics_ts: float = 0.0

        self.refresh_storage_location(run_initial_sync=False)

        # Hintergrund-Thread: NAS_Sync_Worker (alle 60s oder bei Event)
        self._sync_worker_thread = threading.Thread(
            target=self._nas_sync_worker_loop,
            name="NAS_Sync_Worker",
            daemon=True,
        )
        self._sync_worker_thread.start()

    # ------------------------------------------------------------------
    # Pfad- & Hybrid-Tier-Eigenschaften
    # ------------------------------------------------------------------
    @property
    def hot_cache_dir(self) -> Path:
        self._hot_cache_dir.mkdir(parents=True, exist_ok=True)
        return self._hot_cache_dir

    @property
    def active_brain_db(self) -> Path:
        """Primäre Hot-Tier SQLite-Datenbank (`%LOCALAPPDATA%\\JarvisCommandCenter\\HotCache\\active_brain.db`)."""
        return self.hot_cache_dir / "active_brain.db"

    @property
    def active_dir(self) -> Path:
        return self._active_dir

    @property
    def is_remote_nas(self) -> bool:
        return self._is_remote_nas

    @property
    def skills_file(self) -> Path:
        """Hot-Tier `skills_db.json` für verzögerungsfreien Zugriff (wird im Hintergrund aufs NAS gespiegelt)."""
        return self.hot_cache_dir / "skills_db.json"

    @property
    def db_path(self) -> Path:
        """Hot-Tier SQLite-Pfad (`active_brain.db`)."""
        return self.active_brain_db

    def refresh_storage_location(self, run_initial_sync: bool = True, probe_network: bool = False) -> dict[str, Any]:
        """
        Initialisiert den lokalen NVMe `HotCache` (`active_brain.db`) sofort und verzögerungsfrei.
        Wenn kein NAS-Pfad konfiguriert ist, arbeitet Jarvis zu 100 % lokal im `%LOCALAPPDATA%`-Ordner,
        ohne Windows-Netzlaufwerke in blockierenden Schleifen abzufragen.
        """
        with self._lock:
            now_mono = time.monotonic()
            configured_env = (os.getenv("NAS_MEMORY_PATH") or "").strip()
            if configured_env or probe_network:
                if (now_mono - self._last_nas_probe_ts) >= 120.0 or probe_network:
                    self._last_nas_probe_ts = now_mono
                    self._explorer_drives = discover_explorer_network_drives() if probe_network else []
                    resolved_dir, is_nas, raw_cfg = resolve_nas_memory_path()
                    self._active_dir = resolved_dir
                    self._is_remote_nas = is_nas
                    self._configured_path = raw_cfg
            else:
                self._explorer_drives = []
                self._active_dir = self._local_dir
                self._is_remote_nas = False
                self._configured_path = str(self._local_dir)

            # Hot-Tier DBs initialisieren (active_brain.db + Kompatibilitäts-Spiegel jarvis_brain.sqlite3)
            self._init_sqlite_db(self.active_brain_db)
            self._init_sqlite_db(self._hot_cache_dir / "jarvis_brain.sqlite3")
            self._init_sqlite_db(self._local_dir / "jarvis_brain.sqlite3")
            self._init_sqlite_db(self._local_dir / "active_brain.db")

            self._ensure_skills_db(self.skills_file)
            self._ensure_skills_db(self._local_dir / "skills_db.json")

            # Falls auf dem Cold-NAS bereits Wissen/Skills existieren, beim Start einmalig in den HotCache laden
            try:
                cold_skills = self._active_dir / "skills_db.json"
                if cold_skills.is_file():
                    cold_data = json.loads(cold_skills.read_text(encoding="utf-8"))
                    if isinstance(cold_data, dict):
                        hot_data = self._ensure_skills_db(self.skills_file)
                        merged = {**cold_data, **hot_data}
                        self.skills_file.write_text(json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass

            self._init_optional_chromadb(self._hot_cache_dir)
            if run_initial_sync:
                self.trigger_async_cold_sync()
            return self.get_status()

    def _init_sqlite_db(self, sqlite_path: Path) -> None:
        try:
            sqlite_path.parent.mkdir(parents=True, exist_ok=True)
            with contextlib.closing(sqlite3.connect(str(sqlite_path), timeout=10.0)) as conn:
                conn.execute("PRAGMA journal_mode=WAL;")
                conn.execute("PRAGMA synchronous=NORMAL;")
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS episodes (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp TEXT NOT NULL,
                        role TEXT NOT NULL,
                        agent TEXT NOT NULL,
                        content TEXT NOT NULL,
                        tags TEXT DEFAULT '',
                        metadata_json TEXT DEFAULT '{}',
                        embedding_json TEXT DEFAULT '[]'
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS knowledge_archive (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp TEXT NOT NULL,
                        category TEXT NOT NULL,
                        title TEXT NOT NULL,
                        content TEXT NOT NULL,
                        source TEXT DEFAULT '',
                        metadata_json TEXT DEFAULT '{}',
                        embedding_json TEXT DEFAULT '[]'
                    )
                    """
                )
                conn.execute("CREATE INDEX IF NOT EXISTS idx_episodes_ts ON episodes(timestamp DESC);")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_knowledge_cat ON knowledge_archive(category);")
                conn.commit()
        except Exception as exc:
            print(f"[NASBrain SQLite-Init Hinweis] {exc}")

    def _init_optional_chromadb(self, base_dir: Path) -> None:
        try:
            import chromadb  # type: ignore

            chroma_dir = base_dir / "chroma_vectors"
            chroma_dir.mkdir(parents=True, exist_ok=True)
            self._chroma_client = chromadb.PersistentClient(path=str(chroma_dir))
            self._chroma_collection = self._chroma_client.get_or_create_collection(name="jarvis_unlimited_brain")
        except Exception:
            self._chroma_client = None
            self._chroma_collection = None

    def _ensure_skills_db(self, skills_path: Path) -> dict[str, dict[str, Any]]:
        skills_path.parent.mkdir(parents=True, exist_ok=True)
        data: dict[str, dict[str, Any]] = {}
        if skills_path.is_file():
            try:
                loaded = json.loads(skills_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
            except Exception:
                data = {}
        changed = False
        for k, default_val in DEFAULT_UI_SKILLS.items():
            if k not in data:
                data[k] = dict(default_val)
                changed = True
        if changed or not skills_path.is_file():
            try:
                skills_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass
        return data

    # ------------------------------------------------------------------
    # STUFE 1 (HOT TIER): 1-FPS-RAM-Ringpuffer & NVMe Video-Staging
    # ------------------------------------------------------------------
    def push_ram_screenshot(
        self,
        jpeg_bytes: bytes,
        cursor_x: int = 0,
        cursor_y: int = 0,
        label: str = "1-FPS-VISION",
    ) -> dict[str, Any]:
        """
        Legt den aktuellen 1-FPS-Screenshot im RAM-Ringpuffer (`maxlen=30`) ab.
        Kein Festplatten-/HDD-Zugriff -> 0 Millisekunden Latenz!
        """
        item = {
            "timestamp": time.time(),
            "time_str": datetime.now().strftime("%H:%M:%S"),
            "cursor_x": int(cursor_x),
            "cursor_y": int(cursor_y),
            "label": label,
            "size_bytes": len(jpeg_bytes or b""),
            "jpeg_bytes": jpeg_bytes,
        }
        with self._lock:
            self._ram_screenshot_ring.append(item)
        return item

    def get_latest_ram_screenshots(self, limit: int = 5) -> list[dict[str, Any]]:
        """Liefert die letzten `limit` Screenshots direkt aus dem RAM-Ringpuffer."""
        with self._lock:
            items = list(self._ram_screenshot_ring)[-max(1, limit) :]
        return items

    def stage_video_to_hot_cache(
        self,
        source_path: str | Path,
        on_status: Optional[Any] = None,
    ) -> Path:
        """
        Wenn Agent 2 ein Video schneiden soll, das auf dem Explorer-NAS (oder extern) liegt,
        kopiert diese Methode die Rohdatei vorab mit maximaler sequentieller LAN-Geschwindigkeit (8 MB Blöcke)
        in den lokalen `HotCache\\video_workspace` auf die Laptop-NVMe-SSD.
        """
        src = Path(str(source_path)).expanduser()
        if not src.is_file():
            raise FileNotFoundError(f"Quelldatei nicht gefunden: {src}")

        try:
            if src.resolve().parent == self._hot_video_workspace.resolve():
                return src
        except Exception:
            pass

        staged_target = self._hot_video_workspace / src.name
        if staged_target.is_file() and staged_target.stat().st_size == src.stat().st_size:
            return staged_target

        if callable(on_status):
            on_status(f"Kopiere Rohvideo '{src.name}' in lokalen NVMe-HotCache (8 MB Sequential Stream)...")
        return copy_file_sequential_hdd(src, staged_target, block_size=SEQUENTIAL_HDD_BLOCK_SIZE)

    def archive_video_to_cold_nas(
        self,
        rendered_hot_path: str | Path,
        async_sync: bool = False,
    ) -> Path:
        """
        Speichert ein im lokalen NVMe-HotCache fertig gerendertes Video (`.mp4`)
        in großen sequentiellen Blöcken zurück in den Explorer-NAS-Ordner (`video_renders`).
        """
        hot_file = Path(str(rendered_hot_path)).expanduser()
        if not hot_file.is_file():
            return hot_file

        cold_renders_dir = self._active_dir / "video_renders"
        cold_target = cold_renders_dir / hot_file.name

        if async_sync:
            with self._lock:
                self._pending_cold_files.append((hot_file, f"video_renders/{hot_file.name}"))
                self._dirty_cold_sync = True
            self.trigger_async_cold_sync()
            return cold_target

        try:
            copy_file_sequential_hdd(hot_file, cold_target, block_size=SEQUENTIAL_HDD_BLOCK_SIZE)
            with self._lock:
                self._last_cold_sync_ts = time.time()
                self._last_cold_sync_str = datetime.now().strftime("%H:%M:%S")
            return cold_target
        except Exception:
            local_fallback = self._local_dir / "video_renders" / hot_file.name
            copy_file_sequential_hdd(hot_file, local_fallback, block_size=SEQUENTIAL_HDD_BLOCK_SIZE)
            return local_fallback

    def register_asset_for_cold_sync(self, local_file: str | Path, relative_dest: str = "") -> None:
        """Reiht ein heruntergeladenes Asset oder Dokument für den gebündelten HDD-NAS-Sync ein."""
        p = Path(str(local_file)).expanduser()
        if not p.is_file():
            return
        rel = relative_dest.strip("/") if relative_dest else f"assets/{p.name}"
        with self._lock:
            self._pending_cold_files.append((p, rel))
            self._dirty_cold_sync = True
        self.trigger_async_cold_sync()

    # ------------------------------------------------------------------
    # STUFE 2 (COLD TIER): Asynchroner Hintergrund-Worker (`NAS_Sync_Worker`)
    # ------------------------------------------------------------------
    def trigger_async_cold_sync(self) -> None:
        """Signalisiert dem `NAS_Sync_Worker`, ausstehende HotCache-Änderungen gebündelt aufs NAS zu schreiben."""
        with self._lock:
            self._dirty_cold_sync = True
        self._sync_event.set()

    def sync_to_cold_nas_now(self) -> dict[str, Any]:
        """Führt sofort einen gebündelten, sequentiellen Cold-Tier-Sync vom NVMe-HotCache auf das NAS durch."""
        return self._perform_bundled_cold_sync(force=True)

    def _nas_sync_worker_loop(self) -> None:
        """
        Hintergrund-Thread (`NAS_Sync_Worker`):
        Schreibt alle 60 Sekunden bzw. nach Abschluss einer Aufgabe alle neuen Erinnerungen,
        `skills_db.json`, Assets und Video-Renders in großen sequentiellen Blöcken auf das HDD-NAS.
        """
        while not self._stop_event.is_set():
            triggered = self._sync_event.wait(timeout=COLD_SYNC_INTERVAL_SEC)
            if self._stop_event.is_set():
                break
            if triggered:
                self._sync_event.clear()
                # Kurzes Coalescing-Fenster ohne Thread-Starre
                if self._stop_event.wait(0.25):
                    break
            self._perform_bundled_cold_sync(force=False)

    def _perform_bundled_cold_sync(self, force: bool = False) -> dict[str, Any]:
        with self._lock:
            if not force and not self._dirty_cold_sync and not self._pending_cold_files:
                return {"ok": True, "synced": False, "last_sync": self._last_cold_sync_str}
            self._sync_in_progress = True
            pending_files = list(self._pending_cold_files)
            self._pending_cold_files.clear()
            self._dirty_cold_sync = False

        synced_items = 0
        try:
            cold_targets = {self._active_dir, self._local_dir}

            # 1. Konsistenter SQLite-Snapshot von HotCache\active_brain.db in temporäre Bundle-Datei
            snap_db = self._hot_cache_dir / "active_brain_sync_snapshot.db"
            try:
                with contextlib.closing(sqlite3.connect(str(self.active_brain_db), timeout=10.0)) as src_conn:
                    with contextlib.closing(sqlite3.connect(str(snap_db), timeout=10.0)) as dst_conn:
                        src_conn.backup(dst_conn)
            except Exception:
                snap_db = self.active_brain_db

            # 2. Sequentieller 8-MB-Block-Sync auf das Explorer-NAS & das lokale Cold-Archiv
            for target_root in cold_targets:
                try:
                    target_root.mkdir(parents=True, exist_ok=True)
                    if snap_db.is_file():
                        copy_file_sequential_hdd(snap_db, target_root / "active_brain.db")
                        copy_file_sequential_hdd(snap_db, target_root / "jarvis_brain.sqlite3")
                        synced_items += 1
                    if self.skills_file.is_file():
                        copy_file_sequential_hdd(self.skills_file, target_root / "skills_db.json")
                        synced_items += 1
                    for src_f, rel_dst in pending_files:
                        if src_f.is_file():
                            copy_file_sequential_hdd(src_f, target_root / rel_dst)
                            synced_items += 1
                except Exception:
                    pass
        finally:
            with self._lock:
                self._sync_in_progress = False
                self._last_cold_sync_ts = time.time()
                self._last_cold_sync_str = datetime.now().strftime("%H:%M:%S")

        return {
            "ok": True,
            "synced": True,
            "synced_items": synced_items,
            "last_sync": self._last_cold_sync_str,
            "cold_nas_path": str(self._active_dir),
        }

    # ------------------------------------------------------------------
    # 1. UI-Skill-Library (`skills_db.json`)
    # ------------------------------------------------------------------
    def learn_ui_skill(
        self,
        app_name: str,
        skill_name: str,
        action_type: str = "click",
        x: Optional[int] = None,
        y: Optional[int] = None,
        shortcut: str = "",
        description: str = "",
        screenshot_bytes: Optional[bytes] = None,
        coordinates: Optional[dict[str, int]] = None,
    ) -> dict[str, Any]:
        """
        Speichert einen gelernten UI-Bedienablauf (Koordinaten, Shortcut, Beschreibung und
        optionalen Screenshot) sofort im NVMe-HotCache (`skills_db.json`) und synchronisiert
        ihn asynchron über den `NAS_Sync_Worker` auf das HDD-NAS.
        """
        if coordinates and isinstance(coordinates, dict):
            if x is None and "x" in coordinates:
                x = int(coordinates["x"])
            if y is None and "y" in coordinates:
                y = int(coordinates["y"])
        clean_app = (app_name or "Desktop").strip()
        clean_skill = (skill_name or "UI-Aktion").strip()
        slug = re.sub(r"[^a-z0-9_]+", "_", f"{clean_app}_{clean_skill}".lower()).strip("_")
        if not slug:
            slug = f"skill_{int(time.time())}"

        screenshot_rel = ""
        if screenshot_bytes:
            fname = f"{slug}_{int(time.time())}.jpg"
            hot_shot = self._hot_screenshots_dir / fname
            try:
                hot_shot.write_bytes(screenshot_bytes)
                screenshot_rel = f"skill_screenshots/{fname}"
                with self._lock:
                    self._pending_cold_files.append((hot_shot, screenshot_rel))
            except Exception:
                pass

        entry = {
            "id": slug,
            "app_name": clean_app,
            "skill_name": clean_skill,
            "action_type": (action_type or "click").strip().lower(),
            "x": int(x) if x is not None else None,
            "y": int(y) if y is not None else None,
            "shortcut": (shortcut or "").strip().lower(),
            "description": (description or f"{clean_app}: {clean_skill}").strip(),
            "screenshot": screenshot_rel,
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

        with self._lock:
            skills = self._ensure_skills_db(self.skills_file)
            skills[slug] = entry
            try:
                self.skills_file.write_text(json.dumps(skills, indent=2, ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass
            self._dirty_cold_sync = True

        self.trigger_async_cold_sync()
        return entry

    def queue_cold_asset(self, hot_path: str | Path, rel_cold_path: str) -> None:
        """Reiht eine Asset-/Template-Datei in den nächsten gebündelten Cold-NAS-Sync ein."""
        src = Path(hot_path)
        if not src.is_file():
            return
        with self._lock:
            self._pending_cold_files.append((src, str(rel_cold_path).replace("\\", "/")))
            self._dirty_cold_sync = True
        self.trigger_async_cold_sync()

    def learn_recorded_workflow(
        self,
        app_name: str,
        skill_name: str,
        steps: list[dict[str, Any]],
        description: str = "",
    ) -> dict[str, Any]:
        """
        Speichert einen per `skill_recorder.py` aufgezeichneten Multi-Step-Workflow inklusive
        150x150-Button-Templates dauerhaft in `skills_db.json` (NVMe HotCache + Cold-NAS).
        """
        clean_app = (app_name or "Desktop").strip()
        clean_skill = (skill_name or "Workflow").strip()
        slug = re.sub(r"[^a-z0-9_]+", "_", f"{clean_app}_{clean_skill}".lower()).strip("_")
        if not slug:
            slug = f"workflow_{int(time.time())}"

        first_click = next((s for s in steps if s.get("type") == "click" and s.get("x") is not None), None)
        entry = {
            "id": slug,
            "app_name": clean_app,
            "skill_name": clean_skill,
            "action_type": "recorded_workflow",
            "x": int(first_click["x"]) if first_click else None,
            "y": int(first_click["y"]) if first_click else None,
            "shortcut": "",
            "description": (description or f"Gelernter Workflow '{clean_skill}' ({len(steps)} Schritte)").strip(),
            "steps": list(steps),
            "steps_count": len(steps),
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

        with self._lock:
            skills = self._ensure_skills_db(self.skills_file)
            skills[slug] = entry
            try:
                self.skills_file.write_text(json.dumps(skills, indent=2, ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass
            self._dirty_cold_sync = True

        self.trigger_async_cold_sync()
        return entry

    def get_all_ui_skills(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return self._ensure_skills_db(self.skills_file)

    def find_ui_skills(self, query: str, limit: int = 8, top_k: Optional[int] = None) -> list[dict[str, Any]]:
        """Durchsucht `skills_db.json` im NVMe-HotCache nach passenden gelernten Programm-Skills."""
        if top_k is not None:
            limit = int(top_k)
        skills = self.get_all_ui_skills()
        q_low = (query or "").strip().lower()
        if not q_low:
            return list(skills.values())[:limit]

        q_words = [w for w in re.split(r"\W+", q_low) if len(w) >= 2]
        scored: list[tuple[float, dict[str, Any]]] = []
        for slug, item in skills.items():
            hay = f"{slug} {item.get('app_name', '')} {item.get('skill_name', '')} {item.get('description', '')}".lower()
            score = 0.0
            if q_low in hay:
                score += 5.0
            for w in q_words:
                if w in hay:
                    score += 1.5
            if score > 0:
                scored.append((score, item))

        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [item for _, item in scored[:limit]]

    # ------------------------------------------------------------------
    # 2. Permanentes Episoden- & Wissens-Archiv (Hot NVMe SQLite + Cold NAS Sync)
    # ------------------------------------------------------------------
    def remember_episode(
        self,
        role: str = "assistant",
        content: str = "",
        agent: str = "OVERMIND",
        tags: str = "",
        metadata: Optional[dict[str, Any]] = None,
        *,
        agent_name: str = "",
        category: str = "",
        summary: str = "",
        details: Optional[dict[str, Any]] = None,
    ) -> int:
        """
        Schreibt eine Gesprächs-Episode oder Agenten-Aktion mit 0ms Verzögerung in
        `HotCache\\active_brain.db` und plant den asynchronen Cold-Tier-Sync aufs NAS.
        """
        if summary and not content:
            content = summary
        if agent_name:
            agent = agent_name
        if category and not tags:
            tags = category
        if details and metadata is None:
            metadata = details
        clean_content = (content or role or "").strip()
        if not clean_content:
            return 0

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        emb = compute_semantic_embedding(clean_content)
        emb_json = json.dumps(emb)
        meta_json = json.dumps(metadata or {}, ensure_ascii=False)
        row_id = 0

        with self._lock:
            hot_targets = [
                self.active_brain_db,
                self._hot_cache_dir / "jarvis_brain.sqlite3",
            ]
            for db_p in hot_targets:
                try:
                    with contextlib.closing(sqlite3.connect(str(db_p), timeout=8.0)) as conn:
                        cur = conn.execute(
                            """
                            INSERT INTO episodes (timestamp, role, agent, content, tags, metadata_json, embedding_json)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                            """,
                            (now_str, role, agent, clean_content, tags, meta_json, emb_json),
                        )
                        if db_p == self.active_brain_db:
                            row_id = int(cur.lastrowid or 0)
                        conn.commit()
                except Exception:
                    pass

            if self._chroma_collection is not None and row_id > 0:
                try:
                    self._chroma_collection.upsert(
                        ids=[f"ep_{row_id}_{int(time.time()*1000)}"],
                        documents=[clean_content],
                        metadatas=[{"type": "episode", "role": role, "agent": agent, "timestamp": now_str}],
                    )
                except Exception:
                    pass

            self._dirty_cold_sync = True

        self.trigger_async_cold_sync()
        return row_id

    def store_knowledge(
        self,
        category: str = "general",
        title: str = "",
        content: str = "",
        source: str = "",
        metadata: Optional[dict[str, Any]] = None,
        *,
        topic: str = "",
        tags: Optional[Any] = None,
    ) -> int:
        """
        Speichert Web-Recherchen, Finanzanalysen, Videoschnitt-Daten oder Projekt-Wissen
        sofort in `HotCache\\active_brain.db` und synchronisiert sie im Hintergrund aufs HDD-NAS.
        """
        if topic and not title:
            title = topic
        if tags and metadata is None:
            metadata = {"tags": tags}
        clean_content = (content or "").strip()
        if not clean_content:
            return 0

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        clean_cat = (category or "general").strip().lower()
        clean_title = (title or clean_content[:60]).strip()
        emb = compute_semantic_embedding(f"{clean_title} {clean_content}")
        emb_json = json.dumps(emb)
        meta_json = json.dumps(metadata or {}, ensure_ascii=False)
        row_id = 0

        with self._lock:
            hot_targets = [
                self.active_brain_db,
                self._hot_cache_dir / "jarvis_brain.sqlite3",
            ]
            for db_p in hot_targets:
                try:
                    with contextlib.closing(sqlite3.connect(str(db_p), timeout=8.0)) as conn:
                        cur = conn.execute(
                            """
                            INSERT INTO knowledge_archive (timestamp, category, title, content, source, metadata_json, embedding_json)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                            """,
                            (now_str, clean_cat, clean_title, clean_content, source, meta_json, emb_json),
                        )
                        if db_p == self.active_brain_db:
                            row_id = int(cur.lastrowid or 0)
                        conn.commit()
                except Exception:
                    pass

            if self._chroma_collection is not None and row_id > 0:
                try:
                    self._chroma_collection.upsert(
                        ids=[f"kn_{row_id}_{int(time.time()*1000)}"],
                        documents=[f"{clean_title}: {clean_content}"],
                        metadatas=[{"type": "knowledge", "category": clean_cat, "title": clean_title, "timestamp": now_str}],
                    )
                except Exception:
                    pass

            self._dirty_cold_sync = True

        self.trigger_async_cold_sync()
        return row_id

    def search_memory(
        self,
        query: str,
        top_k: int = 6,
        category: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """
        Hybride Volltext- & 256-D-Vektor-Suche über `HotCache\\active_brain.db` (0ms NVMe-Speed).
        """
        clean_q = (query or "").strip()
        if not clean_q:
            return []

        q_vec = compute_semantic_embedding(clean_q)
        q_words = [w for w in re.findall(r"[a-z0-9äöüß]{2,}", clean_q.lower())]
        results: list[tuple[float, dict[str, Any]]] = []

        with self._lock:
            try:
                with contextlib.closing(sqlite3.connect(str(self.active_brain_db), timeout=8.0)) as conn:
                    conn.row_factory = sqlite3.Row

                    if category:
                        kn_rows = conn.execute(
                            "SELECT * FROM knowledge_archive WHERE category = ? ORDER BY id DESC LIMIT 400",
                            (category.lower(),),
                        ).fetchall()
                    else:
                        kn_rows = conn.execute(
                            "SELECT * FROM knowledge_archive ORDER BY id DESC LIMIT 400"
                        ).fetchall()

                    for r in kn_rows:
                        title_str = str(r["title"] or "")
                        content_str = str(r["content"] or "")
                        hay = f"{title_str} {content_str}".lower()
                        try:
                            emb = json.loads(r["embedding_json"] or "[]")
                        except Exception:
                            emb = []
                        sim = cosine_similarity(q_vec, emb) if emb else 0.0
                        kw_hits = sum(1 for w in q_words if w in hay)
                        score = sim * 3.0 + (kw_hits * 0.8)
                        if score >= 0.35 or kw_hits > 0:
                            results.append(
                                (
                                    score,
                                    {
                                        "type": "knowledge",
                                        "category": r["category"],
                                        "title": title_str,
                                        "content": content_str[:500],
                                        "summary": f"{title_str}: {content_str[:300]}",
                                        "source": r["source"],
                                        "timestamp": r["timestamp"],
                                        "score": round(score, 3),
                                    },
                                )
                            )

                    if not category:
                        ep_rows = conn.execute(
                            "SELECT * FROM episodes ORDER BY id DESC LIMIT 300"
                        ).fetchall()
                        for r in ep_rows:
                            content_str = str(r["content"] or "")
                            hay = content_str.lower()
                            try:
                                emb = json.loads(r["embedding_json"] or "[]")
                            except Exception:
                                emb = []
                            sim = cosine_similarity(q_vec, emb) if emb else 0.0
                            kw_hits = sum(1 for w in q_words if w in hay)
                            score = sim * 2.5 + (kw_hits * 0.7)
                            if score >= 0.40 or kw_hits > 0:
                                results.append(
                                    (
                                        score,
                                        {
                                            "type": "episode",
                                            "category": r["tags"] or "episode",
                                            "role": r["role"],
                                            "agent": r["agent"],
                                            "content": content_str[:400],
                                            "summary": content_str[:400],
                                            "timestamp": r["timestamp"],
                                            "score": round(score, 3),
                                        },
                                    )
                                )
            except Exception as exc:
                print(f"[NASBrain Search Hinweis] {exc}")

        results.sort(key=lambda item: item[0], reverse=True)
        return [r for _, r in results[: max(1, top_k)]]

    def list_cold_nas_videos(self) -> list[Path]:
        """Listet Rohvideos und gerenderte Videos auf dem Explorer-NAS sowie im HotCache auf."""
        found: list[Path] = []
        search_dirs = [
            self._active_dir,
            self._active_dir / "video_renders",
            self._active_dir / "raw_videos",
            self._hot_video_workspace,
            self._hot_video_exports,
        ]
        seen: set[str] = set()
        for d in search_dirs:
            try:
                if not d.is_dir():
                    continue
                for f in d.iterdir():
                    if f.is_file() and f.suffix.lower() in (".mp4", ".mov", ".mkv", ".avi", ".webm"):
                        key = str(f.resolve()).lower()
                        if key not in seen:
                            seen.add(key)
                            found.append(f)
            except Exception:
                pass
        return found

    def build_overmind_memory_context(self, query: str) -> str:
        """
        Erstellt für Agent 1 (`OVERMIND`) einen kompakten Wissens- & UI-Skill-Kontextblock
        aus dem lokalen NVMe-HotCache und dem Explorer-NAS-Index.
        """
        memories = self.search_memory(query, top_k=4)
        skills = self.find_ui_skills(query, limit=4)
        lines: list[str] = []

        if skills:
            lines.append("GELERNTE UI-SKILLS AUS `skills_db.json` (NVMe HotCache):")
            for sk in skills:
                coord_str = f" @ [{sk['x']}, {sk['y']}]" if sk.get("x") and sk.get("y") else ""
                sc_str = f" (Shortcut: {sk['shortcut'].upper()})" if sk.get("shortcut") else ""
                lines.append(f"- [{sk.get('app_name')}] {sk.get('skill_name')}{sc_str}{coord_str}: {sk.get('description')}")

        if memories:
            lines.append("RELEVANTES WISSEN AUS DEM HYBRID-GEDÄCHTNIS (`active_brain.db` + NAS):")
            for mem in memories:
                if mem["type"] == "knowledge":
                    lines.append(f"- [{mem['category'].upper()}] {mem['title']}: {mem['content'][:220]}")
                else:
                    lines.append(f"- [EPISODE {mem['timestamp']}] ({mem['agent']}): {mem['content'][:200]}")

        return "\n".join(lines) if lines else "Noch keine spezifischen Vorab-Einträge zu diesem Auftrag im Hybrid-NAS-Gehirn."

    def sync_project_knowledge_graph(self, graph_snapshot: dict[str, Any]) -> int:
        """
        Persistiert den Second-Brain Projekt- & Datei-Wissensgraphen im lokalen NVMe-HotCache
        und synchronisiert ihn asynchron auf das HDD-NAS.
        """
        if not isinstance(graph_snapshot, dict):
            return 0
        recent_files = list(graph_snapshot.get("recent_files", []) or [])
        active_projects = list(graph_snapshot.get("active_projects", []) or [])
        summary_payload = {
            "scanned_at": graph_snapshot.get("scanned_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            "root_count": int(graph_snapshot.get("root_count", 0)),
            "file_count": len(recent_files),
            "active_projects": active_projects[:25],
            "recent_files": recent_files[:60],
        }
        graph_file = self._hot_cache_dir / "project_graph.json"
        try:
            graph_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = graph_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(summary_payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(graph_file)
            with self._lock:
                self._dirty_cold_sync = True
        except Exception:
            pass

        highlights = [
            f"{p.get('name', '?')} ({p.get('category', 'general')}): {p.get('path', '')}"
            for p in active_projects[:12]
        ]
        if not highlights:
            highlights = [
                f"{f.get('name', '?')} [{f.get('category', 'file')}] -> {f.get('path', '')}"
                for f in recent_files[:12]
            ]
        if not highlights:
            return 0
        return self.store_knowledge(
            category="project_graph",
            title=f"Second-Brain Projekt-Graph ({len(recent_files)} aktuelle Dateien)",
            content=" | ".join(highlights),
            source="ProjectKnowledgeGraph",
            metadata={"scanned_at": summary_payload["scanned_at"], "count": len(recent_files)},
        )

    def get_project_context_summary(self, limit: int = 8) -> dict[str, Any]:
        """Lädt den zuletzt persistierten Projekt-Wissensgraphen direkt aus dem NVMe-HotCache."""
        graph_file = self._hot_cache_dir / "project_graph.json"
        if graph_file.exists():
            try:
                data = json.loads(graph_file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return {
                        "scanned_at": data.get("scanned_at", ""),
                        "file_count": int(data.get("file_count", 0)),
                        "active_projects": list(data.get("active_projects", []))[:limit],
                        "recent_files": list(data.get("recent_files", []))[:limit],
                    }
            except Exception:
                pass
        return {"scanned_at": "", "file_count": 0, "active_projects": [], "recent_files": []}

    def _refresh_cached_metrics_locked(self, force: bool = False) -> None:
        now_mono = time.monotonic()
        if not force and (now_mono - self._last_metrics_ts) < 5.0:
            return
        self._last_metrics_ts = now_mono
        try:
            with contextlib.closing(sqlite3.connect(str(self.active_brain_db), timeout=1.5)) as conn:
                self._cached_episodes_count = int(conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0])
                self._cached_knowledge_count = int(conn.execute("SELECT COUNT(*) FROM knowledge_archive").fetchone()[0])
        except Exception:
            pass
        try:
            self._cached_skills_count = len(self._ensure_skills_db(self.skills_file))
        except Exception:
            pass
        try:
            disk_target = self._local_dir if not self._is_remote_nas else self._active_dir
            usage = shutil.disk_usage(str(disk_target))
            self._cached_free_gb = round(usage.free / (1024**3), 1)
        except Exception:
            pass

    def close(self) -> None:
        self._stop_event.set()
        self._sync_event.set()

    def get_status(self) -> dict[str, Any]:
        """
        Liefert den Live-Status des 2-Stufen-Hybrid-Speichers verzögerungsfrei (mit 5s-Metrik-Cache):
        `NVMe HOT-CACHE: OK // NAS HDD: SYNCED`
        """
        with self._lock:
            self._refresh_cached_metrics_locked(force=False)
            episodes_count = self._cached_episodes_count
            knowledge_count = self._cached_knowledge_count
            skills_count = self._cached_skills_count
            free_gb = self._cached_free_gb

            storage_mode = "NAS" if self._is_remote_nas else "LOCAL"
            nas_state_str = "SYNCING" if self._sync_in_progress else "SYNCED"
            status_label = f"NVMe HOT-CACHE: OK // NAS HDD: {nas_state_str}"

            return {
                "connected": True,
                "status_label": status_label,
                "hybrid_label": status_label,
                "legacy_label": "NAS BRAIN: CONNECTED",
                "hot_cache_ok": True,
                "nas_hdd_state": nas_state_str,
                "storage_mode": storage_mode,
                "using_nas": self._is_remote_nas,
                "hot_cache_path": str(self._hot_cache_dir),
                "hot_db_path": str(self.active_brain_db),
                "active_path": str(self._active_dir),
                "cold_nas_path": str(self._active_dir),
                "configured_path": self._configured_path,
                "explorer_drives": list(self._explorer_drives),
                "ram_ring_buffer_count": len(self._ram_screenshot_ring),
                "last_cold_sync": self._last_cold_sync_str,
                "skills_file": str(self.skills_file),
                "skills_db": str(self.skills_file),
                "db_path": str(self.active_brain_db),
                "sqlite_db": str(self.active_brain_db),
                "episodes_count": episodes_count,
                "knowledge_count": knowledge_count,
                "skills_count": skills_count,
                "ui_skills_count": skills_count,
                "free_gb": free_gb,
            }


# Globale Singleton-Instanz des 2-Stufen Hybrid-Speicher-Gehirns (NVMe HotCache + Explorer HDD-NAS)
NAS_BRAIN = UnlimitedNASBrain()
