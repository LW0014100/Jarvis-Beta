@echo off
setlocal enabledelayedexpansion
title JARVIS - Windows Shell & Explorer Reparatur

echo ============================================================
echo   JARVIS WINDOWS SHELL ^& EXPLORER REPARATUR-TOOL
echo ============================================================
echo.

REM --- Pruefen, ob Administrator-Rechte fuer HKLM vorhanden sind ---
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [Hinweis] Fuer HKEY_LOCAL_MACHINE werden Administrator-Rechte benoetigt.
    echo Fordere UAC-Berechtigung an...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b 0
)

echo [1/4] Pruefe aktuellen Winlogon\Shell Registry-Wert...
for /f "tokens=2,*" %%A in ('reg query "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon" /v Shell 2^>nul') do (
    echo       Aktueller HKLM Shell-Wert: %%B
)

echo [2/4] Setze HKLM Winlogon\Shell sicher auf "explorer.exe" zurueck...
reg add "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon" /v Shell /t REG_SZ /d "explorer.exe" /f
if errorlevel 1 (
    echo [Warnung] HKLM Winlogon\Shell konnte nicht beschrieben werden.
) else (
    echo       [OK] HKLM Winlogon\Shell = explorer.exe
)

echo [3/4] Entferne eventuelle HKCU Winlogon\Shell Ueberschreibung...
reg query "HKCU\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon" /v Shell >nul 2>&1
if %errorlevel%==0 (
    reg delete "HKCU\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon" /v Shell /f >nul 2>&1
    echo       [OK] HKCU Shell-Override entfernt.
) else (
    echo       [OK] Kein HKCU Shell-Override vorhanden.
)

echo [4/4] Pruefe und starte Windows-Desktop (explorer.exe)...
tasklist /FI "IMAGENAME eq explorer.exe" 2>nul | find /I "explorer.exe" >nul
if errorlevel 1 (
    echo       explorer.exe laeuft nicht - starte Windows-Oberflaeche neu...
    start "" "%windir%\explorer.exe"
) else (
    echo       explorer.exe laeuft bereits. Starte Shell bei Bedarf frisch...
    taskkill /F /IM explorer.exe >nul 2>&1
    timeout /t 1 /nobreak >nul
    start "" "%windir%\explorer.exe"
)

echo.
echo ============================================================
echo   Reparatur abgeschlossen! Windows-Oberflaeche ist aktiv.
echo ============================================================
pause
exit /b 0
