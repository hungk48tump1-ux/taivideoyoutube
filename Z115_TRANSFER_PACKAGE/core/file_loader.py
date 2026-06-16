"""
core/file_loader.py - Đọc file TXT với nhiều encoding fallback
"""
from pathlib import Path
from typing import List
from core.logger import get_logger

logger = get_logger()

ENCODINGS = ["utf-8", "utf-8-sig", "cp949", "cp1258", "latin-1"]


def load_lines(file_path: str, skip_empty: bool = True) -> List[str]:
    """
    Đọc file txt và trả về danh sách các dòng.

    Args:
        file_path: Đường dẫn tới file .txt
        skip_empty: Nếu True thì bỏ qua dòng chỉ có whitespace

    Returns:
        Danh sách dòng theo thứ tự file

    Raises:
        FileNotFoundError: Nếu không tìm thấy file
        ValueError: Nếu không đọc được với bất kỳ encoding nào
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"Không tìm thấy file: {file_path}")
    if not path.is_file():
        raise ValueError(f"Không phải file hợp lệ: {file_path}")

    content = None
    used_encoding = None

    for enc in ENCODINGS:
        try:
            content = path.read_text(encoding=enc)
            used_encoding = enc
            logger.info("Đọc file thành công với encoding: %s", enc)
            break
        except (UnicodeDecodeError, LookupError) as e:
            logger.debug("Thử encoding %s thất bại: %s", enc, e)
            continue

    if content is None:
        raise ValueError(
            f"Không thể đọc file '{file_path}' với các encoding: {ENCODINGS}"
        )

    lines = content.splitlines()
    logger.info("Tổng số dòng thô trong file: %d", len(lines))

    if skip_empty:
        lines = [ln for ln in lines if ln.strip()]
        logger.info("Sau khi bỏ dòng trống: %d dòng", len(lines))

    return lines
