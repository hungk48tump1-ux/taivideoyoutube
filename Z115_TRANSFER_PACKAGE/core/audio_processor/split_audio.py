from __future__ import annotations

import os
import math
import subprocess
import sys
from datetime import datetime
from typing import Callable, List, Tuple, Optional

# Ẩn cửa sổ CMD trên Windows khi dùng pythonw / background mode
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

ProgressCB = Optional[Callable[[int, int, str], None]]

# 4 phút đầu -> tùy chọn 8 hoặc 10 giây/đoạn
# phần còn lại -> 10 giây/đoạn
_PHASE1_LIMIT_MS = 4 * 60 * 1000   # 240,000 ms
_PHASE2_CHUNK_MS = 10 * 1000       # 10,000 ms


def _run_bytes(cmd: List[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
        shell=False,
        creationflags=_NO_WINDOW,
    )


def _decode_bytes(b: bytes) -> str:
    if b is None:
        return ""
    try:
        return b.decode("utf-8", errors="replace").strip()
    except Exception:
        try:
            return b.decode(errors="replace").strip()
        except Exception:
            return ""


def _get_duration_ms_from_ffprobe(input_file: str) -> int:
    """
    Lấy duration từ ffprobe dưới dạng text thuần, không dùng JSON.
    Cách này ổn định hơn trên Windows.
    """
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=nokey=1:noprint_wrappers=1",
        input_file,
    ]

    try:
        cp = _run_bytes(cmd)
    except FileNotFoundError:
        raise RuntimeError("Khong tim thay ffprobe. Hay kiem tra ffmpeg/ffprobe da cai va da them vao PATH.")
    except subprocess.CalledProcessError as e:
        err = _decode_bytes(e.stderr)
        raise RuntimeError(f"ffprobe loi: {err or 'khong doc duoc duration'}")

    out = _decode_bytes(cp.stdout)
    if not out:
        err = _decode_bytes(cp.stderr)
        raise RuntimeError(f"ffprobe khong tra ve duration. stderr: {err or '(rong)'}")

    # ffprobe có thể trả về nhiều dòng, lấy dòng đầu tiên có số
    dur_sec = None
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            dur_sec = float(line)
            break
        except ValueError:
            continue

    if dur_sec is None:
        raise RuntimeError(f"Khong parse duoc duration tu ffprobe: {out}")

    return int(math.ceil(dur_sec * 1000))


def _build_boundaries(total_ms: int, phase1_chunk_seconds: int = 8) -> List[Tuple[int, int]]:
    """
    Tạo mốc cắt:
      - 4 phút đầu: phase1_chunk_seconds giây/đoạn
      - sau đó: 10 giây/đoạn
    """
    boundaries: List[Tuple[int, int]] = []
    pos = 0
    phase1_chunk_ms = max(1, int(phase1_chunk_seconds)) * 1000

    phase1_end = min(total_ms, _PHASE1_LIMIT_MS)
    while pos < phase1_end:
        end = min(pos + phase1_chunk_ms, phase1_end, total_ms)
        boundaries.append((pos, end))
        pos = end

    while pos < total_ms:
        end = min(pos + _PHASE2_CHUNK_MS, total_ms)
        boundaries.append((pos, end))
        pos = end

    if boundaries and boundaries[-1][1] < total_ms:
        boundaries.append((boundaries[-1][1], total_ms))

    return boundaries


def _fmt_sec(ms: int) -> str:
    return f"{ms / 1000.0:.6f}"


def _extract_chunk_ffmpeg(
    input_file: str,
    output_file: str,
    start_ms: int,
    end_ms: int,
    *,
    target_sr: int = 16000,
    mono: bool = True,
) -> None:
    """
    Cắt trực tiếp từ file gốc bằng ffmpeg.
    Không đệm silence.
    """
    dur_ms = max(0, end_ms - start_ms)
    if dur_ms <= 0:
        raise RuntimeError("Chunk duration <= 0")

    cmd = [
        "ffmpeg",
        "-y",
        "-v", "error",
        "-nostdin",
        "-ss", _fmt_sec(start_ms),
        "-i", input_file,
        "-t", _fmt_sec(dur_ms),
        "-map", "0:a:0",
        "-vn",
        "-sn",
        "-dn",
    ]

    if mono:
        cmd += ["-ac", "1"]
    if target_sr:
        cmd += ["-ar", str(int(target_sr))]

    cmd += [
        "-c:a", "pcm_s16le",
        output_file,
    ]

    try:
        _run_bytes(cmd)
    except FileNotFoundError:
        raise RuntimeError("Khong tim thay ffmpeg. Hay kiem tra ffmpeg da cai va da them vao PATH.")
    except subprocess.CalledProcessError as e:
        err = _decode_bytes(e.stderr)
        raise RuntimeError(f"ffmpeg cat chunk loi: {err or output_file}")


def split_audio(
    input_file: str,
    base_chunk_dir: str,
    chunk_seconds: int,          # giữ để tương thích app.py, không dùng
    progress_cb: ProgressCB = None,
    *,
    target_sr: int = 16000,
    mono: bool = True,
    phase1_chunk_seconds: int = 8,
) -> Tuple[List[str], str, str]:
    """
    Chia audio theo metadata duration lấy từ ffprobe,
    nhưng cắt trực tiếp từ file gốc bằng ffmpeg.

    Kết quả:
      - 4 phút đầu dùng phase1_chunk_seconds giây, phần còn lại 10 giây
      - không đệm silence giả
      - đoạn cuối lấy đúng từ file gốc
    """
    total_ms = _get_duration_ms_from_ffprobe(input_file)

    base_name = os.path.splitext(os.path.basename(input_file))[0]
    time_str = datetime.now().strftime("%Y%m%d_%H%M%S")

    chunk_dir = os.path.join(base_chunk_dir, f"{base_name}_{time_str}")
    os.makedirs(chunk_dir, exist_ok=True)

    boundaries = _build_boundaries(total_ms, phase1_chunk_seconds=phase1_chunk_seconds)
    total = len(boundaries)
    paths: List[str] = []

    print("=" * 60)
    print("SPLIT AUDIO DEBUG (FFPROBE TEXT + FFMPEG DIRECT CUT)")
    print(f"input_file : {input_file}")
    print(f"total_ms   : {total_ms}")
    print(f"total_sec  : {total_ms / 1000:.3f}")
    print(f"phase1_sec : {phase1_chunk_seconds}")
    print(f"chunks     : {total}")
    print("=" * 60)

    for i, (start, end) in enumerate(boundaries, start=1):
        fname = f"{base_name}_{i:03d}.wav"
        path = os.path.join(chunk_dir, fname)

        _extract_chunk_ffmpeg(
            input_file,
            path,
            start,
            end,
            target_sr=target_sr,
            mono=mono,
        )
        paths.append(path)

        if progress_cb:
            progress_cb(i, total, "Chia audio")

    return paths, base_name, time_str
