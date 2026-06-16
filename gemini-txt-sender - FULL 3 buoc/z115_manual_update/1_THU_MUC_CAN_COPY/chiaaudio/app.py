# app.py
from __future__ import annotations

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import threading
import os
import time

from split_audio import split_audio
from transcribe_whisper import transcribe_audio

overall_start_ts = None
stage_start_ts = None
last_stage = None


def _fmt_time(sec: float) -> str:
    if sec is None:
        return "--:--"
    sec = max(0, int(sec))
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _require_int(var: tk.StringVar, field_name: str) -> int:
    s = str(var.get()).strip()
    if not s:
        raise ValueError(f"Ban chua nhap {field_name}.")
    v = int(s)
    if v <= 0:
        raise ValueError(f"{field_name} phai > 0.")
    return v


def choose_file():
    path = filedialog.askopenfilename(
        title="Chon file audio",
        filetypes=[("Audio", "*.mp3 *.wav *.m4a *.aac *.flac *.ogg")],
    )
    if path:
        audio_path.set(path)


def update_progress(done: int, total: int, stage: str):
    global overall_start_ts, stage_start_ts, last_stage

    now = time.time()

    if overall_start_ts is None:
        overall_start_ts = now

    if last_stage != stage:
        last_stage = stage
        stage_start_ts = now

    total = max(1, int(total))
    done = max(0, int(done))
    percent = int(min(100, done * 100 / total))
    progress["value"] = percent

    elapsed_total = now - overall_start_ts if overall_start_ts else None
    elapsed_stage = now - stage_start_ts if stage_start_ts else None
    eta_stage = (elapsed_stage * (total - done) / done) if (done > 0 and elapsed_stage is not None) else None

    status.set(
        f"{stage}: {percent}% | Da chay: {_fmt_time(elapsed_total)} | Con lai (uoc tinh): {_fmt_time(eta_stage)}"
    )
    root.update_idletasks()


def run_process():
    global overall_start_ts, stage_start_ts, last_stage

    try:
        audio = audio_path.get().strip()
        if not audio or not os.path.exists(audio):
            messagebox.showerror("Loi", "Chua chon file audio hop le")
            return

        overall_start_ts = time.time()
        stage_start_ts = overall_start_ts
        last_stage = None

        chunk_sec = 10  # khong dung, split_audio tu dong chia 2 giai doan (8s/10s)

        engine = (engine_var.get().strip().lower() or "openai-whisper")
        model = (model_var.get().strip() or "large-v2")

        # Lay ma ngon ngu tu lua chon
        lang_display = language_var.get().strip()
        lang_map = {
            "Tieng Han (ko)": "ko",
            "Tieng Nhat (ja)": "ja",
            "Tieng Anh (en)": "en",
            "Auto (de trong)": None,
        }
        lang = lang_map.get(lang_display, None)

        beam = int(str(beam_size.get()).strip() or "1")
        if beam <= 0:
            beam = 1

        # output nam cung thu muc voi file audio dau vao
        audio_dir = os.path.dirname(os.path.abspath(audio))
        folder_name = os.path.basename(audio_dir)
        os.makedirs("chunks", exist_ok=True)

        progress["value"] = 0
        status.set("Bat dau")

        chunks, base_name, time_str = split_audio(
            audio,
            "chunks",
            chunk_sec,
            update_progress,
            target_sr=16000,
            mono=True,
        )

        txt_path = transcribe_audio(
            chunks,
            audio_dir,
            folder_name,
            time_str,
            model,
            update_progress,
            engine=engine,
            language=lang,
            beam_size=beam,
            vad_filter=True,
        )

        total_time = time.time() - overall_start_ts if overall_start_ts else 0
        status.set(f"Hoan thanh | Tong thoi gian: {_fmt_time(total_time)}")
        messagebox.showinfo("Xong", "Da tao file:\n" + txt_path + f"\n\nTong thoi gian: {_fmt_time(total_time)}")

    except Exception as e:
        messagebox.showerror("Loi", str(e))


def start():
    threading.Thread(target=run_process, daemon=True).start()


root = tk.Tk()
root.title("Audio -> Text (Whisper)")
root.geometry("760x520")

audio_path = tk.StringVar()
model_var = tk.StringVar(value="large-v3")         # mac dinh large-v3
engine_var = tk.StringVar(value="openai-whisper")  # mac dinh openai-whisper
language_var = tk.StringVar(value="Tieng Han (ko)")  # mac dinh Tieng Han
beam_size = tk.StringVar(value="5")               # mac dinh beam 5

status = tk.StringVar(value="Cho")

frm = tk.Frame(root)
frm.pack(fill="both", expand=True, padx=12, pady=12)

row = 0
tk.Label(frm, text="File audio").grid(row=row, column=0, sticky="w")
tk.Entry(frm, textvariable=audio_path, width=80).grid(row=row, column=1, sticky="we", padx=6)
tk.Button(frm, text="Chon file", command=choose_file).grid(row=row, column=2, padx=4)
row += 1

tk.Label(frm, text="Chia audio: 4 phut dau = 8s/doan | Phan con lai = 10s/doan", fg="gray").grid(row=row, column=0, columnspan=3, sticky="w", pady=(4, 0))
row += 1

tk.Label(frm, text="Model").grid(row=row, column=0, sticky="w", pady=(10, 0))
ttk.Combobox(frm, textvariable=model_var, values=["tiny", "small", "medium", "large", "large-v2", "large-v3"]).grid(
    row=row, column=1, sticky="w", pady=(10, 0)
)
row += 1

tk.Label(frm, text="Engine").grid(row=row, column=0, sticky="w", pady=(6, 0))
ttk.Combobox(frm, textvariable=engine_var, values=["openai-whisper", "faster-whisper", "auto"], state="readonly").grid(
    row=row, column=1, sticky="w", pady=(6, 0)
)
row += 1

tk.Label(frm, text="Ngon ngu").grid(row=row, column=0, sticky="w", pady=(10, 0))
ttk.Combobox(
    frm,
    textvariable=language_var,
    values=["Tieng Han (ko)", "Tieng Nhat (ja)", "Tieng Anh (en)", "Auto (de trong)"],
    state="readonly",
    width=20,
).grid(row=row, column=1, sticky="w", pady=(10, 0))
row += 1

tk.Label(frm, text="Beam size (1 nhanh nhat, 5 chinh xac hon)").grid(row=row, column=0, sticky="w", pady=(6, 0))
tk.Entry(frm, textvariable=beam_size, width=12).grid(row=row, column=1, sticky="w", pady=(6, 0))
row += 1

progress = ttk.Progressbar(frm, length=700)
progress.grid(row=row, column=0, columnspan=3, pady=(18, 8))
row += 1

tk.Button(frm, text="BAT DAU", command=start, bg="green", fg="white", height=2).grid(
    row=row, column=0, columnspan=3, pady=8
)
row += 1

tk.Label(frm, textvariable=status).grid(row=row, column=0, columnspan=3, pady=6)

frm.columnconfigure(1, weight=1)

root.mainloop()
