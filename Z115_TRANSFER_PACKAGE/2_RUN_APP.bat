@echo off
setlocal EnableExtensions
cd /d "%~dp0"

if not exist ".venv\Scripts\pythonw.exe" (
    echo [ERROR] Chua co moi truong local.
    echo         Hay chay 1_SETUP_MAY_MOI.bat truoc.
    echo.
    pause
    exit /b 1
)

if exist "ffmpeg\bin\ffmpeg.exe" (
    set "PATH=%~dp0ffmpeg\bin;%PATH%"
)

set "Z115_OMNIVOICE_DIR=%~dp0OmniVoice"

if not exist "outputs" mkdir "outputs"
if not exist "logs" mkdir "logs"
if not exist "browser_profile" mkdir "browser_profile"

start "" ".venv\Scripts\pythonw.exe" main.py
exit /b 0
