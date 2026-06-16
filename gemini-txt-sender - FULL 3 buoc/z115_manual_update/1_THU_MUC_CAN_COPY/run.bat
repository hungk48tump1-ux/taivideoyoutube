@echo off
setlocal
cd /d "%~dp0"

echo Dang kiem tra moi truong...

:: 1. Kiem tra Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python chua duoc cai dat! 
    echo Vui long chay setup.bat truoc.
    pause
    exit /b 1
)

:: 2. Them FFmpeg local vao PATH neu co
if exist "ffmpeg\bin\ffmpeg.exe" (
    set "PATH=%~dp0ffmpeg\bin;%PATH%"
)

:: 3. Kiem tra thu vien quan trong (demo playwright)
python -c "import playwright" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Thư vien chua duoc cai dat day du!
    echo Vui long chay setup.bat de hoan tat cai dat.
    pause
    exit /b 1
)

:: 4. Chay App
start "" pythonw main.py
exit /b 0
