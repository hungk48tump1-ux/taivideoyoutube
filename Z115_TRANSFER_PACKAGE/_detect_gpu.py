"""
_detect_gpu.py - Phát hiện GPU và chọn PyTorch wheel phù hợp nhất
Output ra stdout: WHEEL|INDEX_URL
Ví dụ: cu128|https://download.pytorch.org/whl/cu128
        cpu|
"""
import subprocess
import re
import sys


def _run(cmd, timeout=10):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception:
        return ""


def get_compute_capability():
    """Lấy compute capability của GPU đầu tiên (VD: 8.6, 12.0)."""
    out = _run(["nvidia-smi", "--query-gpu=compute_cap", "--format=csv,noheader"])
    if not out:
        return None
    try:
        first_line = out.split("\n")[0].strip()
        return float(first_line)
    except Exception:
        return None


def get_cuda_driver_version():
    """Lấy CUDA version từ driver nvidia-smi (VD: 12.4 → (12, 4))."""
    out = _run(["nvidia-smi"])
    m = re.search(r"CUDA Version[:\s]+(\d+)\.(\d+)", out)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None, None


def get_gpu_name():
    out = _run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"])
    return out.split("\n")[0].strip() if out else "Unknown"


def select_wheel(cc, cuda_major, cuda_minor):
    """
    Map (compute capability, cuda driver) → PyTorch wheel.

    Bảng tham chiếu:
      CC >= 12.0  → Blackwell (RTX 5000+)        → cu128
      CC >= 8.9   → Ada Lovelace (RTX 4000)      → cu121
      CC >= 8.0   → Ampere (RTX 3000)             → cu121 (hoặc cu118 nếu driver CUDA 11)
      CC >= 7.5   → Turing (RTX 2000 / GTX 1600) → cu118
      CC >= 7.0   → Volta (V100)                  → cu118
      CC <  7.0   → Quá cũ, không hỗ trợ         → cpu
    """
    if cc is None or cuda_major is None:
        return "cpu", ""

    if cc >= 12.0:
        # Blackwell - bắt buộc cu128
        return "cu128", "https://download.pytorch.org/whl/cu128"

    if cc >= 8.0:
        # Ampere (RTX 3000) / Ada (RTX 4000)
        if cuda_major >= 12:
            return "cu121", "https://download.pytorch.org/whl/cu121"
        else:
            # Driver CUDA 11.x
            return "cu118", "https://download.pytorch.org/whl/cu118"

    if cc >= 7.0:
        # Turing / Volta
        if cuda_major >= 12:
            return "cu121", "https://download.pytorch.org/whl/cu121"
        else:
            return "cu118", "https://download.pytorch.org/whl/cu118"

    # CC < 7.0 → Pascal trở xuống (GTX 10 series cũ...)
    return "cpu", ""


def main():
    cc = get_compute_capability()
    cuda_major, cuda_minor = get_cuda_driver_version()
    gpu_name = get_gpu_name()

    if cc is None:
        # Không có GPU NVIDIA
        print("cpu ")
        print(f"[INFO] Không phát hiện GPU NVIDIA → sẽ dùng CPU", file=sys.stderr)
        return

    wheel, url = select_wheel(cc, cuda_major, cuda_minor)

    cuda_str = f"{cuda_major}.{cuda_minor}" if cuda_major else "?"
    print(f"{wheel} {url}")
    print(
        f"[INFO] GPU: {gpu_name} | CC: {cc} | CUDA driver: {cuda_str} -> Wheel: {wheel}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
