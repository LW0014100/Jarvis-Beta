"""Plattformunabhängige Werkzeuge für GUI-Steuerung, E-Mail, System und Finanz-Schutz."""

from __future__ import annotations

import ctypes
from datetime import datetime
import email
from email.header import decode_header
from email.message import EmailMessage
import imaplib
import io
import json
import os
from pathlib import Path
import platform
import re
import shutil
import smtplib
import socket
import subprocess
import tempfile
import threading
import time
from typing import Any, Callable, Optional
import urllib.parse
import urllib.request
import webbrowser

import mss
from PIL import Image, ImageDraw, ImageFont
import pyautogui

from config import get_archive_subdir, get_is_online, resolve_archive_root
from ghost_cursor import (
    GHOST_OVERLAY,
    GhostCursorInterrupted,
    check_user_interrupt,
    find_windows_start_or_search_coords,
    get_cursor_pos,
    ghost_click,
    ghost_drag,
    ghost_swipe_scroll,
    smooth_move_cursor,
)
from hardware_pi5 import (
    find_rp1_gpio_chip,
    get_os_codename,
    get_pi5_fan_rpm,
    get_pi5_throttle_warning,
    get_session_type,
    is_raspberry_pi_5,
)
from vision import draw_precision_coordinate_grid, locate_ui_element_visually


pyautogui.FAILSAFE = False
pyautogui.PAUSE = 0.02

SAFETY_STOP_MESSAGE = (
    "Chef, alles ist vorbereitet. Bitte prüfen Sie die Daten und klicken Sie selbst mit der Maus auf Kaufen."
)

FORBIDDEN_FINANCIAL_PATTERNS = re.compile(
    r"("
    r"jetzt\s+kaufen|zahlungspflichtig|kauf\s+bestätigen|order\s+absenden|"
    r"order\s+bestätigen|bestellung\s+absenden|jetzt\s+bestellen|"
    r"jetzt\s+bezahlen|zahlung\s+abschließen|transaktion\s+bestätigen|"
    r"überweisung\s+ausführen|\büberweisen\b|\büberweisung\b|geld\s+senden|"
    r"zahlung\s+freigeben|zur\s+kasse|trade\s+bestätigen|"
    r"buy\s+now|place\s+order|confirm\s+order|confirm\s+purchase|"
    r"submit\s+order|complete\s+purchase|pay\s+now|execute\s+trade|money\s+transfer|"
    r"\bkaufen\b|\bbestellen\b|\bbezahlen\b|\bcheckout\b|\bbestätigen\b|\bconfirm\b"
    r")",
    flags=re.IGNORECASE,
)


def is_financial_action_forbidden(
    target_label: str,
    reason: str,
    status: str,
    screen_contains_purchase_button: bool = False,
) -> bool:
    """Hardcoded Safety Guard: Blockiert jeden finalen Kauf-, Order-, Bezahl- oder Bestätigungs-Klick."""
    if status.strip().lower() == "safety_stop" or screen_contains_purchase_button:
        return True
    combined = f"{target_label} {reason}".strip()
    return bool(combined and FORBIDDEN_FINANCIAL_PATTERNS.search(combined))


def _overlay_coordinate_grid(img: Image.Image, step: int = 100) -> Image.Image:
    """Zeichnet ein feines Koordinaten-Raster (100px) für präzise Vision-Mausklicks ein."""
    grid_img = img.convert("RGBA")
    overlay = Image.new("RGBA", grid_img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    font = ImageFont.load_default()
    width, height = grid_img.size

    line_col = (255, 140, 40, 95)
    label_bg = (8, 12, 20, 175)
    label_fg = (255, 220, 120, 240)

    for x in range(step, width, step):
        draw.line([(x, 0), (x, height)], fill=line_col, width=1)
        tag = str(x)
        draw.rectangle([x + 2, 2, x + 32, 16], fill=label_bg)
        draw.text((x + 4, 3), tag, fill=label_fg, font=font)

    for y in range(step, height, step):
        draw.line([(0, y), (width, y)], fill=line_col, width=1)
        tag = str(y)
        draw.rectangle([2, y + 2, 34, y + 16], fill=label_bg)
        draw.text((4, y + 3), tag, fill=label_fg, font=font)

    combined = Image.alpha_composite(grid_img, overlay).convert("RGB")
    return combined


def capture_screen_jpeg(quality: int = 85, with_grid: bool = True) -> tuple[bytes, int, int]:
    """Erstellt einen hochauflösenden Screenshot (blendet Ghost-Overlay kurzzeitig aus, X11 via mss, Wayland via grim)."""
    logical_w, logical_h = pyautogui.size()
    img: Optional[Image.Image] = None

    GHOST_OVERLAY.set_capture_stealth(True)
    try:
        try:
            with mss.mss() as sct:
                monitor = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
                raw = sct.grab(monitor)
                img = Image.frombytes("RGB", raw.size, raw.rgb)
        except Exception:
            if platform.system() == "Linux" and shutil.which("grim"):
                proc = subprocess.run(
                    ["grim", "-t", "jpeg", "-q", str(quality), "-"],
                    capture_output=True,
                    timeout=4.0,
                    check=False,
                )
                if proc.returncode == 0 and proc.stdout:
                    img = Image.open(io.BytesIO(proc.stdout)).convert("RGB")
                    if logical_w <= 0 or logical_h <= 0:
                        logical_w, logical_h = img.size
            if img is None:
                raise
    finally:
        GHOST_OVERLAY.set_capture_stealth(False)

    if logical_w > 0 and logical_h > 0 and img.size != (logical_w, logical_h):
        img = img.resize((logical_w, logical_h), Image.Resampling.LANCZOS)

    if with_grid:
        img = draw_precision_coordinate_grid(img, step=100)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue(), logical_w, logical_h


def _safe_int_coord(val: Any, default: int = 0) -> int:
    """Konvertiert ints, Floats oder Strings ('350', '350.5', '350px') kugelsicher in int."""
    if val is None or isinstance(val, bool):
        return default
    if isinstance(val, (int, float)):
        return int(round(float(val)))
    m = re.search(r"-?\d+(?:[.,]\d+)?", str(val).strip())
    if not m:
        return default
    try:
        return int(round(float(m.group(0).replace(",", "."))))
    except Exception:
        return default


def execute_gui_action(
    action: str,
    x: Any = 0,
    y: Any = 0,
    end_x: Any = 0,
    end_y: Any = 0,
    text: str = "",
    key: str = "",
    keys: Optional[list[str]] = None,
    amount: Any = 0,
    target_label: str = "",
    direction: str = "",
    press_enter: bool = False,
    ask_vision_fn: Optional[Callable[[str, bytes, str], str]] = None,
) -> str:
    """
    Führt eine vollständig sichtbare Maus-, Drag&Drop-, Rechtsklick-, Scroll- oder Tastaturaktion
    über `agent_hand` inkl. `SmartMouseGuard` und Ghost-Cursor-Overlay aus.
    """
    from agent_hand import (
        SMART_MOUSE_GUARD,
        click as hand_click,
        click_and_type as hand_click_and_type,
        double_click as hand_double_click,
        drag_and_drop as hand_drag_and_drop,
        ghost_typewrite,
        hotkey as hand_hotkey,
        right_click as hand_right_click,
        scroll as hand_scroll,
    )

    SMART_MOUSE_GUARD.check_or_raise()
    logical_w, logical_h = pyautogui.size()
    act = action.strip().lower()
    lbl = (target_label or text or key or "").strip()
    x_i = _safe_int_coord(x, 0)
    y_i = _safe_int_coord(y, 0)
    end_x_i = _safe_int_coord(end_x, 0)
    end_y_i = _safe_int_coord(end_y, 0)
    amount_i = _safe_int_coord(amount, 0)

    if act in ("click", "double_click", "right_click", "click_and_type", "move") and (x_i <= 0 or y_i <= 0) and lbl:
        GHOST_OVERLAY.set_status(f"Suche: {lbl[:32]}...", mode="search")
        located = locate_ui_element_visually(lbl, ask_vision_fn=ask_vision_fn, taskbar_only=False)
        if located and located.get("found"):
            x_i = _safe_int_coord(located.get("x"), x_i)
            y_i = _safe_int_coord(located.get("y"), y_i)

    cx = max(3, min(x_i, logical_w - 4)) if x_i > 0 else get_cursor_pos()[0]
    cy = max(3, min(y_i, logical_h - 4)) if y_i > 0 else get_cursor_pos()[1]
    ex = max(3, min(end_x_i, logical_w - 4)) if end_x_i > 0 else cx
    ey = max(3, min(end_y_i, logical_h - 4)) if end_y_i > 0 else cy

    GHOST_OVERLAY.start(f"JARVIS // {lbl or act.upper()}")
    check_user_interrupt()

    if act == "click":
        return hand_click(cx, cy, button="left", clicks=1, duration=0.45, label=f"Klicke: {lbl}" if lbl else "")

    if act == "double_click":
        return hand_double_click(cx, cy, duration=0.42, label=f"Doppelklick: {lbl}" if lbl else "", press_enter=True)

    if act == "right_click":
        return hand_right_click(cx, cy, duration=0.4, label=f"Rechtsklick: {lbl}" if lbl else "")

    if act in ("drag", "drag_and_drop"):
        return hand_drag_and_drop(cx, cy, ex, ey, duration=0.65, label=f"Ziehe: {lbl}" if lbl else "")

    if act == "click_and_type":
        return hand_click_and_type(cx, cy, text, press_enter=bool(press_enter), label=lbl)

    if act == "move":
        tag = f"Fahre zu: {lbl}" if lbl else f"Fahre zu ({cx}, {cy})"
        smooth_move_cursor(cx, cy, duration=0.55, label=tag, mode="move")
        return f"Maus flüssig bewegt nach ({cx}, {cy}) [{tag}]"

    if act in ("scroll", "swipe", "swipe_down", "swipe_up", "swipe_left", "swipe_right"):
        eff_dir = direction.strip().lower()
        if not eff_dir:
            if "up" in act:
                eff_dir = "up"
            elif "left" in act:
                eff_dir = "left"
            elif "right" in act:
                eff_dir = "right"
            elif "down" in act:
                eff_dir = "down"
            else:
                eff_dir = "down" if (amount_i if amount_i != 0 else -5) < 0 else "up"
        if eff_dir in ("left", "right"):
            return ghost_swipe_scroll(
                amount=int(amount_i or -420),
                x=cx if x_i > 0 else None,
                y=cy if y_i > 0 else None,
                direction=eff_dir,
                label=f"Wische ({eff_dir}): {lbl}" if lbl else f"Wische {eff_dir}...",
            )
        amt = int(amount_i) if amount_i != 0 else (-5 if eff_dir == "down" else 5)
        return hand_scroll(amt, x=cx if x_i > 0 else None, y=cy if y_i > 0 else None, label=lbl)

    if act == "open_app":
        return open_app(text or key or target_label, visible_cursor=True, ask_vision_fn=ask_vision_fn)

    if act == "open_url":
        GHOST_OVERLAY.set_status(f"Öffne URL: {(text or key)[:30]}", mode="click")
        return open_url(text or key)

    if act == "type":
        return ghost_typewrite(text, interval=0.03, press_enter=bool(press_enter), label=lbl)

    if act == "press":
        if key:
            return hand_hotkey(key, label=f"TASTE: {key.upper()}")
        return "Keine Taste angegeben."

    if act == "hotkey":
        combo = [k for k in (keys or []) if k]
        if not combo and key:
            combo = [key]
        if combo:
            return hand_hotkey(*combo)
        return "Keine Tastenkombination angegeben."

    if act == "wait":
        GHOST_OVERLAY.set_status(lbl or "Warte auf Fenster...", mode="search")
        time.sleep(1.0)
        return "Kurz gewartet"

    return f"Unbekannte Aktion ignoriert: {action}"


# ============================================================
# E-Mail-Werkzeuge (IMAP / Entwürfe)
# ============================================================

_KNOWN_EMAIL_SERVERS = {
    "gmail.com": ("imap.gmail.com", "smtp.gmail.com"),
    "googlemail.com": ("imap.gmail.com", "smtp.gmail.com"),
    "gmx.de": ("imap.gmx.net", "mail.gmx.net"),
    "gmx.net": ("imap.gmx.net", "mail.gmx.net"),
    "web.de": ("imap.web.de", "smtp.web.de"),
    "outlook.com": ("outlook.office365.com", "smtp.office365.com"),
    "hotmail.com": ("outlook.office365.com", "smtp.office365.com"),
    "icloud.com": ("imap.mail.me.com", "smtp.mail.me.com"),
}


def _email_config() -> dict[str, Any]:
    address = (os.getenv("EMAIL_USER") or os.getenv("EMAIL_ADDRESS") or "").strip()
    password = (os.getenv("EMAIL_PASS") or os.getenv("EMAIL_PASSWORD") or "").strip()
    imap_server = (os.getenv("IMAP_SERVER") or os.getenv("EMAIL_IMAP_SERVER") or "").strip()
    smtp_server = (os.getenv("SMTP_SERVER") or os.getenv("EMAIL_SMTP_SERVER") or "").strip()
    imap_port = int(os.getenv("EMAIL_IMAP_PORT", "993") or 993)
    smtp_port = int(os.getenv("EMAIL_SMTP_PORT", "465") or 465)

    if address and "@" in address:
        domain = address.split("@", 1)[1].lower()
        default_imap, default_smtp = _KNOWN_EMAIL_SERVERS.get(domain, ("", ""))
        imap_server = imap_server or default_imap
        smtp_server = smtp_server or default_smtp

    return {
        "address": address,
        "password": password,
        "imap_server": imap_server,
        "imap_port": imap_port,
        "smtp_server": smtp_server,
        "smtp_port": smtp_port,
    }


def _decode_mime_text(raw_value: Optional[str]) -> str:
    if not raw_value:
        return ""
    parts = decode_header(raw_value)
    decoded: list[str] = []
    for chunk, charset in parts:
        if isinstance(chunk, bytes):
            decoded.append(chunk.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(chunk)
    return "".join(decoded).strip()


def fetch_unread_emails(limit: int = 5) -> list[dict[str, str]]:
    """Ruft die neuesten ungelesenen E-Mails via IMAP ab."""
    cfg = _email_config()
    if not cfg["address"] or not cfg["password"] or not cfg["imap_server"]:
        raise RuntimeError(
            "E-Mail-Zugangsdaten sind nicht vollständig in .env konfiguriert."
        )

    results: list[dict[str, str]] = []
    with imaplib.IMAP4_SSL(cfg["imap_server"], cfg["imap_port"]) as mail:
        mail.login(cfg["address"], cfg["password"])
        mail.select("INBOX")
        status, data = mail.search(None, "UNSEEN")
        if status != "OK" or not data or not data[0]:
            return results

        ids = data[0].split()[-limit:]
        for msg_id in reversed(ids):
            res_status, msg_data = mail.fetch(msg_id, "(BODY.PEEK[])")
            if res_status != "OK" or not msg_data:
                continue
            raw_bytes = msg_data[0][1]
            if not isinstance(raw_bytes, bytes):
                continue

            msg = email.message_from_bytes(raw_bytes)
            sender = _decode_mime_text(msg.get("From"))
            subject = _decode_mime_text(msg.get("Subject")) or "(Kein Betreff)"

            snippet = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain" and not part.get("Content-Disposition"):
                        payload = part.get_payload(decode=True)
                        if isinstance(payload, bytes):
                            charset = part.get_content_charset() or "utf-8"
                            snippet = payload.decode(charset, errors="replace")
                            break
            else:
                payload = msg.get_payload(decode=True)
                if isinstance(payload, bytes):
                    charset = msg.get_content_charset() or "utf-8"
                    snippet = payload.decode(charset, errors="replace")

            snippet = re.sub(r"\s+", " ", snippet).strip()[:220]
            results.append({"from": sender, "subject": subject, "snippet": snippet})

    return results


def save_email_draft(to_addr: str, subject: str, body: str) -> str:
    """Speichert einen E-Mail-Entwurf im IMAP-Entwürfe-Ordner (oder lokal unter drafts/)."""
    cfg = _email_config()

    msg = EmailMessage()
    msg["From"] = cfg["address"] or "jarvis-entwurf@lokal"
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)

    drafts_dir, _ = get_archive_subdir("drafts")
    safe_name = re.sub(r"[^a-zA-Z0-9_-]+", "_", subject)[:40] or "entwurf"
    local_path = drafts_dir / f"{int(time.time())}_{safe_name}.txt"
    local_path.write_text(f"An: {to_addr}\nBetreff: {subject}\n\n{body}\n", encoding="utf-8")

    if cfg["address"] and cfg["password"] and cfg["imap_server"]:
        try:
            with imaplib.IMAP4_SSL(cfg["imap_server"], cfg["imap_port"]) as mail:
                mail.login(cfg["address"], cfg["password"])
                for folder in ("Drafts", "Entwürfe", "INBOX.Drafts", "[Gmail]/Drafts"):
                    status, _ = mail.append(
                        folder,
                        r"\Draft",
                        imaplib.Time2Internaldate(time.time()),
                        msg.as_bytes(),
                    )
                    if status == "OK":
                        return f"Entwurf im Postfach ({folder}) und lokal unter {local_path} gespeichert."
        except Exception:
            pass

    return f"Entwurf lokal unter {local_path} vorbereitet."


def send_email_smtp(to_addr: str, subject: str, body: str) -> str:
    """Versendet eine zuvor vorbereitete und vom Chef bestätigte E-Mail via SMTP."""
    cfg = _email_config()
    if not cfg["address"] or not cfg["password"] or not cfg["smtp_server"]:
        raise RuntimeError(
            "E-Mail-SMTP-Zugangsdaten sind nicht vollständig in den Einstellungen (.env) hinterlegt."
        )
    if not to_addr or "@" not in to_addr:
        raise ValueError("Keine gültige Empfänger-Adresse angegeben.")

    msg = EmailMessage()
    msg["From"] = cfg["address"]
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)

    port = int(cfg["smtp_port"])
    if port == 465:
        with smtplib.SMTP_SSL(cfg["smtp_server"], port, timeout=12.0) as server:
            server.login(cfg["address"], cfg["password"])
            server.send_message(msg)
    else:
        with smtplib.SMTP(cfg["smtp_server"], port, timeout=12.0) as server:
            server.ehlo()
            server.starttls()
            server.login(cfg["address"], cfg["password"])
            server.send_message(msg)

    return f"E-Mail an {to_addr} mit Betreff '{subject}' erfolgreich versendet."


def send_email_smtp(to_addr: str, subject: str, body: str) -> str:
    """Versendet eine zuvor vorbereitete und vom Chef bestätigte E-Mail via SMTP."""
    cfg = _email_config()
    if not cfg["address"] or not cfg["password"] or not cfg["smtp_server"]:
        raise RuntimeError(
            "E-Mail-SMTP-Zugangsdaten sind nicht vollständig in den Einstellungen (.env) hinterlegt."
        )
    if not to_addr or "@" not in to_addr:
        raise ValueError("Keine gültige Empfänger-Adresse angegeben.")

    msg = EmailMessage()
    msg["From"] = cfg["address"]
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)

    port = int(cfg["smtp_port"])
    if port == 465:
        with smtplib.SMTP_SSL(cfg["smtp_server"], port, timeout=12.0) as server:
            server.login(cfg["address"], cfg["password"])
            server.send_message(msg)
    else:
        with smtplib.SMTP(cfg["smtp_server"], port, timeout=12.0) as server:
            server.ehlo()
            server.starttls()
            server.login(cfg["address"], cfg["password"])
            server.send_message(msg)

    return f"E-Mail an {to_addr} mit Betreff '{subject}' erfolgreich versendet."


# ============================================================
# System- & Datei-Werkzeuge
# ============================================================

_APP_ALIASES_WIN_EXE = {
    "rechner": "calc.exe",
    "taschenrechner": "calc.exe",
    "calculator": "calc.exe",
    "explorer": "explorer.exe",
    "dateimanager": "explorer.exe",
    "taskmanager": "taskmgr.exe",
    "task-manager": "taskmgr.exe",
    "task manager": "taskmgr.exe",
    "notepad": "notepad.exe",
    "editor": "notepad.exe",
    "paint": "mspaint.exe",
    "terminal": "wt.exe",
    "cmd": "cmd.exe",
    "eingabeaufforderung": "cmd.exe",
    "powershell": "powershell.exe",
    "chrome": "chrome.exe",
    "edge": "msedge.exe",
    "firefox": "firefox.exe",
    "vscode": "code",
    "vs code": "code",
    "visual studio code": "code",
    "code": "code",
    "obs": "obs64",
    "vlc": "vlc",
    "steam": "steam",
    "kdenlive": "kdenlive",
    "schnittprogramm": "kdenlive",
}

_APP_URI_WIN = {
    "einstellungen": "ms-settings:",
    "settings": "ms-settings:",
    "spotify": "spotify:",
    "store": "ms-windows-store:",
    "microsoft store": "ms-windows-store:",
    "kamera": "microsoft.windows.camera:",
    "kalender": "outlookcal:",
}

_APP_UWP_WIN = {
    "xbox": r"Microsoft.GamingApp_8wekyb3d8bbwe!Microsoft.Xbox.App",
}

_APP_ALIASES_LINUX = {
    "chromium": "chromium",
    "chrome": "google-chrome",
    "firefox": "firefox",
    "dateimanager": "pcmanfm",
    "explorer": "pcmanfm",
    "editor": "mousepad",
    "rechner": "galculator",
    "taschenrechner": "galculator",
    "terminal": "lxterminal",
    "kdenlive": "kdenlive",
    "schnittprogramm": "kdenlive",
    "vscode": "code",
    "vs code": "code",
    "visual studio code": "code",
    "code": "code",
    "vlc": "vlc",
    "obs": "obs",
    "einstellungen": "rc_gui",
}

_URL_LOOKALIKE_PATTERN = re.compile(
    r"^(?:https?://|www\.|[a-zA-Z0-9-]+\.(?:de|com|org|net|io|ai|tv|ch|at|eu)(?:/.*)?$)",
    flags=re.IGNORECASE,
)


def open_url(url_or_query: str, brain: Any = None) -> str:
    """Öffnet eine saubere Ziel-URL im Standardbrowser (niemals den rohen gesprochenen Satz in Google werfen)."""
    raw = url_or_query.strip()
    if not raw:
        raw = "https://www.google.de"

    if raw.startswith(("http://", "https://")):
        url = raw
    elif _URL_LOOKALIKE_PATTERN.match(raw) and " " not in raw:
        url = f"https://{raw}"
    else:
        try:
            from brain import extract_browser_intent

            intent = extract_browser_intent(raw, brain=brain)
            url = intent.get("target_url") or "https://www.google.de"
        except Exception:
            url = "https://www.google.de"

    system = platform.system()
    if system == "Windows":
        try:
            os.startfile(url)  # type: ignore[attr-defined]
            return f"Browser geöffnet: {url}"
        except Exception:
            pass
    elif system == "Linux":
        try:
            subprocess.Popen(["xdg-open", url], start_new_session=True)
            return f"Browser geöffnet: {url}"
        except Exception:
            pass

    webbrowser.open(url)
    return f"Browser geöffnet: {url}"


def _find_windows_startmenu_shortcut(app_name: str) -> Optional[Path]:
    query = app_name.strip().lower()
    if not query:
        return None
    roots = [
        Path(os.getenv("ProgramData", r"C:\ProgramData")) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
        Path(os.getenv("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
    ]
    best_match: Optional[tuple[int, int, Path]] = None
    for base in roots:
        if not base.exists():
            continue
        for shortcut in base.rglob("*.lnk"):
            stem = shortcut.stem.lower()
            if stem == query:
                score = 0
            elif stem.startswith(query):
                score = 1
            elif query in stem:
                score = 2
            else:
                continue
            candidate = (score, len(stem), shortcut)
            if best_match is None or candidate[:2] < best_match[:2]:
                best_match = candidate
    return best_match[2] if best_match is not None else None


def _find_windows_startapp_id(app_name: str) -> Optional[tuple[str, str]]:
    """Sucht per PowerShell Get-StartApps nach Store-/UWP- und Startmenü-AppIDs."""
    safe_query = re.sub(r"[^a-zA-Z0-9äöüÄÖÜß _.-]", "", app_name).strip()
    if not safe_query:
        return None

    ps_exe = shutil.which("powershell") or shutil.which("pwsh")
    if not ps_exe:
        return None

    script = (
        f"$q = '*{safe_query}*'; "
        "Get-StartApps | Where-Object { $_.Name -like $q } | "
        "Select-Object -First 8 Name, AppID | ConvertTo-Json -Compress"
    )
    try:
        proc = subprocess.run(
            [ps_exe, "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=4.0,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        raw_out = (proc.stdout or "").strip()
        if proc.returncode != 0 or not raw_out:
            return None
        parsed = json.loads(raw_out)
    except Exception:
        return None

    items = parsed if isinstance(parsed, list) else ([parsed] if isinstance(parsed, dict) else [])
    q_low = safe_query.lower()
    ranked: list[tuple[int, int, str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("Name") or "").strip()
        app_id = str(item.get("AppID") or "").strip()
        if not name or not app_id:
            continue
        n_low = name.lower()
        rank = 0 if n_low == q_low else (1 if n_low.startswith(q_low) else 2)
        ranked.append((rank, len(n_low), name, app_id))

    if not ranked:
        return None
    ranked.sort(key=lambda x: (x[0], x[1]))
    return ranked[0][2], ranked[0][3]


def _find_linux_desktop_exec(app_name: str) -> Optional[list[str]]:
    query = app_name.strip().lower()
    if not query:
        return None
    dirs = [
        Path("/usr/share/applications"),
        Path.home() / ".local" / "share" / "applications",
    ]
    for d in dirs:
        if not d.exists():
            continue
        for desktop_file in d.glob("*.desktop"):
            try:
                text = desktop_file.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            name_match = re.search(r"^Name=(.+)$", text, flags=re.MULTILINE)
            exec_match = re.search(r"^Exec=(.+)$", text, flags=re.MULTILINE)
            disp = name_match.group(1).strip().lower() if name_match else ""
            if (query in desktop_file.stem.lower() or query in disp) and exec_match:
                raw_exec = re.sub(r"\s+%[fFuUdDnNickvm]", "", exec_match.group(1)).strip()
                parts = raw_exec.split()
                if parts:
                    return parts
    return None


def _launch_windows_app(raw_name: str) -> str:
    """Startet lokale Windows-/Store-Apps strikt über 3 Stufen (kein Browser-/Google-Fallback)."""
    cleaned = re.sub(
        r"^(?:die|der|das|den|eine?n?)\s+|\s+(?:app|programm|anwendung)$",
        "",
        raw_name.strip(),
        flags=re.IGNORECASE,
    ).strip()
    key = cleaned.lower() or raw_name.strip().lower()

    # Stufe 1: Schnelle App- & URI-Tabelle
    _win_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if key == "browser":
        for candidate in ("msedge.exe", "chrome.exe", "firefox.exe"):
            exe = shutil.which(candidate)
            if exe:
                subprocess.Popen([exe], creationflags=_win_flags)
                return f"Browser gestartet: {candidate}"

    if key == "discord":
        discord_update = Path(os.getenv("LOCALAPPDATA", "")) / "Discord" / "Update.exe"
        if discord_update.is_file():
            subprocess.Popen([str(discord_update), "--processStart", "Discord.exe"], creationflags=_win_flags)
            return "App gestartet: Discord"

    if key in _APP_UWP_WIN:
        app_id = _APP_UWP_WIN[key]
        try:
            subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{app_id}"], creationflags=_win_flags)
            return f"App gestartet: {cleaned or raw_name}"
        except Exception:
            pass

    if key in _APP_URI_WIN:
        uri = _APP_URI_WIN[key]
        try:
            os.startfile(uri)  # type: ignore[attr-defined]
            return f"App gestartet: {cleaned or raw_name}"
        except Exception:
            pass

    exe_hint = _APP_ALIASES_WIN_EXE.get(key)
    if exe_hint:
        exe = shutil.which(exe_hint)
        if exe:
            subprocess.Popen([exe], creationflags=_win_flags)
            return f"Programm gestartet: {exe_hint}"

    # Stufe 2: Dynamische Windows-Startmenü-Suche (Store-/UWP-Apps via Get-StartApps)
    uwp_match = _find_windows_startapp_id(cleaned or raw_name)
    if uwp_match is not None:
        disp_name, app_id = uwp_match
        subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{app_id}"], creationflags=_win_flags)
        return f"App gestartet: {disp_name}"

    # Stufe 3: Startmenü-Verknüpfungen (.lnk) & shutil.which
    for candidate in dict.fromkeys([c for c in (exe_hint, cleaned, raw_name) if c]):
        exe = shutil.which(candidate) or shutil.which(f"{candidate}.exe")
        if exe:
            subprocess.Popen([exe], creationflags=_win_flags)
            return f"Programm gestartet: {Path(exe).name}"

    shortcut = _find_windows_startmenu_shortcut(cleaned) or _find_windows_startmenu_shortcut(raw_name)
    if shortcut is not None:
        os.startfile(str(shortcut))  # type: ignore[attr-defined]
        return f"Programm über Startmenü gestartet: {shortcut.stem}"

    # Stufe 4: Intelligenter Tiefen-App-Scanner (apps_cache.json mit Fuzzy-Matching via difflib)
    try:
        from app_indexer import APP_INDEXER

        indexed = APP_INDEXER.find_app(cleaned or raw_name)
        if indexed is not None:
            APP_INDEXER._launch_target(indexed.get("target", ""), indexed.get("kind", "exe"))
            return f"Programm über Tiefen-App-Scanner gestartet: {indexed.get('name', cleaned or raw_name)}"
    except Exception as idx_exc:
        print(f"[AppIndexer Hinweis] {idx_exc}")

    # Stufe 5: Visueller Windows-Suche-Fallback (Win-Taste -> Name tippen -> 1-FPS Screenshot -> Enter)
    try:
        from agent_hand import VISUAL_HAND_AGENT

        return VISUAL_HAND_AGENT.visual_windows_search_open(cleaned or raw_name)
    except Exception:
        pass

    raise FileNotFoundError(f"Die lokale App '{raw_name}' wurde auf diesem System nicht gefunden.")


def open_app(
    target: str,
    visible_cursor: bool = True,
    keep_overlay_active: bool = False,
    ask_vision_fn: Optional[Callable[[str, bytes, str], str]] = None,
) -> str:
    """
    Startet lokal installierte Programme/Apps – standardmäßig mit sichtbarem Ghost-Cursor:
    Sucht das Icon in der Taskleiste (z. B. Xbox App), gleitet flüssig mit easeInOutQuad dorthin und klickt es an.
    """
    raw = target.strip().strip("\"'")
    if not raw:
        raise ValueError("Bitte nennen Sie den Namen der gewünschten App.")

    cleaned = re.sub(
        r"^(?:die|der|das|den|eine?n?)\s+|\s+(?:app|programm|anwendung)$",
        "",
        raw,
        flags=re.IGNORECASE,
    ).strip() or raw

    system = platform.system()
    if system == "Windows":
        from multi_monitor import snapshot_open_windows, verify_launched_app_across_monitors

        before_snap = snapshot_open_windows()
        if visible_cursor:
            try:
                GHOST_OVERLAY.start(f"Suche: {cleaned}")
                check_user_interrupt()

                # 1. Prüfe per UIAutomation / Vision, ob das App-Symbol direkt in der Taskleiste liegt
                taskbar_hit = locate_ui_element_visually(
                    cleaned,
                    ask_vision_fn=ask_vision_fn,
                    taskbar_only=True,
                )
                if taskbar_hit and taskbar_hit.get("found"):
                    tx = int(taskbar_hit["x"])
                    ty = int(taskbar_hit["y"])
                    disp = str(taskbar_hit.get("name") or cleaned)
                    ghost_click(
                        tx,
                        ty,
                        button="left",
                        clicks=1,
                        duration=0.75,
                        label=f"Klicke Taskleiste: {disp[:28]}",
                    )
                    # Zweistufige 3-Monitor-Verifizierung VOR einem erneuten Start (verhindert doppeltes Starten!)
                    ver = verify_launched_app_across_monitors(cleaned, before_snap, cursor_timeout=1.5, all_timeout=1.2)
                    if ver.get("found"):
                        return str(ver.get("message") or f"App über Taskleiste geöffnet: {disp}")
                    # Erst wenn auf keinem der 3 Monitore erschienen -> gezielter Fallback-Start
                    launch_msg = _launch_windows_app(raw)
                    ver2 = verify_launched_app_across_monitors(cleaned, before_snap, cursor_timeout=1.5, all_timeout=1.0)
                    return str(ver2.get("message") or launch_msg)

                # 2. Falls nicht direkt in der Taskleiste angeheftet: Sichtbar zur Taskleisten-Suche gleiten
                sx, sy, s_name = find_windows_start_or_search_coords()
                smooth_move_cursor(
                    sx,
                    sy,
                    duration=0.7,
                    label=f"Suche '{cleaned}' über {s_name}",
                    mode="search",
                )
                GHOST_OVERLAY.add_ripple(sx, sy, label=f"Starte: {cleaned}")
                time.sleep(0.25)
            except GhostCursorInterrupted:
                GHOST_OVERLAY.stop()
                raise
            except Exception as exc:
                print(f"[Ghost-Cursor App-Start Hinweis] {exc}")
            finally:
                if not keep_overlay_active:
                    threading.Timer(1.2, GHOST_OVERLAY.stop).start()

        launch_msg = _launch_windows_app(raw)
        ver_after = verify_launched_app_across_monitors(cleaned, before_snap, cursor_timeout=1.5, all_timeout=1.2)
        if ver_after.get("found"):
            return str(ver_after.get("message") or launch_msg)
        return launch_msg

    key = raw.lower()
    if key == "browser":
        for candidate in ("chromium", "chromium-browser", "google-chrome", "firefox", "epiphany"):
            exe = shutil.which(candidate)
            if exe:
                subprocess.Popen([exe], start_new_session=True)
                return f"Browser gestartet: {candidate}"

    cmd = _APP_ALIASES_LINUX.get(key, raw)
    exe = shutil.which(cmd.split()[0])
    if not exe and cmd == "chromium":
        exe = shutil.which("chromium-browser")
    elif not exe and cmd == "lxterminal":
        exe = shutil.which("x-terminal-emulator") or shutil.which("xfce4-terminal")
    if exe:
        subprocess.Popen([exe, *cmd.split()[1:]], start_new_session=True)
        return f"Programm gestartet: {cmd}"

    desktop_cmd = _find_linux_desktop_exec(cmd) or _find_linux_desktop_exec(raw)
    if desktop_cmd is not None:
        subprocess.Popen(desktop_cmd, start_new_session=True)
        return f"Programm gestartet: {' '.join(desktop_cmd)}"

    raise FileNotFoundError(f"Die lokale App '{raw}' wurde auf diesem System nicht gefunden.")


def open_application(target: str) -> str:
    """Abwärtskompatibler Alias für open_app."""
    return open_app(target)


def create_project_folder(
    folder_name: str,
    base_dir: Optional[str] = None,
    video_template: bool = False,
) -> str:
    """Erstellt einen neuen Ordner (auf Wunsch mit Unterordnern für Videoschnitt)."""
    safe_name = re.sub(r'[<>:"/\\|?*]+', "_", folder_name).strip() or "Jarvis_Projekt"
    root = Path(base_dir).expanduser() if base_dir else Path.home() / "Desktop"
    target = root / safe_name
    target.mkdir(parents=True, exist_ok=True)

    if video_template:
        for sub in ("01_Rohmaterial", "02_Audio", "03_Grafiken", "04_Export"):
            (target / sub).mkdir(exist_ok=True)
        return f"Schnittprojekt-Ordner mit Unterordnern erstellt unter {target}"

    return f"Ordner erstellt unter {target}"


def move_or_rename_path(src: str, dst: str) -> str:
    """Verschiebt oder benennt eine Datei bzw. einen Ordner um."""
    src_path = Path(src).expanduser()
    dst_path = Path(dst).expanduser()
    if not src_path.exists():
        raise FileNotFoundError(f"Quelle nicht gefunden: {src_path}")
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src_path), str(dst_path))
    return f"Verschoben/Umbenannt: {src_path.name} nach {dst_path}"


def get_cpu_temperature_celsius() -> Optional[float]:
    """Liest die CPU-Temperatur auf Linux/Raspberry Pi aus (oder None auf Windows)."""
    thermal_file = Path("/sys/class/thermal/thermal_zone0/temp")
    if not thermal_file.exists():
        return None
    try:
        return int(thermal_file.read_text(encoding="utf-8").strip()) / 1000.0
    except Exception:
        return None


def get_ram_usage() -> Optional[tuple[float, float, float]]:
    """Gibt (belegt_gb, gesamt_gb, prozent) plattformunabhängig zurück."""
    if platform.system() == "Windows":
        class _MemoryStatusEx(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        mem = _MemoryStatusEx()
        mem.dwLength = ctypes.sizeof(_MemoryStatusEx)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(mem)):  # type: ignore[attr-defined]
            total_gb = mem.ullTotalPhys / (1024**3)
            used_gb = (mem.ullTotalPhys - mem.ullAvailPhys) / (1024**3)
            return used_gb, total_gb, float(mem.dwMemoryLoad)
        return None

    meminfo = Path("/proc/meminfo")
    if meminfo.exists():
        try:
            values: dict[str, int] = {}
            for line in meminfo.read_text(encoding="utf-8").splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    num = v.strip().split()[0]
                    if num.isdigit():
                        values[k] = int(num)
            total_kb = values.get("MemTotal", 0)
            avail_kb = values.get("MemAvailable", values.get("MemFree", 0))
            if total_kb > 0:
                used_kb = max(0, total_kb - avail_kb)
                return used_kb / (1024**2), total_kb / (1024**2), (used_kb / total_kb) * 100.0
        except Exception:
            return None
    return None


def get_system_status() -> str:
    """Liefert kompakte Systeminformationen (Speicher, RAM, Temperatur und Pi-5-Sensoren)."""
    disk = shutil.disk_usage(Path.home())
    free_gb = disk.free / (1024**3)
    total_gb = disk.total / (1024**3)

    arch = platform.machine()
    sys_label = "Raspberry Pi 5" if is_raspberry_pi_5() else f"{platform.system()} ({arch})"
    parts = [f"System {sys_label}, {free_gb:.1f} von {total_gb:.1f} Gigabyte Speicher frei"]

    ram = get_ram_usage()
    if ram is not None:
        used_gb, ram_total, pct = ram
        parts.append(f"Arbeitsspeicher zu {pct:.0f} Prozent belegt, also {used_gb:.1f} von {ram_total:.1f} Gigabyte")

    temp_c = get_cpu_temperature_celsius()
    if temp_c is not None:
        parts.append(f"CPU-Temperatur bei {temp_c:.1f} Grad Celsius")

    fan_rpm = get_pi5_fan_rpm()
    if fan_rpm is not None:
        parts.append(f"Lüfterdrehzahl {fan_rpm} Umdrehungen pro Minute")

    throttle_warn = get_pi5_throttle_warning()
    if throttle_warn:
        parts.append(throttle_warn)

    return ", ".join(parts) + "."


_LAST_CPU_TIMES: Optional[tuple[int, int]] = None


def get_cpu_load_percent() -> float:
    """Ermittelt die aktuelle CPU-Auslastung plattformunabhängig in Prozent."""
    global _LAST_CPU_TIMES
    try:
        import psutil  # type: ignore[import-not-found]

        return float(psutil.cpu_percent(interval=None))
    except Exception:
        pass

    stat_path = Path("/proc/stat")
    if stat_path.exists():
        try:
            first_line = stat_path.read_text(encoding="utf-8").splitlines()[0]
            nums = [int(x) for x in first_line.split()[1:]]
            idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
            total = sum(nums)
            if _LAST_CPU_TIMES is not None:
                prev_idle, prev_total = _LAST_CPU_TIMES
                d_total = total - prev_total
                d_idle = idle - prev_idle
                _LAST_CPU_TIMES = (idle, total)
                if d_total > 0:
                    return max(0.0, min(100.0, (1.0 - d_idle / d_total) * 100.0))
            _LAST_CPU_TIMES = (idle, total)
        except Exception:
            pass
    return 0.0


def get_network_info() -> dict[str, Any]:
    """Liefert Hostname, lokale IPv4-Adresse und Erreichbarkeit des Netzwerks."""
    hostname = socket.gethostname()
    local_ip = "127.0.0.1"
    online = False
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(0.4)
            sock.connect(("8.8.8.8", 80))
            local_ip = sock.getsockname()[0]
            online = True
    except Exception:
        try:
            local_ip = socket.gethostbyname(hostname)
        except Exception:
            pass
    return {
        "hostname": hostname,
        "ip": local_ip,
        "online": online,
    }


_LAST_NET_IO: Optional[tuple[int, int, float]] = None


def _get_network_throughput_kbps() -> tuple[float, float]:
    """Berechnet aktuelle Upload-/Download-Rate in KB/s über psutil."""
    global _LAST_NET_IO
    try:
        import psutil  # type: ignore[import-not-found]

        counters = psutil.net_io_counters()
        now = time.monotonic()
        sent = int(getattr(counters, "bytes_sent", 0))
        recv = int(getattr(counters, "bytes_recv", 0))
        if _LAST_NET_IO is not None:
            prev_sent, prev_recv, prev_ts = _LAST_NET_IO
            dt = max(0.2, now - prev_ts)
            up_kbps = max(0.0, (sent - prev_sent) / 1024.0 / dt)
            down_kbps = max(0.0, (recv - prev_recv) / 1024.0 / dt)
            _LAST_NET_IO = (sent, recv, now)
            return round(up_kbps, 1), round(down_kbps, 1)
        _LAST_NET_IO = (sent, recv, now)
    except Exception:
        pass
    return 0.0, 0.0


def _get_process_count() -> int:
    try:
        import psutil  # type: ignore[import-not-found]

        return len(psutil.pids())
    except Exception:
        return 0


def _check_ollama_port_status() -> str:
    try:
        with socket.create_connection(("127.0.0.1", 11434), timeout=0.06):
            return "AKTIV"
    except Exception:
        return "BEREIT (STANDBY)"


_GPU_CACHE: dict[str, Any] = {"ts": 0.0, "pct": None, "name": ""}


def _get_gpu_telemetry() -> tuple[Optional[float], str]:
    """Liest GPU-Auslastung leichtgewichtig und gecacht (max. alle 6s, kein Dauer-Subprozess)."""
    now = time.monotonic()
    if (now - float(_GPU_CACHE.get("ts", 0.0))) < 6.0:
        return _GPU_CACHE.get("pct"), str(_GPU_CACHE.get("name") or "")
    pct: Optional[float] = None
    name = ""
    try:
        smi = shutil.which("nvidia-smi")
        if smi:
            hide_flags = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)} if platform.system() == "Windows" else {}
            res = subprocess.run(
                [smi, "--query-gpu=utilization.gpu,name", "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
                timeout=1.2,
                check=False,
                **hide_flags,
            )
            line = (res.stdout or "").strip().splitlines()[0] if (res.stdout or "").strip() else ""
            if "," in line:
                u_str, g_name = line.split(",", 1)
                pct = round(float(u_str.strip()), 1)
                name = g_name.strip()[:32]
    except Exception:
        pct = None
    _GPU_CACHE["ts"] = now
    _GPU_CACHE["pct"] = pct
    _GPU_CACHE["name"] = name
    return pct, name


def get_live_telemetry() -> dict[str, Any]:
    """Sammelt Echtzeit-Systemdaten (CPU, GPU, RAM, Temperatur, Speicher, Netzwerk, Ollama) für das HUD."""
    ram = get_ram_usage()
    ram_used_gb = round(ram[0], 1) if ram else 0.0
    ram_total_gb = round(ram[1], 1) if ram else 0.0
    ram_pct = round(ram[2], 1) if ram else 0.0

    disk = shutil.disk_usage(Path.home())
    disk_free_gb = round(disk.free / (1024**3), 1)
    disk_total_gb = round(disk.total / (1024**3), 1)
    net_up_kbps, net_down_kbps = _get_network_throughput_kbps()
    gpu_pct, gpu_name = _get_gpu_telemetry()

    return {
        "cpu_pct": round(get_cpu_load_percent(), 1),
        "gpu_pct": gpu_pct,
        "gpu_name": gpu_name,
        "ram_used_gb": ram_used_gb,
        "ram_total_gb": ram_total_gb,
        "ram_pct": ram_pct,
        "temp_c": get_cpu_temperature_celsius(),
        "disk_free_gb": disk_free_gb,
        "disk_total_gb": disk_total_gb,
        "net_up_kbps": net_up_kbps,
        "net_down_kbps": net_down_kbps,
        "proc_count": _get_process_count(),
        "ollama_status": _check_ollama_port_status(),
        "network": get_network_info(),
        "pi5": {
            "is_pi5": is_raspberry_pi_5(),
            "arch": platform.machine(),
            "os_codename": get_os_codename(),
            "session_type": get_session_type(),
            "gpio_chip": find_rp1_gpio_chip(),
            "fan_rpm": get_pi5_fan_rpm(),
            "throttle_warning": get_pi5_throttle_warning(),
        },
    }


_WEEKDAYS_DE = (
    "Montag",
    "Dienstag",
    "Mittwoch",
    "Donnerstag",
    "Freitag",
    "Samstag",
    "Sonntag",
)
_MONTHS_DE = (
    "",
    "Januar",
    "Februar",
    "März",
    "April",
    "Mai",
    "Juni",
    "Juli",
    "August",
    "September",
    "Oktober",
    "November",
    "Dezember",
)


def fetch_live_weather(city: str = "") -> Optional[str]:
    """DEAKTIVIERT (100% VISIBLE DESKTOP OPERATOR): Keine stillen HTTP-Wetterabfragen im Hintergrund."""
    return None


def count_unread_emails() -> Optional[int]:
    """Zählt ungelesene E-Mails, falls Zugangsdaten konfiguriert und erreichbar sind."""
    cfg = _email_config()
    if not cfg["address"] or not cfg["password"] or not cfg["imap_server"]:
        return None
    try:
        with imaplib.IMAP4_SSL(cfg["imap_server"], cfg["imap_port"], timeout=5.0) as mail:
            mail.login(cfg["address"], cfg["password"])
            mail.select("INBOX")
            status, data = mail.search(None, "UNSEEN")
            if status == "OK" and data and data[0]:
                return len(data[0].split())
            return 0
    except Exception:
        return None


def build_status_briefing() -> str:
    """Erstellt den gesprochenen Statusbericht (Zeit, Wetter, E-Mails, Systemdiagnose)."""
    now = datetime.now()
    weekday = _WEEKDAYS_DE[now.weekday()]
    month = _MONTHS_DE[now.month]
    segments = [
        f"Guten Tag, Chef. Hier ist Ihr Statusbericht. Es ist {now.hour} Uhr {now.minute:02d} am {weekday}, dem {now.day}. {month}.",
    ]

    if not get_is_online():
        segments.append("Der Online-Modus ist aktuell deaktiviert, daher überspringe ich Wetter- und Börsendaten.")
    else:
        weather = fetch_live_weather()
        if weather:
            segments.append(weather)
        else:
            segments.append("Das Live-Wetter ist im Moment leider nicht abrufbar.")

        unread = count_unread_emails()
        if unread is not None:
            if unread == 0:
                segments.append("In Ihrem Postfach befinden sich keine ungelesenen E-Mails.")
            elif unread == 1:
                segments.append("Sie haben eine ungelesene E-Mail.")
            else:
                segments.append(f"Sie haben {unread} ungelesene E-Mails.")

    ram = get_ram_usage()
    temp_c = get_cpu_temperature_celsius()
    diag_parts: list[str] = []
    if temp_c is not None:
        diag_parts.append(f"die CPU-Temperatur liegt bei {temp_c:.1f} Grad Celsius")
    if ram is not None:
        _, _, pct = ram
        diag_parts.append(f"der Arbeitsspeicher ist zu {pct:.0f} Prozent ausgelastet")
    if diag_parts:
        segments.append("Zur Systemdiagnose: " + " und ".join(diag_parts) + ".")

    return " ".join(segments)


def save_project_log_entry(description: str, image_bytes: Optional[bytes]) -> Path:
    """Speichert ein Werkstatt-Foto samt Zeitstempel und Beschreibung im aktiven Archiv (NAS oder Lokal)."""
    log_dir, _ = get_archive_subdir("projekt_logs")
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

    txt_path = log_dir / f"{stamp}.txt"
    img_info = "Kein Kamerabild verfügbar"
    if image_bytes is not None:
        img_path = log_dir / f"{stamp}.jpg"
        img_path.write_bytes(image_bytes)
        img_info = img_path.name

    txt_path.write_text(
        f"Zeitstempel: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"Bilddatei: {img_info}\n"
        f"Beschreibung: {description}\n",
        encoding="utf-8",
    )
    return txt_path


_TICKER_DISPLAY_NAMES = {
    "NVDA": "Nvidia",
    "AAPL": "Apple",
    "TSLA": "Tesla",
    "RHM.DE": "Rheinmetall",
    "MSFT": "Microsoft",
    "AMZN": "Amazon",
    "GOOGL": "Alphabet",
    "META": "Meta",
    "BTC-USD": "Bitcoin",
}


def fetch_watchlist_quotes() -> list[dict[str, Any]]:
    """DEAKTIVIERT (100% VISIBLE DESKTOP OPERATOR): Keine stillen yfinance-Hintergrundsuchen."""
    return []


def fetch_today_ics_events() -> list[str]:
    """Liest heutige Termine aus einer optionalen iCal-URL (.ics) aus."""
    if not get_is_online():
        return []
    ics_url = os.getenv("CALENDAR_ICS_URL", "").strip()
    if not ics_url:
        return []

    try:
        req = urllib.request.Request(ics_url, headers={"User-Agent": "Jarvis-Calendar/1.0"})
        with urllib.request.urlopen(req, timeout=4.0) as resp:
            raw_ics = resp.read().decode("utf-8", errors="replace")
    except Exception:
        return []

    unfolded = re.sub(r"\r?\n[ \t]", "", raw_ics)
    today_compact = datetime.now().strftime("%Y%m%d")
    events: list[str] = []

    for block in unfolded.split("BEGIN:VEVENT")[1:]:
        body = block.split("END:VEVENT", 1)[0]
        dt_match = re.search(r"^DTSTART[^:]*:(\d{8})(?:T(\d{2})(\d{2}))?", body, flags=re.MULTILINE)
        sum_match = re.search(r"^SUMMARY[^:]*:(.+)$", body, flags=re.MULTILINE)
        if not dt_match or not sum_match:
            continue
        if dt_match.group(1) != today_compact:
            continue
        summary = sum_match.group(1).strip()
        if dt_match.group(2) and dt_match.group(3):
            events.append(f"um {dt_match.group(2)} Uhr {dt_match.group(3)}: {summary}")
        else:
            events.append(summary)

    return events[:6]


def collect_morning_briefing_data(memory_tasks: list[str]) -> dict[str, Any]:
    """Sammelt alle Live-Daten für das High-Energy Morgen-Briefing."""
    now = datetime.now()
    return {
        "time_str": f"{now.hour} Uhr {now.minute:02d}",
        "weekday": _WEEKDAYS_DE[now.weekday()],
        "date_str": f"{now.day}. {_MONTHS_DE[now.month]}",
        "weather": fetch_live_weather(),
        "calendar_events": fetch_today_ics_events(),
        "memory_tasks": memory_tasks[:5],
        "stocks": fetch_watchlist_quotes(),
        "unread_emails": count_unread_emails(),
    }


def format_fallback_morning_briefing(data: dict[str, Any], trolling_active: bool) -> str:
    """Erstellt ein energiegeladenes Morgen-Briefing ohne Cloud-Abhängigkeit."""
    parts = [
        f"Guten Morgen, Chef! Es ist {data['time_str']} am {data['weekday']}, dem {data['date_str']}. Raus aus den Federn, die Welt rettet sich nicht von allein."
    ]
    if trolling_active:
        parts.append(
            "Und ja, der schrille Piepton war volle Absicht – schließlich wollte ich sicherstellen, dass Sie nicht wieder die Schlummertaste gewinnen lassen."
        )

    if data.get("weather"):
        parts.append(data["weather"])

    events = data.get("calendar_events") or []
    tasks = data.get("memory_tasks") or []
    if events:
        parts.append("Auf Ihrem heutigen Terminplan steht: " + ", ".join(events) + ".")
    elif tasks:
        parts.append("Im Gedächtnis sind noch folgende Projekte vorgemerkt: " + "; ".join(tasks[:3]) + ".")

    stocks = data.get("stocks") or []
    if stocks:
        best = max(stocks, key=lambda s: s["change_pct"])
        if best["change_pct"] >= 1.5:
            parts.append(
                f"Gute Nachrichten für Ihr Depot, Chef: {best['name']} hat um {best['change_pct']:.1f} Prozent zugelegt!"
            )
        summary_bits = [
            f"{s['name']} bei {s['price']} mit {s['change_pct']:+.1f} Prozent"
            for s in stocks[:4]
        ]
        parts.append("Kurzer Blick auf Ihre Watchlist: " + ", ".join(summary_bits) + ".")

    unread = data.get("unread_emails")
    if unread is not None:
        if unread > 0:
            parts.append(f"Außerdem warten {unread} ungelesene E-Mails auf Ihre Aufmerksamkeit.")
        else:
            parts.append("Ihr Posteingang ist komplett sauber.")

    parts.append("Systeme laufen auf hundert Prozent, Chef. Viel Erfolg heute!")
    return " ".join(parts)


def get_archive_temp_dir() -> Path:
    """Liefert den aktiven Temp-Ordner (auf NAS falls erreichbar, sonst ./projekt_logs/temp)."""
    temp_dir, _ = get_archive_subdir("temp")
    return temp_dir


def sync_archive_temp_environment() -> tuple[Path, bool]:
    """Synchronisiert tempfile.tempdir & JARVIS_TEMP_DIR mit dem aktiven Archiv-Pfad."""
    root, using_nas = resolve_archive_root()
    temp_dir, using_nas_temp = get_archive_subdir("temp")
    tempfile.tempdir = str(temp_dir)
    os.environ["JARVIS_TEMP_DIR"] = str(temp_dir)
    return root, (using_nas and using_nas_temp)


# ============================================================
# Commercial Release: 1-FPS Hand-Agent & Sichtbare Browser-Recherche
# ============================================================


def visual_browser_search_and_read(
    query: str,
    brain: Any = None,
    on_status: Any = None,
    on_preview_frame: Any = None,
) -> str:
    """
    100% VISIBLE DESKTOP & APP OPERATOR (AUFGABE 2):
    Sucht niemals still im Hintergrund! Öffnet den echten Desktop-Webbrowser sichtbar im Vordergrund,
    tippt `clean_query` ein, drückt Enter, bedient per 1-FPS-Mausschleife Ergebnisse/Cookie-Banner
    und liest die Antwort direkt vom Live-Screenshot ab.
    """
    from agent_hand import VISUAL_HAND_AGENT

    res = VISUAL_HAND_AGENT.visual_browser_search_and_read(
        query=query,
        brain=brain,
        on_status=on_status,
        on_preview_frame=on_preview_frame,
        interactive_click=True,
        max_steps=None,
    )
    return str(res.get("summary") or res.get("message") or f"Ich habe '{query}' sichtbar im Desktop-Browser geöffnet.")


def visible_desktop_browser_search(
    query: str,
    brain: Any = None,
    on_status: Any = None,
    on_preview_frame: Any = None,
) -> str:
    """
    FIX 1 + FIX 2:
    1. Extrahiert vorab sauber `target_url`, `clean_search_term` und `final_goal` (kein stumpfes Satz-Kopieren!).
    2. Öffnet die echte Zielwebseite sichtbar im Standard-Desktop-Browser, holt ihn in den Vordergrund
       und startet zwingend die aktive 1-FPS-Vision-Mausschleife (Cookie-Check, Suche, Ergebnis-Klick),
       bis `final_goal` auf dem Monitor sichtbar erreicht ist.
    """
    return visual_browser_search_and_read(
        query=query,
        brain=brain,
        on_status=on_status,
        on_preview_frame=on_preview_frame,
    )


def visual_desktop_app_operator(
    goal: str,
    app_name: str = "",
    brain: Any = None,
    on_status: Any = None,
    on_preview_frame: Any = None,
    max_steps: Optional[int] = None,
) -> str:
    """
    100% VISIBLE DESKTOP & APP OPERATOR (AUFGABE 3 & 4):
    Steuert lokale Laptop-Aufgaben inklusive CAD (Solid Craft 3D), 3D-Druck (OrcaSlicer),
    Videoschnitt (CapCut/DaVinci/Premiere) und Live-Desktop-Coding sichtbar über Maus- & Action-Batching.
    """
    from agent_hand import VISUAL_HAND_AGENT, detect_pro_app_profile

    profile_key, _ = detect_pro_app_profile(goal, app_name)
    res = VISUAL_HAND_AGENT.visual_desktop_app_operator(
        goal=goal,
        app_name=app_name,
        brain=brain,
        on_status=on_status,
        on_preview_frame=on_preview_frame,
        max_steps=max_steps,
    )
    if profile_key and isinstance(res, dict) and not res.get("app_profile"):
        res["app_profile"] = profile_key
    return str(res.get("summary") or res.get("message") or f"App-Operator für '{goal}' ausgeführt.")


def run_visual_mouse_loop(
    goal: str,
    brain: Any = None,
    on_status: Any = None,
    on_preview_frame: Any = None,
    max_steps: Optional[int] = None,
    initial_url: str = "",
) -> str:
    """Startet die zwingende 1-FPS Marathon-Mausschleife (Standard 200 Schritte, 50–500 konfigurierbar) nach dem Öffnen einer Webseite oder auf dem Desktop."""
    try:
        from agent_hand import VISUAL_HAND_AGENT

        return VISUAL_HAND_AGENT.run_visual_mouse_loop(
            goal=goal,
            brain=brain,
            on_status=on_status,
            on_preview_frame=on_preview_frame,
            max_steps=max_steps,
            initial_url=initial_url,
            require_interaction_after_open=True,
        )
    except Exception as exc:
        return f"Fehler in der visuellen 1-FPS-Mausschleife: {exc}"


def run_visual_hand_agent(
    goal: str,
    brain: Any = None,
    on_status: Any = None,
    on_preview_frame: Any = None,
    max_steps: Optional[int] = None,
) -> str:
    """
    Führt den geschlossenen 1-FPS Vision-Maus-Agenten (mit rotem [X, Y] Fadenkreuz & 200px Gitter) aus.
    """
    from agent_hand import VISUAL_HAND_AGENT

    res = VISUAL_HAND_AGENT.run_closed_loop_goal(
        goal=goal,
        brain=brain,
        on_status=on_status,
        on_preview_frame=on_preview_frame,
        max_steps=max_steps,
    )
    return str(res.get("message") or "Visueller 1-FPS Hand-Agent abgeschlossen.")


def click(x: int, y: int, button: str = "left", clicks: int = 1, label: str = "") -> str:
    from agent_hand import click as _hand_click

    return _hand_click(x=x, y=y, button=button, clicks=clicks, label=label)


def double_click(x: int, y: int, label: str = "", press_enter: bool = True) -> str:
    from agent_hand import double_click as _hand_double_click

    return _hand_double_click(x=x, y=y, label=label, press_enter=press_enter)


def right_click(x: int, y: int, label: str = "") -> str:
    from agent_hand import right_click as _hand_right_click

    return _hand_right_click(x=x, y=y, label=label)


def drag_and_drop(start_x: int, start_y: int, end_x: int, end_y: int, duration: float = 0.65, label: str = "") -> str:
    from agent_hand import drag_and_drop as _hand_drag_and_drop

    return _hand_drag_and_drop(start_x=start_x, start_y=start_y, end_x=end_x, end_y=end_y, duration=duration, label=label)


def click_and_type(x: int, y: int, text: str, press_enter: bool = False, label: str = "") -> str:
    from agent_hand import click_and_type as _hand_click_and_type

    return _hand_click_and_type(x=x, y=y, text=text, press_enter=press_enter, label=label)


def scroll(amount: int, x: Optional[int] = None, y: Optional[int] = None, label: str = "") -> str:
    from agent_hand import scroll as _hand_scroll

    return _hand_scroll(amount=amount, x=x, y=y, label=label)


def hotkey(*keys: Any, label: str = "") -> str:
    from agent_hand import hotkey as _hand_hotkey

    return _hand_hotkey(*keys, label=label)


def precision_click(
    x: int,
    y: int,
    button: str = "left",
    clicks: int = 1,
    target_label: str = "",
    label: str = "",
) -> str:
    from agent_hand import precision_click as _hand_precision_click

    return _hand_precision_click(
        x=x,
        y=y,
        button=button,
        clicks=clicks,
        target_label=target_label,
        label=label,
    )


def click_tag(tag_id: int, button: str = "left", clicks: int = 1, label: str = "") -> str:
    from ui_som_tagger import UI_SOM_TAGGER
    from agent_hand import click as _hand_click

    resolved = UI_SOM_TAGGER.resolve_tag_to_coords(int(tag_id))
    if resolved is None:
        return f"SoM-Tag [{tag_id}] nicht im aktuellen UI-Snapshot gefunden."
    tx, ty, tag_lbl = resolved
    return _hand_click(
        x=tx,
        y=ty,
        button=button,
        clicks=clicks,
        label=label or f"TAG [{tag_id}] {tag_lbl}",
    )


def cad_orbit(
    dx: int = 120,
    dy: int = -45,
    start_x: Optional[int] = None,
    start_y: Optional[int] = None,
    button: str = "auto",
    app_hint: str = "",
    duration: float = 0.28,
) -> str:
    from agent_hand import cad_orbit as _hand_cad_orbit

    return _hand_cad_orbit(
        dx=dx,
        dy=dy,
        start_x=start_x,
        start_y=start_y,
        button=button,
        app_hint=app_hint,
        duration=duration,
    )


def cad_pan(
    dx: int = 120,
    dy: int = 0,
    start_x: Optional[int] = None,
    start_y: Optional[int] = None,
    duration: float = 0.25,
) -> str:
    from agent_hand import cad_pan as _hand_cad_pan

    return _hand_cad_pan(dx=dx, dy=dy, start_x=start_x, start_y=start_y, duration=duration)


def cad_zoom(steps: int = 4, x: Optional[int] = None, y: Optional[int] = None) -> str:
    from agent_hand import cad_zoom as _hand_cad_zoom

    return _hand_cad_zoom(steps=steps, x=x, y=y)


def cad_box_and_dimension(
    start_x: int,
    start_y: int,
    end_x: int,
    end_y: int,
    dims: Optional[list[str]] = None,
) -> dict[str, Any]:
    from agent_hand import cad_box_and_dimension as _hand_cad_box

    return _hand_cad_box(
        start_x=start_x,
        start_y=start_y,
        end_x=end_x,
        end_y=end_y,
        dims=dims or ["4", "4", "4"],
    )


def timeline_scrub_and_cut(
    playhead_x: Optional[int] = None,
    timeline_y: Optional[int] = None,
    target_x: Optional[int] = None,
    target_end_x: Optional[int] = None,
    editor_name: str = "CapCut",
    ripple_delete: bool = True,
) -> dict[str, Any]:
    from agent_hand import timeline_scrub_and_cut as _hand_timeline_cut

    return _hand_timeline_cut(
        playhead_x=playhead_x,
        timeline_y=timeline_y,
        target_x=target_x,
        target_end_x=target_end_x,
        editor_name=editor_name,
        ripple_delete=ripple_delete,
    )


def start_skill_recording(skill_name: str = "", app_name: str = "") -> dict[str, Any]:
    from skill_recorder import SKILL_RECORDER

    return SKILL_RECORDER.start_recording(skill_name=skill_name, app_name=app_name)


def stop_skill_recording(skill_name: str = "", app_name: str = "") -> dict[str, Any]:
    from skill_recorder import SKILL_RECORDER

    return SKILL_RECORDER.stop_recording(skill_name=skill_name, app_name=app_name)


def replay_ui_skill(skill_query: str, speed_multiplier: float = 10.0) -> dict[str, Any]:
    from skill_recorder import SKILL_RECORDER

    return SKILL_RECORDER.replay_skill(skill_query=skill_query, speed_multiplier=speed_multiplier)


def browser_new_tab(url: str = "") -> str:
    from agent_hand import browser_new_tab as _hand_browser_new_tab

    return _hand_browser_new_tab(url=url)


def browser_switch_tab(
    tab_index: Optional[int] = None,
    direction: str = "next",
    x: Optional[int] = None,
    y: Optional[int] = None,
) -> str:
    from agent_hand import browser_switch_tab as _hand_browser_switch_tab

    return _hand_browser_switch_tab(tab_index=tab_index, direction=direction, x=x, y=y)


def browser_back() -> str:
    from agent_hand import browser_back as _hand_browser_back

    return _hand_browser_back()


def browser_forward() -> str:
    from agent_hand import browser_forward as _hand_browser_forward

    return _hand_browser_forward()


def browser_address_bar(url_or_query: str = "", press_enter: bool = True) -> str:
    from agent_hand import browser_address_bar as _hand_browser_address_bar

    return _hand_browser_address_bar(url_or_query=url_or_query, press_enter=press_enter)


def browser_find_on_page(search_text: str = "", next_match: bool = False) -> str:
    from agent_hand import browser_find_on_page as _hand_browser_find

    return _hand_browser_find(search_text=search_text, next_match=next_match)


def browser_select_dropdown(
    x: Optional[int] = None,
    y: Optional[int] = None,
    option_text: str = "",
    steps_down: int = 0,
) -> str:
    from agent_hand import browser_select_dropdown as _hand_browser_dropdown

    return _hand_browser_dropdown(x=x, y=y, option_text=option_text, steps_down=steps_down)


def browser_upload_file(
    file_path: str,
    button_x: Optional[int] = None,
    button_y: Optional[int] = None,
) -> str:
    from agent_hand import browser_upload_file as _hand_browser_upload

    return _hand_browser_upload(file_path=file_path, button_x=button_x, button_y=button_y)


def browser_video_control(
    command: str = "toggle_pause",
    x: Optional[int] = None,
    y: Optional[int] = None,
) -> str:
    from agent_hand import browser_video_control as _hand_browser_video

    return _hand_browser_video(command=command, x=x, y=y)


def propose_app_installation(user_goal: str, explicit_app: str = "") -> Optional[dict[str, Any]]:
    from app_installer_agent import APP_INSTALLER_AGENT

    return APP_INSTALLER_AGENT.detect_missing_app_and_propose(user_goal=user_goal, explicit_app=explicit_app)


def execute_pending_app_installation() -> dict[str, Any]:
    from app_installer_agent import APP_INSTALLER_AGENT

    return APP_INSTALLER_AGENT.execute_pending_install()


def rescan_installed_apps(deep: bool = True) -> dict[str, Any]:
    from app_indexer import APP_INDEXER

    apps = APP_INDEXER.rescan_apps(deep=deep)
    return {"ok": True, "count": len(apps)}


def show_live_browser_search(query: str, brain: Any = None, on_status: Optional[Callable[[str], None]] = None) -> dict[str, Any]:
    """100% SHOW-EFFEKT: Übergibt Kurs-, Preis-, Wetter- oder Wissensfragen an den sichtbaren Desktop-Browser."""
    from agent_hand import VISUAL_HAND_AGENT

    return VISUAL_HAND_AGENT.visual_browser_search_and_read(
        query=query,
        brain=brain,
        on_status=on_status,
        interactive_click=True,
    )


def run_executive_briefing_tool(open_browser_cockpit: bool = True) -> dict[str, Any]:
    """MODUL 2: Startet das proaktive CEO Morning Briefing inkl. sichtbarer Browser-Cockpit-Tabs."""
    from executive_assistant import EXECUTIVE_ASSISTANT

    return EXECUTIVE_ASSISTANT.run_executive_briefing(open_browser_cockpit=open_browser_cockpit)


def second_brain_resume_project(query: str = "letztes Projekt") -> dict[str, Any]:
    """MODUL 1: Öffnet das passende aktuelle oder gestrige Projekt aus dem Second Brain sichtbar auf dem Desktop."""
    from executive_assistant import EXECUTIVE_ASSISTANT

    return EXECUTIVE_ASSISTANT.resume_project_or_file(query=query)


def pipeline_research_to_desktop(query: str) -> dict[str, Any]:
    """MODUL 3 Pipeline A: Live Browser-Recherche -> formatierte Zusammenfassung/Tabelle auf dem Desktop."""
    from executive_assistant import EXECUTIVE_ASSISTANT

    return EXECUTIVE_ASSISTANT.run_research_to_desktop_pipeline(query=query)


def pipeline_project_setup(request_text: str, project_name: str = "", category: str = "") -> dict[str, Any]:
    """MODUL 3 Pipeline B: Projektordner anlegen + Hauptprogramm & Referenz-Website öffnen."""
    from executive_assistant import EXECUTIVE_ASSISTANT

    return EXECUTIVE_ASSISTANT.run_project_setup_pipeline(
        request_text=request_text,
        project_name=project_name,
        category=category,
    )


def confirm_smart_suggestion() -> dict[str, Any]:
    """MODUL 4: Bestätigt und startet den aktuellen proaktiven Smart-Suggestion-Vorschlag."""
    from executive_assistant import EXECUTIVE_ASSISTANT

    return EXECUTIVE_ASSISTANT.suggestions.confirm_suggestion(assistant=EXECUTIVE_ASSISTANT)


def list_monitors_tool() -> dict[str, Any]:
    """Listet alle angeschlossenen Monitore inkl. Auflösung, Position (links/mitte/rechts) und Offset."""
    from multi_monitor import get_connected_monitors

    mons = get_connected_monitors()
    return {"ok": True, "count": len(mons), "monitors": mons}


def capture_monitor_screen(monitor: Any = "primary", with_grid: bool = False) -> dict[str, Any]:
    """Erstellt gezielt einen Screenshot von Monitor 1, 2, 3 oder allen ('all') gleichzeitig."""
    from multi_monitor import capture_monitor_jpeg

    gui_inst = None
    try:
        from gui import get_active_gui

        gui_inst = get_active_gui()
    except Exception:
        gui_inst = None

    def _on_scan(tgt: Any, detail: str) -> None:
        if gui_inst is not None:
            gui_inst.emit_brain_activity(state="screen_scan", intensity=0.9, active_monitor=tgt, detail=detail)
            gui_inst.emit_neural_thought(category="vision", tool="capture_monitor_screen", summary=detail, monitor=tgt)

    res = capture_monitor_jpeg(target=monitor, with_grid=with_grid, on_scan=_on_scan)
    return {
        "ok": res.get("ok", False),
        "target": res.get("target"),
        "monitors_captured": res.get("monitors_captured", []),
        "width": res.get("width", 0),
        "height": res.get("height", 0),
        "summary": res.get("summary", ""),
    }


def move_window_to_monitor_tool(window_title: str = "", monitor: Any = 1, layout: str = "maximize") -> dict[str, Any]:
    """Verschiebt ein Programmfenster gezielt auf Monitor 1, 2 oder 3 (links/mitte/rechts) und ordnet es an."""
    from multi_monitor import move_window_to_monitor

    res = move_window_to_monitor(window_query=window_title, monitor_target=monitor, layout=layout)
    try:
        from gui import get_active_gui

        gui_inst = get_active_gui()
        if gui_inst is not None:
            gui_inst.emit_neural_thought(
                category="tool",
                tool="move_window_to_monitor",
                summary=str(res.get("message") or ""),
                monitor=(res.get("monitor") or {}).get("index"),
            )
    except Exception:
        pass
    return res


def create_n8n_workflow(goal: str, name: str = "", activate: bool = True, brain: Any = None) -> dict[str, Any]:
    """Generiert ein valides n8n-Workflow-JSON inkl. Webhook-Trigger, lädt es hoch & aktiviert es (oder nutzt Webhook-Fallback)."""
    from n8n_architect import N8N_ARCHITECT

    return N8N_ARCHITECT.create_workflow(goal=goal, name=name, activate=activate, brain=brain)


def list_and_manage_n8n_workflows(action: str = "list", workflow_id: str = "") -> dict[str, Any]:
    """Listet, aktiviert, deaktiviert oder löscht Workflows über die lokale n8n REST-API (/api/v1/workflows)."""
    from n8n_architect import N8N_ARCHITECT

    return N8N_ARCHITECT.list_and_manage(action=action, workflow_id=workflow_id)


def run_n8n_workflow(target: str = "", payload: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Führt einen lokalen n8n-Workflow per Webhook/REST-API aus und visualisiert Impulse im 3D-Gehirn."""
    from n8n_architect import N8N_ARCHITECT

    return N8N_ARCHITECT.run_workflow(target=target, payload=payload)


def open_window_tool(window_name: str = "screen-matrix", monitor: Any = None) -> dict[str, Any]:
    """Öffnet Fenster 2 ('screen-matrix') oder Fenster 3 ('system-telemetry') direkt auf dem Zielmonitor."""
    from macro_engine import open_window

    return open_window(window_name=window_name, monitor=monitor)


def highlight_monitor_region_tool(
    monitor: Any = 1,
    x_pct: float = 0.0,
    y_pct: float = 0.0,
    w_pct: float = 1.0,
    h_pct: float = 1.0,
    label: str = "JARVIS SCAN",
    duration: float = 2.4,
) -> dict[str, Any]:
    """Markiert einen Monitor oder Bildschirm-Ausschnitt live per Overlay-Highlight-Rahmen."""
    from multi_monitor import highlight_monitor_region

    return highlight_monitor_region(
        monitor=monitor,
        x_pct=x_pct,
        y_pct=y_pct,
        w_pct=w_pct,
        h_pct=h_pct,
        label=label,
        duration=duration,
    )


def read_monitor_ocr_tool(monitor: Any = 1, question: str = "Lies den sichtbaren Text und die Inhalte aus.") -> dict[str, Any]:
    """Erfasst den gewählten Monitor in voller Auflösung und liest Inhalte per Vision/OCR aus."""
    from macro_engine import read_monitor_ocr

    return read_monitor_ocr(monitor_target=monitor, question=question)


def run_work_mode_macro() -> dict[str, Any]:
    """Makro 'Arbeitsmodus starten': verteilt Browser, Editor & Zusatzfenster über alle Monitore."""
    from macro_engine import MACRO_ENGINE

    return MACRO_ENGINE.start_work_mode()


def focus_and_analyze_monitor(monitor: Any = 2) -> dict[str, Any]:
    """Makro 'Fokus auf Monitor X': hebt den Monitor hervor und analysiert ihn in voller Auflösung."""
    from macro_engine import MACRO_ENGINE

    return MACRO_ENGINE.focus_monitor(monitor_target=monitor)


def get_system_processes_and_windows() -> dict[str, Any]:
    """Liefert Live-Prozesse (psutil), aktive Fenster pro Monitor und Maus-Koordinaten."""
    from multi_monitor import get_monitor_window_telemetry, get_system_processes

    return {**get_monitor_window_telemetry(force=True), "processes": get_system_processes(10)}


def bring_window_here(window_title: str = "") -> dict[str, Any]:
    """Holt das zuletzt gestartete oder benannte Fenster auf den aktuellen Maus-Monitor."""
    from multi_monitor import bring_last_window_to_cursor_monitor

    return bring_last_window_to_cursor_monitor(window_query=window_title)


TOOL_REGISTRY: dict[str, Any] = {
    "open_app": open_app,
    "open_application": open_application,
    "open_url": open_url,
    "visual_browser_search_and_read": visual_browser_search_and_read,
    "visual_desktop_app_operator": visual_desktop_app_operator,
    "visible_desktop_browser_search": visible_desktop_browser_search,
    "run_visual_hand_agent": run_visual_hand_agent,
    "run_visual_mouse_loop": run_visual_mouse_loop,
    "click": click,
    "precision_click": precision_click,
    "click_tag": click_tag,
    "double_click": double_click,
    "right_click": right_click,
    "drag_and_drop": drag_and_drop,
    "click_and_type": click_and_type,
    "scroll": scroll,
    "hotkey": hotkey,
    "cad_orbit": cad_orbit,
    "cad_pan": cad_pan,
    "cad_zoom": cad_zoom,
    "cad_box_and_dimension": cad_box_and_dimension,
    "timeline_scrub_and_cut": timeline_scrub_and_cut,
    "start_skill_recording": start_skill_recording,
    "stop_skill_recording": stop_skill_recording,
    "replay_ui_skill": replay_ui_skill,
    "browser_new_tab": browser_new_tab,
    "browser_switch_tab": browser_switch_tab,
    "browser_back": browser_back,
    "browser_forward": browser_forward,
    "browser_address_bar": browser_address_bar,
    "browser_find_on_page": browser_find_on_page,
    "browser_select_dropdown": browser_select_dropdown,
    "browser_upload_file": browser_upload_file,
    "browser_video_control": browser_video_control,
    "propose_app_installation": propose_app_installation,
    "execute_pending_app_installation": execute_pending_app_installation,
    "rescan_installed_apps": rescan_installed_apps,
    "show_live_browser_search": show_live_browser_search,
    "run_executive_briefing": run_executive_briefing_tool,
    "second_brain_resume_project": second_brain_resume_project,
    "pipeline_research_to_desktop": pipeline_research_to_desktop,
    "pipeline_project_setup": pipeline_project_setup,
    "confirm_smart_suggestion": confirm_smart_suggestion,
    "list_monitors": list_monitors_tool,
    "capture_monitor_screen": capture_monitor_screen,
    "move_window_to_monitor": move_window_to_monitor_tool,
    "create_n8n_workflow": create_n8n_workflow,
    "list_and_manage_n8n_workflows": list_and_manage_n8n_workflows,
    "run_n8n_workflow": run_n8n_workflow,
    "open_window": open_window_tool,
    "highlight_monitor_region": highlight_monitor_region_tool,
    "read_monitor_ocr": read_monitor_ocr_tool,
    "run_work_mode_macro": run_work_mode_macro,
    "focus_and_analyze_monitor": focus_and_analyze_monitor,
    "get_system_processes_and_windows": get_system_processes_and_windows,
    "bring_window_here": bring_window_here,
    "build_status_briefing": build_status_briefing,
}

