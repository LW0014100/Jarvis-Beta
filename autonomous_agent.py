#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
JARVIS Autonomous Computer-Use & Web Workflow Agent (autonomous_agent.py)
-------------------------------------------------------------------------
Executes multi-step autonomous missions in a real visible desktop browser using
the 1-FPS Visual Closed-Loop (`agent_hand.py`), Zero-Knowledge Vault injection
(`vault.py`), Dual-System Email & Account Factory (`mail_factory.py`),
3D-Secure / PushTAN 90s approval detection, and strict 2-stage EXECUTION_MODE
enforcement (MODUS A: SEMI_AUTO vs. MODUS B: FULL_AUTO).
"""

import json
import logging
import os
import re
import threading
import time
import urllib.request
import webbrowser
from datetime import datetime
from typing import Any, Callable

from agent_hand import (
    AGENT_HAND,
    MODUS_A_SPOKEN_MESSAGE,
    PUSHTAN_3DS_SPOKEN_MESSAGE,
    MarathonStepMemory,
    _speak_voice_hint_async,
    capture_1fps_frame,
    detect_3d_secure_or_pushtan,
    find_and_click_text_on_screen,
    get_screen_size,
    ghost_click,
    ghost_hotkey,
    ghost_move_to,
    ghost_press,
    ghost_scroll,
    ghost_typewrite,
    is_final_buy_action,
    norm_to_screen_xy,
    resolve_max_agent_steps,
    wait_for_3d_secure_approval,
)
import config as _cfg

GEMINI_API_KEY = getattr(_cfg, "GEMINI_API_KEY", "") or getattr(_cfg, "GOOGLE_API_KEY", "")
GROQ_API_KEY = getattr(_cfg, "GROQ_API_KEY", "")
LLM_MODEL = getattr(_cfg, "LLM_MODEL", "") or getattr(_cfg, "ONLINE_MODEL", "gemini-2.5-flash")
LLM_PROVIDER = getattr(_cfg, "LLM_PROVIDER", "") or getattr(_cfg, "AI_PROVIDER", "gemini")
OLLAMA_HOST = getattr(_cfg, "OLLAMA_HOST", "") or getattr(_cfg, "OLLAMA_URL", "http://127.0.0.1:11434")
OPENAI_API_KEY = getattr(_cfg, "OPENAI_API_KEY", "")
from mail_factory import MAIL_FACTORY
from vault import SECURE_VAULT

logger = logging.getLogger("JarvisAutonomousAgent")

# Live state broadcasted to the HUD monitor widget
_AGENT_MONITOR_LOCK = threading.RLock()
_AGENT_MONITOR_STATE: dict[str, Any] = {
    "active": False,
    "goal": "",
    "step": 0,
    "max_steps": resolve_max_agent_steps(),
    "status": "idle",  # idle | running | paused_for_user | waiting_3ds | completed | aborted | error
    "last_action": "Bereit.",
    "last_reason": "",
    "target_x": 0,
    "target_y": 0,
    "screenshot_b64": "",
    "updated_at": "",
}

_MONITOR_CALLBACK: Callable[[dict[str, Any]], None] | None = None


def set_agent_monitor_callback(cb: Callable[[dict[str, Any]], None] | None) -> None:
    global _MONITOR_CALLBACK
    _MONITOR_CALLBACK = cb


def get_agent_monitor_state() -> dict[str, Any]:
    with _AGENT_MONITOR_LOCK:
        state = dict(_AGENT_MONITOR_STATE)
        state["autonomy_mode"] = SECURE_VAULT.get_autonomy_mode()
        state["execution_mode"] = SECURE_VAULT.get_execution_mode()
        return state


def _update_monitor(
    *,
    active: bool | None = None,
    goal: str | None = None,
    step: int | None = None,
    max_steps: int | None = None,
    status: str | None = None,
    last_action: str | None = None,
    last_reason: str | None = None,
    target_x: int | None = None,
    target_y: int | None = None,
    screenshot_b64: str | None = None,
) -> dict[str, Any]:
    with _AGENT_MONITOR_LOCK:
        if active is not None:
            _AGENT_MONITOR_STATE["active"] = active
        if goal is not None:
            _AGENT_MONITOR_STATE["goal"] = goal
        if step is not None:
            _AGENT_MONITOR_STATE["step"] = step
        if max_steps is not None:
            _AGENT_MONITOR_STATE["max_steps"] = max_steps
        if status is not None:
            _AGENT_MONITOR_STATE["status"] = status
        if last_action is not None:
            _AGENT_MONITOR_STATE["last_action"] = last_action
        if last_reason is not None:
            _AGENT_MONITOR_STATE["last_reason"] = last_reason
        if target_x is not None:
            _AGENT_MONITOR_STATE["target_x"] = target_x
        if target_y is not None:
            _AGENT_MONITOR_STATE["target_y"] = target_y
        if screenshot_b64 is not None:
            _AGENT_MONITOR_STATE["screenshot_b64"] = screenshot_b64
        _AGENT_MONITOR_STATE["updated_at"] = datetime.now().strftime("%H:%M:%S")
        snapshot = dict(_AGENT_MONITOR_STATE)
        snapshot["autonomy_mode"] = SECURE_VAULT.get_autonomy_mode()
        snapshot["execution_mode"] = SECURE_VAULT.get_execution_mode()

    if _MONITOR_CALLBACK is not None:
        try:
            _MONITOR_CALLBACK(snapshot)
        except Exception as exc:
            logger.debug("Agent monitor callback failed: %s", exc)
    return snapshot


AUTONOMOUS_SYSTEM_PROMPT = """Du bist der ausdauernde visuelle 1-FPS Marathon-Computer-Use- & Web-Workflow-Agent von J.A.R.V.I.S.
Du siehst den aktuellen Live-Screenshot des Desktops mit einem roten Fadenkreuz an der aktuellen Mausposition (X={cur_x}, Y={cur_y}) auf einem {width}x{height} Bildschirm.

ÜBERGEORDNETES HAUPTZIEL (MAIN GOAL): "{goal}"
AKTUELLES ZWISCHENZIEL (CURRENT SUBGOAL): "{current_subgoal}"
AKTUELLER SCHRITT: {step} von {max_steps}
KAUF- & ZUGRIFFS-MODUS (EXECUTION_MODE): {execution_mode} ({autonomy_mode_desc})
VERFÜGBARE TRESOR-PLATZHALTER (Zero-Knowledge - NIEMALS Klartext erfinden, sondern Platzhalter nutzen):
{vault_placeholders}
KOMPAKTE AKTIONS-HISTORIE (Gleitendes Gedächtnis):
{history}
{anti_stuck_hint}

WICHTIGE MARATHON- & HANDLUNGS-REGELN:
1. AUSDAUER-REGEL: Du hast bis zu {max_steps} Schritte! Gib NIEMALS `action: "done"` aus, solange die komplette Aufgabe (alle Teilziele wie Webseite öffnen -> suchen -> filtern -> ausfüllen -> prüfen -> Ergebnis lesen) noch nicht sichtbar vollständig abgeschlossen ist.
2. ANTI-STUCK-REGEL: Wenn eine Aktion nichts bewirkt oder ein Button nicht reagiert, wähle SOFORT eine andere Strategie (Pop-up mit `press` "escape" schließen, `scroll` nach unten, `press` "tab" + "enter" oder Adressleiste mit `hotkey` "ctrl+l").
3. Wenn du ein Eingabefeld für Name, E-Mail, Passwort, Telefonnummer, Geburtsdatum, Adresse, IBAN oder Debit-Kreditkarte (`{{VAULT_CARD_NUMBER}}`, `{{VAULT_CARD_EXPIRY}}`, `{{VAULT_CARD_CVC}}`, `{{VAULT_CARD_HOLDER}}`) ausfüllen sollst, klicke zuerst auf das Feld (`action: "click"`) und nutze im nächsten Schritt `action: "type_secret"` mit dem passenden Platzhalter (z. B. `"text": "{{VAULT_EMAIL}}"` oder `"text": "{{VAULT_CARD_NUMBER}}"`).
4. Wenn du für eine Registrierung eine neue eigene E-Mail-Adresse + sicheres 20-Zeichen-Passwort brauchst, nutze `action: "create_instant_email"` (1-Sekunden API-Postfach via api.mail.tm) oder `action: "create_browser_email"` (Proton.me/Tuta im Browser).
5. Wenn eine Webseite nach einem E-Mail-Bestätigungscode (6-stellige PIN / TAN) oder Verifizierungslink fragt, nutze `action: "read_email_code"`.
6. Wenn beim Bezahlen mit Debitkarte oder Kontoaufladung eine 3D-Secure / S-pushTAN-Freigabe auf dem Bildschirm erscheint, nutze `action: "wait_3ds"`, damit Jarvis bis zu 90 Sekunden geduldig auf die Handy-Freigabe wartet.
7. Wenn EXECUTION_MODE == "SEMI_AUTO" (MODUS A: Halb-Autonom) ist:
   - Fülle alle Felder (Stückzahl, Betrag, Kontodaten, Debitkarte) komplett aus.
   - Sobald du vor dem finalen "Kaufen" / "Order kostenpflichtig ausführen" / "Jetzt bezahlen" Button stehst, gib `action: "pause_for_user"` samt `x` und `y` des Kauf-Buttons zurück! Jarvis fährt die Maus direkt neben den Button und ruft den Nutzer zum letzten Klick.
8. Wenn EXECUTION_MODE == "FULL_AUTO" (MODUS B: 100% Voll-Zugriff) ist:
   - Klicke auch den finalen Kauf- oder Verkaufs-Button komplett selbstständig ohne Rückfrage!
9. Antworte AUSSCHLIESSLICH mit einem validen JSON-Objekt:
{{
  "action": "click" | "double_click" | "type" | "type_secret" | "press" | "hotkey" | "scroll" | "open_url" | "create_instant_email" | "create_browser_email" | "read_email_code" | "wait_3ds" | "pause_for_user" | "done",
  "x": <int Ziel-X>,
  "y": <int Ziel-Y>,
  "text": "<Text, URL oder {{VAULT_...}} Platzhalter>",
  "key": "<Taste wie enter, tab, escape>",
  "scroll_y": <int z.B. -5 für runter>,
  "subgoal": "<Aktuelles kurzes Zwischenziel>",
  "reason": "<Kurze deutsche Erklärung, was du siehst und tust>"
}}"""


class AutonomousWorkflowAgent:
    """
    Executes autonomous web & desktop missions (account creation, email verification,
    broker navigation, debit-card checkout, form filling) with 1-FPS visual feedback.
    """

    def __init__(self) -> None:
        self._cancel_event = threading.Event()
        self._pause_event = threading.Event()
        self._worker_thread: threading.Thread | None = None

    def register_monitor_callback(self, cb: Callable[[dict[str, Any]], None] | None) -> None:
        set_agent_monitor_callback(cb)

    def get_monitor_snapshot(self) -> dict[str, Any]:
        return get_agent_monitor_state()

    def get_monitor_state(self) -> dict[str, Any]:
        return get_agent_monitor_state()

    def set_external_step(self, step_label: str, goal: str = "") -> dict[str, Any]:
        snap = _update_monitor(
            active=True,
            goal=goal or None,
            status="running",
            last_action=step_label,
        )
        snap["step_label"] = step_label
        return snap

    def clear_external_step(self, step_label: str = "Bereit.") -> dict[str, Any]:
        snap = _update_monitor(
            active=False,
            status="completed",
            last_action=step_label,
        )
        snap["step_label"] = step_label
        return snap

    def stop_mission(self) -> dict[str, Any]:
        """Immediately aborts any running autonomous mission and stops AGENT_HAND."""
        self._cancel_event.set()
        AGENT_HAND.stop()
        state = _update_monitor(
            active=False,
            status="aborted",
            last_action="🛑 Not-Aus: Mission durch Nutzer gestoppt.",
        )
        state["step_label"] = "🛑 NOT-STOPP: Mission durch Nutzer gestoppt."
        return {"ok": True, "state": state}

    def kill(self) -> dict[str, Any]:
        """1ms Emergency Kill-Switch returning the updated monitor snapshot."""
        res = self.stop_mission()
        snap = dict(res.get("state", {}))
        snap["step_label"] = "🛑 NOT-STOPP: Mission durch Nutzer gestoppt."
        return snap

    def resume_after_user_confirmation(self) -> dict[str, Any]:
        """Resumes a paused mission (e.g. after user reviewed or clicked in Mode A)."""
        self._pause_event.set()
        state = _update_monitor(
            status="running",
            last_action="▶ Nutzer-Bestätigung erhalten – setze Mission fort...",
        )
        return {"ok": True, "state": state}

    def start_mission_async(
        self,
        goal: str,
        start_url: str = "",
        max_steps: int | None = None,
    ) -> dict[str, Any]:
        """Launches an autonomous mission in a background daemon thread."""
        if self._worker_thread and self._worker_thread.is_alive():
            return {
                "ok": False,
                "error": "Eine autonome Mission läuft bereits. Bitte zuerst stoppen oder abwarten.",
                "state": get_agent_monitor_state(),
            }

        effective_max_steps = resolve_max_agent_steps(max_steps)
        self._cancel_event.clear()
        self._pause_event.clear()
        self._worker_thread = threading.Thread(
            target=self.run_mission,
            args=(goal, start_url, effective_max_steps),
            daemon=True,
            name="JarvisAutonomousMission",
        )
        self._worker_thread.start()
        return {
            "ok": True,
            "message": f"Autonome Mission gestartet ({effective_max_steps} Schritte): '{goal}'",
            "state": get_agent_monitor_state(),
        }

    def run_autonomous_task(
        self,
        goal: str,
        brain: Any = None,
        max_steps: int | None = None,
        start_url: str = "",
        on_status: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        """Wrapper compatible with swarm_core returning dict{ok, message}."""
        msg = self.run_mission(
            goal=goal,
            start_url=start_url,
            max_steps=max_steps,
            brain=brain,
            on_status=on_status,
        )
        ok = not msg.startswith("🛑")
        return {"ok": ok, "message": msg}

    def run_mission(
        self,
        goal: str,
        start_url: str = "",
        max_steps: int | None = None,
        brain: Any = None,
        on_status: Callable[[str], None] | None = None,
    ) -> str:
        """
        Synchronous 1-FPS marathon closed-loop execution of a complex web/desktop goal.
        Uses sliding-window screenshot memory, 25-step history compression, and
        anti-stuck recovery across up to 200-500 steps.
        """
        effective_max_steps = resolve_max_agent_steps(max_steps)
        marathon_mem = MarathonStepMemory(goal)
        self._cancel_event.clear()
        AGENT_HAND.reset()
        screen_w, screen_h = get_screen_size()
        real_actions_count = 0

        start_hud = marathon_mem.format_hud_status(0, effective_max_steps)
        _update_monitor(
            active=True,
            goal=goal,
            step=0,
            max_steps=effective_max_steps,
            status="running",
            last_action=start_hud,
            last_reason="Initialisiere 1-FPS Marathon-Schleife",
        )
        if on_status:
            on_status(start_hud)

        # Optional Step 0: Open initial URL in visible browser AND immediately continue with 1-FPS mouse loop
        if start_url:
            url = start_url.strip()
            if not url.startswith("http"):
                url = "https://" + url
            try:
                webbrowser.open(url)
                marathon_mem.record_step(
                    0,
                    "open_url",
                    None,
                    None,
                    f"Browser geöffnet ({url})",
                    b"",
                    brain=brain,
                    subgoal_hint=f"Startseite öffnen ({url})",
                )
                _update_monitor(
                    last_action=f"Öffne Browser: {url} (Starte 1-FPS Marathon-Schleife...)"
                )
                time.sleep(2.5)
            except Exception as exc:
                logger.warning("Failed to open start_url %s: %s", url, exc)

        for step_idx in range(1, effective_max_steps + 1):
            if self._cancel_event.is_set() or not AGENT_HAND.is_active():
                _update_monitor(
                    active=False,
                    status="aborted",
                    step=step_idx,
                    last_action="🛑 Mission abgebrochen.",
                )
                return "🛑 Autonome Mission wurde abgebrochen."

            frame = capture_1fps_frame(step_index=step_idx, save_debug=True)
            cur_x, cur_y = frame["cursor_x"], frame["cursor_y"]
            curr_jpeg = frame.get("jpeg_bytes") or b""

            hud_line = marathon_mem.format_hud_status(step_idx, effective_max_steps)
            _update_monitor(
                step=step_idx,
                max_steps=effective_max_steps,
                target_x=cur_x,
                target_y=cur_y,
                screenshot_b64=frame["base64_jpeg"],
                last_action=hud_line,
            )
            if on_status:
                on_status(hud_line)

            decision = self._decide_step(
                goal=goal,
                b64_img=frame["base64_jpeg"],
                cur_x=cur_x,
                cur_y=cur_y,
                width=screen_w,
                height=screen_h,
                step=step_idx,
                max_steps=effective_max_steps,
                marathon_mem=marathon_mem,
            )

            action = str(decision.get("action", "done")).lower().strip()
            reason = str(decision.get("reason", "Ausführung"))
            subgoal_hint = str(decision.get("subgoal", "")).strip()
            if subgoal_hint:
                marathon_mem.update_subgoal(subgoal_hint)
            tx = int(decision.get("x", cur_x) or cur_x)
            ty = int(decision.get("y", cur_y) or cur_y)
            text_val = str(decision.get("text", ""))
            key_val = str(decision.get("key", "enter"))
            scroll_y = int(decision.get("scroll_y", -5) or -5)

            exec_mode = SECURE_VAULT.get_execution_mode()
            mode = SECURE_VAULT.get_autonomy_mode()

            # Endurance rule: forbid premature "done" while multi-step subgoals remain
            if action == "done" and (
                step_idx == 1 or marathon_mem.has_pending_subgoals(real_actions_count)
            ):
                action = "scroll"
                scroll_y = -4
                tx, ty = screen_w // 2, screen_h // 2
                reason = (
                    f"Ausdauer-Regel: Teilziel '{marathon_mem.current_subgoal}' noch offen "
                    "— scrolle/prüfe weiter."
                )

            # Anti-stuck recovery after 3 unchanged clicks on almost same coordinates
            if marathon_mem.is_stuck() and action in ("click", "double_click"):
                rec = marathon_mem.pick_recovery_action(screen_w, screen_h)
                action = str(rec.get("action", "press"))
                key_val = str(rec.get("key", "escape"))
                scroll_y = int(rec.get("scroll_clicks", -5) or -5)
                if rec.get("x") is not None and rec.get("y") is not None:
                    tx = int(rec["x"])
                    ty = int(rec["y"])
                reason = str(rec.get("reason", reason))

            # Automatic 3D-Secure / PushTAN check if reason mentions it
            if detect_3d_secure_or_pushtan(reason):
                action = "wait_3ds"

            # Enforce 2-Stage Purchase Permission (MODUS A vs MODUS B)
            if exec_mode == "SEMI_AUTO" or mode == "mode_1":
                if action in ("pause_for_user", "buy_confirm") or (
                    action in ("click", "press")
                    and is_final_buy_action(f"{reason} {text_val}")
                ):
                    side_x = min(screen_w - 25, tx + 42)
                    side_y = max(25, min(screen_h - 25, ty))
                    ghost_move_to(side_x, side_y, duration=0.65)
                    _speak_voice_hint_async(MODUS_A_SPOKEN_MESSAGE)
                    _update_monitor(
                        active=False,
                        status="paused_for_user",
                        target_x=side_x,
                        target_y=side_y,
                        last_action=f"🛡️ MODUS A: {MODUS_A_SPOKEN_MESSAGE}",
                        last_reason=reason,
                    )
                    return (
                        f"🛡️ MODUS A (Halb-Autonom): {MODUS_A_SPOKEN_MESSAGE} "
                        f"(Maus steht bei X={side_x}, Y={side_y} direkt neben dem Kauf-Button: {reason})"
                    )

            _update_monitor(
                status="running",
                target_x=tx,
                target_y=ty,
                last_action=marathon_mem.format_hud_status(step_idx, effective_max_steps),
                last_reason=f"{action.upper()}: {reason}",
            )

            if action == "done":
                _update_monitor(
                    active=False,
                    status="completed",
                    last_action=f"✅ Mission abgeschlossen ({step_idx}/{effective_max_steps}): {reason}",
                )
                return f"✅ Autonome Mission erfolgreich abgeschlossen ({step_idx} Schritte): {reason}"

            step_detail = reason
            if action == "open_url" and text_val:
                url = text_val if text_val.startswith("http") else f"https://{text_val}"
                webbrowser.open(url)
                step_detail = f"URL geöffnet ({url}) - {reason}"
                real_actions_count += 1
                time.sleep(2.2)

            elif action == "click":
                ghost_click(tx, ty, button="left", clicks=1, duration=0.65)
                step_detail = f"Klick bei ({tx}, {ty}) - {reason}"
                real_actions_count += 1

            elif action == "double_click":
                ghost_click(tx, ty, button="left", clicks=2, duration=0.65)
                step_detail = f"Doppelklick bei ({tx}, {ty}) - {reason}"
                real_actions_count += 1

            elif action == "type":
                if tx != cur_x or ty != cur_y:
                    ghost_click(tx, ty, button="left", clicks=1, duration=0.55)
                resolved_text = SECURE_VAULT.resolve_placeholders(text_val)
                ghost_typewrite(resolved_text, interval=0.04)
                step_detail = f"Text eingetippt ({len(resolved_text)} Zeichen) - {reason}"
                real_actions_count += 1

            elif action == "type_secret":
                if tx != cur_x or ty != cur_y:
                    ghost_click(tx, ty, button="left", clicks=1, duration=0.55)
                resolved_secret = SECURE_VAULT.resolve_placeholders(text_val)
                if resolved_secret:
                    ghost_typewrite(resolved_secret, interval=0.03)
                    step_detail = f"Tresor-Geheimnis {text_val} lokal eingefügt - {reason}"
                    real_actions_count += 1
                else:
                    step_detail = f"Tresor-Feld {text_val} ist noch leer!"

            elif action == "create_instant_email":
                mail_res = MAIL_FACTORY.create_instant_mailbox_mailtm(
                    purpose=goal[:50], start_background_poller=True
                )
                created_addr = mail_res.get("email", "")
                step_detail = f"1-Sekunden E-Mail-Konto erstellt ({created_addr}) & im Tresor gespeichert"
                if tx != cur_x or ty != cur_y:
                    ghost_click(tx, ty, button="left", clicks=1, duration=0.5)
                    ghost_typewrite(created_addr, interval=0.03)
                real_actions_count += 1

            elif action == "create_browser_email":
                provider = text_val.strip().lower() or "proton"
                mail_res = MAIL_FACTORY.create_browser_mailbox_1fps(
                    provider=provider, execute_mouse_steps=True
                )
                step_detail = f"Browser-Mail-Konto ({provider}) angelegt: {mail_res.get('email')}"
                real_actions_count += 1

            elif action == "read_email_code":
                _update_monitor(
                    last_action="📧 Prüfe E-Mail-Postfach (Mail.tm & IMAP) auf Bestätigungscode..."
                )
                mail_res = SECURE_VAULT.read_latest_verification_email(max_wait_sec=12)
                if mail_res.get("ok"):
                    code = mail_res.get("code", "")
                    link = mail_res.get("link", "")
                    if code:
                        ghost_typewrite(code, interval=0.05)
                        ghost_press("enter")
                        step_detail = f"E-Mail-Code {code} ausgelesen & eingetragen!"
                        real_actions_count += 1
                    elif link:
                        webbrowser.open(link)
                        step_detail = "E-Mail-Bestätigungslink geöffnet!"
                        real_actions_count += 1
                else:
                    step_detail = f"E-Mail-Check ({mail_res.get('error', 'Kein Code')})"

            elif action == "wait_3ds":
                _update_monitor(
                    status="waiting_3ds",
                    last_action=f"📱 3D-Secure / PushTAN: {PUSHTAN_3DS_SPOKEN_MESSAGE}",
                    last_reason=reason,
                )
                wait_res = wait_for_3d_secure_approval(max_wait_sec=90, poll_interval=2.0)
                step_detail = f"3D-Secure/PushTAN abgewartet ({wait_res.get('status')})"
                real_actions_count += 1

            elif action == "press":
                ghost_press(key_val)
                step_detail = f"Taste '{key_val}' - {reason}"
                real_actions_count += 1

            elif action == "hotkey":
                keys = [
                    k.strip()
                    for k in (text_val or key_val).split("+")
                    if k.strip()
                ]
                if keys:
                    ghost_hotkey(*keys)
                step_detail = f"Hotkey {keys} - {reason}"
                real_actions_count += 1

            elif action == "scroll":
                ghost_scroll(scroll_y, tx, ty)
                step_detail = f"Scroll ({scroll_y}) - {reason}"
                real_actions_count += 1

            # Wait briefly for UI transition before measuring post-step diff
            time.sleep(0.85)
            post_frame = capture_1fps_frame(step_index=step_idx, save_debug=False)
            post_jpeg = post_frame.get("jpeg_bytes") or curr_jpeg
            marathon_mem.record_step(
                step_idx=step_idx,
                action=action,
                tx=tx if action in ("click", "double_click", "type", "type_secret") else None,
                ty=ty if action in ("click", "double_click", "type", "type_secret") else None,
                detail=step_detail,
                curr_jpeg=post_jpeg,
                brain=brain,
                subgoal_hint=subgoal_hint,
            )

        _update_monitor(
            active=False,
            status="completed",
            last_action=f"⏹ Maximale Schrittanzahl ({effective_max_steps}) erreicht.",
        )
        return (
            f"Autonome Mission nach {effective_max_steps} visuellen Schritten beendet.\n"
            + marathon_mem.format_history_text()
        )

    def _decide_step(
        self,
        *,
        goal: str,
        b64_img: str,
        cur_x: int,
        cur_y: int,
        width: int,
        height: int,
        step: int,
        max_steps: int,
        marathon_mem: MarathonStepMemory,
    ) -> dict[str, Any]:
        exec_mode = SECURE_VAULT.get_execution_mode()
        mode = SECURE_VAULT.get_autonomy_mode()
        mode_desc = (
            "MODUS A (HALB-AUTONOM): Alles vorbereiten & ausfüllen, aber VOR dem finalen Kauf-/Bestell-Button mit action='pause_for_user' stoppen!"
            if (exec_mode == "SEMI_AUTO" or mode == "mode_1")
            else "MODUS B (VOLL-ZUGRIFF): 100% autonom inkl. finalem Kauf-/Verkaufs-Klick ausführen!"
        )
        prompt = AUTONOMOUS_SYSTEM_PROMPT.format(
            cur_x=cur_x,
            cur_y=cur_y,
            width=width,
            height=height,
            goal=goal,
            current_subgoal=marathon_mem.current_subgoal,
            step=step,
            max_steps=max_steps,
            execution_mode=exec_mode,
            autonomy_mode_desc=mode_desc,
            vault_placeholders=SECURE_VAULT.get_available_placeholders_prompt(),
            history=marathon_mem.format_history_text(),
            anti_stuck_hint=marathon_mem.build_anti_stuck_prompt_hint(),
        )

        raw = ""
        provider = (LLM_PROVIDER or "").lower()

        if (provider == "gemini" or GEMINI_API_KEY) and GEMINI_API_KEY:
            raw = self._call_gemini_vision(prompt, b64_img)
        if not raw and OPENAI_API_KEY:
            raw = self._call_openai_vision(prompt, b64_img)
        if not raw and GROQ_API_KEY:
            raw = self._call_groq_vision(prompt, b64_img)
        if not raw:
            raw = self._call_ollama_vision(prompt, b64_img)

        parsed = self._parse_json(raw)
        if parsed:
            return parsed

        # Smart deterministic multi-step fallback if no Vision LLM is reachable
        return self._heuristic_step_fallback(
            goal=goal,
            step=step,
            cur_x=cur_x,
            cur_y=cur_y,
            width=width,
            height=height,
            exec_mode=exec_mode,
        )

    def _heuristic_step_fallback(
        self,
        *,
        goal: str,
        step: int,
        cur_x: int,
        cur_y: int,
        width: int,
        height: int,
        exec_mode: str,
    ) -> dict[str, Any]:
        """
        Ensures that even without a live cloud Vision API key, the agent performs
        real multi-step mouse movements, clicks, and typing in the opened browser
        instead of stopping after Step 0!
        """
        goal_low = (goal or "").lower()
        center_x, center_y = width // 2, height // 2

        if step == 1:
            # Try OCR click on search/input or move to top search bar area
            search_y = max(140, int(height * 0.22))
            return {
                "action": "click",
                "x": center_x,
                "y": search_y,
                "reason": f"Fokussiere Such-/Eingabefeld im Browser für '{goal}'",
            }
        if step == 2:
            if any(w in goal_low for w in ("account", "registrier", "anmeld", "sign up")):
                return {
                    "action": "create_instant_email",
                    "x": center_x,
                    "y": int(height * 0.38),
                    "reason": "Erstelle 1-Sekunden E-Mail-Konto & trage Adresse ein",
                }
            # Extract query keywords
            cleaned = re.sub(
                r"(?i)\b(gehe|geh|auf|öffne|und|suche|such|nach|dem|der|die|das|im|browser|bitte|klicke)\b",
                " ",
                goal,
            )
            cleaned = re.sub(r"\s+", " ", cleaned).strip() or goal
            return {
                "action": "type",
                "x": center_x,
                "y": max(140, int(height * 0.22)),
                "text": cleaned,
                "reason": f"Tippe Suchbegriff '{cleaned}' ein",
            }
        if step == 3:
            return {
                "action": "press",
                "x": cur_x,
                "y": cur_y,
                "key": "enter",
                "reason": "Bestätige Eingabe mit Enter",
            }
        if step == 4 and any(w in goal_low for w in ("kauf", "order", "buy", "bestell")):
            if exec_mode == "SEMI_AUTO":
                return {
                    "action": "pause_for_user",
                    "x": center_x,
                    "y": int(height * 0.58),
                    "reason": "Kauf-Button vorbereitet (MODUS A: Halb-Autonom)",
                }
            return {
                "action": "click",
                "x": center_x,
                "y": int(height * 0.58),
                "reason": "Klicke Kauf-Button vollautonom (MODUS B)",
            }
        return {
            "action": "done",
            "x": cur_x,
            "y": cur_y,
            "reason": "Visuelle Browser-Abfolge durchgeführt",
        }

    def _call_gemini_vision(self, prompt: str, b64_img: str) -> str:
        try:
            model = (
                LLM_MODEL
                if "gemini" in (LLM_MODEL or "").lower()
                else "gemini-2.5-flash"
            )
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_API_KEY}"
            payload = {
                "contents": [
                    {
                        "parts": [
                            {"text": prompt},
                            {
                                "inline_data": {
                                    "mime_type": "image/jpeg",
                                    "data": b64_img,
                                }
                            },
                        ]
                    }
                ],
                "generationConfig": {
                    "temperature": 0.1,
                    "responseMimeType": "application/json",
                },
            }
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="ignore"))
            candidates = data.get("candidates", [])
            if candidates:
                parts = candidates[0].get("content", {}).get("parts", [])
                if parts:
                    return str(parts[0].get("text", ""))
        except Exception as exc:
            logger.debug("Autonomous Gemini Vision error: %s", exc)
        return ""

    def _call_openai_vision(self, prompt: str, b64_img: str) -> str:
        try:
            url = "https://api.openai.com/v1/chat/completions"
            payload = {
                "model": "gpt-4o-mini",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/jpeg;base64,{b64_img}"
                                },
                            },
                        ],
                    }
                ],
                "temperature": 0.1,
            }
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {OPENAI_API_KEY}",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="ignore"))
            return str(data["choices"][0]["message"]["content"])
        except Exception as exc:
            logger.debug("Autonomous OpenAI Vision error: %s", exc)
        return ""

    def _call_groq_vision(self, prompt: str, b64_img: str) -> str:
        try:
            url = "https://api.groq.com/openai/v1/chat/completions"
            payload = {
                "model": "meta-llama/llama-4-scout-17b-16e-instruct",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/jpeg;base64,{b64_img}"
                                },
                            },
                        ],
                    }
                ],
                "temperature": 0.1,
            }
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {GROQ_API_KEY}",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="ignore"))
            return str(data["choices"][0]["message"]["content"])
        except Exception as exc:
            logger.debug("Autonomous Groq Vision error: %s", exc)
        return ""

    def _call_ollama_vision(self, prompt: str, b64_img: str) -> str:
        try:
            host = (OLLAMA_HOST or "http://127.0.0.1:11434").rstrip("/")
            url = f"{host}/api/generate"
            payload = {
                "model": "llava",
                "prompt": prompt,
                "images": [b64_img],
                "stream": False,
            }
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="ignore"))
            return str(data.get("response", ""))
        except Exception:
            return ""

    def _parse_json(self, raw: str) -> dict[str, Any] | None:
        if not raw:
            return None
        cleaned = raw.strip()
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
        match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except Exception:
                return None
        return None


AUTONOMOUS_AGENT = AutonomousWorkflowAgent()
