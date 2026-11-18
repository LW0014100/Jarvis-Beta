"""Medien-, Lautstärke- und YouTube-/Stream-Steuerung für Raspberry Pi 5 & Windows 11."""

from __future__ import annotations

import platform
import re
import shutil
import subprocess
from typing import Optional
import urllib.parse

import pyautogui

from tools import open_url
from web_search import find_first_youtube_url


VOLUME_UP_PATTERN = re.compile(
    r"\b(?:lauter|lautstärke\s+(?:hoch|erhöhen|rauf)|ton\s+lauter)\b",
    flags=re.IGNORECASE,
)
VOLUME_DOWN_PATTERN = re.compile(
    r"\b(?:leiser|lautstärke\s+(?:runter|leiser|senken|reduzieren)|ton\s+leiser)\b",
    flags=re.IGNORECASE,
)
MUTE_PATTERN = re.compile(
    r"\b(?:ton\s+aus|stumm(?:schalten)?|mute|unmute|ton\s+(?:an|wieder\s+ein))\b",
    flags=re.IGNORECASE,
)
PLAY_PAUSE_PATTERN = re.compile(
    r"\b(?:wiedergabe\s+(?:anhalten|pausieren|fortsetzen)|musik\s+(?:pause|stopp|weiter)|"
    r"video\s+(?:pause|weiter)|pause|pausieren|weiter(?:spielen)?|fortsetzen)\b",
    flags=re.IGNORECASE,
)
NEXT_TRACK_PATTERN = re.compile(
    r"\b(?:nächster?\s+(?:titel|song|lied|video)|überspringen|weiter\s+zum\s+nächsten|skip)\b",
    flags=re.IGNORECASE,
)
PREV_TRACK_PATTERN = re.compile(
    r"\b(?:vorheriger?\s+(?:titel|song|lied|video)|zurück\s+zum\s+vorherigen)\b",
    flags=re.IGNORECASE,
)
YOUTUBE_PLAY_PATTERN = re.compile(
    r"\b(?:spiel(?:e)?|starte|öffne)\b.*\b(?:youtube|musik|lied|song|playlist|stream|video)\b|"
    r"^(?:.*?\bjarvis\b[,:\s]*)?(?:spiel(?:e)?|hör(?:en)?\s+wir)\s+(.+)$",
    flags=re.IGNORECASE,
)


def _run_linux_cmd(args: list[str]) -> bool:
    try:
        res = subprocess.run(args, capture_output=True, timeout=2.5, check=False)
        return res.returncode == 0
    except Exception:
        return False


def adjust_volume(direction: str) -> str:
    """Ändert die Systemlautstärke ('up', 'down', 'mute')."""
    system = platform.system()
    if system == "Windows":
        if direction == "up":
            for _ in range(3):
                pyautogui.press("volumeup")
            return "Lautstärke erhöht, Chef."
        if direction == "down":
            for _ in range(3):
                pyautogui.press("volumedown")
            return "Lautstärke verringert, Chef."
        pyautogui.press("volumemute")
        return "Ton umgeschaltet, Chef."

    # Raspberry Pi OS (PipeWire wpctl / PulseAudio pactl / ALSA amixer)
    if shutil.which("wpctl"):
        if direction == "up" and _run_linux_cmd(["wpctl", "set-volume", "-l", "1.0", "@DEFAULT_AUDIO_SINK@", "8%+"]):
            return "Lautstärke erhöht, Chef."
        if direction == "down" and _run_linux_cmd(["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "8%-"]):
            return "Lautstärke verringert, Chef."
        if direction == "mute" and _run_linux_cmd(["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "toggle"]):
            return "Ton umgeschaltet, Chef."

    if shutil.which("pactl"):
        if direction == "up" and _run_linux_cmd(["pactl", "set-sink-volume", "@DEFAULT_SINK@", "+8%"]):
            return "Lautstärke erhöht, Chef."
        if direction == "down" and _run_linux_cmd(["pactl", "set-sink-volume", "@DEFAULT_SINK@", "-8%"]):
            return "Lautstärke verringert, Chef."
        if direction == "mute" and _run_linux_cmd(["pactl", "set-sink-mute", "@DEFAULT_SINK@", "toggle"]):
            return "Ton umgeschaltet, Chef."

    if shutil.which("amixer"):
        if direction == "up" and _run_linux_cmd(["amixer", "sset", "Master", "8%+"]):
            return "Lautstärke erhöht, Chef."
        if direction == "down" and _run_linux_cmd(["amixer", "sset", "Master", "8%-"]):
            return "Lautstärke verringert, Chef."
        if direction == "mute" and _run_linux_cmd(["amixer", "sset", "Master", "toggle"]):
            return "Ton umgeschaltet, Chef."

    if shutil.which("xdotool"):
        key_map = {
            "up": "XF86AudioRaiseVolume",
            "down": "XF86AudioLowerVolume",
            "mute": "XF86AudioMute",
        }
        _run_linux_cmd(["xdotool", "key", key_map[direction]])
        return "Audio-Taste gesendet, Chef."

    return "Kein unterstütztes Audio-Steuerwerkzeug gefunden, Chef."


def control_playback(action: str) -> str:
    """Steuert aktive Medienwiedergabe ('play_pause', 'next', 'prev')."""
    system = platform.system()
    if system == "Windows":
        key_map = {"play_pause": "playpause", "next": "nexttrack", "prev": "prevtrack"}
        pyautogui.press(key_map.get(action, "playpause"))
        return "Medienwiedergabe gesteuert, Chef."

    if shutil.which("playerctl"):
        cmd_map = {"play_pause": "play-pause", "next": "next", "prev": "previous"}
        if _run_linux_cmd(["playerctl", cmd_map.get(action, "play-pause")]):
            return "Medienwiedergabe gesteuert, Chef."

    if shutil.which("xdotool"):
        xkey_map = {
            "play_pause": "XF86AudioPlay",
            "next": "XF86AudioNext",
            "prev": "XF86AudioPrev",
        }
        if _run_linux_cmd(["xdotool", "key", xkey_map.get(action, "XF86AudioPlay")]):
            return "Medienwiedergabe gesteuert, Chef."

    if shutil.which("wtype"):
        wkey_map = {
            "play_pause": "XF86AudioPlay",
            "next": "XF86AudioNext",
            "prev": "XF86AudioPrev",
        }
        if _run_linux_cmd(["wtype", "-k", wkey_map.get(action, "XF86AudioPlay")]):
            return "Medienwiedergabe gesteuert, Chef."

    pyautogui.press("space" if action == "play_pause" else "right")
    return "Wiedergabe-Steuerung gesendet, Chef."


def play_youtube_or_stream(query: str) -> str:
    """Sucht direkt nach einem YouTube-Video/Musikstream und startet ihn im Browser."""
    cleaned = re.sub(
        r"\b(?:jarvis|spiel(?:e)?|starte|öffne|bitte|mal|auf|youtube|musik|lied|song|video|stream)\b",
        " ",
        query,
        flags=re.IGNORECASE,
    )
    search_term = re.sub(r"\s+", " ", cleaned).strip(" ,.!?")
    if not search_term:
        open_url("https://www.youtube.com")
        return "YouTube im Browser gestartet, Chef."

    direct_url = find_first_youtube_url(search_term)
    if direct_url:
        open_url(direct_url)
        return f"Starte {search_term} direkt auf YouTube, Chef."

    search_url = f"https://www.youtube.com/results?search_query={urllib.parse.quote_plus(search_term)}"
    open_url(search_url)
    return f"YouTube-Suche für {search_term} geöffnet, Chef."


def handle_media_command(query: str) -> Optional[str]:
    """Erkennt und führt Sprachbefehle für Lautstärke, Wiedergabe und Streams aus."""
    if MUTE_PATTERN.search(query):
        return adjust_volume("mute")
    if VOLUME_UP_PATTERN.search(query):
        return adjust_volume("up")
    if VOLUME_DOWN_PATTERN.search(query):
        return adjust_volume("down")
    if NEXT_TRACK_PATTERN.search(query):
        return control_playback("next")
    if PREV_TRACK_PATTERN.search(query):
        return control_playback("prev")
    if PLAY_PAUSE_PATTERN.search(query):
        return control_playback("play_pause")

    if YOUTUBE_PLAY_PATTERN.search(query):
        return play_youtube_or_stream(query)
    return None
