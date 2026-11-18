@echo off
setlocal enabledelayedexpansion
title J.A.R.V.I.S. // SYSTEM LAUNCHER

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [Fehler] Virtuelle Umgebung ^(.venv^) wurde noch nicht erstellt.
    echo Bitte starte zuerst per Doppelklick: 1_Windows_Installieren.bat
    echo.
    pause
    exit /b 1
)

echo ==================================================
echo   J.A.R.V.I.S. // ALL-IN-ONE SYSTEM START
echo ==================================================

REM --- Eventuell noch laufende versteckte Hintergrund-Instanzen (pythonw.exe) beenden ---
taskkill /F /IM pythonw.exe >nul 2>&1

REM --- Konfigurierte Ollama-Modelle aus .env auslesen ---
set "TEXT_MODEL=qwen2.5:7b"
set "VISION_MODEL=moondream"
if exist ".env" (
    for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
        set "K=%%~A"
        set "V=%%~B"
        if /I "!K!"=="OLLAMA_TEXT_MODEL" if not "!V!"=="" set "TEXT_MODEL=!V!"
        if /I "!K!"=="OLLAMA_VISION_MODEL" if not "!V!"=="" set "VISION_MODEL=!V!"
    )
)

REM --- Ollama-Executable ermitteln ---
set "OLLAMA_CMD="
if exist "%LOCALAPPDATA%\Programs\Ollama\ollama.exe" (
    set "OLLAMA_CMD=%LOCALAPPDATA%\Programs\Ollama\ollama.exe"
) else (
    where ollama >nul 2>&1
    if !errorlevel!==0 set "OLLAMA_CMD=ollama"
)

REM --- Schritt 1: Ollama-Server starten & im Hintergrund vorwaermen ---
if defined OLLAMA_CMD (
    curl.exe -sf --max-time 1 http://127.0.0.1:11434/api/tags >nul 2>&1
    if errorlevel 1 (
        echo [1/2] Starte Ollama-Server im Hintergrund...
        powershell -NoProfile -NonInteractive -WindowStyle Hidden -Command "Start-Process -FilePath '%OLLAMA_CMD%' -ArgumentList 'serve' -WindowStyle Hidden" >nul 2>&1
    ) else (
        echo [1/2] Ollama-Server laeuft bereits auf Port 11434.
    )

    start "" /B powershell -NoProfile -NonInteractive -WindowStyle Hidden -Command "for ($i=0; $i -lt 10; $i++) { try { Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/tags' -TimeoutSec 1 | Out-Null; break } catch { Start-Sleep -Seconds 1 } }; try { $tags = (Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/tags' -TimeoutSec 2).models.name; if ($tags -notcontains '!TEXT_MODEL!') { & '%OLLAMA_CMD%' pull '!TEXT_MODEL!' | Out-Null }; if ('!VISION_MODEL!' -ne '' -and ($tags -notcontains '!VISION_MODEL!')) { & '%OLLAMA_CMD%' pull '!VISION_MODEL!' | Out-Null }; Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/generate' -Method Post -ContentType 'application/json' -Body (@{model='!TEXT_MODEL!';keep_alive='30m'} | ConvertTo-Json -Compress) -TimeoutSec 25 | Out-Null } catch {}" >nul 2>&1
) else (
    echo [Hinweis] Ollama nicht gefunden - starte im Online-/Fallback-Modus.
)

REM --- Schritt 2: API- & System-Tray-Pakete in .venv sicherstellen & JARVIS im Fenstermodus starten ---
".venv\Scripts\python.exe" -c "import groq, google.genai, dotenv, requests, pystray, cryptography" >nul 2>&1
if errorlevel 1 (
    echo [Setup] Installiere fehlende Pakete ^(groq, google-genai, python-dotenv, requests, pystray, cryptography^) in .venv...
    ".venv\Scripts\python.exe" -m pip install --no-cache-dir -q groq google-genai python-dotenv requests pystray cryptography
)

echo [2/2] Starte J.A.R.V.I.S. Command Center HUD ^(Sprach- ^& Status-Animationen^)...
if exist ".venv\Scripts\activate.bat" (
    call ".venv\Scripts\activate.bat" >nul 2>&1
)
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
".venv\Scripts\python.exe" "%~dp0main.py" %*

set "EXIT_CODE=%errorlevel%"

REM --- Hintergrund-Prozesse beim Schliessen der GUI sauber beenden ---
taskkill /F /IM ollama.exe /T >nul 2>&1
taskkill /F /IM ollama_llama_server.exe /T >nul 2>&1

if not "%EXIT_CODE%"=="0" (
    echo.
    echo [Hinweis] Jarvis wurde mit Fehlercode %EXIT_CODE% beendet.
    pause
)
exit /b %EXIT_CODE%
