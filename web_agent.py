"""Autonomer Webbrowser- & Hintergrund-Recherche-Agent (Playwright + Crawl4AI + DuckDuckGo)."""

from __future__ import annotations

from concurrent.futures import Future
import json
import os
import platform
import queue
import re
import threading
from typing import Any, Callable, Optional
import urllib.parse

from config import get_is_online
from ghost_cursor import (
    GHOST_OVERLAY,
    GhostCursorInterrupted,
    check_user_interrupt,
    ghost_swipe_scroll,
    smooth_move_cursor,
)
from tools import SAFETY_STOP_MESSAGE, is_financial_action_forbidden
from web_search import research_query

try:
    from playwright.sync_api import Browser, BrowserContext, Page, Playwright, sync_playwright
except ImportError:  # Graceful Fallback auf desktop_mouse_agent / open_url
    Browser = Any  # type: ignore[assignment,misc]
    BrowserContext = Any  # type: ignore[assignment,misc]
    Page = Any  # type: ignore[assignment,misc]
    Playwright = Any  # type: ignore[assignment,misc]
    sync_playwright = None  # type: ignore[assignment]


BROWSER_STEP_PROMPT = """Du bist der sichtbare Chromium-Webbrowser-Agent von Jarvis.
Du steuerst den echten sichtbaren Browser des Nutzers Schritt für Schritt anhand der sichtbaren Seiten-Elemente.

VERFÜGBARE AKTIONEN (antworte AUSSCHLIESSLICH mit genau einem JSON-Objekt ohne Markdown):
- {"status": "continue", "action": "goto", "url": "https://...", "reason": "..."}
- {"status": "continue", "action": "new_tab", "url": "https://...", "reason": "..."}
- {"status": "continue", "action": "switch_tab", "tab_index": 1, "direction": "next|prev", "reason": "..."}
- {"status": "continue", "action": "search_site", "site": "youtube|amazon|google", "query": "Suchbegriff", "reason": "..."}
- {"status": "continue", "action": "click_index", "index": 0, "target_label": "Text des Elements", "reason": "..."}
- {"status": "continue", "action": "click_first_result", "target_label": "Erstes Suchergebnis/Video", "reason": "..."}
- {"status": "continue", "action": "type_index", "index": 0, "text": "Eingabe", "submit": true, "target_label": "Eingabefeld", "reason": "..."}
- {"status": "continue", "action": "select_dropdown", "index": 0, "option_text": "Auswahl", "reason": "..."}
- {"status": "continue", "action": "upload_file", "index": 0, "file_path": "C:/pfad/datei.ext", "reason": "..."}
- {"status": "continue", "action": "find_on_page", "text": "Suchwort", "reason": "..."}
- {"status": "continue", "action": "video_control", "command": "fullscreen|play|pause|mute|forward|rewind", "reason": "..."}
- {"status": "continue", "action": "scroll", "direction": "down|up", "reason": "..."}
- {"status": "continue", "action": "back", "reason": "..."}
- {"status": "continue", "action": "forward", "reason": "..."}
- {"status": "done", "final_message": "Kurze deutsche Rückmeldung an Chef"}
- {"status": "safety_stop", "target_label": "Button-Text", "reason": "Kauf-/Bezahl-/Bestell-Button erkannt"}

STRIKTE SICHERHEITSREGEL:
- Du darfst Seiten öffnen, suchen, scrollen, Videos/Produktseiten öffnen oder bis zur Kasse navigieren.
- Du darfst NIEMALS einen Kauf-, Bestell-, Bezahl-, Überweisungs- oder Checkout-Button anklicken (z. B. "Kaufen", "Jetzt kaufen", "In den Einkaufswagen" nur wenn explizit gewünscht, NIE "Zur Kasse", "Bestellen", "Bezahlen", "Überweisen", "Buy now", "Checkout")! Setze dann SOFORT status="safety_stop"!
"""


def background_research(query: str, max_results: int = 4) -> str:
    """DEAKTIVIERT (100% VISIBLE DESKTOP & APP OPERATOR): Keine stillen Hintergrund-Suchen."""
    return ""


def _extract_json_obj(raw_text: str) -> Optional[dict[str, Any]]:
    if not raw_text:
        return None
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw_text.strip())
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        obj = json.loads(cleaned)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass
    match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    if not match:
        return None
    try:
        obj = json.loads(match.group(0))
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


_SNAPSHOT_JS = """() => {
    const items = [];
    const nodes = document.querySelectorAll(
        'a[href], button, input, textarea, [role="button"], ytd-video-renderer a#video-title'
    );
    let idx = 0;
    for (const el of nodes) {
        if (idx >= 35) break;
        const rect = el.getBoundingClientRect();
        if (rect.width < 12 || rect.height < 12) continue;
        if (rect.bottom < 0 || rect.top > window.innerHeight + 600) continue;
        const tag = el.tagName.toLowerCase();
        const type = el.getAttribute('type') || '';
        const text = (
            el.innerText || el.getAttribute('aria-label') || el.getAttribute('title') ||
            el.getAttribute('placeholder') || ''
        ).replace(/\\s+/g, ' ').trim().slice(0, 90);
        if (!text && tag !== 'input' && tag !== 'textarea') continue;
        el.setAttribute('data-jarvis-idx', String(idx));
        items.push({ index: idx, tag, type, text });
        idx++;
    }
    return {
        url: window.location.href,
        title: document.title || '',
        elements: items
    };
}"""


class PlaywrightBrowserAgent:
    """Führt sichtbare Browser-Aufgaben persistent in Chromium aus (eigener Thread für Playwright)."""

    def __init__(self) -> None:
        self._tasks: queue.Queue[Optional[tuple[Callable[[], Any], Future]]] = queue.Queue()
        self._worker: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._pw: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None

    def is_available(self) -> bool:
        return sync_playwright is not None

    def _ensure_worker(self) -> None:
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._worker = threading.Thread(target=self._worker_loop, daemon=True)
            self._worker.start()

    def _worker_loop(self) -> None:
        while True:
            item = self._tasks.get()
            if item is None:
                self._teardown_on_thread()
                break
            fn, fut = item
            try:
                fut.set_result(fn())
            except Exception as exc:
                fut.set_exception(exc)

    def _submit(self, fn: Callable[[], Any], timeout: float = 90.0) -> Any:
        self._ensure_worker()
        fut: Future = Future()
        self._tasks.put((fn, fut))
        return fut.result(timeout=timeout)

    def _ensure_page_on_thread(self) -> Page:
        if sync_playwright is None:
            raise RuntimeError("Playwright ist nicht installiert.")
        if self._pw is None:
            self._pw = sync_playwright().start()
        if self._browser is None or not self._browser.is_connected():
            launch_args = ["--start-maximized"]
            if platform.system() == "Linux":
                launch_args.append("--disable-dev-shm-usage")
                if os.getenv("WAYLAND_DISPLAY"):
                    launch_args.append("--ozone-platform-hint=auto")
            self._browser = self._pw.chromium.launch(
                headless=False,
                args=launch_args,
            )
            self._context = self._browser.new_context(
                no_viewport=True,
                locale="de-DE",
            )
            self._page = None
        if self._page is None or self._page.is_closed():
            assert self._context is not None
            self._page = self._context.new_page()
            self._page.set_default_timeout(12000)
        self._page.bring_to_front()
        return self._page

    def _teardown_on_thread(self) -> None:
        for obj in (self._page, self._context, self._browser, self._pw):
            if obj is not None:
                try:
                    if hasattr(obj, "close"):
                        obj.close()
                    elif hasattr(obj, "stop"):
                        obj.stop()
                except Exception:
                    pass
        self._page = None
        self._context = None
        self._browser = None
        self._pw = None

    def close(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            self._tasks.put(None)
            self._worker.join(timeout=3.0)

    def _site_search_url(self, site: str, query: str) -> str:
        encoded = urllib.parse.quote_plus(query.strip())
        low = site.strip().lower()
        if "youtube" in low:
            return f"https://www.youtube.com/results?search_query={encoded}" if encoded else "https://www.youtube.com"
        if "amazon" in low:
            return f"https://www.amazon.de/s?k={encoded}" if encoded else "https://www.amazon.de"
        return f"https://www.google.com/search?q={encoded}" if encoded else "https://www.google.de"

    def _ghost_glide_to_locator(self, page: Page, loc: Any, label: str) -> None:
        """Ermittelt die Bildschirmkoordinate eines Browser-DOM-Elements und fährt den Ghost-Cursor sichtbar dorthin."""
        try:
            box = loc.bounding_box(timeout=1500)
            if not box:
                return
            win_metrics = page.evaluate(
                "() => ({sx: window.screenX || 0, sy: window.screenY || 0, oh: window.outerHeight || 800, ih: window.innerHeight || 720})"
            )
            chrome_top = max(65, int(win_metrics.get("oh", 800)) - int(win_metrics.get("ih", 720)))
            sx = int(win_metrics.get("sx", 0) + box["x"] + box["width"] / 2.0)
            sy = int(win_metrics.get("sy", 0) + chrome_top + box["y"] + box["height"] / 2.0)
            smooth_move_cursor(sx, sy, duration=0.68, label=label, mode="move")
            GHOST_OVERLAY.add_ripple(sx, sy, label=label)
        except GhostCursorInterrupted:
            raise
        except Exception:
            pass

    def _dismiss_common_consent(self, page: Page) -> None:
        consent_texts = (
            "Alle akzeptieren",
            "Alles akzeptieren",
            "Akzeptieren",
            "Alle ablehnen",
            "Reject all",
            "Accept all",
        )
        for txt in consent_texts:
            try:
                loc = page.get_by_role("button", name=txt, exact=False).first
                if loc.is_visible(timeout=400):
                    self._ghost_glide_to_locator(page, loc, f"Klicke Cookie-Banner: {txt}")
                    loc.click(timeout=1000)
                    return
            except GhostCursorInterrupted:
                raise
            except Exception:
                continue

    def run_goal(
        self,
        goal: str,
        ask_text_fn: Callable[[str, str], str],
        on_status: Optional[Callable[[str], None]] = None,
        max_steps: int = 7,
    ) -> dict[str, str]:
        """Bedient Chromium sichtbar auf dem Desktop inklusive Ghost-Cursor-Overlay entsprechend dem Nutzerbefehl."""
        if not get_is_online():
            return {
                "status": "offline",
                "message": "Chef, der Online-Modus ist aktuell deaktiviert. Für Internet-Navigation schalten Sie bitte auf Online um.",
            }
        if not self.is_available():
            raise RuntimeError("Playwright nicht verfügbar")

        if on_status:
            on_status("Steuere Browser")

        def _execute() -> dict[str, str]:
            GHOST_OVERLAY.start(f"Browser-Steuerung: {goal[:34]}")
            try:
                page = self._ensure_page_on_thread()
                history: list[str] = []

                for step in range(1, max_steps + 1):
                    check_user_interrupt()
                    try:
                        snapshot = page.evaluate(_SNAPSHOT_JS)
                    except Exception:
                        snapshot = {"url": page.url, "title": "", "elements": []}

                    prompt = (
                        f"Aufgabe des Chefs: {goal}\n"
                        f"Aktuelle URL: {snapshot.get('url', '')}\n"
                        f"Seitentitel: {snapshot.get('title', '')}\n"
                        f"Schritt {step} von {max_steps}\n"
                        f"Bisherige Aktionen: {', '.join(history[-4:]) or 'Keine'}\n"
                        f"Sichtbare UI-Elemente:\n{json.dumps(snapshot.get('elements', [])[:30], ensure_ascii=False)}\n"
                        "Welcher nächste einzelne Browser-Schritt bringt die Aufgabe voran oder schließt sie ab?"
                    )
                    raw_resp = ask_text_fn(prompt, BROWSER_STEP_PROMPT)
                    cmd = _extract_json_obj(raw_resp)
                    if not cmd:
                        return {
                            "status": "error",
                            "message": "Konnte den nächsten Browser-Schritt nicht sauber bestimmen.",
                        }

                    status = str(cmd.get("status", "continue")).lower()
                    action = str(cmd.get("action", "none")).lower()
                    target_label = str(cmd.get("target_label", ""))
                    reason = str(cmd.get("reason", ""))

                    if is_financial_action_forbidden(target_label, reason, status):
                        return {"status": "safety_stop", "message": SAFETY_STOP_MESSAGE}

                    if status == "done" or action == "done":
                        return {
                            "status": "done",
                            "message": str(cmd.get("final_message") or "Chef, ich habe das im Browser ausgeführt."),
                        }

                    if action == "goto":
                        url = str(cmd.get("url") or "https://www.google.de").strip()
                        if not url.startswith(("http://", "https://")):
                            url = f"https://{url}"
                        GHOST_OVERLAY.set_status(f"Öffne Webseite: {url[:32]}", mode="search")
                        page.goto(url, wait_until="domcontentloaded")
                        self._dismiss_common_consent(page)
                        history.append(f"goto {url}")
                    elif action == "search_site":
                        site = str(cmd.get("site") or "google")
                        q = str(cmd.get("query") or "")
                        url = self._site_search_url(site, q)
                        GHOST_OVERLAY.set_status(f"Suche auf {site.capitalize()}: {q[:26]}", mode="search")
                        page.goto(url, wait_until="domcontentloaded")
                        self._dismiss_common_consent(page)
                        history.append(f"search_site {site}:{q}")
                        if "youtube" in site.lower() or "amazon" in site.lower():
                            if not re.search(r"\b(?:klick|öffne\s+das\s+erste|spiel|wähl)\b", goal, flags=re.IGNORECASE):
                                return {
                                    "status": "done",
                                    "message": f"Chef, ich habe auf {site.capitalize()} nach {q} gesucht und die Ergebnisse geöffnet.",
                                }
                    elif action == "click_index":
                        idx = int(cmd.get("index", 0))
                        loc = page.locator(f'[data-jarvis-idx="{idx}"]').first
                        elem_text = (loc.inner_text(timeout=1200) or target_label).strip()
                        if is_financial_action_forbidden(elem_text, reason, status):
                            return {"status": "safety_stop", "message": SAFETY_STOP_MESSAGE}
                        self._ghost_glide_to_locator(page, loc, f"Klicke: {elem_text[:30] or target_label[:30]}")
                        loc.click(timeout=5000)
                        page.wait_for_timeout(800)
                        history.append(f"click [{idx}] {elem_text[:30]}")
                    elif action == "click_first_result":
                        if "youtube.com" in page.url:
                            vid = page.locator("ytd-video-renderer a#video-title").first
                            self._ghost_glide_to_locator(page, vid, "Klicke erstes Video...")
                            vid.click(timeout=5000)
                        elif "amazon" in page.url:
                            prod = page.locator('[data-component-type="s-search-result"] h2 a').first
                            self._ghost_glide_to_locator(page, prod, "Klicke erstes Produkt...")
                            prod.click(timeout=5000)
                        else:
                            link = page.locator("#search a h3, main a").first
                            self._ghost_glide_to_locator(page, link, "Klicke erstes Suchergebnis...")
                            link.click(timeout=5000)
                        page.wait_for_timeout(1000)
                        history.append("click_first_result")
                        return {
                            "status": "done",
                            "message": "Chef, ich habe den ersten Treffer direkt geöffnet.",
                        }
                    elif action == "type_index":
                        idx = int(cmd.get("index", 0))
                        txt = str(cmd.get("text") or "")
                        submit = bool(cmd.get("submit", True))
                        loc = page.locator(f'[data-jarvis-idx="{idx}"]').first
                        self._ghost_glide_to_locator(page, loc, f"Tippe: {txt[:26]}")
                        loc.click(timeout=3000)
                        loc.fill(txt, timeout=3000)
                        if submit:
                            loc.press("Enter")
                        page.wait_for_timeout(900)
                        history.append(f"type [{idx}] {txt[:25]}")
                    elif action == "scroll":
                        direction = str(cmd.get("direction") or "down").lower()
                        delta = -650 if direction == "up" else 650
                        ghost_swipe_scroll(
                            amount=450 if direction == "up" else -450,
                            direction=direction,
                            label=f"Wische im Browser ({direction})...",
                        )
                        page.mouse.wheel(0, delta)
                        page.wait_for_timeout(450)
                        history.append(f"scroll {direction}")
                    elif action == "back":
                        GHOST_OVERLAY.set_status("Gehe eine Seite zurück...", mode="move")
                        page.go_back(wait_until="domcontentloaded")
                        history.append("back")
                    elif action == "forward":
                        GHOST_OVERLAY.set_status("Gehe eine Seite vor...", mode="move")
                        page.go_forward(wait_until="domcontentloaded")
                        history.append("forward")
                    elif action == "new_tab":
                        assert self._context is not None
                        new_p = self._context.new_page()
                        new_p.set_default_timeout(12000)
                        self._page = new_p
                        target_u = str(cmd.get("url") or "").strip()
                        if target_u:
                            if not target_u.startswith(("http://", "https://")):
                                target_u = f"https://{target_u}"
                            new_p.goto(target_u, wait_until="domcontentloaded")
                            self._dismiss_common_consent(new_p)
                        history.append(f"new_tab {target_u}".strip())
                    elif action == "switch_tab":
                        assert self._context is not None
                        pages = self._context.pages
                        if pages:
                            t_idx = cmd.get("tab_index")
                            if t_idx is not None:
                                pick = max(0, min(len(pages) - 1, int(t_idx) - 1))
                            else:
                                cur_i = pages.index(page) if page in pages else 0
                                delta_i = -1 if str(cmd.get("direction") or "next").lower() in ("prev", "previous") else 1
                                pick = (cur_i + delta_i) % len(pages)
                            self._page = pages[pick]
                            self._page.bring_to_front()
                            page = self._page
                        history.append("switch_tab")
                    elif action == "select_dropdown":
                        idx = int(cmd.get("index", 0))
                        opt = str(cmd.get("option_text") or cmd.get("text") or "").strip()
                        loc = page.locator(f'[data-jarvis-idx="{idx}"]').first
                        self._ghost_glide_to_locator(page, loc, f"Dropdown: {opt[:22]}")
                        try:
                            loc.select_option(label=opt)
                        except Exception:
                            loc.click(timeout=3000)
                            if opt:
                                page.keyboard.type(opt)
                                page.keyboard.press("Enter")
                        history.append(f"dropdown [{idx}] {opt}")
                    elif action == "upload_file":
                        idx = int(cmd.get("index", 0))
                        f_path = str(cmd.get("file_path") or "").strip()
                        loc = page.locator(f'[data-jarvis-idx="{idx}"]').first
                        self._ghost_glide_to_locator(page, loc, f"Upload: {f_path[-24:]}")
                        if f_path:
                            try:
                                loc.set_input_files(f_path, timeout=4000)
                            except Exception:
                                with page.expect_file_chooser(timeout=4000) as fc_info:
                                    loc.click(timeout=3000)
                                fc_info.value.set_files(f_path)
                        history.append(f"upload [{idx}]")
                    elif action == "find_on_page":
                        f_txt = str(cmd.get("text") or cmd.get("search_text") or "").strip()
                        GHOST_OVERLAY.set_status(f"Seitensuche: {f_txt[:24]}", mode="search")
                        page.keyboard.press("Control+f")
                        if f_txt:
                            page.keyboard.type(f_txt)
                            page.keyboard.press("Enter")
                        history.append(f"find {f_txt[:20]}")
                    elif action == "video_control":
                        v_cmd = str(cmd.get("command") or "toggle_pause").strip().lower()
                        key_map = {"fullscreen": "f", "vollbild": "f", "play": "k", "pause": "k", "toggle_pause": "k", "mute": "m", "forward": "l", "rewind": "j"}
                        v_key = key_map.get(v_cmd, "k")
                        page.keyboard.press(v_key)
                        history.append(f"video {v_cmd}")
                    else:
                        return {
                            "status": "done",
                            "message": str(cmd.get("final_message") or "Browser-Aufgabe abgeschlossen, Chef."),
                        }

                return {"status": "done", "message": "Chef, der Browser steht mit dem Ergebnis bereit."}
            except GhostCursorInterrupted as exc:
                return {"status": "interrupted", "message": str(exc)}
            finally:
                GHOST_OVERLAY.stop()

        return self._submit(_execute)


class VisibleDesktopBrowser:
    """Sichtbare Desktop-Webbrowser-Komplettsteuerung (Tabs, Navigation, Formulare, File-Upload, Videoplayer)."""

    def new_tab(self, url: str = "") -> str:
        from agent_hand import browser_new_tab
        return browser_new_tab(url=url)

    def switch_tab(self, tab_index: Optional[int] = None, direction: str = "next", x: Optional[int] = None, y: Optional[int] = None) -> str:
        from agent_hand import browser_switch_tab
        return browser_switch_tab(tab_index=tab_index, direction=direction, x=x, y=y)

    def back(self) -> str:
        from agent_hand import browser_back
        return browser_back()

    def forward(self) -> str:
        from agent_hand import browser_forward
        return browser_forward()

    def focus_address_bar(self, url_or_query: str = "", press_enter: bool = True) -> str:
        from agent_hand import browser_address_bar
        return browser_address_bar(url_or_query=url_or_query, press_enter=press_enter)

    def find_on_page(self, search_text: str = "", next_match: bool = False) -> str:
        from agent_hand import browser_find_on_page
        return browser_find_on_page(search_text=search_text, next_match=next_match)

    def select_dropdown(self, x: Optional[int] = None, y: Optional[int] = None, option_text: str = "", steps_down: int = 0) -> str:
        from agent_hand import browser_select_dropdown
        return browser_select_dropdown(x=x, y=y, option_text=option_text, steps_down=steps_down)

    def upload_file(self, file_path: str, button_x: Optional[int] = None, button_y: Optional[int] = None) -> str:
        from agent_hand import browser_upload_file
        return browser_upload_file(file_path=file_path, button_x=button_x, button_y=button_y)

    def video_control(self, command: str = "toggle_pause", x: Optional[int] = None, y: Optional[int] = None) -> str:
        from agent_hand import browser_video_control
        return browser_video_control(command=command, x=x, y=y)


VISIBLE_DESKTOP_BROWSER = VisibleDesktopBrowser()
