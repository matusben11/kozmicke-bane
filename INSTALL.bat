@echo off
title KOZMICKE BANE - Inštalácia
color 06
echo.
echo  ╔══════════════════════════════════════════╗
echo  ║   KOZMICKE BANE v3.0 — INŠTALÁCIA       ║
echo  ╚══════════════════════════════════════════╝
echo.
echo  Inštalujem potrebné knižnice...
echo.

pip install pywebview --upgrade

echo.
if %errorlevel% == 0 (
    color 0A
    echo  ╔══════════════════════════════════════════╗
    echo  ║   HOTOVO!  Teraz spusti SPUSTI.bat       ║
    echo  ╚══════════════════════════════════════════╝
) else (
    color 0C
    echo  CHYBA pri inštalácii!
    echo  Skús spustiť príkazový riadok ako administrátor.
)
echo.
pause
