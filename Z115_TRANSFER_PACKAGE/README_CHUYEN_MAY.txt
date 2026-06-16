Z115_DHVIPPRO - GOI CHUYEN SANG MAY KHAC

1. MAY MOI CAN GI
- Windows 10/11
- Python 3.11 hoac 3.12 (64-bit), da tick Add Python to PATH
- Internet de cai thu vien va tai Playwright / FFmpeg / OmniVoice
- Chrome / Edge / Chromium de dang nhap Gemini

2. CACH CHAY
- Giai nen / copy nguyen thu muc nay sang may moi
- Chay file: 1_SETUP_MAY_MOI.bat
- Setup se:
  + kiem tra Python version
  + phat hien GPU NVIDIA hay CPU
  + cai torch phu hop cho app
  + cai torch phu hop cho OmniVoice local
  + tai FFmpeg local neu may chua co
- Sau khi setup xong, chay: 2_RUN_APP.bat

3. NHUNG THU CAN TU KIEM TRA TREN MAY MOI
- Dang nhap lai Gemini trong trinh duyet lan dau
- Neu preset nao dang tro den anh bang duong dan may cu
  (vi du ref_image trong config\config.json), cap nhat lai duong dan tren may moi
- Neu muon chon browser cu the, vao app va sua "Custom browser path"

4. OMNIVOICE
- Goi nay da kem:
  + run_omnivoice_cli.py
  + settings_1.json / settings_2.json
  + saved_voices\
- Khong kem venv cu de tranh loi theo may cu
- Setup se tao OmniVoice\venv moi tren may moi

5. CAC THU MUC KHONG KEM
- outputs, logs, browser_profile cu khong duoc mang theo du lieu cu
- may moi se tao moi de sach va de dang nhap lai
