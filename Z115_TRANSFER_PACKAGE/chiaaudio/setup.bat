@echo off
setlocal
cd /d "%~dp0"
title CAI DAT WHISPER GPU - CHO MOT CHUT...

echo ============================================================
echo  CAI DAT TU DONG - Audio to Text (Whisper GPU)
echo ============================================================
echo.

:: -------------------------------------------------------
:: 1. Kiem tra Python va version
:: -------------------------------------------------------
echo [1/5] Kiem tra Python...
python --version >nul 2>&1
if errorlevel 1 (
    echo.
    echo  LOI: Khong tim thay Python!
    echo  Vui long cai Python 3.10.11 tai:
    echo  https://www.python.org/downloads/release/python-31011/
    echo  Nho tick "Add Python to PATH" khi cai.
    pause
    exit /b 1
)

:: Lay minor version de kiem tra (phai la 3.9 -> 3.12)
for /f "tokens=2 delims= " %%v in ('python --version 2^>^&1') do set PYVER=%%v
for /f "tokens=2 delims=." %%m in ("%PYVER%") do set PYMINOR=%%m

echo  Phat hien Python %PYVER%

if %PYMINOR% GTR 12 (
    echo.
    echo  LOI: Python %PYVER% KHONG TUONG THICH voi PyTorch^^!
    echo  PyTorch chi ho tro Python 3.9 den 3.12.
    echo.
    echo  Vui long cai Python 3.10.11 tai:
    echo  https://www.python.org/downloads/release/python-31011/
    echo  Chon file: Windows installer 64-bit
    echo  Nho tick "Add Python to PATH" khi cai.
    echo  Sau do xoa thu muc whispergpu va chay lai setup.bat
    pause
    exit /b 1
)
if %PYMINOR% LSS 9 (
    echo.
    echo  LOI: Python %PYVER% qua cu. Can 3.9 tro len.
    echo  Vui long cai Python 3.10.11.
    pause
    exit /b 1
)
echo  OK - Version hop le
echo.

:: -------------------------------------------------------
:: 2. Tao virtual environment
:: -------------------------------------------------------
echo [2/5] Tao virtual environment "whispergpu"...
if exist whispergpu (
    echo  Da ton tai, bo qua buoc nay.
) else (
    python -m venv whispergpu
    if errorlevel 1 (
        echo  LOI: Khong tao duoc venv!
        pause
        exit /b 1
    )
    echo  OK - Da tao xong
)
echo.

set PY="%~dp0whispergpu\Scripts\python.exe"
set PIP="%~dp0whispergpu\Scripts\pip.exe"

:: -------------------------------------------------------
:: 3. Nang cap pip
:: -------------------------------------------------------
echo [3/5] Nang cap pip...
%PY% -m pip install --upgrade pip --quiet
echo  OK
echo.

:: -------------------------------------------------------
:: 4. Cai PyTorch voi CUDA 12.1
:: -------------------------------------------------------
echo [4/5] Cai PyTorch + CUDA (co the mat 5-15 phut tuy mang)...
%PIP% install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu121 --quiet
if errorlevel 1 (
    echo  LOI: Cai PyTorch that bai. Kiem tra ket noi mang.
    pause
    exit /b 1
)
echo  OK - Da cai PyTorch
echo.

:: -------------------------------------------------------
:: 5. Cai cac thu vien con lai
:: -------------------------------------------------------
echo [5/5] Cai openai-whisper, faster-whisper, pydub, ffmpeg-python...
%PIP% install openai-whisper faster-whisper pydub ffmpeg-python --quiet
if errorlevel 1 (
    echo  LOI: Cai thu vien that bai.
    pause
    exit /b 1
)
echo  OK - Da cai xong tat ca thu vien
echo.

:: -------------------------------------------------------
:: Kiem tra CUDA hoat dong khong
:: -------------------------------------------------------
echo Kiem tra CUDA...
%PY% -c "import torch; cuda=torch.cuda.is_available(); print('  CUDA:', cuda); print('  GPU:', torch.cuda.get_device_name(0) if cuda else 'KHONG CO GPU - se chay bang CPU')"
echo.

:: -------------------------------------------------------
:: Nhac cai ffmpeg he thong
:: -------------------------------------------------------
echo ============================================================
echo  LUU Y: Can cai ffmpeg vao he thong (neu chua co)
echo ============================================================
echo  1. Tai tai: https://www.gyan.dev/ffmpeg/builds/
echo     (chon ffmpeg-release-essentials.zip)
echo  2. Giai nen, copy thu muc vao C:\ffmpeg
echo  3. Them C:\ffmpeg\bin vao PATH cua Windows
echo     (Control Panel - Environment Variables - Path - New)
echo  4. Mo CMD moi, go "ffmpeg -version" de kiem tra
echo.
ffmpeg -version >nul 2>&1
if errorlevel 1 (
    echo  CANH BAO: ffmpeg CHUA co trong PATH - can cai theo huong dan tren!
) else (
    echo  OK - ffmpeg da co san trong PATH
)
echo.

echo ============================================================
echo  CAI DAT HOAN TAT!
echo  Bay gio ban co the chay run_gpu.bat de su dung phan mem.
echo ============================================================
echo.
pause
