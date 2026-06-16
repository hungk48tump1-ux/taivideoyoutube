import customtkinter as ctk
import os
import threading
from tkinter import filedialog, messagebox
import datetime
import json

import config
from api_client import APIClient
from pipeline_engine import PipelineEngine

class App(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title(f"{config.APP_NAME} v{config.APP_VERSION}")
        self.geometry("1100x850")
        ctk.set_appearance_mode("dark")
        self.configure(fg_color=config.COLOR_BG_DARK)
        
        # Pipeline State
        self.is_running = False
        self.is_paused = False
        self.pipeline = None
        
        self.whisk_file = ""
        self.veo_file = ""
        self.luot2_file = ""
        self.ref_image_file = ""
        self.saved_references = {}

        self._build_ui()
        self._load_config()
        self._log("Ready to start.")
        
        self.protocol("WM_DELETE_WINDOW", self._on_closing)

    def _build_ui(self):
        # --- TITLE ---
        title_lbl = ctk.CTkLabel(self, text="🎬 DHTNVIPPRO Pipeline - Tự Động Hóa Ảnh & Video", 
                               font=ctk.CTkFont(size=24, weight="bold"), text_color=config.COLOR_TEXT)
        title_lbl.pack(pady=15)
        
        # --- SERVER CONFIG ---
        self.server_frame = ctk.CTkFrame(self, fg_color=config.COLOR_CARD, corner_radius=10)
        self.server_frame.pack(fill="x", padx=20, pady=5)
        
        ctk.CTkLabel(self.server_frame, text="⚙️ CẤU HÌNH SERVER & API KEY", font=ctk.CTkFont(weight="bold")).grid(row=0, column=0, sticky="w", padx=10, pady=5)
        
        self.host_entry = ctk.CTkEntry(self.server_frame, width=150, placeholder_text="127.0.0.1")
        self.host_entry.insert(0, config.DEFAULT_SERVER_HOST)
        self.host_entry.grid(row=1, column=0, padx=10, pady=10)
        
        self.port_entry = ctk.CTkEntry(self.server_frame, width=80, placeholder_text="8777")
        self.port_entry.insert(0, str(config.DEFAULT_SERVER_PORT))
        self.port_entry.grid(row=1, column=1, padx=10, pady=10)
        
        self.api_key_entry = ctk.CTkEntry(self.server_frame, width=400, placeholder_text="Nhập API Key vào đây (có thể để trống)")
        self.api_key_entry.grid(row=1, column=2, padx=10, pady=10)
        
        self.btn_test_conn = ctk.CTkButton(self.server_frame, text="🔌 Test Connection", command=self._test_connection, fg_color=config.COLOR_INFO)
        self.btn_test_conn.grid(row=1, column=3, padx=10, pady=10)
        
        # --- INPUT FILES ---
        self.input_frame = ctk.CTkFrame(self, fg_color=config.COLOR_CARD, corner_radius=10)
        self.input_frame.pack(fill="x", padx=20, pady=10)
        
        # Row 0: Tiêu đề + Chọn Thư mục Auto
        ctk.CTkLabel(self.input_frame, text="📂 FILE ĐẦU VÀO", font=ctk.CTkFont(weight="bold")).grid(row=0, column=0, sticky="w", padx=10, pady=5)
        self.btn_auto_folder = ctk.CTkButton(self.input_frame, text="📁 Chọn Nhanh Thư Mục Chứa 3 File", command=self._browse_folder_auto, fg_color=config.COLOR_INFO)
        self.btn_auto_folder.grid(row=0, column=1, padx=10, pady=5, sticky="w")

        # Helper text function
        def add_file_row(parent, row, label_text, var_name):
            ctk.CTkLabel(parent, text=label_text).grid(row=row, column=0, padx=10, pady=5, sticky="w")
            entry = ctk.CTkEntry(parent, width=450, placeholder_text="Chưa chọn file...", state="disabled")
            entry.grid(row=row, column=1, padx=10, pady=5)
            btn = ctk.CTkButton(parent, text="Browse", width=80, 
                                command=lambda: self._browse_file(entry, var_name, is_image=(var_name=="ref_image_file")))
            btn.grid(row=row, column=2, padx=5, pady=5)
            return entry
            
        self.entry_whisk = add_file_row(self.input_frame, 1, "File 30WHISK.txt (Tạo ảnh B1):", "whisk_file")
        self.entry_veo = add_file_row(self.input_frame, 2, "File 30VEO.txt (Tạo video B2):", "veo_file")
        self.entry_luot2 = add_file_row(self.input_frame, 3, "File luot2_tro_di.txt (Tạo ảnh B3):", "luot2_file")
        
        # Row 4: Ảnh tham chiếu chuyên dụng (Hiển thị path)
        ctk.CTkLabel(self.input_frame, text="🖼️ Ảnh tham chiếu chung:").grid(row=4, column=0, padx=10, pady=5, sticky="w")
        self.entry_ref = ctk.CTkEntry(self.input_frame, width=450, placeholder_text="Chưa chọn file...", state="disabled")
        self.entry_ref.grid(row=4, column=1, padx=(10, 5), pady=5, sticky="w")
        
        self.btn_ref_browse = ctk.CTkButton(self.input_frame, text="Browse", width=80, 
                                            command=lambda: self._browse_file(self.entry_ref, "ref_image_file", is_image=True))
        self.btn_ref_browse.grid(row=4, column=2, padx=5, pady=5, sticky="w")

        # Row 5: Nút điều khiển lưu / quản lý Kho Ảnh Tham Chiếu
        ref_control_frame = ctk.CTkFrame(self.input_frame, fg_color="transparent")
        ref_control_frame.grid(row=5, column=1, padx=(10, 5), pady=(0, 10), sticky="w")
        
        # Combobox để chọn tên ảnh đã lưu
        self.cmb_saved_refs = ctk.CTkComboBox(ref_control_frame, values=["-- Chọn ảnh từ kho --"], width=200, command=self._load_saved_reference)
        self.cmb_saved_refs.pack(side="left", padx=(0, 10))
        
        self.btn_ref_save = ctk.CTkButton(ref_control_frame, text="➕ Lưu Kèm Tên", width=100, command=self._save_reference_dialog, fg_color="#2b7a78", hover_color="#17252a")
        self.btn_ref_save.pack(side="left", padx=5)

        self.btn_ref_del = ctk.CTkButton(ref_control_frame, text="➖ Xóa Mẫu Này", width=100, command=self._delete_reference_dialog, fg_color="#d62828", hover_color="#85182a")
        self.btn_ref_del.pack(side="left", padx=5)
        
        # --- MODELS SETTINGS ---
        self.settings_frame = ctk.CTkFrame(self, fg_color=config.COLOR_CARD, corner_radius=10)
        self.settings_frame.pack(fill="x", padx=20, pady=5)
        
        ctk.CTkLabel(self.settings_frame, text="⚙️ CẤU HÌNH PIPELINE", font=ctk.CTkFont(weight="bold")).grid(row=0, column=0, sticky="w", padx=10, pady=5)
        
        self.cmb_img_model = ctk.CTkOptionMenu(self.settings_frame, values=config.IMAGE_MODELS)
        self.cmb_img_model.set(config.DEFAULT_IMAGE_MODEL)
        self.cmb_img_model.grid(row=1, column=0, padx=10, pady=5)
        
        self.cmb_vid_model = ctk.CTkOptionMenu(self.settings_frame, values=config.VIDEO_MODELS)
        self.cmb_vid_model.set(config.DEFAULT_VIDEO_MODEL)
        self.cmb_vid_model.grid(row=1, column=1, padx=10, pady=5)
        
        self.cmb_aspect = ctk.CTkOptionMenu(self.settings_frame, values=config.ASPECT_RATIOS, width=80)
        self.cmb_aspect.set(config.DEFAULT_ASPECT_RATIO)
        self.cmb_aspect.grid(row=1, column=2, padx=10, pady=5)
        
        self.cmb_res = ctk.CTkOptionMenu(self.settings_frame, values=config.RESOLUTIONS, width=80)
        self.cmb_res.set(config.DEFAULT_RESOLUTION)
        self.cmb_res.grid(row=1, column=3, padx=10, pady=5)

        self.cmb_threads = ctk.CTkOptionMenu(self.settings_frame, values=["1", "3", "5", "10", "20", "30", "50"], width=80)
        self.cmb_threads.set("3")
        self.cmb_threads.grid(row=1, column=4, padx=10, pady=5)
        ctk.CTkLabel(self.settings_frame, text="Luồng song song").grid(row=0, column=4, padx=10)

        # --- PROGRESS & LOG ---
        self.bottom_frame = ctk.CTkFrame(self, fg_color=config.COLOR_BG_DARK)
        self.bottom_frame.pack(fill="both", expand=True, padx=20, pady=10)
        
        self.lbl_status_whisk = ctk.CTkLabel(self.bottom_frame, text="Bước 1 (WHISK): 0/0")
        self.lbl_status_whisk.pack(anchor="w")
        self.prog_whisk = ctk.CTkProgressBar(self.bottom_frame, width=800, progress_color=config.COLOR_HIGHLIGHT)
        self.prog_whisk.pack(fill="x", pady=(0, 10))
        self.prog_whisk.set(0)
        
        self.lbl_status_veo = ctk.CTkLabel(self.bottom_frame, text="Bước 2 (VEO): 0/0")
        self.lbl_status_veo.pack(anchor="w")
        self.prog_veo = ctk.CTkProgressBar(self.bottom_frame, width=800, progress_color=config.COLOR_SUCCESS)
        self.prog_veo.pack(fill="x", pady=(0, 10))
        self.prog_veo.set(0)
        
        self.lbl_status_luot2 = ctk.CTkLabel(self.bottom_frame, text="Bước 3 (Lượt 2): 0/0")
        self.lbl_status_luot2.pack(anchor="w")
        self.prog_luot2 = ctk.CTkProgressBar(self.bottom_frame, width=800, progress_color=config.COLOR_WARNING)
        self.prog_luot2.pack(fill="x", pady=(0, 10))
        self.prog_luot2.set(0)

        self.console = ctk.CTkTextbox(self.bottom_frame, height=150, text_color=config.COLOR_TEXT_DIM, fg_color=config.COLOR_CARD)
        self.console.pack(fill="both", expand=True, pady=10)
        
        # --- CONTROL BUTTONS ---
        self.control_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.control_frame.pack(fill="x", padx=20, pady=10)
        
        self.btn_start = ctk.CTkButton(self.control_frame, text="▶️ BẮT ĐẦU PIPELINE", command=self._start_pipeline, font=ctk.CTkFont(weight="bold", size=16), fg_color=config.COLOR_SUCCESS, height=40)
        self.btn_start.pack(side="left", padx=10, fill="x", expand=True)

        self.btn_pause = ctk.CTkButton(self.control_frame, text="⏸️ TẠM DỪNG", command=self._toggle_pause, state="disabled", fg_color=config.COLOR_WARNING, height=40)
        self.btn_pause.pack(side="left", padx=10, fill="x", expand=True)
        
        self.btn_stop = ctk.CTkButton(self.control_frame, text="⏹️ DỪNG", command=self._stop_pipeline, state="disabled", fg_color=config.COLOR_ERROR, height=40)
        self.btn_stop.pack(side="left", padx=10, fill="x", expand=True)

    def _log(self, msg, level="info"):
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        self.console.insert("end", f"[{timestamp}] {msg}\n")
        self.console.see("end")

    def _set_file_ui(self, entry_widget, var_name, path):
        if path and os.path.exists(path):
            setattr(self, var_name, path)
            entry_widget.configure(state="normal")
            entry_widget.delete(0, "end")
            entry_widget.insert(0, path)
            entry_widget.configure(state="disabled")

    def _browse_file(self, entry_widget, target_var, is_image=False):
        types = [("Image files", "*.png;*.jpg;*.jpeg")] if is_image else [("Text files", "*.txt")]
        filepath = filedialog.askopenfilename(filetypes=types)
        if filepath:
            self._set_file_ui(entry_widget, target_var, filepath)

    def _save_reference_dialog(self):
        if not self.ref_image_file or not os.path.exists(self.ref_image_file):
            messagebox.showwarning("Lỗi", "Bạn chưa chọn ảnh tham chiếu nào hợp lệ để lưu!")
            return
            
        dialog = ctk.CTkInputDialog(text="Tên nhận diện cho ảnh tham chiếu này:", title="Lưu Ảnh Tham Chiếu")
        name = dialog.get_input()
        if name and name.strip():
            self.saved_references[name.strip()] = self.ref_image_file
            self._update_saved_refs_dropdown()
            self._save_config()
            self._log(f"Đã lưu đường dẫn ảnh tham chiếu '{name}'.", "success")

    def _update_saved_refs_dropdown(self):
        names = ["-- Chọn ảnh từ kho --"] + list(self.saved_references.keys())
        self.cmb_saved_refs.configure(values=names)
        self.cmb_saved_refs.set("-- Chọn ảnh từ kho --")

    def _load_saved_reference(self, selected_name):
        if selected_name != "-- Chọn ảnh từ kho --" and selected_name in self.saved_references:
            path = self.saved_references[selected_name]
            if os.path.exists(path):
                self._set_file_ui(self.entry_ref, "ref_image_file", path)
                self._log(f"Đã tải ảnh tham chiếu: {selected_name}")
            else:
                messagebox.showerror("Lỗi", f"File ảnh tham chiếu '{selected_name}' không còn tồn tại ở vị trí: {path}")

    def _delete_reference_dialog(self):
        selected = self.cmb_saved_refs.get()
        if selected == "-- Chọn ảnh từ kho --" or selected not in self.saved_references:
            messagebox.showwarning("Lỗi", "Vui lòng chọn một ảnh đã lưu từ Dropdown để xóa!")
            return
            
        if messagebox.askyesno("Xác nhận", f"Bạn có chắc muốn xóa ảnh mẫu '{selected}' khỏi kho?"):
            del self.saved_references[selected]
            self._update_saved_refs_dropdown()
            self._save_config()
            self._log(f"Đã xóa ảnh tham chiếu: {selected}")
            self._set_file_ui(self.entry_ref, "ref_image_file", "")
            self.ref_image_file = ""
                
    def _browse_folder_auto(self):
        folder = filedialog.askdirectory(title="Chọn Thư Mục Chứa 3 File TXT")
        if not folder: return
        self._log(f"Đang tự động quét thư mục: {folder}")
        
        found_whisk = found_veo = found_luot2 = found_img = False
        
        for f in os.listdir(folder):
            path = os.path.join(folder, f)
            if not os.path.isfile(path): continue
            
            f_lower = f.lower()
            if "30whisk" in f_lower and f_lower.endswith(".txt"):
                self._set_file_ui(self.entry_whisk, "whisk_file", path)
                found_whisk = True
                self._log(f"Đã tự động nạp file B1: {f}", "success")
            elif "30veo" in f_lower and f_lower.endswith(".txt"):
                self._set_file_ui(self.entry_veo, "veo_file", path)
                found_veo = True
                self._log(f"Đã tự động nạp file B2: {f}", "success")
            elif "luot2_tro_di" in f_lower and f_lower.endswith(".txt"):
                self._set_file_ui(self.entry_luot2, "luot2_file", path)
                found_luot2 = True
                self._log(f"Đã tự động nạp file B3: {f}", "success")
            elif not found_img and f_lower.endswith((".png", ".jpg", ".jpeg")):
                self._set_file_ui(self.entry_ref, "ref_image_file", path)
                found_img = True
                self._log(f"Đã tự động nạp ảnh tham chiếu: {f}", "success")

        if not (found_whisk or found_veo or found_luot2):
            messagebox.showwarning("Không tìm thấy file", "Thư mục vừa chọn không chứa các file cần thiết (chứa các từ khóa 30whisk, 30veo, luot2_tro_di)!")
            self._log("Không tìm thấy file hợp lệ nào trong thư mục.", "warning")

    def _test_connection(self):
        client = APIClient(self.host_entry.get(), int(self.port_entry.get()), self.api_key_entry.get(), log_cb=self._log)
        if client.check_health():
            messagebox.showinfo("Success", "Kết nối tới Webhook Server thành công!")
            self._log("Test connection successful.", "success")
        else:
            messagebox.showerror("Error", "Không thể kết nối. Kiểm tra URL, Port hoặc API Key.")
            self._log("Test connection failed.", "error")

    def _update_progress(self, section, current, total):
        pct = current / total if total > 0 else 0
        if section == "WHISK_IMAGES":
            self.lbl_status_whisk.configure(text=f"Bước 1 (WHISK): {current}/{total}")
            self.prog_whisk.set(pct)
        elif section == "VEO_VIDEOS":
            self.lbl_status_veo.configure(text=f"Bước 2 (VEO): {current}/{total}")
            self.prog_veo.set(pct)
        elif section == "LUOT2_IMAGES":
            self.lbl_status_luot2.configure(text=f"Bước 3 (Lượt 2): {current}/{total}")
            self.prog_luot2.set(pct)

    def _on_pipeline_completed(self):
        self._toggle_ui_state(running=False)
        self.btn_start.configure(text="▶️ CHẠY LẠI TỪ ĐẦU")
        try:
             import winsound
             winsound.MessageBeep(winsound.MB_ICONASTERISK)
        except:
             pass

    def _on_pipeline_error(self, err_msg):
        self._toggle_ui_state(running=False)
        messagebox.showerror("Pipeline Error", err_msg)

    def _toggle_ui_state(self, running):
        self.is_running = running
        if running:
            self.btn_start.configure(state="disabled")
            self.btn_pause.configure(state="normal", text="⏸️ TẠM DỪNG")
            self.btn_stop.configure(state="normal")
            self.host_entry.configure(state="disabled")
            self.port_entry.configure(state="disabled")
            self.btn_auto_folder.configure(state="disabled")
        else:
            self.btn_start.configure(state="normal")
            self.btn_pause.configure(state="disabled")
            self.btn_stop.configure(state="disabled")
            self.host_entry.configure(state="normal")
            self.port_entry.configure(state="normal")
            self.btn_auto_folder.configure(state="normal")
            self.is_paused = False

    def _start_pipeline(self):
        if not self.whisk_file.strip() and not self.veo_file.strip() and not self.luot2_file.strip():
            messagebox.showwarning("Thiếu file", "Vui lòng chọn ít nhất 1 file TXT đầu vào.")
            return

        if self.whisk_file.strip() and not self.ref_image_file.strip():
            messagebox.showwarning("Thiếu ảnh tham chiếu", "File 30WHISK cần ảnh tham chiếu để tạo ảnh từ prompt + reference image.")
            self._log("Thiếu ảnh tham chiếu: 30WHISK sẽ không chạy prompt-only.", "error")
            return

        if self.luot2_file.strip() and not self.ref_image_file.strip():
            messagebox.showwarning("Thiếu ảnh tham chiếu", "File Luot2 cần ảnh tham chiếu để tạo ảnh từ prompt + reference image.")
            self._log("Thiếu ảnh tham chiếu: Luot2 sẽ không chạy prompt-only.", "error")
            return

        if (self.whisk_file.strip() or self.luot2_file.strip()) and not os.path.exists(self.ref_image_file):
            messagebox.showwarning("Ảnh tham chiếu không hợp lệ", f"Không tìm thấy ảnh tham chiếu:\n{self.ref_image_file}")
            self._log(f"Không tìm thấy ảnh tham chiếu: {self.ref_image_file}", "error")
            return

        self._save_config()  # Lưu cấu hình mỗi khi bấm chạy
        self._toggle_ui_state(running=True)
        self.prog_whisk.set(0)
        self.prog_veo.set(0)
        self.prog_luot2.set(0)
        self.console.delete("0.0", "end")
        self._log("Khởi động Pipeline...")

        client = APIClient(self.host_entry.get(), int(self.port_entry.get()), self.api_key_entry.get(), log_cb=self._log)
        
        callbacks = {
            'log': self._log,
            'progress': self._update_progress,
            'completed': self._on_pipeline_completed,
            'error': self._on_pipeline_error
        }
        
        settings = {
            'image_model': self.cmb_img_model.get(),
            'video_model': self.cmb_vid_model.get(),
            'aspect_ratio': self.cmb_aspect.get(),
            'resolution': self.cmb_res.get(),
            'video_mode': config.DEFAULT_VIDEO_MODE,
            'category': config.DEFAULT_CATEGORY,
            'output_dir': "output",
            'threads': int(self.cmb_threads.get())
        }

        self.pipeline = PipelineEngine(client, callbacks)
        self.pipeline.run_pipeline(
            self.whisk_file, 
            self.veo_file, 
            self.luot2_file, 
            self.ref_image_file, 
            settings
        )

    def _toggle_pause(self):
        if not self.pipeline: return
        
        if self.is_paused:
            self.pipeline.resume()
            self.is_paused = False
            self.btn_pause.configure(text="⏸️ TẠM DỪNG")
            self._log("Đã tiếp tục pipeline.")
        else:
            self.pipeline.pause()
            self.is_paused = True
            self.btn_pause.configure(text="▶️ TIẾP TỤC")
            self._log("Đã tạm dừng pipeline. Sẽ dừng sau tác vụ hiện tại.")

    def _stop_pipeline(self):
        if not self.pipeline: return
        if messagebox.askyesno("Xác nhận", "Bạn có chắc muốn dừng hoàn toàn tiến trình?"):
            self.pipeline.stop()
            self._toggle_ui_state(running=False)
            self._log("Người dùng yêu cầu dừng pipeline.", "warning")
            
    # --- CONFIGURATION SAVE & LOAD ---
    def _save_config(self):
        config_data = {
            "host": self.host_entry.get(),
            "port": self.port_entry.get(),
            "api_key": self.api_key_entry.get(),
            "whisk_file": self.whisk_file,
            "veo_file": self.veo_file,
            "luot2_file": self.luot2_file,
            "ref_image_file": self.ref_image_file,
            "image_model": self.cmb_img_model.get(),
            "video_model": self.cmb_vid_model.get(),
            "aspect_ratio": self.cmb_aspect.get(),
            "resolution": self.cmb_res.get(),
            "threads": self.cmb_threads.get(),
            "saved_references": self.saved_references
        }
        try:
            with open("settings.json", "w", encoding="utf-8") as f:
                json.dump(config_data, f, indent=4)
        except Exception as e:
            print(f"Error saving config: {e}")

    def _load_config(self):
        if os.path.exists("settings.json"):
            try:
                with open("settings.json", "r", encoding="utf-8") as f:
                    config_data = json.load(f)
                    
                if "host" in config_data:
                    self.host_entry.delete(0, "end"); self.host_entry.insert(0, config_data["host"])
                if "port" in config_data:
                    self.port_entry.delete(0, "end"); self.port_entry.insert(0, config_data["port"])
                if "api_key" in config_data:
                    self.api_key_entry.delete(0, "end"); self.api_key_entry.insert(0, config_data["api_key"])
                    
                self._set_file_ui(self.entry_whisk, "whisk_file", config_data.get("whisk_file", ""))
                self._set_file_ui(self.entry_veo, "veo_file", config_data.get("veo_file", ""))
                self._set_file_ui(self.entry_luot2, "luot2_file", config_data.get("luot2_file", ""))
                self._set_file_ui(self.entry_ref, "ref_image_file", config_data.get("ref_image_file", ""))
                
                if "image_model" in config_data: self.cmb_img_model.set(config_data["image_model"])
                if "video_model" in config_data: self.cmb_vid_model.set(config_data["video_model"])
                if "aspect_ratio" in config_data: self.cmb_aspect.set(config_data["aspect_ratio"])
                if "resolution" in config_data: self.cmb_res.set(config_data["resolution"])
                if "threads" in config_data: self.cmb_threads.set(config_data["threads"])
                
                if "saved_references" in config_data:
                    self.saved_references = config_data["saved_references"]
                    self._update_saved_refs_dropdown()
            except Exception as e:
                print(f"Lỗi tải cấu hình cũ: {e}")

    def _on_closing(self):
        self._save_config()
        self.destroy()

if __name__ == "__main__":
    app = App()
    app.mainloop()
