@echo off
setlocal enabledelayedexpansion

echo ==================================================
echo   Jarvis - Plug ^& Play Installation (Windows 11)
echo ==================================================
echo.

cd /d "%~dp0"

set "PYTHON_CMD="
where py >nul 2>&1
if %errorlevel%==0 (
    set "PYTHON_CMD=py -3"
) else (
    where python >nul 2>&1
    if %errorlevel%==0 (
        set "PYTHON_CMD=python"
    )
)

if "%PYTHON_CMD%"=="" (
    echo [Fehler] Python 3 wurde nicht gefunden.
    echo Bitte installiere Python 3.10+ von https://www.python.org/downloads/
    echo ^(Wichtig: Haken bei "Add Python to PATH" setzen!^)
    pause
    exit /b 1
)

if not exist ".venv" (
    echo [1/5] Erstelle virtuelle Python-Umgebung ^(.venv^)...
    %PYTHON_CMD% -m venv .venv
    if errorlevel 1 (
        echo [Fehler] Konnte .venv nicht erstellen.
        pause
        exit /b 1
    )
) else (
    echo [1/5] Virtuelle Umgebung .venv existiert bereits.
)

echo.
echo [2/5] Installiere Python-Pakete aus requirements.txt...
call ".venv\Scripts\python.exe" -m pip install --upgrade pip
call ".venv\Scripts\pip.exe" install --no-cache-dir -r requirements.txt
if errorlevel 1 (
    echo [Fehler] Installation der Python-Pakete fehlgeschlagen.
    pause
    exit /b 1
)

echo.
echo [3/5] Lade deutsches Piper-Stimmmodell ^(de_DE-karlsson-low^)...
if not exist "models" mkdir "models"

if not exist "models\de_DE-karlsson-low.onnx" (
    curl.exe -L --fail -o "models\de_DE-karlsson-low.onnx" "https://huggingface.co/rhasspy/piper-voices/resolve/main/de/de_DE/karlsson/low/de_DE-karlsson-low.onnx"
    if errorlevel 1 (
        echo [Fehler] Download von de_DE-karlsson-low.onnx fehlgeschlagen.
        pause
        exit /b 1
    )
)

if not exist "models\de_DE-karlsson-low.onnx.json" (
    curl.exe -L --fail -o "models\de_DE-karlsson-low.onnx.json" "https://huggingface.co/rhasspy/piper-voices/resolve/main/de/de_DE/karlsson/low/de_DE-karlsson-low.onnx.json"
    if errorlevel 1 (
        echo [Fehler] Download von de_DE-karlsson-low.onnx.json fehlgeschlagen.
        pause
        exit /b 1
    )
)

echo.
echo [4/5] Pruefe und installiere Ollama + Offline-Modelle ^(qwen2.5:7b ^& moondream^)...
set "OLLAMA_EXE=%LOCALAPPDATA%\Programs\Ollama\ollama.exe"
set "OLLAMA_APP=%LOCALAPPDATA%\Programs\Ollama\ollama app.exe"
set "OLLAMA_FOUND=0"

where ollama >nul 2>&1
if %errorlevel%==0 set "OLLAMA_FOUND=1"
if exist "%OLLAMA_EXE%" set "OLLAMA_FOUND=1"

if "%OLLAMA_FOUND%"=="0" (
    echo Ollama nicht gefunden - starte automatische Installation...
    where winget >nul 2>&1
    if !errorlevel!==0 (
        winget install -e --id Ollama.Ollama --silent --accept-package-agreements --accept-source-agreements
    )
    where ollama >nul 2>&1
    if !errorlevel!==0 set "OLLAMA_FOUND=1"
    if exist "%OLLAMA_EXE%" set "OLLAMA_FOUND=1"
)

if "%OLLAMA_FOUND%"=="0" (
    echo Lade OllamaSetup.exe direkt herunter...
    curl.exe -L --fail -o "%TEMP%\OllamaSetup.exe" "https://ollama.com/download/OllamaSetup.exe"
    if not errorlevel 1 (
        start /wait "" "%TEMP%\OllamaSetup.exe" /VERYSILENT /NORESTART
        del /f /q "%TEMP%\OllamaSetup.exe" >nul 2>&1
    ) else (
        echo [Warnung] Ollama-Download fehlgeschlagen - Offline-Modus vorerst uebersprungen.
    )
    where ollama >nul 2>&1
    if !errorlevel!==0 set "OLLAMA_FOUND=1"
    if exist "%OLLAMA_EXE%" set "OLLAMA_FOUND=1"
)

set "OLLAMA_CMD="
if exist "%OLLAMA_EXE%" (
    set "OLLAMA_CMD=%OLLAMA_EXE%"
) else (
    where ollama >nul 2>&1
    if !errorlevel!==0 set "OLLAMA_CMD=ollama"
)

if defined OLLAMA_CMD (
    curl.exe -sf --max-time 1 http://127.0.0.1:11434/api/tags >nul 2>&1
    if errorlevel 1 (
        echo Starte Ollama-Server im Hintergrund...
        start "" /B "%OLLAMA_CMD%" serve >nul 2>&1
        for /L %%I in (1,1,10) do (
            timeout /t 1 /nobreak >nul
            curl.exe -sf --max-time 1 http://127.0.0.1:11434/api/tags >nul 2>&1
            if not errorlevel 1 goto :install_ollama_pull
        )
    )
:install_ollama_pull
    echo Lade Offline-Modell qwen2.5:7b...
    "%OLLAMA_CMD%" pull qwen2.5:7b
    if errorlevel 1 (
        echo [Hinweis] qwen2.5:7b konnte nicht geladen werden - versuche gemma3:4b...
        "%OLLAMA_CMD%" pull gemma3:4b
    )
    echo Lade Offline-Modell moondream...
    "%OLLAMA_CMD%" pull moondream
    if errorlevel 1 echo [Warnung] Modell moondream konnte nicht geladen werden.
) else (
    echo [Warnung] Ollama konnte nicht automatisch eingerichtet werden.
)

echo.
echo [5/5] Erstelle Konfiguration ^(.env^)...
if not exist ".env" (
    if exist ".env.example" (
        copy /y ".env.example" ".env" >nul
        echo Leere .env aus .env.example erstellt.
    ) else (
        type nul > ".env"
        echo Leere .env erstellt.
    )
) else (
    echo Bestehende .env beibehalten.
)
echo API-Keys, E-Mail, Watchlist und Stadt kannst du jederzeit direkt im HUD unter "EINSTELLUNGEN" eintragen.

echo.
echo ==================================================
echo   Installation abgeschlossen!
echo ==================================================
echo Starte Jarvis ab jetzt einfach per Doppelklick ohne CMD-Fenster auf:
echo   Jarvis_starten.vbs
echo ^(Alternativ mit Diagnose-Konsole: 2_Windows_Jarvis_Starten.bat^)
echo.
timeout /t 4 >nul
