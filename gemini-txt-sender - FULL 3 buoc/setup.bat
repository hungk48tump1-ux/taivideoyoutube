@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo.
echo ====================================================================
echo           Z115_DHVIPPRO - UNIVERSAL SETUP                     
echo           Tu dong nhan dien GPU va cai dat dung moi truong    
echo ====================================================================
echo.

:: -----------------------------------------------------------------
:: BUOC 1: Kiem tra Python
:: -----------------------------------------------------------------
echo [1/8] Kiem tra Python...
python --version >nul 2>&1
if errorlevel 1 (
    echo.
    echo  [ERROR] Python chua duoc cai!
    echo  - Tai tai: https://www.python.org/downloads/
    echo  - QUAN TRONG: Tich vao "Add Python to PATH" khi cai!
    echo.
    pause
    start "" "https://www.python.org/downloads/"
    exit /b 1
)
for /f "tokens=*" %%v in ('python --version 2^>^&1') do set PYVER=%%v
echo  [OK] %PYVER%

:: -----------------------------------------------------------------
:: BUOC 2: Nang cap pip
:: -----------------------------------------------------------------
echo.
echo [2/8] Nang cap pip...
python -m pip install --upgrade pip --quiet
echo  [OK] pip san sang

:: -----------------------------------------------------------------
:: BUOC 3: Phat hien GPU bang Python script
:: -----------------------------------------------------------------
echo.
echo [3/8] Phat hien GPU va CUDA version...

set WHEEL=cpu
set TORCH_URL=

python _detect_gpu.py > "%TEMP%\torch_info.tmp" 2>&1
if errorlevel 1 (
    echo  [WARN] Khong detect duoc GPU, se dung CPU.
    goto :install_torch
)

for /f "tokens=1,2" %%A in ('python _detect_gpu.py 2^>nul') do (
    set WHEEL=%%A
    set TORCH_URL=%%B
)

python _detect_gpu.py >nul
echo  [OK] Ket qua: PyTorch wheel = !WHEEL!


:: -----------------------------------------------------------------
:: BUOC 4: Cai PyTorch dung version, tu dong verify va fallback
:: -----------------------------------------------------------------
:install_torch
echo.
echo [4/8] Cai PyTorch (!WHEEL!)...

set TORCH_OK=0
echo import torch > "%TEMP%\check_torch.py"
echo if "!WHEEL!"=="cpu": >> "%TEMP%\check_torch.py"
echo     print('OK') >> "%TEMP%\check_torch.py"
echo else: >> "%TEMP%\check_torch.py"
echo     import sys; sys.exit(0 if torch.cuda.is_available() else 1) >> "%TEMP%\check_torch.py"

python "%TEMP%\check_torch.py" >nul 2>&1
if not errorlevel 1 (
    set TORCH_OK=1
)
del "%TEMP%\check_torch.py" >nul 2>&1

if "!TORCH_OK!"=="1" (
    echo  [OK] PyTorch hien tai hop le, bo qua buoc cai lai.
    goto :install_libs
)

echo  - Go PyTorch cu...
pip uninstall torch torchvision torchaudio -y --quiet 2>nul

if "!WHEEL!"=="cpu" (
    echo  - Cai PyTorch CPU...
    pip install torch torchvision torchaudio --quiet
) else (
    echo  - Cai PyTorch !WHEEL! tu !TORCH_URL!...
    pip install torch torchvision torchaudio --index-url !TORCH_URL! --quiet
)

echo  - Kiem tra CUDA sau cai...
if not "!WHEEL!"=="cpu" (
    python -c "import torch; import sys; sys.exit(0 if torch.cuda.is_available() else 1)" 2>nul
    if errorlevel 1 (
        echo.
        echo  [WARN] CUDA van khong hoat dong sau khi cai !WHEEL!!
        echo  - Thu cai ban du phong...

        set FALLBACK=0
        if "!WHEEL!"=="cu128" (
            set FALLBACK_WHEEL=cu121
            set FALLBACK_URL=https://download.pytorch.org/whl/cu121
            set FALLBACK=1
        )
        if "!WHEEL!"=="cu121" (
            set FALLBACK_WHEEL=cu118
            set FALLBACK_URL=https://download.pytorch.org/whl/cu118
            set FALLBACK=1
        )
        if "!WHEEL!"=="cu118" (
            set FALLBACK_WHEEL=cpu
            set FALLBACK_URL=
            set FALLBACK=1
        )

        if "!FALLBACK!"=="1" (
            echo  - Thu !FALLBACK_WHEEL!...
            pip uninstall torch torchvision torchaudio -y --quiet 2>nul
            if "!FALLBACK_WHEEL!"=="cpu" (
                pip install torch torchvision torchaudio --quiet
            ) else (
                pip install torch torchvision torchaudio --index-url !FALLBACK_URL! --quiet
            )
        )
    ) else (
        echo  [OK] CUDA hoat dong tot!
    )
)

:: -----------------------------------------------------------------
:: BUOC 5: Kiem tra va in ket qua cuoi cung
:: -----------------------------------------------------------------
echo.
echo [5/8] Thong tin GPU / CUDA:
echo import torch > "%TEMP%\check_env.py"
echo print('  PyTorch version :', torch.__version__) >> "%TEMP%\check_env.py"
echo ok = torch.cuda.is_available() >> "%TEMP%\check_env.py"
echo print('  CUDA available  :', ok) >> "%TEMP%\check_env.py"
echo if ok: >> "%TEMP%\check_env.py"
echo     print('  GPU name        :', torch.cuda.get_device_name(0)) >> "%TEMP%\check_env.py"
echo     print('  VRAM            :', round(torch.cuda.get_device_properties(0).total_memory / 1024**3, 1), 'GB') >> "%TEMP%\check_env.py"
echo     print('  CUDA version    :', torch.version.cuda) >> "%TEMP%\check_env.py"
echo else: >> "%TEMP%\check_env.py"
echo     print('  Mode            : CPU only') >> "%TEMP%\check_env.py"
python "%TEMP%\check_env.py" 2>nul
del "%TEMP%\check_env.py" 2>nul

:: -----------------------------------------------------------------
:: BUOC 6: Cai cac thu vien Python khac
:: -----------------------------------------------------------------
:install_libs
echo.
echo [6/8] Cai thu vien Python...
pip install "playwright>=1.44.0" openai-whisper faster-whisper scipy soundfile librosa numpy requests --quiet
echo  [OK] Thu vien Python da cai.

:: -----------------------------------------------------------------
:: BUOC 7: Cai Playwright browsers
:: -----------------------------------------------------------------
echo.
echo [7/8] Cai Playwright Chromium...
python -m playwright install chromium >nul 2>&1
if errorlevel 1 (
    python -m playwright install >nul 2>&1
)
echo  [OK] Playwright san sang.

:: -----------------------------------------------------------------
:: BUOC 8: Kiem tra va Tu dong cai FFmpeg
:: -----------------------------------------------------------------
echo.
echo [8/8] Kiem tra FFmpeg...
ffmpeg -version >nul 2>&1
if not errorlevel 1 (
    echo  [OK] FFmpeg da co san trong he thong.
) else (
    if exist "ffmpeg\bin\ffmpeg.exe" (
        echo  [OK] FFmpeg da co san trong thu muc local.
    ) else (
        echo  [WARN] Khong tim thay FFmpeg. Dang tu dong tai ve (khoang 30MB)...
        powershell -Command "& { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; $url = 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip'; $out = 'ffmpeg.zip'; Invoke-WebRequest -Uri $url -OutFile $out; Expand-Archive -Path $out -DestinationPath 'ffmpeg_temp' -Force; $inner = Get-ChildItem -Path 'ffmpeg_temp' -Directory | Select-Object -First 1; Move-Item -Path \"$($inner.FullName)\bin\", \"$($inner.FullName)\presets\" -Destination 'ffmpeg' -Force; Remove-Item -Path 'ffmpeg_temp', 'ffmpeg.zip' -Recurse -Force; }" >nul 2>&1
        
        if exist "ffmpeg\bin\ffmpeg.exe" (
            echo  [OK] Da tu dong cai dat FFmpeg vao thu muc 'ffmpeg'.
        ) else (
            echo.
            echo  [ERROR] Khong the tu dong tai FFmpeg.
            echo  - Vui long tai thu cong: https://www.gyan.dev/ffmpeg/builds/
            echo  - Giai nen vao thu muc 'ffmpeg' trong app.
        )
    )
)

:: -----------------------------------------------------------------
:: HOAN TAT
:: -----------------------------------------------------------------
echo.
echo ====================================================================
echo   [OK] SETUP HOAN TAT!                                          
echo   Chay ung dung: nhap dup vao run.bat                       
echo ====================================================================
echo.

set /p RUN_NOW="  Chay ung dung ngay bay gio? (Y/N): "
if /i "!RUN_NOW!"=="Y" (
    echo  Dang khoi dong...
    start "" pythonw main.py
)

echo.
pause
endlocal
