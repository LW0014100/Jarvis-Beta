# -*- coding: utf-8 -*-
"""
J.A.R.V.I.S. Command Center – All-in-One Windows Setup Wizard (setup_wizard.py)
===============================================================================
Grafischer Einzel-Datei-Installer für Windows 10 / 11 (64-Bit), komplett ohne
Konsolenfenster (`--noconsole`). Entpackt `JARVIS_App.zip`, prüft und
installiert bei Bedarf Visual C++ Redistributable & Ollama Offline-KI, lädt
ausgewählte KI-Modelle, richtet `%LOCALAPPDATA%\\JarvisCommandCenter` portabel
ohne fremde API-Keys ein und erstellt die Desktop-Verknüpfung
`JARVIS Command Center`.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
import urllib.request
import zipfile

if platform.system().lower() == "windows":
    import winreg

IS_WINDOWS = platform.system().lower() == "windows"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if IS_WINDOWS else 0
APP_NAME = "JARVIS Command Center"
APP_VERSION = "1.0.0"
APP_EXE = "JARVIS.exe"


def _hide_kwargs() -> dict[str, int]:
    return {"creationflags": CREATE_NO_WINDOW} if IS_WINDOWS and CREATE_NO_WINDOW else {}


def _resource_root() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(getattr(sys, "_MEIPASS")).resolve()
    return Path(__file__).resolve().parent


def _find_payload_zip() -> Path | None:
    root = _resource_root()
    for cand in (
        root / "JARVIS_App.zip",
        Path(__file__).resolve().parent / "JARVIS_App.zip",
        Path(__file__).resolve().parent / "Fertige_Verkaufs_Version" / "JARVIS_App.zip",
    ):
        if cand.is_file():
            return cand
    return None


def _find_payload_dir() -> Path | None:
    root = _resource_root()
    for cand in (
        root / "JARVIS_App",
        Path(__file__).resolve().parent / "Fertige_Verkaufs_Version" / "JARVIS_App",
    ):
        if cand.is_dir() and (cand / APP_EXE).is_file():
            return cand
    return None


def is_vc_redist_installed() -> bool:
    if not IS_WINDOWS:
        return True
    keys = (
        r"SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64",
        r"SOFTWARE\WOW6432Node\Microsoft\VisualStudio\14.0\VC\Runtimes\x64",
    )
    for sub in keys:
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, sub) as key:
                val, _ = winreg.QueryValueEx(key, "Installed")
                if int(val) == 1:
                    return True
        except OSError:
            continue
    return False


def find_ollama_exe() -> Path | None:
    which_hit = shutil.which("ollama")
    if which_hit and Path(which_hit).is_file():
        return Path(which_hit)
    local_app = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        Path(local_app) / "Programs" / "Ollama" / "ollama.exe" if local_app else Path(),
        Path(r"C:\Program Files\Ollama\ollama.exe"),
        Path(r"C:\Program Files (x86)\Ollama\ollama.exe"),
    ]
    for cand in candidates:
        if str(cand) and cand.is_file():
            return cand
    return None


def _download_file(url: str, dest: Path) -> bool:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "JarvisSetup/1.0"})
        with urllib.request.urlopen(req, timeout=180) as resp, open(dest, "wb") as fh:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                fh.write(chunk)
        return dest.is_file() and dest.stat().st_size > 1024
    except Exception:
        return False


def _create_shortcut(shortcut_path: Path, target_exe: Path, work_dir: Path, icon_path: Path | None = None) -> None:
    if not IS_WINDOWS:
        return
    shortcut_path.parent.mkdir(parents=True, exist_ok=True)
    ico = str(icon_path if icon_path and icon_path.is_file() else target_exe)
    ps_script = (
        "$ws = New-Object -ComObject WScript.Shell; "
        f"$sc = $ws.CreateShortcut('{str(shortcut_path).replace(chr(39), chr(39)*2)}'); "
        f"$sc.TargetPath = '{str(target_exe).replace(chr(39), chr(39)*2)}'; "
        f"$sc.WorkingDirectory = '{str(work_dir).replace(chr(39), chr(39)*2)}'; "
        f"$sc.IconLocation = '{ico.replace(chr(39), chr(39)*2)}'; "
        f"$sc.Description = '{APP_NAME}'; "
        "$sc.Save();"
    )
    subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", ps_script],
        timeout=30,
        **_hide_kwargs(),
    )


def _register_uninstall_entry(install_dir: Path, exe_path: Path) -> None:
    if not IS_WINDOWS:
        return
    try:
        reg_path = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\JarvisCommandCenter"
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, reg_path) as key:
            winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, APP_NAME)
            winreg.SetValueEx(key, "DisplayVersion", 0, winreg.REG_SZ, APP_VERSION)
            winreg.SetValueEx(key, "Publisher", 0, winreg.REG_SZ, "JARVIS AI Systems")
            winreg.SetValueEx(key, "InstallLocation", 0, winreg.REG_SZ, str(install_dir))
            winreg.SetValueEx(key, "DisplayIcon", 0, winreg.REG_SZ, str(exe_path))
            uninstall_cmd = f'powershell.exe -NoProfile -WindowStyle Hidden -Command "Remove-Item -LiteralPath \'{install_dir}\' -Recurse -Force -ErrorAction SilentlyContinue"'
            winreg.SetValueEx(key, "UninstallString", 0, winreg.REG_SZ, uninstall_cmd)
    except Exception:
        pass


def perform_installation(
    install_dir: Path,
    options: dict[str, bool],
    log_cb,
    progress_cb,
) -> Path:
    progress_cb(5)
    log_cb("Bereite Zielverzeichnis vor...")
    install_dir.mkdir(parents=True, exist_ok=True)

    payload_zip = _find_payload_zip()
    payload_dir = _find_payload_dir()

    progress_cb(15)
    if payload_zip is not None:
        log_cb(f"Entpacke JARVIS Command Center nach {install_dir} ...")
        with zipfile.ZipFile(payload_zip, "r") as zf:
            members = zf.infolist()
            total = max(1, len(members))
            for idx, member in enumerate(members, start=1):
                rel = Path(member.filename)
                if rel.name.lower() in (".env", ".vault_master.key", "secure_vault.enc"):
                    continue
                zf.extract(member, install_dir)
                if idx % 25 == 0 or idx == total:
                    pct = 15 + int((idx / total) * 35)
                    progress_cb(pct)
    elif payload_dir is not None:
        log_cb(f"Kopiere Anwendungsdateien nach {install_dir} ...")
        shutil.copytree(
            str(payload_dir),
            str(install_dir),
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns(".env", ".vault_master.key", "secure_vault.enc"),
        )
        progress_cb(50)
    else:
        raise FileNotFoundError(
            "Kein Anwendungs-Paket (JARVIS_App.zip / JARVIS_App) im Setup gefunden."
        )

    # Portable Benutzer-Daten unter %LOCALAPPDATA%\JarvisCommandCenter initialisieren (ohne fremde API-Keys)
    progress_cb(55)
    user_data_dir = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")) / "JarvisCommandCenter"
    user_data_dir.mkdir(parents=True, exist_ok=True)

    selected_models: list[str] = []
    if options.get("qwen", True):
        selected_models.append("qwen2.5:7b")
    if options.get("gemma", False):
        selected_models.append("gemma3:4b")
    if options.get("llama", False):
        selected_models.append("llama3.2:3b")
    if options.get("moondream", True):
        selected_models.append("moondream")

    text_model = "qwen2.5:7b"
    for cand in ("qwen2.5:7b", "gemma3:4b", "llama3.2:3b"):
        if cand in selected_models:
            text_model = cand
            break

    enable_online = bool(options.get("online_keyless", True))
    enable_ollama = bool(options.get("ollama", True))
    enable_piper = bool(options.get("piper_tts", True))
    primary_provider = "auto" if enable_online else ("offline" if enable_ollama else "auto")

    choice_data = {
        "enable_free_keyless_ai": enable_online,
        "offline_ollama_enabled": enable_ollama,
        "piper_tts_enabled": enable_piper,
        "default_text_model": text_model,
        "default_vision_model": "moondream",
        "selected_models": selected_models,
        "primary_provider": primary_provider,
        "max_agent_steps": 200,
        "voice_id": "de-DE-ConradNeural",
        "voice_pitch": "-15Hz",
        "voice_rate": "-5%",
        "model_priority": ["pollinations", "gemini", "groq", "ollama"],
    }
    (user_data_dir / "installer_choice.json").write_text(
        json.dumps(choice_data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    env_path = user_data_dir / ".env"
    env_example = install_dir / ".env.example"
    if not env_path.exists():
        if env_example.is_file():
            shutil.copy2(env_example, env_path)
        else:
            env_path.write_text(
                "\n".join(
                    [
                        "# J.A.R.V.I.S. Portable Configuration",
                        f"AI_PROVIDER={primary_provider}",
                        "ONLINE_PROVIDER=pollinations",
                        f"OLLAMA_MODEL={text_model}",
                        f"OLLAMA_VISION_MODEL=moondream",
                        "MAX_AGENT_STEPS=200",
                        "EXECUTION_MODE=SEMI_AUTO",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

    cfg_json = user_data_dir / "config.json"
    if not cfg_json.exists():
        cfg_json.write_text(
            json.dumps(
                {
                    "AI_PROVIDER": primary_provider,
                    "ONLINE_PROVIDER": "pollinations",
                    "OLLAMA_MODEL": text_model,
                    "OLLAMA_VISION_MODEL": "moondream",
                    "MAX_AGENT_STEPS": 200,
                    "EXECUTION_MODE": "SEMI_AUTO",
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    # Visual C++ Redistributable x64 prüfen & still installieren
    progress_cb(62)
    if IS_WINDOWS and not is_vc_redist_installed():
        log_cb("Installiere Microsoft Visual C++ Redistributable (still)...")
        with tempfile.TemporaryDirectory() as tmpdir:
            vc_exe = Path(tmpdir) / "vc_redist.x64.exe"
            if _download_file("https://aka.ms/vs/17/release/vc_redist.x64.exe", vc_exe):
                subprocess.run(
                    [str(vc_exe), "/install", "/quiet", "/norestart"],
                    timeout=300,
                    **_hide_kwargs(),
                )
                log_cb("Visual C++ Redistributable installiert.")
            else:
                log_cb("Hinweis: VC++ Download übersprungen (offline).")
    else:
        log_cb("Visual C++ Redistributable bereits vorhanden.")

    # Ollama Engine prüfen, still installieren & gewählte Modelle herunterladen
    progress_cb(72)
    if enable_ollama:
        ollama_exe = find_ollama_exe()
        if ollama_exe is None:
            log_cb("Lade & installiere Ollama Offline-KI Engine (still)...")
            with tempfile.TemporaryDirectory() as tmpdir:
                ol_setup = Path(tmpdir) / "OllamaSetup.exe"
                if _download_file("https://ollama.com/download/OllamaSetup.exe", ol_setup):
                    subprocess.run(
                        [str(ol_setup), "/SILENT"],
                        timeout=300,
                        **_hide_kwargs(),
                    )
                    ollama_exe = find_ollama_exe()
                else:
                    log_cb("Hinweis: OllamaSetup Download fehlgeschlagen/offline.")
        else:
            log_cb(f"Ollama bereits vorhanden: {ollama_exe}")

        if ollama_exe is not None and selected_models:
            try:
                subprocess.Popen([str(ollama_exe), "serve"], **_hide_kwargs())
            except Exception:
                pass
            for idx, model_tag in enumerate(selected_models, start=1):
                progress_cb(75 + int((idx / max(1, len(selected_models))) * 15))
                log_cb(f"Lade KI-Modell '{model_tag}' (kann je nach Internet dauern)...")
                try:
                    proc = subprocess.run(
                        [str(ollama_exe), "pull", model_tag],
                        capture_output=True,
                        text=True,
                        timeout=600,
                        **_hide_kwargs(),
                    )
                    if proc.returncode == 0:
                        log_cb(f"Modell '{model_tag}' erfolgreich bereitgestellt.")
                    else:
                        log_cb(f"Hinweis zu '{model_tag}': {proc.stderr.strip()[:120] or 'wird beim Start nachgeladen'}")
                except Exception as exc:
                    log_cb(f"Hinweis zu '{model_tag}': {exc}")

    # Verknüpfungen erstellen
    progress_cb(94)
    exe_path = install_dir / APP_EXE
    ico_path = install_dir / "web_ui" / "favicon.ico"
    if IS_WINDOWS:
        if options.get("desktop_icon", True):
            log_cb("Erstelle Desktop-Verknüpfung 'JARVIS Command Center'...")
            desktop_dir = Path(os.environ.get("USERPROFILE") or Path.home()) / "Desktop"
            _create_shortcut(desktop_dir / f"{APP_NAME}.lnk", exe_path, install_dir, ico_path)
        programs_dir = (
            Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
            / "Microsoft"
            / "Windows"
            / "Start Menu"
            / "Programs"
        )
        _create_shortcut(programs_dir / f"{APP_NAME}.lnk", exe_path, install_dir, ico_path)
        _register_uninstall_entry(install_dir, exe_path)

    progress_cb(100)
    log_cb("Installation erfolgreich abgeschlossen!")
    return exe_path


class JarvisSetupGUI:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(f"{APP_NAME} – All-in-One Setup v{APP_VERSION}")
        self.root.geometry("690x610")
        self.root.resizable(False, False)
        self.root.configure(bg="#090d16")

        default_dir = (
            Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
            / "Programs"
            / "JARVIS Command Center"
        )
        self.install_dir_var = tk.StringVar(value=str(default_dir))

        self.var_core = tk.BooleanVar(value=True)
        self.var_online = tk.BooleanVar(value=True)
        self.var_ollama = tk.BooleanVar(value=True)
        self.var_qwen = tk.BooleanVar(value=True)
        self.var_gemma = tk.BooleanVar(value=False)
        self.var_llama = tk.BooleanVar(value=False)
        self.var_moondream = tk.BooleanVar(value=True)
        self.var_piper = tk.BooleanVar(value=True)
        self.var_desktop = tk.BooleanVar(value=True)
        self.var_launch = tk.BooleanVar(value=True)

        self._installed_exe: Path | None = None
        self._build_ui()

    def _build_ui(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("TProgressbar", troughcolor="#141b2d", background="#00e5ff", thickness=16)

        header = tk.Frame(self.root, bg="#0d1424", height=74)
        header.pack(fill="x")
        header.pack_propagate(False)
        tk.Label(
            header,
            text="J.A.R.V.I.S. COMMAND CENTER  //  SETUP v1.0",
            font=("Segoe UI", 15, "bold"),
            fg="#00e5ff",
            bg="#0d1424",
        ).pack(anchor="w", padx=22, pady=(14, 2))
        tk.Label(
            header,
            text="Vollautonomes 4-Agenten-System • Offline- & kostenlose Online-KI • Windows Desktop HUD",
            font=("Segoe UI", 9),
            fg="#94a3b8",
            bg="#0d1424",
        ).pack(anchor="w", padx=22)

        body = tk.Frame(self.root, bg="#090d16")
        body.pack(fill="both", expand=True, padx=22, pady=14)

        tk.Label(
            body,
            text="Komponenten & KI-Engines auswählen:",
            font=("Segoe UI", 10, "bold"),
            fg="#e2e8f0",
            bg="#090d16",
        ).pack(anchor="w", pady=(0, 6))

        cb_opts = {
            "bg": "#090d16",
            "fg": "#e2e8f0",
            "selectcolor": "#111b2e",
            "activebackground": "#090d16",
            "activeforeground": "#00e5ff",
            "font": ("Segoe UI", 9),
        }

        tk.Checkbutton(
            body,
            text="JARVIS Command Center & 4-Agenten-System (Pflicht)",
            variable=self.var_core,
            state="disabled",
            disabledforeground="#7dd3fc",
            **cb_opts,
        ).pack(anchor="w", pady=2)

        tk.Checkbutton(
            body,
            text="Kostenlose Online-KI ohne Anmeldung direkt aktivieren (Sofort einsatzbereit ohne eigene API-Keys)",
            variable=self.var_online,
            **cb_opts,
        ).pack(anchor="w", pady=2)

        tk.Checkbutton(
            body,
            text="Ollama Offline-KI Engine automatisch mitinstallieren",
            variable=self.var_ollama,
            command=self._toggle_ollama_models,
            **cb_opts,
        ).pack(anchor="w", pady=2)

        models_frame = tk.Frame(body, bg="#0d1424", highlightbackground="#1e293b", highlightthickness=1)
        models_frame.pack(fill="x", padx=(24, 0), pady=4)
        self._model_checks: list[tk.Checkbutton] = []
        for var, label in (
            (self.var_qwen, "Qwen 2.5 (7B - Empfohlen)"),
            (self.var_gemma, "Gemma 3 (4B - Schnell für schwächere Laptops)"),
            (self.var_llama, "Llama 3.2 (3B)"),
            (self.var_moondream, "Moondream (Lokale Kamera-KI)"),
        ):
            chk = tk.Checkbutton(
                models_frame,
                text=label,
                variable=var,
                bg="#0d1424",
                fg="#cbd5e1",
                selectcolor="#111b2e",
                activebackground="#0d1424",
                activeforeground="#00e5ff",
                font=("Segoe UI", 9),
            )
            chk.pack(anchor="w", padx=10, pady=2)
            self._model_checks.append(chk)

        tk.Checkbutton(
            body,
            text="Lokale deutsche Sprachausgabe (Piper TTS)",
            variable=self.var_piper,
            **cb_opts,
        ).pack(anchor="w", pady=2)

        tk.Checkbutton(
            body,
            text="Desktop-Verknüpfung 'JARVIS Command Center' erstellen",
            variable=self.var_desktop,
            **cb_opts,
        ).pack(anchor="w", pady=2)

        tk.Checkbutton(
            body,
            text="JARVIS nach Abschluss automatisch starten",
            variable=self.var_launch,
            **cb_opts,
        ).pack(anchor="w", pady=2)

        dir_row = tk.Frame(body, bg="#090d16")
        dir_row.pack(fill="x", pady=(10, 8))
        tk.Label(dir_row, text="Zielordner:", fg="#94a3b8", bg="#090d16", font=("Segoe UI", 9)).pack(side="left")
        tk.Entry(
            dir_row,
            textvariable=self.install_dir_var,
            bg="#0f172a",
            fg="#e2e8f0",
            insertbackground="#00e5ff",
            relief="flat",
            font=("Segoe UI", 9),
        ).pack(side="left", fill="x", expand=True, padx=8, ipady=4)
        tk.Button(
            dir_row,
            text="Ändern...",
            command=self._browse_dir,
            bg="#1e293b",
            fg="#e2e8f0",
            relief="flat",
            padx=10,
        ).pack(side="right")

        self.progress = ttk.Progressbar(body, mode="determinate", maximum=100)
        self.progress.pack(fill="x", pady=(4, 6))

        self.log_box = tk.Text(
            body,
            height=6,
            bg="#0d1424",
            fg="#94a3b8",
            font=("Consolas", 8),
            relief="flat",
            state="disabled",
        )
        self.log_box.pack(fill="x", pady=(0, 10))

        btn_row = tk.Frame(body, bg="#090d16")
        btn_row.pack(fill="x")
        self.btn_install = tk.Button(
            btn_row,
            text="Jetzt installieren",
            command=self._start_install,
            bg="#00e5ff",
            fg="#06101c",
            font=("Segoe UI", 10, "bold"),
            relief="flat",
            padx=20,
            pady=7,
        )
        self.btn_install.pack(side="right")

        tk.Button(
            btn_row,
            text="Abbrechen",
            command=self.root.destroy,
            bg="#1e293b",
            fg="#cbd5e1",
            font=("Segoe UI", 9),
            relief="flat",
            padx=14,
            pady=7,
        ).pack(side="right", padx=(0, 8))

    def _toggle_ollama_models(self) -> None:
        state = "normal" if self.var_ollama.get() else "disabled"
        for chk in self._model_checks:
            chk.configure(state=state)

    def _browse_dir(self) -> None:
        chosen = filedialog.askdirectory(initialdir=self.install_dir_var.get())
        if chosen:
            self.install_dir_var.set(chosen)

    def _append_log(self, text: str) -> None:
        def _ui():
            self.log_box.configure(state="normal")
            self.log_box.insert("end", f"> {text}\n")
            self.log_box.see("end")
            self.log_box.configure(state="disabled")
        self.root.after(0, _ui)

    def _set_progress(self, pct: int) -> None:
        self.root.after(0, lambda: self.progress.configure(value=max(0, min(100, pct))))

    def _start_install(self) -> None:
        self.btn_install.configure(state="disabled", text="Installiere...")
        target = Path(self.install_dir_var.get().strip())
        opts = {
            "online_keyless": self.var_online.get(),
            "ollama": self.var_ollama.get(),
            "qwen": self.var_qwen.get(),
            "gemma": self.var_gemma.get(),
            "llama": self.var_llama.get(),
            "moondream": self.var_moondream.get(),
            "piper_tts": self.var_piper.get(),
            "desktop_icon": self.var_desktop.get(),
        }

        def _worker():
            try:
                exe = perform_installation(target, opts, self._append_log, self._set_progress)
                self._installed_exe = exe
                self.root.after(0, self._on_success)
            except Exception as exc:
                self._append_log(f"FEHLER: {exc}")
                self.root.after(0, lambda e=exc: self._on_error(str(e)))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_success(self) -> None:
        self.btn_install.configure(state="normal", text="Fertigstellen", command=self._finish_and_close)
        messagebox.showinfo(APP_NAME, "J.A.R.V.I.S. Command Center wurde erfolgreich installiert!")
        self._finish_and_close()

    def _on_error(self, msg: str) -> None:
        self.btn_install.configure(state="normal", text="Erneut versuchen")
        messagebox.showerror("Installationsfehler", msg)

    def _finish_and_close(self) -> None:
        if self.var_launch.get() and self._installed_exe and self._installed_exe.is_file():
            try:
                subprocess.Popen([str(self._installed_exe)], cwd=str(self._installed_exe.parent), **_hide_kwargs())
            except Exception:
                pass
        self.root.destroy()


def main() -> int:
    if "--silent" in sys.argv:
        default_dir = (
            Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
            / "Programs"
            / "JARVIS Command Center"
        )
        opts = {
            "online_keyless": True,
            "ollama": True,
            "qwen": True,
            "gemma": False,
            "llama": False,
            "moondream": True,
            "piper_tts": True,
            "desktop_icon": True,
        }
        perform_installation(default_dir, opts, lambda m: None, lambda p: None)
        return 0

    root = tk.Tk()
    JarvisSetupGUI(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
