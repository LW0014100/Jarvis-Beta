"""Hybrid STT-Engine: Online-Kaskade (Groq Whisper -> Gemini Audio -> Google/Fallback) + lokales faster_whisper."""

from __future__ import annotations

import io
import os
import re
import sys
import threading
from typing import Any, Callable, Optional
import wave

import numpy as np

from config import get_is_online, load_settings


_NOISE_HALLUCINATIONS = {
    "untertitel",
    "untertitel der amara.org-community",
    "vielen dank für ihre aufmerksamkeit",
    "danke fürs zuschauen",
    "thanks for watching",
    "thank you for watching",
}


def _pcm_float32_to_wav_bytes(audio_float32: np.ndarray, sample_rate: int = 16000) -> bytes:
    """Konvertiert 16-kHz-Mono-float32-Audio verlustfrei im RAM zu WAV-Bytes (PCM16)."""
    clipped = np.clip(audio_float32, -1.0, 1.0)
    pcm_i16 = (clipped * 32767.0).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_i16.tobytes())
    return buf.getvalue()


def _clean_transcript(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", (text or "")).strip()
    if not cleaned:
        return ""
    low = cleaned.lower().strip(" .!?,;:\"'")
    if low in _NOISE_HALLUCINATIONS:
        return ""
    if cleaned in {"...", ".", ",", "!", "?"}:
        return ""
    return cleaned


class SpeechToText:
    """Schnelle, mehrstufige Sprach-zu-Text-Engine mit Lazy-Loading für faster_whisper."""

    def __init__(
        self,
        model_size: str = "base",
        compute_type: str = "int8",
        cpu_threads: int = 4,
        language: str = "de",
        lazy: bool = True,
        on_error: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.model_size = model_size
        self.compute_type = compute_type
        self.cpu_threads = cpu_threads
        self.language = language
        self.on_error = on_error
        self.last_provider: str = ""
        self._load_lock = threading.Lock()
        self._model: Optional[Any] = None
        if not lazy:
            self.ensure_loaded()

    def set_error_callback(self, callback: Optional[Callable[[str], None]]) -> None:
        self.on_error = callback

    def _emit_error(self, msg: str) -> None:
        text = msg if msg.startswith("[MIC-ERROR]") else f"[MIC-ERROR] {msg}"
        print(text, file=sys.stderr)
        if self.on_error is not None:
            try:
                self.on_error(text)
            except Exception:
                pass

    def is_loaded(self) -> bool:
        return self._model is not None

    def ensure_loaded(self) -> Any:
        if self._model is not None:
            return self._model
        with self._load_lock:
            if self._model is None:
                from faster_whisper import WhisperModel

                self._model = WhisperModel(
                    self.model_size,
                    device="cpu",
                    compute_type=self.compute_type,
                    cpu_threads=self.cpu_threads,
                )
        return self._model

    def preload_async(self, on_ready: Optional[Callable[[bool, str], None]] = None) -> None:
        """Lädt faster_whisper nicht-blockierend in einem Hintergrund-Thread."""
        if self._model is not None:
            if on_ready is not None:
                try:
                    on_ready(True, "")
                except Exception:
                    pass
            return

        def _worker() -> None:
            try:
                self.ensure_loaded()
                if on_ready is not None:
                    on_ready(True, "")
            except Exception as exc:
                self._emit_error(f"Whisper-Preload fehlgeschlagen: {exc}")
                if on_ready is not None:
                    on_ready(False, str(exc))

        threading.Thread(target=_worker, name="JarvisWhisperPreload", daemon=True).start()

    @staticmethod
    def _get_env_or_setting(key: str) -> str:
        val = (os.getenv(key) or "").strip()
        if val:
            return val
        try:
            settings = load_settings()
            return str(settings.get(key) or "").strip()
        except Exception:
            return ""

    def _transcribe_groq(self, wav_bytes: bytes, initial_prompt: str) -> str:
        api_key = self._get_env_or_setting("GROQ_API_KEY")
        if not api_key:
            return ""
        try:
            from groq import Groq

            client = Groq(api_key=api_key, timeout=8.0)
            resp = client.audio.transcriptions.create(
                file=("speech.wav", wav_bytes, "audio/wav"),
                model="whisper-large-v3-turbo",
                language=self.language,
                prompt=initial_prompt or None,
                temperature=0.0,
            )
            text = getattr(resp, "text", None) or (resp.get("text") if isinstance(resp, dict) else "")
            cleaned = _clean_transcript(str(text or ""))
            if cleaned:
                self.last_provider = "groq:whisper-large-v3-turbo"
            return cleaned
        except Exception as exc:
            self._emit_error(f"STT Stufe 1 (Groq Whisper) fehlgeschlagen: {exc}")
            return ""

    def _transcribe_gemini(self, wav_bytes: bytes) -> str:
        api_key = self._get_env_or_setting("GEMINI_API_KEY")
        if not api_key:
            return ""
        try:
            from google import genai
            from google.genai import types

            client = genai.Client(
                api_key=api_key,
                http_options=types.HttpOptions(timeout=8000),
            )
            model_name = self._get_env_or_setting("GEMINI_MODEL") or "gemini-2.5-flash"
            prompt = (
                "Transkribiere die gesprochene Sprache in dieser Audiodatei exakt Wort für Wort "
                f"auf {self.language.upper()} (Deutsch). Antworte AUSSCHLIESSLICH mit dem erkannten Text, "
                "ohne Anführungszeichen oder Kommentare. Falls keine Sprache hörbar ist, antworte leer."
            )
            resp = client.models.generate_content(
                model=model_name,
                contents=[
                    prompt,
                    types.Part.from_bytes(data=wav_bytes, mime_type="audio/wav"),
                ],
            )
            cleaned = _clean_transcript(getattr(resp, "text", "") or "")
            if cleaned:
                self.last_provider = f"gemini:{model_name}"
            return cleaned
        except Exception as exc:
            self._emit_error(f"STT Stufe 2 (Gemini Audio) fehlgeschlagen: {exc}")
            return ""

    def _transcribe_google_sr(self, wav_bytes: bytes) -> str:
        try:
            import speech_recognition as sr  # type: ignore
        except ImportError:
            return ""
        try:
            recognizer = sr.Recognizer()
            with sr.AudioFile(io.BytesIO(wav_bytes)) as source:
                audio_data = recognizer.record(source)
            lang_tag = "de-DE" if self.language.lower().startswith("de") else self.language
            text = recognizer.recognize_google(audio_data, language=lang_tag)
            cleaned = _clean_transcript(str(text or ""))
            if cleaned:
                self.last_provider = "google-web-speech"
            return cleaned
        except Exception as exc:
            self._emit_error(f"STT Stufe 3 (Google SpeechRecognition) fehlgeschlagen: {exc}")
            return ""

    def _transcribe_local_whisper(self, audio_float32: np.ndarray, initial_prompt: str) -> str:
        try:
            model = self.ensure_loaded()
            segments, _ = model.transcribe(
                audio_float32,
                language=self.language,
                beam_size=1,
                best_of=1,
                vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 300},
                condition_on_previous_text=False,
                initial_prompt=initial_prompt,
            )
            text = " ".join(seg.text.strip() for seg in segments if seg.text.strip())
            cleaned = _clean_transcript(text)
            if cleaned:
                self.last_provider = f"faster-whisper:{self.model_size}"
            return cleaned
        except Exception as exc:
            self._emit_error(f"Lokales STT (faster_whisper) fehlgeschlagen: {exc}")
            return ""

    def transcribe(
        self,
        audio_float32: np.ndarray,
        initial_prompt: str = "Jarvis, schau mal, was ist das?",
    ) -> str:
        if audio_float32 is None or audio_float32.size < 2400:  # < 0,15 s bei 16 kHz
            return ""

        arr = np.asarray(audio_float32, dtype=np.float32).reshape(-1)
        if float(np.max(np.abs(arr))) < 0.0025:
            return ""

        if get_is_online():
            wav_bytes = _pcm_float32_to_wav_bytes(arr, sample_rate=16000)

            # Stufe 1: Groq whisper-large-v3-turbo (Ultra-Schnell)
            text = self._transcribe_groq(wav_bytes, initial_prompt)
            if text:
                return text

            # Stufe 2: Gemini Multimodal Audio
            text = self._transcribe_gemini(wav_bytes)
            if text:
                return text

            # Stufe 3: Optional Google SpeechRecognition (kein lokales Whisper im Online-Modus laden!)
            text = self._transcribe_google_sr(wav_bytes)
            if text:
                return text
            if self.is_loaded():
                return self._transcribe_local_whisper(arr, initial_prompt)
            return ""

        return self._transcribe_local_whisper(arr, initial_prompt)
