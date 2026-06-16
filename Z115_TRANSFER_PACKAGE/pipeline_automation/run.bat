@echo off
echo =======================================================
echo     DHTNVIPPRO Pipeline - Image ^& Video Generator
echo =======================================================

:: Kiểm tra cài đặt môi trường
echo Đang kiểm tra thư viện...
pip install -r requirements.txt -q

:: Chạy phần mềm ngầm (ẩn CMD)
start "" pythonw app_ui.py
exit
