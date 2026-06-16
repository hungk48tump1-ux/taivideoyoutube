import argparse
import sys
import os
import re
import json
import torch
import numpy as np
import soundfile as sf
import traceback
import random

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

from omnivoice import OmniVoice, OmniVoiceGenerationConfig
from omnivoice.utils.common import get_best_device

SAVED_VOICES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "saved_voices")
SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")


def load_settings():
    """Đọc settings.json, trả về dict mặc định nếu không có."""
    defaults = {
        "num_step": 32, "guidance_scale": 2.0, "denoise": True,
        "speed": 1.0, "duration": 0, "preprocess": True,
        "postprocess": True, "seed": -1, "chunk_mode": "Không cắt",
        "chunk_words": 25, "num_threads": 1, "language": "Tự động"
    }
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            for k, v in defaults.items():
                data.setdefault(k, v)
            return data
    except Exception:
        return defaults


def resolve_ref_audio(ref_audio_arg):
    """Nếu ref_audio là tên giọng trong saved_voices, trả về đường dẫn wav. Nếu là đường dẫn file, giữ nguyên."""
    if os.path.exists(ref_audio_arg):
        return ref_audio_arg
    # Thử tìm trong saved_voices
    wav_path = os.path.join(SAVED_VOICES_DIR, f"{ref_audio_arg}.wav")
    if os.path.exists(wav_path):
        return wav_path
    return ref_audio_arg  # Trả về nguyên bản để báo lỗi ở bước sau


def resolve_ref_text(ref_audio_arg):
    """Nếu giọng đã lưu có file .txt cùng tên, dùng làm ref_text cho voice clone."""
    candidates = []
    if os.path.exists(ref_audio_arg):
        base, _ext = os.path.splitext(ref_audio_arg)
        candidates.append(base + ".txt")
    candidates.append(os.path.join(SAVED_VOICES_DIR, f"{ref_audio_arg}.txt"))

    for path in candidates:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return f.read().strip()
            except Exception:
                return ""
    return ""


def resolve_language(value):
    value = fix_mojibake_text(value)
    if value in (None, "", "Tự động", "Auto"):
        return None
    return value


def fix_mojibake_text(value):
    if not isinstance(value, str) or not any(ch in value for ch in ("Ã", "Â", "á»", "Ä")):
        return value
    for enc in ("cp1252", "latin-1"):
        try:
            fixed = value.encode(enc).decode("utf-8")
            if fixed and "�" not in fixed:
                return fixed
        except Exception:
            pass
    return value


def split_text_by_mode(text, mode, max_words=25):
    """Chia văn bản theo chế độ chunk."""
    mode = fix_mojibake_text(mode)
    if mode == "Cắt gộp thông minh":
        raw_chunks = re.split(r'(?<=[.!?])\s+|(?<=[。！？])\s*|(?<=[,،;])\s+|(?<=[、；])\s*|\n+', text)
        raw_chunks = [c.strip() for c in raw_chunks if c.strip()]
        merged = []
        current = ""
        for chunk in raw_chunks:
            if not current:
                current = chunk
            else:
                test_merge = current + " " + chunk
                if len(test_merge.split()) <= max_words and len(test_merge) <= max_words * 6:
                    current = test_merge
                else:
                    merged.append(current)
                    current = chunk
        if current:
            merged.append(current)
        return merged
    elif mode == "Theo dấu câu":
        parts = re.split(r'(?<=[.!?])\s+', text)
    elif mode == "Theo xuống dòng":
        parts = text.split('\n')
    else:
        return [text]
    return [p.strip() for p in parts if p.strip()]


def format_srt_time(seconds):
    hrs = int(seconds // 3600)
    mins = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int((seconds - int(seconds)) * 1000)
    return f"{hrs:02}:{mins:02}:{secs:02},{millis:03}"


def write_srt(output_wav, entries):
    if not entries:
        return None
    srt_path = os.path.splitext(output_wav)[0] + ".srt"
    with open(srt_path, "w", encoding="utf-8") as f:
        f.write("\n".join(entries))
    return srt_path


def main():
    parser = argparse.ArgumentParser(description="OmniVoice CLI cho tích hợp")
    parser.add_argument("--text_file", required=True, help="Đường dẫn đến file kịch bản txt")
    parser.add_argument("--ref_audio", required=True, help="Đường dẫn file giọng mẫu hoặc tên giọng trong saved_voices")
    parser.add_argument("--output", required=True, help="Đường dẫn file wav đầu ra")
    parser.add_argument("--model", default="k2-fsa/OmniVoice", help="Tên model")
    # Thông số nâng cao (nếu không truyền sẽ lấy từ settings.json)
    parser.add_argument("--num_step", type=int, default=None)
    parser.add_argument("--guidance_scale", type=float, default=None)
    parser.add_argument("--speed", type=float, default=None)
    parser.add_argument("--duration", type=float, default=None)
    parser.add_argument("--language", type=str, default=None)
    parser.add_argument("--denoise", type=str, default=None, help="true/false")
    parser.add_argument("--preprocess", type=str, default=None, help="true/false")
    parser.add_argument("--postprocess", type=str, default=None, help="true/false")
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--chunk_mode", type=str, default=None)
    parser.add_argument("--chunk_words", type=int, default=None)
    parser.add_argument("--num_threads", type=int, default=None, help="Batch size xử lý text dài, 1-20")

    args = parser.parse_args()

    # --- Đọc settings.json làm mặc định ---
    settings = load_settings()
    num_step = args.num_step if args.num_step is not None else int(settings["num_step"])
    guidance_scale = args.guidance_scale if args.guidance_scale is not None else float(settings["guidance_scale"])
    speed = args.speed if args.speed is not None else float(settings["speed"])
    duration = args.duration if args.duration is not None else float(settings.get("duration", 0))
    language = resolve_language(args.language if args.language is not None else settings.get("language", "Tự động"))
    denoise = (args.denoise.lower() == "true") if args.denoise is not None else bool(settings["denoise"])
    preprocess = (args.preprocess.lower() == "true") if args.preprocess is not None else bool(settings["preprocess"])
    postprocess = (args.postprocess.lower() == "true") if args.postprocess is not None else bool(settings["postprocess"])
    seed = args.seed if args.seed is not None else int(settings["seed"])
    chunk_mode = args.chunk_mode if args.chunk_mode is not None else settings["chunk_mode"]
    chunk_words = args.chunk_words if args.chunk_words is not None else int(settings.get("chunk_words", 25))
    num_threads = args.num_threads if args.num_threads is not None else int(settings.get("num_threads", 1))
    num_threads = max(1, min(20, int(num_threads)))

    # --- Đọc văn bản ---
    text = ""
    for enc in ['utf-8-sig', 'utf-8', 'utf-16', 'utf-16-le', 'cp1252', 'latin-1']:
        try:
            with open(args.text_file, 'r', encoding=enc) as f:
                text = f.read().strip()
            break
        except Exception:
            continue

    if not text:
        print(f"ERROR: Không thể đọc file text hoặc file trống.")
        sys.exit(1)

    # --- Resolve giọng mẫu ---
    ref_audio = resolve_ref_audio(args.ref_audio)
    if not os.path.exists(ref_audio):
        print(f"ERROR: Không tìm thấy file giọng mẫu: {args.ref_audio}")
        sys.exit(1)
    ref_text = resolve_ref_text(args.ref_audio)

    # --- Log thông số ---
    print(f"[*] Bắt đầu sinh âm thanh từ kịch bản...")
    print(f"[*] File text: {args.text_file}")
    print(f"[*] Voice mẫu: {ref_audio}")
    print(f"[*] Lưu tại: {args.output}")
    print(f"[*] Thông số: num_step={num_step}, guidance={guidance_scale}, speed={speed}, duration={duration}, language={language or 'auto'}, denoise={denoise}, seed={seed}, chunk={chunk_mode}, batch={num_threads}")

    device = get_best_device()
    print(f"[*] Sử dụng thiết bị: {device}")

    try:
        # --- Seed ---
        if seed != -1:
            torch.manual_seed(seed)
            if torch.cuda.is_available():
                torch.cuda.manual_seed_all(seed)
            random.seed(seed)
            np.random.seed(seed)

        gen_config = OmniVoiceGenerationConfig(
            num_step=num_step,
            guidance_scale=guidance_scale,
            denoise=denoise,
            preprocess_prompt=preprocess,
            postprocess_output=postprocess,
        )

        print(f"[*] Đang tải model vào GPU...")
        model = OmniVoice.from_pretrained(
            args.model,
            device_map=device,
            dtype=torch.float16,
            load_asr=True
        )

        # --- Chunk ---
        chunks = split_text_by_mode(text, chunk_mode, chunk_words)
        print(f"[*] Số phần văn bản: {len(chunks)}")

        all_audio = []
        srt_entries = []
        current_time = 0.0
        sr = None

        i = 0
        completed = 0
        initial_batch_size = min(num_threads, len(chunks))

        while i < len(chunks):
            current_batch_size = min(initial_batch_size, len(chunks) - i)
            success = False

            while current_batch_size > 0 and not success:
                batch_chunks = chunks[i:i + current_batch_size]
                print(f"[*] Đang tổng hợp lô {completed + 1}-{completed + len(batch_chunks)}/{len(chunks)} (batch={current_batch_size})...")

                kw = dict(
                    text=batch_chunks if len(batch_chunks) > 1 else batch_chunks[0],
                    language=language,
                    generation_config=gen_config,
                    ref_audio=ref_audio
                )
                if ref_text:
                    kw["ref_text"] = ref_text
                if speed != 1.0:
                    kw["speed"] = speed
                if duration and duration > 0:
                    kw["duration"] = duration

                try:
                    results = model.generate(**kw)
                    if not isinstance(results, list) or (len(batch_chunks) == 1 and results and not isinstance(results[0], (list, tuple, torch.Tensor, np.ndarray))):
                        results = [results]
                    if len(results) < len(batch_chunks):
                        results = results + [results[-1]] * (len(batch_chunks) - len(results))

                    sr = model.sampling_rate
                    for j, result in enumerate(results[:len(batch_chunks)]):
                        audio_np = result[0] if isinstance(result, (list, tuple)) else result
                        if isinstance(audio_np, torch.Tensor):
                            audio_np = audio_np.cpu().numpy()
                        if audio_np.ndim > 1:
                            audio_np = audio_np.squeeze()

                        all_audio.append(audio_np)
                        chunk_dur = len(audio_np) / sr if sr else 0
                        start_str = format_srt_time(current_time)
                        end_str = format_srt_time(current_time + chunk_dur)
                        srt_entries.append(f"{completed + j + 1}\n{start_str} --> {end_str}\n{batch_chunks[j]}\n")
                        current_time += chunk_dur

                    completed += len(batch_chunks)
                    i += len(batch_chunks)
                    success = True

                except RuntimeError as e:
                    err = str(e).lower()
                    if current_batch_size > 1 and ("out of memory" in err or "cuda" in err or "vram" in err):
                        print(f"[!] Batch {current_batch_size} lỗi VRAM/CUDA, giảm xuống {current_batch_size // 2}...")
                        if torch.cuda.is_available():
                            torch.cuda.empty_cache()
                        current_batch_size = max(1, current_batch_size // 2)
                        continue
                    raise

        # --- Nối audio ---
        final_audio = np.concatenate(all_audio) if all_audio else np.array([])
        dur = len(final_audio) / sr if sr else 0

        sf.write(args.output, final_audio, sr)
        srt_path = write_srt(args.output, srt_entries)
        print(f"[*] Thời lượng tổng: {dur:.2f}s")
        if srt_path:
            print(f"[*] Phụ đề SRT: {srt_path}")
        print(f"SUCCESS: {args.output}")

        # Giải phóng GPU
        import gc
        del model
        torch.cuda.empty_cache()
        gc.collect()

    except Exception as e:
        print(f"ERROR: Quá trình tổng hợp lỗi: {e}")
        traceback.print_exc()
        sys.exit(1)


if __name__ == '__main__':
    main()
