"""
core/chunker.py - Chia danh sách dòng thành các chunk
"""
from dataclasses import dataclass
from typing import List
from core.logger import get_logger

logger = get_logger()


@dataclass
class Chunk:
    """Đại diện cho một chunk dữ liệu cần gửi."""
    index: int          # 1-indexed
    lines: List[str]
    start_line: int     # 1-indexed, vị trí trong danh sách dòng hợp lệ
    end_line: int       # 1-indexed, inclusive

    @property
    def content(self) -> str:
        """Nội dung đầy đủ của chunk, các dòng cách nhau bởi 1 dòng trống (blank line)."""
        return "\n\n".join(self.lines)

    @property
    def label(self) -> str:
        """Nhãn hiển thị, ví dụ: 'chunk_001'."""
        return f"chunk_{self.index:03d}"

    @property
    def line_count(self) -> int:
        return len(self.lines)


def create_chunks(
    lines: List[str],
    first_size: int = 30,
    next_size: int = 50
) -> List[Chunk]:
    """
    Chia danh sách dòng thành các chunk theo quy tắc:
    - Chunk 1: first_size dòng đầu
    - Chunk 2+: mỗi chunk next_size dòng

    Args:
        lines: Danh sách dòng (đã lọc sạch)
        first_size: Số dòng chunk đầu tiên
        next_size: Số dòng các chunk tiếp theo

    Returns:
        Danh sách Chunk, đảm bảo không cắt giữa dòng
    """
    if not lines:
        logger.warning("Danh sách dòng rỗng, không có chunk nào được tạo.")
        return []

    chunks: List[Chunk] = []
    pos = 0
    total = len(lines)

    # Chunk đầu tiên
    end = min(pos + first_size, total)
    chunks.append(Chunk(
        index=1,
        lines=lines[pos:end],
        start_line=pos + 1,
        end_line=end
    ))
    pos = end

    # Các chunk tiếp theo
    chunk_idx = 2
    while pos < total:
        end = min(pos + next_size, total)
        chunks.append(Chunk(
            index=chunk_idx,
            lines=lines[pos:end],
            start_line=pos + 1,
            end_line=end
        ))
        pos = end
        chunk_idx += 1

    logger.info(
        "Đã tạo %d chunk từ %d dòng (first=%d, next=%d)",
        len(chunks), total, first_size, next_size
    )
    for c in chunks:
        logger.debug("  %s: dòng %d–%d (%d dòng)", c.label, c.start_line, c.end_line, c.line_count)

    return chunks
