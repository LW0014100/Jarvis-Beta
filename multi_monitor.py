# -*- coding: utf-8 -*-
"""
Multi-Monitor-Erkennung, gezielte Screen-Vision (Monitor 1/2/3/Alle)
und Fenster-Platzierung für Windows 11 & Raspberry Pi 5.
"""

from __future__ import annotations

import io
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Optional

from PIL import Image, ImageDraw, ImageFont


def _load_pil_font(size: int = 16) -> Any:
    for candidate in (
        "C:/Windows/Fonts/consolab.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ):
        if os.path.isfile(candidate):
            try:
                return ImageFont.truetype(candidate, size=size)
            except Exception:
                pass
    return ImageFont.load_default()


def get_connected_monitors() -> list[dict[str, Any]]:
    """
    Erkennt alle angeschlossenen Monitore inkl. Auflösung, Offset und logischer Position (links/mitte/rechts).
    Kombiniert `mss` (echte Grab-Indizes) und `screeninfo` (Namen & Primary-Flag), sortiert von links nach rechts.
    """
    mss_entries: list[dict[str, Any]] = []
    try:
        import mss  # type: ignore

        with mss.mss() as sct:
            for mss_idx, m in enumerate(sct.monitors[1:], start=1):
                w = int(m.get("width", 0) or 0)
                h = int(m.get("height", 0) or 0)
                if w <= 0 or h <= 0:
                    continue
                mss_entries.append(
                    {
                        "mss_index": mss_idx,
                        "x": int(m.get("left", 0) or 0),
                        "y": int(m.get("top", 0) or 0),
                        "w": w,
                        "h": h,
                        "name": f"Display {mss_idx}",
                        "primary": mss_idx == 1,
                    }
                )
    except Exception:
        pass

    si_entries: list[dict[str, Any]] = []
    try:
        from screeninfo import get_monitors  # type: ignore

        for idx, m in enumerate(get_monitors(), start=1):
            w = int(getattr(m, "width", 0) or 0)
            h = int(getattr(m, "height", 0) or 0)
            if w <= 0 or h <= 0:
                continue
            si_entries.append(
                {
                    "mss_index": idx,
                    "x": int(getattr(m, "x", 0) or 0),
                    "y": int(getattr(m, "y", 0) or 0),
                    "w": w,
                    "h": h,
                    "name": str(getattr(m, "name", "") or f"Display {idx}").strip(),
                    "primary": bool(getattr(m, "is_primary", idx == 1)),
                }
            )
    except Exception:
        pass

    raw = mss_entries if mss_entries else si_entries
    if mss_entries and si_entries:
        # Reichere mss-Monitore über exakten Offset/Größenabgleich mit screeninfo-Metadaten an
        for entry in raw:
            best = None
            best_dist = 10**9
            for si in si_entries:
                dist = abs(si["x"] - entry["x"]) + abs(si["y"] - entry["y"]) + abs(si["w"] - entry["w"])
                if dist < best_dist:
                    best_dist = dist
                    best = si
            if best is not None and best_dist <= 160:
                if best["name"]:
                    entry["name"] = best["name"]
                entry["primary"] = bool(best["primary"])

    if not raw:
        raw = [
            {
                "mss_index": 1,
                "x": 0,
                "y": 0,
                "w": 1920,
                "h": 1080,
                "name": "Primary Display",
                "primary": True,
            }
        ]

    # Sortiere physisch von links nach rechts (dann oben nach unten)
    raw.sort(key=lambda item: (item["x"], item["y"]))
    count = len(raw)
    result: list[dict[str, Any]] = []
    for idx, item in enumerate(raw, start=1):
        if count == 1:
            pos = "mitte"
        elif count == 2:
            pos = "links" if idx == 1 else "rechts"
        else:
            if idx == 1:
                pos = "links"
            elif idx == count:
                pos = "rechts"
            elif count == 3 and idx == 2:
                pos = "mitte"
            else:
                pos = f"mitte-{idx}"

        clean_name = re.sub(r"^\\\\\.\\", "", str(item.get("name") or f"Monitor {idx}")).strip()
        result.append(
            {
                "index": idx,
                "mss_index": int(item.get("mss_index", idx)),
                "name": clean_name or f"Monitor {idx}",
                "label": f"M{idx} · {pos.upper()}",
                "pos": pos,
                "x": int(item["x"]),
                "y": int(item["y"]),
                "w": int(item["w"]),
                "h": int(item["h"]),
                "primary": bool(item.get("primary", False)),
            }
        )
    return result


def resolve_monitor_selector(
    target: Any = "primary",
    monitors: Optional[list[dict[str, Any]]] = None,
) -> tuple[Any, Optional[dict[str, Any]]]:
    """Löst '1/2/3', 'links/mitte/rechts', 'primary' oder 'all' in konkrete Monitore auf."""
    mons = monitors if monitors is not None else get_connected_monitors()
    if not mons:
        return 1, None

    if isinstance(target, int):
        idx = max(1, min(len(mons), target))
        return idx, mons[idx - 1]

    raw = str(target or "primary").strip().lower()
    if raw in ("cursor", "mouse", "maus", "auto"):
        c_idx, c_mon, _ = get_cursor_monitor(mons)
        return c_idx, c_mon

    if raw in ("all", "alle", "all_screens", "3x", "panorama", "alle drei", "alle monitore"):
        return "all", None

    # Explizite Zahl (z. B. "monitor 2", "bildschirm 3", "m1")
    num_m = re.search(r"\b(?:monitor|bildschirm|screen|display|m)?\s*([1-9])\b", raw)
    if num_m:
        idx = max(1, min(len(mons), int(num_m.group(1))))
        return idx, mons[idx - 1]

    if any(k in raw for k in ("link", "left")):
        for m in mons:
            if m["pos"] == "links":
                return m["index"], m
        return mons[0]["index"], mons[0]

    if any(k in raw for k in ("recht", "right")):
        for m in mons:
            if m["pos"] == "rechts":
                return m["index"], m
        return mons[-1]["index"], mons[-1]

    if any(k in raw for k in ("mitt", "center", "centre", "zentr")):
        for m in mons:
            if m["pos"] == "mitte":
                return m["index"], m
        mid = mons[len(mons) // 2]
        return mid["index"], mid

    if any(k in raw for k in ("haupt", "primary", "main", "primär")):
        for m in mons:
            if m["primary"]:
                return m["index"], m
        return mons[0]["index"], mons[0]

    for m in mons:
        if m["primary"]:
            return m["index"], m
    return mons[0]["index"], mons[0]


def extract_monitor_target_from_text(text: str) -> Optional[str | int]:
    """Erkennt einen gewünschten Ziel-Monitor (links/mitte/rechts/1/2/3/alle) aus natürlicher Sprache."""
    q = (text or "").lower()
    if re.search(r"\b(?:alle\s*(?:drei|3|monitore|bildschirme|screens)|gesamtansicht|alle\s+screens)\b", q):
        return "all"
    num_m = re.search(r"\b(?:monitor|bildschirm|screen|display)\s*([1-9])\b", q)
    if num_m:
        return int(num_m.group(1))
    if re.search(r"\b(?:auf\s+(?:den|dem|meinen|meinem)\s+)?link(?:en|er|es|s)\b", q):
        return "links"
    if re.search(r"\b(?:auf\s+(?:den|dem|meinen|meinem)\s+)?recht(?:en|er|es|s)\b", q):
        return "rechts"
    if re.search(r"\b(?:mittler(?:e|en|er)|zentral(?:e|en)|haupt(?:monitor|bildschirm))\b", q):
        return "mitte"
    return None


def _grab_single_mss_image(sct: Any, monitor_info: dict[str, Any]) -> Image.Image:
    mss_idx = int(monitor_info.get("mss_index", 1))
    if 0 < mss_idx < len(sct.monitors):
        raw = sct.grab(sct.monitors[mss_idx])
    else:
        raw = sct.grab(
            {
                "left": int(monitor_info["x"]),
                "top": int(monitor_info["y"]),
                "width": int(monitor_info["w"]),
                "height": int(monitor_info["h"]),
            }
        )
    return Image.frombytes("RGB", raw.size, raw.rgb)


def _draw_compact_grid(img: Image.Image, label: str = "") -> Image.Image:
    out = img.convert("RGB")
    draw = ImageDraw.Draw(out)
    w, h = out.size
    font = _load_pil_font(14)
    for x in range(200, w, 200):
        draw.line([(x, 0), (x, h)], fill=(0, 210, 255), width=1)
        draw.text((x + 4, 4), str(x), fill=(0, 240, 255), font=font)
    for y in range(200, h, 200):
        draw.line([(0, y), (w, y)], fill=(0, 210, 255), width=1)
        draw.text((4, y + 4), str(y), fill=(0, 240, 255), font=font)
    if label:
        draw.rectangle([(8, 8), (8 + max(160, len(label) * 9 + 18), 32)], fill=(4, 14, 24), outline=(0, 216, 255), width=1)
        draw.text((16, 12), label, fill=(130, 245, 255), font=font)
    return out


def capture_monitor_jpeg(
    target: Any = "primary",
    quality: int = 80,
    max_width: int = 1440,
    with_grid: bool = False,
    on_scan: Optional[Callable[[Any, str], None]] = None,
) -> dict[str, Any]:
    """
    Erstellt einen gezielten Screenshot von Monitor 1, 2, 3 oder allen angeschlossenen Bildschirmen.
    """
    mons = get_connected_monitors()
    resolved, chosen = resolve_monitor_selector(target, mons)

    if on_scan is not None:
        try:
            on_scan(resolved, f"Scanne Monitor: {resolved}")
        except Exception:
            pass

    # Ghost-Overlay nur kurz ausblenden, falls es bereits geladen ist (kein unnoetiger Tkinter-Start)
    ghost = None
    if "ghost_cursor" in sys.modules:
        try:
            ghost = sys.modules["ghost_cursor"].GHOST_OVERLAY
            ghost.set_capture_stealth(True)
        except Exception:
            ghost = None

    captured_ids: list[int] = []
    final_img: Optional[Image.Image] = None

    try:
        try:
            import mss  # type: ignore

            with mss.mss() as sct:
                if resolved == "all":
                    panels: list[Image.Image] = []
                    target_h = 540
                    font = _load_pil_font(16)
                    for m in mons:
                        m_img = _grab_single_mss_image(sct, m)
                        scale = target_h / max(1, m_img.height)
                        tw = max(160, int(round(m_img.width * scale)))
                        m_img = m_img.resize((tw, target_h), Image.Resampling.LANCZOS)
                        d = ImageDraw.Draw(m_img)
                        d.rectangle([(0, 0), (tw - 1, target_h - 1)], outline=(0, 216, 255), width=2)
                        badge = f"M{m['index']} [{m['pos'].upper()}] {m['w']}x{m['h']}"
                        d.rectangle([(6, 6), (min(tw - 6, len(badge) * 10 + 20), 32)], fill=(4, 14, 26), outline=(0, 216, 255))
                        d.text((12, 11), badge, fill=(0, 240, 255), font=font)
                        panels.append(m_img)
                        captured_ids.append(m["index"])

                    gap = 8
                    total_w = sum(p.width for p in panels) + gap * max(0, len(panels) - 1)
                    stitched = Image.new("RGB", (max(1, total_w), target_h), (4, 8, 16))
                    cursor_x = 0
                    for p in panels:
                        stitched.paste(p, (cursor_x, 0))
                        cursor_x += p.width + gap
                    final_img = stitched
                else:
                    assert chosen is not None
                    final_img = _grab_single_mss_image(sct, chosen)
                    captured_ids.append(int(chosen["index"]))
        except Exception as exc:
            # Wayland grim Fallback auf Raspberry Pi 5
            if platform.system() == "Linux" and shutil.which("grim"):
                proc = subprocess.run(
                    ["grim", "-t", "jpeg", "-q", str(quality), "-"],
                    capture_output=True,
                    timeout=4.0,
                    check=False,
                )
                if proc.returncode == 0 and proc.stdout:
                    final_img = Image.open(io.BytesIO(proc.stdout)).convert("RGB")
                    captured_ids.append(1)
            if final_img is None:
                return {
                    "ok": False,
                    "target": resolved,
                    "monitors_captured": [],
                    "jpeg_bytes": b"",
                    "width": 0,
                    "height": 0,
                    "summary": f"Screenshot fehlgeschlagen: {exc}",
                }
    finally:
        if ghost is not None:
            try:
                ghost.set_capture_stealth(False)
            except Exception:
                pass

    if max_width > 0 and final_img.width > max_width:
        ratio = max_width / float(final_img.width)
        final_img = final_img.resize(
            (max_width, max(1, int(round(final_img.height * ratio)))),
            Image.Resampling.LANCZOS,
        )

    if with_grid:
        lbl = (
            f"ALLE MONITORE ({len(captured_ids)})"
            if resolved == "all"
            else f"MONITOR {chosen['index']} ({chosen['pos'].upper()}) {chosen['w']}x{chosen['h']}"
        )
        final_img = _draw_compact_grid(final_img, label=lbl)

    buf = io.BytesIO()
    final_img.save(buf, format="JPEG", quality=max(40, min(95, int(quality))))
    jpeg_bytes = buf.getvalue()

    if resolved == "all":
        summary = f"Alle {len(captured_ids)} Monitore ({', '.join(f'M{i}' for i in captured_ids)}) erfasst ({final_img.width}x{final_img.height})."
    else:
        assert chosen is not None
        summary = f"Monitor {chosen['index']} ({chosen['pos']}, {chosen['w']}x{chosen['h']}) erfasst."

    return {
        "ok": True,
        "target": resolved,
        "monitor": chosen,
        "monitors_captured": captured_ids,
        "jpeg_bytes": jpeg_bytes,
        "width": final_img.width,
        "height": final_img.height,
        "summary": summary,
    }


def list_open_windows() -> list[dict[str, Any]]:
    """Listet sichtbare Anwendungsfenster samt Titel und aktuellem Rechteck."""
    out: list[dict[str, Any]] = []
    if platform.system() == "Windows":
        try:
            import pygetwindow as gw  # type: ignore

            for win in gw.getAllWindows():
                title = str(getattr(win, "title", "") or "").strip()
                if not title or len(title) < 2:
                    continue
                if int(getattr(win, "width", 0) or 0) <= 80 or int(getattr(win, "height", 0) or 0) <= 60:
                    continue
                out.append(
                    {
                        "title": title,
                        "left": int(getattr(win, "left", 0) or 0),
                        "top": int(getattr(win, "top", 0) or 0),
                        "width": int(getattr(win, "width", 0) or 0),
                        "height": int(getattr(win, "height", 0) or 0),
                    }
                )
        except Exception:
            pass
    elif platform.system() == "Linux" and shutil.which("wmctrl"):
        try:
            res = subprocess.run(["wmctrl", "-lG"], capture_output=True, text=True, timeout=2.0, check=False)
            for line in (res.stdout or "").splitlines():
                parts = line.split(None, 7)
                if len(parts) >= 8:
                    out.append(
                        {
                            "id": parts[0],
                            "left": int(parts[2]),
                            "top": int(parts[3]),
                            "width": int(parts[4]),
                            "height": int(parts[5]),
                            "title": parts[7].strip(),
                        }
                    )
        except Exception:
            pass
    return out


def compute_window_rect_for_monitor(
    monitor: dict[str, Any],
    layout: str = "maximize",
    margin: int = 24,
) -> tuple[int, int, int, int]:
    """Berechnet `(x, y, width, height)` auf einem Ziel-Monitor für ein gegebenes Layout."""
    mx, my, mw, mh = int(monitor["x"]), int(monitor["y"]), int(monitor["w"]), int(monitor["h"])
    lay = (layout or "maximize").strip().lower()
    if lay in ("left", "left_half", "links"):
        return mx + 8, my + 8, max(320, mw // 2 - 12), max(240, mh - 56)
    if lay in ("right", "right_half", "rechts"):
        half = mw // 2
        return mx + half + 4, my + 8, max(320, mw - half - 12), max(240, mh - 56)
    if lay in ("center", "mitte", "window", "popout"):
        w = max(480, int(mw * 0.72))
        h = max(360, int(mh * 0.78))
        return mx + (mw - w) // 2, my + (mh - h) // 2, w, h
    # Standard: fast vollflächig (oder sauber mit kleinem Rand vor Maximierung)
    return mx + margin, my + margin, max(480, mw - 2 * margin), max(360, mh - 2 * margin - 40)


def move_window_to_monitor(
    window_query: str = "",
    monitor_target: Any = 1,
    layout: str = "maximize",
) -> dict[str, Any]:
    """Verschiebt ein passendes Programmfenster gezielt auf Monitor 1, 2 oder 3 und ordnet es an."""
    mons = get_connected_monitors()
    resolved, chosen = resolve_monitor_selector(monitor_target, mons)
    if resolved == "all" or chosen is None:
        chosen = mons[0]

    q = (window_query or "").strip().lower()
    lay = (layout or "maximize").strip().lower()
    tx, ty, tw, th = compute_window_rect_for_monitor(chosen, layout=lay)

    if platform.system() == "Windows":
        try:
            import ctypes
            import pygetwindow as gw  # type: ignore

            user32 = ctypes.windll.user32
            all_wins = [w for w in gw.getAllWindows() if str(getattr(w, "title", "") or "").strip()]
            matches = [w for w in all_wins if q and q in str(w.title).lower()] if q else []
            if not matches:
                active = gw.getActiveWindow()
                if active is not None:
                    matches = [active]
            if not matches:
                return {"ok": False, "message": f"Kein passendes Fenster für '{window_query}' gefunden.", "monitor": chosen}

            win = matches[0]
            hwnd = int(getattr(win, "_hWnd", 0) or 0)
            if hwnd:
                user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                user32.SetWindowPos(hwnd, 0, int(tx), int(ty), int(tw), int(th), 0x0040)
                if lay in ("max", "maximize", "vollbild", "maximieren"):
                    user32.ShowWindow(hwnd, 3)  # SW_MAXIMIZE
                try:
                    user32.SetForegroundWindow(hwnd)
                except Exception:
                    pass
            else:
                if getattr(win, "isMinimized", False):
                    win.restore()
                win.moveTo(tx, ty)
                win.resizeTo(tw, th)
                if lay in ("max", "maximize", "vollbild", "maximieren"):
                    win.maximize()

            return {
                "ok": True,
                "window_title": str(win.title),
                "monitor": chosen,
                "layout": lay,
                "message": f"Fenster '{str(win.title)[:45]}' wurde auf Monitor {chosen['index']} ({chosen['pos']}) verschoben.",
            }
        except Exception as exc:
            return {"ok": False, "message": f"Fenster konnte nicht verschoben werden: {exc}", "monitor": chosen}

    # Linux / Raspberry Pi 5 Fallback
    if shutil.which("wmctrl"):
        wins = list_open_windows()
        matches = [w for w in wins if q and q in w["title"].lower()] if q else wins[:1]
        if matches:
            wid = matches[0]["id"]
            subprocess.run(["wmctrl", "-ir", wid, "-b", "remove,maximized_vert,maximized_horz"], check=False)
            subprocess.run(["wmctrl", "-ir", wid, "-e", f"0,{tx},{ty},{tw},{th}"], check=False)
            if lay in ("max", "maximize", "vollbild", "maximieren"):
                subprocess.run(["wmctrl", "-ir", wid, "-b", "add,maximized_vert,maximized_horz"], check=False)
            return {
                "ok": True,
                "window_title": matches[0]["title"],
                "monitor": chosen,
                "layout": lay,
                "message": f"Fenster '{matches[0]['title'][:45]}' auf Monitor {chosen['index']} ({chosen['pos']}) platziert.",
            }

    return {"ok": False, "message": "Fenster-Verschiebung auf dieser Plattform nicht unterstützt.", "monitor": chosen}


_WIN_TEL_CACHE: dict[str, Any] = {"ts": 0.0, "data": None}


def _get_cursor_xy() -> tuple[int, int]:
    if platform.system() == "Windows":
        try:
            import ctypes

            class _PT(ctypes.Structure):
                _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

            pt = _PT()
            if ctypes.windll.user32.GetCursorPos(ctypes.byref(pt)):
                return int(pt.x), int(pt.y)
        except Exception:
            pass
    try:
        import pyautogui  # type: ignore

        pos = pyautogui.position()
        return int(pos[0]), int(pos[1])
    except Exception:
        return 0, 0


def get_system_processes(limit: int = 10) -> list[dict[str, Any]]:
    """Liest die ressourcenstärksten laufenden Prozesse via psutil aus (gecacht/schnell)."""
    procs: list[dict[str, Any]] = []
    try:
        import psutil  # type: ignore

        rows: list[dict[str, Any]] = []
        for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_info"]):
            try:
                info = p.info
                mem_mb = round((info.get("memory_info").rss if info.get("memory_info") else 0) / (1024 * 1024), 1)
                rows.append(
                    {
                        "pid": int(info.get("pid", 0)),
                        "name": str(info.get("name") or "")[:36],
                        "cpu_pct": round(float(info.get("cpu_percent") or 0.0), 1),
                        "ram_mb": mem_mb,
                    }
                )
            except Exception:
                continue
        rows.sort(key=lambda r: (r["cpu_pct"], r["ram_mb"]), reverse=True)
        procs = rows[: max(1, int(limit))]
    except Exception:
        pass
    return procs


def get_monitor_window_telemetry(force: bool = False) -> dict[str, Any]:
    """
    Liefert Live-Mauskoordinaten, zugeordnete aktive Fenster pro Monitor (1/2/3)
    und Top-Prozesse (gecacht für < 1% CPU-Overhead).
    """
    now = time.monotonic()
    if not force and _WIN_TEL_CACHE["data"] is not None and (now - float(_WIN_TEL_CACHE["ts"])) < 0.45:
        return _WIN_TEL_CACHE["data"]

    mons = get_connected_monitors()
    mx, my = _get_cursor_xy()
    mouse_mon = mons[0]["index"] if mons else 1
    for m in mons:
        if m["x"] <= mx < (m["x"] + m["w"]) and m["y"] <= my < (m["y"] + m["h"]):
            mouse_mon = m["index"]
            break

    wins_by_mon: dict[str, list[str]] = {str(m["index"]): [] for m in mons}
    active_by_mon: dict[str, str] = {str(m["index"]): "Desktop" for m in mons}
    wins = list_open_windows()
    for w in wins:
        cx = int(w["left"]) + int(w["width"]) // 2
        cy = int(w["top"]) + int(w["height"]) // 2
        assigned = str(mons[0]["index"]) if mons else "1"
        for m in mons:
            if m["x"] <= cx < (m["x"] + m["w"]) and m["y"] <= cy < (m["y"] + m["h"]):
                assigned = str(m["index"])
                break
        title = str(w["title"])[:52]
        if title not in wins_by_mon[assigned]:
            wins_by_mon[assigned].append(title)
            if active_by_mon[assigned] == "Desktop":
                active_by_mon[assigned] = title

    payload = {
        "mouse": {"x": mx, "y": my, "monitor": mouse_mon},
        "monitors": mons,
        "active_by_monitor": active_by_mon,
        "windows_by_monitor": wins_by_mon,
        "window_count": len(wins),
    }
    _WIN_TEL_CACHE["ts"] = now
    _WIN_TEL_CACHE["data"] = payload
    return payload


def highlight_monitor_region(
    monitor: Any = 1,
    x_pct: float = 0.0,
    y_pct: float = 0.0,
    w_pct: float = 1.0,
    h_pct: float = 1.0,
    label: str = "JARVIS SCAN",
    color: str = "#f59e0b",
    duration: float = 2.4,
) -> dict[str, Any]:
    """
    Hebt einen Monitor oder einen gezielten Prozent-Ausschnitt (0..1) per Overlay-Rahmen
    hervor und feuert ein synchronisiertes Scan-Event an alle HUD-Fenster.
    """
    mons = get_connected_monitors()
    resolved, chosen = resolve_monitor_selector(monitor, mons)
    target_mons = mons if resolved == "all" else ([chosen] if chosen else mons[:1])

    x0 = max(0.0, min(0.95, float(x_pct)))
    y0 = max(0.0, min(0.95, float(y_pct)))
    wp = max(0.05, min(1.0 - x0, float(w_pct)))
    hp = max(0.05, min(1.0 - y0, float(h_pct)))

    boxes: list[dict[str, Any]] = []
    for m in target_mons:
        bx = int(m["x"] + m["w"] * x0)
        by = int(m["y"] + m["h"] * y0)
        bw = max(40, int(m["w"] * wp))
        bh = max(40, int(m["h"] * hp))
        boxes.append({"monitor": m["index"], "x": bx, "y": by, "w": bw, "h": bh})
        try:
            from ghost_cursor import GHOST_OVERLAY

            GHOST_OVERLAY.add_highlight_box(bx, by, bw, bh, label=f"{label} · M{m['index']}", color=color, duration=duration)
        except Exception:
            pass

    try:
        from gui import get_active_gui

        gui_inst = get_active_gui()
        if gui_inst is not None:
            gui_inst.emit_brain_activity(state="screen_scan", intensity=0.95, active_monitor=resolved, detail=label[:64])
    except Exception:
        pass
    try:
        from macro_engine import EVENT_TICKER

        EVENT_TICKER.emit("highlight", "monitor_overlay", f"{label} -> Monitor {resolved}", monitor=resolved, data={"boxes": boxes})
    except Exception:
        pass

    return {"ok": True, "target": resolved, "boxes": boxes, "message": f"Region auf Monitor {resolved} hervorgehoben ({label})."}


def get_cursor_monitor(
    monitors: Optional[list[dict[str, Any]]] = None,
) -> tuple[int, dict[str, Any], tuple[int, int]]:
    """Ordnet die aktuelle Mausposition exakt Monitor 1, 2 oder 3 anhand der mss-Koordinaten zu."""
    mons = monitors if monitors is not None else get_connected_monitors()
    mx, my = _get_cursor_xy()
    for m in mons:
        if m["x"] <= mx < (m["x"] + m["w"]) and m["y"] <= my < (m["y"] + m["h"]):
            return int(m["index"]), m, (mx, my)
    fallback = mons[0] if mons else {"index": 1, "pos": "mitte", "x": 0, "y": 0, "w": 1920, "h": 1080}
    return int(fallback["index"]), fallback, (mx, my)


LAST_LAUNCHED_WINDOW: dict[str, Any] = {"title": "", "monitor_index": 1, "app_query": ""}


def _find_window_monitor(win: dict[str, Any], mons: list[dict[str, Any]]) -> dict[str, Any]:
    cx = int(win.get("left", 0)) + max(1, int(win.get("width", 100))) // 2
    cy = int(win.get("top", 0)) + max(1, int(win.get("height", 100))) // 2
    for m in mons:
        if m["x"] <= cx < (m["x"] + m["w"]) and m["y"] <= cy < (m["y"] + m["h"]):
            return m
    return mons[0] if mons else {"index": 1, "pos": "mitte"}


def snapshot_open_windows() -> dict[str, Any]:
    """Erstellt einen Snapshot aller offenen Fenstertitel & laufenden Prozessnamen vor App-Start."""
    wins = list_open_windows()
    titles = {str(w.get("title", "")).strip().lower() for w in wins if w.get("title")}
    procs: set[str] = set()
    try:
        import psutil  # type: ignore

        for p in psutil.process_iter(["name"]):
            nm = str((p.info or {}).get("name") or "").strip().lower()
            if nm:
                procs.add(nm)
    except Exception:
        pass
    return {"titles": titles, "procs": procs, "windows": wins}


def _match_app_window(app_query: str, wins: list[dict[str, Any]], before_titles: set[str]) -> Optional[dict[str, Any]]:
    q = (app_query or "").strip().lower()
    tokens = [t for t in re.split(r"[\s\-_.]+", q) if len(t) >= 2]
    # Alias-Keywords für typische Windows-Apps
    alias_map = {
        "rechner": ["rechner", "calculator", "calc"],
        "editor": ["editor", "notepad", "unbenannt"],
        "browser": ["chrome", "edge", "vivaldi", "firefox", "brave", "opera"],
        "vscode": ["visual studio code", "code"],
        "terminal": ["powershell", "eingabeaufforderung", "terminal", "cmd"],
    }
    search_terms = list(dict.fromkeys([q, *tokens, *(alias_map.get(q, []))]))

    # 1. Bevorzuge komplett neu erschienene Fenster, die zur Query passen (oder einzig neues Hauptfenster)
    new_wins = [w for w in wins if str(w.get("title", "")).strip().lower() not in before_titles]
    for w in new_wins:
        tl = str(w.get("title", "")).lower()
        if any(term and term in tl for term in search_terms):
            return w
    # 2. Bereits offenes/in den Vordergrund geholtes passendes Fenster
    for w in wins:
        tl = str(w.get("title", "")).lower()
        if any(term and term in tl for term in search_terms):
            return w
    # 3. Irgendein brandneues Anwendungsfenster (z. B. lokalisierter Fenstertitel)
    if new_wins:
        return new_wins[0]
    return None


def verify_launched_app_across_monitors(
    app_query: str,
    before_snapshot: Optional[dict[str, Any]] = None,
    cursor_timeout: float = 1.5,
    all_timeout: float = 1.4,
) -> dict[str, Any]:
    """
    Zweistufige App-Start-Prüfkette über alle 3 Monitore:
    1. Schnell-Check (bis 1,5s) auf dem aktiven Maus-Monitor & Prozessliste.
    2. Automatischer 3-Monitor-Fallback auf Monitor 1, 2 und 3 (kein zweiter Start bei Fund!).
    """
    mons = get_connected_monitors()
    cursor_idx, cursor_mon, _ = get_cursor_monitor(mons)
    before_titles: set[str] = set((before_snapshot or {}).get("titles") or ())
    before_procs: set[str] = set((before_snapshot or {}).get("procs") or ())
    q_low = (app_query or "").strip().lower()

    # Stufe 1: Schnell-Check auf Maus-Monitor + Prozessliste
    deadline_cursor = time.monotonic() + max(0.2, float(cursor_timeout))
    process_seen = False
    while time.monotonic() < deadline_cursor:
        wins = list_open_windows()
        cand = _match_app_window(app_query, wins, before_titles)
        if cand is not None:
            found_mon = _find_window_monitor(cand, mons)
            if int(found_mon["index"]) == cursor_idx:
                LAST_LAUNCHED_WINDOW.update({"title": str(cand["title"]), "monitor_index": cursor_idx, "app_query": app_query})
                msg = f"App '{cand['title'][:42]}' erfolgreich auf aktuellem Monitor {cursor_idx} ({cursor_mon['pos']}) erkannt."
                _emit_launch_ticker(msg, cursor_idx)
                return {
                    "found": True,
                    "stage": "cursor_monitor",
                    "monitor_index": cursor_idx,
                    "cursor_monitor": cursor_idx,
                    "on_cursor_monitor": True,
                    "window_title": str(cand["title"]),
                    "message": msg,
                }
        # Prüfe kurz Prozessliste (psutil) für Hintergrund-Startsignal
        try:
            import psutil  # type: ignore

            for p in psutil.process_iter(["name"]):
                nm = str((p.info or {}).get("name") or "").lower()
                if nm and nm not in before_procs and q_low and q_low[:4] in nm:
                    process_seen = True
                    break
        except Exception:
            pass
        time.sleep(0.25)

    # Stufe 2: Automatischer 3-Monitor-Fallback (falls nicht auf Maus-Monitor erschienen)
    deadline_all = time.monotonic() + max(0.2, float(all_timeout))
    while time.monotonic() < deadline_all:
        wins = list_open_windows()
        cand = _match_app_window(app_query, wins, before_titles)
        if cand is not None:
            found_mon = _find_window_monitor(cand, mons)
            f_idx = int(found_mon["index"])
            LAST_LAUNCHED_WINDOW.update({"title": str(cand["title"]), "monitor_index": f_idx, "app_query": app_query})
            on_cur = f_idx == cursor_idx
            hint = "" if on_cur else f" Mit 'Hol das Fenster her' ziehe ich es direkt auf Monitor {cursor_idx}."
            msg = f"App '{cand['title'][:42]}' erfolgreich auf Monitor {f_idx} ({found_mon['pos']}) erkannt.{hint}"
            _emit_launch_ticker(msg, f_idx)
            return {
                "found": True,
                "stage": "all_monitors_fallback",
                "monitor_index": f_idx,
                "cursor_monitor": cursor_idx,
                "on_cursor_monitor": on_cur,
                "window_title": str(cand["title"]),
                "message": msg,
            }
        time.sleep(0.25)

    if process_seen:
        msg = f"Prozess für '{app_query}' gestartet (Initialisierung läuft)."
        _emit_launch_ticker(msg, cursor_idx)
        return {
            "found": True,
            "stage": "process_list",
            "monitor_index": cursor_idx,
            "cursor_monitor": cursor_idx,
            "on_cursor_monitor": True,
            "window_title": app_query,
            "message": msg,
        }

    return {
        "found": False,
        "stage": "none",
        "monitor_index": None,
        "cursor_monitor": cursor_idx,
        "on_cursor_monitor": False,
        "window_title": "",
        "message": f"Kein neues Fenster für '{app_query}' auf den Monitoren 1-{len(mons)} erkannt.",
    }


def _emit_launch_ticker(summary: str, monitor_idx: int) -> None:
    try:
        from macro_engine import EVENT_TICKER

        EVENT_TICKER.emit("window", "app_verify", summary, monitor=monitor_idx)
    except Exception:
        pass


def bring_last_window_to_cursor_monitor(window_query: str = "") -> dict[str, Any]:
    """Holt das zuletzt gestartete oder benannte Fenster auf den aktuellen Maus-Monitor."""
    mons = get_connected_monitors()
    cursor_idx, cursor_mon, _ = get_cursor_monitor(mons)
    target_title = (window_query or "").strip() or str(LAST_LAUNCHED_WINDOW.get("title") or "") or str(LAST_LAUNCHED_WINDOW.get("app_query") or "")
    res = move_window_to_monitor(window_query=target_title, monitor_target=cursor_idx, layout="maximize")
    if res.get("ok"):
        LAST_LAUNCHED_WINDOW["monitor_index"] = cursor_idx
        msg = f"Ich habe '{res.get('window_title', target_title)[:40]}' auf deinen aktuellen Bildschirm (Monitor {cursor_idx}, {cursor_mon['pos']}) geholt."
        _emit_launch_ticker(msg, cursor_idx)
        return {**res, "message": msg}
    return res


class MultiMonitorLiveStreamer:
    """
    Kontinuierlicher asynchroner Low-Latency Live-Capture-Worker für alle 3 Monitore.
    Aktiver Maus-Monitor läuft mit 15+ FPS; nicht fokussierte Monitore laufen ressourcenschonend mit ~5 FPS.
    """

    def __init__(
        self,
        fps: float = 16.0,
        background_fps: float = 5.0,
        preview_max_width: int = 640,
        jpeg_quality: int = 58,
    ) -> None:
        self._fps = max(1.0, min(20.0, float(fps)))
        self._bg_fps = max(1.0, min(10.0, float(background_fps)))
        self._preview_max_width = max(320, int(preview_max_width))
        self._jpeg_quality = max(40, min(90, int(jpeg_quality)))
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._frames: dict[int, bytes] = {}
        self._sizes: dict[int, tuple[int, int]] = {}
        self._mon_seqs: dict[int, int] = {}
        self._cursor_monitor: int = 1
        self._seq = 0
        self._last_viewer_touch = 0.0
        self._last_error = ""
        self._actual_fps = 0.0

    def set_fps(self, fps: float) -> float:
        self._fps = max(1.0, min(20.0, float(fps)))
        return self._fps

    def get_fps(self) -> float:
        return self._fps

    def get_cursor_monitor_index(self) -> int:
        return self._cursor_monitor

    def touch_viewer(self) -> None:
        self._last_viewer_touch = time.monotonic()
        self.ensure_started()

    def ensure_started(self) -> None:
        from beta_guard import BETA_GUARD

        if BETA_GUARD.is_expired():
            self.stop()
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._capture_loop, name="MultiMonitorLiveCapture", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    def get_cached_frame(self, monitor_index: int) -> Optional[bytes]:
        self.touch_viewer()
        with self._lock:
            return self._frames.get(int(monitor_index))

    def get_latest_bundle(self) -> tuple[int, dict[int, bytes], dict[int, int], int, float]:
        self.touch_viewer()
        with self._lock:
            return (
                self._seq,
                dict(self._frames),
                dict(self._mon_seqs),
                self._cursor_monitor,
                self._actual_fps,
            )

    def _capture_loop(self) -> None:
        try:
            import mss  # type: ignore
        except Exception as exc:
            self._last_error = str(exc)
            return

        sct = None
        last_active_t = time.monotonic()
        mons_refresh_t = 0.0
        last_bg_ts: dict[int, float] = {}
        mons: list[dict[str, Any]] = []

        while not self._stop_event.is_set():
            now = time.monotonic()
            active_viewer = (now - self._last_viewer_touch) < 4.0
            if not active_viewer:
                self._stop_event.wait(0.35)
                continue

            interval = 1.0 / self._fps
            bg_interval = 1.0 / self._bg_fps
            t_start = time.monotonic()
            if (now - mons_refresh_t) >= 5.0 or not mons:
                mons = get_connected_monitors()
                mons_refresh_t = now

            cursor_idx, _, _ = get_cursor_monitor(mons)

            try:
                if sct is None:
                    sct = mss.mss()
                new_frames: dict[int, bytes] = {}
                new_sizes: dict[int, tuple[int, int]] = {}
                cursor_captured = False

                for m in mons[:3]:
                    idx = int(m["index"])
                    is_cursor = idx == cursor_idx
                    if not is_cursor and (now - last_bg_ts.get(idx, 0.0)) < bg_interval and idx in self._frames:
                        continue

                    img = _grab_single_mss_image(sct, m)
                    if self._preview_max_width > 0 and img.width > self._preview_max_width:
                        ratio = self._preview_max_width / float(img.width)
                        img = img.resize(
                            (self._preview_max_width, max(1, int(round(img.height * ratio)))),
                            Image.Resampling.BILINEAR,
                        )
                    buf = io.BytesIO()
                    img.save(buf, format="JPEG", quality=self._jpeg_quality, optimize=False)
                    new_frames[idx] = buf.getvalue()
                    new_sizes[idx] = img.size
                    if is_cursor:
                        cursor_captured = True
                    else:
                        last_bg_ts[idx] = now

                if new_frames:
                    with self._lock:
                        for k_idx, frame_b in new_frames.items():
                            self._frames[k_idx] = frame_b
                            self._sizes[k_idx] = new_sizes[k_idx]
                            self._mon_seqs[k_idx] = self._mon_seqs.get(k_idx, 0) + 1
                        self._cursor_monitor = cursor_idx
                        self._seq += 1
                        if cursor_captured:
                            dt_loop = max(0.001, t_start - last_active_t)
                            self._actual_fps = round(1.0 / dt_loop, 1)
                            last_active_t = t_start
                        self._last_error = ""
            except Exception as exc:
                self._last_error = str(exc)
                if sct is not None:
                    try:
                        sct.close()
                    except Exception:
                        pass
                    sct = None
                self._stop_event.wait(0.8)
                continue

            elapsed = time.monotonic() - t_start
            sleep_for = max(0.004, interval - elapsed)
            self._stop_event.wait(sleep_for)

        if sct is not None:
            try:
                sct.close()
            except Exception:
                pass


MULTI_MONITOR_STREAMER = MultiMonitorLiveStreamer(fps=16.0, background_fps=5.0, preview_max_width=640, jpeg_quality=58)

