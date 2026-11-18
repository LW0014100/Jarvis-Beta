# -*- coding: utf-8 -*-
"""
JARVIS Autonomer Webbrowser-App-Beschaffer & Installer (app_installer_agent.py)
================================================================================
Wenn für einen Auftrag (z. B. "Animiere mir dieses CAD-Modell", "Bearbeite diese
Audiodatei", "Öffne Blender") noch keine passende App lokal installiert ist:
1. Bedarfs-Erkennung & Pflicht-Rückfrage (`pending_install_task`) mit Angabe des
   offiziellen App-Namens und der offiziellen Hersteller-Domain.
2. Erst nach Nutzer-Bestätigung ("Ja", "Mach das", "Starte", ...) öffnet Jarvis
   sichtbar den Desktop-Webbrowser auf der offiziellen Download-Seite.
3. Live-Überwachung von `%USERPROFILE%\\Downloads` (inkl. `.crdownload`/`.part`/`.tmp`)
   im 1-Sekunden-Takt mit HUD-Status `⬇️ DOWNLOAD LÄUFT: [Dateiname] (XX MB)...`.
4. Automatischer Start des fertigen `.exe`/`.msi`-Installers, UAC-Erkennung mit
   gesprochenem Hinweis & sicherem Warten, Durchklicken des Setup-Assistenten
   ("Weiter"/"Next", Lizenz, "Installieren"/"Install", "Fertigstellen"/"Finish").
5. `APP_INDEXER.rescan_apps()` + direkter Start der neuen App + Fortsetzen des
   ursprünglichen Nutzerauftrags (`original_user_goal`).
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import platform
import re
import threading
import time
from typing import Any, Callable, Optional
from urllib.parse import urlparse

from app_indexer import APP_INDEXER, normalize_app_key

IS_WINDOWS = platform.system().lower() == "windows"

DOWNLOAD_PARTIAL_SUFFIXES = (".crdownload", ".part", ".tmp", ".opdownload", ".partial")
INSTALLER_COMPLETE_SUFFIXES = (".exe", ".msi")

UAC_SPOKEN_PROMPT = (
    "Bitte bestätige kurz das Windows-Sicherheitsfenster mit Ja, "
    "ich übernehme danach sofort wieder den Installer."
)

BLOCKED_THIRD_PARTY_DOMAINS: tuple[str, ...] = (
    "softonic.",
    "chip.de",
    "computerbild.",
    "heise.de",
    "filehippo.",
    "uptodown.",
    "cnet.com",
    "download.cnet.",
    "malavida.",
    "brothersoft.",
    "lo4d.com",
    "softpedia.",
    "techspot.",
    "filehorse.",
    "en.softonic",
)

INSTALL_CONFIRM_PATTERN = re.compile(
    r"^\s*(?:ja|jo|jap|jup|yes|ok|okay|einverstanden|gerne|klar|sicher)"
    r"(?:[\s,]+(?:bitte|mach\s+das|machs|tu\s+das|starte?(?:\s+das)?|los(?:\s+geht'?s)?|"
    r"gerne|installier\w*(?:\s+es|\s+das)?))*\s*[!.]*\s*$|"
    r"^\s*(?:mach\s+das|machs|tu\s+das|starte?(?:\s+das)?|los(?:\s+geht'?s)?|"
    r"bitte\s+(?:installier\w*|starte\w*|mach\w*)|"
    r"installier\w*(?:\s+es|\s+das)?(?:\s+bitte)?)\s*[!.]*\s*$",
    flags=re.IGNORECASE,
)

INSTALL_CANCEL_PATTERN = re.compile(
    r"\b(?:nein|abbrechen|stopp|abbruch|nicht\s+installieren|lass\s+es|vergiss\s+es|verwerfen|cancel|no)\b",
    flags=re.IGNORECASE,
)


@dataclass(frozen=True)
class OfficialAppSpec:
    app_name: str
    official_url: str
    official_domain: str
    aliases: tuple[str, ...]
    task_keywords: tuple[str, ...]
    search_terms: str
    task_regexes: tuple[str, ...] = ()


OFFICIAL_APP_CATALOG: tuple[OfficialAppSpec, ...] = (
    OfficialAppSpec(
        app_name="Blender",
        official_url="https://www.blender.org/download/",
        official_domain="blender.org",
        aliases=("blender", "blender 3d"),
        task_keywords=(
            "animiere",
            "animieren",
            "3d animation",
            "3d-animation",
            "cad-modell animier",
            "cad modell animier",
            "3d modell animier",
            "3d-modell animier",
            "rigging",
            "rendern",
            "mesh sculpt",
        ),
        search_terms="Blender Download Windows Installer",
        task_regexes=(
            r"\b(?:animier\w*|rigg\w*|render\w*).*\b(?:cad|3d|modell|objekt|szene|mesh)\b",
            r"\b(?:cad|3d|modell).*\b(?:animier\w*|rendern)\b",
        ),
    ),
    OfficialAppSpec(
        app_name="Audacity",
        official_url="https://www.audacityteam.org/download/windows/",
        official_domain="audacityteam.org",
        aliases=("audacity",),
        task_keywords=(
            "audiodatei bearbeiten",
            "audio bearbeiten",
            "tonspur bearbeiten",
            "tonspur schneiden",
            "audio schneiden",
            "podcast schneiden",
            "mp3 schneiden",
            "wav bearbeiten",
            "rauschunterdrückung",
        ),
        search_terms="Audacity Windows Download",
        task_regexes=(
            r"\b(?:bearbeit\w*|schneid\w*|entfern\w*|verbesser\w*|normalisier\w*).*\b(?:audiodatei|audio|tonspur|mp3|wav|ogg|podcast|sprachaufnahme)\b",
            r"\b(?:audiodatei|audio|tonspur|mp3|wav).*\b(?:bearbeit\w*|schneid\w*|optimier\w*)\b",
        ),
    ),
    OfficialAppSpec(
        app_name="GIMP",
        official_url="https://www.gimp.org/downloads/",
        official_domain="gimp.org",
        aliases=("gimp", "gnu image manipulation"),
        task_keywords=(
            "bild bearbeiten",
            "bildbearbeitung",
            "foto bearbeiten",
            "fotobearbeitung",
            "grafik bearbeiten",
            "hintergrund entfernen",
            "ebenen bearbeiten",
        ),
        search_terms="GIMP Windows Download",
        task_regexes=(
            r"\b(?:bearbeit\w*|retuschier\w*|freistell\w*).*\b(?:bild|foto|png|jpg|grafik)\b",
            r"\b(?:bild|foto|grafik).*\b(?:bearbeit\w*|retuschier\w*)\b",
        ),
    ),
    OfficialAppSpec(
        app_name="Krita",
        official_url="https://krita.org/en/download/",
        official_domain="krita.org",
        aliases=("krita",),
        task_keywords=(
            "digital malen",
            "digitales zeichnen",
            "illustration zeichnen",
            "zeichenprogramm",
        ),
        search_terms="Krita Windows Installer Download",
    ),
    OfficialAppSpec(
        app_name="Inkscape",
        official_url="https://inkscape.org/release/",
        official_domain="inkscape.org",
        aliases=("inkscape",),
        task_keywords=(
            "vektorgrafik",
            "svg bearbeiten",
            "svg erstellen",
            "vektor zeichnen",
        ),
        search_terms="Inkscape Windows Download",
    ),
    OfficialAppSpec(
        app_name="VLC Media Player",
        official_url="https://www.videolan.org/vlc/",
        official_domain="videolan.org",
        aliases=("vlc", "vlc media player", "vlc player"),
        task_keywords=(
            "vlc öffnen",
            "mkv abspielen",
            "mp4 lokal abspielen",
            "media player",
        ),
        search_terms="VLC Media Player Windows Download",
    ),
    OfficialAppSpec(
        app_name="OBS Studio",
        official_url="https://obsproject.com/download",
        official_domain="obsproject.com",
        aliases=("obs", "obs studio"),
        task_keywords=(
            "bildschirm aufnehmen",
            "screen recording",
            "gameplay aufnehmen",
            "livestream starten",
        ),
        search_terms="OBS Studio Windows Download",
    ),
    OfficialAppSpec(
        app_name="HandBrake",
        official_url="https://handbrake.fr/downloads.php",
        official_domain="handbrake.fr",
        aliases=("handbrake",),
        task_keywords=(
            "video konvertieren",
            "video komprimieren",
            "mp4 umwandeln",
            "video transkodieren",
        ),
        search_terms="HandBrake Windows Download",
    ),
    OfficialAppSpec(
        app_name="Shotcut",
        official_url="https://shotcut.org/download/",
        official_domain="shotcut.org",
        aliases=("shotcut",),
        task_keywords=(
            "video schneiden",
            "videoschnitt",
            "film schneiden",
            "clip schneiden",
        ),
        search_terms="Shotcut Windows Installer Download",
    ),
    OfficialAppSpec(
        app_name="OrcaSlicer",
        official_url="https://github.com/SoftFever/OrcaSlicer/releases",
        official_domain="github.com",
        aliases=("orcaslicer", "orca slicer", "orca-slicer"),
        task_keywords=(
            "3d druck slicen",
            "stl slicen",
            "3mf slicen",
            "slicer öffnen",
        ),
        search_terms="OrcaSlicer Windows Release Download",
    ),
    OfficialAppSpec(
        app_name="FreeCAD",
        official_url="https://www.freecad.org/downloads.php",
        official_domain="freecad.org",
        aliases=("freecad", "free cad"),
        task_keywords=(
            "cad datei öffnen",
            "step datei öffnen",
            "3d konstruktion",
        ),
        search_terms="FreeCAD Windows Download",
    ),
    OfficialAppSpec(
        app_name="LibreOffice",
        official_url="https://www.libreoffice.org/download/download-libreoffice/",
        official_domain="libreoffice.org",
        aliases=("libreoffice", "libre office", "libreoffice writer", "libreoffice calc"),
        task_keywords=(
            "word dokument bearbeiten",
            "docx bearbeiten",
            "excel tabelle lokal",
            "präsentation lokal",
        ),
        search_terms="LibreOffice Windows Download",
    ),
    OfficialAppSpec(
        app_name="7-Zip",
        official_url="https://www.7-zip.org/download.html",
        official_domain="7-zip.org",
        aliases=("7-zip", "7zip", "seven zip"),
        task_keywords=(
            "7z entpacken",
            "rar entpacken",
            "archiv entpacken",
        ),
        search_terms="7-Zip Windows x64 Download",
    ),
    OfficialAppSpec(
        app_name="Visual Studio Code",
        official_url="https://code.visualstudio.com/download",
        official_domain="code.visualstudio.com",
        aliases=("visual studio code", "vs code", "vscode"),
        task_keywords=(
            "code editor öffnen",
            "quellcode editieren",
        ),
        search_terms="Visual Studio Code Windows Download",
    ),
    OfficialAppSpec(
        app_name="Notepad++",
        official_url="https://notepad-plus-plus.org/downloads/",
        official_domain="notepad-plus-plus.org",
        aliases=("notepad++", "notepad plus plus"),
        task_keywords=(),
        search_terms="Notepad++ Windows Download",
    ),
)


def is_official_url_safe(url: str, expected_domain: str = "") -> bool:
    """Blockiert Drittanbieter-/Adware-Portale und erlaubt nur echte HTTPS-Herstellerseiten."""
    raw = (url or "").strip()
    if not raw.lower().startswith("https://"):
        return False
    host = (urlparse(raw).hostname or "").lower()
    if not host or "." not in host:
        return False
    if any(bad in host or bad in raw.lower() for bad in BLOCKED_THIRD_PARTY_DOMAINS):
        return False
    if expected_domain:
        exp = expected_domain.lower().lstrip("www.")
        if host != exp and not host.endswith("." + exp):
            return False
    return True


def build_install_proposal_question(app_name: str, official_domain: str) -> str:
    """Erzeugt den exakten deutschen Rückfrage-Text vor jedem Download."""
    return (
        f"Für diese Aufgabe fehlt dir noch ein passendes Programm. "
        f"Soll ich dafür {app_name} installieren? "
        f"Ich würde jetzt über den Webbrowser auf die offizielle Webseite ({official_domain}) gehen, "
        f"es herunterladen, installieren und danach direkt mit deiner Aufgabe weitermachen. "
        f"Soll ich starten?"
    )


def _speak_uac_prompt_async() -> None:
    def _runner() -> None:
        try:
            from tts import speak_text

            speak_text(UAC_SPOKEN_PROMPT)
        except Exception:
            pass

    threading.Thread(target=_runner, name="jarvis-uac-tts", daemon=True).start()


def detect_uac_screen(pil_img: Any = None, capture_error: Optional[Exception] = None) -> bool:
    """
    Erkennt den abgedunkelten Windows Secure-Desktop (UAC-Fenster):
    1) Screenshot-Grab schlägt unter Secure Desktop mit `ScreenShotError` / AccessDenied fehl, ODER
    2) der Vollbild-Screenshot ist fast komplett schwarz / extrem abgedunkelt (< 10 mittlere Helligkeit).
    """
    if capture_error is not None:
        err_msg = str(capture_error).lower()
        if any(k in err_msg for k in ("gdi", "bitblt", "access", "screenshot", "handle", "secure")):
            return True
    if pil_img is None:
        return False
    try:
        small = pil_img.convert("L").resize((96, 54))
        pixels = list(small.tobytes())
        if not pixels:
            return False
        mean_luma = sum(pixels) / float(len(pixels))
        max_luma = max(pixels)
        # Normaler abgedunkelter Secure Desktop: fast alle Pixel extrem dunkel, evtl. kleines helles Dialog-Rechteck
        dark_ratio = sum(1 for p in pixels if p < 18) / float(len(pixels))
        if mean_luma < 8.5 and max_luma < 35:
            return True
        if dark_ratio >= 0.80 and 15 <= mean_luma <= 72:
            return True
    except Exception:
        pass
    return False


def monitor_downloads_for_installer(
    downloads_dir: Optional[Path] = None,
    baseline_files: Optional[dict[str, float]] = None,
    min_mtime: Optional[float] = None,
    timeout_sec: float = 180.0,
    poll_interval: float = 1.2,
    on_status: Optional[Callable[[str], None]] = None,
    progress_hook: Optional[Callable[[str], None]] = None,
    stop_event: Optional[threading.Event] = None,
) -> Optional[Path]:
    """
    Überwacht `%USERPROFILE%\\Downloads` alle `poll_interval` Sekunden auf wachsende
    `.crdownload`/`.part`/`.tmp`-Dateien und gibt den Pfad des fertigen `.exe`/`.msi`-Installers zurück,
    sobald die Dateigröße stabil abgeschlossen ist.
    """
    dl_dir = downloads_dir or (Path.home() / "Downloads")
    dl_dir.mkdir(parents=True, exist_ok=True)
    known = dict(baseline_files or {})
    since_ts = float(min_mtime if min_mtime is not None else time.time() - 5.0)
    deadline = time.monotonic() + max(5.0, float(timeout_sec))

    last_sizes: dict[str, int] = {}
    stable_hits: dict[str, int] = {}
    seen_partial = False

    def _emit(msg: str) -> None:
        if progress_hook is not None:
            try:
                progress_hook(msg)
            except Exception:
                pass
        if on_status is not None:
            try:
                on_status(msg)
            except Exception:
                pass
        try:
            from multi_cursor_overlay import MULTI_CURSOR_OVERLAY

            MULTI_CURSOR_OVERLAY.set_status_pill(msg)
        except Exception:
            pass

    while time.monotonic() < deadline:
        if stop_event is not None and stop_event.is_set():
            return None
        try:
            from agent_hand import SMART_MOUSE_GUARD

            SMART_MOUSE_GUARD.check_or_raise()
        except Exception as guard_exc:
            if "AgentAbortedByUser" in type(guard_exc).__name__:
                raise

        try:
            entries = list(dl_dir.iterdir())
        except Exception:
            entries = []

        partials: list[tuple[Path, int]] = []
        completed_candidates: list[tuple[float, Path, int]] = []

        for item in entries:
            if not item.is_file():
                continue
            try:
                st = item.stat()
            except OSError:
                continue
            name_low = item.name.lower()
            prev_mtime = known.get(item.name)
            is_new_or_updated = (prev_mtime is None and st.st_mtime >= since_ts) or (
                prev_mtime is not None and st.st_mtime > prev_mtime + 0.2
            )
            if not is_new_or_updated:
                continue

            if name_low.endswith(DOWNLOAD_PARTIAL_SUFFIXES):
                partials.append((item, int(st.st_size)))
                seen_partial = True
            elif name_low.endswith(INSTALLER_COMPLETE_SUFFIXES) and st.st_size > 0:
                completed_candidates.append((st.st_mtime, item, int(st.st_size)))

        if partials:
            p_file, p_size = max(partials, key=lambda tup: tup[1])
            mb = p_size / (1024.0 * 1024.0)
            clean_name = p_file.name
            for suf in DOWNLOAD_PARTIAL_SUFFIXES:
                if clean_name.lower().endswith(suf):
                    clean_name = clean_name[: -len(suf)]
                    break
            _emit(f"⬇️ DOWNLOAD LÄUFT: {clean_name} ({mb:.1f} MB)...")

        if completed_candidates:
            completed_candidates.sort(key=lambda tup: tup[0], reverse=True)
            _, newest_installer, newest_size = completed_candidates[0]
            # Prüfen, ob noch eine gleichnamige .crdownload/.part-Datei existiert
            matching_partial = any(
                p_path.name.lower().startswith(newest_installer.name.lower())
                for p_path, _ in partials
            )
            key = str(newest_installer)
            if not matching_partial:
                if last_sizes.get(key) == newest_size:
                    stable_hits[key] = stable_hits.get(key, 0) + 1
                else:
                    last_sizes[key] = newest_size
                    stable_hits[key] = 1
                # Wenn bereits abgeschlossen gesehen (mind. 2 stabile Polls oder sofort ohne Partial nach Download)
                needed_hits = 2 if (seen_partial or poll_interval >= 0.3) else 1
                if stable_hits[key] >= needed_hits:
                    mb_done = newest_size / (1024.0 * 1024.0)
                    _emit(f"✅ DOWNLOAD FERTIG: {newest_installer.name} ({mb_done:.1f} MB)")
                    return newest_installer

        time.sleep(max(0.05, float(poll_interval)))

    return None


class AppInstallerAgent:
    """
    Verwaltet fehlende App-Erkennung, Nutzer-Rückfrage (`pending_install_task`),
    sichtbaren Browser-Download von der offiziellen Herstellerseite, Download-Monitoring,
    Setup-Wizard-Automatisierung mit UAC-Pause sowie Re-Scan & Aufgaben-Fortsetzung.
    """

    def __init__(self, indexer: Any = None) -> None:
        self._lock = threading.Lock()
        self.indexer = indexer or APP_INDEXER
        self.pending_install_task: Optional[dict[str, Any]] = None
        self.last_install_result: Optional[dict[str, Any]] = None

    def get_pending_proposal(self) -> Optional[dict[str, Any]]:
        with self._lock:
            return dict(self.pending_install_task) if self.pending_install_task else None

    def clear_pending_proposal(self) -> None:
        with self._lock:
            self.pending_install_task = None

    @staticmethod
    def is_confirm(user_text: str) -> bool:
        return bool(INSTALL_CONFIRM_PATTERN.search((user_text or "").strip()))

    @staticmethod
    def is_cancel(user_text: str) -> bool:
        return bool(INSTALL_CANCEL_PATTERN.search((user_text or "").strip()))

    def _find_catalog_spec(self, user_goal: str, explicit_app: str = "") -> Optional[OfficialAppSpec]:
        low_goal = (user_goal or "").lower()
        low_app = normalize_app_key(explicit_app)

        # 1. Expliziter App-Name gegen Aliases prüfen
        if low_app:
            for spec in OFFICIAL_APP_CATALOG:
                for alias in spec.aliases:
                    na = normalize_app_key(alias)
                    if low_app == na or (len(na) >= 4 and na in low_app):
                        return spec

        # 2. Aliases direkt im gesamten Nutzerbefehl suchen
        for spec in OFFICIAL_APP_CATALOG:
            for alias in spec.aliases:
                if len(alias) >= 3 and re.search(rf"\b{re.escape(alias)}\b", low_goal, flags=re.IGNORECASE):
                    return spec

        # 3. Aufgabenbezogene Bedarfs-Keywords & flexible Satz-Regexes
        for spec in OFFICIAL_APP_CATALOG:
            for kw in spec.task_keywords:
                if kw in low_goal:
                    return spec
            for pat in spec.task_regexes:
                if re.search(pat, low_goal, flags=re.IGNORECASE):
                    return spec

        return None

    def _resolve_dynamic_official_app(
        self,
        requested_app: str,
        user_goal: str,
        brain: Any = None,
    ) -> Optional[dict[str, str]]:
        """
        Wenn eine nicht im Katalog enthaltene App explizit gewünscht wurde (z. B. "Öffne Programm X"),
        fragt optional das Brain nach ausschließlich der offiziellen Herstellerseite.
        """
        clean_name = (requested_app or "").strip(" ?!.,\"'")
        if not clean_name or len(clean_name) < 2:
            return None
        if brain is None or not hasattr(brain, "ask"):
            return None

        sys_prompt = (
            "Ermittle für Windows-Software ausschließlich die OFFIZIELLE Hersteller-Download-Webseite. "
            "NIEMALS Drittanbieter-Portale wie chip.de, softonic, computerbild, filehippo, uptodown oder heise nennen! "
            "Antworte AUSSCHLIESSLICH mit validem JSON ohne Markdown:\n"
            '{"app_name": "Offizieller Name", "official_url": "https://...", "official_domain": "example.org", "search_terms": "Offizieller Name Windows Download"}'
        )
        try:
            resp = brain.ask(
                f'Software: "{clean_name}" | Auftrag: "{user_goal}"',
                system_prompt=sys_prompt,
                use_history=False,
            )
            from brain import extract_json_object

            parsed = extract_json_object(str(getattr(resp, "text", resp) or ""))
            if parsed:
                u = str(parsed.get("official_url") or "").strip()
                dom = str(parsed.get("official_domain") or "").strip().lower()
                if not dom and u:
                    dom = (urlparse(u).hostname or "").lower().lstrip("www.")
                if is_official_url_safe(u, expected_domain=dom):
                    return {
                        "app_name": str(parsed.get("app_name") or clean_name).strip(),
                        "official_url": u,
                        "official_domain": dom,
                        "search_terms": str(parsed.get("search_terms") or f"{clean_name} Windows Download").strip(),
                    }
        except Exception:
            pass
        return None

    def detect_missing_app_and_propose(
        self,
        user_goal: str,
        explicit_app: str = "",
        brain: Any = None,
    ) -> Optional[dict[str, Any]]:
        """
        Prüft über `APP_INDEXER`, ob für `user_goal` / `explicit_app` bereits eine passende App
        installiert ist. Falls NEIN, bereitet sie `pending_install_task` vor und gibt die
        Pflicht-Rückfrage zurück.
        """
        goal_clean = (user_goal or "").strip()
        if not goal_clean:
            return None

        # Keine Installations-Rückfrage auslösen bei reinen Websuchen, YouTube-Videos oder Wissensfragen
        if not explicit_app and re.search(
            r"\b(was\s+ist|erklär\w*|wer\s+hat|wie\s+funktioniert|youtube|wikipedia|google|preis\s+von|aktie|news|wetter|"
            r"suche?\s+(?:im\s+internet|im\s+web|auf\s+(?:google|youtube|amazon)))\b",
            goal_clean,
            flags=re.IGNORECASE,
        ):
            return None

        # Erst prüfen, ob die gewünschte App / Kategorie überhaupt einen lokalen Installer betrifft
        spec = self._find_catalog_spec(goal_clean, explicit_app=explicit_app)
        if spec is not None:
            installed = self.indexer.is_app_installed(spec.app_name, aliases=list(spec.aliases))
            if installed is not None:
                return None  # Bereits installiert! Normal starten/bedienen
            app_name = spec.app_name
            official_url = spec.official_url
            official_domain = spec.official_domain
            search_terms = spec.search_terms
        elif explicit_app:
            installed = self.indexer.is_app_installed(explicit_app)
            if installed is not None:
                return None
            dyn = self._resolve_dynamic_official_app(explicit_app, goal_clean, brain=brain)
            if dyn is None:
                return None
            app_name = dyn["app_name"]
            official_url = dyn["official_url"]
            official_domain = dyn["official_domain"]
            search_terms = dyn["search_terms"]
        else:
            return None

        proposal = {
            "app_name": app_name,
            "official_url": official_url,
            "official_domain": official_domain,
            "original_user_goal": goal_clean,
            "search_terms": search_terms,
        }
        with self._lock:
            self.pending_install_task = dict(proposal)

        question = build_install_proposal_question(app_name, official_domain)
        return {
            "status": "needs_confirmation",
            "pending_install_task": dict(proposal),
            "question": question,
            "message": question,
        }

    @staticmethod
    def _snapshot_downloads(downloads_dir: Optional[Path] = None) -> tuple[Path, dict[str, float]]:
        dl_dir = downloads_dir or (Path.home() / "Downloads")
        dl_dir.mkdir(parents=True, exist_ok=True)
        snap: dict[str, float] = {}
        try:
            for item in dl_dir.iterdir():
                if item.is_file():
                    try:
                        snap[item.name] = item.stat().st_mtime
                    except OSError:
                        pass
        except Exception:
            pass
        return dl_dir, snap

    def _run_download_via_visible_browser(
        self,
        proposal: dict[str, Any],
        brain: Any = None,
        on_status: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        from agent_hand import VISUAL_HAND_AGENT

        app_name = proposal["app_name"]
        official_url = proposal["official_url"]
        official_domain = proposal["official_domain"]

        if not is_official_url_safe(official_url, expected_domain=official_domain):
            return {
                "ok": False,
                "message": f"Sicherheits-Stopp: '{official_url}' ist keine verifizierte Herstellerseite.",
            }

        dl_goal = (
            f"Wir befinden uns auf der offiziellen Webseite von {app_name} ({official_domain}). "
            f"1) Klicke zuerst eventuelle Cookie-Banner weg. "
            f"2) Suche sichtbar den offiziellen Download-Button für Windows "
            f"('Download', 'Herunterladen', 'Windows Installer', '.exe' oder '.msi') und klicke ihn an, "
            f"damit der Download im Browser startet. "
            f"3) Klicke NIEMALS auf Werbung oder Drittanbieter-Links."
        )
        return VISUAL_HAND_AGENT.run_visual_mouse_loop(
            goal=dl_goal,
            brain=brain,
            start_url=official_url,
            max_steps=18,
            on_status=on_status,
            minimize_jarvis=True,
            restore_jarvis_after=False,
        )

    def _wait_through_uac_if_needed(
        self,
        on_status: Optional[Callable[[str], None]] = None,
        max_wait_sec: float = 90.0,
    ) -> bool:
        """Falls der Windows Secure-Desktop (UAC) aktiv ist: Hinweis sprechen und sicher warten."""
        from agent_hand import SMART_MOUSE_GUARD, capture_screen_for_vision

        announced = False
        uac_seen = False
        deadline = time.monotonic() + max(5.0, float(max_wait_sec))

        while time.monotonic() < deadline:
            SMART_MOUSE_GUARD.check_or_raise()
            cap_err: Optional[Exception] = None
            pil_img = None
            try:
                pil_img, _, _, _ = capture_screen_for_vision()
            except Exception as exc:
                cap_err = exc

            if not detect_uac_screen(pil_img, capture_error=cap_err):
                return uac_seen

            uac_seen = True
            if not announced:
                announced = True
                _speak_uac_prompt_async()
                msg = "🛡️ UAC-SICHERHEITSFENSTER ERKANNT // Bitte kurz mit Ja bestätigen..."
                if on_status:
                    try:
                        on_status(msg)
                    except Exception:
                        pass
                try:
                    from multi_cursor_overlay import MULTI_CURSOR_OVERLAY

                    MULTI_CURSOR_OVERLAY.set_status_pill(msg)
                except Exception:
                    pass
            time.sleep(0.8)
        return uac_seen

    def run_setup_wizard_loop(
        self,
        installer_path: Path,
        app_name: str,
        brain: Any = None,
        on_status: Optional[Callable[[str], None]] = None,
        launch_installer: bool = True,
        max_steps: int = 30,
    ) -> dict[str, Any]:
        """
        Startet den heruntergeladenen Installer, wartet sauber bei UAC-Abfragen und bedient
        den Setup-Assistenten sichtbar per Mausklick ("Weiter"/"Next", Lizenz, "Installieren",
        "Fertigstellen"/"Finish").
        """
        from agent_hand import VISUAL_HAND_AGENT

        if launch_installer:
            if on_status:
                on_status(f"⚙️ STARTE INSTALLER: {installer_path.name}...")
            if IS_WINDOWS and hasattr(os, "startfile"):
                os.startfile(str(installer_path))  # type: ignore[attr-defined]
            time.sleep(2.0)

        self._wait_through_uac_if_needed(on_status=on_status, max_wait_sec=90.0)

        wizard_goal = (
            f"Installiere '{app_name}' über den sichtbaren Setup-Assistenten vollständig auf Windows: "
            f"1) Klicke auf 'Weiter' / 'Next' / 'Fortfahren' / 'Continue'. "
            f"2) Setze bei Lizenzvereinbarungen das Häkchen / klicke 'Ich akzeptiere' / 'I accept' / 'I Agree' / 'Akzeptieren'. "
            f"3) Lasse optionale Drittanbieter-Werbung/Toolbars unbedingt ABGEWÄHLT. "
            f"4) Klicke auf 'Installieren' / 'Install'. "
            f"5) Warte mit action='wait' (oder erneuten Screenshots), solange der Fortschrittsbalken läuft. "
            f"6) Sobald der Abschlussbildschirm erscheint, klicke auf 'Fertigstellen' / 'Finish' / 'Abschließen' und beende mit action='done'. "
            f"Falls ein Windows-UAC-Sicherheitsdialog aktiv ist, pausiere kurz."
        )

        # Vor und während Wizard UAC-Checks sauber einhängen
        res = VISUAL_HAND_AGENT.run_visual_mouse_loop(
            goal=wizard_goal,
            brain=brain,
            max_steps=max_steps,
            on_status=on_status,
            minimize_jarvis=True,
            restore_jarvis_after=False,
        )
        self._wait_through_uac_if_needed(on_status=on_status, max_wait_sec=20.0)
        return res

    def execute_pending_install(
        self,
        brain: Any = None,
        on_status: Optional[Callable[[str], None]] = None,
        downloads_dir: Optional[Path] = None,
        download_timeout_sec: float = 180.0,
    ) -> dict[str, Any]:
        """
        Führt den kompletten bestätigten Beschaffungs- & Installations-Workflow aus:
        Browser-Download -> Downloads-Ordner-Monitoring -> Installer starten + UAC + Wizard
        -> `APP_INDEXER.rescan_apps()` -> App starten -> Originalauftrag fortsetzen.
        """
        with self._lock:
            proposal = dict(self.pending_install_task) if self.pending_install_task else None

        if not proposal:
            return {
                "ok": False,
                "status": "no_pending_task",
                "message": "Es liegt aktuell kein ausstehender Installationsauftrag vor.",
            }

        app_name = proposal["app_name"]
        original_goal = proposal["original_user_goal"]

        try:
            dl_dir, baseline_snap = self._snapshot_downloads(downloads_dir)
            started_ts = time.time()

            # 1. Browser öffnen & offiziellen Download-Button anklicken
            if on_status:
                on_status(f"🌐 ÖFFNE OFFIZIELLE SEITE: {proposal['official_domain']}...")
            dl_browser_res = self._run_download_via_visible_browser(
                proposal,
                brain=brain,
                on_status=on_status,
            )

            # 2. Downloads-Ordner im Live-Takt auf .crdownload -> fertige .exe/.msi überwachen
            installer_file = monitor_downloads_for_installer(
                downloads_dir=dl_dir,
                baseline_files=baseline_snap,
                min_mtime=started_ts - 2.0,
                timeout_sec=download_timeout_sec,
                poll_interval=1.2,
                on_status=on_status,
            )

            # Fallback: Ctrl+J im Browser öffnen, falls Download im Browser-Menü hängt
            if installer_file is None:
                from agent_hand import hotkey

                try:
                    hotkey("ctrl", "j", label="BROWSER DOWNLOADS (CTRL+J)")
                    time.sleep(1.2)
                    installer_file = monitor_downloads_for_installer(
                        downloads_dir=dl_dir,
                        baseline_files=baseline_snap,
                        min_mtime=started_ts - 2.0,
                        timeout_sec=20.0,
                        poll_interval=1.0,
                        on_status=on_status,
                    )
                except Exception:
                    pass

            if installer_file is None:
                msg = (
                    f"Ich habe {proposal['official_domain']} geöffnet, konnte in {dl_dir} aber "
                    f"innerhalb von {int(download_timeout_sec)} Sekunden noch keine abgeschlossene "
                    f".exe- oder .msi-Installationsdatei erkennen."
                )
                return {
                    "ok": False,
                    "status": "download_timeout",
                    "browser_result": dl_browser_res,
                    "message": msg,
                }

            # 3. Setup-Wizard + UAC durchklicken
            wizard_res = self.run_setup_wizard_loop(
                installer_path=installer_file,
                app_name=app_name,
                brain=brain,
                on_status=on_status,
                launch_installer=True,
                max_steps=35,
            )

            # 4. System-App-Index sofort aktualisieren (`rescan_apps`)
            if on_status:
                on_status(f"🔄 AKTUALISIERE APP-INDEX NACH INSTALLATION VON {app_name.upper()}...")
            self.indexer.rescan_apps(deep=True)

            # 5. Frisch installierte App starten und Original-Auftrag fortsetzen
            launch_res = self.indexer.launch_app(app_name)
            time.sleep(1.5)

            from agent_hand import VISUAL_HAND_AGENT

            resume_res = VISUAL_HAND_AGENT.visual_desktop_app_operator(
                goal=original_goal,
                app_name=app_name,
                brain=brain,
                on_status=on_status,
                max_steps=None,
                restore_hud_after=True,
            )
            final_msg = (
                f"Erledigt, Chef. Ich habe {app_name} von {proposal['official_domain']} heruntergeladen "
                f"({installer_file.name}), installiert, den App-Index aktualisiert und direkt mit deinem "
                f"Auftrag weitergemacht: {resume_res.get('summary') or resume_res.get('message') or original_goal}"
            )
            out = {
                "ok": True,
                "status": "completed",
                "app_name": app_name,
                "installer_path": str(installer_file),
                "wizard_result": wizard_res,
                "launch_result": launch_res,
                "resume_result": resume_res,
                "message": final_msg,
            }
            self.last_install_result = out
            return out
        finally:
            self.clear_pending_proposal()
            try:
                from multi_cursor_overlay import MULTI_CURSOR_OVERLAY

                MULTI_CURSOR_OVERLAY.clear_status_pill()
            except Exception:
                pass


APP_INSTALLER_AGENT = AppInstallerAgent()
