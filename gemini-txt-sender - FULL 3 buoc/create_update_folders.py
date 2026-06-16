import os
import shutil
from pathlib import Path

def main():
    base_dir = Path(os.getcwd())
    update_dir = base_dir / "z115_manual_update"
    
    # Dọn dẹp thư mục cũ nếu có
    if update_dir.exists():
        try:
            shutil.rmtree(update_dir)
        except Exception:
            pass
            
    update_dir.mkdir(parents=True, exist_ok=True)
    
    can_copy_dir = update_dir / "1_THU_MUC_CAN_COPY"
    giu_nguyen_dir = update_dir / "2_THU_MUC_GIU_NGUYEN"
    
    can_copy_dir.mkdir(parents=True, exist_ok=True)
    giu_nguyen_dir.mkdir(parents=True, exist_ok=True)
    
    # Danh sách cần copy (chứa code mới nhất)
    code_items = [
        "ui",
        "automation",
        "gemini_extension",
        "core",
        "chiaaudio",
        "pipeline_automation",
        "main.py",
        "run.bat",
        "install.bat",
        "setup.bat",
        "requirements.txt",
        "logo.png",
        "_detect_gpu.py",
        "README.md"
    ]
    
    print("[+] Dang sao chep cac tep tin code moi vao '1_THU_MUC_CAN_COPY'...")
    for item_name in code_items:
        src = base_dir / item_name
        dst = can_copy_dir / item_name
        if src.exists():
            if src.is_dir():
                # Bỏ qua các thư mục cache __pycache__ khi copy
                shutil.copytree(src, dst, ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.pyo'))
            else:
                shutil.copy2(src, dst)
            print(f" -> Da copy: {item_name}")
            
    # Danh sách giữ nguyên (tạo hướng dẫn và folder trống để người dùng dễ hiểu)
    config_items = [
        ("config", "Thu muc nay chua file config.json (Preset The Loai, API Key, cau hinh rieng cua may do)."),
        ("browser_profile", "Thu muc chua tai khoan/profile trinh duyet Playwright da dang nhap tren may kia."),
        ("outputs", "Thu muc chua toan bo van ban, hinh anh, video da tao ra tren may kia."),
        ("logs", "Thu muc chua log van hanh cuc bo cua may kia.")
    ]
    
    print("\n[-] Dang tao cau truc mau thu muc can giu nguyen vao '2_THU_MUC_GIU_NGUYEN'...")
    for folder_name, desc in config_items:
        dst_folder = giu_nguyen_dir / folder_name
        dst_folder.mkdir(parents=True, exist_ok=True)
        # Tạo file hướng dẫn bên trong
        readme_file = dst_folder / "LuuY_GiuNguyenThuMucNayTrenMayKia.txt"
        readme_content = f"LUU Y QUAN TRONG:\n{desc}\n\n👉 Khi cap nhat phan mem sang may khac, anh tuyet doi KHONG DE thu muc nay de len thu muc cu cua may do!"
        readme_file.write_text(readme_content, encoding="utf-8")
        print(f" -> Da tao thu muc mau: {folder_name}/")
        
    # Tạo hướng dẫn chung
    general_readme = update_dir / "HUONG_DAN_CAP_NHAT.txt"
    general_readme_content = """HUONG DAN CAP NHAT PHAN MEM THU CONG SANG MAY TINH KHAC:

Buoc 1: Nen hoac sao chep TOAN BO noi dung nam BEN TRONG thu muc:
        👉 '1_THU_MUC_CAN_COPY'
        (Day la code phien ban moi nhat vua duoc cap nhat)

Buoc 2: Sang may tinh can cap nhat, DAN DE (Paste & Replace) tat ca cac file vua copy o tren
        vao thu muc cai phan mem cua may do.

Buoc 3: LUU Y TUYET DOI:
        👉 KHONG DUOC COPY cac thu muc nam trong '2_THU_MUC_GIU_NGUYEN' de sang may kia.
        (Dieu nay giup may kia giu nguyen 100% Cai dat Preset The loai, API Key, Chrome cua no)

Buoc 4: Cap nhat Extension tren trinh duyet Chrome cua may kia:
        - Mo Chrome tren may kia, truy cap link: chrome://extensions/
        - Bam nut "Tai lai" (Reload 🔁) o the Addon 'Z115_DHVIPPRO' de Chrome nap code content.js moi nhat.

Chuc anh thuc hien thanh cong!
"""
    general_readme.write_text(general_readme_content, encoding="utf-8")
    
    print(f"\n[SUCCESS] HOAN THANH! Anh co the xem thu muc ket qua tai: {update_dir}")

if __name__ == "__main__":
    main()
