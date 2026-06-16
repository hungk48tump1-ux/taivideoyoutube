:: SỬA LẠI run_gpu.bat (copy đè toàn bộ)
@echo off
setlocal
cd /d "%~dp0"

:: ép dùng đúng python của venv (không phụ thuộc activate)
set PY="%~dp0whispergpu\Scripts\python.exe"

if not exist %PY% (
  echo KHONG TIM THAY: %PY%
  pause
  exit /b 1
)

%PY% -c "import sys; print('PY:',sys.executable); import torch; print('CUDA:',torch.cuda.is_available()); print('GPU:', torch.cuda.get_device_name(0) if torch.cuda.is_available() else None)"
echo.

%PY% "%~dp0app.py"