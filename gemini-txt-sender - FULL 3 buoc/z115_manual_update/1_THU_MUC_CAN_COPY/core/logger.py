"""
core/logger.py - Hệ thống logging tập trung
"""
import logging
import logging.handlers
import os
from datetime import datetime
from pathlib import Path


def setup_logger(log_dir: str = "logs", log_level: str = "DEBUG") -> logging.Logger:
    """
    Khởi tạo logger với handler ghi file và stdout.
    Trả về logger 'gemini_sender' để dùng trong toàn bộ app.
    """
    log_dir_path = Path(log_dir)
    log_dir_path.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = log_dir_path / f"session_{timestamp}.log"

    logger = logging.getLogger("gemini_sender")
    logger.setLevel(getattr(logging, log_level.upper(), logging.DEBUG))

    # Tránh thêm handler trùng khi gọi lại
    if logger.handlers:
        logger.handlers.clear()

    # Format chi tiết
    fmt = logging.Formatter(
        fmt="%(asctime)s [%(levelname)-8s] %(name)s:%(funcName)s:%(lineno)d - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # Handler ghi ra file (rotate khi đạt 10MB, giữ 5 bản)
    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    file_handler.setLevel(logging.DEBUG)
    logger.addHandler(file_handler)

    # Handler ra console
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(fmt)
    console_handler.setLevel(logging.INFO)
    logger.addHandler(console_handler)

    logger.info("Logger khởi tạo thành công. Log file: %s", log_file)
    return logger


def get_logger() -> logging.Logger:
    """Lấy logger đã được khởi tạo."""
    return logging.getLogger("gemini_sender")
