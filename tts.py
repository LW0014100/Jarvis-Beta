"""Hybride deutsche Streaming-Sprachausgabe: edge-tts (de-DE-ConradNeural / de-DE-KillianNeural, -15Hz, -5%) -> Piper (Karlsson Deep)."""

from __future__ import annotations

import asyncio
import io
import os
from pathlib import Path
import platform
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Callable, Optional
import urllib.request
import uuid
import wave

import edge_tts
import numpy as np
import sounddevice as sd
import soundfile as sf

DEFAULT_EDGE_VOICE = "de-DE-ConradNeural"
DEFAULT_EDGE_FALLBACK_VOICE = "de-DE-KillianNeural"
DEFAULT_EDGE_PITCH = "-15Hz"
DEFAULT_EDGE_RATE = "-5%"
MAX_TTS_PLAYBACK_SECONDS = 20.0


def create_unique_temp_audio_path(ext: str = ".mp3") -> Path:
    """Erzeugt für jede TTS-Ausgabe einen eindeutigen temporären Dateinamen via uuid4."""
    suffix = ext if ext.startswith(".") else f".{ext}"
    return Path(tempfile.gettempdir()) / f"jarvis_tts_{uuid.uuid4().hex}{suffix}"


def cleanup_temp_audio_file(path: Optional[Path]) -> None:
    """Entlädt ggf. Mixer-Handles und löscht eine temporäre TTS-Audiodatei rückstandsfrei."""
    if path is None:
        return
    try:
        import sys as _sys
        if "pygame" in _sys.modules:
            pygame_mod = _sys.modules["pygame"]
            mixer = getattr(pygame_mod, "mixer", None)
            music = getattr(mixer, "music", None) if mixer is not None else None
            if music is not None and hasattr(music, "unload"):
                music.unload()
    except Exception:
        pass
    try:
        Path(path).unlink(missing_ok=True)
    except Exception:
        pass

MOBILE_VOICE_PACK_PHRASES: tuple[str, ...] = (
    "Guten Tag, Chef. Mobile Einheit und AR-Kamera bereit. Wie kann ich behilflich sein?",
    "Systeme stehen auf Grün. Alle Subsysteme nominal.",
    "Verbindung zum Heim-Server hergestellt. Alle Systeme synchron.",
    "Offline-Modus aktiv. Nutze lokale Sprachsicherung.",
    "Ich analysiere das Kamerabild, einen Moment bitte.",
    "Verstanden, Chef. Wird ausgeführt.",
    "Gedächtnis erfolgreich mit dem Heim-Server synchronisiert.",
    "Achtung, keine Verbindung zum Heim-Server. Autarker Modus aktiv.",
)

from config import (
    get_base_dir,
    get_is_online,
    get_user_data_dir,
    register_online_mode_listener,
)

try:
    from piper import PiperVoice
except ImportError:
    PiperVoice = None  # type: ignore[assignment,misc]

try:
    from piper import SynthesisConfig
except ImportError:  # ältere piper-tts-Versionen
    SynthesisConfig = None  # type: ignore[assignment,misc]

try:
    import pyttsx3
except ImportError:
    pyttsx3 = None  # type: ignore[assignment]

try:
    import av
except ImportError:  # optionaler MP3-Decoder über PyAV (kommt mit faster-whisper)
    av = None  # type: ignore[assignment]

PIPER_VOICE_URLS: dict[str, tuple[str, str]] = {
    "de_DE-karlsson-low.onnx": (
        "https://huggingface.co/rhasspy/piper-voices/resolve/main/de/de_DE/karlsson/low/de_DE-karlsson-low.onnx",
        "https://huggingface.co/rhasspy/piper-voices/resolve/main/de/de_DE/karlsson/low/de_DE-karlsson-low.onnx.json",
    ),
    "de_DE-thorsten-medium.onnx": (
        "https://huggingface.co/rhasspy/piper-voices/resolve/main/de/de_DE/thorsten/medium/de_DE-thorsten-medium.onnx",
        "https://huggingface.co/rhasspy/piper-voices/resolve/main/de/de_DE/thorsten/medium/de_DE-thorsten-medium.onnx.json",
    ),
}


URL_PATTERN = re.compile(r"https?://\S+|www\.\S+", flags=re.IGNORECASE)
EMOJI_PATTERN = re.compile(
    r"[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF]+",
    flags=re.UNICODE,
)
ABBREVIATION_ENDINGS = (
    "z.b.",
    "z. b.",
    "d.h.",
    "d. h.",
    "u.a.",
    "u. a.",
    "ca.",
    "bzw.",
    "etc.",
    "dr.",
    "prof.",
    "nr.",
    "mio.",
    "mrd.",
)


def _clean_for_speech(text: str) -> str:
    """Bereinigt Text von Markdown, URLs und Emojis für natürliche Sprachausgabe."""
    cleaned = URL_PATTERN.sub("", text)
    cleaned = EMOJI_PATTERN.sub("", cleaned)
    cleaned = re.sub(r"[*_`#>~|]+", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()


def split_complete_sentences(buffer: str, force: bool = False) -> tuple[list[str], str]:
    """Extrahiert vollständige Sätze/Chunks an '.', '!', '?' oder '\\n' für sofortiges Streaming."""
    sentences: list[str] = []
    start = 0
    length = len(buffer)
    i = 0

    while i < length:
        ch = buffer[i]
        is_newline = ch == "\n"
        is_end_punct = ch in ".!?"

        should_split = False
        if is_newline:
            should_split = True
        elif is_end_punct:
            next_ch = buffer[i + 1] if i + 1 < length else ""
            # Keine Trennung mitten in Dezimalzahlen (z. B. 3.8 oder 14.2)
            prev_ch = buffer[i - 1] if i > 0 else ""
            if ch == "." and prev_ch.isdigit() and next_ch.isdigit():
                i += 1
                continue

            # Bei '.', '!', '?' am Ende eines Wortes (gefolgt von Leerzeichen/Quote/Ende) trennen
            if next_ch in ("", " ", "\n", "\r", "\t", '"', "'", "”", "„", ")"):
                candidate = buffer[start : i + 1].strip()
                low_cand = candidate.lower()
                if not any(low_cand.endswith(abbr) for abbr in ABBREVIATION_ENDINGS):
                    if len(candidate) >= 6 or ch in ("!", "?"):
                        should_split = True

        # Sehr lange Sätze ohne Satzzeichen am Komma/Semikolon vorziehen für geringe Latenz
        if not should_split and ch in (",", ";") and (i - start) >= 120:
            next_ch = buffer[i + 1] if i + 1 < length else ""
            if next_ch in (" ", "\n"):
                should_split = True

        if should_split:
            piece = _clean_for_speech(buffer[start : i + 1])
            if piece:
                sentences.append(piece)
            start = i + 1
        i += 1

    remainder = buffer[start:]
    if force:
        tail = _clean_for_speech(remainder)
        if tail:
            sentences.append(tail)
        remainder = ""

    return sentences, remainder


def _decode_mp3_bytes(mp3_bytes: bytes) -> tuple[np.ndarray, int]:
    """Dekodiert MP3-Bytes im RAM via soundfile oder PyAV (ohne Temp-Dateien)."""
    try:
        data, sample_rate = sf.read(io.BytesIO(mp3_bytes), dtype="float32")
        if data.size > 0:
            return data, int(sample_rate)
    except Exception:
        pass

    if av is None:
        raise RuntimeError("MP3-Audio konnte weder über soundfile noch PyAV dekodiert werden.")

    chunks: list[np.ndarray] = []
    sample_rate = 24000
    with av.open(io.BytesIO(mp3_bytes), format="mp3") as container:
        stream = container.streams.audio[0]
        resampler = av.audio.resampler.AudioResampler(format="flt", layout="mono")
        for frame in container.decode(stream):
            for out_frame in resampler.resample(frame):
                arr = out_frame.to_ndarray().reshape(-1)
                chunks.append(arr.astype(np.float32))
                sample_rate = int(out_frame.sample_rate or sample_rate)

    if not chunks:
        raise RuntimeError("Leerer MP3-Stream von edge-tts.")
    return np.concatenate(chunks), sample_rate


class SpeechStreamSession:
    """Asynchrone Satz-Warteschlange: KI streamt Text -> Synthesizer-Thread -> Audio-Player."""

    def __init__(
        self,
        tts: TextToSpeech,
        on_playback_start: Optional[Callable[[], None]] = None,
    ) -> None:
        self._tts = tts
        self._on_playback_start = on_playback_start
        self._buffer = ""
        self._closed = False
        self._aborted = False
        self._lock_released = False
        self._cleanup_lock = threading.Lock()
        self._active_temp_files: list[Path] = []

        self._text_queue: queue.Queue[Optional[str]] = queue.Queue()
        self._audio_queue: queue.Queue[Optional[tuple[Optional[np.ndarray], int, Optional[str]]]] = queue.Queue(maxsize=6)

        self._synth_thread = threading.Thread(target=self._synth_worker, daemon=True)
        self._play_thread = threading.Thread(target=self._play_worker, daemon=True)
        self._synth_thread.start()
        self._play_thread.start()

    def _cleanup_after_speech(self) -> None:
        """Garantiert am Ende jeder Sprachausgabe das Freigeben von Lock, Events & Mikrofon."""
        with self._cleanup_lock:
            self._tts.speaking_event.clear()
            if self._tts.on_speaking_level is not None:
                try:
                    self._tts.on_speaking_level(0.0)
                except Exception:
                    pass
            if self._tts.on_speaking_change is not None:
                try:
                    self._tts.on_speaking_change(False)
                except Exception:
                    pass
            for tmp_path in list(self._active_temp_files):
                cleanup_temp_audio_file(tmp_path)
            self._active_temp_files.clear()
            if not self._lock_released:
                self._lock_released = True
                try:
                    self._tts._speak_lock.release()
                except RuntimeError:
                    pass

    def push_token(self, token: str) -> None:
        """Nimmt gestreamte Wort-/Zeichen-Tokens an und reiht fertige Sätze sofort ein."""
        if self._closed or self._aborted or not token:
            return
        self._buffer += token
        ready, self._buffer = split_complete_sentences(self._buffer, force=False)
        for sentence in ready:
            self._text_queue.put(sentence)

    def push_sentence(self, text: str) -> None:
        """Reiht einen kompletten Textabschnitt (mit automatischer Satztrennung) ein."""
        if self._closed or self._aborted or not text:
            return
        ready, remainder = split_complete_sentences(text, force=True)
        for sentence in ready:
            self._text_queue.put(sentence)
        if remainder:
            cleaned = _clean_for_speech(remainder)
            if cleaned:
                self._text_queue.put(cleaned)

    def _synth_worker(self) -> None:
        try:
            while not self._aborted:
                sentence = self._text_queue.get()
                if sentence is None:
                    break
                if self._aborted:
                    break
                try:
                    audio, sr, fallback_text = self._tts._synthesize_chunk(sentence)
                    if not self._aborted and ((audio is not None and audio.size > 0) or fallback_text):
                        self._audio_queue.put((audio, sr, fallback_text))
                except Exception as exc:
                    print(f"[TTS-Chunk-Fehler] {exc}")
        finally:
            self._audio_queue.put(None)

    def _play_worker(self) -> None:
        started_notified = False
        played_any = False
        try:
            while not self._aborted:
                item = self._audio_queue.get()
                if item is None:
                    break
                if self._aborted:
                    break
                audio, sr, fallback_text = item
                if not played_any:
                    self._tts.speaking_event.set()
                    if self._tts.on_speaking_change is not None:
                        try:
                            self._tts.on_speaking_change(True)
                        except Exception:
                            pass
                    played_any = True
                if not started_notified and self._on_playback_start is not None:
                    started_notified = True
                    try:
                        self._on_playback_start()
                    except Exception:
                        pass
                if audio is not None and audio.size > 0:
                    self._tts._play_audio_with_level(audio, sr, abort_check=lambda: self._aborted)
                elif fallback_text:
                    self._tts._speak_emergency_fallback(fallback_text, abort_check=lambda: self._aborted)
        finally:
            if played_any and not self._aborted:
                time.sleep(0.18)  # Kurze Anti-Echo-Pause bevor VAD wieder lauscht
            self._cleanup_after_speech()

    def finish(self, wait: bool = True, timeout: float = MAX_TTS_PLAYBACK_SECONDS) -> None:
        """Schließt den Text-Stream ab, spricht Resttext aus und wartet maximal `timeout` Sekunden."""
        try:
            if not self._closed:
                self._closed = True
                ready, _ = split_complete_sentences(self._buffer, force=True)
                self._buffer = ""
                for sentence in ready:
                    self._text_queue.put(sentence)
                self._text_queue.put(None)

            if wait:
                deadline = time.monotonic() + max(1.0, float(timeout))
                rem_synth = max(0.2, deadline - time.monotonic())
                self._synth_thread.join(timeout=rem_synth)
                rem_play = max(0.2, deadline - time.monotonic())
                self._play_thread.join(timeout=rem_play)
                if self._synth_thread.is_alive() or self._play_thread.is_alive():
                    print(f"[TTS-TIMEOUT] Harter {timeout:.0f}s-Timeout erreicht – breche hängende Audio-Ausgabe ab.")
                    self.stop()
        finally:
            if wait or self._aborted:
                self._cleanup_after_speech()

    def stop(self) -> None:
        """Bricht die laufende Sprachausgabe sofort ab."""
        self._aborted = True
        self._closed = True
        try:
            sd.stop()
        except Exception:
            pass
        while not self._text_queue.empty():
            try:
                self._text_queue.get_nowait()
            except queue.Empty:
                break
        while not self._audio_queue.empty():
            try:
                self._audio_queue.get_nowait()
            except queue.Empty:
                break
        try:
            self._text_queue.put_nowait(None)
        except Exception:
            pass
        try:
            self._audio_queue.put_nowait(None)
        except Exception:
            pass
        self._synth_thread.join(timeout=1.5)
        self._play_thread.join(timeout=1.5)
        self._cleanup_after_speech()


def _apply_deep_raspy_filter(
    audio: np.ndarray,
    sample_rate: int,
    drive: float = 1.48,
    roughness: float = 0.045,
    bass_boost: float = 0.32,
) -> np.ndarray:
    """Verleiht der Stimme ein tiefes, männliches, leicht raues Klangprofil (DSP-Filter).

    Kombiniert:
    1. Tieffrequente Brustresonanz (Low-Pass-Grundtonverstärkung ~175 Hz)
    2. Hüllkurven-gesteuerte Stimm-Rauheit (Subharmonische Textur, in Sprechpausen stumm)
    3. Warme asymmetrische Röhren-Sättigung (Soft-Tanh-Overdrive)
    """
    if audio is None or audio.size == 0 or sample_rate <= 0:
        return audio

    x = audio.astype(np.float32, copy=True)
    if x.ndim > 1:
        x = x[:, 0]

    # 1. Brustresonanz / Tiefton-Fundament (~175 Hz 1st-Order IIR Low-Pass)
    rc = 1.0 / (2.0 * np.pi * 175.0)
    dt = 1.0 / float(sample_rate)
    alpha = float(dt / (rc + dt))
    low = np.empty_like(x)
    acc = 0.0
    # Schneller Vektor-Block-Filter über kurze Fenster für minimale CPU-Last
    win = max(1, int(sample_rate * 0.004))
    kernel = np.exp(-np.arange(win, dtype=np.float32) * alpha * 3.5)
    kernel /= float(np.sum(kernel)) or 1.0
    low = np.convolve(x, kernel, mode="same").astype(np.float32)
    x = x + bass_boost * low

    # 2. Leichte Stimm-Rauheit (nur auf stimmhaften Signalanteilen, 0% in Stille)
    if roughness > 0.0:
        t = np.arange(x.size, dtype=np.float32) / float(sample_rate)
        envelope = np.clip(np.abs(x) * 2.2, 0.0, 1.0)
        gravel = (
            0.65 * np.sin(2.0 * np.pi * 68.0 * t)
            + 0.35 * np.sin(2.0 * np.pi * 112.0 * t + 0.7)
        ).astype(np.float32)
        x = x * (1.0 + roughness * envelope * gravel)

    # 3. Leichte asymmetrische Sättigung (Röhren-Charakter / Wärme)
    if drive > 1.0:
        asym = x + 0.035 * (x * np.abs(x))
        norm = float(np.tanh(drive)) or 1.0
        x = (np.tanh(asym * drive) / norm).astype(np.float32)

    peak = float(np.max(np.abs(x)))
    if peak > 0.01:
        x = (x / peak * min(0.88, peak * 1.05)).astype(np.float32)
    return x


class TextToSpeech:
    """Streaming-fähige deutsche TTS mit tiefer, männlicher, leicht rauer Stimme (Conrad/Killian -> Karlsson)."""

    def __init__(
        self,
        model_path: str | Path,
        output_device: Optional[int | str] = None,
        edge_voice: str = "de-DE-ConradNeural",
        edge_fallback_voice: str = "de-DE-KillianNeural",
        edge_rate: str = "-5%",
        edge_pitch: str = "-15Hz",
        edge_timeout: float = 9.0,
        offline_pitch_rate_factor: float = 0.85,
        on_speaking_level: Optional[Callable[[float], None]] = None,
        on_speaking_change: Optional[Callable[[bool], None]] = None,
    ) -> None:
        self.voice: Optional[object] = None
        resolved = self._resolve_or_download_piper_model(Path(model_path))
        if resolved is not None and PiperVoice is not None:
            try:
                self.voice = PiperVoice.load(str(resolved))
            except Exception as exc:
                print(f"[TTS-Warnung] Piper-Modell '{resolved}' konnte nicht geladen werden: {exc}")
                self.voice = None

        if self.voice is None:
            print("[TTS-Hinweis] Piper-Modell nicht verfügbar – nutze lokalen SAPI5-/System-Fallback.")

        self.output_device = output_device

        self.edge_voice = os.getenv("EDGE_TTS_VOICE", edge_voice).strip() or edge_voice
        self.edge_fallback_voice = edge_fallback_voice
        self.edge_rate = os.getenv("EDGE_TTS_RATE", edge_rate).strip() or edge_rate
        raw_pitch = os.getenv("EDGE_TTS_PITCH", edge_pitch).strip() or edge_pitch
        # edge-tts erwartet für Pitch-Angaben Hz oder st (z. B. -15Hz); wandle -15% sicher in -15Hz um
        if raw_pitch.endswith("%"):
            raw_pitch = raw_pitch[:-1] + "Hz"
        self.edge_pitch = raw_pitch
        self.edge_timeout = edge_timeout
        self.offline_pitch_rate_factor = offline_pitch_rate_factor
        self.offline_only = not get_is_online()
        self.on_speaking_level = on_speaking_level
        self.on_speaking_change = on_speaking_change

        self.speaking_event = threading.Event()
        self._speak_lock = threading.Lock()
        self._sapi_lock = threading.Lock()
        self._active_session: Optional[SpeechStreamSession] = None
        self._edge_cooldown_until = 0.0
        register_online_mode_listener(self._on_online_mode_changed)

    @property
    def is_speaking(self) -> bool:
        return self.speaking_event.is_set()

    @staticmethod
    def _has_piper_pair(onnx_path: Path) -> bool:
        cfg_path = Path(f"{onnx_path}.json")
        return (
            onnx_path.is_file()
            and cfg_path.is_file()
            and onnx_path.stat().st_size > 0
            and cfg_path.stat().st_size > 0
        )

    @staticmethod
    def _download_file(url: str, dest: Path, timeout: float = 25.0) -> bool:
        dest.parent.mkdir(parents=True, exist_ok=True)
        part_path = Path(f"{dest}.part")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Jarvis-TTS/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp, open(part_path, "wb") as out:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    out.write(chunk)
            if part_path.stat().st_size <= 0:
                part_path.unlink(missing_ok=True)
                return False
            os.replace(part_path, dest)
            return True
        except Exception as exc:
            print(f"[TTS-Download] Fehlgeschlagen ({dest.name}): {exc}")
            part_path.unlink(missing_ok=True)
            return False

    @classmethod
    def _ensure_piper_pair_downloaded(cls, model_dir: Path, filename: str) -> Optional[Path]:
        urls = PIPER_VOICE_URLS.get(filename)
        if not urls:
            return None
        onnx_url, json_url = urls
        onnx_path = model_dir / filename
        json_path = Path(f"{onnx_path}.json")
        if not onnx_path.is_file() or onnx_path.stat().st_size <= 0:
            print(f"[TTS] Lade Piper-Stimmmodell '{filename}' herunter...")
            if not cls._download_file(onnx_url, onnx_path):
                return None
        if not json_path.is_file() or json_path.stat().st_size <= 0:
            print(f"[TTS] Lade Piper-Konfiguration '{json_path.name}' herunter...")
            if not cls._download_file(json_url, json_path):
                return None
        return onnx_path if cls._has_piper_pair(onnx_path) else None

    @classmethod
    def _resolve_or_download_piper_model(cls, configured_path: Path) -> Optional[Path]:
        user_models = get_user_data_dir() / "models"
        base_models = get_base_dir() / "models"
        candidates = []

        if "thorsten" not in configured_path.name.lower():
            candidates.append(configured_path)
        candidates.extend(
            [
                base_models / configured_path.name,
                user_models / configured_path.name,
                configured_path.parent / "de_DE-karlsson-low.onnx",
                base_models / "de_DE-karlsson-low.onnx",
                user_models / "de_DE-karlsson-low.onnx",
                configured_path,
                configured_path.parent / "de_DE-thorsten-high.onnx",
                configured_path.parent / "de_DE-thorsten-medium.onnx",
            ]
        )

        for candidate in candidates:
            if cls._has_piper_pair(candidate):
                return candidate

        # Fehlende .onnx.json für bereits vorhandene Datei oder komplettes Modellpaar nachladen
        # Bevorzuge beschreibbaren Nutzerordner (besonders bei Installation unter C:\Program Files)
        target_dir = user_models
        for candidate_name in (
            configured_path.name,
            "de_DE-karlsson-low.onnx",
            "de_DE-thorsten-medium.onnx",
        ):
            if candidate_name in PIPER_VOICE_URLS:
                downloaded = cls._ensure_piper_pair_downloaded(target_dir, candidate_name)
                if downloaded is not None:
                    return downloaded
        return None

    def _on_online_mode_changed(self, enabled: bool) -> None:
        self.set_offline_mode(not enabled)

    def set_offline_mode(self, offline: bool) -> None:
        self.offline_only = offline
        if not offline:
            self._edge_cooldown_until = 0.0

    def _play_audio_with_level(
        self,
        audio_data: np.ndarray,
        sample_rate: int,
        abort_check: Optional[Callable[[], bool]] = None,
        max_timeout_seconds: float = MAX_TTS_PLAYBACK_SECONDS,
    ) -> None:
        if audio_data.size == 0 or sample_rate <= 0:
            return
        mono = audio_data[:, 0] if audio_data.ndim > 1 else audio_data
        chunk_samples = max(1, int(sample_rate * 0.06))
        hard_limit = max(1.0, float(max_timeout_seconds))
        try:
            sd.play(audio_data, samplerate=sample_rate, device=self.output_device)
            started_at = time.monotonic()
            total_duration = min(mono.size / float(sample_rate), hard_limit)
            while True:
                if abort_check is not None and abort_check():
                    sd.stop()
                    return
                elapsed = time.monotonic() - started_at
                if elapsed >= total_duration or elapsed >= hard_limit:
                    break
                if self.on_speaking_level is not None:
                    idx = min(mono.size - 1, int(elapsed * sample_rate))
                    win = mono[idx : idx + chunk_samples]
                    rms = float(np.sqrt(np.mean(win.astype(np.float32) ** 2))) if win.size else 0.0
                    try:
                        self.on_speaking_level(min(1.0, rms * 4.5))
                    except Exception:
                        pass
                time.sleep(0.05)
        finally:
            try:
                sd.stop()
            except Exception:
                pass
            if self.on_speaking_level is not None:
                try:
                    self.on_speaking_level(0.0)
                except Exception:
                    pass

    async def _fetch_edge_mp3(
        self,
        text: str,
        voice: str = "de-DE-ConradNeural",
        pitch: str = "-15Hz",
        rate: str = "-5%",
    ) -> bytes:
        communicate = edge_tts.Communicate(
            text,
            voice=voice,
            pitch=pitch,
            rate=rate,
        )
        chunks: list[bytes] = []
        async for chunk in communicate.stream():
            if chunk.get("type") == "audio":
                data = chunk.get("data")
                if isinstance(data, bytes):
                    chunks.append(data)
        if not chunks:
            raise RuntimeError(f"edge-tts ({voice}) lieferte keine Audiodaten.")
        return b"".join(chunks)

    def _synthesize_edge_pcm(self, text: str) -> tuple[np.ndarray, int]:
        if self.offline_only or not get_is_online():
            raise RuntimeError("Offline-Modus aktiv: edge-tts wird nicht aufgerufen.")
        try:
            mp3_bytes = asyncio.run(
                asyncio.wait_for(
                    self._fetch_edge_mp3(
                        text,
                        voice=self.edge_voice,
                        pitch=self.edge_pitch,
                        rate=self.edge_rate,
                    ),
                    timeout=self.edge_timeout,
                )
            )
            audio, sr = _decode_mp3_bytes(mp3_bytes)
        except Exception:
            if not self.edge_fallback_voice or self.edge_fallback_voice == self.edge_voice:
                raise
            mp3_bytes = asyncio.run(
                asyncio.wait_for(
                    self._fetch_edge_mp3(
                        text,
                        voice=self.edge_fallback_voice,
                        pitch=self.edge_pitch,
                        rate=self.edge_rate,
                    ),
                    timeout=self.edge_timeout,
                )
            )
            audio, sr = _decode_mp3_bytes(mp3_bytes)

        # Dezente Röhren-Sättigung & Tiefton-Wärme für natürlichen, markanten ConradNeural-Klang
        processed = _apply_deep_raspy_filter(
            audio,
            sr,
            drive=1.18,
            roughness=0.0,
            bass_boost=0.16,
        )
        return processed, sr

    def _synthesize_piper_pcm(self, text: str) -> tuple[np.ndarray, int]:
        if self.voice is None:
            raise RuntimeError("Kein Piper-Modell geladen.")

        syn_cfg = None
        if SynthesisConfig is not None:
            try:
                # piper-tts >= 1.2 nutzt noise_w_scale; höherer noise_scale erzeugt rauere Textur
                syn_cfg = SynthesisConfig(
                    length_scale=0.96,
                    noise_scale=0.75,
                    noise_w_scale=0.45,
                )
            except TypeError:
                try:
                    syn_cfg = SynthesisConfig(
                        length_scale=0.96,
                        noise_scale=0.75,
                        noise_w=0.45,
                    )
                except Exception:
                    syn_cfg = None

        wav_buffer = io.BytesIO()
        with wave.open(wav_buffer, "wb") as wav_file:
            if syn_cfg is not None:
                self.voice.synthesize_wav(text, wav_file, syn_config=syn_cfg)
            else:
                self.voice.synthesize_wav(text, wav_file)

        wav_buffer.seek(0)
        audio_data, sample_rate = sf.read(wav_buffer, dtype="float32")
        # Pitch-Shift nach unten über abgesenkte Sample-Rate + Sättigungs-/Rauheits-Filter
        deep_rate = max(8000, int(round(int(sample_rate) * self.offline_pitch_rate_factor)))
        processed = _apply_deep_raspy_filter(
            audio_data,
            deep_rate,
            drive=1.55,
            roughness=0.055,
            bass_boost=0.38,
        )
        return processed, deep_rate

    def _speak_emergency_fallback(
        self,
        text: str,
        abort_check: Optional[Callable[[], bool]] = None,
    ) -> None:
        """Letzter Offline-Notfall-Fallback (pyttsx3 / Windows SAPI5 / Linux TTS mit tiefer Stimme)."""
        cleaned = _clean_for_speech(text)
        if not cleaned or (abort_check is not None and abort_check()):
            return

        if self.on_speaking_level is not None:
            try:
                self.on_speaking_level(0.45)
            except Exception:
                pass

        try:
            with self._sapi_lock:
                if abort_check is not None and abort_check():
                    return
                if pyttsx3 is not None:
                    try:
                        if platform.system() == "Windows":
                            import ctypes
                            ctypes.windll.ole32.CoInitialize(None)
                        try:
                            engine = pyttsx3.init("sapi5" if platform.system() == "Windows" else None)
                            voices = engine.getProperty("voices") or []
                            chosen_id = None
                            # Bevorzuge männliche deutsche Stimmen (Stefan / Conrad / Male)
                            for voice in voices:
                                meta = f"{getattr(voice, 'id', '')} {getattr(voice, 'name', '')}".lower()
                                if any(k in meta for k in ("stefan", "conrad", "karsten", "klaus")):
                                    chosen_id = voice.id
                                    break
                            if chosen_id is None:
                                for voice in voices:
                                    meta = f"{getattr(voice, 'id', '')} {getattr(voice, 'name', '')}".lower()
                                    if any(k in meta for k in ("de-de", "de_de", "german", "hedda", "katja")):
                                        chosen_id = voice.id
                                        break
                            if chosen_id is not None:
                                engine.setProperty("voice", chosen_id)
                            engine.setProperty("rate", 148)
                            engine.say(cleaned)
                            engine.runAndWait()
                            engine.stop()
                            return
                        finally:
                            if platform.system() == "Windows":
                                import ctypes
                                ctypes.windll.ole32.CoUninitialize()
                    except Exception as exc:
                        print(f"[TTS-Fallback] pyttsx3 fehlgeschlagen ({exc}) – prüfe System-TTS.")

                if platform.system() == "Windows":
                    env = os.environ.copy()
                    env["JARVIS_TTS_TEXT"] = cleaned
                    ps_script = (
                        "Add-Type -AssemblyName System.Speech; "
                        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                        "$v = $s.GetInstalledVoices() | Where-Object { $_.VoiceInfo.Culture.Name -like 'de*' -and $_.VoiceInfo.Gender -eq 'Male' } | Select-Object -First 1; "
                        "if (-not $v) { $v = $s.GetInstalledVoices() | Where-Object { $_.VoiceInfo.Culture.Name -like 'de*' } | Select-Object -First 1 }; "
                        "if ($v) { $s.SelectVoice($v.VoiceInfo.Name) }; "
                        "$s.Rate = -2; "
                        "$escaped = [System.Security.SecurityElement]::Escape($env:JARVIS_TTS_TEXT); "
                        "$ssml = \"<speak version='1.0' xml:lang='de-DE'><prosody pitch='-15%' rate='-6%'>$escaped</prosody></speak>\"; "
                        "try { $s.SpeakSsml($ssml) } catch { $s.Speak($env:JARVIS_TTS_TEXT) }"
                    )
                    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                    startupinfo = subprocess.STARTUPINFO()
                    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                    startupinfo.wShowWindow = 0
                    proc = subprocess.Popen(
                        ["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", ps_script],
                        env=env,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        creationflags=flags,
                        startupinfo=startupinfo,
                    )
                    ps_started = time.monotonic()
                    while proc.poll() is None:
                        if (abort_check is not None and abort_check()) or (
                            (time.monotonic() - ps_started) >= MAX_TTS_PLAYBACK_SECONDS
                        ):
                            try:
                                proc.terminate()
                                proc.wait(timeout=1.0)
                            except Exception:
                                try:
                                    proc.kill()
                                except Exception:
                                    pass
                            return
                        time.sleep(0.05)
                    return

                for cmd in ("espeak-ng", "espeak"):
                    if shutil.which(cmd):
                        subprocess.run(
                            [cmd, "-v", "de", "-p", "28", "-s", "150", cleaned],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            timeout=MAX_TTS_PLAYBACK_SECONDS,
                            check=False,
                        )
                        return
                if shutil.which("spd-say"):
                    subprocess.run(
                        ["spd-say", "-l", "de", "-p", "-35", "-r", "-15", cleaned],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=MAX_TTS_PLAYBACK_SECONDS,
                        check=False,
                    )
                    return
        finally:
            if self.on_speaking_level is not None:
                try:
                    self.on_speaking_level(0.0)
                except Exception:
                    pass

    def _synthesize_offline_chunk(self, cleaned: str) -> tuple[Optional[np.ndarray], int, Optional[str]]:
        if self.voice is not None:
            try:
                audio, sr = self._synthesize_piper_pcm(cleaned)
                return audio, sr, None
            except Exception as exc:
                print(f"[TTS-Warnung] Piper-Synthese fehlgeschlagen ({exc}) – wechsle auf Notfall-Fallback.")
        return None, 0, cleaned

    def _synthesize_chunk(self, text: str) -> tuple[Optional[np.ndarray], int, Optional[str]]:
        """Wandelt einen Satz-Chunk in PCM um – im Offline-Modus strikt ohne edge-tts."""
        cleaned = _clean_for_speech(text)
        if not cleaned:
            return np.empty(0, dtype=np.float32), 22050, None

        if self.offline_only or not get_is_online():
            return self._synthesize_offline_chunk(cleaned)

        if time.monotonic() >= self._edge_cooldown_until:
            try:
                audio, sr = self._synthesize_edge_pcm(cleaned)
                return audio, sr, None
            except Exception:
                # Kurzer Cooldown (20s), damit Folgesätze bei Netz-Ausfall sofort ohne Delay lokal laufen
                self._edge_cooldown_until = time.monotonic() + 20.0
                print("[TTS-Fallback: edge-tts Chunk fehlgeschlagen – wechsle nahtlos auf lokale Stimme]")

        return self._synthesize_offline_chunk(cleaned)

    def _synthesize_chunk_pcm(self, text: str) -> tuple[np.ndarray, int]:
        """Kompatibler Helfer für reinen PCM-Abruf."""
        audio, sr, _ = self._synthesize_chunk(text)
        if audio is None:
            return np.empty(0, dtype=np.float32), 22050
        return audio, sr

    def synthesize_wav_bytes(
        self,
        text: str,
        voice: Optional[str] = None,
        pitch: Optional[str] = None,
        rate: Optional[str] = None,
    ) -> bytes:
        """Synthetisiert Text in der tiefen, rauen Standardstimme (ConradNeural/KillianNeural -> Piper) als WAV-Bytes für PC & Mobile (/tts)."""
        cleaned = _clean_for_speech(text)
        if not cleaned:
            return b""

        target_voice = (voice or self.edge_voice or DEFAULT_EDGE_VOICE).strip()
        target_pitch = (pitch or self.edge_pitch or DEFAULT_EDGE_PITCH).strip()
        if target_pitch.endswith("%"):
            target_pitch = target_pitch[:-1] + "Hz"
        target_rate = (rate or self.edge_rate or DEFAULT_EDGE_RATE).strip()

        audio: Optional[np.ndarray] = None
        sr: int = 24000

        if not self.offline_only and get_is_online():
            for cand_voice in (target_voice, self.edge_fallback_voice or DEFAULT_EDGE_FALLBACK_VOICE):
                if not cand_voice:
                    continue
                try:
                    mp3_bytes = asyncio.run(
                        asyncio.wait_for(
                            self._fetch_edge_mp3(
                                cleaned,
                                voice=cand_voice,
                                pitch=target_pitch,
                                rate=target_rate,
                            ),
                            timeout=self.edge_timeout,
                        )
                    )
                    raw_audio, raw_sr = _decode_mp3_bytes(mp3_bytes)
                    audio = _apply_deep_raspy_filter(
                        raw_audio,
                        raw_sr,
                        drive=1.18,
                        roughness=0.0,
                        bass_boost=0.16,
                    )
                    sr = raw_sr
                    break
                except Exception:
                    continue

        if audio is None or audio.size == 0:
            audio, sr, _ = self._synthesize_offline_chunk(cleaned)

        if audio is None or audio.size == 0:
            return b""

        return encode_pcm_to_wav_bytes(audio, sr)

    def start_stream(
        self,
        on_playback_start: Optional[Callable[[], None]] = None,
    ) -> SpeechStreamSession:
        """Startet eine thread-sichere Streaming-Ausgabe für token-/satzweises Vorlesen."""
        acquired = self._speak_lock.acquire(timeout=MAX_TTS_PLAYBACK_SECONDS + 2.0)
        if not acquired:
            print("[TTS-Warnung] _speak_lock Timeout nach 22s – erzwinge Reset für nächste Runde.")
            if self._active_session is not None:
                try:
                    self._active_session.stop()
                except Exception:
                    pass
            try:
                self._speak_lock.release()
            except RuntimeError:
                pass
            self.speaking_event.clear()
            if self.on_speaking_change is not None:
                try:
                    self.on_speaking_change(False)
                except Exception:
                    pass
            self._speak_lock.acquire(timeout=1.0)
        try:
            session = SpeechStreamSession(self, on_playback_start=on_playback_start)
            self._active_session = session
            return session
        except Exception:
            self.speaking_event.clear()
            if self.on_speaking_change is not None:
                try:
                    self.on_speaking_change(False)
                except Exception:
                    pass
            try:
                self._speak_lock.release()
            except RuntimeError:
                pass
            raise

    def speak(self, text: str) -> None:
        """Spricht einen vollständigen Text satzweise mit Vorab-Pufferung über ConradNeural/KillianNeural bzw. Karlsson."""
        speech_text = _clean_for_speech(text)
        if not speech_text:
            return

        session = self.start_stream()
        try:
            session.push_sentence(speech_text)
        finally:
            try:
                session.finish(wait=True, timeout=MAX_TTS_PLAYBACK_SECONDS)
            finally:
                self.speaking_event.clear()
                if self.on_speaking_change is not None:
                    try:
                        self.on_speaking_change(False)
                    except Exception:
                        pass


def encode_pcm_to_wav_bytes(audio: np.ndarray, sample_rate: int) -> bytes:
    """Kodiert ein float32-PCM-Array in 16-Bit-Mono-WAV-Bytes für Browser & Mobile-Streaming."""
    if audio is None or audio.size == 0 or sample_rate <= 0:
        return b""
    mono = audio[:, 0] if audio.ndim > 1 else audio
    clipped = np.clip(mono, -1.0, 1.0)
    pcm16 = (clipped * 32767.0).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(int(sample_rate))
        wf.writeframes(pcm16.tobytes())
    return buf.getvalue()


async def _collect_edge_mp3_async(
    cleaned_text: str,
    voice: str = DEFAULT_EDGE_FALLBACK_VOICE,
    fallback_voice: str = DEFAULT_EDGE_VOICE,
    pitch: str = DEFAULT_EDGE_PITCH,
    rate: str = DEFAULT_EDGE_RATE,
    timeout: float = 9.0,
) -> bytes:
    """Streamt rohe MP3-Bytes über edge-tts für den ersten verfügbaren Sprecher."""
    norm_pitch = pitch[:-1] + "Hz" if pitch.endswith("%") else pitch
    tried: list[str] = []
    for candidate in (voice, fallback_voice, DEFAULT_EDGE_FALLBACK_VOICE, DEFAULT_EDGE_VOICE):
        v = (candidate or "").strip()
        if not v or v in tried:
            continue
        tried.append(v)
        try:
            comm = edge_tts.Communicate(cleaned_text, voice=v, pitch=norm_pitch, rate=rate)
            chunks: list[bytes] = []

            async def _collect() -> None:
                async for chunk in comm.stream():
                    if chunk.get("type") == "audio" and isinstance(chunk.get("data"), bytes):
                        chunks.append(chunk["data"])

            await asyncio.wait_for(_collect(), timeout=timeout)
            if chunks:
                return b"".join(chunks)
        except Exception:
            continue
    return b""


async def synthesize_edge_tts_mp3_async(
    text: str,
    voice: str = DEFAULT_EDGE_FALLBACK_VOICE,
    fallback_voice: str = DEFAULT_EDGE_VOICE,
    pitch: str = DEFAULT_EDGE_PITCH,
    rate: str = DEFAULT_EDGE_RATE,
    timeout: float = 9.0,
    tts_engine: Optional[object] = None,
) -> tuple[bytes, str]:
    """
    Erzeugt komprimiertes Jarvis-Audio für Mobile-Streaming:
    1. Online via edge-tts (`de-DE-KillianNeural` / `de-DE-ConradNeural`) als `audio/mp3`
    2. Offline-Fallback via lokalem Piper als `audio/wav`.
    """
    cleaned = _clean_for_speech(text)
    if not cleaned:
        return b"", "audio/mp3"

    if get_is_online():
        mp3_bytes = await _collect_edge_mp3_async(
            cleaned,
            voice=voice,
            fallback_voice=fallback_voice,
            pitch=pitch,
            rate=rate,
            timeout=timeout,
        )
        if mp3_bytes:
            return mp3_bytes, "audio/mp3"

    if tts_engine is not None:
        try:
            if hasattr(tts_engine, "_synthesize_offline_chunk"):
                pcm, sr, _ = await asyncio.to_thread(tts_engine._synthesize_offline_chunk, cleaned)  # type: ignore[attr-defined]
                wav_bytes = encode_pcm_to_wav_bytes(pcm, sr)
                if wav_bytes:
                    return wav_bytes, "audio/wav"
            elif hasattr(tts_engine, "synthesize_wav_bytes"):
                wav_bytes = await asyncio.to_thread(tts_engine.synthesize_wav_bytes, cleaned, voice, pitch, rate)  # type: ignore[attr-defined]
                if wav_bytes:
                    return wav_bytes, "audio/wav"
        except Exception:
            pass

    return b"", "audio/mp3"


async def synthesize_edge_tts_wav_async(
    text: str,
    voice: str = DEFAULT_EDGE_VOICE,
    fallback_voice: str = DEFAULT_EDGE_FALLBACK_VOICE,
    pitch: str = DEFAULT_EDGE_PITCH,
    rate: str = DEFAULT_EDGE_RATE,
    timeout: float = 9.0,
) -> bytes:
    """Asynchroner Helfer für FastAPI-Endpunkte (/tts), falls keine TTS-Instanz übergeben wurde."""
    cleaned = _clean_for_speech(text)
    if not cleaned:
        return b""
    mp3_bytes = await _collect_edge_mp3_async(
        cleaned,
        voice=voice,
        fallback_voice=fallback_voice,
        pitch=pitch,
        rate=rate,
        timeout=timeout,
    )
    if mp3_bytes:
        try:
            raw_audio, sr = _decode_mp3_bytes(mp3_bytes)
            processed = _apply_deep_raspy_filter(
                raw_audio,
                sr,
                drive=1.18,
                roughness=0.0,
                bass_boost=0.16,
            )
            return encode_pcm_to_wav_bytes(processed, sr)
        except Exception:
            return b""
    return b""
