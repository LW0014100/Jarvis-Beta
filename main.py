"""Hauptschleife, GUI-Steuerung und Multi-Agenten-Orchestrierung für Jarvis."""

from __future__ import annotations

import ctypes
import platform

if platform.system() == "Windows":
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

from datetime import datetime
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time
from typing import TYPE_CHECKING, Any, Optional
import urllib.request

# AUFGABE 1 (Turbo-Start < 1,5s): Ganz oben NUR leichtgewichtige Basis-Module laden!
import fastapi  # noqa: F401
import uvicorn  # noqa: F401

from dotenv import load_dotenv

from beta_guard import BETA_GUARD
from config import (
    clean_api_key,
    default_ollama_text_model,
    detect_online_mode_command,
    ensure_env_file,
    get_env_path,
    get_is_online,
    get_subprocess_hide_flags,
    get_user_data_dir,
    sanitize_gemini_model,
    sanitize_groq_model,
    set_online_mode,
    sync_config_json,
    sync_env_aliases,
)

# .env direkt beim Start aus dem portablen Nutzerdaten-Verzeichnis laden
PROJECT_ENV_PATH = get_env_path()
if PROJECT_ENV_PATH.exists():
    load_dotenv(dotenv_path=PROJECT_ENV_PATH, override=True)
from gui import JarvisGUI, enable_windows_high_dpi

if TYPE_CHECKING:
    from agents import AgentRouter
    from alarm import AlarmManager
    from audio_in import AudioRecorder, TerminalTrigger
    from brain import JarvisBrain
    from memory import LongTermMemory
    from stt import SpeechToText
    from tts import SpeechStreamSession, TextToSpeech
    from vision import CameraVision


WAKE_VARIANTS = (
    "jarvis",
    "dscharvis",
    "charvis",
    "jarwis",
    "jarves",
    "djarvis",
    "dschawis",
    "dscharwis",
    "jawis",
    "javis",
    "tschawis",
    "tscharvis",
    "sharvis",
)
CONVERSATION_WINDOW_SECONDS = 15.0
PROJECT_NOTE_PATTERN = re.compile(
    r"\b(?:mach(?:e)?\s+eine\s+projektnotiz|projektnotiz(?:\s+anlegen|\s+erstellen)?)\b[:,.\s-]*(.*)$",
    flags=re.IGNORECASE,
)
MORNING_GREETING_PATTERN = re.compile(
    r"\b(?:hallo|guten\s+morgen|morgen|moin|servus|bin\s+wach|erzähl|los\s+geht)\b",
    flags=re.IGNORECASE,
)


def _is_morning_briefing_due(memory: LongTermMemory) -> bool:
    hour = datetime.now().hour
    return 5 <= hour < 12 and not memory.was_briefing_done_today()


def _parse_device(env_value: Optional[str]) -> Optional[int | str]:
    if not env_value or not env_value.strip():
        return None
    raw = env_value.strip()
    return int(raw) if raw.isdigit() else raw


def _wake_regex(wake_word: str) -> re.Pattern[str]:
    variants = list(dict.fromkeys([wake_word.lower(), *WAKE_VARIANTS]))
    return re.compile(r"\b(?:" + "|".join(re.escape(v) for v in variants) + r")\b", flags=re.IGNORECASE)


def _contains_wake_word(transcript: str, wake_word: str) -> bool:
    """Prüft, ob 'Jarvis' (oder ähnliche Schreibweise) irgendwo im Satz vorkommt."""
    return bool(transcript and _wake_regex(wake_word).search(transcript))


def _has_command_beyond_wake(transcript: str, wake_word: str) -> bool:
    """Prüft, ob der Satz neben Begrüßung und 'Jarvis' bereits einen echten Befehl enthält."""
    stripped = _wake_regex(wake_word).sub(" ", transcript)
    stripped = re.sub(
        r"\b(?:hey|hi|hallo|guten\s+morgen|morgen|moin|servus|bitte|ja|okay|ok)\b",
        " ",
        stripped,
        flags=re.IGNORECASE,
    )
    stripped = re.sub(r"[,.:;!?-]+", " ", stripped).strip()
    return len(stripped) >= 3


def _extract_command_from_transcript(transcript: str, wake_word: str) -> str:
    """Extrahiert ab dem Satz mit 'Jarvis' den vollständigen Befehl inkl. Sätzen vor/nach dem Wake-Word."""
    cleaned = transcript.strip()
    if not cleaned:
        return ""
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", cleaned) if s.strip()]
    if len(sentences) <= 1:
        return cleaned

    wake_pat = _wake_regex(wake_word)
    for idx, sent in enumerate(sentences):
        if wake_pat.search(sent):
            # Falls direkt vor einem reinen "Und .../Bitte ..., Jarvis" ein zugehöriger Teilsatz steht, beibehalten
            if idx > 0 and re.match(r"^(?:und|oder|bitte|dann|noch|mal)\b", sent, flags=re.IGNORECASE):
                return " ".join(sentences[idx - 1 :]).strip()
            return " ".join(sentences[idx:]).strip()
    return cleaned


def _needs_ring_context(transcript: str, wake_word: str) -> bool:
    """Prüft, ob vor dem kurzen Segment im 30s-Ringspeicher wahrscheinlich noch Satzanfang liegt."""
    if not _has_command_beyond_wake(transcript, wake_word):
        return True
    first_word = transcript.strip().lstrip(" ,.-").split(maxsplit=1)[0].lower() if transcript.strip() else ""
    return first_word in ("und", "oder", "aber", "bitte", "dann", "noch")


def _set_ui_status(gui: Optional[JarvisGUI], status: str) -> None:
    print(f"[{status}]")
    if gui is not None:
        gui.set_status(status)
        if status != "Bereit":
            gui.add_log(f"Status: {status}")


def _sync_mode_ui(
    is_online: bool,
    brain: JarvisBrain,
    gui: Optional[JarvisGUI],
) -> None:
    """Aktualisiert Kippschalter und Modell-Badge im HUD konsistent."""
    if gui is None:
        return
    gui.update_online_switch(is_online)
    if not is_online:
        gui.set_model_label("Ollama (Lokal)", brain.ollama_text_model)
    else:
        provider, model = brain.active_online_provider()
        gui.set_model_label(provider, model)


def _apply_online_mode(
    is_online: bool,
    brain: JarvisBrain,
    tts: TextToSpeech,
    gui: Optional[JarvisGUI],
    announce: bool = False,
) -> str:
    """Schaltet alle Module zentral über set_online_mode um und aktualisiert das HUD."""
    set_online_mode(is_online)
    _sync_mode_ui(is_online, brain, gui)
    reply = "Online-Systeme aktiviert." if is_online else "Wechsle in den autarken Offline-Modus."
    if gui is not None:
        gui.add_log("Modus umgeschaltet: ONLINE MODE" if is_online else "Modus umgeschaltet: OFFLINE MODE (Lokal)")
        gui.set_jarvis_text(reply)

    if announce:
        def _speak_async() -> None:
            _set_ui_status(gui, "Spreche")
            try:
                tts.speak(reply)
            finally:
                _set_ui_status(gui, "Bereit")

        threading.Thread(target=_speak_async, daemon=True).start()

    return reply


def _handle_mode_command(
    query: str,
    brain: JarvisBrain,
    tts: TextToSpeech,
    gui: Optional[JarvisGUI],
) -> bool:
    """Fängt Modus-Sprachbefehle vor dem LLM-Aufruf (< 0,1s) ab."""
    requested_online = detect_online_mode_command(query)
    if requested_online is None:
        return False

    reply = _apply_online_mode(requested_online, brain, tts, gui, announce=False)
    _set_ui_status(gui, "Spreche")
    tts.speak(reply)
    return True


def _handle_project_note(
    inline_description: str,
    recorder: AudioRecorder,
    stt: SpeechToText,
    vision: CameraVision,
    memory: LongTermMemory,
    tts: TextToSpeech,
    gui: Optional[JarvisGUI],
) -> None:
    description = inline_description.strip()
    if len(description) < 4:
        prompt_text = "Sehr wohl, Chef. Kamera ist bereit – bitte beschreiben Sie kurz Ihre Projektnotiz."
        if gui is not None:
            gui.set_jarvis_text(prompt_text)
        _set_ui_status(gui, "Spreche")
        tts.speak(prompt_text)

        _set_ui_status(gui, "Höre zu")
        audio = recorder.record_utterance(
            max_seconds=15.0,
            silence_duration=1.2,
            wait_for_speech_timeout=9.0,
        )
        if audio is not None:
            _set_ui_status(gui, "Verarbeite Sprache")
            description = stt.transcribe(audio).strip()

    if not description:
        description = "Werkstatt-Schnappschuss ohne gesprochene Beschreibung."

    _set_ui_status(gui, "Kamera aktiv")
    image_bytes = vision.get_current_frame(archive=False)
    if image_bytes is not None and gui is not None:
        gui.update_camera_preview(image_bytes)
    from tools import save_project_log_entry

    log_path = save_project_log_entry(description, image_bytes)
    memory.remember(description, category="project")

    reply = (
        f"Sehr wohl, Chef. Die Projektnotiz wurde samt Foto unter {log_path.name} "
        "und im Langzeitgedächtnis gespeichert."
    )
    print(f"[Logbuch] {log_path}")
    if gui is not None:
        gui.set_model_label("Projekt-Logbuch", str(log_path.parent))
        gui.set_jarvis_text(reply)
    _set_ui_status(gui, "Spreche")
    tts.speak(reply)


def _handle_query(
    query: str,
    recorder: AudioRecorder,
    stt: SpeechToText,
    vision: CameraVision,
    brain: JarvisBrain,
    router: AgentRouter,
    memory: LongTermMemory,
    tts: TextToSpeech,
    gui: Optional[JarvisGUI],
    wake_word: str = "jarvis",
) -> None:
    print(f"[Chef] {query}")
    try:
        from macro_engine import EVENT_TICKER

        EVENT_TICKER.emit("voice", "stt", f"Eingabe: {query[:90]}")
    except Exception:
        pass
    if gui is not None:
        gui.set_user_text(query)
        gui.add_log(f"Chef: {query[:60]}")

    if _handle_mode_command(query, brain, tts, gui):
        return

    if _is_morning_briefing_due(memory) and (
        not _has_command_beyond_wake(query, wake_word)
        or (MORNING_GREETING_PATTERN.search(query) and len(query.split()) <= 5)
    ):
        _deliver_wake_up_briefing(False, brain, memory, tts, gui)
        return

    note_match = PROJECT_NOTE_PATTERN.search(query)
    if note_match:
        _handle_project_note(note_match.group(1), recorder, stt, vision, memory, tts, gui)
        return

    started_at = time.monotonic()
    image_bytes: Optional[bytes] = None
    from multi_monitor import capture_monitor_jpeg, extract_monitor_target_from_text
    from vision import should_capture_screen, should_capture_webcam

    explicit_mon = extract_monitor_target_from_text(query)
    if should_capture_webcam(query) and explicit_mon is None:
        _set_ui_status(gui, "Kamera aktiv")
        image_bytes = vision.get_current_frame(archive=True)
        if image_bytes is None:
            print("[Warnung: On-Demand-Kamerabild nicht verfügbar – fahre mit Text fort]")
    elif should_capture_screen(query) or (
        explicit_mon is not None
        and re.search(r"\b(schau|sieh|guck|zeig|was\s+ist|analysier|prüf)\b", query, flags=re.IGNORECASE)
    ):
        # Cursor-Aware Vision: Standardmäßig nur aktiven Maus-Monitor in voller Auflösung erfassen
        target_mon = explicit_mon if explicit_mon is not None else "cursor"
        if gui is not None:
            gui.emit_brain_activity(state="screen_scan", intensity=0.92, active_monitor=target_mon, detail=f"SCAN MONITOR {target_mon}")
        cap = capture_monitor_jpeg(
            target=target_mon,
            quality=88,
            max_width=1440 if target_mon == "all" else 0,
            with_grid=False,
        )
        if cap.get("ok") and cap.get("jpeg_bytes"):
            image_bytes = bytes(cap["jpeg_bytes"])
            if gui is not None:
                gui.emit_neural_thought(category="vision", tool="capture_monitor_screen", summary=str(cap.get("summary") or ""), monitor=cap.get("target"))
        else:
            from agent_hand import VISUAL_HAND_AGENT

            image_bytes = VISUAL_HAND_AGENT.get_latest_passive_screen_bytes(capture_if_missing=True)
        if image_bytes is None:
            print("[Warnung: Desktop-Screen-Snapshot nicht verfügbar – fahre mit Text fort]")

    _set_ui_status(gui, "Denke nach")
    if gui is not None:
        gui.emit_neural_thought(category="thought", tool="router.handle", summary=f"Verarbeite Anfrage: {query[:90]}")

    stream_session: Optional[SpeechStreamSession] = None
    streamed_chunks: list[str] = []
    first_token_at: Optional[float] = None
    last_ui_update = 0.0
    result = None

    def _on_provider(provider: str, model: str) -> None:
        if gui is not None:
            gui.set_model_label(provider, model)

    def _on_token(token: str) -> None:
        nonlocal stream_session, first_token_at, last_ui_update
        if first_token_at is None:
            first_token_at = time.monotonic()
            if gui is not None:
                gui.set_response_time(first_token_at - started_at)
        if stream_session is None:
            stream_session = tts.start_stream(
                on_playback_start=lambda: _set_ui_status(gui, "Spreche")
            )
        streamed_chunks.append(token)
        stream_session.push_token(token)
        if gui is not None:
            now = time.monotonic()
            if (now - last_ui_update) >= 0.06 or token.endswith((".", "!", "?", "\n")):
                gui.set_jarvis_text("".join(streamed_chunks))
                last_ui_update = now

    try:
        try:
            result = router.handle(
                user_text=query,
                webcam_image=image_bytes,
                on_status=lambda st: _set_ui_status(gui, st),
                on_enter_screen_mode=gui.enter_screen_control if gui is not None else None,
                on_exit_screen_mode=gui.exit_screen_control if gui is not None else None,
                on_provider=_on_provider,
                on_token=_on_token,
            )

            if first_token_at is None and gui is not None:
                gui.set_response_time(time.monotonic() - started_at)

            print(f"[Jarvis | {result.provider} ({result.model})] {result.text}")
            if gui is not None:
                gui.set_model_label(result.provider, result.model)
                gui.set_jarvis_text(result.text)
                gui.add_log(f"Jarvis ({result.provider}): {result.text[:55]}")
                gui.emit_neural_thought(
                    category="thought",
                    tool=f"{result.provider}/{result.model}",
                    summary=result.text[:220],
                )
        except Exception as exc:
            err_msg = f"Entschuldigung Chef, bei der Anfrage gab es einen Fehler: {exc}"
            print(f"[Query-Fehler] {exc}", file=sys.stderr)
            if gui is not None:
                gui.set_jarvis_text(err_msg)
                gui.add_log(f"Fehler: {exc}")
        finally:
            if stream_session is not None:
                try:
                    stream_session.finish(wait=True)
                except Exception as exc:
                    print(f"[TTS-Stream-Finish-Fehler] {exc}", file=sys.stderr)
                    try:
                        stream_session.stop()
                    except Exception:
                        pass

        if stream_session is None and result is not None and result.text:
            _set_ui_status(gui, "Spreche")
            try:
                tts.speak(result.text)
            except Exception as exc:
                print(f"[TTS-Speak-Fehler] {exc}", file=sys.stderr)
    finally:
        try:
            tts.speaking_event.clear()
        except Exception:
            pass
        try:
            recorder.resume_listening()
        except Exception:
            pass
        _set_ui_status(gui, "Bereit")


def _record_and_handle(
    recorder: AudioRecorder,
    stt: SpeechToText,
    vision: CameraVision,
    brain: JarvisBrain,
    router: AgentRouter,
    memory: LongTermMemory,
    tts: TextToSpeech,
    gui: Optional[JarvisGUI],
    wake_word: str = "jarvis",
) -> None:
    _set_ui_status(gui, "Höre zu")
    query_audio = recorder.record_utterance(
        max_seconds=15.0,
        silence_duration=1.1,
        wait_for_speech_timeout=8.0,
    )
    if query_audio is None:
        print("[Hinweis] Keine Sprache erkannt.")
        return

    _set_ui_status(gui, "Verarbeite Sprache")
    query_text = stt.transcribe(query_audio)
    if not query_text:
        print("[Hinweis] Konnte nichts verstehen.")
        return

    _handle_query(query_text, recorder, stt, vision, brain, router, memory, tts, gui, wake_word=wake_word)


def _thermal_monitor_loop(
    stop_event: threading.Event,
    tts: TextToSpeech,
    gui: Optional[JarvisGUI],
    threshold_c: float = 80.0,
) -> None:
    from tools import get_cpu_temperature_celsius

    warned = False
    while not stop_event.wait(20.0):
        temp = get_cpu_temperature_celsius()
        if temp is None:
            continue
        if temp > threshold_c and not warned:
            warned = True
            alert = (
                f"Warnung, Chef. Die Prozessortemperatur liegt bei {temp:.1f} Grad Celsius. "
                "Bitte prüfen Sie die Kühlung."
            )
            print(f"[Thermische Warnung] {alert}")
            if gui is not None:
                gui.set_jarvis_text(alert)
                _set_ui_status(gui, "Fehler")
            tts.speak(alert)
            _set_ui_status(gui, "Bereit")
        elif temp < (threshold_c - 8.0):
            warned = False


HIGH_ENERGY_BRIEFING_PROMPT = (
    "Du bist Jarvis aus den Iron-Man-Filmen und weckst den Chef gerade auf. "
    "Sprich extrem energiegeladen, motivierend, souverän und charmant-sarkastisch auf Deutsch. "
    "Sprich den Nutzer mit 'Chef' an und beginne mit einem mitreißenden 'Guten Morgen, Chef!'. "
    "Falls Trolling-Modus aktiv war, mach einen kurzen frechen Kommentar zum schrillen Weckton. "
    "Fasse Wetter, Termine/Aufgaben, Aktien-Watchlist (hebe starke Gewinner oder Bewegungen hervor!) "
    "und ungelesene E-Mails flüssig zusammen. "
    "KRITISCH: Reiner Fließtext für Sprachausgabe, KEIN Markdown, KEINE Sternchen, KEINE Emojis, KEINE URLs, "
    "Zahlen und Prozentwerte ausgeschrieben."
)


def _deliver_wake_up_briefing(
    trolling_active: bool,
    brain: JarvisBrain,
    memory: LongTermMemory,
    tts: TextToSpeech,
    gui: Optional[JarvisGUI],
) -> None:
    from tools import collect_morning_briefing_data, format_fallback_morning_briefing

    started_at = time.monotonic()
    _set_ui_status(gui, "Denke nach")
    data = collect_morning_briefing_data(memory.recall(limit=5))
    payload = json.dumps({"trolling_modus_war_aktiv": trolling_active, **data}, ensure_ascii=False)

    stream_session: Optional[SpeechStreamSession] = None
    streamed_chunks: list[str] = []
    first_token_at: Optional[float] = None
    last_ui_update = 0.0

    def _on_provider(provider: str, model: str) -> None:
        if gui is not None:
            gui.set_model_label(provider, model)

    def _on_token(token: str) -> None:
        nonlocal stream_session, first_token_at, last_ui_update
        if first_token_at is None:
            first_token_at = time.monotonic()
            if gui is not None:
                gui.set_response_time(first_token_at - started_at)
        if stream_session is None:
            stream_session = tts.start_stream(
                on_playback_start=lambda: _set_ui_status(gui, "Spreche")
            )
        streamed_chunks.append(token)
        stream_session.push_token(token)
        if gui is not None:
            now = time.monotonic()
            if (now - last_ui_update) >= 0.06 or token.endswith((".", "!", "?", "\n")):
                gui.set_jarvis_text("".join(streamed_chunks))
                last_ui_update = now

    try:
        res = brain.ask_stream(
            f"Erstelle jetzt das mitreißende Morgen-Briefing aus diesen Live-Daten:\n{payload}",
            system_prompt=HIGH_ENERGY_BRIEFING_PROMPT,
            use_history=False,
            on_provider=_on_provider,
            on_token=_on_token,
        )
        if res.provider == "Kein Anbieter":
            briefing_text = format_fallback_morning_briefing(data, trolling_active)
            provider_label, model_label = "Morgen-Briefing", "Lokal"
        else:
            briefing_text = res.text
            provider_label, model_label = res.provider, res.model

        memory.mark_briefing_done_today()
        print(f"[Morgen-Briefing | {provider_label}] {briefing_text}")
        if gui is not None:
            gui.set_model_label(provider_label, model_label)
            gui.set_jarvis_text(briefing_text)
            gui.add_log("Morgen-Briefing gestartet.")
    finally:
        if stream_session is not None:
            stream_session.finish(wait=True)

    if stream_session is None:
        _set_ui_status(gui, "Spreche")
        tts.speak(briefing_text)


def _wait_for_wakeup_sign_and_brief(
    was_trolling: bool,
    stop_event: threading.Event,
    trigger: TerminalTrigger,
    recorder: AudioRecorder,
    stt: SpeechToText,
    brain: JarvisBrain,
    memory: LongTermMemory,
    tts: TextToSpeech,
    gui: Optional[JarvisGUI],
) -> None:
    """Zweistufiger Aufwach-Ablauf: Wartet nach Wecker-Stopp auf eine beliebige Reaktion des Chefs."""
    cue = "Wecker gestoppt, Chef. Sagen Sie mir einfach Bescheid oder drücken Sie die Taste für Ihr Morgen-Briefing."
    print(f"[Wecker] {cue}")
    if gui is not None:
        gui.set_jarvis_text(cue)
    _set_ui_status(gui, "Spreche")
    tts.speak(cue)

    _set_ui_status(gui, "Höre zu")
    deadline = time.monotonic() + 60.0
    while not stop_event.is_set() and time.monotonic() < deadline:
        pressed, _ = trigger.consume()
        if pressed:
            _deliver_wake_up_briefing(was_trolling, brain, memory, tts, gui)
            return

        audio = recorder.record_utterance(
            max_seconds=5.0,
            silence_duration=0.7,
            min_speech_seconds=0.2,
            wait_for_speech_timeout=0.5,
            stop_event=trigger.triggered,
        )
        if audio is None:
            continue

        spoken = stt.transcribe(audio, initial_prompt="Morgen, Bin wach, Ja erzähl, Los geht's")
        if spoken and len(spoken.strip()) >= 2:
            if gui is not None:
                gui.set_user_text(spoken)
            _deliver_wake_up_briefing(was_trolling, brain, memory, tts, gui)
            return

    print("[Wecker] Keine Reaktion innerhalb von 60 Sekunden – kehre in Bereit-Modus zurück.")


def _assistant_loop(
    stop_event: threading.Event,
    trigger: TerminalTrigger,
    gui: Optional[JarvisGUI],
    wake_word: str,
    stt: SpeechToText,
    tts: TextToSpeech,
    brain: JarvisBrain,
    router: AgentRouter,
    memory: LongTermMemory,
    alarm_manager: AlarmManager,
    recorder: AudioRecorder,
    vision: CameraVision,
) -> None:
    if gui is not None:
        _sync_mode_ui(get_is_online(), brain, gui)
        gui.set_jarvis_text("System bereit, Chef.")

    _set_ui_status(gui, "Spreche")
    tts.speak("System bereit, Chef.")

    _set_ui_status(gui, "Bereit")
    print(
        f"[Bereit] Freihändiger 30s-Ringspeicher aktiv – sprich einfach mit '{wake_word.capitalize()}' "
        "im Satz (Strg+C zum Beenden)."
    )

    conversation_until = 0.0
    in_conversation_ui = False

    while not stop_event.is_set():
        try:
            recorder.ensure_alive()

            # 0. Aktiver Wecker (stoppt per GPIO 17, Leertaste, Enter oder Sprachbefehl)
            if alarm_manager.is_ringing:
                conversation_until = 0.0
                in_conversation_ui = False
                _set_ui_status(gui, "Fehler")
                if gui is not None:
                    mode_hint = "TROLLING-MODUS!" if alarm_manager.trolling_active else "WECKER AKTIV"
                    gui.set_jarvis_text(
                        f"{mode_hint} Drücke [Leertaste]/[Enter], GPIO-Taster oder sage 'Stopp'!"
                    )

                triggered, _ = trigger.consume()
                if triggered or not alarm_manager.is_ringing:
                    was_trolling = alarm_manager.stop_alarm()
                    _wait_for_wakeup_sign_and_brief(
                        was_trolling, stop_event, trigger, recorder, stt, brain, memory, tts, gui
                    )
                    conversation_until = time.monotonic() + CONVERSATION_WINDOW_SECONDS
                    _set_ui_status(gui, "Höre zu")
                    in_conversation_ui = True
                    continue

                alarm_manager.play_alarm_burst(stop_event=trigger.triggered)

                triggered, _ = trigger.consume()
                if triggered or not alarm_manager.is_ringing:
                    was_trolling = alarm_manager.stop_alarm()
                    _wait_for_wakeup_sign_and_brief(
                        was_trolling, stop_event, trigger, recorder, stt, brain, memory, tts, gui
                    )
                    conversation_until = time.monotonic() + CONVERSATION_WINDOW_SECONDS
                    _set_ui_status(gui, "Höre zu")
                    in_conversation_ui = True
                    continue

                listen_audio = recorder.record_utterance(
                    max_seconds=3.0,
                    silence_duration=0.45,
                    min_speech_seconds=0.2,
                    wait_for_speech_timeout=0.5,
                    stop_event=trigger.triggered,
                )
                if listen_audio is not None:
                    from alarm import STOP_ALARM_PATTERN

                    spoken = stt.transcribe(listen_audio, initial_prompt="Jarvis, ich bin wach, Stopp")
                    if spoken and STOP_ALARM_PATTERN.search(spoken):
                        was_trolling = alarm_manager.stop_alarm()
                        if gui is not None:
                            gui.set_user_text(spoken)
                        _wait_for_wakeup_sign_and_brief(
                            was_trolling, stop_event, trigger, recorder, stt, brain, memory, tts, gui
                        )
                        conversation_until = time.monotonic() + CONVERSATION_WINDOW_SECONDS
                        _set_ui_status(gui, "Höre zu")
                        in_conversation_ui = True
                continue

            # 1. Optionaler manueller Shortcut (Terminal-Text, Button oder Leertaste/Enter als Backup)
            triggered, typed_text = trigger.consume()
            if triggered:
                in_conversation_ui = False
                recorder.mark_consumed()
                if typed_text:
                    _handle_query(typed_text, recorder, stt, vision, brain, router, memory, tts, gui, wake_word=wake_word)
                else:
                    _record_and_handle(recorder, stt, vision, brain, router, memory, tts, gui, wake_word=wake_word)
                time.sleep(0.2)
                conversation_until = time.monotonic() + CONVERSATION_WINDOW_SECONDS
                _set_ui_status(gui, "Höre zu")
                in_conversation_ui = True
                continue

            # 2. Konversations-Fenster prüfen (15s aktives Zuhören ohne erneutes 'Jarvis')
            now_mono = time.monotonic()
            in_conversation = now_mono < conversation_until
            if in_conversation and not in_conversation_ui:
                _set_ui_status(gui, "Höre zu")
                in_conversation_ui = True
            elif not in_conversation and in_conversation_ui:
                _set_ui_status(gui, "Bereit")
                in_conversation_ui = False

            # 3. Freihändiges Segment aus dem laufenden 30s-Ringspeicher holen (Echo-Blocking aktiv)
            utterance = recorder.next_utterance(
                timeout=1.5,
                stop_event=trigger.triggered,
                extra_stop_check=lambda: alarm_manager.is_ringing,
            )
            if utterance is None:
                continue

            speech_audio, end_seq = utterance
            if in_conversation:
                _set_ui_status(gui, "Verarbeite Sprache")

            spoken_text = stt.transcribe(
                speech_audio,
                initial_prompt="Jarvis, öffne bitte den Browser, schau dir das mal an, mach das Licht an.",
            )
            if not spoken_text:
                _set_ui_status(gui, "Höre zu" if time.monotonic() < conversation_until else "Bereit")
                continue

            # Im aktiven 15s-Konversations-Fenster gilt jeder Satz direkt als Befehl
            if in_conversation:
                recorder.mark_consumed(end_seq)
                _handle_query(spoken_text, recorder, stt, vision, brain, router, memory, tts, gui, wake_word=wake_word)
                recorder.mark_consumed()
                conversation_until = time.monotonic() + CONVERSATION_WINDOW_SECONDS
                _set_ui_status(gui, "Höre zu")
                in_conversation_ui = True
                continue

            # Im Bereit-Modus: 'Jarvis' (oder Variante) irgendwo im Satz? Sonst zurück auf Bereit!
            if not _contains_wake_word(spoken_text, wake_word):
                _set_ui_status(gui, "Bereit")
                continue

            # Bei kurzem Nachsatz wie '... , Jarvis' den vorherigen Gedankenteil aus dem 30s-Ringspeicher ergänzen
            full_spoken = spoken_text
            if _needs_ring_context(spoken_text, wake_word):
                thought_audio = recorder.get_recent_thought_audio(end_seq=end_seq, max_gap_seconds=2.4)
                if thought_audio.size > speech_audio.size + int(0.55 * 16000):
                    _set_ui_status(gui, "Verarbeite Sprache")
                    expanded = stt.transcribe(
                        thought_audio,
                        initial_prompt="Öffne bitte den Browser und such nach etwas, Jarvis.",
                    )
                    if expanded and _contains_wake_word(expanded, wake_word):
                        full_spoken = expanded

            command_text = _extract_command_from_transcript(full_spoken, wake_word)
            recorder.mark_consumed(end_seq)

            if _has_command_beyond_wake(command_text, wake_word):
                _handle_query(command_text, recorder, stt, vision, brain, router, memory, tts, gui, wake_word=wake_word)
                recorder.mark_consumed()
                conversation_until = time.monotonic() + CONVERSATION_WINDOW_SECONDS
                _set_ui_status(gui, "Höre zu")
                in_conversation_ui = True
            elif _is_morning_briefing_due(memory):
                if gui is not None:
                    gui.set_user_text(command_text)
                _deliver_wake_up_briefing(False, brain, memory, tts, gui)
                recorder.mark_consumed()
                conversation_until = time.monotonic() + CONVERSATION_WINDOW_SECONDS
                _set_ui_status(gui, "Höre zu")
                in_conversation_ui = True
            else:
                # Nur 'Jarvis' / 'Hey Jarvis' gesagt -> direkt 15s aktiv zuhören
                if gui is not None:
                    gui.set_user_text(command_text)
                conversation_until = time.monotonic() + CONVERSATION_WINDOW_SECONDS
                _set_ui_status(gui, "Höre zu")
                in_conversation_ui = True

        except Exception as exc:
            print(f"[Fehler in Hauptschleife] {exc}", file=sys.stderr)
            _set_ui_status(gui, "Fehler")
            time.sleep(0.8)
            _set_ui_status(gui, "Bereit")


def _ensure_windowed_stdio() -> None:
    """Stellt bei pythonw.exe oder --noconsole gültige stdout/stderr-Streams in jarvis.log sicher."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        log_path = Path(__file__).resolve().parent / "jarvis.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_file = open(log_path, "a", encoding="utf-8", buffering=1)
        if sys.stdout is None:
            sys.stdout = log_file  # type: ignore[assignment]
        if sys.stderr is None:
            sys.stderr = log_file  # type: ignore[assignment]
    except Exception:
        pass


def _start_ollama_serve_nonblocking(ollama_host: str) -> str:
    """AUFGABE 1 Stufe 4: Startet `ollama serve` mit CREATE_NO_WINDOW komplett asynchron ohne Modell-Wartezeit."""
    try:
        with urllib.request.urlopen(f"{ollama_host.rstrip('/')}/api/tags", timeout=0.35) as resp:
            if int(getattr(resp, "status", 200)) == 200:
                return "Ollama-Dienst bereits aktiv."
    except Exception:
        pass

    import shutil

    candidates = [
        shutil.which("ollama"),
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Ollama\ollama.exe"),
        r"C:\Program Files\Ollama\ollama.exe",
    ]
    ollama_exe = next((p for p in candidates if p and Path(p).is_file()), None)
    if not ollama_exe:
        return "Ollama nicht lokal installiert (Online-KI aktiv)."

    try:
        hide_flags = get_subprocess_hide_flags()
        subprocess.Popen(
            [ollama_exe, "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            **hide_flags,
        )
        return "Ollama-Hintergrundserver non-blocking gestartet."
    except Exception as exc:
        return f"Ollama-Hintergrundstart Hinweis: {exc}"


def main() -> int:
    boot_t0 = time.perf_counter()
    enable_windows_high_dpi()
    _ensure_windowed_stdio()
    env_file = ensure_env_file()
    if env_file.exists():
        load_dotenv(dotenv_path=env_file, override=True)
    sync_env_aliases()
    try:
        sync_config_json()
    except Exception:
        pass

    wake_word = os.getenv("WAKE_WORD", "jarvis").strip().lower() or "jarvis"
    gemini_api_key = clean_api_key(os.getenv("GEMINI_API_KEY"))
    groq_api_key = clean_api_key(os.getenv("GROQ_API_KEY"))
    deepseek_api_key = clean_api_key(os.getenv("DEEPSEEK_API_KEY"))
    openrouter_api_key = clean_api_key(os.getenv("OPENROUTER_API_KEY"))
    gemini_model = sanitize_gemini_model(os.getenv("GEMINI_MODEL", "gemini-2.5-flash"))
    groq_model = sanitize_groq_model(os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile"))
    deepseek_model = clean_api_key(os.getenv("DEEPSEEK_MODEL", "deepseek-chat")) or "deepseek-chat"
    openrouter_model = clean_api_key(os.getenv("OPENROUTER_MODEL", "openrouter/free")) or "openrouter/free"
    ollama_host = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434").strip()
    ollama_text_model = os.getenv("OLLAMA_TEXT_MODEL", default_ollama_text_model()).strip()
    ollama_vision_model = os.getenv("OLLAMA_VISION_MODEL", "moondream").strip()

    whisper_model = os.getenv("WHISPER_MODEL", "base").strip()
    whisper_compute = os.getenv("WHISPER_COMPUTE_TYPE", "int8").strip()
    whisper_threads = int(os.getenv("WHISPER_CPU_THREADS", "4"))
    piper_model_path = os.getenv("PIPER_MODEL_PATH", "models/de_DE-karlsson-low.onnx").strip()
    camera_index = int(os.getenv("CAMERA_INDEX", "0"))
    alarm_gpio_pin = int(os.getenv("ALARM_GPIO_PIN", "17"))
    fullscreen = False
    input_device = _parse_device(os.getenv("AUDIO_INPUT_DEVICE"))
    output_device = _parse_device(os.getenv("AUDIO_OUTPUT_DEVICE"))

    stop_event = threading.Event()
    use_gui = "--headless" not in sys.argv
    if platform.system() == "Linux" and not (os.getenv("DISPLAY") or os.getenv("WAYLAND_DISPLAY")):
        use_gui = False

    runtime: dict[str, Any] = {
        "trigger": None,
        "stt": None,
        "memory": None,
        "smart_home": None,
        "alarm_manager": None,
        "tts": None,
        "brain": None,
        "vision": None,
        "router": None,
        "recorder": None,
        "telegram_bot": None,
    }
    router_lock = threading.Lock()
    gui: Optional[JarvisGUI] = None

    def _log_boot(msg: str) -> None:
        print(msg)
        if gui is not None:
            gui.add_log(msg)

    def _full_cleanup_and_exit(*_args: object) -> None:
        stop_event.set()
        trg = runtime.get("trigger")
        if trg is not None:
            try:
                trg.triggered.set()
            except Exception:
                pass
        for key, method in (
            ("telegram_bot", "stop"),
            ("recorder", "close"),
            ("vision", "close"),
            ("router", "close"),
        ):
            obj = runtime.get(key)
            if obj is not None:
                try:
                    getattr(obj, method)()
                except Exception:
                    pass
        if "agent_hand" in sys.modules:
            try:
                sys.modules["agent_hand"].VISUAL_HAND_AGENT.stop_passive_screen_loop()
            except Exception:
                pass
        if "nas_brain" in sys.modules:
            try:
                sys.modules["nas_brain"].NAS_BRAIN.close()
            except Exception:
                pass

        try:
            unload_req = urllib.request.Request(
                f"{ollama_host}/api/generate",
                data=json.dumps({"model": ollama_text_model, "keep_alive": 0}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(unload_req, timeout=1.0)
        except Exception:
            pass

        if sys.platform == "win32":
            hide_flags = get_subprocess_hide_flags()
            for proc_name in ("ollama.exe", "ollama_llama_server.exe"):
                try:
                    subprocess.run(
                        ["taskkill", "/F", "/IM", proc_name, "/T"],
                        capture_output=True,
                        timeout=2.0,
                        **hide_flags,
                    )
                except Exception:
                    pass

        print("\n[Beendet] Jarvis fährt herunter.")
        os._exit(0)

    signal.signal(signal.SIGINT, _full_cleanup_and_exit)
    signal.signal(signal.SIGTERM, _full_cleanup_and_exit)

    mic_start_lock = threading.Lock()

    def _on_mic_error(msg: str) -> None:
        text = msg if msg.startswith("[MIC-ERROR]") else f"[MIC-ERROR] {msg}"
        if gui is not None:
            gui.add_log(text)
        else:
            print(text, file=sys.stderr)

    def _on_audio_level(lvl: float, vad_active: bool) -> None:
        if gui is not None:
            gui.set_audio_level(lvl, vad_active, speaking=False)

    def _on_vad_change(active: bool) -> None:
        if gui is None:
            return
        if active and gui._live_state.get("status") == "Bereit":
            _set_ui_status(gui, "Höre zu")

    # Simpler Beta-Datums-Check anhand der lokalen Systemzeit (>= 2027-02-01)
    beta_expired = BETA_GUARD.is_expired()

    def _ensure_mic_started() -> Any:
        if BETA_GUARD.is_expired():
            raise RuntimeError("Beta-Phase beendet")
        with mic_start_lock:
            existing = runtime.get("recorder")
            tts_obj = runtime.get("tts")
            if existing is not None:
                if tts_obj is not None and existing.mute_event is None:
                    existing.mute_event = tts_obj.speaking_event
                existing.ensure_alive()
                return existing

            from audio_in import start_listening

            rec = start_listening(
                device=input_device,
                mute_event=getattr(tts_obj, "speaking_event", None),
                on_level=_on_audio_level,
                on_error=_on_mic_error,
                on_vad_change=_on_vad_change,
            )
            runtime["recorder"] = rec
            if rec.last_error:
                _on_mic_error(rec.last_error)
            else:
                mic_msg = (
                    f"[Audio-Start] Aktives Mikrofon: '{rec.device_name}' "
                    f"(Device #{rec.device if rec.device is not None else 'default'}, "
                    f"Host-API: {getattr(rec, 'host_api_name', '') or 'PortAudio'}, "
                    f"{rec.input_sample_rate} Hz, {rec.input_dtype})."
                )
                print(mic_msg)
                _log_boot(mic_msg)
            return rec

    def _background_bootloader() -> None:
        """
        AUFGABE 1: Asynchroner 4-Stufen-Bootloader NACH sofortigem Öffnen des HUD-Fensters.
        Meldet jeden Fortschritt live über WebSocket an das HUD-Log!
        """
        try:
            if BETA_GUARD.is_expired():
                _log_boot("Beta-Phase beendet.")
                BETA_GUARD.announce_expired_once(log_fn=_log_boot, output_device=output_device)
                if gui is not None:
                    try:
                        gui._broadcast({"type": "beta_guard", "beta_guard": BETA_GUARD.get_status(), **BETA_GUARD.get_api_expired_payload()})
                    except Exception:
                        pass
                return

            # Garantiere sofortigen Mikrofon-Start unabhängig vom weiteren Ladezustand
            _ensure_mic_started()

            # ------------------------------------------------------------------
            # STUFE 1 (nach 0.2s): Konfiguration, Tresor, Brain & Online-API
            # ------------------------------------------------------------------
            time.sleep(0.2)
            _log_boot("[Boot 1/4] Lade Konfiguration, AES-Tresor & Online-API...")

            from agents import AgentRouter
            from alarm import AlarmManager
            from audio_in import TerminalTrigger
            from brain import JarvisBrain
            from memory import LongTermMemory
            from smart_home import SmartHomeController
            from tools import sync_archive_temp_environment
            from tts import TextToSpeech
            from vault import SECURE_VAULT

            _ = SECURE_VAULT.get_execution_mode()
            archive_root, using_nas = sync_archive_temp_environment()
            user_data = get_user_data_dir()
            memory = LongTermMemory(str(user_data / "memory.json"))
            smart_home = SmartHomeController()
            trigger = TerminalTrigger()
            trigger.start()
            alarm_manager = AlarmManager(
                str(user_data / "alarms.json"),
                output_device=output_device,
                gpio_pin=alarm_gpio_pin,
                on_hardware_button=trigger.triggered.set,
            )
            tts = TextToSpeech(model_path=piper_model_path, output_device=output_device)
            brain = JarvisBrain(
                gemini_api_key=gemini_api_key,
                gemini_model=gemini_model,
                groq_api_key=groq_api_key,
                groq_model=groq_model,
                deepseek_api_key=deepseek_api_key,
                deepseek_model=deepseek_model,
                openrouter_api_key=openrouter_api_key,
                openrouter_model=openrouter_model,
                ollama_host=ollama_host,
                ollama_text_model=ollama_text_model,
                ollama_vision_model=ollama_vision_model,
                memory=memory,
            )

            runtime["trigger"] = trigger
            runtime["memory"] = memory
            runtime["smart_home"] = smart_home
            runtime["alarm_manager"] = alarm_manager
            runtime["tts"] = tts
            runtime["brain"] = brain

            early_recorder = runtime.get("recorder")
            if early_recorder is not None:
                early_recorder.mute_event = tts.speaking_event
                tts.on_speaking_change = lambda speaking, rec=early_recorder: (
                    rec.pause_listening() if speaking else rec.resume_listening()
                )

            if gui is not None:
                _sync_mode_ui(get_is_online(), brain, gui)
                brain.set_log_callback(gui.add_log)
                tts.on_speaking_level = lambda lvl: gui.set_audio_level(lvl, False, speaking=True)

            # Online-API-Check asynchron, damit Jarvis in <1s sofort sprechbereit bleibt
            threading.Thread(
                target=brain.run_startup_api_check,
                name="JarvisApiCheck",
                daemon=True,
            ).start()

            boot1_ms = (time.perf_counter() - boot_t0) * 1000.0
            _log_boot(
                f"[Boot 1/4] Online-KI, Tresor & Archiv ({'NAS' if using_nas else 'Lokal'}: {archive_root}) bereit ({boot1_ms:.0f} ms)."
            )

            # ------------------------------------------------------------------
            # STUFE 2 (Hintergrund): Mikrofon-Stream sicherstellen & Lazy-STT
            # ------------------------------------------------------------------
            _log_boot("[Boot 2/4] Verknüpfe Mikrofon-Stream & Online/Offline-STT...")
            from stt import SpeechToText
            from vision import CameraVision, should_capture_webcam
            from agent_hand import VISUAL_HAND_AGENT

            recorder = _ensure_mic_started()
            recorder.mute_event = tts.speaking_event
            tts.on_speaking_change = lambda speaking, rec=recorder: (
                rec.pause_listening() if speaking else rec.resume_listening()
            )

            stt = SpeechToText(
                model_size=whisper_model,
                compute_type=whisper_compute,
                cpu_threads=whisper_threads,
                language="de",
                lazy=True,
                on_error=_on_mic_error,
            )
            if not get_is_online():
                _log_boot("[Boot 2/4] Offline-Modus erkannt – lade faster_whisper asynchron im Hintergrund...")
                stt.preload_async(
                    on_ready=lambda ok, err: _log_boot(
                        "[Boot 2/4] Lokales faster_whisper im RAM bereit."
                        if ok
                        else f"[Boot 2/4] faster_whisper Ladehinweis: {err}"
                    )
                )
            else:
                _log_boot("[Boot 2/4] Online-STT-Kaskade aktiv (Groq -> Gemini -> Fallback); faster_whisper im Standby.")

            vision_status_cb = gui.set_vision_status if gui is not None else None
            vision = CameraVision(
                camera_index=camera_index,
                on_demand=True,
                on_status_change=vision_status_cb,
            )
            vision.start()
            VISUAL_HAND_AGENT.attach_brain(brain)
            if vision_status_cb is not None:
                VISUAL_HAND_AGENT.register_vision_status_callback(vision_status_cb)
            VISUAL_HAND_AGENT.start_passive_screen_loop()
            router = AgentRouter(
                brain=brain,
                memory=memory,
                smart_home=smart_home,
                alarm_manager=alarm_manager,
                vision=vision,
            )

            runtime["stt"] = stt
            runtime["recorder"] = recorder
            runtime["vision"] = vision
            runtime["router"] = router

            def _capture_telegram_photo() -> Optional[bytes]:
                return vision.get_current_frame(archive=True)

            def _handle_telegram_command(cmd_text: str) -> str:
                if gui is not None:
                    gui.set_user_text(f"[Telegram] {cmd_text}")
                    gui.set_status("Telegram-Befehl")
                try:
                    webcam_img = _capture_telegram_photo() if should_capture_webcam(cmd_text) else None
                    with router_lock:
                        res = router.handle(cmd_text, webcam_image=webcam_img)
                    if gui is not None:
                        gui.set_model_label(res.provider, res.model)
                        gui.set_mode_label(brain.mode)
                        gui.set_jarvis_text(res.text)
                        gui.set_status("Bereit")
                    return res.text
                except Exception as exc:
                    if gui is not None:
                        gui.set_status("Bereit")
                    return f"Chef, Fehler bei Telegram-Befehl: {exc}"

            from telegram_bot import TelegramBridge

            telegram_bot = TelegramBridge(
                on_command=_handle_telegram_command,
                on_capture_photo=_capture_telegram_photo,
                on_log=lambda msg: gui.add_log(msg) if gui is not None else print(f"[Telegram] {msg}"),
            )
            runtime["telegram_bot"] = telegram_bot

            def _on_settings_saved(_saved: dict[str, str]) -> None:
                new_mode = brain.reload_from_env()
                is_online = new_mode == "online" and get_is_online()
                tts.set_offline_mode(not is_online)
                telegram_bot.configure_from_env()
                a_root, u_nas = sync_archive_temp_environment()
                memory.backup_to_archive()
                if not is_online and not stt.is_loaded():
                    stt.preload_async()
                if gui is not None:
                    _sync_mode_ui(is_online, brain, gui)
                    gui.add_log(f"Archiv: {a_root} ({'NAS' if u_nas else 'Lokal'})")

            def _handle_quick_command(cmd: str) -> None:
                trigger.trigger_command(cmd)

            def _handle_mobile_query(query: str, image_bytes: Optional[bytes] = None) -> dict[str, Any]:
                if gui is not None:
                    _set_ui_status(gui, "Denke nach")
                try:
                    with router_lock:
                        res = router.handle(query, webcam_image=image_bytes)
                    if gui is not None:
                        gui.set_user_text(f"[Mobile] {query}")
                        gui.set_model_label(res.provider, res.model)
                        gui.set_jarvis_text(res.text)
                        gui.add_log(f"Mobile ({res.provider}): {res.text[:50]}")
                    return {"text": res.text, "provider": res.provider, "model": res.model}
                except Exception as exc:
                    err_text = f"Fehler bei Mobile-Anfrage: {exc}"
                    if gui is not None:
                        gui.add_log(err_text)
                    return {"text": err_text, "provider": "Fehler", "model": "-"}
                finally:
                    _set_ui_status(gui, "Bereit")

            if gui is not None:
                recorder.on_level = lambda lvl, vad: gui.set_audio_level(lvl, vad, speaking=False)
                gui.attach_runtime_services(
                    on_manual_trigger=trigger.triggered.set,
                    on_camera_toggle=vision.toggle_paused,
                    on_mic_mute_toggle=recorder.toggle_manual_mute,
                    on_quick_command=_handle_quick_command,
                    frame_provider=vision.get_latest_bgr,
                    on_settings_saved=_on_settings_saved,
                    on_mode_toggle=lambda online: _apply_online_mode(online, brain, tts, gui, announce=True),
                    on_close=_full_cleanup_and_exit,
                    memory=memory,
                    on_mobile_ask=_handle_mobile_query,
                    tts_engine=tts,
                    brain=brain,
                    alarm_manager=alarm_manager,
                )

            try:
                from executive_assistant import EXECUTIVE_ASSISTANT

                EXECUTIVE_ASSISTANT.start(memory=memory)
            except Exception as exec_boot_exc:
                _log_boot(f"[Boot 2/4] Executive Assistant Hinweis: {exec_boot_exc}")

            _log_boot("[Boot 2/4] Mikrofon-Stream, Kamera & Multi-Agenten-Router aktiv.")

            # ------------------------------------------------------------------
            # STUFE 3: Schneller 0.3s App-Scanner (Startmenü + Desktop) -> später Tiefenscan
            # ------------------------------------------------------------------
            from app_indexer import APP_INDEXER

            fast_apps = APP_INDEXER.fast_startup_scan(force=False)
            APP_INDEXER.start_background_scan(force=False, delay_sec=5.0, low_priority=True)
            _log_boot(
                f"[Boot 3/4] App-Schnellindex bereit ({len(fast_apps)} Startmenü-/Desktop-Apps; Tiefenscan später im Hintergrund)."
            )

            # ------------------------------------------------------------------
            # STUFE 4: Ollama Non-Blocking & Assistenz-/Watchdog-Schleifen
            # ------------------------------------------------------------------
            ollama_msg = _start_ollama_serve_nonblocking(ollama_host)
            _log_boot(f"[Boot 4/4] {ollama_msg}")

            worker = threading.Thread(
                target=_assistant_loop,
                args=(
                    stop_event,
                    trigger,
                    gui,
                    wake_word,
                    stt,
                    tts,
                    brain,
                    router,
                    memory,
                    alarm_manager,
                    recorder,
                    vision,
                ),
                daemon=True,
            )
            worker.start()

            def _audio_watchdog() -> None:
                while not stop_event.is_set():
                    stop_event.wait(3.0)
                    if stop_event.is_set():
                        break
                    try:
                        recorder.ensure_alive()
                    except Exception as exc:
                        print(f"[Audio-Watchdog] Stream-Prüfung fehlgeschlagen: {exc}")

            threading.Thread(target=_audio_watchdog, daemon=True).start()
            alarm_manager.start_background_loop(stop_event)
            threading.Thread(
                target=_thermal_monitor_loop,
                args=(stop_event, tts, gui),
                daemon=True,
            ).start()
            telegram_bot.start()

            total_ms = (time.perf_counter() - boot_t0) * 1000.0
            _log_boot(f"[Boot 4/4] Hintergrund-Initialisierung komplett ({total_ms:.0f} ms).")
        except Exception as exc:
            print(f"[Fehler im Background Bootloader] {exc}", file=sys.stderr)
            if gui is not None:
                gui.add_log(f"Bootloader-Fehler: {exc}")

    if use_gui:
        try:
            gui = JarvisGUI(
                fullscreen=fullscreen,
                on_mic_mute_toggle=(None if beta_expired else (lambda: _ensure_mic_started().toggle_manual_mute())),
                on_close=_full_cleanup_and_exit,
                on_server_startup=(None if beta_expired else _ensure_mic_started),
            )
            gui.run(on_window_ready=_background_bootloader)
            _full_cleanup_and_exit()
        except Exception as exc:
            print(f"[Hinweis] GUI konnte nicht geöffnet werden ({exc}) – starte im Terminal-Modus.")
            gui = None

    if gui is None:
        if BETA_GUARD.is_expired():
            _log_boot("Beta-Phase beendet")
            BETA_GUARD.announce_expired_once(log_fn=_log_boot, output_device=output_device)
            time.sleep(1.5)
            return 1
        _background_bootloader()
        try:
            while not stop_event.is_set():
                time.sleep(0.3)
        except KeyboardInterrupt:
            pass
        _full_cleanup_and_exit()

    return 0


if __name__ == "__main__":
    _ensure_windowed_stdio()
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        import traceback

        err_txt = traceback.format_exc()
        try:
            with open(Path(__file__).resolve().parent / "jarvis.log", "a", encoding="utf-8") as lf:
                lf.write(f"\n[FATAL {datetime.now().isoformat()}]\n{err_txt}\n")
        except Exception:
            pass
        print(err_txt, file=sys.stderr)
        raise SystemExit(1)
