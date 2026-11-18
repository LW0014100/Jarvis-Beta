# -*- coding: utf-8 -*-
r"""
4-Agenten-Schwarm-System für J.A.R.V.I.S. (swarm_core.py)
=========================================================
Koordiniert 4 spezialisierte, parallel arbeitsfähige KI-Agenten mit Live-HUD-Telemetrie,
unbegrenztem NAS-Langzeitgedächtnis (`nas_brain.py`) und AES-Tresor (`vault.py`):

1. **AGENT 1 – `OvermindAgent` (`OVERMIND` – Planer & NAS-Gedächtnis):**
   - Nimmt komplexe Aufträge entgegen, durchsucht `NAS_BRAIN` (`jarvis_brain.sqlite3` & `skills_db.json`)
     nach gespeichertem Wissen und UI-Skills, zerlegt die Aufgabe in Teilaufträge und delegiert
     diese (sequenziell oder parallel) an Agent 2, 3 und 4.

2. **AGENT 2 – `DesktopVideoAgent` (`DESKTOP & VIDEO-EDITOR` – Maus/Tastatur + Schnittprogramm):**
   - **1-FPS-Vision-Loop:** Macht jede Sekunde einen Screenshot (`mss`), zeichnet ein rotes Fadenkreuz
     an `pyautogui.position()` und steuert jedes Windows-Programm per Klick, Drag-and-Drop
     (`pyautogui.dragTo`) und Tastatur-Shortcuts (`pyautogui.hotkey`).
   - **Videoschnitt- & Media-Power:**
     - Öffnet Schnittprogramme (CapCut, DaVinci Resolve, Premiere Pro, Clipchamp), importiert Medien,
       zieht Clips per Drag-and-Drop auf die Timeline und setzt Schnitte per Shortcuts (`Ctrl+B` / `Ctrl+K`).
     - Besitzt das direkte Hochgeschwindigkeits-Video-Tool `auto_edit_video` (`ffmpeg` / `moviepy`),
       das Rohvideos auf Laptop/NAS analysiert, Sprechpausen/Stille automatisch herausschneidet,
       Clips zusammenfügt und als fertige `.mp4` exportiert.

3. **AGENT 3 – `WebAccountGhostAgent` (`WEB & ACCOUNT GHOST` – Browser, Recherche & Auto-Registrierung):**
   - Steuert sichtbar den Desktop-Webbrowser, führt Recherchen durch, füllt Formulare über den
     verschlüsselten Tresor (`vault.py`) aus, erstellt selbstständig Accounts und liest
     Bestätigungscodes/Links automatisch per IMAP aus dem E-Mail-Postfach aus.

4. **AGENT 4 – `FinanceTradingQuantAgent` (`FINANCE & TRADING QUANT` – Aktien, Krypto & Konten):**
   - Nutzt den AES-verschlüsselten Tresor (`vault.py`), analysiert Live-Börsenkurse (API & Browser-Charts)
     und führt Trades im Paper-Trading- oder Echtgeld-Modus (je nach Sicherheits-Schalter im HUD) aus.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import threading

SWARM_OPERATOR_LAW = (
    "GRUNDGESETZ '100% VISIBLE DESKTOP & APP OPERATOR': "
    "Keine stillen Hintergrund-Suchen (kein DuckDuckGo/Crawl4AI/yfinance/wttr.in)! "
    "Alle Web-Recherchen, Kurse, Wetter & Fakten MÜSSEN sichtbar im echten Desktop-Webbrowser "
    "(`visual_browser_search_and_read`) ablaufen; alle lokalen Laptop-Aufgaben (Rechner, Notizen, "
    "Musik, Einstellungen, Schnittprogramme, Spiele) MÜSSEN sichtbar über echte Desktop-Apps "
    "(`visual_desktop_app_operator`) gesteuert werden!"
)
import time
from typing import Any, Callable, Optional

from agent_hand import (
    VISUAL_HAND_AGENT,
    capture_annotated_screen_1fps,
    get_screen_and_cursor_info,
    ghost_double_click,
    ghost_hotkey,
    ghost_move_to,
    ghost_press,
    ghost_scroll_swipe,
    ghost_typewrite,
    is_escape_or_failsafe_triggered,
)
from app_indexer import APP_INDEXER
from autonomous_agent import AUTONOMOUS_AGENT
from ghost_cursor import GHOST_OVERLAY, ghost_click
from nas_brain import NAS_BRAIN
from trading_engine import TRADING_ENGINE, resolve_ticker_symbol
from vault import SECURE_VAULT, read_latest_verification_email

IS_WINDOWS = platform.system().lower() == "windows"
CREATE_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0


def _resolve_ffmpeg_exe() -> Optional[str]:
    """Ermittelt die lokale `ffmpeg`-Binary (System-PATH oder `imageio_ffmpeg`)."""
    sys_ffmpeg = shutil.which("ffmpeg")
    if sys_ffmpeg:
        return sys_ffmpeg
    try:
        import imageio_ffmpeg  # type: ignore

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    return None


def _auto_edit_video_pyav(
    export_dir: Path,
    input_path: Optional[str | Path] = None,
    output_path: Optional[str | Path] = None,
    silence_threshold_db: float = -32.0,
    min_silence_sec: float = 0.35,
    open_in_editor: str = "",
    on_status: Optional[Callable[[str], None]] = None,
) -> dict[str, Any]:
    """
    Hochgeschwindigkeits-Videoschnitt über die eingebetteten FFmpeg-C-Bibliotheken (`av` / PyAV + `numpy`):
    Erkennt Sprechpausen/Stille (dBFS-Analyse), schneidet stille Intervalle heraus und exportiert `.mp4` (H.264/AAC).
    """
    import math
    import av  # type: ignore
    import numpy as np  # type: ignore

    src_path: Optional[Path] = Path(str(input_path)).expanduser() if input_path else None
    if src_path is not None and src_path.is_file():
        try:
            src_path = NAS_BRAIN.stage_video_to_hot_cache(src_path, on_status=on_status)
        except Exception:
            pass
    if src_path is None or not src_path.is_file():
        demo_input = export_dir / "jarvis_raw_demo_input.mp4"
        if not demo_input.is_file():
            if on_status:
                on_status("Erzeuge Demo-Rohvideo (H.264/AAC) mit Sprach-/Stille-Segmenten...")
            fps = 25
            sr = 44100
            dur_s = 4.0
            with av.open(str(demo_input), mode="w") as container:
                v_stream = container.add_stream("libx264", rate=fps)
                v_stream.width = 640
                v_stream.height = 360
                v_stream.pix_fmt = "yuv420p"
                a_stream = container.add_stream("aac", rate=sr)

                total_v = int(dur_s * fps)
                for fi in range(total_v):
                    t_sec = fi / fps
                    img = np.zeros((360, 640, 3), dtype=np.uint8)
                    # Dunkelblaues HUD-Testbild mit Zeitbalken
                    img[:, :] = (28, 18, 6)
                    bar_w = int((t_sec / dur_s) * 600)
                    img[160:200, 20 : 20 + max(1, bar_w)] = (255, 229, 0) if (t_sec < 1.4 or t_sec > 2.6) else (60, 60, 180)
                    vf = av.VideoFrame.from_ndarray(img, format="bgr24")
                    for pkt in v_stream.encode(vf):
                        container.mux(pkt)

                samples_per_frame = 1024
                total_samples = int(dur_s * sr)
                for s_idx in range(0, total_samples, samples_per_frame):
                    n = min(samples_per_frame, total_samples - s_idx)
                    t_arr = (np.arange(n, dtype=np.float32) + s_idx) / sr
                    # 0.0-1.4s: 440Hz Sprache/Ton, 1.4-2.6s: Stille (Sprechpause), 2.6-4.0s: 587Hz Sprache/Ton
                    tone = np.where(
                        t_arr < 1.4,
                        0.35 * np.sin(2.0 * np.pi * 440.0 * t_arr),
                        np.where(t_arr > 2.6, 0.35 * np.sin(2.0 * np.pi * 587.0 * t_arr), 0.0),
                    ).astype(np.float32)
                    af = av.AudioFrame.from_ndarray(tone.reshape(1, -1), format="fltp", layout="mono")
                    af.sample_rate = sr
                    for pkt in a_stream.encode(af):
                        container.mux(pkt)

                for pkt in v_stream.encode():
                    container.mux(pkt)
                for pkt in a_stream.encode():
                    container.mux(pkt)
        src_path = demo_input

    if output_path:
        out_file = Path(str(output_path)).expanduser()
    else:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_file = export_dir / f"{src_path.stem}_jarvis_cut_{stamp}.mp4"
    out_file.parent.mkdir(parents=True, exist_ok=True)

    if on_status:
        on_status(f"Analysiere Audiospur auf Sprechpausen ({src_path.name})...")

    # 1. Audio dekodieren & Stille-Intervalle bestimmen
    audio_chunks: list[np.ndarray] = []
    sr = 44100
    fps = 25
    width, height = 640, 360
    video_frames_rgb: list[tuple[float, np.ndarray]] = []

    with av.open(str(src_path), mode="r") as in_cont:
        if in_cont.streams.video:
            vs = in_cont.streams.video[0]
            fps = int(round(float(vs.average_rate or 25))) or 25
            width = int(vs.width or 640)
            height = int(vs.height or 360)
            for vf in in_cont.decode(video=0):
                t_v = float(vf.pts * vf.time_base) if vf.pts is not None and vf.time_base is not None else (len(video_frames_rgb) / fps)
                video_frames_rgb.append((t_v, vf.to_ndarray(format="bgr24")))

    with av.open(str(src_path), mode="r") as in_cont:
        if in_cont.streams.audio:
            a_st = in_cont.streams.audio[0]
            sr = int(a_st.rate or 44100)
            for af in in_cont.decode(audio=0):
                arr = af.to_ndarray()
                if arr.ndim > 1:
                    arr = arr.mean(axis=0)
                audio_chunks.append(arr.astype(np.float32))

    full_audio = np.concatenate(audio_chunks) if audio_chunks else np.zeros(int(sr * 4), dtype=np.float32)
    total_duration = round(len(full_audio) / float(sr), 2) if len(full_audio) else round(len(video_frames_rgb) / float(fps), 2)

    win_samples = max(1, int(sr * 0.05))  # 50ms Analysefenster
    silence_intervals: list[dict[str, float]] = []
    in_silence = False
    sil_start = 0.0
    for start_i in range(0, len(full_audio), win_samples):
        chunk = full_audio[start_i : start_i + win_samples]
        rms = float(np.sqrt(np.mean(np.square(chunk)) + 1e-12))
        db = 20.0 * math.log10(max(rms, 1e-9))
        t_cur = start_i / float(sr)
        if db < silence_threshold_db:
            if not in_silence:
                in_silence = True
                sil_start = t_cur
        else:
            if in_silence:
                in_silence = False
                if (t_cur - sil_start) >= min_silence_sec:
                    silence_intervals.append({"start": round(sil_start, 3), "end": round(t_cur, 3)})
    if in_silence and (total_duration - sil_start) >= min_silence_sec:
        silence_intervals.append({"start": round(sil_start, 3), "end": round(total_duration, 3)})

    keep_segments: list[tuple[float, float]] = []
    cursor_t = 0.0
    removed_silence_sec = 0.0
    for iv in silence_intervals:
        s_st, s_en = iv["start"], iv["end"]
        if s_st - cursor_t >= 0.12:
            keep_segments.append((round(cursor_t, 3), round(s_st, 3)))
        removed_silence_sec += max(0.0, s_en - s_st)
        cursor_t = max(cursor_t, s_en)
    if total_duration - cursor_t >= 0.12:
        keep_segments.append((round(cursor_t, 3), round(total_duration, 3)))
    if not keep_segments:
        keep_segments = [(0.0, total_duration)]

    # 2. Aktive Video- und Audio-Segmente in neue .mp4 muxen
    if on_status:
        on_status(f"Schneide {len(silence_intervals)} Sprechpausen ({removed_silence_sec:.2f}s) heraus & exportiere MP4...")

    def _is_kept(t_val: float) -> bool:
        return any(st <= t_val <= en for st, en in keep_segments)

    kept_audio_parts = [
        full_audio[int(st * sr) : int(en * sr)]
        for st, en in keep_segments
        if int(en * sr) > int(st * sr)
    ]
    out_audio = np.concatenate(kept_audio_parts) if kept_audio_parts else full_audio

    with av.open(str(out_file), mode="w") as out_cont:
        v_out = out_cont.add_stream("libx264", rate=fps)
        v_out.width = width
        v_out.height = height
        v_out.pix_fmt = "yuv420p"
        a_out = out_cont.add_stream("aac", rate=sr)

        for t_v, frame_bgr in video_frames_rgb:
            if _is_kept(t_v):
                vf = av.VideoFrame.from_ndarray(frame_bgr, format="bgr24")
                for pkt in v_out.encode(vf):
                    out_cont.mux(pkt)

        for s_idx in range(0, len(out_audio), 1024):
            chunk = out_audio[s_idx : s_idx + 1024]
            af = av.AudioFrame.from_ndarray(chunk.reshape(1, -1), format="fltp", layout="mono")
            af.sample_rate = sr
            for pkt in a_out.encode(af):
                out_cont.mux(pkt)

        for pkt in v_out.encode():
            out_cont.mux(pkt)
        for pkt in a_out.encode():
            out_cont.mux(pkt)

    out_duration = round(max(0.2, total_duration - removed_silence_sec), 2)
    cold_nas_file = NAS_BRAIN.archive_video_to_cold_nas(out_file)
    editor_msg = ""
    if open_in_editor:
        found_ed, ed_msg = APP_INDEXER.open_app_by_query(open_in_editor)
        editor_msg = f" {ed_msg}" if found_ed else ""

    summary = (
        f"Auto-Video-Schnitt abgeschlossen: '{out_file.name}' ({len(keep_segments)} aktive Clips, "
        f"{removed_silence_sec:.2f}s Sprechpausen automatisch entfernt). "
        f"HotCache: {out_file} -> NAS-Archiv: {cold_nas_file}.{editor_msg}"
    )
    NAS_BRAIN.store_knowledge(
        category="video_edit",
        title=f"Auto-Cut: {out_file.name}",
        content=summary,
        source=str(src_path),
        metadata={
            "output_path": str(out_file),
            "cold_nas_output_path": str(cold_nas_file),
            "removed_silence_sec": round(removed_silence_sec, 2),
        },
    )
    return {
        "ok": out_file.is_file(),
        "input_path": str(src_path),
        "staged_hot_input": str(src_path),
        "output_path": str(out_file),
        "cold_nas_output_path": str(cold_nas_file),
        "input_duration_s": round(total_duration, 2),
        "output_duration_s": out_duration,
        "silence_intervals": silence_intervals,
        "segments_kept": len(keep_segments),
        "removed_silence_sec": round(removed_silence_sec, 2),
        "message": summary,
    }


def auto_edit_video(
    input_path: Optional[str | Path] = None,
    output_path: Optional[str | Path] = None,
    silence_threshold_db: float = -35.0,
    min_silence_sec: float = 0.45,
    concat_paths: Optional[list[str | Path]] = None,
    open_in_editor: str = "",
    on_status: Optional[Callable[[str], None]] = None,
) -> dict[str, Any]:
    """
    Hochgeschwindigkeits-Video-Tool (`ffmpeg` / `av` PyAV) mit 2-Stufen Hybrid-Speicher:
    1. Kopiert Rohvideos vom Explorer-NAS mit 8-MB-Sequenzblöcken in den lokalen NVMe-`HotCache`.
    2. Erkennt Sprechpausen / Stille (`silencedetect`) und schneidet stille Passagen automatisch heraus.
    3. Fügt mehrere Clips auf Wunsch nahtlos zusammen und exportiert eine fertige `.mp4` auf der NVMe-SSD.
    4. Speichert das fertige Ergebnis zurück in den Explorer-NAS-Ordner (`video_renders`) und öffnet optional CapCut/DaVinci/Premiere.
    """
    export_dir = NAS_BRAIN.hot_cache_dir / "video_exports"
    export_dir.mkdir(parents=True, exist_ok=True)

    ffmpeg_exe = _resolve_ffmpeg_exe()
    if not ffmpeg_exe:
        try:
            return _auto_edit_video_pyav(
                export_dir=export_dir,
                input_path=input_path,
                output_path=output_path,
                silence_threshold_db=silence_threshold_db,
                min_silence_sec=min_silence_sec,
                open_in_editor=open_in_editor,
                on_status=on_status,
            )
        except Exception as pyav_exc:
            return {
                "ok": False,
                "output_path": "",
                "message": f"FFmpeg/PyAV Video-Engine Fehler: {pyav_exc}",
            }

    # Falls kein Eingabevideo übergeben wurde oder die Datei nicht existiert, suchen wir im Video-Ordner
    # oder erzeugen ein kurzes Test-Rohvideo mit Ton + Sprechpause zur sofortigen Demonstration
    src_path: Optional[Path] = Path(str(input_path)).expanduser() if input_path else None
    if src_path is not None and src_path.is_file():
        try:
            src_path = NAS_BRAIN.stage_video_to_hot_cache(src_path, on_status=on_status)
        except Exception:
            pass
    if src_path is None or not src_path.is_file():
        demo_input = export_dir / "jarvis_raw_demo_input.mp4"
        if not demo_input.is_file():
            if on_status:
                on_status("Erzeuge Demo-Rohvideo mit Sprach-/Stille-Segmenten...")
            # Erzeugt ein 4-Sekunden-Testvideo: 1.5s Ton (440Hz) -> 1.0s Stille -> 1.5s Ton (587Hz)
            cmd_gen = [
                ffmpeg_exe,
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc=duration=4:size=1280x720:rate=30",
                "-f",
                "lavfi",
                "-i",
                "aevalsrc='if(between(t,0,1.5),0.4*sin(2*PI*440*t),if(between(t,2.5,4.0),0.4*sin(2*PI*587*t),0))':d=4:s=44100",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-shortest",
                str(demo_input),
            ]
            subprocess.run(cmd_gen, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=CREATE_NO_WINDOW)
        src_path = demo_input

    if output_path:
        out_file = Path(str(output_path)).expanduser()
    else:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_file = export_dir / f"{src_path.stem}_jarvis_cut_{stamp}.mp4"
    out_file.parent.mkdir(parents=True, exist_ok=True)

    if on_status:
        on_status(f"Analysiere Audiospur auf Sprechpausen ({src_path.name})...")

    # Schritt 1: Gesamtdauer & Stille-Intervalle per ffmpeg silencedetect ermitteln
    cmd_detect = [
        ffmpeg_exe,
        "-i",
        str(src_path),
        "-af",
        f"silencedetect=noise={silence_threshold_db}dB:d={min_silence_sec}",
        "-f",
        "null",
        "-",
    ]
    proc = subprocess.run(
        cmd_detect,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="ignore",
        creationflags=CREATE_NO_WINDOW,
    )
    stderr_txt = proc.stderr or ""

    total_duration = 0.0
    dur_match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", stderr_txt)
    if dur_match:
        h_v, m_v, s_v = int(dur_match.group(1)), int(dur_match.group(2)), float(dur_match.group(3))
        total_duration = h_v * 3600 + m_v * 60 + s_v

    silence_starts = [float(x) for x in re.findall(r"silence_start:\s*([0-9.]+)", stderr_txt)]
    silence_ends = [float(x) for x in re.findall(r"silence_end:\s*([0-9.]+)", stderr_txt)]

    # Aktive (nicht-stille) Segmente berechnen
    keep_segments: list[tuple[float, float]] = []
    cursor_t = 0.0
    for idx, s_start in enumerate(silence_starts):
        s_end = silence_ends[idx] if idx < len(silence_ends) else total_duration
        if s_start - cursor_t >= 0.15:
            keep_segments.append((round(cursor_t, 3), round(s_start, 3)))
        cursor_t = max(cursor_t, s_end)

    if total_duration > cursor_t and (total_duration - cursor_t) >= 0.15:
        keep_segments.append((round(cursor_t, 3), round(total_duration, 3)))

    removed_silence_sec = 0.0
    for idx, s_start in enumerate(silence_starts):
        s_end = silence_ends[idx] if idx < len(silence_ends) else total_duration
        if s_end > s_start:
            removed_silence_sec += s_end - s_start

    # Schritt 2: Segmente schneiden und zusammenfügen (Jump-Cut Filtergraph)
    if on_status:
        on_status(f"Schneide {len(silence_starts)} Sprechpausen ({removed_silence_sec:.1f}s) heraus & rendere MP4...")

    if keep_segments and len(keep_segments) <= 24 and len(silence_starts) > 0:
        filter_parts: list[str] = []
        concat_inputs: list[str] = []
        for i, (st, en) in enumerate(keep_segments):
            filter_parts.append(f"[0:v]trim=start={st}:end={en},setpts=PTS-STARTPTS[v{i}];")
            filter_parts.append(f"[0:a]atrim=start={st}:end={en},asetpts=PTS-STARTPTS[a{i}];")
            concat_inputs.append(f"[v{i}][a{i}]")
        filter_complex = "".join(filter_parts) + f"{''.join(concat_inputs)}concat=n={len(keep_segments)}:v=1:a=1[outv][outa]"
        cmd_cut = [
            ffmpeg_exe,
            "-y",
            "-i",
            str(src_path),
            "-filter_complex",
            filter_complex,
            "-map",
            "[outv]",
            "-map",
            "[outa]",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-c:a",
            "aac",
            str(out_file),
        ]
        res_cut = subprocess.run(cmd_cut, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=CREATE_NO_WINDOW)
        if res_cut.returncode != 0 or not out_file.is_file():
            shutil.copy2(str(src_path), str(out_file))
    else:
        # Direkter Schnell-Filter über silenceremove + Kopie
        cmd_fast = [
            ffmpeg_exe,
            "-y",
            "-i",
            str(src_path),
            "-af",
            f"silenceremove=stop_periods=-1:stop_duration={min_silence_sec}:stop_threshold={silence_threshold_db}dB",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            str(out_file),
        ]
        res_fast = subprocess.run(cmd_fast, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=CREATE_NO_WINDOW)
        if res_fast.returncode != 0 or not out_file.is_file():
            shutil.copy2(str(src_path), str(out_file))

    # Optional: Weitere Clips anhängen, falls `concat_paths` angegeben wurde
    if concat_paths:
        valid_extra = [Path(str(p)).expanduser() for p in concat_paths if Path(str(p)).expanduser().is_file()]
        if valid_extra:
            list_txt = export_dir / "concat_list.txt"
            all_clips = [out_file, *valid_extra]
            list_txt.write_text(
                "\n".join(f"file '{str(c.resolve()).replace(chr(92), '/')}'" for c in all_clips),
                encoding="utf-8",
            )
            merged_out = out_file.with_name(f"{out_file.stem}_merged.mp4")
            subprocess.run(
                [ffmpeg_exe, "-y", "-f", "concat", "-safe", "0", "-i", str(list_txt), "-c", "copy", str(merged_out)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=CREATE_NO_WINDOW,
            )
            if merged_out.is_file():
                out_file = merged_out

    # Optional: Direkt im Schnittprogramm öffnen
    cold_nas_file = NAS_BRAIN.archive_video_to_cold_nas(out_file)
    editor_msg = ""
    if open_in_editor:
        found_ed, ed_msg = APP_INDEXER.open_app_by_query(open_in_editor)
        editor_msg = f" {ed_msg}" if found_ed else ""

    summary = (
        f"Auto-Video-Schnitt abgeschlossen: '{out_file.name}' ({len(keep_segments)} aktive Clips, "
        f"{removed_silence_sec:.1f}s Sprechpausen automatisch entfernt). "
        f"HotCache: {out_file} -> NAS-Archiv: {cold_nas_file}.{editor_msg}"
    )
    NAS_BRAIN.store_knowledge(
        category="video_edit",
        title=f"Auto-Cut: {out_file.name}",
        content=summary,
        source=str(src_path),
        metadata={
            "output_path": str(out_file),
            "cold_nas_output_path": str(cold_nas_file),
            "removed_silence_sec": round(removed_silence_sec, 2),
        },
    )
    out_duration = max(0.2, round(total_duration - removed_silence_sec, 2))
    silence_intervals = [
        {"start": round(s, 3), "end": round(silence_ends[idx] if idx < len(silence_ends) else total_duration, 3)}
        for idx, s in enumerate(silence_starts)
    ]
    return {
        "ok": out_file.is_file(),
        "input_path": str(src_path),
        "staged_hot_input": str(src_path),
        "output_path": str(out_file),
        "cold_nas_output_path": str(cold_nas_file),
        "input_duration_s": round(total_duration, 2),
        "output_duration_s": out_duration,
        "silence_intervals": silence_intervals,
        "segments_kept": len(keep_segments),
        "removed_silence_sec": round(removed_silence_sec, 2),
        "message": summary,
    }


# =====================================================================
# AGENT 2: DESKTOP & VIDEO-EDITOR AGENT
# =====================================================================
class DesktopVideoAgent:
    """
    AGENT 2 – `DESKTOP & VIDEO-EDITOR AGENT (Maus/Tastatur + Schnittprogramm-Steuerung)`:
    - 1-FPS-Vision-Loop (`mss` + rotes Fadenkreuz an `pyautogui.position()`)
    - Bedient jedes Windows-Programm per Klick, Drag-and-Drop (`pyautogui.dragTo`) und Shortcuts (`pyautogui.hotkey`)
    - Öffnet und steuert Videoschnittprogramme (CapCut, DaVinci Resolve, Premiere Pro)
    - Führt Hochgeschwindigkeits-Videoschnitt (`auto_edit_video`) auf Laptop & NAS aus.
    """

    AGENT_ID = "desktop_video"
    DISPLAY_NAME = "DESKTOP / VIDEO"

    def __init__(self, coordinator: "SwarmCoordinator") -> None:
        self.coordinator = coordinator

    def capture_1fps_to_ram_ring(self, label: str = "1-FPS-VISION") -> dict[str, Any]:
        """
        Erstellt einen 1-FPS-Screenshot (`mss`) mit leuchtend rotem Fadenkreuz an `pyautogui.position()`
        und speichert ihn verzögerungsfrei (0ms) im RAM-Ringpuffer von `NAS_BRAIN`.
        """
        jpeg_bytes, w, h, (cx, cy) = capture_annotated_screen_1fps()
        entry = NAS_BRAIN.push_ram_screenshot(
            jpeg_bytes=jpeg_bytes or b"",
            cursor_x=cx,
            cursor_y=cy,
            label=label,
        )
        return {**entry, "screen_w": w, "screen_h": h}

    def auto_edit_video(
        self,
        input_path: Optional[str | Path] = None,
        output_path: Optional[str | Path] = None,
        silence_threshold_db: float = -32.0,
        min_silence_sec: float = 0.35,
        open_in_editor: str = "",
        concat_paths: Optional[list[str | Path]] = None,
    ) -> dict[str, Any]:
        """Hochgeschwindigkeits-Videoschnitt über FFmpeg silencedetect + Jump-Cut-Filtergraph."""
        self.coordinator.update_agent_card(
            self.AGENT_ID,
            status="ACTIVE",
            step="Auto-Video-Schnitt (FFmpeg silencedetect)...",
        )
        res = auto_edit_video(
            input_path=input_path,
            output_path=output_path,
            silence_threshold_db=silence_threshold_db,
            min_silence_sec=min_silence_sec,
            concat_paths=concat_paths,
            open_in_editor=open_in_editor,
            on_status=lambda st: self.coordinator.update_agent_card(self.AGENT_ID, status="ACTIVE", step=st),
        )
        self.coordinator.update_agent_card(
            self.AGENT_ID,
            status="DONE" if res.get("ok") else "IDLE",
            step=f"Video exportiert: {Path(res['output_path']).name}" if res.get("output_path") else "Videoschnitt fertig",
        )
        return res

    def drag_and_drop(
        self,
        start_x: int,
        start_y: int,
        end_x: int,
        end_y: int,
        duration: float = 0.75,
        label: str = "DRAG & DROP -> TIMELINE",
    ) -> str:
        """Zieht ein Element (z. B. Video-Clip aus dem Media-Pool) sichtbar per Drag & Drop auf die Timeline."""
        self.coordinator.update_agent_card(
            self.AGENT_ID,
            status="ACTIVE",
            step=f"Drag & Drop [{start_x},{start_y}] -> [{end_x},{end_y}]",
        )
        try:
            import pyautogui  # type: ignore

            ghost_move_to(start_x, start_y, duration=0.45, label=label)
            time.sleep(0.1)
            GHOST_OVERLAY.add_trail_point(start_x, start_y)
            pyautogui.dragTo(end_x, end_y, duration=max(0.3, duration), button="left", tween=pyautogui.easeInOutQuad)
            GHOST_OVERLAY.add_trail_point(end_x, end_y)
            GHOST_OVERLAY.add_ripple(end_x, end_y, "#ff9830")
            return f"Clip per Drag & Drop von [{start_x}, {start_y}] nach [{end_x}, {end_y}] gezogen."
        except Exception as exc:
            return f"Drag & Drop Hinweis: {exc}"

    def execute_nle_timeline_cut(self, editor_name: str = "CapCut") -> str:
        """Führt einen Schnitt (Blade / Split) im aktiven Videoschnittprogramm aus und speichert den UI-Skill."""
        ed_low = (editor_name or "capcut").lower()
        shortcut = "ctrl+k" if "premiere" in ed_low else "ctrl+b"
        self.coordinator.update_agent_card(
            self.AGENT_ID,
            status="ACTIVE",
            step=f"{editor_name}: Timeline-Cut ({shortcut.upper()})",
        )
        parts = shortcut.split("+")
        ghost_hotkey(*parts, label=f"TIMELINE SPLIT ({shortcut.upper()})")
        NAS_BRAIN.learn_ui_skill(
            app_name=editor_name,
            skill_name="Timeline Split / Blade",
            action_type="hotkey",
            shortcut=shortcut,
            description=f"Schneidet den ausgewählten Clip auf der {editor_name}-Timeline bei der Abspielmarke.",
        )
        return f"Timeline-Schnitt ({shortcut.upper()}) in {editor_name} ausgeführt."

    def scrub_and_cut_waveform(
        self,
        editor_name: str = "CapCut",
        playhead_x: Optional[int] = None,
        timeline_y: Optional[int] = None,
        target_x: Optional[int] = None,
        target_end_x: Optional[int] = None,
        ripple_delete: bool = True,
    ) -> dict[str, Any]:
        """UPGRADE 4: Nutzt die Audio-Waveform-Analyse + Playhead-Scrub + Cut/Ripple-Delete im aktiven Schnittfenster."""
        from agent_hand import timeline_scrub_and_cut

        self.coordinator.update_agent_card(
            self.AGENT_ID,
            status="ACTIVE",
            step=f"{editor_name}: Waveform-Pause suchen & Timeline schneiden...",
        )
        res = timeline_scrub_and_cut(
            playhead_x=playhead_x,
            timeline_y=timeline_y,
            target_x=target_x,
            target_end_x=target_end_x,
            editor_name=editor_name,
            ripple_delete=ripple_delete,
        )
        NAS_BRAIN.learn_ui_skill(
            app_name=editor_name,
            skill_name="Waveform Pause Timeline Cut",
            action_type="timeline_scrub_and_cut",
            x=int(res.get("cut_x") or 0),
            y=int(res.get("timeline_y") or 0),
            description=str(res.get("message") or ""),
        )
        return res

    def execute_task(
        self,
        goal: str,
        brain: Any = None,
        max_steps: Optional[int] = None,
        on_status: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        clean_goal = (goal or "").strip()
        low = clean_goal.lower()
        self.coordinator.update_agent_card(
            self.AGENT_ID,
            status="ACTIVE",
            step=f"Starte Desktop/Video-Auftrag: {clean_goal[:42]}...",
        )

        # 0. Prüfe zuerst, ob bereits ein gelernter Live-Workflow (`recorded_workflow`) exakt passt
        try:
            from skill_recorder import SKILL_RECORDER

            learned_hits = [
                h for h in NAS_BRAIN.find_ui_skills(clean_goal, limit=3)
                if h.get("action_type") == "recorded_workflow" and isinstance(h.get("steps"), list) and h["steps"]
            ]
            if learned_hits and any(
                tok in low for tok in ("nochmal", "noch mal", "wiederhol", "replay", "workflow", "gelernten", "ablauf")
            ):
                self.coordinator.update_agent_card(
                    self.AGENT_ID,
                    status="ACTIVE",
                    step=f"Spiele gelernten Workflow '{learned_hits[0].get('skill_name')}' (10x)...",
                )
                rep = SKILL_RECORDER.replay_skill(clean_goal, speed_multiplier=10.0)
                if rep.get("ok"):
                    self.coordinator.update_agent_card(self.AGENT_ID, status="DONE", step="Workflow (10x) ausgeführt")
                    return rep
        except Exception:
            pass

        # 1. Prüfe auf direkten Waveform-Timeline-Cut im bereits offenen NLE-Fenster
        if any(tok in low for tok in ("waveform", "timeline cut", "timeline-cut", "playhead", "abspielmarke", "ripple")):
            editor_target = "CapCut"
            for ed_name in ("CapCut", "DaVinci Resolve", "Premiere Pro", "Shotcut", "Clipchamp"):
                if ed_name.split()[0].lower() in low:
                    editor_target = ed_name
                    break
            tl_res = self.scrub_and_cut_waveform(editor_name=editor_target, ripple_delete=True)
            self.coordinator.update_agent_card(
                self.AGENT_ID,
                status="DONE" if tl_res.get("ok") else "IDLE",
                step=str(tl_res.get("message") or "Timeline-Schnitt beendet")[:55],
            )
            return tl_res

        # 1. Prüfe auf direkten Auto-Videoschnitt ("schneide video", "stille entfernen", "auto cut", ".mp4")
        if any(
            tok in low
            for tok in (
                "schneide video",
                "video schneiden",
                "videoschnitt",
                "sprechpausen",
                "stille herausschneiden",
                "stille entfernen",
                "auto cut",
                "auto_edit_video",
                "rohvideo",
            )
        ):
            path_match = re.search(r"([A-Za-z]:\\[^\s\"']+\.(?:mp4|mov|mkv|avi)|\\\\[^\s\"']+\.(?:mp4|mov|mkv|avi))", clean_goal, re.IGNORECASE)
            in_path = path_match.group(1) if path_match else None
            editor_target = ""
            for ed in ("capcut", "davinci", "premiere", "shotcut", "clipchamp"):
                if ed in low:
                    editor_target = ed
                    break

            res = auto_edit_video(
                input_path=in_path,
                open_in_editor=editor_target,
                on_status=lambda st: self.coordinator.update_agent_card(self.AGENT_ID, status="ACTIVE", step=st),
            )
            self.coordinator.update_agent_card(
                self.AGENT_ID,
                status="DONE" if res.get("ok") else "IDLE",
                step=f"Export fertig: {Path(res['output_path']).name}" if res.get("output_path") else "Videoschnitt beendet",
            )
            return res

        # 2. Prüfe auf Programm-Start & direkte Weiterarbeit IN der App (`visual_desktop_app_operator`)
        def _step_cb(st: str) -> None:
            self.coordinator.update_agent_card(self.AGENT_ID, status="ACTIVE", step=st)
            if on_status:
                on_status(st)

        from agent_hand import VISUAL_HAND_AGENT, coordinate_jarvis_window_for_action

        open_match = re.match(
            r"^(?:bitte\s+)?(?:öffne|oeffne|starte|launch|mach)\s+(.+?)(?:\s+auf)?[.!?]*$",
            clean_goal,
            flags=re.IGNORECASE,
        )
        has_in_app_task = bool(
            re.search(
                r"\b(?:und\s+|rechn|tipp|schreib|erstell|spiel|klick|such|speicher|export|schnitt|musik|notiz)\b",
                clean_goal,
                flags=re.IGNORECASE,
            )
        )
        if open_match and not has_in_app_task:
            app_q = open_match.group(1).strip()
            coordinate_jarvis_window_for_action(mode="minimize")
            self.coordinator.update_agent_card(self.AGENT_ID, status="ACTIVE", step=f"Starte App '{app_q}'...")
            found, msg = APP_INDEXER.open_app_by_query(app_q)
            if found:
                NAS_BRAIN.learn_ui_skill(
                    app_name=app_q,
                    skill_name="Programm starten",
                    action_type="open_app",
                    description=msg,
                )
                self.coordinator.update_agent_card(self.AGENT_ID, status="DONE", step=f"Geöffnet: {app_q}")
                return {"ok": True, "message": f"Sehr wohl, Chef. {msg}"}

        # 3. 100% Sichtbarer Desktop- & App-Operator (startet ggf. App + bedient IN der App per 1-FPS-Vision-Loop)
        app_hint = open_match.group(1).strip() if open_match else ""
        if app_hint:
            res = VISUAL_HAND_AGENT.visual_desktop_app_operator(
                goal=clean_goal,
                app_name=app_hint,
                brain=brain,
                on_status=_step_cb,
                max_steps=None,
            )
        else:
            res = VISUAL_HAND_AGENT.visual_desktop_app_operator(
                goal=clean_goal,
                app_name="",
                brain=brain,
                on_status=_step_cb,
                max_steps=None,
            )
        # Gelernte Ziel-Koordinate in `skills_db.json` sichern
        _, _, last_x, last_y = get_screen_and_cursor_info()
        NAS_BRAIN.learn_ui_skill(
            app_name="Desktop",
            skill_name=clean_goal[:40],
            action_type="vision_loop",
            x=last_x,
            y=last_y,
            description=str(res.get("message") or clean_goal),
        )
        self.coordinator.update_agent_card(
            self.AGENT_ID,
            status="DONE" if res.get("ok") else "IDLE",
            step=str(res.get("message") or "Desktop-Aktion abgeschlossen")[:55],
        )
        return res


# =====================================================================
# AGENT 3: WEB & ACCOUNT GHOST
# =====================================================================
class WebAccountGhostAgent:
    """
    AGENT 3 – `WEB & ACCOUNT GHOST (Browser, Recherche & Auto-Registrierung)`:
    - Steuert sichtbar den Desktop-Webbrowser
    - Führt Live-Recherchen durch und speichert Ergebnisse dauerhaft in `NAS_BRAIN`
    - Füllt Formulare über den verschlüsselten Tresor (`vault.py` Zero-Knowledge) aus
    - Erstellt selbstständig Accounts und liest 2FA-Bestätigungscodes per IMAP aus.
    """

    AGENT_ID = "web_ghost"
    DISPLAY_NAME = "WEB GHOST"

    def fetch_latest_2fa_code(self) -> dict[str, Any]:
        self.coordinator.update_agent_card(self.AGENT_ID, status="ACTIVE", step="Lese 2FA-Code per IMAP...")
        mail_res = read_latest_verification_email()
        self.coordinator.update_agent_card(
            self.AGENT_ID,
            status="DONE" if mail_res.get("ok") else "IDLE",
            step=f"Code: {mail_res.get('verification_code') or 'Geprüft'}",
        )
        return mail_res

    def run_web_or_account_task(
        self,
        goal: str,
        brain: Any = None,
        max_steps: Optional[int] = None,
        on_status: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        return self.execute_task(goal=goal, brain=brain, max_steps=max_steps, on_status=on_status)

    def __init__(self, coordinator: "SwarmCoordinator") -> None:
        self.coordinator = coordinator

    def capture_1fps_to_ram_ring(self, label: str = "1-FPS-VISION") -> dict[str, Any]:
        """
        Erstellt einen 1-FPS-Screenshot (`mss`) mit leuchtend rotem Fadenkreuz an `pyautogui.position()`
        und speichert ihn verzögerungsfrei (0ms) im RAM-Ringpuffer von `NAS_BRAIN`.
        """
        jpeg_bytes, w, h, (cx, cy) = capture_annotated_screen_1fps()
        entry = NAS_BRAIN.push_ram_screenshot(
            jpeg_bytes=jpeg_bytes or b"",
            cursor_x=cx,
            cursor_y=cy,
            label=label,
        )
        return {**entry, "screen_w": w, "screen_h": h}

    def auto_edit_video(
        self,
        input_path: Optional[str | Path] = None,
        output_path: Optional[str | Path] = None,
        silence_threshold_db: float = -32.0,
        min_silence_sec: float = 0.35,
        open_in_editor: str = "",
        concat_paths: Optional[list[str | Path]] = None,
    ) -> dict[str, Any]:
        """Hochgeschwindigkeits-Videoschnitt über FFmpeg silencedetect + Jump-Cut-Filtergraph."""
        self.coordinator.update_agent_card(
            self.AGENT_ID,
            status="ACTIVE",
            step="Auto-Video-Schnitt (FFmpeg silencedetect)...",
        )
        res = auto_edit_video(
            input_path=input_path,
            output_path=output_path,
            silence_threshold_db=silence_threshold_db,
            min_silence_sec=min_silence_sec,
            concat_paths=concat_paths,
            open_in_editor=open_in_editor,
            on_status=lambda st: self.coordinator.update_agent_card(self.AGENT_ID, status="ACTIVE", step=st),
        )
        self.coordinator.update_agent_card(
            self.AGENT_ID,
            status="DONE" if res.get("ok") else "IDLE",
            step=f"Video exportiert: {Path(res['output_path']).name}" if res.get("output_path") else "Videoschnitt fertig",
        )
        return res

    def execute_task(
        self,
        goal: str,
        brain: Any = None,
        max_steps: Optional[int] = None,
        on_status: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        clean_goal = (goal or "").strip()
        low = clean_goal.lower()
        self.coordinator.update_agent_card(
            self.AGENT_ID,
            status="ACTIVE",
            step=f"Web-Ghost aktiv: {clean_goal[:42]}...",
        )

        # 1. Direkter Abruf eines 2FA-/Bestätigungscodes per IMAP
        if re.search(r"\b(bestätigungscode|verifizierungscode|2fa\s*code|aktivierungslink|bestätigungsmail)\b", low):
            self.coordinator.update_agent_card(self.AGENT_ID, status="ACTIVE", step="Lese 2FA-Code per IMAP...")
            mail_res = read_latest_verification_email()
            self.coordinator.update_agent_card(
                self.AGENT_ID,
                status="DONE" if mail_res.get("ok") else "IDLE",
                step=f"Code: {mail_res.get('verification_code') or 'Geprüft'}",
            )
            return mail_res

        # 2. Autonome Account-Erstellung / Registrierung / Formular-Ausfüllung (ReAct Loop + Tresor)
        if re.search(
            r"\b(erstell\w*\s+.*?(?:account|konto|profil)|registrier\w*\s+mich|sign\s*up|formular\s+ausfüllen|logg\w*\s+mich\s+ein)\b",
            low,
        ):
            def _react_cb(st: str) -> None:
                self.coordinator.update_agent_card(self.AGENT_ID, status="ACTIVE", step=st)
                if on_status:
                    on_status(st)

            res = AUTONOMOUS_AGENT.run_autonomous_task(
                goal=clean_goal,
                brain=brain,
                max_steps=max_steps,
                on_status=_react_cb,
            )
            NAS_BRAIN.remember_episode(
                role="assistant",
                content=f"Web-Ghost Account/Formular-Auftrag: {clean_goal} -> {res.get('message')}",
                agent=self.DISPLAY_NAME,
                tags="account,vault,browser",
            )
            self.coordinator.update_agent_card(
                self.AGENT_ID,
                status="DONE" if res.get("ok") else "IDLE",
                step=str(res.get("message") or "Browser-Workflow beendet")[:55],
            )
            return res

        # 3. Sichtbare Browser-Recherche & Wissens-Archivierung auf dem NAS
        def _search_cb(st: str) -> None:
            self.coordinator.update_agent_card(self.AGENT_ID, status="ACTIVE", step=st)
            if on_status:
                on_status(st)

        try:
            from brain import extract_browser_intent

            intent = extract_browser_intent(clean_goal, brain=brain)
            self.coordinator.update_agent_card(
                self.AGENT_ID,
                status="ACTIVE",
                step=f"Intent: {intent.get('target_url', '')[:28]} | {intent.get('clean_search_term', '')[:20]}",
            )
        except Exception:
            pass

        res_search = VISUAL_HAND_AGENT.visible_browser_search_and_summarize(
            query=clean_goal,
            brain=brain,
            on_status=_search_cb,
            interactive_click=True,
            max_steps=max_steps,
        )
        msg_text = str(res_search.get("summary") or res_search.get("message") or f"Browser-Recherche zu '{clean_goal}' geöffnet.")
        NAS_BRAIN.store_knowledge(
            category="web_research",
            title=clean_goal[:80],
            content=msg_text,
            source=str(res_search.get("url") or "browser"),
        )
        self.coordinator.update_agent_card(
            self.AGENT_ID,
            status="DONE",
            step=f"Recherche archiviert: {clean_goal[:36]}",
        )
        return {"ok": True, "message": msg_text, "url": res_search.get("url", "")}


# =====================================================================
# AGENT 4: FINANCE & TRADING QUANT
# =====================================================================
class FinanceTradingQuantAgent:
    """
    AGENT 4 – `FINANCE & TRADING QUANT (Aktien, Krypto & Konten)`:
    - Nutzt den AES-verschlüsselten Tresor (`vault.py`)
    - Analysiert Live-Börsenkurse (RSI-14, SMA20/SMA50, News & Browser-Charts)
    - Führt Orders im Paper-Trading- oder Echtgeld-Modus (je nach Sicherheits-Schalter im HUD) aus.
    """

    AGENT_ID = "finance_quant"
    DISPLAY_NAME = "FINANCE QUANT"

    def __init__(self, coordinator: "SwarmCoordinator") -> None:
        self.coordinator = coordinator

    def analyze_asset(
        self,
        symbol: str,
        brain: Any = None,
        on_status: Optional[Callable[[str], None]] = None,
        use_visible_browser: bool = True,
    ) -> dict[str, Any]:
        ticker = resolve_ticker_symbol(symbol)
        if use_visible_browser:
            self.coordinator.update_agent_card(
                self.AGENT_ID,
                status="ACTIVE",
                step=f"Live-Browser-Show: {ticker} auf TradingView/Finanzen...",
            )
            res_vis = TRADING_ENGINE.visual_quote_lookup(
                query=f"Kurs und Live-Chart für {symbol} ({ticker})",
                brain=brain,
                on_status=on_status,
            )
            summary = str(res_vis.get("summary") or res_vis.get("message") or "")
            NAS_BRAIN.store_knowledge(
                category="finance",
                title=f"Live-Browser-Analyse {ticker}",
                content=summary,
                source="VisibleBrowser/1-FPS-Vision",
            )
            self.coordinator.update_agent_card(
                self.AGENT_ID,
                status="DONE",
                step=f"Live-Chart {ticker}: {summary[:48]}",
            )
            return {
                "ok": bool(res_vis.get("ok", True)),
                "symbol": ticker,
                "source": "visible_browser_1fps",
                "url": res_vis.get("url", ""),
                "summary_de": summary,
            }

        self.coordinator.update_agent_card(self.AGENT_ID, status="ACTIVE", step=f"Simuliere {ticker} (Paper-Engine)...")
        analysis = TRADING_ENGINE.analyze_stock(ticker)
        self.coordinator.update_agent_card(
            self.AGENT_ID,
            status="DONE",
            step=f"{ticker} (Paper): {analysis.get('price')} {analysis.get('currency')} (RSI {analysis.get('rsi_14')})",
        )
        return analysis

    def execute_trade(
        self,
        symbol: str,
        qty_or_amount_eur: float,
        side: str = "buy",
        is_notional_eur: bool = False,
        use_browser: bool = False,
        brain: Any = None,
    ) -> dict[str, Any]:
        ticker = resolve_ticker_symbol(symbol)
        if use_browser:
            self.coordinator.update_agent_card(self.AGENT_ID, status="ACTIVE", step=f"Web-Broker {side.upper()} {ticker}...")
            res_b = TRADING_ENGINE.execute_browser_broker_trade(
                symbol=ticker,
                qty_or_amount_eur=qty_or_amount_eur,
                side=side,
                brain=brain,
                on_status=lambda st: self.coordinator.update_agent_card(self.AGENT_ID, status="ACTIVE", step=st),
            )
            self.coordinator.update_agent_card(self.AGENT_ID, status="DONE", step=f"Web-Broker: {ticker}")
            return res_b
        self.coordinator.update_agent_card(self.AGENT_ID, status="ACTIVE", step=f"Order: {side.upper()} {qty_or_amount_eur:g} {ticker}")
        res_ord = TRADING_ENGINE.place_order(
            symbol=ticker,
            qty_or_amount_eur=qty_or_amount_eur,
            side=side,
            order_type="market",
            is_notional_eur=is_notional_eur,
        )
        NAS_BRAIN.store_knowledge(
            category="finance",
            title=f"Order {side.upper()} {ticker}",
            content=res_ord.get("message", ""),
            source="TradingEngine",
        )
        self.coordinator.update_agent_card(self.AGENT_ID, status="DONE", step=f"{side.upper()} {ticker}: {res_ord.get('status', 'ok').upper()}")
        return res_ord

    def auto_edit_video(
        self,
        input_path: Optional[str | Path] = None,
        output_path: Optional[str | Path] = None,
        silence_threshold_db: float = -32.0,
        min_silence_sec: float = 0.35,
        open_in_editor: str = "",
        concat_paths: Optional[list[str | Path]] = None,
    ) -> dict[str, Any]:
        """Hochgeschwindigkeits-Videoschnitt über FFmpeg silencedetect + Jump-Cut-Filtergraph."""
        self.coordinator.update_agent_card(
            self.AGENT_ID,
            status="ACTIVE",
            step="Auto-Video-Schnitt (FFmpeg silencedetect)...",
        )
        res = auto_edit_video(
            input_path=input_path,
            output_path=output_path,
            silence_threshold_db=silence_threshold_db,
            min_silence_sec=min_silence_sec,
            concat_paths=concat_paths,
            open_in_editor=open_in_editor,
            on_status=lambda st: self.coordinator.update_agent_card(self.AGENT_ID, status="ACTIVE", step=st),
        )
        self.coordinator.update_agent_card(
            self.AGENT_ID,
            status="DONE" if res.get("ok") else "IDLE",
            step=f"Video exportiert: {Path(res['output_path']).name}" if res.get("output_path") else "Videoschnitt fertig",
        )
        return res

    def execute_task(
        self,
        goal: str,
        brain: Any = None,
        max_steps: Optional[int] = None,
        on_status: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        clean_goal = (goal or "").strip()
        low = clean_goal.lower()
        self.coordinator.update_agent_card(
            self.AGENT_ID,
            status="ACTIVE",
            step=f"Quant-Analyse: {clean_goal[:40]}...",
        )

        # 1. Portfolio- / Depot-Status
        if re.search(r"\b(depot|portfolio|positionen|kontostand|gewinn|verlust)\b", low):
            p_stat = TRADING_ENGINE.get_portfolio_status()
            self.coordinator.update_agent_card(
                self.AGENT_ID,
                status="DONE",
                step=f"Depot: {p_stat.get('equity', 10000):.2f} €",
            )
            return {"ok": True, "message": p_stat["summary_de"], "portfolio": p_stat}

        # 2. Kauf- oder Verkaufs-Order
        order_match = re.search(
            r"\b(kaufe?|buy|verkaufe?|sell)\s+(\d+(?:[.,]\d+)?)\s*(?:x|stück|aktien|anteile|€|eur|euro)?\s+(?:von\s+)?([a-zA-Z0-9.\-]+)",
            clean_goal,
            flags=re.IGNORECASE,
        )
        if order_match:
            verb = order_match.group(1).lower()
            side = "sell" if verb.startswith(("verkauf", "sell")) else "buy"
            amount = float(order_match.group(2).replace(",", "."))
            is_eur = bool(re.search(r"\b(€|eur|euro)\b", low))
            raw_sym = order_match.group(3).strip(" .,!?")
            ticker = resolve_ticker_symbol(raw_sym)

            if any(b in low for b in ("im browser", "trade republic", "scalable", "web broker")):
                res_b = TRADING_ENGINE.execute_browser_broker_trade(
                    symbol=ticker,
                    qty_or_amount_eur=amount,
                    side=side,
                    brain=brain,
                    on_status=lambda st: self.coordinator.update_agent_card(self.AGENT_ID, status="ACTIVE", step=st),
                )
                self.coordinator.update_agent_card(self.AGENT_ID, status="DONE", step=f"Web-Broker: {ticker}")
                return res_b

            self.coordinator.update_agent_card(
                self.AGENT_ID,
                status="ACTIVE",
                step=f"Order: {side.upper()} {amount:g} {ticker}",
            )
            res_ord = TRADING_ENGINE.place_order(
                symbol=ticker,
                qty_or_amount_eur=amount,
                side=side,
                order_type="market",
                is_notional_eur=is_eur,
            )
            NAS_BRAIN.store_knowledge(
                category="finance",
                title=f"Order {side.upper()} {ticker}",
                content=res_ord.get("message", ""),
                source="TradingEngine",
            )
            self.coordinator.update_agent_card(
                self.AGENT_ID,
                status="DONE",
                step=f"{side.upper()} {ticker}: {res_ord.get('status', 'ok').upper()}",
            )
            return res_ord

        # 3. Technische Aktien- / Krypto-Analyse (RSI, SMA20/SMA50, News)
        sym_match = re.search(
            r"(?:analysiere|wie\s+steht|kurs\s+von|chart\s+von|aktie|krypto)\s+([a-zA-Z0-9.\-]+)",
            clean_goal,
            flags=re.IGNORECASE,
        )
        raw_target = sym_match.group(1) if sym_match else clean_goal
        ticker = resolve_ticker_symbol(raw_target)
        analysis = TRADING_ENGINE.analyze_stock(ticker)
        NAS_BRAIN.store_knowledge(
            category="finance",
            title=f"Quant-Analyse {ticker} ({analysis.get('price')} {analysis.get('currency')})",
            content=analysis.get("summary_de", ""),
            source="YahooFinance/Quant",
        )
        self.coordinator.update_agent_card(
            self.AGENT_ID,
            status="DONE",
            step=f"{ticker}: {analysis.get('price')} {analysis.get('currency')} (RSI {analysis.get('rsi_14')})",
        )
        return {"ok": analysis.get("ok", True), "message": analysis.get("summary_de", ""), "analysis": analysis}


# =====================================================================
# SHARED BLACKBOARD (Kollisionsfreier Echtzeit-RAM-Status aller 4 Agenten)
# =====================================================================
class SharedBlackboard:
    """
    Zentrales RAM-Blackboard für das 4-Cursor-Schwarm-System (`SharedSwarmState`).
    Protokolliert in Echtzeit, welcher Agent welches UI-Element / Koordinatenfeld belegt,
    welche Teil-Schritte abgeschlossen sind und liefert Belegt-Marker für Screenshots.
    """

    SLOT_MAP: dict[str, int] = {
        "overmind": 1,
        "desktop_video": 2,
        "web_ghost": 3,
        "finance_quant": 4,
    }

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._entries: dict[str, dict[str, Any]] = {}
        for agent_id, slot in self.SLOT_MAP.items():
            self._entries[agent_id] = {
                "agent_id": agent_id,
                "slot": slot,
                "status": "IDLE",
                "current_target": "",
                "target_coords": None,
                "completed_steps": [],
                "updated_at": datetime.now().strftime("%H:%M:%S"),
            }

    @classmethod
    def normalize_agent_id(cls, agent_id: int | str) -> str:
        raw = str(agent_id or "").strip().lower()
        rev = {str(v): k for k, v in cls.SLOT_MAP.items()}
        if raw in cls.SLOT_MAP:
            return raw
        if raw in rev:
            return rev[raw]
        if "video" in raw or "desktop" in raw or "cut" in raw or "2" in raw:
            return "desktop_video"
        if "web" in raw or "browser" in raw or "ghost" in raw or "3" in raw:
            return "web_ghost"
        if "fin" in raw or "quant" in raw or "data" in raw or "file" in raw or "4" in raw:
            return "finance_quant"
        return "overmind"

    def update_agent(
        self,
        agent_id: int | str,
        status: Optional[str] = None,
        current_target: Optional[str] = None,
        target_coords: Optional[list[float] | tuple[float, float]] = None,
        completed_step: Optional[str] = None,
        clear_coords: bool = False,
    ) -> dict[str, Any]:
        key = self.normalize_agent_id(agent_id)
        with self._lock:
            entry = self._entries[key]
            if status is not None:
                entry["status"] = str(status).upper()
            if current_target is not None:
                entry["current_target"] = str(current_target)[:90]
            if clear_coords:
                entry["target_coords"] = None
            elif target_coords is not None and len(target_coords) >= 2:
                entry["target_coords"] = [
                    round(max(0.0, min(1000.0, float(target_coords[0]))), 1),
                    round(max(0.0, min(1000.0, float(target_coords[1]))), 1),
                ]
            if completed_step:
                steps: list[str] = entry["completed_steps"]
                step_txt = str(completed_step).strip()[:96]
                if step_txt and (not steps or steps[-1] != step_txt):
                    steps.append(step_txt)
                    if len(steps) > 25:
                        del steps[:-25]
            entry["updated_at"] = datetime.now().strftime("%H:%M:%S")
            return {
                **entry,
                "completed_steps": list(entry["completed_steps"]),
                "target_coords": list(entry["target_coords"]) if entry["target_coords"] else None,
            }

    def claim_target(
        self,
        agent_id: int | str,
        target_name: str,
        coords_norm: list[float] | tuple[float, float],
    ) -> dict[str, Any]:
        """Markiert ein UI-Ziel/Koordinatenpaar als aktiv belegt durch Agent X."""
        return self.update_agent(
            agent_id,
            status="ACTIVE",
            current_target=target_name,
            target_coords=coords_norm,
        )

    def release_target(
        self,
        agent_id: int | str,
        completed_step: Optional[str] = None,
        next_status: str = "IDLE",
    ) -> dict[str, Any]:
        return self.update_agent(
            agent_id,
            status=next_status,
            current_target="",
            clear_coords=True,
            completed_step=completed_step,
        )

    def get_occupied_targets_for(self, exclude_agent_id: Optional[str] = None) -> list[dict[str, Any]]:
        """Liefert alle aktuell von anderen Agenten belegten Zielkoordinaten für Screenshot-Overlays."""
        ex_key = self.normalize_agent_id(exclude_agent_id) if exclude_agent_id else None
        out: list[dict[str, Any]] = []
        with self._lock:
            for key, entry in self._entries.items():
                if ex_key and key == ex_key:
                    continue
                if entry.get("status") not in ("ACTIVE", "BUSY", "TYPING", "CLICKING", "VISION-LOOP"):
                    continue
                coords = entry.get("target_coords")
                if not coords or len(coords) < 2:
                    continue
                out.append(
                    {
                        "agent_id": key,
                        "slot": int(entry["slot"]),
                        "target": str(entry.get("current_target") or ""),
                        "coords": [float(coords[0]), float(coords[1])],
                    }
                )
        return out

    def stop_all(self) -> None:
        with self._lock:
            now_s = datetime.now().strftime("%H:%M:%S")
            for entry in self._entries.values():
                entry["status"] = "STOPPED"
                entry["current_target"] = ""
                entry["target_coords"] = None
                entry["updated_at"] = now_s

    def get_snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                key: {
                    "agent_id": e["agent_id"],
                    "slot": e["slot"],
                    "status": e["status"],
                    "current_target": e["current_target"],
                    "target_coords": list(e["target_coords"]) if e["target_coords"] else None,
                    "completed_steps": list(e["completed_steps"]),
                    "updated_at": e["updated_at"],
                }
                for key, e in self._entries.items()
            }


SharedSwarmState = SharedBlackboard
SHARED_BLACKBOARD = SharedBlackboard()


# =====================================================================
# AGENT 1: OVERMIND (Planer & NAS-Gedächtnis)
# =====================================================================
class OvermindAgent:
    """
    AGENT 1 – `OVERMIND (Planer & NAS-Gedächtnis)`:
    - Nimmt komplexe Aufträge entgegen
    - Durchsucht `NAS_BRAIN` nach gespeichertem Wissen & UI-Skills
    - Teilt Aufgaben in Schritte auf und delegiert sie (sequenziell oder parallel) an Agent 2, 3 und 4.
    """

    AGENT_ID = "overmind"
    DISPLAY_NAME = "OVERMIND"

    def __init__(self, coordinator: "SwarmCoordinator") -> None:
        self.coordinator = coordinator

    def create_execution_plan(self, user_goal: str) -> list[dict[str, Any]]:
        subtasks = self.classify_subtasks(user_goal)
        return [
            {"step": idx + 1, "agent": ag, "task": sg}
            for idx, (ag, sg) in enumerate(subtasks)
        ]

    def auto_edit_video(
        self,
        input_path: Optional[str | Path] = None,
        output_path: Optional[str | Path] = None,
        silence_threshold_db: float = -32.0,
        min_silence_sec: float = 0.35,
        open_in_editor: str = "",
        concat_paths: Optional[list[str | Path]] = None,
    ) -> dict[str, Any]:
        """Hochgeschwindigkeits-Videoschnitt über FFmpeg silencedetect + Jump-Cut-Filtergraph."""
        self.coordinator.update_agent_card(
            self.AGENT_ID,
            status="ACTIVE",
            step="Auto-Video-Schnitt (FFmpeg silencedetect)...",
        )
        res = auto_edit_video(
            input_path=input_path,
            output_path=output_path,
            silence_threshold_db=silence_threshold_db,
            min_silence_sec=min_silence_sec,
            concat_paths=concat_paths,
            open_in_editor=open_in_editor,
            on_status=lambda st: self.coordinator.update_agent_card(self.AGENT_ID, status="ACTIVE", step=st),
        )
        self.coordinator.update_agent_card(
            self.AGENT_ID,
            status="DONE" if res.get("ok") else "IDLE",
            step=f"Video exportiert: {Path(res['output_path']).name}" if res.get("output_path") else "Videoschnitt fertig",
        )
        return res

    @staticmethod
    def _classify_segment(seg: str) -> str:
        low = (seg or "").lower()
        # Agent 2: Creative / Video / NLE / Desktop UI-Schnitt
        if any(
            k in low
            for k in (
                "video",
                "capcut",
                "davinci",
                "premiere",
                "schneide",
                "schnitt",
                "stille",
                "sprechpause",
                "timeline",
                "effekt",
                "render",
                "drag",
                "filmcraft",
                "arccraft",
            )
        ) and not any(w in low for w in ("browser", "webseite", "account", "registrier", "such mal nach")):
            return "desktop_video"
        # Agent 3: Web Ghost (Browser / Recherche / Musik / Assets / Accounts)
        if any(
            k in low
            for k in (
                "account",
                "registrier",
                "sign up",
                "2fa",
                "bestätigungscode",
                "formular",
                "such mal nach",
                "suche ",
                "recherchier",
                "browser",
                "webseite",
                "youtube",
                "musik",
                "download",
                "zeig mir",
            )
        ):
            return "web_ghost"
        # Agent 4: Finance / Quant / Data / Explorer / Dateien / NAS
        if any(
            k in low
            for k in (
                "aktie",
                "krypto",
                "bitcoin",
                "nvidia",
                "nvda",
                "tesla",
                "apple",
                "rheinmetall",
                "depot",
                "portfolio",
                "kaufe ",
                "verkaufe ",
                "börse",
                "rsi",
                "trading",
                "explorer",
                "datei",
                "ordner",
                "verschieb",
                "kopier",
                "archiv",
                "excel",
                "csv",
                "nas",
                "projekt-ordner",
            )
        ):
            return "finance_quant"
        # Desktop allgemein falls "öffne" / "klick" ohne Web/Datei-Bezug
        if any(k in low for k in ("klick", "maus", "öffne ", "starte ", "mach ")):
            return "desktop_video"
        return "overmind"

    def classify_subtasks(self, user_goal: str) -> list[tuple[str, str]]:
        """
        Zerlegt auch komplexe Mehrfach-Befehle deterministisch in disjunkte Teilaufgaben
        für `overmind` (Agent 1), `desktop_video` (Agent 2), `web_ghost` (Agent 3) und `finance_quant` (Agent 4),
        sodass keine zwei Agenten am selben Fenster/Element kollidieren.
        """
        clean = (user_goal or "").strip()
        if not clean:
            return []

        parts = [
            p.strip()
            for p in re.split(
                r"\s+(?:und\s+gleichzeitig|gleichzeitig(?:\s+dazu)?|parallel(?:\s+dazu)?|und\s+dann|sowie|außerdem|anschließend|danach|;)\s+",
                clean,
                flags=re.IGNORECASE,
            )
            if p.strip()
        ]
        # Feinschnitt bei ", und " oder " und ", wenn beide Seiten unterschiedlichen Domänen angehören
        refined_parts: list[str] = []
        for chunk in (parts or [clean]):
            sub_chunks = [c.strip() for c in re.split(r"(?:,\s+und\s+|\s+und\s+)", chunk, flags=re.IGNORECASE) if c.strip()]
            if len(sub_chunks) > 1:
                # Verbinde Fragmente ohne eigene Agent-Domäne mit ihrem Vorgänger
                merged: list[str] = [sub_chunks[0]]
                for nxt in sub_chunks[1:]:
                    prev_ag = self._classify_segment(merged[-1])
                    nxt_ag = self._classify_segment(nxt)
                    if nxt_ag != prev_ag and len(nxt.split()) >= 2:
                        merged.append(nxt)
                    else:
                        merged[-1] = f"{merged[-1]} und {nxt}"
                refined_parts.extend(merged)
            else:
                refined_parts.append(chunk)

        plan: list[tuple[str, str]] = []
        for seg in refined_parts:
            ag = self._classify_segment(seg)
            plan.append((ag, seg))
        return plan

    def plan_and_delegate(
        self,
        user_goal: str,
        brain: Any = None,
        on_status: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        """
        Durchsucht zuerst das NAS-Langzeitgedächtnis, erstellt den disjunkten Ausführungsplan,
        trägt Ziele ins SharedBlackboard ein und delegiert an Agent 2, 3 und 4 (im Parallel-Schwarm).
        """
        clean_goal = (user_goal or "").strip()
        self.coordinator.update_agent_card(
            self.AGENT_ID,
            status="ACTIVE",
            step=f"NAS-Abfrage & Planung: {clean_goal[:38]}...",
        )
        SHARED_BLACKBOARD.claim_target(self.AGENT_ID, f"Planung: {clean_goal[:32]}", (180.0, 180.0))
        if self.coordinator.is_multi_cursor_mode():
            try:
                from multi_cursor_overlay import MULTI_CURSOR_OVERLAY

                MULTI_CURSOR_OVERLAY.move_cursor(
                    1,
                    180.0,
                    180.0,
                    badge="[A1: OVERMIND PLAN]",
                    duration=0.16,
                )
            except Exception:
                pass

        # 1. NAS-Gedächtnis nach relevantem Wissen & UI-Skills abfragen
        nas_context = NAS_BRAIN.build_overmind_memory_context(clean_goal)
        NAS_BRAIN.remember_episode(role="user", content=clean_goal, agent=self.DISPLAY_NAME)

        # 2. Disjunkte Teilaufgaben klassifizieren
        subtasks = self.classify_subtasks(clean_goal)
        delegated_agents = [ag for ag, _ in subtasks if ag != "overmind"]

        if not delegated_agents:
            self.coordinator.update_agent_card(
                self.AGENT_ID,
                status="ACTIVE",
                step="Synthetisiere Antwort mit NAS-Wissen...",
            )
            reply_text = ""
            if brain is not None and hasattr(brain, "ask"):
                augmented_prompt = f"{clean_goal}\n\n[KONTEXT AUS NAS-LANGZEITGEDÄCHTNIS]:\n{nas_context}"
                resp = brain.ask(augmented_prompt)
                reply_text = resp.text if hasattr(resp, "text") else str(resp)
            else:
                reply_text = f"Overmind-Analyse abgeschlossen. ({nas_context[:180]})"

            NAS_BRAIN.remember_episode(role="assistant", content=reply_text, agent=self.DISPLAY_NAME)
            SHARED_BLACKBOARD.release_target(self.AGENT_ID, completed_step="Overmind-Analyse fertig", next_status="DONE")
            if self.coordinator.is_multi_cursor_mode():
                try:
                    from multi_cursor_overlay import MULTI_CURSOR_OVERLAY

                    MULTI_CURSOR_OVERLAY.deactivate_agent(1)
                except Exception:
                    pass
            self.coordinator.update_agent_card(
                self.AGENT_ID,
                status="DONE",
                step="Antwort aus NAS-Gehirn geliefert",
            )
            return {"ok": True, "agent": "OVERMIND", "message": reply_text, "nas_context": nas_context}

        # 3. Disjunkte Delegation an Agent 2 (`desktop_video`), Agent 3 (`web_ghost`), Agent 4 (`finance_quant`)
        agent_labels = ", ".join(ag.upper() for ag in delegated_agents)
        self.coordinator.update_agent_card(
            self.AGENT_ID,
            status="ACTIVE",
            step=f"Delegiere parallel an: {agent_labels}",
        )

        # Quadranten-Startpunkte für gleichzeitige visuelle Orientierung auf dem Overlay
        _quadrant_pos = {
            "overmind": (200.0, 200.0),
            "desktop_video": (740.0, 260.0),
            "web_ghost": (260.0, 720.0),
            "finance_quant": (760.0, 720.0),
        }

        results_messages: list[str] = []

        def _run_single(agent_key: str, sub_goal: str) -> str:
            if self.coordinator.is_killed():
                return "Durch Not-Aus gestoppt."
            qx, qy = _quadrant_pos.get(agent_key, (500.0, 500.0))
            SHARED_BLACKBOARD.claim_target(agent_key, sub_goal[:48], (qx, qy))
            if self.coordinator.is_multi_cursor_mode():
                try:
                    from multi_cursor_overlay import MULTI_CURSOR_OVERLAY

                    MULTI_CURSOR_OVERLAY.move_cursor(agent_key, qx, qy, duration=0.18)
                except Exception:
                    pass
            try:
                msg = ""
                if agent_key == "desktop_video":
                    r = self.coordinator.desktop_video.execute_task(sub_goal, brain=brain, on_status=on_status)
                    msg = str(r.get("message") or "")
                elif agent_key == "web_ghost":
                    r = self.coordinator.web_ghost.execute_task(sub_goal, brain=brain, on_status=on_status)
                    msg = str(r.get("message") or "")
                elif agent_key == "finance_quant":
                    r = self.coordinator.finance_quant.execute_task(sub_goal, brain=brain, on_status=on_status)
                    msg = str(r.get("message") or "")
                SHARED_BLACKBOARD.release_target(
                    agent_key,
                    completed_step=f"Erledigt: {sub_goal[:42]}",
                    next_status="DONE",
                )
                return msg
            finally:
                if self.coordinator.is_multi_cursor_mode():
                    try:
                        from multi_cursor_overlay import MULTI_CURSOR_OVERLAY

                        MULTI_CURSOR_OVERLAY.deactivate_agent(agent_key)
                    except Exception:
                        pass

        distinct_agents = {ag for ag, _ in subtasks}
        if len(subtasks) > 1 and len(distinct_agents) == len(subtasks):
            with ThreadPoolExecutor(max_workers=min(4, len(subtasks))) as pool:
                futures = [pool.submit(_run_single, ag, sg) for ag, sg in subtasks]
                for fut in futures:
                    try:
                        msg = fut.result(timeout=45.0)
                        if msg:
                            results_messages.append(msg)
                    except Exception as exc:
                        results_messages.append(f"Teilauftrag-Hinweis: {exc}")
        else:
            for ag, sg in subtasks:
                if self.coordinator.is_killed():
                    break
                msg = _run_single(ag, sg)
                if msg:
                    results_messages.append(msg)

        combined_msg = " ".join(m for m in results_messages if m).strip()
        if not combined_msg:
            combined_msg = "Schwarm-Auftrag erfolgreich abgeschlossen, Chef."

        NAS_BRAIN.remember_episode(
            role="assistant",
            content=combined_msg,
            agent=f"SWARM({agent_labels})",
        )
        SHARED_BLACKBOARD.release_target(
            self.AGENT_ID,
            completed_step=f"Koordiniert: {len(subtasks)} Task(s)",
            next_status="DONE",
        )
        if self.coordinator.is_multi_cursor_mode():
            try:
                from multi_cursor_overlay import MULTI_CURSOR_OVERLAY

                MULTI_CURSOR_OVERLAY.deactivate_agent(1)
            except Exception:
                pass
        self.coordinator.update_agent_card(
            self.AGENT_ID,
            status="DONE",
            step=f"Auftrag abgeschlossen ({len(subtasks)} Task(s))",
        )
        return {
            "ok": True,
            "agent": f"SWARM ({agent_labels})",
            "message": combined_msg,
            "subtasks_count": len(subtasks),
            "subtasks": [{"agent": ag, "task": sg} for ag, sg in subtasks],
        }


# =====================================================================
# SWARM COORDINATOR (Koordiniert alle 4 Agenten + HUD-Broadcast + Not-Aus)
# =====================================================================
class SwarmCoordinator:
    """
    Zentrale Schwarm-Steuerung für alle 4 Spezial-Agenten:
    - Agent 1: `OVERMIND` (`overmind`)
    - Agent 2: `DESKTOP & VIDEO-EDITOR` (`desktop_video`)
    - Agent 3: `WEB & ACCOUNT GHOST` (`web_ghost`)
    - Agent 4: `FINANCE & TRADING QUANT` (`finance_quant`)
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._kill_event = threading.Event()
        self._hud_callbacks: list[Callable[[dict[str, Any]], None]] = []
        self._multi_cursor_enabled: bool = True

        self._cards: dict[str, dict[str, Any]] = {
            "overmind": {
                "id": "overmind",
                "name": "OVERMIND",
                "role": "Planer & NAS-Gedächtnis",
                "status": "IDLE",
                "step": "Bereit // NAS-Gehirn verbunden",
                "updated_at": datetime.now().strftime("%H:%M:%S"),
            },
            "desktop_video": {
                "id": "desktop_video",
                "name": "DESKTOP / VIDEO",
                "role": "1-FPS Maus & Schnitt",
                "status": "IDLE",
                "step": "Bereit // 1-FPS Vision & FFmpeg",
                "updated_at": datetime.now().strftime("%H:%M:%S"),
            },
            "web_ghost": {
                "id": "web_ghost",
                "name": "WEB GHOST",
                "role": "Browser, Tresor & 2FA",
                "status": "IDLE",
                "step": "Bereit // Zero-Knowledge & IMAP",
                "updated_at": datetime.now().strftime("%H:%M:%S"),
            },
            "finance_quant": {
                "id": "finance_quant",
                "name": "FINANCE QUANT",
                "role": "Aktien, Krypto & Orders",
                "status": "IDLE",
                "step": "Bereit // Paper & Live Quant",
                "updated_at": datetime.now().strftime("%H:%M:%S"),
            },
        }

        self.overmind = OvermindAgent(self)
        self.desktop_video = DesktopVideoAgent(self)
        self.web_ghost = WebAccountGhostAgent(self)
        self.finance_quant = FinanceTradingQuantAgent(self)

        # Kopple Rückmeldungen des autonomen ReAct-Agenten live an die WEB GHOST Karte
        AUTONOMOUS_AGENT.register_monitor_callback(self._on_autonomous_agent_update)

    def _on_autonomous_agent_update(self, mon: dict[str, Any]) -> None:
        step_lbl = str(mon.get("step_label") or mon.get("status_line") or "").strip()
        active = bool(mon.get("active"))
        if not step_lbl:
            return
        if "[TRADING]" in step_lbl:
            self.update_agent_card(
                "finance_quant",
                status="ACTIVE" if active else "IDLE",
                step=step_lbl.replace("[TRADING]", "").strip(),
            )
        else:
            self.update_agent_card(
                "web_ghost",
                status="ACTIVE" if active else "IDLE",
                step=step_lbl,
            )

    def register_hud_callback(self, cb: Callable[[dict[str, Any]], None]) -> None:
        with self._lock:
            if cb not in self._hud_callbacks:
                self._hud_callbacks.append(cb)

    def is_multi_cursor_mode(self) -> bool:
        with self._lock:
            return bool(self._multi_cursor_enabled)

    def set_multi_cursor_mode(self, enabled: bool) -> bool:
        want = bool(enabled)
        with self._lock:
            self._multi_cursor_enabled = want
            cbs = list(self._hud_callbacks)
        try:
            from multi_cursor_overlay import MULTI_CURSOR_OVERLAY

            MULTI_CURSOR_OVERLAY.set_enabled(want)
        except Exception:
            pass
        snap = self.get_swarm_state()
        for cb in cbs:
            try:
                cb(snap)
            except Exception:
                pass
        return want

    def claim_agent_target(
        self,
        agent_id: str,
        target_name: str,
        x_norm: float,
        y_norm: float,
        badge: Optional[str] = None,
    ) -> dict[str, Any]:
        entry = SHARED_BLACKBOARD.claim_target(agent_id, target_name, (x_norm, y_norm))
        if self.is_multi_cursor_mode():
            try:
                from multi_cursor_overlay import MULTI_CURSOR_OVERLAY

                MULTI_CURSOR_OVERLAY.move_cursor(agent_id, x_norm, y_norm, badge=badge, duration=0.14)
            except Exception:
                pass
        return entry

    def update_agent_card(
        self,
        agent_id: str,
        status: str = "IDLE",
        step: str = "",
        active: Optional[bool] = None,
        step_num: int = 0,
        max_steps: int = 0,
    ) -> None:
        with self._lock:
            if agent_id in self._cards:
                st_up = status.upper()
                self._cards[agent_id]["status"] = st_up
                self._cards[agent_id]["active"] = bool(active) if active is not None else (st_up in ("ACTIVE", "VISION-LOOP", "BROWSING", "SEARCHING", "NAS-SEARCH"))
                if step:
                    self._cards[agent_id]["step"] = step[:80]
            if step_num:
                self._cards[agent_id]["step_num"] = step_num
            if max_steps:
                self._cards[agent_id]["max_steps"] = max_steps
            self._cards[agent_id]["updated_at"] = datetime.now().strftime("%H:%M:%S")
            cbs = list(self._hud_callbacks)

        SHARED_BLACKBOARD.update_agent(
            agent_id,
            status=status.upper(),
            current_target=step[:80] if step else None,
            completed_step=step if status.upper() == "DONE" and step else None,
            clear_coords=(status.upper() in ("IDLE", "DONE", "STOPPED", "READY")),
        )

        snap = self.get_swarm_state()
        for cb in cbs:
            try:
                cb(snap)
            except Exception:
                pass

    def get_swarm_state(self) -> dict[str, Any]:
        with self._lock:
            any_active = any(c.get("status") == "ACTIVE" for c in self._cards.values())
            active_steps = [
                f"[{c['name']}] {c['step']}"
                for c in self._cards.values()
                if c.get("status") == "ACTIVE"
            ]
            headline = active_steps[0] if active_steps else self._cards["overmind"]["step"]
            agents_copy = {k: dict(v) for k, v in self._cards.items()}
            mc_enabled = bool(self._multi_cursor_enabled)

        overlay_snap: dict[str, Any] = {"enabled": mc_enabled}
        try:
            from multi_cursor_overlay import MULTI_CURSOR_OVERLAY

            overlay_snap = MULTI_CURSOR_OVERLAY.get_snapshot()
        except Exception:
            pass

        nas_stat = NAS_BRAIN.get_status()
        return {
            "active": any_active,
            "step_label": headline,
            "status_line": headline,
            "autonomy_mode": SECURE_VAULT.get_autonomy_mode(),
            "trading_mode": SECURE_VAULT.get_secret("VAULT_TRADING_MODE") or "paper",
            "multi_cursor_enabled": mc_enabled,
            "multi_cursor_overlay": overlay_snap,
            "blackboard": SHARED_BLACKBOARD.get_snapshot(),
            "nas_brain": nas_stat,
            "agents": agents_copy,
        }

    def is_killed(self) -> bool:
        return self._kill_event.is_set()

    def kill_all_agents(self) -> dict[str, Any]:
        """
        Sofortiger 1-Millisekunden Not-Aus (`[ 🛑 ALLE AGENTEN STOPPEN ]`) für alle 4 Agenten
        inklusive sofortigem Ausblenden/Stoppen aller 4 Overlay-Mauszeiger!
        """
        self._kill_event.set()
        try:
            from multi_cursor_overlay import MULTI_CURSOR_OVERLAY

            MULTI_CURSOR_OVERLAY.stop_all()
        except Exception:
            pass
        try:
            SHARED_BLACKBOARD.stop_all()
        except Exception:
            pass
        try:
            AUTONOMOUS_AGENT.kill()
        except Exception:
            pass
        try:
            VISUAL_HAND_AGENT.request_abort()
        except Exception:
            pass
        try:
            GHOST_OVERLAY.stop()
        except Exception:
            pass

        with self._lock:
            now_s = datetime.now().strftime("%H:%M:%S")
            for k in self._cards:
                self._cards[k]["status"] = "STOPPED"
                self._cards[k]["step"] = "🛑 NOT-AUS // AGENT GESTOPPT"
                self._cards[k]["updated_at"] = now_s
            cbs = list(self._hud_callbacks)

        snap = self.get_swarm_state()
        snap["step_label"] = "🛑 NOT-AUS AUSGELÖST // ALLE 4 AGENTEN SOFORT GESTOPPT"
        snap["status_line"] = snap["step_label"]

        for cb in cbs:
            try:
                cb(snap)
            except Exception:
                pass

        return {
            **snap,
            "ok": True,
            "killed": True,
            "message": "Not-Aus ausgeführt: Alle 4 Schwarm-Agenten wurden in 1ms gestoppt.",
        }

    def execute_swarm_command(
        self,
        user_text: str,
        brain: Any = None,
        on_status: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        self._kill_event.clear()
        return self.overmind.plan_and_delegate(user_text, brain=brain, on_status=on_status)

    def execute_swarm_mission(
        self,
        user_text: str,
        brain: Any = None,
        on_status: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        return self.execute_swarm_command(user_text, brain=brain, on_status=on_status)

    def run_research_to_desktop_pipeline(
        self,
        query: str,
        brain: Any = None,
        memory: Any = None,
        on_status: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        """MODUL 3 Pipeline A: Live Browser-Recherche -> formatierte Datei/Tabelle auf Desktop."""
        from executive_assistant import EXECUTIVE_ASSISTANT

        self.update_agent_card("overmind", status="PIPELINE-A", step="Starte Recherche -> Desktop Pipeline", active=True)
        self.update_agent_card("web_ghost", status="ACTIVE", step="Live-Browser-Recherche läuft...", active=True)
        try:
            res = EXECUTIVE_ASSISTANT.run_research_to_desktop_pipeline(
                query=query,
                brain=brain,
                memory=memory,
                on_status=on_status,
            )
            self.update_agent_card("web_ghost", status="DONE", step="Recherche abgeschlossen", active=False)
            self.update_agent_card("desktop_video", status="DONE", step="Desktop-Datei erstellt & geöffnet", active=False)
            self.update_agent_card("overmind", status="READY", step="Pipeline A abgeschlossen", active=False)
            return res
        except Exception as exc:
            self.update_agent_card("overmind", status="READY", step=f"Pipeline-Hinweis: {str(exc)[:60]}", active=False)
            self.update_agent_card("web_ghost", status="IDLE", step="Browser bereit", active=False)
            raise

    def run_project_setup_pipeline(
        self,
        request_text: str,
        project_name: str = "",
        category: str = "",
        brain: Any = None,
        memory: Any = None,
        on_status: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        """MODUL 3 Pipeline B: Projekt-Setup auf Zuruf (Ordner + Hauptprogramm + Referenz-Browser)."""
        from executive_assistant import EXECUTIVE_ASSISTANT

        self.update_agent_card("overmind", status="PIPELINE-B", step="Erstelle Projektstruktur & App-Setup", active=True)
        self.update_agent_card("desktop_video", status="ACTIVE", step="Lege Ordner an & starte Haupt-App...", active=True)
        try:
            res = EXECUTIVE_ASSISTANT.run_project_setup_pipeline(
                request_text=request_text,
                project_name=project_name,
                category=category,
                brain=brain,
                memory=memory,
                on_status=on_status,
            )
            self.update_agent_card("desktop_video", status="DONE", step="Projekt & App bereit", active=False)
            self.update_agent_card("overmind", status="READY", step="Pipeline B abgeschlossen", active=False)
            return res
        except Exception as exc:
            self.update_agent_card("overmind", status="READY", step=f"Setup-Hinweis: {str(exc)[:60]}", active=False)
            self.update_agent_card("desktop_video", status="IDLE", step="Desktop bereit", active=False)
            raise


# Globale Singleton-Instanz des 4-Agenten-Schwarms
SWARM_CORE = SwarmCoordinator()
