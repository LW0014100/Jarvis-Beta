"""Raspberry Pi 5 (ARM64 / Bookworm / RP1-Southbridge) Hardware- & Diagnose-Abstraktion.

Ersetzt veraltetes RPi.GPIO vollständig durch Linux gpiod / lgpio / gpiozero (RP1-kompatibel)
und stellt Diagnose- sowie Steuerungsfunktionen für GPIO, I2C, SPI, UART/Serial, Lüfter und
Throttling-Status bereit. Auf Windows oder Nicht-Pi-Systemen fallen alle Aufrufe sicher zurück.
"""

from __future__ import annotations

import os
from pathlib import Path
import platform
import shutil
import subprocess
from typing import Any, Optional


_MODEL_PATHS = (
    Path("/proc/device-tree/model"),
    Path("/sys/firmware/devicetree/base/model"),
)


def read_pi_model() -> str:
    """Liest das Board-Modell aus dem Device-Tree aus (leer auf Windows/x86)."""
    for path in _MODEL_PATHS:
        try:
            if path.exists():
                return path.read_text(encoding="utf-8", errors="ignore").replace("\x00", "").strip()
        except Exception:
            continue
    return ""


def is_raspberry_pi_5() -> bool:
    """Prüft, ob das System auf einem Raspberry Pi 5 (RP1-Architektur) läuft."""
    return "raspberry pi 5" in read_pi_model().lower()


def is_arm64() -> bool:
    """Prüft auf 64-Bit-ARM-Architektur (aarch64 / arm64)."""
    return platform.machine().lower() in ("aarch64", "arm64")


def get_os_codename() -> str:
    """Liest den Debian/OS-Codename aus /etc/os-release (z. B. 'bookworm')."""
    os_release = Path("/etc/os-release")
    if not os_release.exists():
        return platform.system().lower()
    try:
        for line in os_release.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("VERSION_CODENAME="):
                return line.split("=", 1)[1].strip().strip('"').lower()
    except Exception:
        pass
    return "linux"


def get_session_type() -> str:
    """Erkennt Wayland, X11 oder Windows."""
    if platform.system() == "Windows":
        return "windows"
    if os.getenv("WAYLAND_DISPLAY"):
        return "wayland"
    xdg_type = os.getenv("XDG_SESSION_TYPE", "").strip().lower()
    if xdg_type:
        return xdg_type
    if os.getenv("DISPLAY"):
        return "x11"
    return "headless"


def find_rp1_gpio_chip() -> Optional[str]:
    """Ermittelt das RP1-GPIO-Character-Device (/dev/gpiochip4 bzw. /dev/gpiochip0 je Kernel).

    Raspberry Pi OS Bookworm weist dem RP1-Controller unter Kernel 6.6 teilweise
    /dev/gpiochip4 und ab neueren 6.6.45+/6.12-Kerneln /dev/gpiochip0 zu.
    Daher wird gezielt nach dem Device mit Label 'pinctrl-rp1' gesucht.
    """
    if platform.system() != "Linux":
        return None

    sys_gpio = Path("/sys/bus/gpio/devices")
    if sys_gpio.exists():
        for dev in sorted(sys_gpio.iterdir()):
            label_file = dev / "label"
            try:
                label = label_file.read_text(encoding="utf-8", errors="ignore").strip().lower()
            except Exception:
                continue
            if "pinctrl-rp1" in label or label == "rp1":
                candidate = Path("/dev") / dev.name
                if candidate.exists():
                    return str(candidate)

    for fallback in ("/dev/gpiochip4", "/dev/gpiochip0"):
        if Path(fallback).exists():
            return fallback
    return None


def find_rp1_gpio_chip_index() -> int:
    """Gibt die numerische Chip-ID für lgpio zurück (Standard 4 oder 0 auf Pi 5)."""
    chip_path = find_rp1_gpio_chip()
    if chip_path:
        digits = "".join(ch for ch in Path(chip_path).name if ch.isdigit())
        if digits:
            return int(digits)
    return 4 if is_raspberry_pi_5() else 0


def ensure_gpiozero_lgpio_factory() -> Optional[object]:
    """Erzwingt für gpiozero die RP1-kompatible LGPIOFactory statt veraltetem RPi.GPIO."""
    if platform.system() != "Linux":
        return None
    os.environ.setdefault("GPIOZERO_PIN_FACTORY", "lgpio")
    try:
        from gpiozero.pins.lgpio import LGPIOFactory  # type: ignore[import-not-found]

        return LGPIOFactory()
    except Exception:
        return None


def list_rp1_interfaces() -> dict[str, Any]:
    """Listet aktive GPIO-, I2C-, SPI- und UART/Serial-Schnittstellen auf dem Pi 5."""
    if platform.system() != "Linux":
        return {
            "is_pi5": False,
            "is_arm64": is_arm64(),
            "model": platform.platform(),
            "os_codename": get_os_codename(),
            "session_type": get_session_type(),
            "gpio_chip": None,
            "i2c_buses": [],
            "spi_devices": [],
            "uart_devices": [],
            "fan_rpm": None,
        }

    dev_dir = Path("/dev")
    i2c_buses = sorted(str(p) for p in dev_dir.glob("i2c-*"))
    spi_devices = sorted(str(p) for p in dev_dir.glob("spidev*"))
    uart_candidates: list[str] = []
    for pattern in ("ttyAMA0", "ttyAMA10", "serial0", "serial1", "ttyUSB*", "ttyACM*"):
        uart_candidates.extend(sorted(str(p) for p in dev_dir.glob(pattern)))

    return {
        "is_pi5": is_raspberry_pi_5(),
        "is_arm64": is_arm64(),
        "model": read_pi_model() or platform.machine(),
        "os_codename": get_os_codename(),
        "session_type": get_session_type(),
        "gpio_chip": find_rp1_gpio_chip(),
        "i2c_buses": i2c_buses,
        "spi_devices": spi_devices,
        "uart_devices": uart_candidates,
        "fan_rpm": get_pi5_fan_rpm(),
    }


def get_pi5_fan_rpm() -> Optional[int]:
    """Liest die Drehzahl (RPM) des Raspberry Pi 5 Active Coolers über sysfs aus."""
    if platform.system() != "Linux":
        return None
    hwmon_root = Path("/sys/devices/platform/cooling_fan/hwmon")
    if not hwmon_root.exists():
        return None
    for fan_input in hwmon_root.glob("hwmon*/fan1_input"):
        try:
            return int(fan_input.read_text(encoding="utf-8").strip())
        except Exception:
            continue
    return None


def get_pi5_throttle_warning() -> Optional[str]:
    """Prüft auf Unterspannung oder thermische Drosselung über vcgencmd (falls vorhanden)."""
    if platform.system() != "Linux":
        return None
    vcgencmd = shutil.which("vcgencmd")
    if not vcgencmd:
        return None
    try:
        res = subprocess.run([vcgencmd, "get_throttled"], capture_output=True, text=True, timeout=1.5, check=False)
        out = (res.stdout or "").strip()
        if "=" not in out:
            return None
        val = int(out.split("=", 1)[1].strip(), 16)
        if val & 0x1:
            return "Aktuelle Unterspannung erkannt (bitte offizielles 27W USB-C PD Netzteil prüfen)."
        if val & 0x4:
            return "Aktuelle thermische CPU-Drosselung auf dem Raspberry Pi 5 aktiv."
    except Exception:
        return None
    return None


def set_gpio_pin_rp1(pin: int, state: bool, chip_path: Optional[str] = None) -> tuple[bool, str]:
    """Schaltet einen BCM-GPIO-Pin auf dem Raspberry Pi 5 RP1 sauber per gpiod oder lgpio."""
    if platform.system() != "Linux":
        return False, "GPIO-Steuerung ist nur auf Linux / Raspberry Pi 5 verfügbar."

    target_chip = chip_path or find_rp1_gpio_chip() or "/dev/gpiochip4"

    # 1. Primär: libgpiod v2 (gpiod >= 2.0, Standard auf Pi 5 / Debian Bookworm)
    try:
        import gpiod  # type: ignore[import-not-found]

        if hasattr(gpiod, "request_lines") and hasattr(gpiod, "LineSettings"):
            from gpiod.line import Direction, Value  # type: ignore[import-not-found]

            target_val = Value.ACTIVE if state else Value.INACTIVE
            with gpiod.request_lines(
                target_chip,
                consumer="jarvis-rpi5",
                config={
                    int(pin): gpiod.LineSettings(
                        direction=Direction.OUTPUT,
                        output_value=target_val,
                    )
                },
            ) as req:
                req.set_value(int(pin), target_val)
            return True, f"GPIO {pin} über gpiod ({target_chip}) auf {'HIGH' if state else 'LOW'} gesetzt."
    except Exception:
        pass

    # 2. Sekundär: lgpio direkt auf RP1-Chip
    try:
        import lgpio  # type: ignore[import-not-found]

        chip_idx = find_rp1_gpio_chip_index()
        handle = lgpio.gpiochip_open(chip_idx)
        try:
            lgpio.gpio_claim_output(handle, int(pin), 1 if state else 0)
            lgpio.gpio_write(handle, int(pin), 1 if state else 0)
        finally:
            lgpio.gpiochip_close(handle)
        return True, f"GPIO {pin} über lgpio (gpiochip{chip_idx}) auf {'HIGH' if state else 'LOW'} gesetzt."
    except Exception as exc:
        return False, f"RP1-GPIO-Fehler an Pin {pin}: {exc}"


def send_uart_command(
    port: str,
    command: str,
    baudrate: int = 115200,
    timeout: float = 1.5,
) -> tuple[bool, str]:
    """Sendet einen Steuerbefehl über UART/USB-Serial (z. B. /dev/ttyAMA0 oder /dev/ttyUSB0)."""
    try:
        import serial  # type: ignore[import-not-found]
    except ImportError:
        return False, "pyserial ist nicht installiert."

    target_port = port.strip() or ("/dev/ttyAMA0" if is_raspberry_pi_5() else "/dev/ttyUSB0")
    payload = (command.rstrip("\r\n") + "\n").encode("utf-8")
    try:
        with serial.Serial(target_port, baudrate=int(baudrate), timeout=timeout) as ser:
            ser.write(payload)
            ser.flush()
        return True, f"UART-Befehl an {target_port} ({baudrate} Baud) gesendet."
    except Exception as exc:
        return False, f"UART-Fehler auf {target_port}: {exc}"
