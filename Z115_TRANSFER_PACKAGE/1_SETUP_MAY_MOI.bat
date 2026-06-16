@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

echo.
echo ================================================================
echo   Z115_DHVIPPRO - SETUP MAY MOI
echo   Tao moi truong local, kiem tra Python, GPU va cai dat tu dong
echo ================================================================
echo.

set "ROOT=%~dp0"
set "APP_VENV=%ROOT%.venv"
set "APP_PY=%APP_VENV%\Scripts\python.exe"
set "APP_PIP=%APP_VENV%\Scripts\pip.exe"
set "OMNI_DIR=%ROOT%OmniVoice"
set "OMNI_VENV=%OMNI_DIR%\venv"
set "OMNI_PY=%OMNI_VENV%\Scripts\python.exe"
set "OMNI_PIP=%OMNI_VENV%\Scripts\pip.exe"
set "WHEEL=cpu"
set "TORCH_URL="

call :find_python
if errorlevel 1 goto :fail

call :check_python_version
if errorlevel 1 goto :fail

call :detect_gpu

echo [1/7] Tao virtualenv cho app...
if not exist "%APP_PY%" (
    %PY_BOOTSTRAP% -m venv "%APP_VENV%"
    if errorlevel 1 (
        echo [ERROR] Khong tao duoc .venv cho app.
        goto :fail
    )
)
call :upgrade_pip "%APP_PY%"
if errorlevel 1 goto :fail

echo [2/7] Cai PyTorch cho app (!WHEEL!)...
call :install_torch "%APP_PY%" "%APP_PIP%"
if errorlevel 1 goto :fail

echo [3/7] Cai thu vien Python cho app...
"%APP_PIP%" install -r "%ROOT%requirements.txt"
if errorlevel 1 (
    echo [ERROR] Cai requirements.txt that bai.
    goto :fail
)
if exist "%ROOT%pipeline_automation\requirements.txt" (
    "%APP_PIP%" install -r "%ROOT%pipeline_automation\requirements.txt"
    if errorlevel 1 (
        echo [ERROR] Cai pipeline_automation requirements that bai.
        goto :fail
    )
)

echo [4/7] Kiem tra / cai FFmpeg local...
call :ensure_ffmpeg
if errorlevel 1 goto :fail

echo [5/7] Cai OmniVoice local...
if exist "%OMNI_DIR%\run_omnivoice_cli.py" (
    if not exist "%OMNI_PY%" (
        %PY_BOOTSTRAP% -m venv "%OMNI_VENV%"
        if errorlevel 1 (
            echo [ERROR] Khong tao duoc venv cho OmniVoice.
            goto :fail
        )
    )
    call :upgrade_pip "%OMNI_PY%"
    if errorlevel 1 goto :fail
    call :install_torch "%OMNI_PY%" "%OMNI_PIP%"
    if errorlevel 1 goto :fail
    if exist "%OMNI_DIR%\requirements-portable.txt" (
        "%OMNI_PIP%" install -r "%OMNI_DIR%\requirements-portable.txt"
    ) else (
        "%OMNI_PIP%" install -r "%OMNI_DIR%\requirements.txt"
    )
    if errorlevel 1 (
        echo [ERROR] Cai thu vien OmniVoice that bai.
        goto :fail
    )
    echo [OK] OmniVoice local da san sang.
) else (
    echo [WARN] Khong thay OmniVoice local. App van chay, nhung tinh nang OmniVoice se khong dung duoc.
)

echo [6/7] Tao thu muc runtime...
if not exist "%ROOT%outputs" mkdir "%ROOT%outputs"
if not exist "%ROOT%logs" mkdir "%ROOT%logs"
if not exist "%ROOT%browser_profile" mkdir "%ROOT%browser_profile"

echo.
echo ====================== HOAN TAT ======================
echo Python bootstrap : %PY_BOOTSTRAP%
echo Torch wheel      : !WHEEL!
if /I "!WHEEL!"=="cpu" (
    echo GPU mode         : CPU
) else (
    echo GPU mode         : !WHEEL!
)
"%APP_PY%" -c "import torch; print('App torch:', torch.__version__); print('CUDA:', torch.cuda.is_available()); print('GPU:', torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'CPU only')"
echo.
echo Chay app bang file: 2_RUN_APP.bat
echo ======================================================
echo.
pause
exit /b 0

:find_python
set "PY_BOOTSTRAP="
python --version >nul 2>&1
if not errorlevel 1 (
    set "PY_BOOTSTRAP=python"
    goto :eof
)
py -3 --version >nul 2>&1
if not errorlevel 1 (
    set "PY_BOOTSTRAP=py -3"
    goto :eof
)
echo [ERROR] Khong tim thay Python trong PATH.
echo         Cai Python 3.11 hoac 3.12 64-bit va tick Add Python to PATH.
exit /b 1

:check_python_version
for /f "tokens=2" %%v in ('%PY_BOOTSTRAP% --version 2^>^&1') do set "PY_VER=%%v"
for /f "tokens=1,2 delims=." %%a in ("%PY_VER%") do (
    set "PY_MAJOR=%%a"
    set "PY_MINOR=%%b"
)
echo Python phat hien: %PY_VER%
if not "%PY_MAJOR%"=="3" (
    echo [ERROR] App can Python 3.11 hoac 3.12.
    exit /b 1
)
if %PY_MINOR% LSS 11 (
    echo [ERROR] Python qua cu. Can Python 3.11+.
    exit /b 1
)
if %PY_MINOR% GEQ 13 (
    echo [WARN] Python %PY_VER% co the chua duoc test day du. Khuyen dung 3.11 hoac 3.12.
)
exit /b 0

:detect_gpu
echo GPU detection...
set "WHEEL=cpu"
set "TORCH_URL="
for /f "tokens=1,2" %%A in ('%PY_BOOTSTRAP% "%ROOT%_detect_gpu.py" 2^>nul') do (
    set "WHEEL=%%A"
    set "TORCH_URL=%%B"
)
if not defined WHEEL set "WHEEL=cpu"
echo Torch wheel du kien: !WHEEL!
exit /b 0

:upgrade_pip
set "TARGET_PY=%~1"
"%TARGET_PY%" -m pip install --upgrade pip setuptools wheel
if errorlevel 1 (
    echo [ERROR] Khong nang cap duoc pip cho %TARGET_PY%.
    exit /b 1
)
exit /b 0

:install_torch
set "TARGET_PY=%~1"
set "TARGET_PIP=%~2"
set "CHECK_WHEEL=!WHEEL!"
"%TARGET_PIP%" uninstall -y torch torchvision torchaudio >nul 2>&1
if /I "!WHEEL!"=="cpu" (
    "%TARGET_PIP%" install torch torchvision torchaudio
) else (
    "%TARGET_PIP%" install torch torchvision torchaudio --index-url !TORCH_URL!
)
if errorlevel 1 (
    echo [ERROR] Cai PyTorch that bai.
    exit /b 1
)

"%TARGET_PY%" -c "import os, sys, torch; w=os.environ.get('CHECK_WHEEL','cpu'); ok=(w=='cpu') or torch.cuda.is_available(); sys.exit(0 if ok else 1)"
if not errorlevel 1 exit /b 0

if /I "!WHEEL!"=="cu128" (
    set "WHEEL=cu121"
    set "TORCH_URL=https://download.pytorch.org/whl/cu121"
    goto :retry_torch
)
if /I "!WHEEL!"=="cu121" (
    set "WHEEL=cu118"
    set "TORCH_URL=https://download.pytorch.org/whl/cu118"
    goto :retry_torch
)
if /I "!WHEEL!"=="cu118" (
    set "WHEEL=cpu"
    set "TORCH_URL="
    goto :retry_torch
)

exit /b 0

:retry_torch
echo [WARN] Thu fallback PyTorch sang !WHEEL!...
set "CHECK_WHEEL=!WHEEL!"
"%TARGET_PIP%" uninstall -y torch torchvision torchaudio >nul 2>&1
if /I "!WHEEL!"=="cpu" (
    "%TARGET_PIP%" install torch torchvision torchaudio
) else (
    "%TARGET_PIP%" install torch torchvision torchaudio --index-url !TORCH_URL!
)
if errorlevel 1 (
    echo [ERROR] Fallback PyTorch that bai.
    exit /b 1
)
"%TARGET_PY%" -c "import os, sys, torch; w=os.environ.get('CHECK_WHEEL','cpu'); ok=(w=='cpu') or torch.cuda.is_available(); sys.exit(0 if ok else 1)"
if errorlevel 1 (
    echo [ERROR] Torch da cai nhung CUDA van khong hoat dong.
    exit /b 1
)
exit /b 0

:ensure_ffmpeg
ffmpeg -version >nul 2>&1
if not errorlevel 1 (
    echo [OK] FFmpeg da co san trong he thong.
    exit /b 0
)
if exist "%ROOT%ffmpeg\bin\ffmpeg.exe" (
    echo [OK] FFmpeg local da ton tai.
    exit /b 0
)
echo Dang tai FFmpeg local...
powershell -NoProfile -ExecutionPolicy Bypass -Command "& { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; $url = 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip'; $zip = Join-Path '%ROOT%' 'ffmpeg.zip'; $tmp = Join-Path '%ROOT%' 'ffmpeg_tmp'; Invoke-WebRequest -Uri $url -OutFile $zip; Expand-Archive -Path $zip -DestinationPath $tmp -Force; $inner = Get-ChildItem -Path $tmp -Directory | Select-Object -First 1; New-Item -ItemType Directory -Force -Path (Join-Path '%ROOT%' 'ffmpeg') | Out-Null; Move-Item -Path (Join-Path $inner.FullName 'bin') -Destination (Join-Path '%ROOT%' 'ffmpeg') -Force; if (Test-Path (Join-Path $inner.FullName 'presets')) { Move-Item -Path (Join-Path $inner.FullName 'presets') -Destination (Join-Path '%ROOT%' 'ffmpeg') -Force }; Remove-Item -Recurse -Force $tmp, $zip; }"
if exist "%ROOT%ffmpeg\bin\ffmpeg.exe" exit /b 0
echo [ERROR] Khong tai duoc FFmpeg local.
exit /b 1

:fail
echo.
echo [FAIL] Setup chua hoan tat.
echo.
pause
exit /b 1
