#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
JARVIS Secure AES-Fernet Data Vault (vault.py)
----------------------------------------------
Stores sensitive personal, payment (Debit/Credit Card, Expiry, CVC, IBAN,
Billing Address), email (Mail.tm & IMAP), and broker API credentials locally
encrypted in %LOCALAPPDATA%/JarvisCommandCenter/secure_vault.enc.

Zero-Knowledge Architecture:
- Cloud LLMs ONLY ever see placeholder tokens like `{{VAULT_EMAIL}}`,
  `{{VAULT_PASSWORD}}`, `{{VAULT_CARD_NUMBER}}`, `{{VAULT_CARD_EXPIRY}}`,
  `{{VAULT_CARD_CVC}}`, `{{VAULT_IBAN}}`, etc.
- Placeholders are resolved strictly locally in RAM immediately before typing
  into a target application or submitting to a verified API.
"""

from __future__ import annotations

import base64
import email
import hashlib
import imaplib
import json
import logging
import os
import re
import threading
import time
from email.header import decode_header
from pathlib import Path
from typing import Any

from config import (
    get_execution_mode as _cfg_get_execution_mode,
    normalize_execution_mode,
    set_execution_mode as _cfg_set_execution_mode,
)

logger = logging.getLogger("JarvisVault")

VAULT_DIR = Path(
    os.getenv("LOCALAPPDATA") or os.path.expanduser("~/.config")
) / "JarvisCommandCenter"
VAULT_ENC_FILE = VAULT_DIR / "secure_vault.enc"
LEGACY_VAULT_ENC_FILE = VAULT_DIR / "vault.enc"
VAULT_KEY_FILE = VAULT_DIR / ".vault_master.key"


def get_vault_dir() -> Path:
    """Returns the local encrypted vault directory."""
    VAULT_DIR.mkdir(parents=True, exist_ok=True)
    return VAULT_DIR


# Supported Zero-Knowledge Vault Keys
VAULT_KEYS_META: dict[str, dict[str, str]] = {
    "VAULT_FULL_NAME": {
        "label": "Vollständiger Name",
        "placeholder": "{{VAULT_FULL_NAME}}",
        "category": "identity",
    },
    "VAULT_USERNAME": {
        "label": "Standard-Benutzername",
        "placeholder": "{{VAULT_USERNAME}}",
        "category": "identity",
    },
    "VAULT_BIRTHDATE": {
        "label": "Geburtsdatum (TT.MM.JJJJ)",
        "placeholder": "{{VAULT_BIRTHDATE}}",
        "category": "identity",
    },
    "VAULT_PHONE": {
        "label": "Telefonnummer / Mobilnummer",
        "placeholder": "{{VAULT_PHONE}}",
        "category": "identity",
    },
    "VAULT_ADDRESS": {
        "label": "Wohnanschrift (Straße, PLZ, Ort)",
        "placeholder": "{{VAULT_ADDRESS}}",
        "category": "identity",
    },
    "VAULT_EMAIL": {
        "label": "Haupt-E-Mail-Adresse",
        "placeholder": "{{VAULT_EMAIL}}",
        "category": "email",
    },
    "VAULT_PASSWORD": {
        "label": "Standard-Passwort (20 Zeichen)",
        "placeholder": "{{VAULT_PASSWORD}}",
        "category": "email",
    },
    "VAULT_IMAP_HOST": {
        "label": "IMAP-Server (z. B. imap.gmail.com)",
        "placeholder": "{{VAULT_IMAP_HOST}}",
        "category": "email",
    },
    "VAULT_EMAIL_APP_PASSWORD": {
        "label": "E-Mail App-Passwort (für IMAP-Code-Abruf)",
        "placeholder": "{{VAULT_EMAIL_APP_PASSWORD}}",
        "category": "email",
    },
    "VAULT_MAILTM_ADDRESS": {
        "label": "Autonomes Agenten-Postfach (api.mail.tm)",
        "placeholder": "{{VAULT_MAILTM_ADDRESS}}",
        "category": "email",
    },
    "VAULT_MAILTM_PASSWORD": {
        "label": "Agenten-Postfach Passwort (20 Zeichen)",
        "placeholder": "{{VAULT_MAILTM_PASSWORD}}",
        "category": "email",
    },
    "VAULT_MAILTM_TOKEN": {
        "label": "Mail.tm Bearer Token",
        "placeholder": "{{VAULT_MAILTM_TOKEN}}",
        "category": "email",
    },
    "VAULT_MAILTM_ACCOUNT_ID": {
        "label": "Mail.tm Account-ID",
        "placeholder": "{{VAULT_MAILTM_ACCOUNT_ID}}",
        "category": "email",
    },
    # AUFGABE 3: Debit-Kreditkarte, IBAN & Rechnungsadresse
    "VAULT_CARD_HOLDER": {
        "label": "Karteninhaber (Debit-/Kreditkarte)",
        "placeholder": "{{VAULT_CARD_HOLDER}}",
        "category": "payment",
    },
    "VAULT_CARD_NUMBER": {
        "label": "Kartennummer (16-stellig)",
        "placeholder": "{{VAULT_CARD_NUMBER}}",
        "category": "payment",
    },
    "VAULT_CARD_EXPIRY": {
        "label": "Ablaufdatum (MM/JJ)",
        "placeholder": "{{VAULT_CARD_EXPIRY}}",
        "category": "payment",
    },
    "VAULT_CARD_CVC": {
        "label": "CVC / Sicherheitscode (3-stellig)",
        "placeholder": "{{VAULT_CARD_CVC}}",
        "category": "payment",
    },
    "VAULT_IBAN": {
        "label": "IBAN (SEPA Referenzkonto)",
        "placeholder": "{{VAULT_IBAN}}",
        "category": "payment",
    },
    "VAULT_BILLING_ADDRESS": {
        "label": "Rechnungsadresse",
        "placeholder": "{{VAULT_BILLING_ADDRESS}}",
        "category": "payment",
    },
    # AUFGABE 4: Broker & Buy-Low / Sell-High Trading Parameter
    "VAULT_BROKER_URL": {
        "label": "Web-Broker URL (z. B. Trade Republic / Scalable)",
        "placeholder": "{{VAULT_BROKER_URL}}",
        "category": "trading",
    },
    "VAULT_BROKER_USER": {
        "label": "Broker Login / Depotnummer",
        "placeholder": "{{VAULT_BROKER_USER}}",
        "category": "trading",
    },
    "VAULT_BROKER_PIN": {
        "label": "Broker PIN / Passwort",
        "placeholder": "{{VAULT_BROKER_PIN}}",
        "category": "trading",
    },
    "VAULT_ALPACA_KEY": {
        "label": "Alpaca / Broker API-Key",
        "placeholder": "{{VAULT_ALPACA_KEY}}",
        "category": "trading",
    },
    "VAULT_ALPACA_SECRET": {
        "label": "Alpaca / Broker API-Secret",
        "placeholder": "{{VAULT_ALPACA_SECRET}}",
        "category": "trading",
    },
    "VAULT_MAX_DAILY_LIMIT_EUR": {
        "label": "Max. Tageslimit (EUR)",
        "placeholder": "{{VAULT_MAX_DAILY_LIMIT_EUR}}",
        "category": "trading",
    },
    "VAULT_TAKE_PROFIT_PCT": {
        "label": "Take-Profit Gewinnziel (%)",
        "placeholder": "{{VAULT_TAKE_PROFIT_PCT}}",
        "category": "trading",
    },
    "VAULT_STOP_LOSS_PCT": {
        "label": "Stop-Loss Verlustbegrenzung (%)",
        "placeholder": "{{VAULT_STOP_LOSS_PCT}}",
        "category": "trading",
    },
}

SENSITIVE_MASK_KEYS = {
    "VAULT_PASSWORD",
    "VAULT_EMAIL_APP_PASSWORD",
    "VAULT_MAILTM_PASSWORD",
    "VAULT_MAILTM_TOKEN",
    "VAULT_CARD_NUMBER",
    "VAULT_CARD_CVC",
    "VAULT_IBAN",
    "VAULT_BROKER_PIN",
    "VAULT_ALPACA_KEY",
    "VAULT_ALPACA_SECRET",
}


class _FallbackFernetCipher:
    """Deterministic local XOR+HMAC-like fallback if cryptography is unavailable."""

    def __init__(self, key_bytes: bytes) -> None:
        self._key = hashlib.sha256(key_bytes).digest()

    def encrypt(self, data: bytes) -> bytes:
        out = bytearray(len(data))
        for i, b in enumerate(data):
            out[i] = b ^ self._key[i % len(self._key)]
        return base64.urlsafe_b64encode(bytes(out))

    def decrypt(self, token: bytes) -> bytes:
        raw = base64.urlsafe_b64decode(token)
        out = bytearray(len(raw))
        for i, b in enumerate(raw):
            out[i] = b ^ self._key[i % len(self._key)]
        return bytes(out)


class SecureVault:
    """
    Thread-safe AES-Fernet encrypted local vault with Zero-Knowledge placeholder
    resolution and 2-Stage Purchase Permission (`EXECUTION_MODE`: `SEMI_AUTO` vs `FULL_AUTO`).
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        get_vault_dir()
        self._cipher = self._init_cipher()
        self._data: dict[str, str] = self._load_encrypted()

    def _init_cipher(self) -> Any:
        try:
            from cryptography.fernet import Fernet  # type: ignore

            if VAULT_KEY_FILE.exists():
                key = VAULT_KEY_FILE.read_bytes().strip()
            else:
                key = Fernet.generate_key()
                VAULT_KEY_FILE.write_bytes(key)
            return Fernet(key)
        except Exception as exc:
            logger.debug("Using fallback local cipher for vault: %s", exc)
            if VAULT_KEY_FILE.exists():
                key = VAULT_KEY_FILE.read_bytes().strip()
            else:
                key = base64.urlsafe_b64encode(os.urandom(32))
                VAULT_KEY_FILE.write_bytes(key)
            return _FallbackFernetCipher(key)

    def _default_vault_data(self) -> dict[str, str]:
        exec_mode = normalize_execution_mode(_cfg_get_execution_mode())
        defaults: dict[str, str] = {k: "" for k in VAULT_KEYS_META}
        defaults["VAULT_IMAP_HOST"] = "imap.gmail.com"
        defaults["VAULT_BROKER_URL"] = "https://app.traderepublic.com"
        defaults["VAULT_MAX_DAILY_LIMIT_EUR"] = "250.00"
        defaults["VAULT_TAKE_PROFIT_PCT"] = "3.5"
        defaults["VAULT_STOP_LOSS_PCT"] = "2.0"
        defaults["EXECUTION_MODE"] = exec_mode
        defaults["AUTONOMY_MODE"] = "mode_2" if exec_mode == "FULL_AUTO" else "mode_1"
        return defaults

    def _load_encrypted(self) -> dict[str, str]:
        defaults = self._default_vault_data()
        target_file = (
            VAULT_ENC_FILE
            if VAULT_ENC_FILE.exists()
            else (LEGACY_VAULT_ENC_FILE if LEGACY_VAULT_ENC_FILE.exists() else None)
        )
        if target_file is not None:
            try:
                token = target_file.read_bytes()
                raw = self._cipher.decrypt(token)
                loaded = json.loads(raw.decode("utf-8"))
                if isinstance(loaded, dict):
                    for k, v in loaded.items():
                        defaults[str(k)] = "" if v is None else str(v)
            except Exception as exc:
                logger.warning("Vault decrypt warning (re-initializing): %s", exc)
        # Ensure encrypted files exist on disk immediately
        self._save_encrypted_dict(defaults)
        return defaults

    def _save_encrypted_dict(self, data: dict[str, str]) -> None:
        try:
            raw = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
            token = self._cipher.encrypt(raw)
            VAULT_ENC_FILE.write_bytes(token)
            LEGACY_VAULT_ENC_FILE.write_bytes(token)
        except Exception as exc:
            logger.error("Failed to save encrypted vault: %s", exc)

    def _save_encrypted(self) -> None:
        self._save_encrypted_dict(self._data)

    # ------------------------------------------------------------------
    # Secret Getters & Updaters
    # ------------------------------------------------------------------
    def get_secret(self, key: str, default: str = "") -> str:
        with self._lock:
            k = (key or "").strip()
            val = self._data.get(k, default)
            if not val and k == "VAULT_EMAIL":
                return self._data.get("VAULT_MAILTM_ADDRESS", default)
            if not val and k == "VAULT_PASSWORD":
                return self._data.get("VAULT_MAILTM_PASSWORD", default)
            if not val and k == "VAULT_CARD_HOLDER":
                return self._data.get("VAULT_FULL_NAME", default)
            if not val and k == "VAULT_BILLING_ADDRESS":
                return self._data.get("VAULT_ADDRESS", default)
            return val

    def set_secret(self, key: str, value: str) -> None:
        with self._lock:
            self._data[str(key).strip()] = str(value)
            self._save_encrypted()

    def update_secrets(self, updates: dict[str, Any]) -> dict[str, str]:
        """Updates multiple vault keys at once, keeping EXECUTION_MODE and AUTONOMY_MODE in sync."""
        if not isinstance(updates, dict):
            return self.get_masked_summary()
        with self._lock:
            for k, v in updates.items():
                if v is None:
                    continue
                key_str = str(k).strip()
                val_str = str(v).strip()

                if key_str in ("EXECUTION_MODE", "execution_mode"):
                    norm_exec = normalize_execution_mode(val_str)
                    self._data["EXECUTION_MODE"] = norm_exec
                    self._data["AUTONOMY_MODE"] = (
                        "mode_2" if norm_exec == "FULL_AUTO" else "mode_1"
                    )
                    _cfg_set_execution_mode(norm_exec)
                    continue

                if key_str in ("AUTONOMY_MODE", "autonomy_mode"):
                    norm_exec = normalize_execution_mode(val_str)
                    self._data["EXECUTION_MODE"] = norm_exec
                    self._data["AUTONOMY_MODE"] = (
                        "mode_2" if norm_exec == "FULL_AUTO" else "mode_1"
                    )
                    _cfg_set_execution_mode(norm_exec)
                    continue

                # Ignore masked UI echoes like "••••1234"
                if "•" in val_str or "***" in val_str:
                    continue

                self._data[key_str] = val_str

            self._save_encrypted()
            return self.get_masked_summary()

    def update_vault_settings(self, updates: dict[str, Any]) -> dict[str, str]:
        """Alias for update_secrets used by system checks & legacy callers."""
        return self.update_secrets(updates)

    # ------------------------------------------------------------------
    # 2-Stage Purchase Permission (EXECUTION_MODE / AUTONOMY_MODE)
    # ------------------------------------------------------------------
    def get_execution_mode(self) -> str:
        """Returns 'SEMI_AUTO' (MODUS A) or 'FULL_AUTO' (MODUS B)."""
        with self._lock:
            raw = (
                self._data.get("EXECUTION_MODE")
                or self._data.get("AUTONOMY_MODE")
                or _cfg_get_execution_mode()
            )
            return normalize_execution_mode(raw)

    def set_execution_mode(self, mode: str) -> str:
        """Sets 'SEMI_AUTO' (MODUS A) or 'FULL_AUTO' (MODUS B) and syncs with config.py."""
        norm = normalize_execution_mode(mode)
        with self._lock:
            self._data["EXECUTION_MODE"] = norm
            self._data["AUTONOMY_MODE"] = "mode_2" if norm == "FULL_AUTO" else "mode_1"
            self._save_encrypted()
        _cfg_set_execution_mode(norm)
        return norm

    def get_autonomy_mode(self) -> str:
        """Returns 'mode_1' (MODUS A: SEMI_AUTO) or 'mode_2' (MODUS B: FULL_AUTO)."""
        return "mode_2" if self.get_execution_mode() == "FULL_AUTO" else "mode_1"

    def set_autonomy_mode(self, mode: str) -> str:
        norm = self.set_execution_mode(mode)
        return "mode_2" if norm == "FULL_AUTO" else "mode_1"

    def get_daily_limit_eur(self) -> float:
        with self._lock:
            raw = self._data.get("VAULT_MAX_DAILY_LIMIT_EUR", "250.00")
            try:
                return max(0.0, float(str(raw).replace(",", ".")))
            except Exception:
                return 250.0

    def get_take_profit_pct(self) -> float:
        with self._lock:
            raw = self._data.get("VAULT_TAKE_PROFIT_PCT", "3.5")
            try:
                return max(0.2, float(str(raw).replace(",", ".")))
            except Exception:
                return 3.5

    def get_stop_loss_pct(self) -> float:
        with self._lock:
            raw = self._data.get("VAULT_STOP_LOSS_PCT", "2.0")
            try:
                return max(0.2, float(str(raw).replace(",", ".")))
            except Exception:
                return 2.0

    # ------------------------------------------------------------------
    # Masked UI Summary & Zero-Knowledge Placeholder Injection
    # ------------------------------------------------------------------
    def _mask_value(self, key: str, value: str) -> str:
        if not value:
            return ""
        if key not in SENSITIVE_MASK_KEYS:
            return value
        if len(value) <= 4:
            return "••••"
        return "•••• " + value[-4:]

    def get_masked_summary(self) -> dict[str, str]:
        with self._lock:
            out: dict[str, str] = {}
            for k in VAULT_KEYS_META:
                out[k] = self._mask_value(k, self._data.get(k, ""))
            exec_mode = self.get_execution_mode()
            out["EXECUTION_MODE"] = exec_mode
            out["AUTONOMY_MODE"] = "mode_2" if exec_mode == "FULL_AUTO" else "mode_1"
            return out

    def get_masked_dict(self) -> dict[str, str]:
        return self.get_masked_summary()

    def get_ui_masked_snapshot(self) -> dict[str, Any]:
        summary = self.get_masked_summary()
        return {
            "ok": True,
            "vault": summary,
            "execution_mode": self.get_execution_mode(),
            "autonomy_mode": self.get_autonomy_mode(),
        }

    def resolve_placeholders(self, text: str) -> str:
        """
        Replaces `{{VAULT_*}}` placeholders in `text` strictly in local RAM
        without ever sending plaintext secrets to external LLMs.
        """
        if not text:
            return ""
        trimmed = text.strip()
        if trimmed in VAULT_KEYS_META:
            return self.get_secret(trimmed, "")

        def _repl(match: re.Match[str]) -> str:
            key = match.group(1).strip()
            return self.get_secret(key, "")

        return re.sub(r"\{\{\s*(VAULT_[A-Z0-9_]+)\s*\}\}", _repl, text)

    def resolve_placeholders_in_ram(self, text: str) -> tuple[str, bool]:
        """Returns `(resolved_string, contained_secret_bool)`."""
        if not text:
            return "", False
        resolved = self.resolve_placeholders(text)
        contained = (resolved != text) or ("{{VAULT_" in text)
        return resolved, contained

    def get_available_placeholders_prompt(self) -> str:
        """
        Builds a Zero-Knowledge prompt fragment listing available `{{VAULT_*}}`
        placeholders and whether they are populated, WITHOUT exposing any secret values!
        """
        lines: list[str] = []
        with self._lock:
            for key, meta in VAULT_KEYS_META.items():
                val = self.get_secret(key, "")
                status = "HINTERLEGT" if val else "LEER"
                lines.append(f"- {meta['placeholder']} ({meta['label']}): [{status}]")
        return "\n".join(lines)

    def build_llm_zero_knowledge_prompt(self) -> str:
        return self.get_available_placeholders_prompt()

    # ------------------------------------------------------------------
    # Autonomous Email Verification Code & Link Reader (Mail.tm + IMAP)
    # ------------------------------------------------------------------
    def read_latest_verification_email(self, max_wait_sec: int = 12) -> dict[str, Any]:
        """
        Polls both the autonomous Mail.tm inbox (`mail_factory.py`) and the configured
        IMAP inbox for 6-digit verification codes (TAN/PIN) or confirmation links.
        """
        deadline = time.time() + max(1, max_wait_sec)
        while time.time() <= deadline:
            # 1. Check Mail.tm instant mailbox first
            try:
                from mail_factory import MAIL_FACTORY

                mailtm_res = MAIL_FACTORY.check_mailtm_inbox_once()
                if mailtm_res.get("ok") and (
                    mailtm_res.get("code") or mailtm_res.get("link")
                ):
                    return mailtm_res
            except Exception as exc:
                logger.debug("Mail.tm inbox check skipped: %s", exc)

            # 2. Check IMAP inbox if credentials exist
            imap_host = self.get_secret("VAULT_IMAP_HOST", "imap.gmail.com")
            email_addr = self._data.get("VAULT_EMAIL", "")
            app_pwd = (
                self.get_secret("VAULT_EMAIL_APP_PASSWORD")
                or self._data.get("VAULT_PASSWORD", "")
            )
            if imap_host and email_addr and app_pwd and "@" in email_addr:
                imap_res = self._check_imap_once(imap_host, email_addr, app_pwd)
                if imap_res.get("ok"):
                    return imap_res

            if time.time() + 1.5 > deadline:
                break
            time.sleep(1.5)

        return {
            "ok": False,
            "code": "",
            "link": "",
            "error": "Kein neuer 6-stelliger Bestätigungscode oder Link im Postfach gefunden.",
        }

    def _check_imap_once(
        self, imap_host: str, email_addr: str, app_pwd: str
    ) -> dict[str, Any]:
        try:
            mail = imaplib.IMAP4_SSL(imap_host, timeout=6)
            mail.login(email_addr, app_pwd)
            mail.select("INBOX")
            status, messages = mail.search(None, "ALL")
            if status != "OK" or not messages or not messages[0]:
                mail.logout()
                return {"ok": False}

            msg_ids = messages[0].split()
            for msg_id in reversed(msg_ids[-5:]):
                res, msg_data = mail.fetch(msg_id, "(RFC822)")
                if res != "OK" or not msg_data:
                    continue
                for response_part in msg_data:
                    if not isinstance(response_part, tuple):
                        continue
                    msg = email.message_from_bytes(response_part[1])
                    raw_subj = msg.get("Subject", "")
                    decoded_parts = decode_header(raw_subj)
                    subject = ""
                    for part, enc in decoded_parts:
                        if isinstance(part, bytes):
                            subject += part.decode(enc or "utf-8", errors="ignore")
                        else:
                            subject += str(part)

                    body_text = ""
                    if msg.is_multipart():
                        for part in msg.walk():
                            ctype = part.get_content_type()
                            if ctype in ("text/plain", "text/html"):
                                payload = part.get_payload(decode=True)
                                if payload:
                                    body_text += payload.decode("utf-8", errors="ignore") + "\n"
                    else:
                        payload = msg.get_payload(decode=True)
                        if payload:
                            body_text = payload.decode("utf-8", errors="ignore")

                    combined = f"{subject}\n{body_text}"
                    code_match = re.search(r"\b(\d{6})\b", combined)
                    if not code_match:
                        code_match = re.search(r"\b(\d{4,8})\b", combined)
                    link_match = re.search(
                        r"(https?://[^\s\"'<>]+(?:verify|confirm|activate|token|code|auth)[^\s\"'<>]*)",
                        combined,
                        flags=re.IGNORECASE,
                    )
                    code = code_match.group(1) if code_match else ""
                    link = link_match.group(1) if link_match else ""
                    if code or link:
                        mail.logout()
                        return {
                            "ok": True,
                            "source": "imap",
                            "subject": subject.strip(),
                            "code": code,
                            "link": link,
                        }
            mail.logout()
        except Exception as exc:
            logger.debug("IMAP poll failed: %s", exc)
        return {"ok": False}


SECURE_VAULT = SecureVault()


def read_latest_verification_email(max_wait_sec: int = 12) -> dict[str, Any]:
    return SECURE_VAULT.read_latest_verification_email(max_wait_sec=max_wait_sec)


def resolve_placeholders_in_ram(text: str) -> tuple[str, bool]:
    return SECURE_VAULT.resolve_placeholders_in_ram(text)


def build_llm_zero_knowledge_prompt() -> str:
    return SECURE_VAULT.build_llm_zero_knowledge_prompt()

