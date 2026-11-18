"""Smart-Home-Hook für Shelly, Tasmota, Home Assistant Webhooks sowie Raspberry Pi 5 RP1 GPIO/UART."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional
import urllib.error
import urllib.request

from hardware_pi5 import send_uart_command, set_gpio_pin_rp1


class SmartHomeController:
    """Steuert Smart-Home-Geräte aus devices.json (HTTP, Pi 5 RP1-GPIO oder UART/Serial)."""

    def __init__(self, devices_file: Optional[str | Path] = None) -> None:
        configured = devices_file or os.getenv("SMART_HOME_DEVICES_FILE", "devices.json")
        self.devices_path = Path(configured)
        self.timeout_seconds = 5.0

    def _load_devices(self) -> list[dict[str, Any]]:
        if self.devices_path.exists():
            try:
                raw = json.loads(self.devices_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and isinstance(raw.get("devices"), list):
                    return raw["devices"]
                if isinstance(raw, list):
                    return raw
            except Exception:
                return []

        env_json = os.getenv("SMART_HOME_DEVICES_JSON", "").strip()
        if env_json:
            try:
                parsed = json.loads(env_json)
                if isinstance(parsed, list):
                    return parsed
            except Exception:
                pass

        return []

    def _find_device(self, query: str) -> Optional[dict[str, Any]]:
        devices = self._load_devices()
        if not devices:
            return None

        q = query.lower()
        for dev in devices:
            names = [str(dev.get("name", ""))] + [str(a) for a in dev.get("aliases", [])]
            for name in names:
                if name and name.lower() in q:
                    return dev
        return None

    def list_device_names(self) -> list[str]:
        return [str(d.get("name", "")) for d in self._load_devices() if d.get("name")]

    def _control_gpio_device(self, device: dict[str, Any], name: str, turn_on: bool) -> str:
        raw_pin = device.get("gpio_pin", device.get("pin"))
        if raw_pin is None:
            return f"Chef, für das GPIO-Gerät {name} fehlt das Feld 'gpio_pin'."
        active_low = bool(device.get("active_low", False))
        level = (not turn_on) if active_low else turn_on
        chip = str(device.get("gpio_chip", "")).strip() or None
        ok, detail = set_gpio_pin_rp1(int(raw_pin), level, chip_path=chip)
        if ok:
            state_word = "eingeschaltet" if turn_on else "ausgeschaltet"
            return f"Sehr wohl, Chef. {name} wurde per RP1-GPIO {state_word}."
        return f"Chef, {name} konnte nicht über GPIO geschaltet werden: {detail}"

    def _control_uart_device(self, device: dict[str, Any], name: str, turn_on: bool) -> str:
        port = str(device.get("port", "/dev/ttyAMA0")).strip()
        baudrate = int(device.get("baudrate", 115200))
        cmd = str(device.get("on_command" if turn_on else "off_command", "ON" if turn_on else "OFF"))
        ok, detail = send_uart_command(port=port, command=cmd, baudrate=baudrate)
        if ok:
            state_word = "eingeschaltet" if turn_on else "ausgeschaltet"
            return f"Sehr wohl, Chef. {name} wurde über {port} {state_word}."
        return f"Chef, UART-Steuerung für {name} fehlgeschlagen: {detail}"

    def control_device(self, query: str, turn_on: bool) -> str:
        """Schaltet ein passendes Smart-Home-Gerät per HTTP, RP1-GPIO oder UART ein/aus."""
        devices = self._load_devices()
        if not devices:
            return (
                "Chef, es sind noch keine Smart-Home-Geräte eingerichtet. "
                "Kopieren Sie bitte devices.example.json nach devices.json und tragen Sie Ihre Geräte ein."
            )

        device = self._find_device(query)
        if device is None:
            available = ", ".join(self.list_device_names()) or "keine"
            return f"Chef, ich habe kein passendes Gerät gefunden. Verfügbar sind: {available}."

        name = str(device.get("name", "Gerät"))
        dev_type = str(device.get("type", "http")).strip().lower()

        if dev_type == "gpio" or "gpio_pin" in device:
            return self._control_gpio_device(device, name, turn_on)

        if dev_type in ("uart", "serial"):
            return self._control_uart_device(device, name, turn_on)

        url = str(device.get("on_url" if turn_on else "off_url", "")).strip()
        if not url:
            return f"Chef, für {name} ist keine URL zum {'Einschalten' if turn_on else 'Ausschalten'} hinterlegt."

        method = str(device.get("method", "GET")).strip().upper()
        payload = device.get("on_payload" if turn_on else "off_payload")
        headers = {"User-Agent": "Jarvis-SmartHome/1.0"}
        if isinstance(device.get("headers"), dict):
            headers.update({str(k): str(v) for k, v in device["headers"].items()})

        body_bytes: Optional[bytes] = None
        if payload is not None:
            if isinstance(payload, (dict, list)):
                body_bytes = json.dumps(payload).encode("utf-8")
                headers.setdefault("Content-Type", "application/json")
            else:
                body_bytes = str(payload).encode("utf-8")
            if method == "GET":
                method = "POST"

        req = urllib.request.Request(url=url, data=body_bytes, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                if 200 <= resp.status < 300:
                    state_word = "eingeschaltet" if turn_on else "ausgeschaltet"
                    return f"Sehr wohl, Chef. {name} wurde {state_word}."
                return f"Chef, {name} hat mit Statuscode {resp.status} geantwortet."
        except urllib.error.URLError as exc:
            return f"Chef, {name} ist im Netzwerk leider nicht erreichbar: {exc.reason}."
        except Exception as exc:
            return f"Chef, beim Schalten von {name} ist ein Fehler aufgetreten: {exc}."
