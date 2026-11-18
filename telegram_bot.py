"""Optionaler Telegram-Bot-Listener (Smartphone-Bridge für Befehle & Tisch-Kamera)."""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Callable, Optional
import urllib.error
import urllib.parse
import urllib.request
import uuid


CAMERA_KEYWORDS = ("foto", "bild", "kamera", "tisch", "werkbank", "cam", "snapshot", "/foto", "/cam")


def _encode_multipart(fields: dict[str, str], file_field: str, filename: str, file_bytes: bytes) -> tuple[bytes, str]:
    boundary = f"----JarvisBoundary{uuid.uuid4().hex}"
    chunks: list[bytes] = []

    for name, value in fields.items():
        chunks.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n"
            ).encode("utf-8")
        )

    chunks.append(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
            f"Content-Type: image/jpeg\r\n\r\n"
        ).encode("utf-8")
    )
    chunks.append(file_bytes)
    chunks.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


class TelegramBridge:
    """Leichtgewichtiger Telegram-Long-Polling-Listener ohne Zusatzpakete."""

    def __init__(
        self,
        on_command: Callable[[str], str],
        on_capture_photo: Callable[[], Optional[bytes]],
        on_log: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._on_command = on_command
        self._on_capture_photo = on_capture_photo
        self._on_log = on_log

        self._lock = threading.Lock()
        self._token = ""
        self._allowed_chat_id = ""
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._offset = 0

    def configure_from_env(self) -> bool:
        """Lädt TELEGRAM_BOT_TOKEN & TELEGRAM_ALLOWED_CHAT_ID neu und startet/stoppt den Bot."""
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        allowed = os.getenv("TELEGRAM_ALLOWED_CHAT_ID", "").strip()
        with self._lock:
            changed = token != self._token or allowed != self._allowed_chat_id
            self._token = token
            self._allowed_chat_id = allowed

        if token and (self._thread is None or not self._thread.is_alive()):
            self.start()
            return True
        if changed and not token:
            self.stop()
        return bool(token)

    def start(self) -> None:
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            return
        with self._lock:
            self._token = token
            self._allowed_chat_id = os.getenv("TELEGRAM_ALLOWED_CHAT_ID", "").strip()
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._poll_loop, daemon=True)
            self._thread.start()
        if self._on_log:
            self._on_log("Telegram-Bridge aktiv.")

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._thread = None

    def _api_call(self, method: str, payload: dict[str, object], timeout: float = 25.0) -> Optional[dict]:
        with self._lock:
            token = self._token
        if not token:
            return None

        url = f"https://api.telegram.org/bot{token}/{method}"
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def send_message(self, chat_id: str, text: str) -> None:
        if not text:
            return
        try:
            self._api_call("sendMessage", {"chat_id": chat_id, "text": text[:4000]}, timeout=10.0)
        except Exception as exc:
            print(f"[Telegram-Fehler sendMessage] {exc}")

    def send_photo(self, chat_id: str, photo_bytes: bytes, caption: str = "") -> None:
        with self._lock:
            token = self._token
        if not token:
            return
        url = f"https://api.telegram.org/bot{token}/sendPhoto"
        fields = {"chat_id": chat_id}
        if caption:
            fields["caption"] = caption[:1000]
        body, content_type = _encode_multipart(fields, "photo", "jarvis_desk_cam.jpg", photo_bytes)
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": content_type},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=20.0) as resp:
                resp.read()
        except Exception as exc:
            print(f"[Telegram-Fehler sendPhoto] {exc}")

    def _handle_message(self, chat_id: str, text: str) -> None:
        low = text.strip().lower()
        if not low:
            return

        if low in ("/start", "start"):
            self.send_message(
                chat_id,
                "Jarvis Smartphone-Bridge aktiv, Chef. Senden Sie beliebige Befehle oder 'Foto' für ein Tisch-Kamera-Bild.",
            )
            return

        if any(k in low for k in CAMERA_KEYWORDS):
            img = self._on_capture_photo()
            if img is None:
                self.send_message(chat_id, "Chef, die Kamera konnte kein Bild aufnehmen.")
                return
            self.send_photo(chat_id, img, caption="Aktuelles Bild der Tisch-Kamera, Chef.")
            if len(low.split()) <= 2:
                return

        reply = self._on_command(text)
        self.send_message(chat_id, reply or "Ausgeführt, Chef.")

    def _poll_loop(self) -> None:
        while not self._stop_event.is_set():
            with self._lock:
                token = self._token
                allowed = self._allowed_chat_id
            if not token:
                break

            try:
                data = self._api_call(
                    "getUpdates",
                    {"offset": self._offset, "timeout": 18, "allowed_updates": ["message"]},
                    timeout=24.0,
                )
                if not data or not data.get("ok"):
                    time.sleep(2.0)
                    continue

                for upd in data.get("result", []):
                    upd_id = int(upd.get("update_id", 0))
                    self._offset = max(self._offset, upd_id + 1)

                    msg = upd.get("message") or {}
                    chat_id = str((msg.get("chat") or {}).get("id") or "")
                    text = str(msg.get("text") or "").strip()
                    if not chat_id or not text:
                        continue
                    if allowed and chat_id != allowed:
                        continue

                    if self._on_log:
                        self._on_log(f"Telegram: {text[:45]}")
                    self._handle_message(chat_id, text)
            except urllib.error.URLError:
                self._stop_event.wait(3.0)
            except Exception as exc:
                print(f"[Telegram-Polling-Hinweis] {exc}")
                self._stop_event.wait(3.0)
