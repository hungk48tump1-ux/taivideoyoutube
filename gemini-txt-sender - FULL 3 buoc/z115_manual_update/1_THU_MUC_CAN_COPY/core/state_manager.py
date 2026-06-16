"""
core/state_manager.py - Quản lý trạng thái phiên làm việc, hỗ trợ resume
"""
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from core.logger import get_logger

logger = get_logger()

SESSION_FILE = "session_state.json"


class SessionState:
    """
    Lưu trữ và phục hồi trạng thái phiên làm việc.
    Hỗ trợ tiếp tục từ chunk chưa hoàn thành khi app bị tắt.
    """

    def __init__(self, output_dir: Path):
        self.output_dir = output_dir
        self.session_file = output_dir / SESSION_FILE
        self.data: Dict[str, Any] = {}

    def init_session(
        self,
        source_file: str,
        total_lines: int,
        total_chunks: int,
        config: Dict[str, Any]
    ) -> None:
        """Khởi tạo session mới."""
        self.data = {
            "session_id": datetime.now().strftime("%Y%m%d_%H%M%S"),
            "source_file": source_file,
            "total_lines": total_lines,
            "total_chunks": total_chunks,
            "current_chunk_index": 0,   # 0 = chưa bắt đầu
            "completed_chunks": [],
            "failed_chunks": [],
            "status": "initialized",
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
            "config_snapshot": config,
        }
        self._save()
        logger.info("Session mới khởi tạo: %s", self.data["session_id"])

    def load(self) -> bool:
        """
        Tải session từ file nếu tồn tại.
        Returns: True nếu load thành công, False nếu không có file.
        """
        if not self.session_file.exists():
            return False
        try:
            raw = self.session_file.read_text(encoding="utf-8")
            self.data = json.loads(raw)
            logger.info("Đã load session: %s", self.data.get("session_id", "unknown"))
            return True
        except Exception as e:
            logger.error("Không thể load session file: %s", e)
            return False

    def mark_chunk_started(self, chunk_index: int) -> None:
        """Đánh dấu chunk đang được xử lý."""
        self.data["current_chunk_index"] = chunk_index
        self.data["status"] = "running"
        self.data["updated_at"] = datetime.now().isoformat()
        self._save()

    def mark_chunk_completed(self, chunk_index: int) -> None:
        """Đánh dấu chunk đã hoàn thành."""
        if chunk_index not in self.data["completed_chunks"]:
            self.data["completed_chunks"].append(chunk_index)
        self.data["updated_at"] = datetime.now().isoformat()
        self._save()
        logger.debug("Chunk %d đánh dấu hoàn thành.", chunk_index)

    def mark_chunk_failed(self, chunk_index: int, reason: str = "") -> None:
        """Đánh dấu chunk thất bại."""
        entry = {"index": chunk_index, "reason": reason, "time": datetime.now().isoformat()}
        self.data["failed_chunks"].append(entry)
        self.data["updated_at"] = datetime.now().isoformat()
        self._save()
        logger.warning("Chunk %d thất bại: %s", chunk_index, reason)

    def mark_session_done(self) -> None:
        self.data["status"] = "completed"
        self.data["updated_at"] = datetime.now().isoformat()
        self._save()
        logger.info("Session hoàn tất.")

    def mark_session_paused(self) -> None:
        self.data["status"] = "paused"
        self.data["updated_at"] = datetime.now().isoformat()
        self._save()

    def mark_session_stopped(self) -> None:
        self.data["status"] = "stopped"
        self.data["updated_at"] = datetime.now().isoformat()
        self._save()

    def is_chunk_completed(self, chunk_index: int) -> bool:
        return chunk_index in self.data.get("completed_chunks", [])

    def get_resume_chunk_index(self) -> Optional[int]:
        """
        Trả về chunk index để resume (chunk đầu tiên chưa completed).
        None nếu tất cả đã xong.
        """
        completed = set(self.data.get("completed_chunks", []))
        total = self.data.get("total_chunks", 0)
        for i in range(1, total + 1):
            if i not in completed:
                return i
        return None

    def get_summary(self) -> Dict[str, Any]:
        return {
            "session_id": self.data.get("session_id"),
            "source_file": self.data.get("source_file"),
            "status": self.data.get("status"),
            "total_chunks": self.data.get("total_chunks"),
            "completed": len(self.data.get("completed_chunks", [])),
            "failed": len(self.data.get("failed_chunks", [])),
            "current_chunk": self.data.get("current_chunk_index"),
        }

    def _save(self) -> None:
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            self.session_file.write_text(
                json.dumps(self.data, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
        except Exception as e:
            logger.error("Không thể lưu session state: %s", e)


class OutputManager:
    """
    Quản lý lưu trữ prompt/response/meta cho mỗi chunk.
    Tạo thư mục output theo timestamp.
    """

    def __init__(self, base_dir: str = "outputs", input_file_path: Optional[str] = None):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.session_dir = Path(base_dir) / f"session_{timestamp}"
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.input_file_path = input_file_path
        # Timestamp dùng cho tên file output: DDMM_HHmm (ví dụ: 1704_2142)
        self.file_timestamp = datetime.now().strftime("%d%m_%H%M")
        logger.info("Output directory: %s", self.session_dir)

    @classmethod
    def from_existing(cls, session_dir: str) -> "OutputManager":
        """Tạo OutputManager từ thư mục session đã có sẵn (resume mode)."""
        obj = cls.__new__(cls)
        obj.session_dir = Path(session_dir)
        obj.session_dir.mkdir(parents=True, exist_ok=True)
        return obj

    def save_chunk(
        self,
        chunk_index: int,
        prompt: str,
        response: str,
        code_blocks: List[str],
        meta: Dict[str, Any]
    ) -> None:
        """Lưu prompt, response, code blocks và metadata của một chunk."""
        prefix = f"{chunk_index:03d}"

        (self.session_dir / f"{prefix}_prompt.txt").write_text(
            prompt, encoding="utf-8"
        )
        (self.session_dir / f"{prefix}_response.txt").write_text(
            response, encoding="utf-8"
        )
        (self.session_dir / f"{prefix}_meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
        
        # THAY ĐỔI: Không gọi _extract_and_append_code ở đây nữa, 
        # mà sẽ đợi đến cuối session (build_final_output) mới gọi.
        
        logger.info("Đã lưu chunk %d vào: %s", chunk_index, self.session_dir)

    def _is_video_prompt_block(self, block: str) -> bool:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if not lines:
            return False
        video_lines = sum(1 for line in lines if re.match(r"^\[\d+\]\s*VEO\d*\s*:", line, re.IGNORECASE))
        return video_lines >= max(1, len(lines) // 2)

    def _extract_and_append_code(self, blocks: List[str], chunk_index: int) -> None:
        """Xuất các code block nhận được ra 3 file theo yêu cầu."""
        if not blocks:
            logger.debug("Không tìm thấy code block nào trong chunk %d", chunk_index)
            return

        if self.input_file_path:
            input_path = Path(self.input_file_path)
            out_dir = input_path.parent
            # Tên thư mục chứa input
            folder_name = out_dir.name
            ts = self.file_timestamp   # DDMM_HHmm, nhất quán cho cả session

            file1_path = out_dir / f"30whisk_{folder_name}_{ts}.txt"
            file2_path = out_dir / f"30VEO3_{folder_name}_{ts}.txt"
            file3_path = out_dir / f"luot2_tro_di_{folder_name}_{ts}.txt"

            if chunk_index == 1:
                # Chunk 1: 2 code blocks — block[0]=WHISK → file1, block[1]=VEO3 → file2
                if len(blocks) >= 1:
                    with open(file1_path, "a", encoding="utf-8") as f:
                        f.write(blocks[0].strip() + "\n\n")
                if len(blocks) >= 2:
                    with open(file2_path, "a", encoding="utf-8") as f:
                        f.write(blocks[1].strip() + "\n\n")
            else:
                # Chunk 2+: Chỉ lấy duy nhất block đầu tiên (sau khi lọc bỏ video)
                image_blocks = [blk for blk in blocks if not self._is_video_prompt_block(blk)]
                if image_blocks:
                    with open(file3_path, "a", encoding="utf-8") as f:
                        f.write(image_blocks[0].strip() + "\n\n")
            
            logger.info("Trích xuất được %d code block(s) từ chunk %d vào thư mục gốc.", len(blocks), chunk_index)
        else:
            # Fallback nếu không có input_file_path
            extracted_file = self.session_dir / "all_extracted_results.txt"
            with open(extracted_file, "a", encoding="utf-8") as f:
                for idx, block in enumerate(blocks, 1):
                    f.write(block.strip() + "\n\n")
            logger.info("Trích xuất được %d code block(s) từ chunk %d", len(blocks), chunk_index)

    def build_final_output(self) -> None:
        """Được gọi 1 lần khi toàn bộ quá trình gửi gửi chunk thành công (hoặc thủ công), để sinh ra 3 file TXT."""
        # Lấy danh sách tất cả file *_meta.json
        meta_files = sorted(list(self.session_dir.glob("*_meta.json")))
        if not meta_files:
            logger.info("Không có file meta nào để trích xuất.")
            return

        # Xóa các file cũ nếu có (tránh ghi đè trùng lặp khi chạy lại logic này)
        if self.input_file_path:
            input_path = Path(self.input_file_path)
            out_dir = input_path.parent
            folder_name = out_dir.name
            ts = self.file_timestamp
            
            for file_name in [f"30whisk_{folder_name}_{ts}.txt", f"30VEO3_{folder_name}_{ts}.txt", f"luot2_tro_di_{folder_name}_{ts}.txt"]:
                fpath = out_dir / file_name
                if fpath.exists():
                    try:
                        fpath.unlink()
                    except:
                        pass
        else:
            extracted_file = self.session_dir / "all_extracted_results.txt"
            if extracted_file.exists():
                try:
                    extracted_file.unlink()
                except:
                    pass

        # Gộp tất cả các code_blocks vào file.
        for meta_file in meta_files:
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
                blocks = meta.get("code_blocks", [])
                chunk_index = meta.get("chunk_index")
                if chunk_index is not None and blocks:
                    self._extract_and_append_code(blocks, chunk_index)
            except Exception as e:
                logger.error("Lỗi khi đọc file meta %s: %s", meta_file, e)
                
        logger.info("Đã hoàn tất trích xuất code block vào các file đầu ra!")

    def get_session_dir(self) -> Path:
        return self.session_dir

    def has_chunk(self, chunk_index: int) -> bool:
        """Kiểm tra xem chunk đã được lưu chưa."""
        prefix = f"{chunk_index:03d}"
        return (self.session_dir / f"{prefix}_meta.json").exists()
