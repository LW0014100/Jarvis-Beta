"""Intelligentes Wecker-System mit numpy-Sound-Synthese und Trolling-Modus."""

from __future__ import annotations

from datetime import date, datetime, timedelta
import json
import os
from pathlib import Path
import platform
import random
import re
import threading
import time
from typing import Any, Callable, Optional
import uuid

import numpy as np
import schedule
import sounddevice as sd

if platform.system() == "Linux":
    os.environ.setdefault("GPIOZERO_PIN_FACTORY", "lgpio")

try:
    from gpiozero import Button as GpioButton
except ImportError:  # Windows 11 / Systeme ohne gpiozero
    GpioButton = None  # type: ignore[assignment,misc]

from hardware_pi5 import ensure_gpiozero_lgpio_factory, find_rp1_gpio_chip


WEEKDAY_MAP = {
    "montag": "monday",
    "dienstag": "tuesday",
    "mittwoch": "wednesday",
    "donnerstag": "thursday",
    "freitag": "friday",
    "samstag": "saturday",
    "sonnabend": "saturday",
    "sonntag": "sunday",
}

WEEKDAY_NAMES_DE = {
    "monday": "Montag",
    "tuesday": "Dienstag",
    "wednesday": "Mittwoch",
    "thursday": "Donnerstag",
    "friday": "Freitag",
    "saturday": "Samstag",
    "sunday": "Sonntag",
}

ALL_DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
WORKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday"]
WEEKEND = ["saturday", "sunday"]

STOP_ALARM_PATTERN = re.compile(
    r"\b(?:ich\s+bin\s+wach|stopp|stop|wecker\s+aus|aufhören|ausmachen|ruhe|genug)\b",
    flags=re.IGNORECASE,
)


def generate_standard_scifi_pulse(sample_rate: int = 44100, duration: float = 1.8) -> np.ndarray:
    """Erzeugt einen kraftvollen, ansteigenden Sci-Fi-Weckton mit Bass-Puls."""
    t = np.linspace(0.0, duration, int(sample_rate * duration), endpoint=False, dtype=np.float32)
    rise = np.linspace(0.25, 1.0, t.size, dtype=np.float32)

    # Tiefer Bass-Puls (55 Hz + 110 Hz)
    bass_mod = 0.5 + 0.5 * np.sin(2.0 * np.pi * 3.0 * t)
    bass = (0.55 * np.sin(2.0 * np.pi * 55.0 * t) + 0.35 * np.sin(2.0 * np.pi * 110.0 * t)) * bass_mod

    # Ansteigender Sci-Fi-Akkord (440 -> 554.37 -> 659.25 -> 880 Hz)
    step = (t / (duration / 4.0)).astype(np.int32)
    freqs = np.select(
        [step == 0, step == 1, step == 2],
        [440.0, 554.37, 659.25],
        default=880.0,
    )
    phase = 2.0 * np.pi * np.cumsum(freqs) / sample_rate
    chime = 0.45 * np.sin(phase)

    signal = (bass + chime) * rise
    fade = int(sample_rate * 0.03)
    signal[:fade] *= np.linspace(0.0, 1.0, fade, dtype=np.float32)
    signal[-fade:] *= np.linspace(1.0, 0.0, fade, dtype=np.float32)

    peak = float(np.max(np.abs(signal))) or 1.0
    return (signal / peak * 0.72).astype(np.float32)


def generate_trolling_beep(sample_rate: int = 44100, duration: float = 1.8) -> np.ndarray:
    """Erzeugt einen extrem schrillen Hochfrequenz-Pieps-Alarm (3200-3800 Hz Stakkato)."""
    t = np.linspace(0.0, duration, int(sample_rate * duration), endpoint=False, dtype=np.float32)

    # Schnelles 10-Hz-Stakkato (60 ms an, 40 ms aus), wechselnde Frequenzen 3350 / 3750 Hz
    cycle_pos = (t * 10.0) % 1.0
    gate = np.where(cycle_pos < 0.6, 1.0, 0.0).astype(np.float32)
    freq = np.where(((t * 5.0).astype(np.int32) % 2) == 0, 3350.0, 3750.0)

    phase = 2.0 * np.pi * np.cumsum(freq) / sample_rate
    signal = np.sin(phase) * gate
    return (signal * 0.72).astype(np.float32)


def _extract_time_str(text: str) -> Optional[str]:
    """Parst Zeitangaben wie '6:30', '7 Uhr', '07 Uhr 15' nach 'HH:MM'."""
    m = re.search(r"\b([01]?\d|2[0-3])\s*(?::|\.|uhr\s*)\s*([0-5]\d)\b", text, flags=re.IGNORECASE)
    if m:
        return f"{int(m.group(1)):02d}:{int(m.group(2)):02d}"

    m_hour = re.search(r"\b([01]?\d|2[0-3])\s*uhr\b", text, flags=re.IGNORECASE)
    if m_hour:
        return f"{int(m_hour.group(1)):02d}:00"
    return None


def _extract_days_and_date(text: str, time_str: str) -> tuple[list[str], Optional[str]]:
    low = text.lower()
    if any(w in low for w in ("jeden tag", "täglich", "immer")):
        return list(ALL_DAYS), None
    if any(w in low for w in ("werktags", "wochentags", "unter der woche")):
        return list(WORKDAYS), None
    if "wochenende" in low:
        return list(WEEKEND), None

    found_days = [eng for de, eng in WEEKDAY_MAP.items() if re.search(rf"\b{de}\b", low)]
    if found_days:
        return list(dict.fromkeys(found_days)), None

    now = datetime.now()
    hh, mm = map(int, time_str.split(":"))
    if "übermorgen" in low:
        return [], (now.date() + timedelta(days=2)).isoformat()
    if "morgen" in low:
        return [], (now.date() + timedelta(days=1)).isoformat()

    candidate_dt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if candidate_dt <= now:
        candidate_dt += timedelta(days=1)
    return [], candidate_dt.date().isoformat()


class AlarmManager:
    """Verwaltet dauerhafte Wecker in alarms.json und überwacht sie per schedule."""

    def __init__(
        self,
        alarms_path: str | Path = "alarms.json",
        output_device: Optional[int | str] = None,
        gpio_pin: Optional[int] = 17,
        on_alarm_triggered: Optional[Callable[[dict[str, Any], bool], None]] = None,
        on_hardware_button: Optional[Callable[[], None]] = None,
    ) -> None:
        self.path = Path(alarms_path)
        self.output_device = output_device
        self.on_alarm_triggered = on_alarm_triggered
        self.on_hardware_button = on_hardware_button

        self._lock = threading.Lock()
        self._scheduler = schedule.Scheduler()
        self._alarms: list[dict[str, Any]] = self._load()

        self._ringing = threading.Event()
        self._trolling_active = False
        self._active_alarm_label = ""
        self._stop_requested = threading.Event()
        self._gpio_button = self._init_gpio_button(gpio_pin)

        self._sync_schedule()

    def _init_gpio_button(self, gpio_pin: Optional[int]) -> object:
        if gpio_pin is None or gpio_pin < 0 or platform.system() != "Linux":
            return None
        if GpioButton is not None:
            try:
                pin_factory = ensure_gpiozero_lgpio_factory()
                kwargs: dict[str, Any] = {"pull_up": True, "bounce_time": 0.12}
                if pin_factory is not None:
                    kwargs["pin_factory"] = pin_factory
                btn = GpioButton(gpio_pin, **kwargs)
                btn.when_pressed = self._handle_gpio_press
                chip = find_rp1_gpio_chip() or "lgpio"
                print(f"[GPIO] Physischer Stopp-Taster an BCM GPIO {gpio_pin} ({chip}, Pull-Up) aktiv.")
                return btn
            except Exception as exc:
                print(f"[GPIO-Hinweis] lgpio/gpiozero für GPIO {gpio_pin} fehlgeschlagen ({exc}) – prüfe gpiod.")

        return self._start_gpiod_button_watcher(gpio_pin)

    def _start_gpiod_button_watcher(self, gpio_pin: int) -> Optional[threading.Thread]:
        """Fallback für Raspberry Pi 5 RP1 über libgpiod v2 (ohne RPi.GPIO)."""
        try:
            import gpiod  # type: ignore[import-not-found]
            from gpiod.line import Bias, Direction, Edge  # type: ignore[import-not-found]
        except Exception:
            print(f"[GPIO-Hinweis] GPIO {gpio_pin} nicht verfügbar – nutze Tastatur/Sprache.")
            return None

        chip_path = find_rp1_gpio_chip()
        if not chip_path:
            return None

        def _watch() -> None:
            try:
                with gpiod.request_lines(
                    chip_path,
                    consumer="jarvis-alarm-button",
                    config={
                        int(gpio_pin): gpiod.LineSettings(
                            direction=Direction.INPUT,
                            bias=Bias.PULL_UP,
                            edge_detection=Edge.FALLING,
                            debounce_period=timedelta(milliseconds=120),
                        )
                    },
                ) as request:
                    print(f"[GPIO] RP1 gpiod Stopp-Taster an BCM GPIO {gpio_pin} ({chip_path}) aktiv.")
                    while True:
                        if request.wait_edge_events(timeout=1.0):
                            events = request.read_edge_events()
                            if events:
                                self._handle_gpio_press()
            except Exception as exc:
                print(f"[GPIO-Hinweis] gpiod-Überwachung beendet ({exc}).")

        thread = threading.Thread(target=_watch, daemon=True)
        thread.start()
        return thread

    def _handle_gpio_press(self) -> None:
        if self._ringing.is_set():
            self._stop_requested.set()
            sd.stop()
        if self.on_hardware_button:
            self.on_hardware_button()

    def _load(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                return raw
        except Exception:
            pass
        return []

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._alarms, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def _sync_schedule(self) -> None:
        self._scheduler.clear()
        for alarm in self._alarms:
            if not alarm.get("enabled", True):
                continue
            alarm_id = alarm["id"]
            t_str = alarm["time"]
            days: list[str] = alarm.get("days", [])

            if not days:
                self._scheduler.every().day.at(t_str).do(self._check_and_fire, alarm_id=alarm_id)
            else:
                for day in days:
                    if hasattr(self._scheduler.every(), day):
                        getattr(self._scheduler.every(), day).at(t_str).do(
                            self._check_and_fire, alarm_id=alarm_id
                        )

    def _check_and_fire(self, alarm_id: str) -> None:
        today_str = date.today().isoformat()
        with self._lock:
            target = next((a for a in self._alarms if a.get("id") == alarm_id), None)
            if not target or not target.get("enabled", True):
                return

            days = target.get("days", [])
            target_date = target.get("date")
            if not days and target_date:
                if target_date < today_str:
                    target["enabled"] = False
                    self._save()
                    return
                if target_date != today_str:
                    return

            # Einmal-Wecker nach dem Auslösen deaktivieren
            if not days:
                target["enabled"] = False
                self._save()

            self._active_alarm_label = target.get("label") or f"Wecker {target['time']} Uhr"

        trolling = random.random() < 0.25
        self._trolling_active = trolling
        self._stop_requested.clear()
        self._ringing.set()

        if self.on_alarm_triggered:
            self.on_alarm_triggered(target, trolling)

    def start_background_loop(self, stop_event: threading.Event) -> threading.Thread:
        def _loop() -> None:
            while not stop_event.is_set():
                with self._lock:
                    self._scheduler.run_pending()
                stop_event.wait(0.8)

        thread = threading.Thread(target=_loop, daemon=True)
        thread.start()
        return thread

    @property
    def is_ringing(self) -> bool:
        return self._ringing.is_set()

    @property
    def trolling_active(self) -> bool:
        return self._trolling_active

    def play_alarm_burst(self, stop_event: Optional[threading.Event] = None) -> None:
        """Spielt einen Weck-Zyklus ab (sofort abbrechbar via stop_alarm oder stop_event)."""
        if self._stop_requested.is_set() or not self._ringing.is_set():
            return
        if stop_event is not None and stop_event.is_set():
            return
        sample_rate = 44100
        samples = (
            generate_trolling_beep(sample_rate=sample_rate)
            if self._trolling_active
            else generate_standard_scifi_pulse(sample_rate=sample_rate)
        )
        try:
            sd.play(samples, samplerate=sample_rate, device=self.output_device)
            deadline = time.monotonic() + (samples.size / sample_rate)
            while time.monotonic() < deadline:
                if (
                    self._stop_requested.is_set()
                    or not self._ringing.is_set()
                    or (stop_event is not None and stop_event.is_set())
                ):
                    sd.stop()
                    break
                time.sleep(0.05)
        finally:
            sd.stop()

    def stop_alarm(self) -> bool:
        """Stoppt den aktiven Wecker und gibt zurück, ob der Trolling-Modus aktiv war."""
        was_trolling = self._trolling_active
        self._stop_requested.set()
        self._ringing.clear()
        sd.stop()
        with self._lock:
            self._sync_schedule()
        return was_trolling

    def _format_alarm(self, alarm: dict[str, Any]) -> str:
        t_str = alarm["time"]
        days = alarm.get("days", [])
        if days == ALL_DAYS:
            when = "jeden Tag"
        elif days == WORKDAYS:
            when = "werktags"
        elif days == WEEKEND:
            when = "am Wochenende"
        elif days:
            when = "jeden " + ", ".join(WEEKDAY_NAMES_DE.get(d, d) for d in days)
        else:
            when = f"am {alarm.get('date', 'nächsten Termin')}"
        return f"{when} um {t_str} Uhr"

    def handle_voice_command(self, query: str) -> str:
        """Verarbeitet Sprachbefehle zum Stellen, Ändern, Auflisten und Löschen von Weckern."""
        low = query.lower()

        if any(w in low for w in ("welche wecker", "zeig wecker", "meine wecker", "wecker gestellt")):
            with self._lock:
                active = [a for a in self._alarms if a.get("enabled", True)]
            if not active:
                return "Chef, es sind aktuell keine aktiven Wecker gestellt."
            desc = "; ".join(self._format_alarm(a) for a in active)
            return f"Chef, folgende Wecker sind aktiv: {desc}."

        if any(w in low for w in ("lösch", "entfern", "deaktivier", "ausschalt")):
            if "alle" in low:
                with self._lock:
                    self._alarms = []
                    self._save()
                    self._sync_schedule()
                return "Sehr wohl, Chef. Ich habe alle Wecker gelöscht."

            target_time = _extract_time_str(query)
            with self._lock:
                before = len(self._alarms)
                if target_time:
                    self._alarms = [a for a in self._alarms if a.get("time") != target_time]
                elif self._alarms:
                    self._alarms.pop()
                removed = before - len(self._alarms)
                self._save()
                self._sync_schedule()

            if removed == 0:
                return "Chef, ich habe keinen passenden Wecker zum Löschen gefunden."
            return "Sehr wohl, Chef. Der Wecker wurde gelöscht."

        time_str = _extract_time_str(query)
        if not time_str:
            return "Chef, für welche Uhrzeit soll ich den Wecker genau stellen?"

        days, target_date = _extract_days_and_date(query, time_str)
        is_change = any(w in low for w in ("änder", "verschieb", "korrigier"))

        new_alarm = {
            "id": str(uuid.uuid4())[:8],
            "time": time_str,
            "days": days,
            "date": target_date,
            "label": "Jarvis-Wecker",
            "enabled": True,
        }

        with self._lock:
            if is_change and self._alarms:
                self._alarms[-1] = new_alarm
            else:
                self._alarms.append(new_alarm)
            self._save()
            self._sync_schedule()

        verb = "geändert" if is_change else "gestellt"
        return f"Sehr wohl, Chef. Wecker {verb} auf {self._format_alarm(new_alarm)}."
