"""Simpler, offline-fähiger Beta-Datums-Check (Stichtag: 2027-02-01)."""

from __future__ import annotations

from datetime import datetime
import threading
from typing import Any, Callable, Optional

EXPIRY_DATE = datetime(2027, 2, 1)
EXPIRY_DISPLAY = "01.02.2027"
EXPIRED_RESPONSE: dict[str, Any] = {"expired": True, "message": "Beta-Phase beendet"}


class BetaGuard:
    """Minimaler Beta-Ablauf-Check anhand der lokalen Systemzeit."""

    def __init__(self) -> None:
        self._spoken_once = False

    def is_expired(self, now: Optional[datetime] = None) -> bool:
        return (now or datetime.now()) >= EXPIRY_DATE

    def get_status(self, now: Optional[datetime] = None) -> dict[str, Any]:
        if self.is_expired(now):
            return dict(EXPIRED_RESPONSE)
        return {
            "expired": False,
            "status": "active",
            "message": f"BETA ACTIVE · GÜLTIG BIS {EXPIRY_DISPLAY}",
            "expiry_display": EXPIRY_DISPLAY,
        }

    def get_api_expired_payload(self) -> dict[str, Any]:
        return dict(EXPIRED_RESPONSE)

    def announce_expired_once(self, log_fn: Optional[Callable[[str], None]] = None, output_device: Any = None) -> None:
        if self._spoken_once:
            return
        self._spoken_once = True
        msg = "Beta-Phase beendet"
        if log_fn is not None:
            try:
                log_fn(msg)
            except Exception:
                print(msg)
        else:
            print(msg)

        def _speak() -> None:
            try:
                from tts import TextToSpeech

                TextToSpeech(output_device=output_device).speak(msg)
                return
            except Exception:
                pass
            try:
                import pyttsx3  # type: ignore

                eng = pyttsx3.init()
                eng.say(msg)
                eng.runAndWait()
            except Exception:
                pass

        threading.Thread(target=_speak, daemon=True).start()


BETA_GUARD = BetaGuard()
