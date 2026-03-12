@echo off
title KOZMICKE BANE v3.0
python app.py
if %errorlevel% NEQ 0 (
    echo.
    echo  Chyba! Pravdepodobne chyba kninica pywebview.
    echo  Spusti najprv:  INSTALL.bat
    echo.
    pause
)
