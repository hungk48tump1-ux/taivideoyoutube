# transcribe_whisper.py
from __future__ import annotations

import os
import re
from typing import Callable, List, Optional, Tuple

ProgressCB = Optional[Callable[[int, int, str], None]]


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
    os.makedirs(os.path.dirname(txt_path), exist_ok=True)
    with open(txt_path, "w", encoding="utf-8") as f:
        for t in texts:
            t = (t or "").strip()
            if not t:
                continue
            f.write(t + "\n\n")  # moi doan cach 1 dong trong


def _transcribe_openai_whisper(
    files: List[str],
    model_name: str,
    *,
    language: Optional[str],
    beam_size: int,
    progress_cb: ProgressCB,
) -> List[str]:
    import whisper
    import torch

    # bat buoc cuda neu co; neu muon cho phep CPU thi doi thanh: device = "cuda" if ... else "cpu"
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model = whisper.load_model(model_name, device=device)

    texts: List[str] = []
    total = len(files)

    with torch.inference_mode():
        for i, file in enumerate(files, start=1):
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

            text = (result.get("text") or "").strip()
            text = _collapse_repeats(text)
            texts.append(text)

            if progress_cb:
                progress_cb(i, total, f"Nhan dang giong noi | openai-whisper | {device}")

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
    from faster_whisper import WhisperModel

    # uu tien GPU; neu fail thi CPU int8
    try:
        model = WhisperModel(model_name, device="cuda", compute_type="float16")
        device_used = "cuda"
    except Exception:
        model = WhisperModel(model_name, device="cpu", compute_type="int8")
        device_used = "cpu"

    texts: List[str] = []
    total = len(files)

    for i, file in enumerate(files, start=1):
        segments, _info = model.transcribe(
            file,
            language=language or None,
            beam_size=max(1, int(beam_size or 1)),
            vad_filter=bool(vad_filter),

            # QUAN TRONG: giam lap cau
            condition_on_previous_text=False,
        )
        text = "".join(seg.text for seg in segments).strip()
        text = _collapse_repeats(text)
        texts.append(text)

        if progress_cb:
            progress_cb(i, total, f"Nhan dang giong noi | faster-whisper | {device_used}")

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
    else:  # auto
        try:
            texts = _transcribe_faster_whisper(
                files, model_name, language=language, beam_size=beam_size, vad_filter=vad_filter, progress_cb=progress_cb
            )
        except Exception:
            texts = _transcribe_openai_whisper(
                files, model_name, language=language, beam_size=beam_size, progress_cb=progress_cb
            )

    _write_out(texts, txt_path)
    return txt_path
