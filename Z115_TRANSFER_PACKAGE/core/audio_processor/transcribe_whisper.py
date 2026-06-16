# transcribe_whisper.py
from __future__ import annotations

import io
import os
import re
import sys
from typing import Callable, List, Optional, Tuple

ProgressCB = Optional[Callable[[int, int, str], None]]
EMPTY_TRANSCRIPT_MARKER = "[WHISPER_EMPTY]"


def _patch_stdio() -> None:
    """
    Khi chạy bằng pythonw, sys.stdout và sys.stderr là None.
    Whisper / tqdm nội bộ gọi .write() trên đó → 'NoneType has no attribute write'.
    Hàm này redirect về null stream để tránh crash.
    """
    if sys.stdout is None:
        sys.stdout = io.TextIOWrapper(open(os.devnull, "wb"), errors="replace")
    if sys.stderr is None:
        sys.stderr = io.TextIOWrapper(open(os.devnull, "wb"), errors="replace")


def _collapse_repeats(s: str) -> str:
    """
    Chong lap cau dai (kieu: 'AGI... AGI... AGI...').
    Gom cac cum lap lien tiep >= 3 lan -> giu 1 lan.
    """
    s = (s or "").strip()
    if not s:
        return s
    # Lap cum tu 4-60 ky tu, lap lien tiep >=3 lan
    s = re.sub(r"(.{4,60})(?:\s+\1){2,}", r"\1", s)
    # Gon nhieu khoang trang
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _write_out(texts: List[str], txt_path: str) -> None:
    txt_path = os.path.abspath(txt_path)
    parent = os.path.dirname(txt_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(txt_path, "w", encoding="utf-8") as f:
        for t in texts:
            t = re.sub(r"\s+", " ", (t or "").strip())
            if not t:
                t = EMPTY_TRANSCRIPT_MARKER
            f.write(t + "\n\n")


def _transcribe_openai_whisper(
    files: List[str],
    model_name: str,
    *,
    language: Optional[str],
    beam_size: int,
    progress_cb: ProgressCB,
) -> List[str]:
    _patch_stdio()  # fix pythonw: sys.stdout/stderr = None
    import gc
    import whisper
    import torch

    # Thử CUDA trước, fallback CPU nếu CUDA không tương thích (kernel mismatch)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    try:
        model = whisper.load_model(model_name, device=device)
    except Exception as cuda_err:
        if device == "cuda":
            import sys
            print(f"[WARNING] CUDA lỗi khi load Whisper ({cuda_err}). Fallback sang CPU...", file=sys.stderr)
            device = "cpu"
            model = whisper.load_model(model_name, device="cpu")
        else:
            raise

    texts: List[str] = []
    total = len(files)

    try:
        with torch.inference_mode():
            for i, file in enumerate(files, start=1):
                try:
                    result = model.transcribe(
                        file,
                        task="transcribe",
                        language=language or None,
                        fp16=(device == "cuda"),
                        temperature=0.0,
                        beam_size=max(1, int(beam_size or 1)),
                        best_of=1,
                        verbose=False,

                        # QUAN TRONG: giam lap cau
                        condition_on_previous_text=False,
                    )
                    text = (result.get("text") if isinstance(result, dict) else "") or ""
                    text = text.strip()
                except Exception as e:
                    text = ""
                text = _collapse_repeats(text)
                texts.append(text)

                if progress_cb:
                    progress_cb(i, total, f"Nhan dang giong noi | openai-whisper | {device}")
    finally:
        # ── Giải phóng VRAM sau khi dịch xong ──
        del model
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            torch.cuda.synchronize()

    return texts


def _transcribe_faster_whisper(
    files: List[str],
    model_name: str,
    *,
    language: Optional[str],
    beam_size: int,
    vad_filter: bool,
    progress_cb: ProgressCB,
) -> List[str]:
    _patch_stdio()  # fix pythonw: sys.stdout/stderr = None
    import gc
    from faster_whisper import WhisperModel

    # uu tien GPU; neu fail thi CPU int8
    try:
        model = WhisperModel(model_name, device="cuda", compute_type="float16")
        device_used = "cuda"
    except Exception:
        try:
            model = WhisperModel(model_name, device="cuda", compute_type="int8_float16")
            device_used = "cuda"
        except Exception:
            try:
                model = WhisperModel(model_name, device="cuda", compute_type="int8")
                device_used = "cuda"
            except Exception:
                model = WhisperModel(model_name, device="cpu", compute_type="int8")
                device_used = "cpu"

    texts: List[str] = []
    total = len(files)

    try:
        for i, file in enumerate(files, start=1):
            try:
                segments, _info = model.transcribe(
                    file,
                    language=language or None,
                    beam_size=max(1, int(beam_size or 1)),
                    vad_filter=bool(vad_filter),

                    # QUAN TRONG: giam lap cau
                    condition_on_previous_text=False,
                )
                text = "".join(seg.text for seg in (segments or [])).strip()
            except Exception as e:
                text = ""
            text = _collapse_repeats(text)
            texts.append(text)

            if progress_cb:
                progress_cb(i, total, f"Nhan dang giong noi | faster-whisper | {device_used}")
    finally:
        # ── Giải phóng VRAM sau khi dịch xong ──
        del model
        gc.collect()
        if device_used == "cuda":
            try:
                import torch
                torch.cuda.empty_cache()
                torch.cuda.synchronize()
            except Exception:
                pass

    return texts


def transcribe_audio(
    files: List[str],
    output_dir: str,
    base_name: str,
    time_str: str,
    model_name: str,
    progress_cb: ProgressCB = None,
    *,
    engine: str = "openai-whisper",  # openai-whisper | faster-whisper | auto
    language: Optional[str] = None,
    beam_size: int = 3,
    vad_filter: bool = True,
) -> str:
    os.makedirs(output_dir, exist_ok=True)
    txt_path = os.path.join(output_dir, f"{base_name}_{time_str}.txt")

    engine = (engine or "auto").strip().lower()

    if engine == "faster-whisper":
        texts = _transcribe_faster_whisper(
            files, model_name, language=language, beam_size=beam_size, vad_filter=vad_filter, progress_cb=progress_cb
        )
    elif engine == "openai-whisper":
        texts = _transcribe_openai_whisper(
            files, model_name, language=language, beam_size=beam_size, progress_cb=progress_cb
        )
    else:  # auto - thử faster-whisper trước, fallback openai-whisper
        try:
            import faster_whisper  # noqa: kiểm tra cài chưa
            texts = _transcribe_faster_whisper(
                files, model_name, language=language, beam_size=beam_size, vad_filter=vad_filter, progress_cb=progress_cb
            )
        except ModuleNotFoundError:
            import sys
            print("[AUTO] faster-whisper chưa được cài, dùng openai-whisper với CUDA...", file=sys.stderr)
            texts = _transcribe_openai_whisper(
                files, model_name, language=language, beam_size=beam_size, progress_cb=progress_cb
            )
        except Exception:
            texts = _transcribe_openai_whisper(
                files, model_name, language=language, beam_size=beam_size, progress_cb=progress_cb
            )

    _write_out(texts, txt_path)
    return txt_path
