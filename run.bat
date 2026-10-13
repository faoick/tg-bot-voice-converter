@echo off
setlocal
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"
where py >nul 2>nul
if not errorlevel 1 (
    py -3 launcher.py
) else (
    where python >nul 2>nul
    if errorlevel 1 (
        echo Install Python 3.10 or newer from https://www.python.org/downloads/
    ) else (
        python launcher.py
    )
)
pause
