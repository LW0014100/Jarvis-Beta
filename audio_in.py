"""Plattformunabhängige Mikrofon-Aufnahme mit dauerhaftem 30s-Ringspeicher, VAD und WASAPI/MME-Fallback."""

from __future__ import annotations

from collections import deque
import queue
import sys
import threading
import time
from typing import Any, Callable, Optional

import numpy as np
import sounddevice as sd

try:
    import webrtcvad
except ImportError:  # Plattformunabhängiger NumPy-Fallback ohne C-Compiler
    webrtcvad = None

OpenWakeWordModel = None  # Lazy bei Bedarf statt beim Turbo-Start laden


class WakeWordDetector:
    """Schlanker ONNX-Wake-Word-Detektor auf Basis von openwakeword (kein PyTorch)."""

    def __init__(self, wakeword: str = "hey_jarvis", threshold: float = 0.5) -> None:
        self.wakeword = wakeword
        self.threshold = float(threshold)
        self._model: Any = None
        self._load_error: str = ""

    def _ensure_loaded(self) -> Any:
        if self._model is not None:
            return self._model
        global OpenWakeWordModel
        if OpenWakeWordModel is None:
            from openwakeword.model import Model as _OWWModel  # type: ignore[import-not-found]

            OpenWakeWordModel = _OWWModel
        self._model = OpenWakeWordModel(
            wakeword_models=[self.wakeword] if self.wakeword else [],
            inference_framework="onnx",
        )
        return self._model

    def score_frame(self, pcm_int16: np.ndarray) -> float:
        """Berechnet den Wake-Word-Score [0..1] für ein 16-kHz-Mono-int16-Frame."""
        if pcm_int16.size == 0:
            return 0.0
        try:
            model = self._ensure_loaded()
            preds = model.predict(pcm_int16)
            if not preds:
                return 0.0
            return float(max(preds.values()))
        except Exception as exc:
            self._load_error = str(exc)
            return 0.0

    def detected(self, pcm_int16: np.ndarray) -> bool:
        return self.score_frame(pcm_int16) >= self.threshold


TARGET_SAMPLE_RATE = 16000
FRAME_DURATION_MS = 30
TARGET_FRAME_SAMPLES = int(TARGET_SAMPLE_RATE * FRAME_DURATION_MS / 1000)  # 480 Samples
DEFAULT_RING_SECONDS = 30.0
DEFAULT_SILENCE_DURATION = 0.8
VAD_THRESHOLD = 115.0
STREAM_RESTART_DELAY_SEC = 1.0


def _resample_to_16k(audio_int16: np.ndarray, src_rate: int) -> np.ndarray:
    """Resampelt ein Mono-int16-Signal bei Bedarf sauber im RAM auf 16 kHz."""
    if src_rate == TARGET_SAMPLE_RATE or audio_int16.size == 0:
        return audio_int16

    target_len = max(1, int(round(audio_int16.size * TARGET_SAMPLE_RATE / src_rate)))
    x_old = np.linspace(0.0, 1.0, num=audio_int16.size, endpoint=False)
    x_new = np.linspace(0.0, 1.0, num=target_len, endpoint=False)
    resampled = np.interp(x_new, x_old, audio_int16.astype(np.float32))
    return np.clip(np.round(resampled), -32768, 32767).astype(np.int16)


def select_input_device(preferred: Optional[int | str] = None) -> tuple[Optional[int], dict[str, Any]]:
    """
    Ermittelt ein gültiges Eingabegerät (Standardgerät via sd.default.device[0] oder Auto-Fallback).
    """
    if preferred is not None:
        info = dict(sd.query_devices(preferred, kind="input"))
        if int(info.get("max_input_channels", 0) or 0) > 0:
            idx = info.get("index")
            return (int(idx) if idx is not None else None), info

    try:
        default_in = sd.default.device[0]
    except Exception:
        default_in = -1

    if isinstance(default_in, (int, np.integer)) and int(default_in) >= 0:
        try:
            info = dict(sd.query_devices(int(default_in), kind="input"))
            if int(info.get("max_input_channels", 0) or 0) > 0:
                return int(default_in), info
        except Exception:
            pass

    candidates: list[tuple[int, int, dict[str, Any]]] = []
    for idx, dev in enumerate(sd.query_devices()):
        info = dict(dev)
        if int(info.get("max_input_channels", 0) or 0) <= 0:
            continue
        name_low = str(info.get("name", "")).lower()
        score = 1
        if any(tok in name_low for tok in ("mikrofon", "microphone", "mic", "headset", "array")):
            score += 3
        if "stereomix" in name_low or "stereo mix" in name_low or "output" in name_low:
            score -= 2
        candidates.append((score, idx, info))

    if candidates:
        candidates.sort(key=lambda item: (-item[0], item[1]))
        _, chosen_idx, chosen_info = candidates[0]
        return chosen_idx, chosen_info

    raise RuntimeError("Kein aktives Mikrofon / Eingabegerät gefunden (max_input_channels > 0 fehlt).")


class VoiceActivityDetector:
    """Empfindlicher Energie-Vorfilter + 0.5s Rauschkalibrierung + WebRTC-VAD / adaptives RMS."""

    def __init__(
        self,
        aggressiveness: int = 2,
        min_rms_threshold: float = VAD_THRESHOLD,
        noise_multiplier: float = 2.0,
        energy_gate_peak: int = 85,
        calibration_seconds: float = 0.5,
    ) -> None:
        self._vad = webrtcvad.Vad(aggressiveness) if webrtcvad is not None else None
        self._min_rms_threshold = float(min_rms_threshold)
        self._noise_multiplier = float(noise_multiplier)
        self._energy_gate_peak = int(energy_gate_peak)
        self._calibration_target = max(1, int(round(calibration_seconds * 1000.0 / FRAME_DURATION_MS)))
        self._calibration_count = 0
        self._noise_floor = 75.0
        self.last_rms = 0.0

    def reset_calibration(self) -> None:
        self._calibration_count = 0
        self._noise_floor = 75.0

    @property
    def noise_floor(self) -> float:
        return self._noise_floor

    def is_speech(self, frame_int16: np.ndarray) -> bool:
        if frame_int16.size != TARGET_FRAME_SAMPLES:
            self.last_rms = 0.0
            return False

        rms = float(np.sqrt(np.mean(frame_int16.astype(np.float32) ** 2)))
        self.last_rms = rms

        # Erste 0.5 Sekunden Umgebungsgeräusch schnell kalibrieren
        if self._calibration_count < self._calibration_target:
            self._calibration_count += 1
            if self._calibration_count == 1:
                self._noise_floor = max(25.0, min(420.0, rms))
            else:
                self._noise_floor = 0.80 * self._noise_floor + 0.20 * min(450.0, rms)
            # Nur sehr klare Sprache schon während Kalibrierung durchlassen
            if rms < max(self._min_rms_threshold * 1.9, self._noise_floor * 3.0):
                return False

        peak = int(np.max(np.abs(frame_int16)))
        if peak < self._energy_gate_peak and rms < self._min_rms_threshold * 0.85:
            self._noise_floor = 0.96 * self._noise_floor + 0.04 * rms
            return False

        threshold = max(self._min_rms_threshold, self._noise_floor * self._noise_multiplier)
        rms_hit = rms >= threshold

        if self._vad is not None:
            try:
                webrtc_hit = bool(self._vad.is_speech(frame_int16.tobytes(), TARGET_SAMPLE_RATE))
            except Exception:
                webrtc_hit = False
            speech_detected = webrtc_hit or rms >= (threshold * 1.25)
        else:
            speech_detected = rms_hit

        if not speech_detected:
            self._noise_floor = 0.95 * self._noise_floor + 0.05 * min(rms, threshold)

        return speech_detected


class AudioRecorder:
    """Dauerhafter freihändiger Mikrofon-Stream mit 30s-Ringspeicher, VAD, Auto-Recovery und Echo-Blocking."""

    def __init__(
        self,
        device: Optional[int | str] = None,
        vad_aggressiveness: int = 2,
        mute_event: Optional[threading.Event] = None,
        on_level: Optional[Callable[[float, bool], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
        on_vad_change: Optional[Callable[[bool], None]] = None,
        ring_seconds: float = DEFAULT_RING_SECONDS,
        silence_duration: float = DEFAULT_SILENCE_DURATION,
        min_speech_seconds: float = 0.22,
        pre_roll_seconds: float = 0.35,
        max_utterance_seconds: float = 30.0,
        echo_cooldown_seconds: float = 0.25,
    ) -> None:
        self._requested_device = device
        self.device: Optional[int | str] = device
        self.device_name: str = "Standard-Mikrofon"
        self.input_dtype: str = "int16"
        self.vad = VoiceActivityDetector(aggressiveness=vad_aggressiveness)
        self.mute_event = mute_event
        self.on_level = on_level
        self.on_error = on_error
        self.on_vad_change = on_vad_change
        self.last_error: str = ""
        self.last_rms_level: float = 0.0
        self.current_level: float = 0.0
        self.last_level_pct: int = 0
        self.callback_count: int = 0
        self.input_sample_rate = self._resolve_sample_rate()

        self._frame_sec = FRAME_DURATION_MS / 1000.0
        self._ring_max_frames = max(1, int(round(ring_seconds / self._frame_sec)))
        self._silence_frames_needed = max(1, int(round(silence_duration / self._frame_sec)))
        self._min_speech_frames = max(1, int(round(min_speech_seconds / self._frame_sec)))
        self._pre_roll_frames = max(1, int(round(pre_roll_seconds / self._frame_sec)))
        self._max_total_frames = max(1, int(round(max_utterance_seconds / self._frame_sec)))
        self._echo_cooldown_seconds = echo_cooldown_seconds

        self._raw_queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=200)
        self.audio_queue = self._raw_queue
        self._utterance_queue: queue.Queue[tuple[np.ndarray, int]] = queue.Queue(maxsize=16)

        self._ring_lock = threading.Lock()
        self._ring_buffer: deque[tuple[int, np.ndarray, bool]] = deque(maxlen=self._ring_max_frames)
        self._frame_seq = 0
        self._consumed_seq = 0

        self._stream: Optional[sd.InputStream] = None
        self._stream_lock = threading.Lock()
        self._stop_capture = threading.Event()
        self.manual_mute_event = threading.Event()
        self._stream_needs_restart = threading.Event()
        self.is_speaking: bool = False
        self._last_callback_mono = time.monotonic()
        self._consecutive_status_errors = 0
        self._consecutive_restart_failures = 0
        self._active_default_index: Optional[int] = None
        self.host_api_name: str = ""
        self._last_default_check_mono = 0.0
        self._capture_thread: Optional[threading.Thread] = None

    def pause_listening(self) -> None:
        """Pausiert das Mikrofon-VAD sauber während Jarvis spricht (Anti-Echo)."""
        self.is_speaking = True

    def resume_listening(self) -> None:
        """Reaktiviert das Mikrofon-VAD garantiert nach Beendigung oder Fehler von TTS."""
        self.is_speaking = False
        if self.mute_event is not None:
            try:
                self.mute_event.clear()
            except Exception:
                pass

    def _emit_error(self, message: str) -> None:
        text = message if message.startswith("[MIC-ERROR]") else f"[MIC-ERROR] {message}"
        self.last_error = text
        print(text, file=sys.stderr)
        if self.on_error is not None:
            try:
                self.on_error(text)
            except Exception:
                pass

    def _candidate_configs(self) -> tuple[Optional[int], dict[str, Any], list[tuple[int, str]]]:
        dev_idx, dev_info = select_input_device(self._requested_device)
        native_sr = int(round(float(dev_info.get("default_samplerate") or 44100)))
        raw_candidates = [
            (TARGET_SAMPLE_RATE, "int16"),
            (TARGET_SAMPLE_RATE, "float32"),
            (native_sr, "int16"),
            (native_sr, "float32"),
            (48000, "int16"),
            (48000, "float32"),
            (44100, "int16"),
            (44100, "float32"),
        ]
        seen: set[tuple[int, str]] = set()
        ordered: list[tuple[int, str]] = []
        for sr, dt in raw_candidates:
            if sr > 0 and (sr, dt) not in seen:
                seen.add((sr, dt))
                ordered.append((sr, dt))
        return dev_idx, dev_info, ordered

    def _resolve_sample_rate(self) -> int:
        try:
            dev_idx, dev_info, candidates = self._candidate_configs()
            self.device = dev_idx
            self.device_name = str(dev_info.get("name") or "Mikrofon")
            for sr, dt in candidates:
                try:
                    sd.check_input_settings(device=dev_idx, samplerate=sr, channels=1, dtype=dt)
                    self.input_dtype = dt
                    return sr
                except Exception:
                    continue
            return int(round(float(dev_info.get("default_samplerate") or TARGET_SAMPLE_RATE)))
        except Exception as exc:
            self._emit_error(f"Geräte-Erkennung fehlgeschlagen: {exc}")
            return TARGET_SAMPLE_RATE

    def _close_stream_only(self) -> None:
        with self._stream_lock:
            if self._stream is not None:
                try:
                    self._stream.stop()
                except Exception:
                    pass
                try:
                    self._stream.close()
                except Exception:
                    pass
                self._stream = None

    def _ensure_stream(self) -> None:
        with self._stream_lock:
            if self._stream is not None and self._stream.active:
                return
            if self._stream is not None:
                try:
                    self._stream.close()
                except Exception:
                    pass
                self._stream = None

            dev_idx, dev_info, candidates = self._candidate_configs()
            self.device = dev_idx
            self.device_name = str(dev_info.get("name") or "Mikrofon")
            self._active_default_index = dev_idx
            try:
                host_apis = sd.query_hostapis()
                ha_idx = int(dev_info.get("hostapi", 0))
                self.host_api_name = str(host_apis[ha_idx].get("name") or "PortAudio")
            except Exception:
                self.host_api_name = "PortAudio"
            self._stream_needs_restart.clear()
            self._consecutive_status_errors = 0
            self._last_callback_mono = time.monotonic()

            def _callback(indata: np.ndarray, frames: int, time_info: object, status: sd.CallbackFlags) -> None:
                # Minimaler, 100% blockadefreier Audio-Callback (keine Locks, kein WebSocket, kein I/O)
                self._last_callback_mono = time.monotonic()
                self.callback_count += 1
                if status:
                    self._consecutive_status_errors += 1
                    if self._consecutive_status_errors >= 5:
                        self._stream_needs_restart.set()
                else:
                    self._consecutive_status_errors = 0

                if indata.size > 0:
                    mono = indata[:, 0]
                    norm_val = float(np.linalg.norm(mono) * 10.0)
                    denom = (32768.0 * max(1.0, float(np.sqrt(mono.size)))) if mono.dtype == np.int16 else max(1.0, float(np.sqrt(mono.size)))
                    self.current_level = max(0.0, min(1.0, norm_val / denom))
                    if not (self.manual_mute_event.is_set() or (self.mute_event is not None and self.mute_event.is_set())):
                        try:
                            self.audio_queue.put_nowait(mono.copy())
                        except queue.Full:
                            try:
                                self.audio_queue.get_nowait()
                                self.audio_queue.put_nowait(mono.copy())
                            except queue.Empty:
                                pass

            open_errors: list[str] = []
            for sr, dt in candidates:
                blocksize = max(1, int(round(sr * 0.05)))  # 50 ms Blöcke für flüssiges HUD-Level
                stream: Optional[sd.InputStream] = None
                try:
                    stream = sd.InputStream(
                        device=dev_idx,
                        samplerate=sr,
                        channels=1,
                        dtype=dt,
                        blocksize=blocksize,
                        callback=_callback,
                    )
                    stream.start()
                    self.input_sample_rate = sr
                    self.input_dtype = dt
                    self._stream = stream
                    self.vad.reset_calibration()
                    self.last_error = ""
                    self._consecutive_restart_failures = 0
                    print(
                        f"[Audio-Start] Aktives Mikrofon: '{self.device_name}' "
                        f"(Device #{dev_idx if dev_idx is not None else 'default'}, "
                        f"Host-API: {self.host_api_name or 'PortAudio'}, {sr} Hz, {dt})."
                    )
                    return
                except Exception as exc:
                    open_errors.append(f"{sr}Hz/{dt}: {exc}")
                    if stream is not None:
                        try:
                            stream.close()
                        except Exception:
                            pass

            raise RuntimeError("; ".join(open_errors) or "Unbekannter PortAudio-Fehler beim Öffnen des Mikrofons")

    def _restart_stream(self, delay: float = STREAM_RESTART_DELAY_SEC) -> bool:
        self._close_stream_only()
        self._drain_queue(self._raw_queue)
        effective_delay = delay if self._consecutive_restart_failures == 0 else min(3.0, max(delay, 1.0 + 0.5 * self._consecutive_restart_failures))
        if effective_delay > 0 and self._stop_capture.wait(effective_delay):
            return False
        if self._consecutive_restart_failures >= 1:
            try:
                sd._terminate()
                sd._initialize()
            except Exception:
                pass
        try:
            self._ensure_stream()
            self._consecutive_restart_failures = 0
            print(
                f"[Audio-Watchdog] Mikrofon-Stream wiederhergestellt: {self.device_name} "
                f"({self.input_sample_rate} Hz, {self.input_dtype})."
            )
            return True
        except Exception as exc:
            self._consecutive_restart_failures += 1
            self._emit_error(f"Stream-Neustart fehlgeschlagen (Versuch {self._consecutive_restart_failures}): {exc}")
            return False

    def _is_stream_healthy(self, now_mono: float) -> bool:
        if self._stream_needs_restart.is_set():
            return False
        with self._stream_lock:
            stream_ok = self._stream is not None and bool(self._stream.active)
        if not stream_ok:
            return False
        if (now_mono - self._last_callback_mono) > 3.0:
            return False
        return True

    @staticmethod
    def _drain_queue(q: queue.Queue) -> None:
        while not q.empty():
            try:
                q.get_nowait()
            except queue.Empty:
                break

    def start(self) -> None:
        """Startet Mikrofon-Stream & 30s-Ringspeicher sofort und nicht-blockierend im Hintergrund."""
        self._stop_capture.clear()
        try:
            self._ensure_stream()
        except Exception as exc:
            self._emit_error(f"Initialer Stream-Start fehlgeschlagen ({exc}) – Neustart in 1.0s.")
        if self._capture_thread is None or not self._capture_thread.is_alive():
            self._capture_thread = threading.Thread(
                target=self._capture_loop,
                name="JarvisMicCapture",
                daemon=True,
            )
            self._capture_thread.start()

    def ensure_alive(self) -> None:
        """Stellt sicher, dass der Capture-/Watchdog-Thread aktiv läuft."""
        if self._stop_capture.is_set():
            return
        if self._capture_thread is None or not self._capture_thread.is_alive():
            print("[Audio-Watchdog] Capture-Thread neu gestartet.")
            self.start()

    def is_manually_muted(self) -> bool:
        return self.manual_mute_event.is_set()

    def set_manual_mute(self, muted: bool) -> bool:
        if muted:
            self.manual_mute_event.set()
            self._emit_level(0.0, False)
        else:
            self.manual_mute_event.clear()
        return self.manual_mute_event.is_set()

    def toggle_manual_mute(self) -> bool:
        return self.set_manual_mute(not self.manual_mute_event.is_set())

    def close(self) -> None:
        """Beendet Hintergrund-Capture und schließt den Audio-Stream sauber."""
        self._stop_capture.set()
        if self._capture_thread is not None and self._capture_thread.is_alive():
            self._capture_thread.join(timeout=1.5)
        self._capture_thread = None
        self._close_stream_only()

    def mark_consumed(self, seq: Optional[int] = None) -> None:
        """Markiert Audio-Frames bis einschließlich seq als verarbeitet."""
        with self._ring_lock:
            target = self._frame_seq if seq is None else seq
            if target > self._consumed_seq:
                self._consumed_seq = target

    def clear_pending_utterances(self) -> None:
        """Verwirft bereits gepufferte, noch nicht abgeholte Segmente."""
        self._drain_queue(self._utterance_queue)

    def get_ring_buffer_audio(self, seconds: float = DEFAULT_RING_SECONDS) -> np.ndarray:
        """Gibt die letzten bis zu 30 Sekunden aus dem Ringspeicher als 16-kHz-float32 zurück."""
        max_frames = max(1, int(round(seconds / self._frame_sec)))
        with self._ring_lock:
            frames = [item[1] for item in list(self._ring_buffer)[-max_frames:]]
        if not frames:
            return np.empty(0, dtype=np.float32)
        return np.concatenate(frames).astype(np.float32) / 32768.0

    def get_recent_thought_audio(
        self,
        end_seq: int,
        max_gap_seconds: float = 2.4,
        max_seconds: float = DEFAULT_RING_SECONDS,
    ) -> np.ndarray:
        """Extrahiert einen zusammenhängenden Sprachgedanken inkl. kurzer Sprechpausen aus dem 30s-Puffer."""
        max_gap_frames = max(1, int(round(max_gap_seconds / self._frame_sec)))
        max_frames = max(1, int(round(max_seconds / self._frame_sec)))

        with self._ring_lock:
            snapshot = [
                item for item in self._ring_buffer if self._consumed_seq < item[0] <= end_seq
            ][-max_frames:]

        if not snapshot:
            return np.empty(0, dtype=np.float32)

        last_speech_idx = -1
        for idx in range(len(snapshot) - 1, -1, -1):
            if snapshot[idx][2]:
                last_speech_idx = idx
                break
        if last_speech_idx < 0:
            return np.empty(0, dtype=np.float32)

        first_speech_idx = last_speech_idx
        silent_run = 0
        for idx in range(last_speech_idx - 1, -1, -1):
            if snapshot[idx][2]:
                first_speech_idx = idx
                silent_run = 0
            else:
                silent_run += 1
                if silent_run > max_gap_frames:
                    break

        start_idx = max(0, first_speech_idx - self._pre_roll_frames)
        end_idx = min(len(snapshot), last_speech_idx + 8)
        selected = [item[1] for item in snapshot[start_idx:end_idx]]
        if not selected:
            return np.empty(0, dtype=np.float32)
        return np.concatenate(selected).astype(np.float32) / 32768.0

    def _emit_level(self, level: float, is_speech: bool) -> None:
        clamped = max(0.0, min(1.0, float(level)))
        self.last_rms_level = clamped
        self.last_level_pct = int(round(clamped * 100.0))
        if self.on_level is not None:
            try:
                self.on_level(clamped, bool(is_speech))
            except Exception:
                pass

    def _emit_vad_change(self, active: bool) -> None:
        if self.on_vad_change is not None:
            try:
                self.on_vad_change(bool(active))
            except Exception:
                pass

    def _push_utterance(self, frames: list[np.ndarray], end_seq: int) -> None:
        pcm = np.concatenate(frames).astype(np.float32) / 32768.0
        item = (pcm, end_seq)
        try:
            self._utterance_queue.put_nowait(item)
        except queue.Full:
            try:
                self._utterance_queue.get_nowait()
                self._utterance_queue.put_nowait(item)
            except queue.Empty:
                pass

    def _capture_loop(self) -> None:
        pre_roll: deque[np.ndarray] = deque(maxlen=self._pre_roll_frames)
        recorded: list[np.ndarray] = []
        sample_buffer = np.empty(0, dtype=np.int16)

        speech_started = False
        speech_frames = 0
        trailing_silence = 0
        mute_until_mono = 0.0
        was_muted = False
        last_level_emit = 0.0

        while not self._stop_capture.is_set():
            try:
                now_mono = time.monotonic()
                # Bei automatischer Standardgeräte-Wahl alle 4s auf Gerätewechsel prüfen (z.B. Headset eingesteckt)
                if self._requested_device is None and (now_mono - self._last_default_check_mono) >= 4.0:
                    self._last_default_check_mono = now_mono
                    try:
                        new_idx, new_info = select_input_device(None)
                        new_name = str(new_info.get("name") or "")
                        if (new_idx is not None and new_idx != self._active_default_index) or (
                            new_name and new_name != self.device_name
                        ):
                            print(
                                f"[Audio-Watchdog] Standard-Mikrofon geändert: "
                                f"'{self.device_name}' -> '{new_name}' (#{new_idx}). Wechsle Stream..."
                            )
                            self._restart_stream(delay=0.4)
                            continue
                    except Exception:
                        pass

                if not self._is_stream_healthy(now_mono):
                    self._emit_error("Mikrofon-Stream unterbrochen oder pausiert – automatischer Neustart nach 1.0s.")
                    if speech_started:
                        self._emit_vad_change(False)
                    speech_started = False
                    recorded.clear()
                    pre_roll.clear()
                    speech_frames = 0
                    trailing_silence = 0
                    sample_buffer = np.empty(0, dtype=np.int16)
                    self._restart_stream(delay=STREAM_RESTART_DELAY_SEC)
                    continue

                muted_now = bool(self.is_speaking or (self.mute_event is not None and self.mute_event.is_set()))
                if muted_now:
                    mute_until_mono = now_mono + self._echo_cooldown_seconds

                if muted_now or now_mono < mute_until_mono:
                    if not was_muted:
                        was_muted = True
                        if speech_started:
                            self._emit_vad_change(False)
                        speech_started = False
                        recorded.clear()
                        pre_roll.clear()
                        speech_frames = 0
                        trailing_silence = 0
                        sample_buffer = np.empty(0, dtype=np.int16)
                        self._emit_level(0.0, False)
                    self._drain_queue(self._raw_queue)
                    self._drain_queue(self._utterance_queue)
                    self.mark_consumed()
                    time.sleep(0.04)
                    continue

                if was_muted:
                    was_muted = False
                    self._drain_queue(self._raw_queue)

                try:
                    raw_chunk = self.audio_queue.get(timeout=0.1)
                except queue.Empty:
                    continue

                if raw_chunk.dtype != np.int16:
                    chunk = np.clip(raw_chunk.astype(np.float32) * 32767.0, -32768.0, 32767.0).astype(np.int16)
                else:
                    chunk = raw_chunk

                resampled = _resample_to_16k(chunk, self.input_sample_rate)
                if resampled.size == 0:
                    continue
                sample_buffer = np.concatenate((sample_buffer, resampled))

                while sample_buffer.size >= TARGET_FRAME_SAMPLES:
                    frame = sample_buffer[:TARGET_FRAME_SAMPLES]
                    sample_buffer = sample_buffer[TARGET_FRAME_SAMPLES:]
                    is_speech = self.vad.is_speech(frame)

                    with self._ring_lock:
                        self._frame_seq += 1
                        current_seq = self._frame_seq
                        self._ring_buffer.append((current_seq, frame, is_speech))

                    now_emit = time.monotonic()
                    if (now_emit - last_level_emit) >= 0.10:
                        rms = self.vad.last_rms
                        floor = self.vad.noise_floor
                        effective = max(0.0, rms - floor * 0.45)
                        level_norm = min(1.0, (effective / 1500.0) ** 0.72)
                        self._emit_level(max(level_norm, self.current_level), is_speech)
                        last_level_emit = now_emit

                    if not speech_started:
                        pre_roll.append(frame)
                        if is_speech:
                            speech_started = True
                            recorded.extend(pre_roll)
                            speech_frames = 1
                            trailing_silence = 0
                            self._emit_vad_change(True)
                    else:
                        recorded.append(frame)
                        if is_speech:
                            speech_frames += 1
                            trailing_silence = 0
                        else:
                            trailing_silence += 1

                        if (
                            trailing_silence >= self._silence_frames_needed
                            or len(recorded) >= self._max_total_frames
                        ):
                            if speech_frames >= self._min_speech_frames:
                                self._push_utterance(recorded, current_seq)
                            speech_started = False
                            recorded.clear()
                            pre_roll.clear()
                            speech_frames = 0
                            trailing_silence = 0
                            self._emit_vad_change(False)
            except Exception as exc:
                self._emit_error(f"Aufnahme-Loop Fehler ({exc}) – Neustart nach 1.0s.")
                if speech_started:
                    self._emit_vad_change(False)
                speech_started = False
                recorded.clear()
                pre_roll.clear()
                speech_frames = 0
                trailing_silence = 0
                sample_buffer = np.empty(0, dtype=np.int16)
                self._restart_stream(delay=STREAM_RESTART_DELAY_SEC)

    def next_utterance(
        self,
        timeout: Optional[float] = 1.5,
        stop_event: Optional[threading.Event] = None,
        extra_stop_check: Optional[Callable[[], bool]] = None,
    ) -> Optional[tuple[np.ndarray, int]]:
        """Wartet freihändig auf das nächste abgeschlossene Sprachsegment aus dem Ringspeicher."""
        self.start()
        deadline = None if timeout is None else (time.monotonic() + timeout)

        while not self._stop_capture.is_set():
            if (stop_event is not None and stop_event.is_set()) or (
                extra_stop_check is not None and extra_stop_check()
            ):
                return None

            remaining = 0.15
            if deadline is not None:
                left = deadline - time.monotonic()
                if left <= 0:
                    return None
                remaining = min(0.15, left)

            try:
                return self._utterance_queue.get(timeout=remaining)
            except queue.Empty:
                continue

        return None

    def record_utterance(
        self,
        max_seconds: float = 15.0,
        silence_duration: float = DEFAULT_SILENCE_DURATION,
        min_speech_seconds: float = 0.25,
        pre_roll_seconds: float = 0.35,
        wait_for_speech_timeout: Optional[float] = 8.0,
        stop_event: Optional[threading.Event] = None,
        extra_stop_check: Optional[Callable[[], bool]] = None,
        mute_event: Optional[threading.Event] = None,
        discard_pending: bool = True,
    ) -> Optional[np.ndarray]:
        """Kompatibilitäts-Wrapper für gezielte Rückfragen (Notiz, Wecker, optionaler Shortcut)."""
        if discard_pending:
            self.clear_pending_utterances()

        effective_mute = mute_event if mute_event is not None else self.mute_event
        wait_start = time.monotonic()
        while effective_mute is not None and effective_mute.is_set():
            if (stop_event is not None and stop_event.is_set()) or (
                extra_stop_check is not None and extra_stop_check()
            ):
                return None
            if (
                wait_for_speech_timeout is not None
                and (time.monotonic() - wait_start) >= wait_for_speech_timeout
            ):
                return None
            time.sleep(0.05)

        item = self.next_utterance(
            timeout=wait_for_speech_timeout,
            stop_event=stop_event,
            extra_stop_check=extra_stop_check,
        )
        if item is None:
            return None
        audio, end_seq = item
        self.mark_consumed(end_seq)
        max_samples = int(max_seconds * TARGET_SAMPLE_RATE)
        return audio[:max_samples] if audio.size > max_samples else audio


def start_listening(
    *,
    device: Optional[int | str] = None,
    mute_event: Optional[threading.Event] = None,
    on_level: Optional[Callable[[float, bool], None]] = None,
    on_error: Optional[Callable[[str], None]] = None,
    on_vad_change: Optional[Callable[[bool], None]] = None,
) -> AudioRecorder:
    """Hilfsfunktion zum sofortigen, garantierten Start des Mikrofon-Listeners."""
    from beta_guard import BETA_GUARD

    if BETA_GUARD.is_expired():
        raise RuntimeError("Beta-Phase beendet")
    recorder = AudioRecorder(
        device=device,
        mute_event=mute_event,
        on_level=on_level,
        on_error=on_error,
        on_vad_change=on_vad_change,
    )
    recorder.start()
    return recorder


class TerminalTrigger:
    """Optionaler Terminal-Shortcut für Texteingabe oder manuelles Testen (kein Zwang)."""

    def __init__(self) -> None:
        self.triggered = threading.Event()
        self._typed_lines: queue.Queue[str] = queue.Queue()
        self._thread = threading.Thread(target=self._listen_loop, daemon=True)

    def start(self) -> None:
        if not self._thread.is_alive():
            self._thread.start()

    def _listen_loop(self) -> None:
        while True:
            try:
                line = sys.stdin.readline()
            except Exception:
                break
            if line == "":
                break
            self._typed_lines.put(line.strip())
            self.triggered.set()

    def trigger_command(self, text: str) -> None:
        """Reiht einen UI-/Quick-Action-Befehl wie eine Eingabe zur sofortigen Ausführung ein."""
        cleaned = (text or "").strip()
        if cleaned:
            self._typed_lines.put(cleaned)
        self.triggered.set()

    def consume(self) -> tuple[bool, str]:
        """Gibt (aktiviert, optionaler_text) zurück, wenn Enter/Shortcut betätigt wurde."""
        if not self.triggered.is_set():
            return False, ""
        self.triggered.clear()
        text = ""
        while not self._typed_lines.empty():
            text = self._typed_lines.get_nowait()
        return True, text
