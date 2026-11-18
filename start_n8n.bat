@echo off
setlocal enabledelayedexpansion
title J.A.R.V.I.S. // LOKALES N8N (COMMUNITY EDITION - 0 EUR ABO)

cd /d "%~dp0"
set "N8N_DATA_DIR=%~dp0n8n_data"
if not exist "%N8N_DATA_DIR%" mkdir "%N8N_DATA_DIR%"

echo ============================================================
echo   J.A.R.V.I.S. // LOKALES N8N STARTEN (http://localhost:5678)
echo ============================================================
echo Speicherordner: %N8N_DATA_DIR%
echo.

REM Pruefe ob n8n bereits auf Port 5678 laeuft
curl.exe -sf --max-time 2 http://127.0.0.1:5678/healthz >nul 2>&1
if not errorlevel 1 (
    echo [OK] n8n laeuft bereits unter http://localhost:5678
    start "" "http://localhost:5678"
    exit /b 0
)

REM Option 1: Docker (empfohlen, isoliert und persistent ueber n8n_data)
where docker >nul 2>&1
if !errorlevel!==0 (
    docker info >nul 2>&1
    if !errorlevel!==0 (
        echo [Docker] Starte kostenlose n8n Community Edition...
        docker inspect jarvis-n8n >nul 2>&1
        if not errorlevel 1 (
            docker start jarvis-n8n >nul
        ) else (
            docker run -d ^
              --name jarvis-n8n ^
              --restart unless-stopped ^
              -p 5678:5678 ^
              -v "%N8N_DATA_DIR%:/home/node/.n8n" ^
              -e N8N_SECURE_COOKIE=false ^
              -e GENERIC_TIMEZONE=Europe/Berlin ^
              -e TZ=Europe/Berlin ^
              docker.n8n.io/n8nio/n8n
        )
        if not errorlevel 1 (
            echo Warte auf n8n unter http://localhost:5678 ...
            for /L %%I in (1,1,20) do (
                timeout /t 1 /nobreak >nul
                curl.exe -sf --max-time 1 http://127.0.0.1:5678/healthz >nul 2>&1
                if not errorlevel 1 (
                    echo [OK] Lokales n8n bereit unter http://localhost:5678
                    start "" "http://localhost:5678"
                    exit /b 0
                )
            )
            echo [Hinweis] Container laeuft - oeffne http://localhost:5678 in wenigen Sekunden.
            exit /b 0
        )
    ) else (
        echo [Hinweis] Docker ist installiert, aber Docker Desktop laeuft noch nicht.
    )
)

REM Option 2: Fallback ueber Node.js / npx n8n
where npx >nul 2>&1
if !errorlevel!==0 (
    echo [Fallback: npx] Starte n8n lokal mit N8N_USER_FOLDER=%N8N_DATA_DIR% ...
    set "N8N_USER_FOLDER=%N8N_DATA_DIR%"
    set "N8N_SECURE_COOKIE=false"
    set "N8N_HOST=127.0.0.1"
    set "N8N_PORT=5678"
    start "JARVIS n8n Community" cmd /c "npx --yes n8n start"
    echo [OK] n8n startet im Hintergrundfenster unter http://localhost:5678
    exit /b 0
)

echo.
echo [Fehler] Weder laufendes Docker noch Node.js (npx) gefunden.
echo Bitte installiere Docker Desktop (empfohlen) ODER Node.js LTS,
echo damit n8n lokal und kostenlos auf Port 5678 laufen kann.
pause
exit /b 1
