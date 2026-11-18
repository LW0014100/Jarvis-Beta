"""
DEAKTIVIERT (AUFGABE 1 – 100% VISIBLE DESKTOP & APP OPERATOR):
Keine stillen Hintergrund-Suchen via DuckDuckGo, Crawl4AI, urllib oder BeautifulSoup4 mehr!
Alle Web-Recherchen, Fakten, Kurse und Wetterabfragen müssen sichtbar über den echten
Desktop-Webbrowser (`visual_browser_search_and_read`) ausgeführt werden.
Die Funktionssignaturen bleiben ausschließlich für Import-Kompatibilität erhalten.
"""

from __future__ import annotations

from typing import Optional


def search_web_ddg(query: str, max_results: int = 5) -> list[dict[str, str]]:
    """Deaktiviert: Keine stillen DuckDuckGo-Hintergrundsuchen."""
    return []


def fetch_page_excerpt(url: str, max_chars: int = 1200, timeout: float = 4.0) -> str:
    """Deaktiviert: Kein stilles Hintergrund-Scraping via Crawl4AI/BeautifulSoup4."""
    return ""


def find_first_youtube_url(query: str) -> Optional[str]:
    """Deaktiviert: YouTube muss sichtbar im Desktop-Browser gesucht und geöffnet werden."""
    return None


def research_query(query: str, max_results: int = 4) -> str:
    """Deaktiviert: Keine stille Hintergrund-Recherche ohne sichtbaren Desktop-Browser."""
    return ""
