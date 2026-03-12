@echo off
title KOZMICKE BANE v3.0
python game_login_system.py
if %errorlevel% NEQ 0 (
    echo.
    echo  Chyba! Skontroluj ci mas Python nainstalovany.
    pause
)
