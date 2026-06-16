@echo off
cd /d "%~dp0"
echo ===================================================
echo   CAI DAT Z115_DHVIPPRO - CACH 1 (Su dung Python)
echo ===================================================
echo.
echo Dang cai dat cac thu vien Python (requirements.txt)...
pip install -r requirements.txt
echo.
echo Dang tai loi trinh duyet tu dong cua Playwright...
python -m playwright install chromium
echo.
echo ===================================================
echo   HOAN TAT!
echo   Vui long an phim bat ky de thoat.
echo   Sau nay ban chi can chay file "run.bat" de mo app!
echo ===================================================
pause
