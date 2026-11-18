"""
Public Beta Status & Feedback Manager (`license_manager.py`) für J.A.R.V.I.S. OS // PUBLIC BETA v0.1
====================================================================================================
Offene Public-Beta-Edition:
- Keine Lizenzschlüssel-Abfrage und keine zeitliche Begrenzung – alle Funktionen stehen direkt offen.
- Speichert optionales Nutzerprofil und lokales Beta-Feedback unter `beta_feedback/`.
"""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import threading
import time
from typing import Any

from config import get_user_data_dir

BETA_VERSION = "PUBLIC BETA v0.1"
PUBLIC_BETA_OPEN = True


class LicenseManager:
    """Offener Public-Beta-Manager (keine Lizenzsperre) inkl. lokalem Feedback-Logger."""

    def __init__(self, license_path: str | Path = "") -> None:
        if license_path:
            self.path = Path(license_path)
        else:
            self.path = get_user_data_dir() / "beta_profile.json"
        self._lock = threading.RLock()
        self._data: dict[str, Any] = self._load()

    def _default_record(self) -> dict[str, Any]:
        return {
            "version": BETA_VERSION,
            "activated": True,
            "tier": "PUBLIC BETA",
            "user_name": "Beta-Tester",
            "activated_at": "",
        }

    def _load(self) -> dict[str, Any]:
        default = self._default_record()
        if not self.path.is_file():
            return default
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for k, v in default.items():
                    raw.setdefault(k, v)
                return raw
        except Exception:
            pass
        return default

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def get_status(self) -> dict[str, Any]:
        """Liefert immer den freien Public-Beta-Status ohne Aktivierungssperre."""
        with self._lock:
            user_name = str(self._data.get("user_name") or "").strip() or "Beta-Tester"
            return {
                "beta_version": BETA_VERSION,
                "public_beta": True,
                "activated": True,
                "needs_activation": False,
                "tier": "PUBLIC BETA",
                "user_name": user_name,
                "masked_key": "",
                "activated_at": str(self._data.get("activated_at") or ""),
                "trial_days_left": 0,
                "expires_at": "",
            }

    def activate_key(self, key: str = "", user_name: str = "") -> dict[str, Any]:
        """Kompatibilitäts-Endpunkt: aktualisiert optional den Namen, ohne einen Key zu verlangen."""
        clean_user = str(user_name or "").strip() or "Beta-Tester"
        with self._lock:
            self._data.update(
                {
                    "version": BETA_VERSION,
                    "activated": True,
                    "tier": "PUBLIC BETA",
                    "user_name": clean_user,
                    "activated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
            )
            self._save()
        st = self.get_status()
        return {
            "ok": True,
            "message": f"J.A.R.V.I.S. {BETA_VERSION} ist frei zugänglich ('{clean_user}').",
            "status": st,
        }

    def start_trial(self, user_name: str = "") -> dict[str, Any]:
        """Kompatibilitäts-Endpunkt für die offene Public Beta."""
        return self.activate_key(user_name=user_name)

    def submit_feedback(self, message: str, category: str = "feedback", contact: str = "") -> dict[str, Any]:
        """Speichert einen Beta-Feedback- oder Bug-Report lokal im Ordner `beta_feedback/`."""
        clean_msg = str(message or "").strip()
        if not clean_msg:
            return {"ok": False, "error": "Bitte gib eine kurze Nachricht ein."}

        fb_dir = self.path.parent / "beta_feedback"
        fb_dir.mkdir(parents=True, exist_ok=True)
        ticket_id = f"BETA-{int(time.time())}"
        st = self.get_status()
        payload = {
            "ticket_id": ticket_id,
            "beta_version": BETA_VERSION,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "category": str(category or "feedback").strip(),
            "user_name": st.get("user_name") or "Beta-Tester",
            "tier": "PUBLIC BETA",
            "contact": str(contact or "").strip(),
            "message": clean_msg,
        }
        report_file = fb_dir / f"{ticket_id}.json"
        report_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return {
            "ok": True,
            "ticket_id": ticket_id,
            "report_path": str(report_file),
            "message": f"Danke für dein Feedback! Report {ticket_id} wurde lokal unter beta_feedback/ gespeichert.",
        }


LICENSE_MANAGER = LicenseManager()
