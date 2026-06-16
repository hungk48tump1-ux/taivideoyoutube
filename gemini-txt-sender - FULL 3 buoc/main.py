"""
main.py - Entry point của Gemini TXT Chunk Sender
"""
import json
import subprocess
import sys
from pathlib import Path

# ── Thêm FFmpeg local vào PATH để các thư viện luôn tìm thấy ffmpeg/ffprobe ──
import os
base_dir = Path(__file__).parent.resolve()
ffmpeg_bin = base_dir / "ffmpeg" / "bin"
if ffmpeg_bin.exists():
    os.environ["PATH"] = str(ffmpeg_bin) + os.pathsep + os.environ.get("PATH", "")

# ── Ẩn tất cả cửa sổ CMD trên Windows (pythonw / background mode) ──
# Patch này áp dụng cho toàn bộ app, kể cả thư viện bên thứ 3
# (faster-whisper, ctranslate2, torch, ffmpeg, playwright...)
if sys.platform == "win32":
    _OrigPopen = subprocess.Popen

    class _NoCmdPopen(_OrigPopen):
        def __init__(self, *args, **kwargs):
            kwargs.setdefault("creationflags", 0)
            kwargs["creationflags"] |= subprocess.CREATE_NO_WINDOW
            super().__init__(*args, **kwargs)

    subprocess.Popen = _NoCmdPopen  # type: ignore
    # Bảo đảm subprocess.run / subprocess.call cũng dùng Popen đã patch


def load_json(path: str, default: dict = None) -> dict:
    """Đọc file JSON, trả về dict rỗng nếu không tìm thấy."""
    p = Path(path)
    if not p.exists():
        return default or {}
    try:
        return json.loads(p.read_text(encoding="utf-8-sig"))
    except Exception as e:
        print(f"[WARNING] Không đọc được {path}: {e}")
        return default or {}


def main():
    # ── Đường dẫn cơ bản (relative to main.py) ──
    base_dir = Path(__file__).parent
    config_dir = base_dir / "config"

    # ── Load config ──
    config = load_json(str(config_dir / "config.json"), {
        "gemini_url": "https://gemini.google.com/app",
        "target_model_text": "Pro",
        "first_chunk_size": 30,
        "next_chunk_size": 50,
        "skip_empty_lines": True,
        "inter_chunk_delay_seconds": 3,
        "response_timeout_seconds": 300,
        "stable_wait_seconds": 5,
        "browser_channel": "chrome",
        "user_data_dir": str(base_dir / "browser_profile"),
        "autosave": True,
        "random_delay_min": 0.5,
        "random_delay_max": 1.5,
        "output_dir": str(base_dir / "outputs"),
        "log_dir": str(base_dir / "logs"),
        "log_level": "DEBUG",
    })

    # Resolve đường dẫn tương đối
    for key in ("user_data_dir", "output_dir", "log_dir"):
        if key in config and not Path(config[key]).is_absolute():
            config[key] = str(base_dir / config[key])

    # ── Load selectors ──
    selectors = load_json(str(config_dir / "selectors.json"), {})

    # ── Khởi tạo logger ──
    from core.logger import setup_logger
    setup_logger(
        log_dir=config.get("log_dir", str(base_dir / "logs")),
        log_level=config.get("log_level", "DEBUG"),
    )

    # ── Khởi động UI ──
    from ui.app_ui import App
    app = App(config=config, selectors=selectors)

    try:
        app.mainloop()
    except KeyboardInterrupt:
        pass
    finally:
        # Đóng controller nếu đang chạy
        if hasattr(app, "_controller") and app._controller:
            try:
                app._controller.close()
            except Exception:
                pass


if __name__ == "__main__":
    main()
