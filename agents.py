"""Multi-Agenten-Router und spezialisierte Agenten (GUI/Maus, E-Mail, System)."""

from __future__ import annotations

import json
import re
import time
from typing import Callable, Optional

from alarm import AlarmManager
from alarm import AlarmManager
from brain import BrainResponse, JarvisBrain, WORKBENCH_VISION_SYSTEM_PROMPT
from config import detect_online_mode_command, get_is_online, set_online_mode
from ghost_cursor import GHOST_OVERLAY, GhostCursorInterrupted, check_user_interrupt
from media_control import handle_media_command
from memory import LongTermMemory
from smart_home import SmartHomeController
from vision import (
    CameraVision,
    is_workbench_query,
    locate_ui_element_visually,
    should_capture_image,
    should_capture_screen,
    should_capture_webcam,
)
from web_agent import PlaywrightBrowserAgent, background_research
from tools import (
    SAFETY_STOP_MESSAGE,
    build_status_briefing,
    capture_screen_jpeg,
    create_project_folder,
    execute_gui_action,
    fetch_unread_emails,
    get_system_status,
    is_financial_action_forbidden,
    move_or_rename_path,
    open_app,
    open_url,
    save_email_draft,
    send_email_smtp,
)


ONLINE_ONLY_QUERY_PATTERN = re.compile(
    r"\b(?:wetter|vorhersage|regnet|aktie\w*|kurs\w*|goldpreis|gold|silberpreis|krypto\w*|bitcoin|btc|ethereum|nvidia|"
    r"depot|börse|recherchier\w*|crawl\w*|such\w*\s+(?:mal\s+)?(?:im\s+netz|im\s+internet)|"
    r"finde\s+infos|news\s+zu|nachrichten\s+zu)\b",
    flags=re.IGNORECASE,
)

OFFLINE_ONLINE_ONLY_REPLY = (
    "Chef, der Online-Modus ist aktuell deaktiviert. "
    "Für Websuche, Online-Wetter, Aktienabfragen oder Webbrowser-Aufgaben schalten Sie mich bitte wieder auf Online um — "
    "lokal arbeite ich mit Ollama, Apps, Maus, Kamera und Wecker normal weiter."
)


def _offline_online_only_response() -> BrainResponse:
    return BrainResponse(
        text=OFFLINE_ONLINE_ONLY_REPLY,
        provider="Offline-Modus",
        model="Lokal",
    )


ONLINE_ONLY_QUERY_PATTERN = re.compile(
    r"\b(?:wetter|vorhersage|regnet|aktie\w*|kurs\w*|goldpreis|gold|silberpreis|krypto\w*|bitcoin|btc|ethereum|nvidia|"
    r"depot|börse|recherchier\w*|crawl\w*|such\w*\s+(?:mal\s+)?(?:im\s+netz|im\s+internet)|"
    r"finde\s+infos|news\s+zu|nachrichten\s+zu)\b",
    flags=re.IGNORECASE,
)

OFFLINE_ONLINE_ONLY_REPLY = (
    "Chef, der Online-Modus ist aktuell deaktiviert. "
    "Für Websuche, Online-Wetter, Aktienabfragen oder Webbrowser-Aufgaben schalten Sie mich bitte wieder auf Online um — "
    "lokal arbeite ich mit Ollama, Apps, Maus, Kamera und Wecker normal weiter."
)


def _offline_online_only_response() -> BrainResponse:
    return BrainResponse(
        text=OFFLINE_ONLINE_ONLY_REPLY,
        provider="Offline-Modus",
        model="Lokal",
    )


REMEMBER_PATTERN = re.compile(
    r"\b(?:bitte\s+)?merk(?:e)?\s+dir(?:\s+bitte)?(?:,?\s*dass)?\s+(.+)$",
    flags=re.IGNORECASE,
)
RECALL_PATTERN = re.compile(
    r"(?:was\s+weißt\s+du\s+(?:noch\s+)?über|woran\s+erinnerst\s+du\s+dich(?:\s+bei|\s+über)?)\s*(.*)$",
    flags=re.IGNORECASE,
)
STATUS_BRIEFING_PATTERN = re.compile(
    r"\b(?:statusbericht|morgen[\s-]*briefing|tagesbericht)\b",
    flags=re.IGNORECASE,
)
SMART_HOME_ON_PATTERN = re.compile(
    r"\b(?:schalte|mach|dreh|stell)\b.*\b(?:ein|an)\b",
    flags=re.IGNORECASE,
)
SMART_HOME_OFF_PATTERN = re.compile(
    r"\b(?:schalte|mach|dreh|stell)\b.*\b(?:aus)\b",
    flags=re.IGNORECASE,
)
ACTION_INTENT_HINT_PATTERN = re.compile(
    r"\b(?:öffne|starte|schließe|beende|browser|internet|website|webseite|suche\s+nach|google|youtube|"
    r"klick|scroll|navigier|maus|bildschirm|fenster|programm|app|ordner|datei|verschieb|umbenenn|"
    r"e-?mail|mail|posteingang|nachrichten|entwurf|verfass|"
    r"systemstatus|speicher|festplatte|ram|cpu|temperatur|auslastung|"
    r"schalte|steckdose|smart\s*home|"
    r"aktie|kurs|depot|broker|kauf|bestell|bezahl|schnitt)\b",
    flags=re.IGNORECASE,
)
EMAIL_SEND_CONFIRM_PATTERN = re.compile(
    r"\b(?:abschicken|ja[\s,]+abschicken|mail\s+senden|entwurf\s+abschicken|jetzt\s+senden)\b",
    flags=re.IGNORECASE,
)
EMAIL_CANCEL_PATTERN = re.compile(
    r"\b(?:entwurf\s+verwerfen|nicht\s+abschicken|mail\s+abbrechen)\b",
    flags=re.IGNORECASE,
)
WEB_RESEARCH_PATTERN = re.compile(
    r"\b(?:recherchier\w*|such(?:e)?\s+(?:mal\s+)?(?:im\s+netz|im\s+internet|infos?)\b|"
    r"finde\s+infos?|aktuelle[rn]?\s+(?:nachrichten|news|stand|infos?|preis\w*|kurs\w*)|"
    r"goldpreis|gold|silberpreis|ölpreis|xauusd|bitcoin|btc|ethereum|krypto\w*|nvidia|aktie\w*|kurs\w*|"
    r"was\s+(?:kostet|ist|sind|bedeutet)|preis\s+(?:von|für)|"
    r"wie\s+(?:sieht\s+es|steht|wird\s+das\s+wetter|ist\s+das\s+wetter|hoch|viel)|"
    r"wetter\b|wer\s+(?:ist|hat|war|gewann)|"
    r"wann\s+(?:ist|startet|erscheint)|nachrichten\s+zu|news\s+zu|internet\s+nachsehen)\b",
    flags=re.IGNORECASE,
)
VISIBLE_BROWSER_PATTERN = re.compile(
    r"\b(?:youtube|amazon|google|webseite|website|browser|surfe|im\s+netz\s+aufmachen|"
    r"erstes?\s+(?:video|ergebnis|treffer)|klick\s+auf\s+das\s+erste)\b",
    flags=re.IGNORECASE,
)
ACTION_INTENT_HINT_PATTERN = re.compile(
    r"\b(?:öffne|starte|mach\s+\w+\s+auf|schließe|beende|browser|internet|website|webseite|suche?\s+(?:nach|mal|mir)|google|youtube|amazon|"
    r"klick|doppelklick|rechtsklick|drag|ziehe|verschieb|scroll|navigier|surfe|maus|bildschirm|fenster|programm|app|ordner|datei|umbenenn|"
    r"hotkey|tastenkombination|shortcut|arccraft|filmcraft|launcher|steam|discord|spotify|explorer|einstellungen|"
    r"e-?mail|mail|posteingang|nachrichten|entwurf|verfass|abschicken|senden|"
    r"systemstatus|speicher|festplatte|ram|cpu|temperatur|auslastung|"
    r"schalte|steckdose|smart\s*home|relais|"
    r"lauter|leiser|ton\s+aus|stumm|pause|weiter|nächste|vorherige|musik|abspielen|"
    r"recherchier\w*|finde\s+infos|news|wetter\s+in|was\s+kostet|preis\s+von|"
    r"aktie|kurs|depot|broker|kauf|bestell|bezahl|überweis\w*|schnitt|vscode|vs\s*code)\b",
    flags=re.IGNORECASE,
)


ROUTER_SYSTEM_PROMPT = """Du bist der Aufgaben-Router für Jarvis (100% VISIBLE DESKTOP & APP OPERATOR). Entscheide anhand der Nutzeranfrage, welches sichtbare Desktop-Werkzeug nötig ist.
WICHTIG: Jarvis sieht den Desktop live und kann JEDE Windows-App, jedes Spiel, jeden Launcher (z. B. ArcCraft, FilmCraft, Steam, Discord, Spotify, Explorer, Browser, Einstellungen) direkt per Maus (`click`, `double_click`, `right_click`, `drag_and_drop`, `click_and_type`, `scroll`, `hotkey`) steuern. Wähle bei jeder gewünschten App-/Desktop-Bedienung zwingend `gui_agent` oder `browser_agent` – behaupte NIEMALS, eine App nicht steuern zu können!
Antworte AUSSCHLIESSLICH mit einem gültigen JSON-Objekt ohne Markdown-Block:

Mögliche Routen:
1. {"agent": "chat"} -> Ausschließlich reine Konversation/Smalltalk ohne Live-Recherche und ohne App-/Desktop-Bezug.
2. {"agent": "web_search_agent", "query": "Präzise Suchanfrage"} -> IMMER über den sichtbaren Desktop-Webbrowser (`visual_browser_search_and_read`) für aktuelle Infos, Preise, Kurse, Wetter, Fakten ("Was kostet...", "Wie ist das Wetter...", "Such nach..."). NIEMALS stille Hintergrund-Suche ohne Browserfenster!
3. {"agent": "browser_agent", "goal": "Sichtbare Browser-Aufgabe"} -> Wenn der Nutzer eine Webseite im sichtbaren Desktop-Browser öffnen, suchen, scrollen oder anklicken lassen will (z. B. "Öffne YouTube und such nach...", "Mach Amazon auf...", "Klick auf das erste Video").
4. {"agent": "media_agent"} -> Lautstärke ändern (lauter/leiser/stumm), Wiedergabe steuern (Pause/Weiter/Nächster).
5. {"agent": "gui_agent", "goal": "Kurze Beschreibung der Desktop-/App-Aufgabe"} -> Sichtbare Steuerung lokaler Programme & Apps (Rechner, Notizen, Spotify, Einstellungen, Explorer, Schnittprogramm, Spiele) über Desktop/Taskleiste/Startmenü mit anschließendem Bedienen IN der App (`visual_desktop_app_operator`).
6. {"agent": "email_agent", "action": "read_unread" oder "draft", "to": "Empfänger oder leer", "subject": "Betreff oder leer", "body_hint": "Inhaltshinweis"} -> E-Mails abrufen/zusammenfassen oder Entwurf vorbereiten.
7. {"agent": "system_agent", "action": "open_app" oder "open_url" oder "create_folder" oder "move_file" oder "system_status", "target": "Programmname oder URL/Suchanfrage", "folder_name": "Ordnername", "video_template": true/false, "src": "Quellpfad", "dst": "Zielpfad"} -> Programme/Apps öffnen (Browser, Terminal, Rechner, VS Code, Schnittprogramm), Ordner anlegen, Dateien verschieben/umbenennen, Systemstatus prüfen.
8. {"agent": "smart_home_agent", "turn_on": true/false, "device": "Gerätename"} -> Lokale Smart-Home-Geräte / Steckdosen / Pi-GPIO-Relais ein- oder ausschalten.
"""


GUI_AGENT_SYSTEM_PROMPT = """Du bist der autonome Desktop-Maus- und Tastatur-Agent ('desktop_mouse_agent') von Jarvis mit sichtbarem Ghost-Cursor.
Du siehst einen Screenshot des Bildschirms mit einem eingeblendeten Koordinaten-Raster (X-Werte oben, Y-Werte links, 50px-Skala an der Taskleiste unten). Lies die exakten X/Y-Koordinaten direkt an diesen Rasterlinien ab!
Verfügbare Aktionen: click, double_click, right_click, drag (mit x/y -> end_x/end_y), move, scroll, swipe (mit direction="down|up|left|right"), open_app, open_url, type, press, hotkey, wait, none.

WICHTIG FÜR SICHTBARE NAVIGATION (Taskleiste & Apps):
- Gib in "target_label" IMMER den exakten sichtbaren Namen des Icons oder Menüpunkts an (z. B. "Xbox", "Meine Bibliothek", "Game Pass", "Store", "Suche"), damit der Ghost-Cursor das Label anzeigt und die Koordinaten präzise ansteuert.
- Wenn du in einer App nach unten/oben/seitwärts blättern sollst, nutze action="swipe" oder action="scroll" mit direction="down"|"up"|"left"|"right".

STRIKTE SICHERHEITSREGEL FÜR FINANZEN, KÄUFE & ÜBERWEISUNGEN:
- Du darfst Aktienkurse prüfen, den Broker öffnen und eine Maske bis zum letzten Schritt ausfüllen.
- Du darfst NIEMALS einen finalen Kauf-, Order-, Bestell-, Bezahl-, Überweisungs- oder Bestätigungs-Button anklicken (z. B. "Kaufen", "Jetzt kaufen", "Order absenden", "Zahlungspflichtig", "Bestätigen", "Bestellen", "Bezahlen", "Überweisen", "Zur Kasse", "Buy now", "Place order", "Confirm", "Checkout").
- Wenn ein solcher finaler Kauf-/Bezahl-/Bestell-/Überweisungs-Button auf dem Bildschirm sichtbar ist oder als nächstes geklickt werden müsste, setze SOFORT status="safety_stop" und screen_contains_purchase_button=true!

Antworte AUSSCHLIESSLICH mit genau einem JSON-Objekt ohne Markdown:
{
  "status": "continue" | "done" | "safety_stop" | "blocked",
  "action": "click" | "double_click" | "right_click" | "drag" | "move" | "scroll" | "swipe" | "open_app" | "open_url" | "type" | "press" | "hotkey" | "wait" | "none",
  "x": 0,
  "y": 0,
  "end_x": 0,
  "end_y": 0,
  "direction": "down",
  "text": "",
  "key": "",
  "keys": [],
  "amount": -420,
  "target_label": "Beschriftung des angeklickten UI-Elements",
  "screen_contains_purchase_button": false,
  "reason": "Kurze Begründung",
  "final_message": "Kurze deutsche Rückmeldung an Chef (bei done/blocked)"
}"""


def _extract_json(raw_text: str) -> Optional[dict]:
    if not raw_text:
        return None
    cleaned = raw_text.strip()
    cleaned = re.sub(r"```(?:json)?", "", cleaned, flags=re.IGNORECASE).strip()

    decoder = json.JSONDecoder()
    for idx, ch in enumerate(cleaned):
        if ch == "{":
            try:
                obj, _ = decoder.raw_decode(cleaned[idx:])
                if isinstance(obj, dict):
                    return obj
            except Exception:
                pass

    match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    if not match:
        return None
    frag = match.group(0)
    for cand in (frag, re.sub(r",\s*([}\]])", r"\1", frag)):
        try:
            parsed = json.loads(cand)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            continue
    return None


def _safe_coord_int(val: Any, default: int = 0) -> int:
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


def _extract_app_target(query: str) -> str:
    """Extrahiert den reinen Programmnamen aus Befehlen wie 'Öffne bitte die Xbox App'."""
    cleaned = query.strip()
    m_open = re.search(r"\b(?:öffne|starte)\s+(.+)$", cleaned, flags=re.IGNORECASE)
    m_auf = re.search(r"\bmach(?:e)?\s+(.+?)\s+auf\b", cleaned, flags=re.IGNORECASE)
    raw = (m_open.group(1) if m_open else (m_auf.group(1) if m_auf else cleaned)).strip()
    raw = re.sub(
        r"^(?:bitte|mal|mir|doch|jetzt|einfach|den|die|das|der|eine?n?)\s+",
        "",
        raw,
        flags=re.IGNORECASE,
    )
    raw = re.sub(
        r"^(?:bitte|mal|mir|doch|jetzt|einfach|den|die|das|der|eine?n?)\s+",
        "",
        raw,
        flags=re.IGNORECASE,
    )
    raw = re.sub(r"\s+(?:app|programm|anwendung|bitte)$", "", raw, flags=re.IGNORECASE)
    return raw.strip(" ?!.,\"'")


def _extract_app_target(query: str) -> str:
    """Extrahiert den reinen Programmnamen aus Befehlen wie 'Öffne bitte die Xbox App'."""
    cleaned = query.strip()
    m_open = re.search(r"\b(?:öffne|starte)\s+(.+)$", cleaned, flags=re.IGNORECASE)
    m_auf = re.search(r"\bmach(?:e)?\s+(.+?)\s+auf\b", cleaned, flags=re.IGNORECASE)
    raw = (m_open.group(1) if m_open else (m_auf.group(1) if m_auf else cleaned)).strip()
    raw = re.sub(
        r"^(?:bitte|mal|mir|doch|jetzt|einfach|den|die|das|der|eine?n?)\s+",
        "",
        raw,
        flags=re.IGNORECASE,
    )
    raw = re.sub(
        r"^(?:bitte|mal|mir|doch|jetzt|einfach|den|die|das|der|eine?n?)\s+",
        "",
        raw,
        flags=re.IGNORECASE,
    )
    raw = re.sub(r"\s+(?:app|programm|anwendung|bitte)$", "", raw, flags=re.IGNORECASE)
    return raw.strip(" ?!.,\"'")


def _heuristic_route(query: str) -> dict:
    q = query.lower()
    if any(w in q for w in ("e-mail", "email", "mail", "posteingang")):
        if any(w in q for w in ("entwurf", "schreib", "verfass", "antworte", "sende", "schick")):
            return {"agent": "email_agent", "action": "draft", "to": "", "subject": "", "body_hint": query}
        return {"agent": "email_agent", "action": "read_unread"}

    if any(w in q for w in ("systemstatus", "speicherplatz", "festplatte", "temperatur", "wie geht es dem pi")):
        return {"agent": "system_agent", "action": "system_status"}

    if any(w in q for w in ("projektordner", "ordner erstellen", "ordner anlegen", "schnittordner")):
        return {
            "agent": "system_agent",
            "action": "create_folder",
            "folder_name": "Neues_Projekt",
            "video_template": any(v in q for v in ("schnitt", "video", "davinci", "kdenlive")),
        }

    if any(w in q for w in ("lauter", "leiser", "ton aus", "lautlos", "stumm", "wiedergabe")):
        return {"agent": "media_agent"}

    if WEB_RESEARCH_PATTERN.search(q) and not any(
        w in q for w in ("mach", "öffne", "im browser", "klick", "scroll", "bildschirm", "screenshot", "broker", "amazon", "youtube")
    ):
        # 100% VISIBLE OPERATOR: Auch Web-Recherchen laufen sichtbar im echten Desktop-Webbrowser!
        return {"agent": "browser_agent", "goal": query}

    if VISIBLE_BROWSER_PATTERN.search(q) and any(
        w in q for w in ("youtube", "amazon", "google", "webseite", "website", "such", "klick", "scroll", "gehe", "surfe", "spiel")
    ):
        return {"agent": "browser_agent", "goal": query}

    # Mehrstufige Desktop-/App- & Fenster-Navigation (z. B. "Geh auf ArcCraft Launcher auf meinem Desktop, Doppelklick und öffne FilmCraft")
    if any(w in q for w in ("desktop", "arbeitsfläche", "doppelklick")) or re.search(
        r"\b(?:öffne|starte|mach|geh\w*\s+auf)\b.*\b(?:und\s+)?(?:geh\w*\s+(?:zu|auf|in)|öffne|starte|klick\w*|wisch\w*|scroll\w*|navigier\w*|such\w*\s+in|rechn\w*|tipp\w*|schreib\w*|spiel\w*|erstell\w*|darin)\b",
        q,
    ):
        return {"agent": "gui_agent", "goal": query}

    if any(
        w in q
        for w in (
            "aktie",
            "kurs",
            "depot",
            "broker",
            "klick",
            "doppelklick",
            "rechtsklick",
            "drag",
            "ziehe",
            "scroll",
            "wisch",
            "swipe",
            "hotkey",
            "taskleiste",
            "navigier",
            "maus",
            "ghost-cursor",
            "schnittprogramm",
            "kaufe",
            "bestelle",
            "überweis",
            "taschenrechner",
            "rechner",
            "rechne ",
            "berechne ",
            "notiz",
            "notepad",
            "spotify",
            "discord",
            "steam",
            "arccraft",
            "filmcraft",
            "launcher",
            "einstellungen",
            "explorer",
        )
    ):
        return {"agent": "gui_agent", "goal": query}

    if re.search(r"\b(?:öffne|starte)\b|\bmach(?:e)?\s+.+\s+auf\b", q):
        target = _extract_app_target(query)
        if target:
            return {"agent": "system_agent", "action": "open_app", "target": target}

    if SMART_HOME_ON_PATTERN.search(q):
        return {"agent": "smart_home_agent", "turn_on": True, "device": query}
    if SMART_HOME_OFF_PATTERN.search(q):
        return {"agent": "smart_home_agent", "turn_on": False, "device": query}

    return {"agent": "chat"}


class AgentRouter:
    """Entscheidet selbstständig zwischen Chat, Web-Recherche, Medien, GUI-Maus, E-Mail, System, Gedächtnis und Smart-Home."""

    def __init__(
        self,
        brain: JarvisBrain,
        memory: Optional[LongTermMemory] = None,
        smart_home: Optional[SmartHomeController] = None,
        alarm_manager: Optional[AlarmManager] = None,
        vision: Optional[CameraVision] = None,
    ) -> None:
        self.brain = brain
        self.memory = memory or LongTermMemory()
        self.smart_home = smart_home or SmartHomeController()
        self.alarm_manager = alarm_manager
        self.vision = vision
        self.browser_agent = PlaywrightBrowserAgent()
        self._pending_email: Optional[dict[str, str]] = None

    def close(self) -> None:
        """Schließt verwaltete Browser-Sessions sauber beim Beenden."""
        self.browser_agent.close()

    def _try_deterministic_shortcuts(
        self,
        user_text: str,
        on_status: Optional[Callable[[str], None]] = None,
    ) -> Optional[BrainResponse]:
        # 0. Pflicht-Rückfrage / Bestätigung für fehlende App-Installation (`pending_install_task`)
        try:
            from app_installer_agent import APP_INSTALLER_AGENT

            if APP_INSTALLER_AGENT.get_pending_proposal() is not None:
                if APP_INSTALLER_AGENT.is_confirm(user_text):
                    inst_res = APP_INSTALLER_AGENT.execute_pending_install(
                        brain=self.brain,
                        on_status=on_status,
                    )
                    return BrainResponse(
                        text=str(inst_res.get("message") or "Installation verarbeitet."),
                        provider="App-Installer-Agent",
                        model="Browser+Setup",
                    )
                if APP_INSTALLER_AGENT.is_cancel(user_text):
                    APP_INSTALLER_AGENT.clear_pending_proposal()
                    return BrainResponse(
                        text="Verstanden, Chef. Ich habe den App-Download und die Installation abgebrochen.",
                        provider="App-Installer-Agent",
                        model="Safety-Guard",
                    )
        except Exception as inst_exc:
            print(f"[AppInstaller Bestätigungs-Hinweis] {inst_exc}")

        # 0b. MODUL 4: Sprach-Bestätigung / Ablehnung für aktiven proaktiven Smart-Suggestion-Vorschlag
        try:
            from executive_assistant import EXECUTIVE_ASSISTANT

            if EXECUTIVE_ASSISTANT.suggestions.get_current_suggestion() is not None:
                if EXECUTIVE_ASSISTANT.is_confirm_reply(user_text):
                    sug_res = EXECUTIVE_ASSISTANT.suggestions.confirm_suggestion(
                        assistant=EXECUTIVE_ASSISTANT,
                        brain=self.brain,
                        on_status=on_status,
                    )
                    return BrainResponse(
                        text=str(sug_res.get("spoken") or "Vorschlag ausgeführt, Chef."),
                        provider="Executive-Assistant",
                        model="Smart-Suggestion",
                    )
                if EXECUTIVE_ASSISTANT.is_dismiss_reply(user_text):
                    EXECUTIVE_ASSISTANT.suggestions.dismiss_suggestion()
                    return BrainResponse(
                        text="Verstanden, Chef. Ich blende den Vorschlag wieder aus.",
                        provider="Executive-Assistant",
                        model="Smart-Suggestion",
                    )
        except Exception as sug_exc:
            print(f"[SmartSuggestion Sprach-Hinweis] {sug_exc}")

        if self._pending_email is not None:
            if EMAIL_SEND_CONFIRM_PATTERN.search(user_text):
                pending = self._pending_email
                self._pending_email = None
                msg = send_email_smtp(pending["to"], pending["subject"], pending["body"])
                return BrainResponse(text=msg, provider="E-Mail-Agent", model="SMTP")
            if EMAIL_CANCEL_PATTERN.search(user_text):
                self._pending_email = None
                return BrainResponse(
                    text="Verstanden, Chef. Ich habe den vorbereiteten E-Mail-Entwurf verworfen.",
                    provider="E-Mail-Agent",
                    model="Lokal",
                )

        if self.alarm_manager is not None and re.search(
            r"\b(?:wecker|weck\s+mich|wecken)\b", user_text, flags=re.IGNORECASE
        ):
            msg = self.alarm_manager.handle_voice_command(user_text)
            return BrainResponse(text=msg, provider="Wecker", model="alarms.json")

        rem_match = REMEMBER_PATTERN.search(user_text.strip())
        if rem_match:
            fact = rem_match.group(1).strip().rstrip(".")
            saved = self.memory.remember(fact, category="fact")
            if saved:
                return BrainResponse(
                    text=f"Sehr wohl, Chef. Ich habe mir gemerkt, dass {saved}.",
                    provider="Langzeitgedächtnis",
                    model="memory.json",
                )

        rec_match = RECALL_PATTERN.search(user_text.strip())
        if rec_match:
            topic = rec_match.group(1).strip(" ?.!")
            entries = self.memory.recall(topic)
            if not entries:
                reply = (
                    f"Chef, dazu habe ich im Langzeitgedächtnis noch nichts zu {topic} gespeichert."
                    if topic
                    else "Chef, mein Langzeitgedächtnis enthält bisher noch keine Einträge."
                )
            else:
                joined = "; ".join(entries)
                reply = f"Chef, dazu habe ich Folgendes gespeichert: {joined}."
            return BrainResponse(text=reply, provider="Langzeitgedächtnis", model="memory.json")

        if STATUS_BRIEFING_PATTERN.search(user_text):
            report = build_status_briefing()
            self.memory.mark_briefing_done_today()
            return BrainResponse(text=report, provider="Statusbericht", model="Systemdiagnose")

        if SMART_HOME_ON_PATTERN.search(user_text) and any(
            d.lower() in user_text.lower()
            for d in self.smart_home.list_device_names() + ["steckdose", "relais", "smart home"]
        ):
            msg = self.smart_home.control_device(user_text, turn_on=True)
            return BrainResponse(text=msg, provider="Smart-Home", model="HTTP")

        if SMART_HOME_OFF_PATTERN.search(user_text) and any(
            d.lower() in user_text.lower()
            for d in self.smart_home.list_device_names() + ["steckdose", "relais", "smart home"]
        ):
            msg = self.smart_home.control_device(user_text, turn_on=False)
            return BrainResponse(text=msg, provider="Smart-Home", model="HTTP")

        media_msg = handle_media_command(user_text)
        if media_msg is not None:
            return BrainResponse(text=media_msg, provider="Medien-Steuerung", model="OS")

        # --- Multi-Monitor & Multi-Window HUD Steuerung ---
        q_low = user_text.strip().lower()
        if re.search(
            r"\b(?:hol(?:e)?\s+(?:das\s+fenster|die\s+app|es)?\s*her|zieh(?:e)?\s+(?:das\s+fenster|es)\s+(?:her|auf\s+meinen\s+bildschirm)|bring(?:e)?\s+(?:das\s+fenster|es)\s+auf\s+meinen\s+monitor)\b",
            q_low,
        ):
            from multi_monitor import bring_last_window_to_cursor_monitor

            b_res = bring_last_window_to_cursor_monitor()
            return BrainResponse(text=str(b_res.get("message") or "Fenster hergeholt."), provider="Multi-Monitor Window", model="CursorBring")

        if re.search(r"\b(?:arbeitsmodus|work[\s\-]*mode)\b.*\b(?:starten|aktivieren|an|los)?\b", q_low):
            from macro_engine import MACRO_ENGINE

            wm_res = MACRO_ENGINE.start_work_mode()
            return BrainResponse(text=str(wm_res.get("message") or "Arbeitsmodus gestartet."), provider="Makro-Engine", model="WorkMode")

        focus_m = re.search(
            r"\b(?:fokus|focus)\s+(?:auf\s+)?(?:den\s+|dem\s+)?(?:monitor|bildschirm|screen)\s*([1-9]|links|mitte|rechts)\b|\b(?:fokus|focus)\s+auf\s+(link(?:s|en)|mittler(?:e|en)|recht(?:s|en))\b",
            q_low,
        )
        if focus_m:
            from macro_engine import MACRO_ENGINE

            tgt = focus_m.group(1) or focus_m.group(2) or 2
            f_res = MACRO_ENGINE.focus_monitor(monitor_target=tgt, brain=self.brain)
            return BrainResponse(text=str(f_res.get("message") or "Monitor analysiert."), provider="Makro-Engine", model="FocusOCR")

        if re.search(r"\b(?:markier|hebe\s+hervor|highlight|ping)\w*\b.*\b(?:monitor|bildschirm|screen)\b", q_low):
            from multi_monitor import extract_monitor_target_from_text, highlight_monitor_region

            tgt = extract_monitor_target_from_text(q_low) or 1
            hl_res = highlight_monitor_region(monitor=tgt, label=f"JARVIS PING M{tgt}", duration=2.8)
            return BrainResponse(text=str(hl_res.get("message") or "Bereich markiert."), provider="Screen-Overlay", model="Highlight")

        if re.search(r"\b(?:lies|ocr|lese)\w*\b.*\b(?:monitor|bildschirm|screen)\b", q_low):
            from macro_engine import read_monitor_ocr
            from multi_monitor import extract_monitor_target_from_text

            tgt = extract_monitor_target_from_text(q_low) or 1
            ocr_res = read_monitor_ocr(monitor_target=tgt, question=user_text, brain=self.brain)
            return BrainResponse(text=str(ocr_res.get("message") or "Inhalt ausgelesen."), provider="Screen-OCR", model="Vision")

        if re.search(
            r"\b(?:öffne|zeig(?:e)?|starte|pop[\s\-]*out)\b.*\b(?:3d[\s\-]*gehirn|neural[\s\-]*gehirn|neural[\s\-]*core|3d[\s\-]*brain|gehirn[\s\-]*fenster|analyse[\s\-]*fenster|neural[\s\-]*logs?|thought[\s\-]*stream|log[\s\-]*fenster|multi[\s\-]*screen|screen[\s\-]*matrix|system[\s\-]*telemetry|vision[\s\-]*fenster|radar|fenster\s*[23])\b",
            q_low,
            flags=re.IGNORECASE,
        ):
            from macro_engine import open_window
            from multi_monitor import extract_monitor_target_from_text

            is_neural_core = re.search(r"\b(3d[\s\-]*gehirn|neural[\s\-]*gehirn|neural[\s\-]*core|3d[\s\-]*brain|gehirn[\s\-]*fenster)\b", q_low)
            is_telemetry = re.search(r"\b(telemetry|logs?|thought|fenster\s*3|n8n)\b", q_low)
            win_name = "neural-core" if is_neural_core else ("system-telemetry" if is_telemetry else "screen-matrix")
            mon_target = extract_monitor_target_from_text(q_low)
            res = open_window(window_name=win_name, monitor=mon_target)
            return BrainResponse(text=str(res.get("message") or "Fenster geöffnet."), provider="Multi-Monitor HUD", model="MultiWindow")

        move_m = re.search(
            r"\b(?:verschiebe|schiebe|platziere|leg(?:e)?)\s+(?:das\s+fenster\s+|fenster\s+|die\s+app\s+)?(.+?)\s+auf\s+(?:den\s+|dem\s+)?(?:monitor|bildschirm|screen)\s*([1-9]|links|mitte|rechts)\b",
            user_text.strip(),
            flags=re.IGNORECASE,
        ) or re.search(
            r"\b(?:verschiebe|schiebe|platziere)\s+(?:das\s+fenster\s+|fenster\s+)?(.*?)\s+auf\s+(?:den\s+)?(link(?:en|s)|mittler(?:en|e)|recht(?:en|s))\s+(?:monitor|bildschirm|screen)\b",
            user_text.strip(),
            flags=re.IGNORECASE,
        )
        if move_m:
            from gui import get_active_gui
            from multi_monitor import move_window_to_monitor

            win_q = re.sub(r"^(?:das|den|die|aktuell(?:e|es))\s+", "", move_m.group(1).strip(), flags=re.IGNORECASE).strip()
            if win_q.lower() in ("fenster", "aktuelles fenster", "dieses fenster", ""):
                win_q = ""
            layout_kind = "left_half" if "linke hälfte" in q_low else ("right_half" if "rechte hälfte" in q_low else "maximize")
            m_res = move_window_to_monitor(window_query=win_q, monitor_target=move_m.group(2), layout=layout_kind)
            gui_inst = get_active_gui()
            if gui_inst is not None:
                gui_inst.emit_neural_thought(
                    category="tool",
                    tool="move_window_to_monitor",
                    summary=str(m_res.get("message") or ""),
                    monitor=(m_res.get("monitor") or {}).get("index"),
                )
            return BrainResponse(text=str(m_res.get("message") or "Fenster verschoben."), provider="Multi-Monitor Window", model="WinManager")

        if re.search(r"\b(?:welche|wie\s*viele)\s+(?:monitore|bildschirme|displays)\b", q_low):
            from gui import get_active_gui
            from multi_monitor import get_connected_monitors

            mons = get_connected_monitors()
            gui_inst = get_active_gui()
            if gui_inst is not None:
                gui_inst.refresh_monitors()
            desc = ", ".join(f"Monitor {m['index']} ({m['pos']}, {m['w']}x{m['h']})" for m in mons)
            return BrainResponse(
                text=f"Ich erkenne aktuell {len(mons)} Bildschirm{'e' if len(mons) != 1 else ''}: {desc}.",
                provider="Multi-Monitor",
                model="screeninfo+mss",
            )

        # --- Autonomer n8n Workflow Architect (kostenloses lokales n8n) ---
        if re.search(r"\bn8n\b", q_low):
            from n8n_architect import N8N_ARCHITECT

            if re.search(r"\b(erstell|bau|generier|mach|anleg|neu)\w*\b", q_low):
                res_n8n = N8N_ARCHITECT.create_workflow(goal=user_text, activate=True, brain=self.brain)
                return BrainResponse(text=str(res_n8n.get("message") or "n8n-Workflow erstellt."), provider="n8n Architect", model="REST-API")
            if re.search(r"\b(start|führ|trigg|ausführ|run|send)\w*\b", q_low):
                slug_m = re.search(r"(?:workflow|webhook|flow)\s+([a-zA-Z0-9_\-/:.]+)", user_text, flags=re.IGNORECASE)
                tgt = slug_m.group(1) if slug_m else ""
                res_run = N8N_ARCHITECT.run_workflow(target=tgt, payload={"query": user_text})
                return BrainResponse(text=str(res_run.get("message") or "n8n-Workflow ausgeführt."), provider="n8n Architect", model="Webhook")
            for act_kw, act_name in (("aktivier", "activate"), ("deaktivier", "deactivate"), ("lösch", "delete")):
                if act_kw in q_low:
                    id_m = re.search(r"\b(?:id\s*)?([a-zA-Z0-9_\-]{3,})\s*$", user_text.strip(), flags=re.IGNORECASE)
                    wf_id = id_m.group(1) if id_m else ""
                    res_m = N8N_ARCHITECT.list_and_manage(action=act_name, workflow_id=wf_id)
                    return BrainResponse(text=str(res_m.get("message") or ""), provider="n8n Architect", model="Manager")
            res_list = N8N_ARCHITECT.list_and_manage(action="list")
            return BrainResponse(text=str(res_list.get("message") or ""), provider="n8n Architect", model="Status")

        # --- Autonomes Trading, Account-Erstellung, Video-Schnitt & 4-Agenten-Schwarm ---
        try:
            from autonomous_agent import AUTONOMOUS_AGENT
            from nas_brain import NAS_BRAIN
            from swarm_core import SWARM_CORE
            from trading_engine import TRADING_ENGINE
            from vault import read_latest_verification_email

            # 1. Bestätigung / Abbruch einer vorbereiteten Order (Modus 1)
            if getattr(TRADING_ENGINE, "_pending_order", None) is not None:
                if EMAIL_SEND_CONFIRM_PATTERN.search(user_text) or re.search(
                    r"\b(order\s+ausführen|kauf\s+bestätigen|jetzt\s+kaufen|ja\s+bitte)\b",
                    user_text,
                    flags=re.IGNORECASE,
                ):
                    res_ord = TRADING_ENGINE.confirm_pending_order()
                    AUTONOMOUS_AGENT._emit_monitor_update(f"[TRADING] {res_ord.get('message', '')[:65]}", active=False)
                    SWARM_CORE.update_agent_card(
                        "finance_quant",
                        status="DONE",
                        step=res_ord.get("message", "")[:70],
                        active=False,
                        step_num=1,
                        max_steps=1,
                    )
                    return BrainResponse(text=res_ord["message"], provider="Trading-Engine", model="Order-Execution")
                if EMAIL_CANCEL_PATTERN.search(user_text):
                    res_can = TRADING_ENGINE.cancel_pending_order()
                    AUTONOMOUS_AGENT._emit_monitor_update("[TRADING] Vorbereitete Order abgebrochen.", active=False)
                    SWARM_CORE.update_agent_card(
                        "finance_quant",
                        status="IDLE",
                        step="Vorbereitete Order abgebrochen.",
                        active=False,
                    )
                    return BrainResponse(text=res_can["message"], provider="Trading-Engine", model="Safety-Guard")

            # 2. Portfolio-Status ("Wie steht mein Depot?", "Portfolio Status")
            if re.search(
                r"\b(depot\s*status|portfolio\s*status|wie\s+steht\s+mein\s+(?:depot|portfolio)|offene\s+positionen|broker\s+kontostand)\b",
                user_text,
                flags=re.IGNORECASE,
            ):
                p_stat = TRADING_ENGINE.get_portfolio_status()
                AUTONOMOUS_AGENT._emit_monitor_update(f"[TRADING] Portfolio geprüft ({p_stat.get('provider')})", active=False)
                SWARM_CORE.update_agent_card(
                    "finance_quant",
                    status="DONE",
                    step=f"Portfolio geprüft ({p_stat.get('provider')})",
                    active=False,
                    step_num=1,
                    max_steps=1,
                )
                return BrainResponse(
                    text=p_stat["summary_de"],
                    provider=str(p_stat.get("provider") or "Trading-Engine"),
                    model=str(p_stat.get("trading_mode") or "paper").upper(),
                )

            # 3. Aktien- / Krypto-Analyse ("Analysiere Nvidia", "Analysiere Aktie TSLA", "Chartanalyse Bitcoin")
            ana_match = re.search(
                r"\b(?:analysiere|chartanalyse|aktienanalyse|kursanalyse|bewerte\s+die\s+aktie)\s+(?:die\s+aktie\s+|den\s+kurs\s+von\s+|mal\s+)?([a-zA-Z0-9.\-]+)",
                user_text.strip(),
                flags=re.IGNORECASE,
            )
            if ana_match:
                sym_raw = ana_match.group(1).strip(" ?.,!")
                AUTONOMOUS_AGENT._emit_monitor_update(
                    f"[LIVE-BROWSER-SHOW] Öffne Live-Chart für {sym_raw.upper()} im Webbrowser...",
                    step=1,
                    max_steps=1,
                    active=True,
                )
                ana_res = SWARM_CORE.finance_quant.analyze_asset(
                    sym_raw,
                    brain=self.brain,
                    on_status=on_status,
                    use_visible_browser=True,
                )
                AUTONOMOUS_AGENT._emit_monitor_update(
                    f"[LIVE-BROWSER-SHOW] Live-Chart fertig: {ana_res.get('symbol')}",
                    step=1,
                    max_steps=1,
                    active=False,
                )
                return BrainResponse(
                    text=ana_res["summary_de"],
                    provider="1-FPS-Browser-Show",
                    model="TradingView/Finanzen",
                )

            # 4. Aktien- / Krypto-Order ("Kaufe 2x NVDA", "Kaufe 2 Nvidia Aktien", "Verkaufe 1 Apple Aktie")
            trade_match = re.search(
                r"\b(kaufe?|verkaufe?|buy|sell)\s+(?:mir\s+|bitte\s+|mal\s+)?(\d+(?:[.,]\d+)?)\s*(?:x|mal|stück|aktien\s+von|aktien|euro\s+in|€\s+in)?\s+([a-zA-Z0-9.\-]+)",
                user_text.strip(),
                flags=re.IGNORECASE,
            )
            if trade_match and not re.search(r"\b(im\s+supermarkt|einkaufen|milch|brot)\b", user_text, re.IGNORECASE):
                verb = trade_match.group(1).lower()
                qty_val = float(trade_match.group(2).replace(",", "."))
                sym_raw = trade_match.group(3).strip(" ?.,!")
                side = "sell" if verb.startswith(("verkauf", "sell")) else "buy"
                is_eur = bool(re.search(r"\b(euro|€|eur)\b", user_text, flags=re.IGNORECASE))

                if re.search(r"\b(im\s+browser|trade\s+republic|scalable|tradingview|web\s*broker)\b", user_text, flags=re.IGNORECASE):
                    b_res = SWARM_CORE.finance_quant.execute_trade(
                        symbol=sym_raw,
                        qty_or_amount_eur=qty_val,
                        side=side,
                        is_notional_eur=is_eur,
                        use_browser=True,
                        brain=self.brain,
                    )
                    return BrainResponse(text=b_res["message"], provider="Browser-Broker", model="1-FPS-Vision")

                AUTONOMOUS_AGENT._emit_monitor_update(
                    f"[TRADING] {'Kaufe' if side == 'buy' else 'Verkaufe'} {qty_val:g}x {sym_raw.upper()} @ Market",
                    step=1,
                    max_steps=1,
                    active=True,
                )
                ord_res = SWARM_CORE.finance_quant.execute_trade(
                    symbol=sym_raw,
                    qty_or_amount_eur=qty_val,
                    side=side,
                    is_notional_eur=is_eur,
                    use_browser=False,
                    brain=self.brain,
                )
                AUTONOMOUS_AGENT._emit_monitor_update(
                    f"[TRADING] {ord_res.get('message', '')[:68]}",
                    step=1,
                    max_steps=1,
                    active=False,
                )
                return BrainResponse(
                    text=ord_res["message"],
                    provider=str(ord_res.get("provider") or "Trading-Engine"),
                    model="Order",
                )

            # 5. Direkter 2FA- / Bestätigungscode-Abruf aus E-Mail
            if re.search(
                r"\b(bestätigungscode|2fa\s*code|verifizierungscode|aktivierungslink)\b.*\b(mail|e-mail|postfach)\b",
                user_text,
                flags=re.IGNORECASE,
            ):
                AUTONOMOUS_AGENT._emit_monitor_update("[SCHRITT 1/1] Lese 2FA-Code aus E-Mail...", step=1, max_steps=1, active=True)
                m_res = SWARM_CORE.web_ghost.fetch_latest_2fa_code()
                AUTONOMOUS_AGENT._emit_monitor_update(f"[E-MAIL 2FA] {m_res.get('message', '')[:60]}", step=1, max_steps=1, active=False)
                if m_res.get("ok"):
                    code_txt = f"Der Bestätigungscode lautet {m_res['verification_code']}." if m_res.get("verification_code") else ""
                    link_txt = f" Aktivierungslink gefunden: {m_res['verification_link']}" if m_res.get("verification_link") else ""
                    return BrainResponse(
                        text=f"Chef, ich habe die Bestätigungsmail ('{m_res.get('subject')}') gelesen. {code_txt}{link_txt}".strip(),
                        provider="IMAP-2FA-Reader",
                        model="SecureVault",
                    )
                return BrainResponse(text=m_res["message"], provider="IMAP-2FA-Reader", model="SecureVault")

            # 6. Autonome Account-Erstellung ("Erstell mir einen Account auf ...", "Registriere mich bei ...")
            if re.search(
                r"\b(erstell\w*\s+(?:mir\s+)?(?:einen\s+)?(?:account|konto)|registrier\w*\s+mich\s+(?:auf|bei)|neuen\s+account\s+(?:auf|bei))\b",
                user_text,
                flags=re.IGNORECASE,
            ):
                auto_res = SWARM_CORE.web_ghost.run_web_or_account_task(
                    goal=user_text,
                    brain=self.brain,
                    max_steps=None,
                )
                return BrainResponse(
                    text=auto_res["message"],
                    provider="Web-Account-Ghost",
                    model="Zero-Knowledge-Vault",
                )

            # 7. High-Speed Auto-Video-Editor & NLE-Schnittprogramm-Steuerung (AGENT 2: DESKTOP/VIDEO)
            if re.search(
                r"\b(schneide\s+(?:das\s+|mein\s+)?video|video\s+schneiden|auto[\s\-]*edit|entferne\s+(?:die\s+)?(?:stille|sprechpausen)|jump\s*cut|rohvideo\s+schneiden)\b",
                user_text,
                flags=re.IGNORECASE,
            ):
                path_m = re.search(r"([A-Za-z]:[\\/][^\s\"']+\.(?:mp4|mov|mkv|avi|webm))", user_text, flags=re.IGNORECASE)
                video_path = path_m.group(1) if path_m else ""
                open_nle = ""
                if re.search(r"\bcapcut\b", user_text, flags=re.IGNORECASE):
                    open_nle = "CapCut"
                elif re.search(r"\b(davinci|resolve)\b", user_text, flags=re.IGNORECASE):
                    open_nle = "DaVinci Resolve"
                elif re.search(r"\bpremiere\b", user_text, flags=re.IGNORECASE):
                    open_nle = "Premiere Pro"
                vid_res = SWARM_CORE.desktop_video.auto_edit_video(
                    input_path=video_path,
                    open_in_editor=open_nle,
                )
                return BrainResponse(
                    text=vid_res.get("message", "Videoschnitt abgeschlossen."),
                    provider="Desktop-Video-Agent",
                    model="FFmpeg-JumpCut",
                )

            # 8. NAS-Gehirn & Wissens-Archiv direkt durchsuchen
            nas_search_m = re.search(
                r"\b(?:durchsuche\s+(?:das\s+)?(?:nas|nas[\s\-]*gehirn|langzeitgedächtnis)|was\s+weißt\s+du\s+aus\s+dem\s+nas\s+über)\s+(.+)",
                user_text.strip(),
                flags=re.IGNORECASE,
            )
            if nas_search_m:
                q_nas = nas_search_m.group(1).strip(" ?.,!")
                SWARM_CORE.update_agent_card(
                    "overmind",
                    status="NAS-SEARCH",
                    step=f"Suche im NAS-Gehirn nach '{q_nas[:36]}'...",
                    active=True,
                    step_num=1,
                    max_steps=1,
                )
                hits = NAS_BRAIN.search_memory(q_nas, top_k=5)
                skills = NAS_BRAIN.find_ui_skills(q_nas, top_k=3)
                st_nas = NAS_BRAIN.get_status()
                SWARM_CORE.update_agent_card(
                    "overmind",
                    status="READY",
                    step=f"NAS-Suche abgeschlossen ({len(hits)} Episoden, {len(skills)} UI-Skills)",
                    active=False,
                    step_num=1,
                    max_steps=1,
                )
                if not hits and not skills:
                    return BrainResponse(
                        text=f"Chef, im NAS-Gehirn ({st_nas.get('active_path')}) wurden noch keine Einträge zu '{q_nas}' gefunden.",
                        provider="Overmind-NAS",
                        model="SQLite-Vector",
                    )
                summary_parts = [f"Chef, im NAS-Gehirn ({st_nas.get('status_label')}) habe ich Folgendes zu '{q_nas}' gefunden:"]
                for h in hits[:3]:
                    summary_parts.append(f"- [{h.get('category')}] {str(h.get('summary') or '')[:140]}")
                for s in skills[:2]:
                    summary_parts.append(f"- [UI-Skill {s.get('app_name')}] {s.get('skill_name')}: Shortcut {s.get('shortcut') or 'Maus'}")
                return BrainResponse(
                    text="\n".join(summary_parts),
                    provider="Overmind-NAS",
                    model="SQLite-Vector",
                )

            # 9. Expliziter 4-Agenten-Schwarm-Auftrag ("Overmind: ...", "Schwarm: ...")
            swarm_m = re.search(
                r"^(?:overmind|schwarm|4[\s\-]*agenten[\s\-]*schwarm)\s*[:\-]?\s*(.+)",
                user_text.strip(),
                flags=re.IGNORECASE,
            )
            if swarm_m:
                sw_goal = swarm_m.group(1).strip()
                sw_res = SWARM_CORE.execute_swarm_mission(sw_goal, brain=self.brain)
                return BrainResponse(
                    text=sw_res.get("message", "Schwarm-Mission abgeschlossen."),
                    provider="4-Agent-Swarm",
                    model="Overmind",
                )

        except Exception as auto_exc:
            print(f"[Autonomous/Trading/Swarm Shortcut Hinweis] {auto_exc}")

        # 10. UPGRADE 5: Live-Workflow-Recorder ("Schau mir kurz zu...", "Fertig gelernt", Workflow-Replay)
        try:
            clean_low = user_text.strip().lower()
            rec_start_m = re.search(
                r"(?:schau\s+mir\s+(?:kurz\s+)?zu|ich\s+zeig(?:e)?\s+dir(?:\s+kurz)?|workflow\s+aufnehmen|lerne\s+(?:diesen\s+)?ablauf)(?:[:,\s\-]+(.+))?",
                user_text.strip(),
                flags=re.IGNORECASE,
            )
            if rec_start_m:
                from skill_recorder import SKILL_RECORDER

                hint_name = (rec_start_m.group(1) or "").strip()
                hint_name = re.sub(r"^(?:wie\s+ich|das\s+hier|mal)\s+", "", hint_name, flags=re.IGNORECASE).strip(" .!?")
                rec_res = SKILL_RECORDER.start_recording(skill_name=hint_name[:60])
                return BrainResponse(
                    text=str(rec_res.get("message") or "Live-Workflow-Aufnahme gestartet."),
                    provider="SkillRecorder",
                    model="Live-Macro",
                )

            if any(
                tok in clean_low
                for tok in (
                    "fertig gelernt",
                    "aufnahme beenden",
                    "workflow speichern",
                    "stopp aufnahme",
                    "stoppe aufnahme",
                    "lernen beenden",
                )
            ):
                from skill_recorder import SKILL_RECORDER

                name_m = re.search(
                    r"(?:als|name|namens|speichern\s+als)\s+[\"']?([^\"'.!?]{2,50})[\"']?",
                    user_text.strip(),
                    flags=re.IGNORECASE,
                )
                stop_res = SKILL_RECORDER.stop_recording(skill_name=(name_m.group(1).strip() if name_m else ""))
                return BrainResponse(
                    text=str(stop_res.get("message") or "Workflow gespeichert."),
                    provider="SkillRecorder",
                    model="Live-Macro",
                )

            replay_m = re.search(
                r"^(?:wiederhole|spiele?|replay|mach\s+noch\s*mal|führe?\s+aus)\s+(?:den\s+|die\s+|das\s+)?(?:gelernten?\s+)?(?:workflow|ablauf|skill|makro)\s*(.+)$",
                user_text.strip(),
                flags=re.IGNORECASE,
            )
            if replay_m:
                from skill_recorder import SKILL_RECORDER

                rep_res = SKILL_RECORDER.replay_skill(replay_m.group(1).strip(), speed_multiplier=10.0)
                return BrainResponse(
                    text=str(rep_res.get("message") or "Workflow abgespielt."),
                    provider="SkillRecorder",
                    model="10x-Replay",
                )
        except Exception as rec_exc:
            print(f"[SkillRecorder Shortcut Hinweis] {rec_exc}")

        # 11. MODUL 1–3 ("Second Brain", "Morning Briefing & Cockpit", "Multi-App End-to-End Pipelines")
        try:
            from executive_assistant import EXECUTIVE_ASSISTANT

            EXECUTIVE_ASSISTANT.attach_memory(self.memory)

            # MODUL 2: Proaktives CEO Morning Briefing & Desktop Cockpit
            if re.search(
                r"\b(?:starte\s+(?:mein\s+)?(?:morning\s+|executive\s+)?briefing|"
                r"executive[\s\-]*briefing|morgen[\s\-]*briefing|"
                r"guten\s+morgen(?:\s*,?\s*jarvis)?\s*[!.]*$)\b",
                user_text.strip(),
                flags=re.IGNORECASE,
            ):
                br_res = EXECUTIVE_ASSISTANT.run_executive_briefing(
                    brain=self.brain,
                    memory=self.memory,
                    alarm_manager=self.alarm_manager,
                    on_status=on_status,
                )
                return BrainResponse(
                    text=str(br_res.get("briefing_text") or "Executive Briefing abgeschlossen."),
                    provider="Executive-Briefing",
                    model="Cockpit-Mode",
                )

            # MODUL 1: Second-Brain Aufgabenverwaltung ("Neue Aufgabe ...", "Welche Aufgaben habe ich?")
            task_add_m = re.match(
                r"^(?:neue\s+aufgabe|aufgabe\s+hinzufügen|to[\s\-]*do(?:\s+hinzufügen)?|erinnere\s+mich\s+an)\s*[:\-]?\s*(.+)$",
                user_text.strip(),
                flags=re.IGNORECASE,
            )
            if task_add_m and not re.search(r"\bwecker\b", user_text, flags=re.IGNORECASE):
                added_task = self.memory.add_open_task(task_add_m.group(1).strip(" .!"))
                return BrainResponse(
                    text=f"Chef, ich habe die Aufgabe '{added_task['title']}' in deinem Second Brain gespeichert.",
                    provider="Second-Brain",
                    model="Tasks",
                )

            if re.search(
                r"\b(?:welche\s+aufgaben|meine\s+aufgaben|offene\s+aufgaben|was\s+steht\s+an|to[\s\-]*do\s*liste)\b",
                user_text.strip(),
                flags=re.IGNORECASE,
            ):
                open_t = self.memory.get_open_tasks(include_done=False)
                if not open_t:
                    return BrainResponse(
                        text="Chef, aktuell stehen keine offenen Aufgaben in deinem Second Brain.",
                        provider="Second-Brain",
                        model="Tasks",
                    )
                t_lines = "; ".join(f"{t.get('title')}" for t in open_t[:6])
                return BrainResponse(
                    text=f"Chef, aktuell sind {len(open_t)} Aufgaben offen: {t_lines}.",
                    provider="Second-Brain",
                    model="Tasks",
                )

            # MODUL 1: Sofortiges Fortsetzen / Öffnen bekannter Projekte ("Mach an meinem letzten Projekt weiter", "Öffne die Datei von gestern")
            if re.search(
                r"\b(?:mach\s+an\s+meinem\s+letzten\s+projekt\s+weiter|"
                r"letztes\s+projekt\s+(?:weiter|öffnen|fortsetzen|laden)|"
                r"weiter\s+am\s+letzten\s+projekt|"
                r"öffne\s+(?:bitte\s+)?(?:die\s+datei|das\s+projekt|mein\s+projekt)\s+(?:von\s+gestern|vom\s+letzten\s+mal)|"
                r"öffne\s+mein\s+letztes\s+(?:cad|3d|video|code|excel)?[\s\-]*projekt)\b",
                user_text.strip(),
                flags=re.IGNORECASE,
            ):
                res_proj = EXECUTIVE_ASSISTANT.resume_project_or_file(
                    user_text,
                    memory=self.memory,
                    brain=self.brain,
                    on_status=on_status,
                )
                return BrainResponse(
                    text=str(res_proj.get("spoken") or "Projekt geöffnet."),
                    provider="Second-Brain",
                    model="ProjectKnowledgeGraph",
                )

            # MODUL 3 Pipeline B: Projekt-Setup auf Zuruf
            if re.search(
                r"\b(?:bereite\s+(?:alles|mir\s+alles)\s+(?:vor\s+)?für\s+(?:ein\s+)?neues\s+.*projekt|"
                r"neues\s+(?:videoschnitt|video|cad|3d|programmier|code)[\s\-]*projekt(?:\s+setup)?\s+(?:vorbereiten|einrichten|namens)|"
                r"projekt[\s\-]*setup\s+(?:für|namens))\b",
                user_text.strip(),
                flags=re.IGNORECASE,
            ):
                setup_res = SWARM_CORE.run_project_setup_pipeline(
                    request_text=user_text,
                    brain=self.brain,
                    memory=self.memory,
                    on_status=on_status,
                )
                return BrainResponse(
                    text=str(setup_res.get("spoken") or "Projekt-Setup abgeschlossen."),
                    provider="Workflow-Pipeline-B",
                    model="Project-Setup",
                )

            # MODUL 3 Pipeline A: Browser-Recherche -> Datei/Tabelle auf dem Desktop
            if re.search(
                r"\brecherchier\w*\b.*\b(?:desktop|zusammenfassung|tabelle|csv|excel|datei|schreib\w*)\b",
                user_text.strip(),
                flags=re.IGNORECASE,
            ):
                pipe_res = SWARM_CORE.run_research_to_desktop_pipeline(
                    query=user_text,
                    brain=self.brain,
                    memory=self.memory,
                    on_status=on_status,
                )
                return BrainResponse(
                    text=str(pipe_res.get("spoken") or "Recherche auf Desktop gespeichert."),
                    provider="Workflow-Pipeline-A",
                    model="Browser+Desktop",
                )
        except Exception as exec_exc:
            print(f"[ExecutiveAssistant Shortcut Hinweis] {exec_exc}")

        # Bedarfs-Erkennung für fehlende Software (nur wenn Aufgabe klar nach lokal fehlender App verlangt)
        try:
            from app_installer_agent import APP_INSTALLER_AGENT

            proposal = APP_INSTALLER_AGENT.detect_missing_app_and_propose(
                user_goal=user_text,
                brain=self.brain,
            )
            if proposal is not None:
                return BrainResponse(
                    text=str(proposal.get("question") or proposal.get("message") or ""),
                    provider="App-Installer-Agent",
                    model="Need-Detection",
                )
        except Exception as prop_exc:
            print(f"[AppInstaller Bedarfs-Hinweis] {prop_exc}")

        return None

    def handle(
        self,
        user_text: str,
        webcam_image: Optional[bytes] = None,
        on_status: Optional[Callable[[str], None]] = None,
        on_enter_screen_mode: Optional[Callable[[], None]] = None,
        on_exit_screen_mode: Optional[Callable[[], None]] = None,
        on_provider: Optional[Callable[[str, str], None]] = None,
        on_token: Optional[Callable[[str], None]] = None,
    ) -> BrainResponse:
        requested_online = detect_online_mode_command(user_text)
        if requested_online is not None:
            set_online_mode(requested_online)
            if requested_online:
                provider, model = self.brain.active_online_provider()
                reply = "Online-Systeme aktiviert."
            else:
                provider, model = "Ollama (Lokal)", self.brain.ollama_text_model
                reply = "Wechsle in den autarken Offline-Modus."
            return BrainResponse(text=reply, provider=provider, model=model)

        from multi_monitor import capture_monitor_jpeg, extract_monitor_target_from_text

        explicit_mon = extract_monitor_target_from_text(user_text)
        is_webcam_frame = webcam_image is not None
        if webcam_image is None and self.vision is not None and should_capture_webcam(user_text) and explicit_mon is None:
            webcam_image = self.vision.get_current_frame(archive=True)
            is_webcam_frame = webcam_image is not None
        elif (webcam_image is None or explicit_mon is not None) and (should_capture_screen(user_text) or explicit_mon is not None and re.search(r"\b(schau|sieh|guck|zeig|was\s+ist|analysier|prüf|monitor|bildschirm)\b", user_text, flags=re.IGNORECASE)):
            from gui import get_active_gui

            gui_inst = get_active_gui()
            # Cursor-Aware Vision: Ohne expliziten Monitor nur den aktuellen Maus-Monitor in voller Auflösung erfassen!
            target_mon = explicit_mon if explicit_mon is not None else "cursor"

            def _on_scan(tgt: Any, detail: str) -> None:
                if gui_inst is not None:
                    gui_inst.emit_brain_activity(state="screen_scan", intensity=0.92, active_monitor=tgt, detail=detail)
                    gui_inst.emit_neural_thought(category="vision", tool="capture_monitor_screen", summary=detail, monitor=tgt)

            cap = capture_monitor_jpeg(
                target=target_mon,
                quality=88,
                max_width=1440 if target_mon == "all" else 0,
                with_grid=False,
                on_scan=_on_scan,
            )
            desktop_image = bytes(cap.get("jpeg_bytes") or b"") if cap.get("ok") else None
            if desktop_image is None:
                from agent_hand import VISUAL_HAND_AGENT

                desktop_image = VISUAL_HAND_AGENT.get_latest_passive_screen_bytes(capture_if_missing=True)
            if desktop_image is not None:
                mon_ctx = f"[MULTI-MONITOR-KONTEXT: {cap.get('summary') or 'Bildschirm erfasst'}]\n{user_text}"
                return self.brain.ask_stream(
                    mon_ctx,
                    image_bytes=desktop_image,
                    on_provider=on_provider,
                    on_token=on_token,
                )

        if webcam_image is not None:
            if is_webcam_frame and is_workbench_query(user_text):
                mem_block = self.memory.get_context_block()
                wb_prompt = (
                    f"{WORKBENCH_VISION_SYSTEM_PROMPT}\n\nLangzeitgedächtnis:\n{mem_block}"
                    if mem_block
                    else WORKBENCH_VISION_SYSTEM_PROMPT
                )
                return self.brain.ask_stream(
                    user_text,
                    image_bytes=webcam_image,
                    system_prompt=wb_prompt,
                    on_provider=on_provider,
                    on_token=on_token,
                )
            return self.brain.ask_stream(
                user_text,
                image_bytes=webcam_image,
                on_provider=on_provider,
                on_token=on_token,
            )

        shortcut = self._try_deterministic_shortcuts(user_text, on_status=on_status)
        if shortcut is not None:
            return shortcut

        if not get_is_online() and ONLINE_ONLY_QUERY_PATTERN.search(user_text):
            return _offline_online_only_response()

        heuristic = _heuristic_route(user_text)
        if heuristic["agent"] == "chat":
            try:
                from brain import FORCE_VISUAL_BROWSER_SHOW, is_smalltalk_only

                if FORCE_VISUAL_BROWSER_SHOW and not is_smalltalk_only(user_text) and not ACTION_INTENT_HINT_PATTERN.search(user_text):
                    heuristic = {"agent": "browser_agent", "goal": user_text}
            except Exception:
                pass
        if heuristic["agent"] in ("system_agent", "media_agent", "smart_home_agent", "browser_agent"):
            route = heuristic
        elif not get_is_online():
            # Im Offline-Modus kein strikter JSON-Router-Zwang für das kleine 1B-Modell
            if heuristic["agent"] != "chat":
                route = heuristic
            else:
                return self.brain.ask_stream(
                    user_text,
                    on_provider=on_provider,
                    on_token=on_token,
                )
        elif heuristic["agent"] == "chat" and not ACTION_INTENT_HINT_PATTERN.search(user_text):
            return self.brain.ask_stream(
                user_text,
                on_provider=on_provider,
                on_token=on_token,
            )
        else:
            route_resp = self.brain.ask(
                user_text,
                system_prompt=ROUTER_SYSTEM_PROMPT,
                use_history=False,
            )
            route = _extract_json(route_resp.text)
            if not route or "agent" not in route:
                route = heuristic

        agent_name = str(route.get("agent", "chat")).lower()

        if agent_name == "smart_home_agent":
            turn_on = bool(route.get("turn_on", True))
            device_query = str(route.get("device") or user_text)
            msg = self.smart_home.control_device(device_query, turn_on=turn_on)
            return BrainResponse(text=msg, provider="Smart-Home", model="HTTP")

        if agent_name == "email_agent":
            return self._run_email_agent(route, user_text, on_provider=on_provider, on_token=on_token)

        if agent_name == "web_search_agent":
            search_q = str(route.get("query") or user_text)
            try:
                from swarm_core import SWARM_CORE
                SWARM_CORE.update_agent_card("web_ghost", status="SEARCHING", step=f"Web-Recherche: '{search_q[:42]}'", active=True, step_num=1, max_steps=1)
            except Exception:
                pass
            res_ws = self._run_web_search_agent(search_q, user_text, on_provider=on_provider, on_token=on_token)
            try:
                from nas_brain import NAS_BRAIN
                from swarm_core import SWARM_CORE
                SWARM_CORE.update_agent_card("web_ghost", status="DONE", step=f"Recherche fertig: '{search_q[:38]}'", active=False, step_num=1, max_steps=1)
                NAS_BRAIN.remember_episode("web_ghost", "web_research", f"Suche: {search_q}\nAntwort: {res_ws.text[:400]}")
            except Exception:
                pass
            return res_ws

        if agent_name == "browser_agent":
            goal = str(route.get("goal") or user_text)
            full_goal = (
                f"{user_text} ({goal})"
                if user_text and goal and user_text.strip() != goal.strip()
                else (user_text or goal)
            )
            try:
                from agent_hand import resolve_max_agent_steps
                from swarm_core import SWARM_CORE
                _m_steps = resolve_max_agent_steps()
                SWARM_CORE.update_agent_card("web_ghost", status="BROWSING", step=f"AGENT AKTIV // SCHRITT 1 / {_m_steps} // SUB-ZIEL: {full_goal[:36]}", active=True, step_num=1, max_steps=_m_steps)
            except Exception:
                _m_steps = 200
            if on_enter_screen_mode is not None:
                try:
                    on_enter_screen_mode()
                except Exception:
                    pass
            try:
                from agent_hand import VISUAL_HAND_AGENT
                hand_txt = VISUAL_HAND_AGENT.visible_browser_search_and_summarize(
                    query=full_goal,
                    brain=self.brain,
                    on_status=on_status,
                    interactive_click=True,
                    max_steps=None,
                )
                res_br = BrainResponse(text=hand_txt, provider="1-FPS-Browser-Hand", model="Closed-Loop-1000x1000")
            except Exception:
                res_br = self._run_browser_agent(
                    goal=goal,
                    on_status=on_status,
                    on_enter_screen_mode=on_enter_screen_mode,
                    on_exit_screen_mode=on_exit_screen_mode,
                )
            finally:
                if on_exit_screen_mode is not None:
                    try:
                        on_exit_screen_mode()
                    except Exception:
                        pass
            try:
                from nas_brain import NAS_BRAIN
                from swarm_core import SWARM_CORE
                SWARM_CORE.update_agent_card("web_ghost", status="DONE", step=res_br.text[:64], active=False, step_num=_m_steps, max_steps=_m_steps)
                NAS_BRAIN.remember_episode("web_ghost", "browser_task", f"Aufgabe: {full_goal}\nErgebnis: {res_br.text[:400]}")
            except Exception:
                pass
            return res_br

        if agent_name == "media_agent":
            media_msg = handle_media_command(user_text)
            if media_msg is not None:
                return BrainResponse(text=media_msg, provider="Medien-Steuerung", model="OS")

        if agent_name == "system_agent":
            return self._run_system_agent(route, user_text)

        if agent_name == "gui_agent":
            goal = str(route.get("goal") or user_text)
            full_gui_goal = (
                user_text
                if (user_text and len(user_text.strip()) >= len(goal.strip()))
                else (goal or user_text)
            )
            try:
                from agent_hand import resolve_max_agent_steps
                from swarm_core import SWARM_CORE
                _g_steps = resolve_max_agent_steps()
                SWARM_CORE.update_agent_card("desktop_video", status="VISION-LOOP", step=f"AGENT AKTIV // SCHRITT 1 / {_g_steps} // SUB-ZIEL: {full_gui_goal[:38]}", active=True, step_num=1, max_steps=_g_steps)
            except Exception:
                pass

            goal_low = full_gui_goal.lower()
            use_marathon_desktop = any(
                tok in goal_low
                for tok in (
                    "desktop",
                    "arbeitsfläche",
                    "doppelklick",
                    "rechtsklick",
                    "drag",
                    "ziehe",
                    "hotkey",
                    "launcher",
                    "darin",
                    "und öffne",
                    "und klick",
                    "arccraft",
                    "filmcraft",
                    "steam",
                    "discord",
                    "spotify",
                    "explorer",
                    "einstellungen",
                )
            )
            if use_marathon_desktop:
                try:
                    if on_enter_screen_mode is not None:
                        try:
                            on_enter_screen_mode()
                        except Exception:
                            pass
                    from agent_hand import VISUAL_HAND_AGENT

                    op_res = VISUAL_HAND_AGENT.visual_desktop_app_operator(
                        goal=full_gui_goal,
                        app_name="",
                        brain=self.brain,
                        on_status=on_status,
                        max_steps=None,
                        restore_hud_after=False,
                    )
                    res_gui = BrainResponse(
                        text=str(op_res.get("summary") or op_res.get("message") or f"Aufgabe '{full_gui_goal}' sichtbar ausgeführt."),
                        provider="Desktop-App-Operator",
                        model="Closed-Loop-1000x1000",
                    )
                except Exception:
                    res_gui = self.desktop_mouse_agent(
                        goal=full_gui_goal,
                        on_status=on_status,
                        on_enter_screen_mode=on_enter_screen_mode,
                        on_exit_screen_mode=on_exit_screen_mode,
                    )
                finally:
                    GHOST_OVERLAY.stop()
            else:
                res_gui = self.desktop_mouse_agent(
                    goal=full_gui_goal,
                    on_status=on_status,
                    on_enter_screen_mode=on_enter_screen_mode,
                    on_exit_screen_mode=on_exit_screen_mode,
                )
            try:
                from nas_brain import NAS_BRAIN
                from swarm_core import SWARM_CORE
                SWARM_CORE.update_agent_card("desktop_video", status="DONE", step=res_gui.text[:64], active=False, step_num=1, max_steps=1)
                NAS_BRAIN.remember_episode("desktop_video", "desktop_control", f"Ziel: {full_gui_goal}\nErgebnis: {res_gui.text[:400]}")
            except Exception:
                pass
            return res_gui

        res_chat = self.brain.ask_stream(
            user_text,
            on_provider=on_provider,
            on_token=on_token,
        )
        try:
            from nas_brain import NAS_BRAIN
            NAS_BRAIN.remember_episode(
                "overmind",
                "conversation",
                f"Nutzer: {user_text[:200]} | Jarvis: {res_chat.text[:250]}",
                details={"user": user_text, "assistant": res_chat.text, "provider": res_chat.provider},
            )
        except Exception:
            pass
        return res_chat

    def _run_browser_agent(
        self,
        goal: str,
        on_status: Optional[Callable[[str], None]] = None,
        on_enter_screen_mode: Optional[Callable[[], None]] = None,
        on_exit_screen_mode: Optional[Callable[[], None]] = None,
    ) -> BrainResponse:
        if not get_is_online():
            return _offline_online_only_response()

        if not self.browser_agent.is_available():
            return self.desktop_mouse_agent(
                goal=goal,
                on_status=on_status,
                on_enter_screen_mode=on_enter_screen_mode,
                on_exit_screen_mode=on_exit_screen_mode,
            )

        if on_enter_screen_mode:
            on_enter_screen_mode()
            time.sleep(0.25)

        used_provider = "Playwright-Browser"
        used_model = "Chromium"

        def _ask_step(prompt: str, sys_prompt: str) -> str:
            nonlocal used_provider, used_model
            resp = self.brain.ask(prompt, system_prompt=sys_prompt, use_history=False)
            used_provider = resp.provider
            used_model = f"Chromium/{resp.model}"
            return resp.text

        try:
            outcome = self.browser_agent.run_goal(goal=goal, ask_text_fn=_ask_step, on_status=on_status)
            if outcome.get("status") == "safety_stop" and on_exit_screen_mode:
                on_exit_screen_mode()
            return BrainResponse(
                text=outcome.get("message") or "Browser-Aufgabe erledigt, Chef.",
                provider=used_provider,
                model=used_model,
            )
        except Exception as exc:
            print(f"[Browser-Agent Fallback auf Desktop-Maus] {exc}")
            return self.desktop_mouse_agent(
                goal=goal,
                on_status=on_status,
                on_enter_screen_mode=on_enter_screen_mode,
                on_exit_screen_mode=on_exit_screen_mode,
            )

    def _run_web_search_agent(
        self,
        search_query: str,
        original_query: str,
        on_provider: Optional[Callable[[str, str], None]] = None,
        on_token: Optional[Callable[[str], None]] = None,
        on_status: Optional[Callable[[str], None]] = None,
    ) -> BrainResponse:
        if not get_is_online():
            return _offline_online_only_response()

        # AUFGABE 1 + 2: Ausschließlich sichtbare Desktop-Webbrowser-Recherche (KEINE stille Hintergrund-Suche!)
        try:
            from agent_hand import VISUAL_HAND_AGENT

            if on_status:
                on_status("BROWSER-OPERATOR // SICHTBARE WEBSUCHE IM DESKTOP-BROWSER...")
            res_search = VISUAL_HAND_AGENT.visual_browser_search_and_read(
                query=original_query or search_query,
                brain=self.brain,
                on_status=on_status,
                interactive_click=True,
                max_steps=None,
            )
            summary_msg = str(
                res_search.get("summary")
                or res_search.get("message")
                or f"Ich habe {res_search.get('url', 'die Webseite')} sichtbar im Desktop-Browser geöffnet und gelesen."
            )
            return BrainResponse(
                text=summary_msg,
                provider="1-FPS-Web-Hand",
                model="Visible-Browser-Operator",
            )
        except Exception as exc:
            print(f"[Sichtbare Browser-Suche Hinweis] {exc}")
            return BrainResponse(
                text=f"Chef, der sichtbare Desktop-Browser-Operator konnte die Suche leider nicht abschließen: {exc}",
                provider="1-FPS-Web-Hand",
                model="Error",
            )
        finally:
            try:
                GHOST_OVERLAY.stop()
            except Exception:
                pass

    def _run_email_agent(
        self,
        route: dict,
        original_query: str,
        on_provider: Optional[Callable[[str, str], None]] = None,
        on_token: Optional[Callable[[str], None]] = None,
    ) -> BrainResponse:
        action = str(route.get("action", "read_unread")).lower()
        if action == "draft":
            to_addr = str(route.get("to", "")).strip()
            if not to_addr or "@" not in to_addr:
                email_match = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", original_query)
                to_addr = email_match.group(0) if email_match else "empfaenger@beispiel.de"
            subject = str(route.get("subject", "")).strip() or "Kurze Rückmeldung"
            hint = str(route.get("body_hint") or original_query)

            draft_resp = self.brain.ask(
                f"Formuliere einen kurzen, höflichen deutschen E-Mail-Text ohne Betreffzeile für: {hint}",
                use_history=False,
            )
            saved_msg = save_email_draft(to_addr, subject, draft_resp.text)
            if "@" in to_addr and to_addr != "empfaenger@beispiel.de":
                self._pending_email = {"to": to_addr, "subject": subject, "body": draft_resp.text}
                return BrainResponse(
                    text=(
                        f"Chef, ich habe die E-Mail an {to_addr} vorbereitet. {saved_msg} "
                        "Sagen Sie einfach 'Abschicken', wenn ich sie direkt per SMTP senden soll, oder 'Entwurf verwerfen'."
                    ),
                    provider=draft_resp.provider,
                    model=draft_resp.model,
                )
            return BrainResponse(
                text=f"Chef, ich habe den E-Mail-Entwurf vorbereitet. {saved_msg} (Keine echte Empfänger-Adresse angegeben.)",
                provider=draft_resp.provider,
                model=draft_resp.model,
            )

        try:
            unread = fetch_unread_emails(limit=5)
        except Exception as exc:
            return BrainResponse(
                text=f"Chef, ich konnte das E-Mail-Postfach nicht abrufen: {exc}",
                provider="E-Mail-Agent",
                model="IMAP",
            )

        if not unread:
            return BrainResponse(
                text="Chef, es liegen derzeit keine ungelesenen E-Mails vor.",
                provider="E-Mail-Agent",
                model="IMAP",
            )

        raw_list = "\n".join(
            f"- Von: {item['from']} | Betreff: {item['subject']} | Auszug: {item['snippet']}"
            for item in unread
        )
        summary = self.brain.ask_stream(
            f"Fasse diese ungelesenen E-Mails kurz für den Chef zum Vorlesen zusammen:\n{raw_list}",
            use_history=False,
            on_provider=on_provider,
            on_token=on_token,
        )
        return summary

    def _ask_vision_locator(self, prompt: str, img_bytes: bytes, sys_prompt: str) -> str:
        resp = self.brain.ask(
            prompt,
            image_bytes=img_bytes,
            system_prompt=sys_prompt,
            use_history=False,
        )
        return resp.text

    def _run_system_agent(self, route: dict, original_query: str) -> BrainResponse:
        action = str(route.get("action", "system_status")).lower()
        try:
            if action == "open_app":
                target = str(route.get("target") or "").strip()
                # Prüfen, ob gewünschte App lokal fehlt -> Pflicht-Rückfrage vor Download & Installation!
                try:
                    from app_indexer import APP_INDEXER
                    from app_installer_agent import APP_INSTALLER_AGENT

                    if target and APP_INDEXER.is_app_installed(target) is None:
                        proposal = APP_INSTALLER_AGENT.detect_missing_app_and_propose(
                            user_goal=original_query,
                            explicit_app=target,
                            brain=self.brain,
                        )
                        if proposal is not None:
                            return BrainResponse(
                                text=str(proposal.get("question") or proposal.get("message") or ""),
                                provider="App-Installer-Agent",
                                model="Need-Detection",
                            )
                except Exception:
                    pass
                # 100% VISIBLE OPERATOR: Wenn neben dem App-Start auch Bedienschritte gewünscht sind,
                # startet & bedient `visual_desktop_app_operator` direkt IN der geöffneten App.
                if re.search(
                    r"\b(?:und\s+)?(?:geh\w*\s+(?:zu|auf|in)|klick\w*|wisch\w*|scroll\w*|navigier\w*|rechn\w*|tipp\w*|schreib\w*|spiel\w*|erstell\w*|such\w*)\b",
                    original_query,
                    flags=re.IGNORECASE,
                ):
                    from agent_hand import VISUAL_HAND_AGENT

                    res = VISUAL_HAND_AGENT.visual_desktop_app_operator(
                        goal=original_query,
                        app_name=target,
                        brain=self.brain,
                        max_steps=None,
                    )
                    return BrainResponse(
                        text=str(res.get("summary") or res.get("message") or f"'{target}' sichtbar bedient."),
                        provider="Desktop-App-Operator",
                        model="Closed-Loop-1000x1000",
                    )
                from agent_hand import coordinate_jarvis_window_for_action

                coordinate_jarvis_window_for_action(mode="minimize")
                msg = open_app(target, visible_cursor=True, ask_vision_fn=self._ask_vision_locator)
                return BrainResponse(text=f"Erledigt, Chef. {msg}", provider="System-Agent", model="Ghost-Cursor")

            if action == "open_url":
                target = str(route.get("target") or route.get("url") or "").strip() or original_query
                from agent_hand import VISUAL_HAND_AGENT

                res_search = VISUAL_HAND_AGENT.visible_browser_search_and_summarize(
                    query=original_query or target,
                    brain=self.brain,
                    interactive_click=True,
                    max_steps=None,
                )
                return BrainResponse(
                    text=str(res_search.get("summary") or res_search.get("message") or f"Webseite geöffnet: {res_search.get('url', target)}"),
                    provider="System+1FPS-Hand",
                    model="Closed-Loop-1000x1000",
                )
            elif False and action == "open_url_legacy":
                if not get_is_online():
                    return _offline_online_only_response()
                target = str(route.get("target") or "https://www.google.de")
                msg = open_url(target)
                return BrainResponse(text=f"Erledigt, Chef. {msg}", provider="System-Agent", model="OS")

            if action == "create_folder":
                folder_name = str(route.get("folder_name") or "Neues_Projekt")
                video_tpl = bool(route.get("video_template", False))
                msg = create_project_folder(folder_name, video_template=video_tpl)
                return BrainResponse(text=f"Chef, erledigt. {msg}", provider="System-Agent", model="OS")

            if action == "move_file":
                src = str(route.get("src") or "")
                dst = str(route.get("dst") or "")
                if not src or not dst:
                    return BrainResponse(
                        text="Chef, bitte nennen Sie mir Quell- und Zielpfad für diese Dateiaktion.",
                        provider="System-Agent",
                        model="OS",
                    )
                msg = move_or_rename_path(src, dst)
                return BrainResponse(text=f"Chef, erledigt. {msg}", provider="System-Agent", model="OS")

            status_text = get_system_status()
            return BrainResponse(text=f"Chef, {status_text}", provider="System-Agent", model="OS")
        except GhostCursorInterrupted as exc:
            return BrainResponse(text=str(exc), provider="Ghost-Cursor", model="Fail-Safe")
        except Exception as exc:
            return BrainResponse(
                text=f"Chef, bei der Systemaktion gab es ein Problem: {exc}",
                provider="System-Agent",
                model="OS",
            )

    def desktop_mouse_agent(
        self,
        goal: str,
        on_status: Optional[Callable[[str], None]] = None,
        on_enter_screen_mode: Optional[Callable[[str], None] | Callable[[], None]] = None,
        on_exit_screen_mode: Optional[Callable[[], None]] = None,
        max_steps: int = 10,
    ) -> BrainResponse:
        """
        Visueller Ghost-Cursor-, Wisch-, Drag-and-Drop- und Tastatur-Agent mit Always-on-Top Sci-Fi-Overlay,
        Taskleisten-/UI-Koordinaten-Erkennung, Koordinaten-Raster & Fail-Safe-Sperre.
        """
        if on_status:
            on_status("Steuere Bildschirm")

        if on_enter_screen_mode:
            try:
                on_enter_screen_mode()  # type: ignore[misc]
            except Exception:
                pass
        goal_low = (goal or "").lower()
        if any(tok in goal_low for tok in ("desktop", "arbeitsfläche", "doppelklick")):
            try:
                from agent_hand import show_desktop_safely

                show_desktop_safely(wait_seconds=1.0, send_win_d=True)
            except Exception:
                time.sleep(1.0)
        else:
            time.sleep(0.65)

        GHOST_OVERLAY.start(f"JARVIS // {goal[:38]}")
        step_history: list[str] = []
        last_provider = "Ghost-Cursor"
        last_model = "Vision-UI"

        try:
            check_user_interrupt()

            # Intelligente Vorstufe: Wenn der Befehl mit "Öffne <App> und gehe zu <Menüpunkt>" beginnt,
            # gleitet der Ghost-Cursor zuerst sichtbar zum Taskleisten-Icon der App, klickt und wartet kurz.
            compound_match = re.search(
                r"\b(?:öffne|starte)\s+(?:bitte\s+)?(?:die\s+|den\s+|das\s+)?([a-zA-Z0-9äöüÄÖÜß _-]+?)(?:\s+app|\s+programm)?\s+(?:und\s+)?(?:gehe?\s+(?:zu|auf|in)|klicke?\s+auf|navigiere?\s+zu|wische?\s+|scrolle?\s+)(.+)$",
                goal.strip(),
                flags=re.IGNORECASE,
            )
            if compound_match:
                app_name = compound_match.group(1).strip(" ,.!?")
                follow_up = compound_match.group(2).strip(" ,.!?")
                open_log = open_app(
                    app_name,
                    visible_cursor=True,
                    keep_overlay_active=True,
                    ask_vision_fn=self._ask_vision_locator,
                )
                step_history.append(f"Vorstufe: {open_log}")
                GHOST_OVERLAY.set_status(f"Warte auf Fenster: {app_name}...", mode="search")
                time.sleep(1.6)

                # Prüfe, ob das Folge-Ziel (z. B. "Bibliothek" oder "Game Pass") direkt per UIAutomation/Vision greifbar ist
                nav_clean = re.sub(
                    r"^(?:die|der|das|den|dem|zur|zum|auf|in)\s+",
                    "",
                    follow_up,
                    flags=re.IGNORECASE,
                ).strip()
                if nav_clean and not re.search(r"\b(?:unten|oben|links|rechts|runter|hoch)\b", nav_clean, flags=re.IGNORECASE):
                    GHOST_OVERLAY.set_status(f"Suche Menüpunkt: {nav_clean[:28]}...", mode="search")
                    ui_hit = locate_ui_element_visually(
                        nav_clean,
                        ask_vision_fn=self._ask_vision_locator,
                        taskbar_only=False,
                    )
                    if ui_hit and ui_hit.get("found"):
                        ux, uy = int(ui_hit["x"]), int(ui_hit["y"])
                        u_name = str(ui_hit.get("name") or nav_clean)
                        if not is_financial_action_forbidden(u_name, follow_up, "continue"):
                            nav_log = execute_gui_action(
                                action="click",
                                x=ux,
                                y=uy,
                                target_label=u_name,
                                ask_vision_fn=self._ask_vision_locator,
                            )
                            step_history.append(f"Direkt-Navigation: {nav_log}")
                            return BrainResponse(
                                text=f"Erledigt, Chef. Ich habe {app_name} sichtbar geöffnet und bin zu {u_name} navigiert.",
                                provider="Ghost-Cursor",
                                model=str(ui_hit.get("source") or "UI-Vision"),
                            )

            for step_idx in range(1, max_steps + 1):
                check_user_interrupt()
                from agent_hand import capture_annotated_screen_1fps

                screen_bytes, width, height, (cur_x, cur_y) = capture_annotated_screen_1fps(
                    max_width=1280,
                    max_height=720,
                    quality=75,
                    grid_step=200,
                )
                GHOST_OVERLAY.set_status(
                    f"HAND-AGENT AKTIV // POS: [{cur_x}, {cur_y}] (Schritt {step_idx})",
                    mode="search",
                )
                if on_status:
                    on_status(f"HAND-AGENT AKTIV // ZIEL: [{cur_x}, {cur_y}]")
                history_text = "\n".join(step_history[-5:]) if step_history else "Noch keine Aktionen."

                prompt = (
                    f"Ziel des Chefs: {goal}\n"
                    f"Bildschirmauflösung (für x/y Koordinaten, Rasterlinien im Abstand 200 px, aktuelles rotes Fadenkreuz bei [{cur_x}, {cur_y}]): {width}x{height} Pixel.\n"
                    f"Schritt {step_idx} von {max_steps}.\n"
                    f"Bisherige Aktionen:\n{history_text}\n"
                    "Analysiere den 1-FPS-Screenshot mit dem roten Positions-Fadenkreuz [X, Y] und 200px-Koordinatengitter und gib den nächsten JSON-Schritt aus."
                )

                ai_resp = self.brain.ask(
                    prompt,
                    image_bytes=screen_bytes,
                    system_prompt=GUI_AGENT_SYSTEM_PROMPT,
                    use_history=False,
                )
                last_provider = ai_resp.provider
                last_model = ai_resp.model

                cmd = _extract_json(ai_resp.text)
                if not cmd:
                    return BrainResponse(
                        text="Chef, ich konnte die Bildschirmanalyse gerade nicht sauber auswerten und habe sicherheitshalber gestoppt.",
                        provider=last_provider,
                        model=last_model,
                    )

                status = str(cmd.get("status", "continue")).lower()
                action = str(cmd.get("action", "none")).lower()
                target_label = str(cmd.get("target_label", ""))
                reason = str(cmd.get("reason", ""))
                direction = str(cmd.get("direction", ""))
                screen_purchase = bool(cmd.get("screen_contains_purchase_button", False))

                # HARDCODED FINANCIAL / ORDER SAFETY GUARD
                if is_financial_action_forbidden(
                    target_label,
                    reason,
                    status,
                    screen_contains_purchase_button=screen_purchase,
                ):
                    print("[SAFETY GUARD] Finale Kauf-/Order-/Bestätigungs-Aktion blockiert!")
                    GHOST_OVERLAY.set_status("SAFETY STOP: Kauf-/Bezahl-Button erkannt!", mode="click")
                    time.sleep(0.8)
                    return BrainResponse(
                        text=SAFETY_STOP_MESSAGE,
                        provider=last_provider,
                        model=last_model,
                    )

                if status == "done" or action == "done" or (status == "continue" and action == "none"):
                    final_msg = str(cmd.get("final_message") or "Chef, die Bildschirmaufgabe ist erledigt.")
                    return BrainResponse(text=final_msg, provider=last_provider, model=last_model)

                if status == "blocked":
                    final_msg = str(
                        cmd.get("final_message") or f"Chef, ich komme hier nicht weiter: {reason}"
                    )
                    return BrainResponse(text=final_msg, provider=last_provider, model=last_model)

                target_x = _safe_coord_int(cmd.get("x"), 0)
                target_y = _safe_coord_int(cmd.get("y"), 0)

                # Präzisions-Verfeinerung über Windows UIAutomation, falls ein benanntes UI-Element geklickt wird
                if action in ("click", "double_click", "right_click", "move") and target_label:
                    is_tb = "taskleiste" in target_label.lower() or "taskleiste" in reason.lower() or target_y >= (height - 75)
                    uia_hit = locate_ui_element_visually(target_label, ask_vision_fn=None, taskbar_only=is_tb)
                    if uia_hit and uia_hit.get("found"):
                        target_x = _safe_coord_int(uia_hit.get("x"), target_x)
                        target_y = _safe_coord_int(uia_hit.get("y"), target_y)

                if on_status:
                    on_status(f"HAND-AGENT AKTIV // ZIEL: [{target_x}, {target_y}]")
                GHOST_OVERLAY.set_status(f"HAND-AGENT AKTIV // ZIEL: [{target_x}, {target_y}]", mode="move")

                action_log = execute_gui_action(
                    action=action,
                    x=target_x,
                    y=target_y,
                    end_x=_safe_coord_int(cmd.get("end_x"), 0),
                    end_y=_safe_coord_int(cmd.get("end_y"), 0),
                    text=str(cmd.get("text") or ""),
                    key=str(cmd.get("key") or ""),
                    keys=cmd.get("keys") if isinstance(cmd.get("keys"), list) else None,
                    amount=_safe_coord_int(cmd.get("amount"), 0),
                    target_label=target_label or reason,
                    direction=direction,
                    ask_vision_fn=self._ask_vision_locator,
                )
                step_log = f"Schritt {step_idx}: {action_log} ({target_label or reason})"
                print(f"[Desktop-Control | 1-FPS Hand-Agent] {step_log}")
                step_history.append(step_log)
                # 1-Sekunden-Takt (1 FPS Closed-Loop Rückkopplung)
                time.sleep(1.0)

            return BrainResponse(
                text="Chef, ich habe die maximale Schrittzahl auf dem Bildschirm erreicht und pausiere jetzt zur Sicherheit.",
                provider=last_provider,
                model=last_model,
            )
        except GhostCursorInterrupted as exc:
            print(f"[Ghost-Cursor Fail-Safe] {exc}")
            return BrainResponse(
                text=str(exc),
                provider="Ghost-Cursor",
                model="Fail-Safe",
            )
        finally:
            GHOST_OVERLAY.stop()
            if on_exit_screen_mode:
                on_exit_screen_mode()

    desktop_control = desktop_mouse_agent
    _run_gui_agent = desktop_mouse_agent


class MultiAgentOrchestrator:
    """Kompatibilitäts-Orchestrator für direkte Hand-Agent- & Browser-Agent-Tests."""

    def __init__(self, brain: Any = None, vision: Any = None, tools_module: Any = None) -> None:
        self.brain = brain
        self.vision = vision
        self.tools = tools_module

    @staticmethod
    def _is_visible_browser_request(lower_text: str) -> bool:
        return bool(
            re.search(
                r"\b(such|suche|recherchiere|google|zeig\s+mir|im\s+internet|im\s+browser|video)\b",
                lower_text or "",
                flags=re.IGNORECASE,
            )
        )

    def should_use_multi_agent(self, user_text: str) -> bool:
        route = _heuristic_route(user_text or "")
        return route.get("agent", "chat") != "chat" or self._is_visible_browser_request((user_text or "").lower())

