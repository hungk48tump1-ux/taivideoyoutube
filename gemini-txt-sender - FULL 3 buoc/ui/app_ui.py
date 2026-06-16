"""
ui/app_ui.py - Giao diện chính của ứng dụng Gemini TXT Chunk Sender

KIẾN TRÚC THREAD:
  - MainThread (Tkinter): chỉ cập nhật UI
  - BrowserThread: toàn bộ Playwright sống ở đây, nhận lệnh qua queue
  - WorkerThread: logic gửi chunk, giao tiếp với BrowserThread qua _browser_call()
"""
import json
import importlib.util
import queue
import subprocess
import threading
import time
import os
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Any, Callable, Dict, List, Optional

from core.logger import get_logger

logger = get_logger()

# ─────────────────────────────────────────────────────────────────────
# Z115 CHROME EXTENSION HTTP BRIDGE SERVER
# ─────────────────────────────────────────────────────────────────────
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler

BRIDGE_JOBS = {
    "file1": {
        "pending_chunk": None,
        "result": None,
        "event": threading.Event(),
        "designated_tab_id": None,   # Tab được chỉ định tự động
        "last_heartbeat": 0,         # Timestamp poll cuối của tab đó
    },
    "file2": {
        "pending_chunk": None,
        "result": None,
        "event": threading.Event(),
        "designated_tab_id": None,
        "last_heartbeat": 0,
    },
}

# Lock để tránh race condition khi nhiều tab poll đồng thời
_bridge_lock = threading.Lock()

class GeminiBridgeRequestHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Ẩn log truy cập HTTP thông thường để tránh làm bẩn bảng console
        pass

    def do_GET(self):
        # Hỗ trợ đầy đủ CORS cho Chrome Extension
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Type", "application/json")
        self.end_headers()

        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        if parsed.path == "/poll":
            job_id = (params.get("job", [""])[0])
            tab_id = (params.get("tab_id", [""])[0])

            # Chế độ auto: duyệt cả file1 và file2 tuần tự
            if job_id == "auto":
                now = time.time()
                data = {"cmd": "idle", "designated": True}
                with _bridge_lock:
                    for jid in ["file1", "file2"]:
                        job = BRIDGE_JOBS[jid]
                        
                        # Tự động gán lại tab nếu đang trống (phòng trường hợp tab bị kick do timeout)
                        if job["designated_tab_id"] is None and tab_id:
                            job["designated_tab_id"] = tab_id
                            logger.info("[Bridge] Tự động nối lại tab '%s' cho luồng '%s'", tab_id[:8], jid)

                        # Cập nhật heartbeat nếu tab này đã được chỉ định
                        if job["designated_tab_id"] == tab_id:
                            job["last_heartbeat"] = now
                        
                        # Trả về pending_chunk nếu có
                        if job["designated_tab_id"] == tab_id and job["pending_chunk"]:
                            data = job["pending_chunk"]
                            data["_from_job"] = jid  # Để addon biết gửi result về đúng luồng
                            break
                self.wfile.write(json.dumps(data).encode("utf-8"))
                return

            if job_id not in BRIDGE_JOBS:
                self.wfile.write(json.dumps({"cmd": "idle"}).encode("utf-8"))
                return

            job = BRIDGE_JOBS[job_id]
            now = time.time()

            with _bridge_lock:
                designated = job["designated_tab_id"]

                if not tab_id:
                    # Extension cũ không gửi tab_id → cho qua bình thường (tương thích ngược)
                    data = job["pending_chunk"] if job["pending_chunk"] else {"cmd": "idle"}

                elif designated is None:
                    # Chưa có tab nào → tự động chỉ định tab này
                    job["designated_tab_id"] = tab_id
                    job["last_heartbeat"] = now
                    logger.info("[Bridge] Auto-designated tab '%s' cho job '%s'", tab_id[:8], job_id)
                    data = job["pending_chunk"] if job["pending_chunk"] else {"cmd": "idle", "designated": True}

                elif designated == tab_id:
                    # Đúng tab được chỉ định → cập nhật heartbeat
                    job["last_heartbeat"] = now
                    data = job["pending_chunk"] if job["pending_chunk"] else {"cmd": "idle", "designated": True}

                else:
                    # Tab khác đang chạy → từ chối
                    data = {"cmd": "idle", "designated": False, "msg": "Tab khác đang được chỉ định"}

            self.wfile.write(json.dumps(data).encode("utf-8"))

        elif parsed.path == "/undesignate":
            # Reset designation thủ công từ Python UI
            job_id = params.get("job", [""])[0]
            if job_id in BRIDGE_JOBS:
                with _bridge_lock:
                    old = BRIDGE_JOBS[job_id]["designated_tab_id"]
                    BRIDGE_JOBS[job_id]["designated_tab_id"] = None
                    BRIDGE_JOBS[job_id]["last_heartbeat"] = 0
                logger.info("[Bridge] Đã reset designation cho job '%s' (cũ: %s)", job_id, (old or "")[:8])
            self.wfile.write(json.dumps({"status": "ok"}).encode("utf-8"))

        elif parsed.path == "/status":
            # Python UI poll để hiển thị trạng thái
            now = time.time()
            result = {}
            for job_id, job in BRIDGE_JOBS.items():
                tab = job["designated_tab_id"]
                hb = job["last_heartbeat"]
                alive = tab is not None and (now - hb) < 10
                result[job_id] = {
                    "tab_id": tab,
                    "tab_short": (tab[:4] + "..." + tab[-4:]) if tab and len(tab) > 8 else tab,
                    "alive": alive,
                    "last_seen": round(now - hb, 1) if tab else None,
                }
            self.wfile.write(json.dumps(result).encode("utf-8"))

        elif parsed.path == "/connect":
            self._handle_connect(params)

        else:
            self.wfile.write(json.dumps({"status": "ok"}).encode("utf-8"))

    def _handle_connect(self, params):
        """Endpoint /connect: Tab mới chiếm quyền điều khiển, đá hết tab cũ ra."""
        tab_id = params.get("tab_id", [""])[0]
        if tab_id:
            with _bridge_lock:
                for jid in BRIDGE_JOBS:
                    BRIDGE_JOBS[jid]["designated_tab_id"] = tab_id
                    BRIDGE_JOBS[jid]["last_heartbeat"] = time.time()
            logger.info("[Bridge] Tab '%s' đã CHIẾM QUYỀN điều khiển tất cả luồng.", tab_id[:8])
            self.wfile.write(json.dumps({"status": "ok", "msg": "connected"}).encode("utf-8"))
        else:
            self.wfile.write(json.dumps({"status": "error", "msg": "missing tab_id"}).encode("utf-8"))

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/result":
            params = urllib.parse.parse_qs(parsed.query)
            job_list = params.get("job", [])
            job_id = job_list[0] if job_list else ""

            content_length = int(self.headers["Content-Length"])
            post_data = self.rfile.read(content_length)
            
            self.send_response(200)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok"}).encode("utf-8"))

            if job_id in BRIDGE_JOBS:
                try:
                    payload = json.loads(post_data.decode("utf-8"))
                    job = BRIDGE_JOBS[job_id]
                    job["result"] = payload
                    job["event"].set()
                except Exception as e:
                    logger.error("Lỗi parse kết quả từ Extension: %s", e)

    def do_OPTIONS(self):
        # Trả lời preflight request CORS từ Google Chrome
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

def start_bridge_server():
    try:
        server = HTTPServer(("127.0.0.1", 8779), GeminiBridgeRequestHandler)
        server.serve_forever()
    except Exception as e:
        logger.error("Lỗi khởi chạy Z115 Bridge Server: %s", e)


def _heartbeat_checker():
    """Kiểm tra mỗi 5 giây — nếu tab chỉ định không poll > 10 giây thì tự xóa designation."""
    while True:
        time.sleep(5)
        now = time.time()
        for job_id, job in BRIDGE_JOBS.items():
            with _bridge_lock:
                tab = job["designated_tab_id"]
                hb = job["last_heartbeat"]
                if tab and (now - hb) > 10:
                    job["designated_tab_id"] = None
                    job["last_heartbeat"] = 0
                    logger.warning(
                        "[Bridge] Tab '%s' của job '%s' mất kết nối (>10s). Đã reset designation.",
                        tab[:8], job_id
                    )


# Khởi chạy server trong một daemon thread để tự động đóng khi app tắt
threading.Thread(target=start_bridge_server, daemon=True, name="Z115BridgeServer").start()
threading.Thread(target=_heartbeat_checker, daemon=True, name="Z115HeartbeatChecker").start()
logger.info("🚀 Z115 Extension Bridge Server đã bắt đầu hoạt động trên http://127.0.0.1:8779")

# ─────────────────────────────────────────────────────────────────────
# Theme
# ─────────────────────────────────────────────────────────────────────

C = {
    "bg":       "#1a1a2e",
    "surface":  "#16213e",
    "surface2": "#0f3460",
    "accent":   "#e94560",
    "accent2":  "#533483",
    "text":     "#eaeaea",
    "dim":      "#8a8a9a",
    "ok":       "#4caf50",
    "warn":     "#ff9800",
    "err":      "#f44336",
    "info":     "#2196f3",
    "border":   "#2d2d4e",
    "teal":     "#1a6b8a",
}
FB = ("Segoe UI", 10)
FS = ("Segoe UI", 9)
FC = ("Courier New", 9)

VIDEO_MODELS = [
    "veo_31_fast_relaxed",
    "veo_31_lite_relaxed",
    "veo_31_fast",
    "veo_31_lite",
    "veo_31_quality",
]
DEFAULT_VIDEO_MODEL = "veo_31_fast_relaxed"
LEGACY_VIDEO_MODELS = {
    "veo_S1_fast_relaxed": "veo_31_fast_relaxed",
    "veo_S1_fast": "veo_31_fast",
    "veo_S1_quality": "veo_31_quality",
}


def _normalize_video_model(value):
    return LEGACY_VIDEO_MODELS.get(value, value or DEFAULT_VIDEO_MODEL)


def _lbl(parent, text, font=FS, fg=None, **kw):
    return tk.Label(parent, text=text, font=font, fg=fg or C["text"], bg=C["bg"], **kw)


def _btn(parent, text, cmd, bg=None, fg=None, width=None, **kw):
    params = {
        "text": text, "command": cmd,
        "bg": bg or C["accent2"], "fg": fg or C["text"],
        "activebackground": C["accent"], "activeforeground": C["text"],
        "relief": "flat", "bd": 0, "cursor": "hand2",
        "font": FB, "padx": 8, "pady": 4
    }
    params.update(kw)
    b = tk.Button(parent, **params)
    if width:
        b.config(width=width)
    return b


def _entry(parent, var=None, width=None, **kw):
    params = {
        "bg": C["surface"], "fg": C["text"], "insertbackground": C["text"],
        "relief": "flat", "bd": 1, "highlightthickness": 1,
        "highlightcolor": C["accent2"], "highlightbackground": C["border"],
        "font": FB
    }
    params.update(kw)
    e = tk.Entry(parent, textvariable=var, **params)
    if width:
        e.config(width=width)
    return e


def _sep(parent):
    return tk.Frame(parent, height=1, bg=C["border"])


# ─────────────────────────────────────────────────────────────────────
# App
# ─────────────────────────────────────────────────────────────────────

class App(tk.Tk):
    """Cửa sổ chính. Mọi Playwright call đi qua BrowserThread qua queue."""

    def __init__(self, config: Dict[str, Any], selectors: Dict[str, Any]):
        super().__init__()
        self.config = config
        self.selectors = selectors
        self._ui_ready = False

        self.title("Z115_DHVIPPRO")
        self.geometry("1200x860")
        self.minsize(1000, 700)
        self.configure(bg=C["bg"])
        try:
            icon_path = Path(__file__).parent.parent / "logo.png"
            if icon_path.exists():
                icon = tk.PhotoImage(file=str(icon_path))
                self.iconphoto(False, icon)
            else:
                self.iconbitmap(default="")
        except Exception as e:
            logger.debug(f"Không thể load logo: {e}")

        # ── Biến Tkinter ──
        self.v_script     = tk.StringVar(value="")
        self.v_script2    = tk.StringVar(value="")
        self.v_audio      = tk.StringVar(value="")
        self.v_audio2     = tk.StringVar(value="")
        self.v_aud_model  = tk.StringVar(value=config.get("audio_model", "large-v3"))
        self.v_aud_engine = tk.StringVar(value=config.get("audio_engine", "openai-whisper"))
        self.v_aud_lang   = tk.StringVar(value=config.get("audio_language", "Tiếng Hàn (ko)"))
        self.v_aud_beam   = tk.IntVar(value=int(config.get("audio_beam_size", 5)))
        self.v_aud_first4 = tk.StringVar(value=str(config.get("audio_first4min_chunk_seconds", 8)))
        self.v_aud_model2  = tk.StringVar(value=config.get("audio_model_2", config.get("audio_model", "large-v3")))
        self.v_aud_engine2 = tk.StringVar(value=config.get("audio_engine_2", config.get("audio_engine", "openai-whisper")))
        self.v_aud_lang2   = tk.StringVar(value=config.get("audio_language_2", config.get("audio_language", "Tiếng Hàn (ko)")))
        self.v_aud_beam2   = tk.IntVar(value=int(config.get("audio_beam_size_2", config.get("audio_beam_size", 5))))
        self.v_aud_first42 = tk.StringVar(value=str(config.get("audio_first4min_chunk_seconds_2", config.get("audio_first4min_chunk_seconds", 8))))

        self.v_file    = tk.StringVar(value="")
        self.v_file2   = tk.StringVar(value="")
        default_url = config.get("gemini_url", "https://gemini.google.com/app")
        # Danh sách URL đã lưu kiểu mới (Dictionary: Tên -> URL)
        saved_urls_raw = config.get("saved_urls", {})
        if isinstance(saved_urls_raw, list):
            # Migrate old list to dict with ref_image
            self._saved_urls = {u: {"url": u, "ref_image": ""} for u in saved_urls_raw}
        else:
            # Check and migrate old dict of strings to dict of dicts
            self._saved_urls = {}
            for k, v in saved_urls_raw.items():
                if isinstance(v, str):
                    self._saved_urls[k] = {"url": v, "ref_image": ""}
                else:
                    self._saved_urls[k] = v
            
        if not any(v.get("url") == default_url for v in self._saved_urls.values()):
            self._saved_urls["Mặc định"] = {"url": default_url, "ref_image": ""}

        # Tìm key ứng với default_url
        initial_key = next((k for k, v in self._saved_urls.items() if v.get("url") == default_url), list(self._saved_urls.keys())[0])
        self.v_url     = tk.StringVar(value=initial_key)
        self.v_url2    = tk.StringVar(value=config.get("last_preset_name_2", initial_key))
        self.v_first   = tk.IntVar(value=config.get("first_chunk_size", 30))
        self.v_next    = tk.IntVar(value=config.get("next_chunk_size", 50))
        self.v_model   = tk.StringVar(value=config.get("target_model_text", "Pro"))
        self.v_skip    = tk.BooleanVar(value=config.get("skip_empty_lines", True))
        self.v_save    = tk.BooleanVar(value=True)
        self.v_delay   = tk.BooleanVar(value=True)
        self.v_delayt  = tk.DoubleVar(value=config.get("inter_chunk_delay_seconds", 3))
        self.v_first2   = tk.IntVar(value=config.get("first_chunk_size_2", config.get("first_chunk_size", 30)))
        self.v_next2    = tk.IntVar(value=config.get("next_chunk_size_2", config.get("next_chunk_size", 50)))
        self.v_model2   = tk.StringVar(value=config.get("target_model_text_2", config.get("target_model_text", "Pro")))
        self.v_skip2    = tk.BooleanVar(value=config.get("skip_empty_lines_2", config.get("skip_empty_lines", True)))
        self.v_save2    = tk.BooleanVar(value=config.get("save_response_2", True))
        self.v_delay2   = tk.BooleanVar(value=config.get("inter_chunk_delay_enabled_2", True))
        self.v_delayt2  = tk.DoubleVar(value=config.get("inter_chunk_delay_seconds_2", config.get("inter_chunk_delay_seconds", 3)))
        self.v_do_audio = tk.BooleanVar(value=config.get("workflow_do_audio", True))
        self.v_do_gemini = tk.BooleanVar(value=config.get("workflow_do_gemini", True))
        self.v_do_pipeline = tk.BooleanVar(value=config.get("workflow_do_pipeline", True))

        self.internal_profiles = [f"Profile {i}" for i in range(1, 6)]
        last_profile = config.get("active_internal_profile", "Profile 1")
        if last_profile not in self.internal_profiles:
            last_profile = "Profile 1"
        self.v_internal_profile = tk.StringVar(value=last_profile)
        self.v_custom_browser_path = tk.StringVar(value=config.get("custom_browser_path", "C:/Program Files/Google/Chrome/Application/chrome.exe"))

        self.v_lines   = tk.StringVar(value="—")
        self.v_chunks  = tk.StringVar(value="—")
        self.v_cur     = tk.StringVar(value="—")
        self.v_status  = tk.StringVar(value="Chờ bắt đầu")
        self.v_lines2   = tk.StringVar(value="—")
        self.v_chunks2  = tk.StringVar(value="—")
        self.v_cur2     = tk.StringVar(value="—")
        self.v_status2  = tk.StringVar(value="Chờ bắt đầu")

        # ── Queues ──
        self._log_q: queue.Queue = queue.Queue()      # UI log
        self._bcmd_q: queue.Queue = queue.Queue()     # lệnh → BrowserThread
        self._bres_q: queue.Queue = queue.Queue()     # kết quả ← BrowserThread
        self._job_log_context = threading.local()

        # ── BrowserThread (Playwright - chỉ dùng cho tính năng cũ / phụ trợ) ──
        self._playwright_available = importlib.util.find_spec("playwright") is not None
        self._bt: Optional[threading.Thread] = None
        self._browser_req_id = 0
        if self._playwright_available:
            self._bt = threading.Thread(target=self._browser_loop, daemon=True, name="BrowserThread")
            self._bt.start()

        # ── Worker ──
        self._worker: Optional[threading.Thread] = None
        self._pause  = threading.Event(); self._pause.set()
        self._stop   = threading.Event()

        self._chunks: List = []
        self._output_mgr = None
        self._session_state = None

        # ── Pipeline State ──
        self.pl_host = tk.StringVar(value=self.config.get("pl_host", "127.0.0.1"))
        self.pl_port = tk.StringVar(value=str(self.config.get("pl_port", "8777")))
        self.pl_api_key = tk.StringVar(value=self.config.get("pl_api_key", ""))
        self.pl_img_model = tk.StringVar(value=self.config.get("pl_img_model", "imagen4"))
        self.pl_vid_model = tk.StringVar(value=_normalize_video_model(self.config.get("pl_vid_model", DEFAULT_VIDEO_MODEL)))
        self.pl_aspect = tk.StringVar(value=self.config.get("pl_aspect", "16:9"))
        self.pl_res = tk.StringVar(value=self.config.get("pl_res", "1080p"))
        self.pl_category = tk.StringVar(value=self.config.get("pl_category", "scene"))
        self.pl_threads = tk.StringVar(value=str(self.config.get("pl_image_threads", self.config.get("pl_threads", "3"))))
        self.pl_video_threads = tk.StringVar(value=str(self.config.get("pl_video_threads", "5")))
        self.pl_run_step1 = tk.BooleanVar(value=self.config.get("pl_run_step1", True))
        self.pl_run_step2 = tk.BooleanVar(value=self.config.get("pl_run_step2", True))
        self.pl_run_step3 = tk.BooleanVar(value=self.config.get("pl_run_step3", True))
        
        self.pl_whisk_file = tk.StringVar(value="")
        self.pl_veo_file = tk.StringVar(value="")
        self.pl_luot2_file = tk.StringVar(value="")
        self.pl_ref_image = tk.StringVar(value=self.config.get("pl_ref_image", self.config.get("ref_image", "")))
        self.pl_img_model2 = tk.StringVar(value=self.config.get("pl_img_model_2", self.config.get("pl_img_model", "imagen4")))
        self.pl_vid_model2 = tk.StringVar(value=_normalize_video_model(self.config.get("pl_vid_model_2", self.config.get("pl_vid_model", DEFAULT_VIDEO_MODEL))))
        self.pl_aspect2 = tk.StringVar(value=self.config.get("pl_aspect_2", self.config.get("pl_aspect", "16:9")))
        self.pl_res2 = tk.StringVar(value=self.config.get("pl_res_2", self.config.get("pl_res", "1080p")))
        self.pl_category2 = tk.StringVar(value=self.config.get("pl_category_2", self.config.get("pl_category", "scene")))
        self.pl_threads2 = tk.StringVar(value=str(self.config.get("pl_image_threads_2", self.config.get("pl_threads_2", self.config.get("pl_threads", "3")))))
        self.pl_video_threads2 = tk.StringVar(value=str(self.config.get("pl_video_threads_2", self.config.get("pl_video_threads", "5"))))
        self.pl_run_step12 = tk.BooleanVar(value=self.config.get("pl_run_step1_2", self.config.get("pl_run_step1", True)))
        self.pl_run_step22 = tk.BooleanVar(value=self.config.get("pl_run_step2_2", self.config.get("pl_run_step2", True)))
        self.pl_run_step32 = tk.BooleanVar(value=self.config.get("pl_run_step3_2", self.config.get("pl_run_step3", True)))
        self.pl_whisk_file2 = tk.StringVar(value="")
        self.pl_veo_file2 = tk.StringVar(value="")
        self.pl_luot2_file2 = tk.StringVar(value="")
        self.pl_ref_image2 = tk.StringVar(value=self.config.get("pl_ref_image_2", self.config.get("pl_ref_image", self.config.get("ref_image", ""))))
        
        self.pl_is_running = False
        self.pl_is_paused = False
        self.pipeline_engine = None
        self.pl_engines = {}  # {job_name: PipelineEngine} — tracks active engines per file
        
        # File 1 state
        self.pl_failed_items = {}
        self.pl_failed_tree = None
        self.btn_pl_retry_failed = None
        self.pl_retry_engines = {}
        self.pl_retry_running_items = set()
        
        # File 2 state
        self.pl_failed_items2 = {}
        self.pl_failed_tree2 = None
        self.btn_pl_retry_failed2 = None
        self.pl_retry_engines2 = {}
        self.pl_retry_running_items2 = set()
        
        self.pl_retry_is_running = False # Global retry flag (or can be separate)
        self._pl_edit_entry = None

        self._build_ui()
        self._bind_auto_save_settings()
        if not self._playwright_available:
            self.log("Addon Extension mode đang hoạt động. Playwright không được cài nên các nút điều khiển browser kiểu cũ sẽ bị bỏ qua.", "INFO")
        
        # Load preset ban đầu
        last_preset = self.config.get("last_preset_name", "")
        if last_preset and last_preset in self._saved_urls:
            self.v_url.set(last_preset)
        if self.v_url2.get() not in self._saved_urls:
            self.v_url2.set(self.v_url.get())
        self._on_url_selected()
        self.pl_host.set(self.config.get("pl_host", self.pl_host.get()))
        self.pl_port.set(str(self.config.get("pl_port", self.pl_port.get())))
        self.pl_api_key.set(self.config.get("pl_api_key", self.pl_api_key.get()))
        self.pl_img_model.set(self.config.get("pl_img_model", self.pl_img_model.get()))
        self.pl_vid_model.set(_normalize_video_model(self.config.get("pl_vid_model", self.pl_vid_model.get())))
        self.pl_aspect.set(self.config.get("pl_aspect", self.pl_aspect.get()))
        self.pl_res.set(self.config.get("pl_res", self.pl_res.get()))
        self.pl_category.set(self.config.get("pl_category", self.pl_category.get()))
        self.pl_threads.set(str(self.config.get("pl_image_threads", self.config.get("pl_threads", self.pl_threads.get()))))
        self.pl_video_threads.set(str(self.config.get("pl_video_threads", self.pl_video_threads.get())))
        self.pl_run_step1.set(self.config.get("pl_run_step1", self.pl_run_step1.get()))
        self.pl_run_step2.set(self.config.get("pl_run_step2", self.pl_run_step2.get()))
        self.pl_run_step3.set(self.config.get("pl_run_step3", self.pl_run_step3.get()))
        self.pl_ref_image.set(self.config.get("pl_ref_image", self.pl_ref_image.get()))
        self.pl_img_model2.set(self.config.get("pl_img_model_2", self.pl_img_model2.get()))
        self.pl_vid_model2.set(_normalize_video_model(self.config.get("pl_vid_model_2", self.pl_vid_model2.get())))
        self.pl_aspect2.set(self.config.get("pl_aspect_2", self.pl_aspect2.get()))
        self.pl_res2.set(self.config.get("pl_res_2", self.pl_res2.get()))
        self.pl_category2.set(self.config.get("pl_category_2", self.pl_category2.get()))
        self.pl_threads2.set(str(self.config.get("pl_image_threads_2", self.config.get("pl_threads_2", self.pl_threads2.get()))))
        self.pl_video_threads2.set(str(self.config.get("pl_video_threads_2", self.pl_video_threads2.get())))
        self.pl_run_step12.set(self.config.get("pl_run_step1_2", self.pl_run_step12.get()))
        self.pl_run_step22.set(self.config.get("pl_run_step2_2", self.pl_run_step22.get()))
        self.pl_run_step32.set(self.config.get("pl_run_step3_2", self.pl_run_step32.get()))
        self.pl_ref_image2.set(self.config.get("pl_ref_image_2", self.pl_ref_image2.get()))
        self._ui_ready = True
        
        self._poll_log()

    # ─────────────────────────────────────────────
    # BrowserThread & queue helper
    # ─────────────────────────────────────────────

    def _browser_loop(self):
        """
        Vòng lặp duy nhất chạy Playwright.
        Tất cả lệnh browser đi qua _bcmd_q, kết quả trả về _bres_q.
        """
        from automation.gemini_controller import GeminiController
        ctrl = GeminiController(self.config, self.selectors)

        while True:
            try:
                item = self._bcmd_q.get(timeout=0.3)
            except queue.Empty:
                continue

            if len(item) == 4:
                req_id, cmd, args, kwargs = item
            else:
                req_id, cmd, args, kwargs = None, item[0], item[1], item[2]

            if cmd == "__STOP__":
                try:
                    ctrl.close()
                except Exception:
                    pass
                break

            try:
                result = getattr(ctrl, cmd)(*args, **kwargs)
                self._bres_q.put((req_id, "OK", result))
            except Exception as exc:
                self._bres_q.put((req_id, "ERR", exc))

    def _bc(self, cmd: str, *args, timeout: int = 60, **kwargs):
        """Gửi lệnh tới BrowserThread và chờ kết quả (blocking)."""
        if not self._playwright_available or not self._bt:
            raise RuntimeError("Playwright không được cài. App đang chạy ở chế độ Addon Extension.")
        self._browser_req_id += 1
        req_id = self._browser_req_id
        self._bcmd_q.put((req_id, cmd, args, kwargs))
        try:
            got_req_id, status, val = self._bres_q.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError(f"BrowserThread không phản hồi lệnh '{cmd}' sau {timeout}s")
        while got_req_id != req_id:
            logger.warning("Bo qua ket qua BrowserThread cu (req=%s, current=%s).", got_req_id, req_id)
            try:
                got_req_id, status, val = self._bres_q.get(timeout=timeout)
            except queue.Empty:
                raise TimeoutError(f"BrowserThread khong phan hoi lenh '{cmd}' sau {timeout}s")
        if status == "ERR":
            raise val
        return val

    def _send_chunk_via_addon(
        self,
        job_id: str,
        chunk_index: int,
        content: str,
        expected_lines: Optional[int],
        baseline_text: str,
        initial_url: str = ""
    ):
        """Gửi chunk đến Chrome Extension thông qua Local HTTP Server và đợi kết quả."""
        from typing import Tuple
        job = BRIDGE_JOBS[job_id]
        job["result"] = None
        job["event"].clear()
        
        # Đặt chunk mới vào hàng đợi poll
        job["pending_chunk"] = {
            "cmd": "send_chunk",
            "chunk_index": chunk_index,
            "content": content,
            "expected_lines": expected_lines,
            "baseline_text": baseline_text,
            "initial_url": initial_url
        }
        
        # Chờ Extension lấy chunk, thực thi và trả kết quả về (Timeout 15 phút)
        completed = job["event"].wait(timeout=900)
        job["pending_chunk"] = None  # Xóa sạch hàng đợi khi đã hoàn tất xử lý
        
        if not completed:
            return False, {"text": "Hết thời gian chờ phản hồi từ Extension (Timeout 15 phút)!", "blocks": [], "retry": False}
            
        res = job["result"]
        if not res:
            return False, {"text": "Lỗi không xác định khi nhận kết quả từ Extension!", "blocks": [], "retry": False}
            
        if res.get("status") == "success":
            return True, {"text": res.get("text", ""), "blocks": res.get("blocks", []), "retry": False}
        elif res.get("status") == "failed_retry":
            return False, {"text": res.get("error", "Số dòng không khớp!"), "blocks": res.get("blocks", []), "retry": True}
        else:
            return False, {"text": res.get("error", "Lỗi gửi chunk!"), "blocks": [], "retry": False}

    # ─────────────────────────────────────────────
    # Build UI
    # ─────────────────────────────────────────────

    def _build_ui(self):
        # Header
        hdr = tk.Frame(self, bg=C["surface"], pady=10)
        hdr.pack(fill="x")
        tk.Label(hdr, text="🤖  Z115_DHVIPPRO",
                 font=("Segoe UI Semibold", 16), fg=C["accent"], bg=C["surface"]).pack(side="left", padx=20)

        # Style notebook
        style = ttk.Style()
        style.theme_use('default')
        style.configure('TNotebook', background=C["bg"], borderwidth=0)
        style.configure('TNotebook.Tab', background=C["surface"], foreground=C["text"], padding=[20, 5], font=("Segoe UI Semibold", 11))
        style.map('TNotebook.Tab', background=[('selected', C["accent2"])])

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=10, pady=10)

        # Tab 1: Bước 1 & 2
        tab1 = tk.Frame(self.notebook, bg=C["bg"])
        self.notebook.add(tab1, text=" Bước 1 & 2: Audio & Gemini ")

        # Tab 2: Bước 3
        tab2 = tk.Frame(self.notebook, bg=C["bg"])
        self.notebook.add(tab2, text=" Bước 3: Pipeline Tạo Ảnh & Video ")

        top_workflow_bar = tk.Frame(tab1, bg=C["surface"], padx=8, pady=6)
        top_workflow_bar.pack(fill="x", pady=(0, 6))
        self._top_controls(top_workflow_bar)

        scroll_host = tk.Frame(tab1, bg=C["bg"])
        scroll_host.pack(fill="x", pady=(0, 6))
        content_canvas = tk.Canvas(scroll_host, bg=C["bg"], height=430, highlightthickness=0, bd=0)
        content_scroll = ttk.Scrollbar(scroll_host, orient="vertical", command=content_canvas.yview)
        content_wrap = tk.Frame(content_canvas, bg=C["bg"])
        content_window = content_canvas.create_window((0, 0), window=content_wrap, anchor="nw")
        content_canvas.configure(yscrollcommand=content_scroll.set)

        def _sync_content_scroll(_event=None):
            content_canvas.configure(scrollregion=content_canvas.bbox("all"))

        def _sync_content_width(event):
            content_canvas.itemconfigure(content_window, width=event.width)

        def _content_mousewheel(event):
            content_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        content_wrap.bind("<Configure>", _sync_content_scroll)
        content_canvas.bind("<Configure>", _sync_content_width)
        content_canvas.bind("<Enter>", lambda _event: content_canvas.bind_all("<MouseWheel>", _content_mousewheel))
        content_canvas.bind("<Leave>", lambda _event: content_canvas.unbind_all("<MouseWheel>"))
        content_scroll.pack(side="right", fill="y")
        content_canvas.pack(side="left", fill="x", expand=True)

        inputs = tk.Frame(content_wrap, bg=C["bg"])
        inputs.pack(fill="x", pady=(0, 6))
        self._left(inputs)

        status = tk.Frame(content_wrap, bg=C["bg"])
        status.pack(fill="x", pady=(0, 6))
        self._right(status)

        self._log_panel(tab1)
        
        self._build_pipeline_ui(tab2)

    def _left(self, p):
        def panel(parent, title):
            box = tk.Frame(parent, bg=C["bg"], highlightthickness=1, highlightbackground=C["border"])
            tk.Label(box, text=title, font=("Segoe UI Semibold", 10), fg=C["accent"], bg=C["bg"]).pack(anchor="w", padx=8, pady=(6, 4))
            return box

        def file_picker(parent, label, var, cmd):
            row = tk.Frame(parent, bg=C["bg"])
            row.pack(fill="x", padx=8, pady=(0, 5))
            tk.Label(row, text=label, font=FS, fg=C["dim"], bg=C["bg"], width=7, anchor="w").pack(side="left")
            _entry(row, var=var).pack(side="left", fill="x", expand=True)
            _btn(row, "...", cmd, width=4).pack(side="right", padx=(4, 0))

        def audio_panel(parent, title, script_var, script_cmd, audio_var, browse_cmd, model_var, engine_var, lang_var, beam_var, first4_var, omnivoice_cmd=None):
            box = panel(parent, title)
            file_picker(box, "Kịch bản:", script_var, script_cmd)
            file_picker(box, "Audio:", audio_var, browse_cmd)
            opts = tk.Frame(box, bg=C["bg"])
            opts.pack(fill="x", padx=8, pady=(0, 5))
            ttk.Combobox(opts, textvariable=model_var, values=["tiny", "small", "medium", "large", "large-v2", "large-v3"], width=10, font=FS).pack(side="left", padx=(0, 5))
            ttk.Combobox(opts, textvariable=engine_var, values=["openai-whisper", "faster-whisper", "auto"], state="readonly", width=16, font=FS).pack(side="left", padx=(0, 5))
            ttk.Combobox(opts, textvariable=lang_var, values=["Tiếng Hàn (ko)", "Tiếng Nhật (ja)", "Tiếng Anh (en)", "Tiếng Trung Phồn Thể (zh-tw)", "Auto"], state="readonly", width=22, font=FS).pack(side="left")
            tuning = tk.Frame(box, bg=C["bg"])
            tuning.pack(fill="x", padx=8, pady=(0, 7))
            tk.Label(tuning, text="Beam Size:", font=FS, fg=C["text"], bg=C["bg"]).pack(side="left", padx=(0, 4))
            ttk.Combobox(tuning, textvariable=beam_var, values=[1, 2, 3, 4, 5, 10], width=4, font=FS).pack(side="left")
            tk.Label(tuning, text="4 phút đầu:", font=FS, fg=C["text"], bg=C["bg"]).pack(side="left", padx=(12, 4))
            ttk.Combobox(tuning, textvariable=first4_var, values=["8", "10"], state="readonly", width=4, font=FS).pack(side="left")
            tk.Label(tuning, text="giây", font=FS, fg=C["dim"], bg=C["bg"]).pack(side="left", padx=(4, 0))
            if omnivoice_cmd:
                ov_row = tk.Frame(box, bg=C["bg"])
                ov_row.pack(fill="x", padx=8, pady=(0, 7))
                _btn(ov_row, "⚙️ Cài đặt OmniVoice", omnivoice_cmd, bg="#3a3a5c").pack(side="left")
            return box

        audio_row = tk.Frame(p, bg=C["bg"])
        audio_row.pack(fill="x", padx=2, pady=(0, 6))
        audio_row.grid_columnconfigure(0, weight=1, uniform="audio")
        audio_row.grid_columnconfigure(1, weight=1, uniform="audio")
        audio_panel(audio_row, "🎙️ Tạo/Trích Xuất Audio 1", self.v_script, self._browse_script, self.v_audio, self._browse_audio, self.v_aud_model, self.v_aud_engine, self.v_aud_lang, self.v_aud_beam, self.v_aud_first4, lambda: self._open_omnivoice_settings(1)).grid(row=0, column=0, sticky="ew", padx=(0, 4))
        audio_panel(audio_row, "🎙️ Tạo/Trích Xuất Audio 2", self.v_script2, self._browse_script2, self.v_audio2, self._browse_audio2, self.v_aud_model2, self.v_aud_engine2, self.v_aud_lang2, self.v_aud_beam2, self.v_aud_first42, lambda: self._open_omnivoice_settings(2)).grid(row=0, column=1, sticky="ew", padx=(4, 0))

        txt_box = panel(p, "📂 Chọn File TXT (Kết quả)")
        txt_box.pack(fill="x", padx=2, pady=(0, 6))
        txt_grid = tk.Frame(txt_box, bg=C["bg"])
        txt_grid.pack(fill="x", padx=0, pady=(0, 4))
        txt_grid.grid_columnconfigure(0, weight=1, uniform="txt")
        txt_grid.grid_columnconfigure(1, weight=1, uniform="txt")
        txt1 = tk.Frame(txt_grid, bg=C["bg"]); txt1.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        txt2 = tk.Frame(txt_grid, bg=C["bg"]); txt2.grid(row=0, column=1, sticky="ew", padx=(4, 0))
        file_picker(txt1, "File 1:", self.v_file, self._browse)
        file_picker(txt2, "File 2:", self.v_file2, self._browse2)

        preset_box = panel(p, "📂 Chọn Thể Loại (Preset)")
        preset_box.pack(fill="x", padx=2, pady=(0, 6))
        preset_grid = tk.Frame(preset_box, bg=C["bg"])
        preset_grid.pack(fill="x", padx=8, pady=(0, 5))
        preset_grid.grid_columnconfigure(0, weight=1, uniform="preset")
        preset_grid.grid_columnconfigure(1, weight=1, uniform="preset")
        p1 = tk.Frame(preset_grid, bg=C["bg"]); p1.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        p2 = tk.Frame(preset_grid, bg=C["bg"]); p2.grid(row=0, column=1, sticky="ew", padx=(4, 0))
        tk.Label(p1, text="Thể loại file 1:", font=FS, fg=C["dim"], bg=C["bg"]).pack(anchor="w")
        self.url_combo = ttk.Combobox(p1, textvariable=self.v_url, values=list(self._saved_urls.keys()), font=FB, state="normal")
        self.url_combo.pack(fill="x")
        self.url_combo.bind("<<ComboboxSelected>>", self._on_url_selected)
        tk.Label(p2, text="Thể loại file 2:", font=FS, fg=C["dim"], bg=C["bg"]).pack(anchor="w")
        self.url_combo2 = ttk.Combobox(p2, textvariable=self.v_url2, values=list(self._saved_urls.keys()), font=FB, state="readonly")
        self.url_combo2.pack(fill="x")
        self.url_combo2.bind("<<ComboboxSelected>>", self._on_url2_selected)
        br = tk.Frame(preset_box, bg=C["bg"])
        br.pack(fill="x", padx=8, pady=(0, 6))
        _btn(br, "➕ Thêm/Lưu Thể Loại", self._add_url, bg=C["teal"]).pack(side="left", fill="x", expand=True, padx=(0, 4))
        _btn(br, "➖ Xóa Thể Loại", self._remove_url, bg="#5a2020").pack(side="right")

        bottom = tk.Frame(p, bg=C["bg"])
        bottom.pack(fill="x", padx=2, pady=(0, 2))
        bottom.grid_columnconfigure(0, weight=1, uniform="bottom")
        bottom.grid_columnconfigure(1, weight=1, uniform="bottom")

        cfg = self._collapsible_panel(bottom, "⚙️ Cấu Hình File 1", default_open=False)
        cfg["box"].grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        frm = tk.Frame(cfg["body"], bg=C["bg"])
        frm.pack(fill="x", padx=8, pady=(0, 6))
        self._row(frm, "Dòng chunk đầu tiên:", self.v_first)
        self._row(frm, "Dòng các chunk sau:", self.v_next)
        self._row(frm, "Tên model cần chọn:", self.v_model)
        self._row(frm, "Giây nghỉ giữa chunks:", self.v_delayt)
        for t, v in [("Bỏ qua dòng trống", self.v_skip), ("Lưu response", self.v_save), ("Tạm dừng giữa chunks", self.v_delay)]:
            self._chk(frm, t, v)

        cfg2 = self._collapsible_panel(bottom, "⚙️ Cấu Hình File 2", default_open=False)
        cfg2["box"].grid(row=0, column=1, sticky="nsew", padx=(4, 0))
        frm2 = tk.Frame(cfg2["body"], bg=C["bg"])
        frm2.pack(fill="x", padx=8, pady=(0, 6))
        self._row(frm2, "Dòng chunk đầu tiên:", self.v_first2)
        self._row(frm2, "Dòng các chunk sau:", self.v_next2)
        self._row(frm2, "Tên model cần chọn:", self.v_model2)
        self._row(frm2, "Giây nghỉ giữa chunks:", self.v_delayt2)
        for t, v in [("Bỏ qua dòng trống", self.v_skip2), ("Lưu response", self.v_save2), ("Tạm dừng giữa chunks", self.v_delay2)]:
            self._chk(frm2, t, v)

        browser_cfg = self._collapsible_panel(p, "🌐 Trình Duyệt Mở Gemini", default_open=False)
        browser_cfg["box"].pack(fill="x", padx=2, pady=(0, 6))
        bf = tk.Frame(browser_cfg["body"], bg=C["bg"])
        bf.pack(fill="x", padx=8, pady=(0, 6))
        
        row = tk.Frame(bf, bg=C["bg"])
        row.pack(fill="x", pady=2)
        tk.Label(row, text="Đường dẫn Edge/Chrome:", font=FS, fg=C["dim"], bg=C["bg"]).pack(side="left")
        
        def _br_chrome():
            p_file = filedialog.askopenfilename(filetypes=[("Executable files", "*.exe")])
            if p_file:
                self.v_custom_browser_path.set(p_file)
                self.config["custom_browser_path"] = p_file
                self._save_all_settings()
            
        _btn(row, "...", _br_chrome, width=3).pack(side="right", padx=(4, 0))
        _entry(row, var=self.v_custom_browser_path).pack(side="right", fill="x", expand=True, padx=4)

        tools = self._collapsible_panel(p, "🔧 Công Cụ", default_open=False)
        tools["box"].pack(fill="x", padx=2, pady=(6, 2))
        af = tk.Frame(tools["body"], bg=C["bg"])
        af.pack(fill="x", padx=8, pady=(0, 6))
        for i, (text, cmd) in enumerate([
            ("🔍 Dry Run", self._dry_run),
            ("🧪 Test Selectors", self._test_sel),
            ("🎯 Model Pro", self._sel_model),
            ("🔁 Gửi lại chunk", self._retry_chunk),
            ("📁 Mở Output", self._open_output),
        ]):
            _btn(af, text, cmd).grid(row=i // 2, column=i % 2, sticky="ew", padx=2, pady=2)
        af.grid_columnconfigure(0, weight=1)
        af.grid_columnconfigure(1, weight=1)

        # ── Bridge Status Panel ──
        bridge_panel = self._collapsible_panel(p, "🌐 Trạng Thái Extension (Tab Chỉ Định)", default_open=True)
        bridge_panel["box"].pack(fill="x", padx=2, pady=(6, 2))
        bf2 = tk.Frame(bridge_panel["body"], bg=C["bg"])
        bf2.pack(fill="x", padx=8, pady=(0, 8))

        self._bridge_status_vars = {}
        for job_id, label in [("file1", "File 1"), ("file2", "File 2")]:
            row = tk.Frame(bf2, bg=C["bg"])
            row.pack(fill="x", pady=3)
            dot_var = tk.StringVar(value="🔴")
            info_var = tk.StringVar(value="Chưa có tab nào kết nối")
            tk.Label(row, textvariable=dot_var, font=FS, bg=C["bg"], width=2).pack(side="left")
            tk.Label(row, text=f"{label}:", font=FS, fg=C["dim"], bg=C["bg"], width=7, anchor="w").pack(side="left")
            tk.Label(row, textvariable=info_var, font=FC, fg=C["text"], bg=C["bg"]).pack(side="left", fill="x", expand=True)
            def _make_reset(jid=job_id):
                def _reset():
                    try:
                        import urllib.request as _ur
                        _ur.urlopen(f"http://127.0.0.1:8779/undesignate?job={jid}", timeout=2)
                        self.log(f"[Bridge] Đã reset designation cho {jid}.", "WARNING")
                    except Exception as e:
                        self.log(f"[Bridge] Lỗi reset: {e}", "ERROR")
                return _reset
            _btn(row, "Reset", _make_reset(), bg="#5a2020", width=6).pack(side="right", padx=(4, 0))
            self._bridge_status_vars[job_id] = (dot_var, info_var)

        self._poll_bridge_status()

    def _poll_bridge_status(self):
        """Cập nhật trạng thái tab chỉ định mỗi 3 giây."""
        try:
            import urllib.request as _ur, json as _json
            with _ur.urlopen("http://127.0.0.1:8779/status", timeout=1) as resp:
                data = _json.loads(resp.read())
            for job_id, (dot_var, info_var) in getattr(self, "_bridge_status_vars", {}).items():
                info = data.get(job_id, {})
                tab_short = info.get("tab_short")
                alive = info.get("alive", False)
                last_seen = info.get("last_seen")
                if alive and tab_short:
                    dot_var.set("🟢")
                    info_var.set(f"{tab_short}  (kết nối {last_seen}s trước)")
                elif tab_short:
                    dot_var.set("🟡")
                    info_var.set(f"{tab_short}  (mất kết nối {last_seen}s)")
                else:
                    dot_var.set("🔴")
                    info_var.set("Chưa có tab nào kết nối")
        except Exception:
            pass
        self.after(3000, self._poll_bridge_status)

    def _top_controls(self, parent):
        wf = tk.Frame(parent, bg=C["surface"])
        wf.pack(side="left", fill="x", expand=True, padx=(0, 8))
        tk.Label(wf, text="Bước chạy:", font=("Segoe UI Semibold", 9), fg=C["accent"], bg=C["surface"]).pack(side="left", padx=(0, 8))
        for text, var in [
            ("B1 Audio", self.v_do_audio),
            ("B2 Gemini", self.v_do_gemini),
            ("B3 Pipeline", self.v_do_pipeline),
        ]:
            tk.Checkbutton(
                wf, text=text, variable=var, command=self._save_all_settings,
                bg=C["surface"], fg=C["text"], activebackground=C["surface"],
                selectcolor=C["surface2"], font=FS
            ).pack(side="left", padx=(0, 4))
            
        profile_frame = tk.Frame(wf, bg=C["surface"])
        profile_frame.pack(side="left", padx=(10, 0))
        tk.Label(profile_frame, text="Hồ sơ Bot:", font=("Segoe UI Semibold", 9), fg=C["text"], bg=C["surface"]).pack(side="left", padx=(0, 4))
        
        self.profile_combo = ttk.Combobox(profile_frame, textvariable=self.v_internal_profile, values=self.internal_profiles, font=FS, state="readonly", width=10)
        self.profile_combo.pack(side="left", padx=0)
        self.profile_combo.bind("<<ComboboxSelected>>", lambda e: self._save_all_settings())

        controls = tk.Frame(parent, bg=C["surface"])
        controls.pack(side="right")
        for text, cmd, bg in [
            ("▶ Bắt đầu", self._start, C["ok"]),
            ("🔁 Gửi lại chunk", self._retry_chunk, "#e67e22"),
            ("🔌 Khởi động lại", self._restart_app, "#8e44ad"),
            ("⏹ Dừng", self._do_stop, C["err"]),
        ]:
            btn = _btn(controls, text, cmd, bg=bg)
            btn.config(padx=7, pady=3)
            btn.pack(side="left", padx=2)

    def _right(self, p):
        wrap = tk.Frame(p, bg=C["bg"])
        wrap.pack(fill="x")
        wrap.grid_columnconfigure(0, weight=1, uniform="progress")
        wrap.grid_columnconfigure(1, weight=1, uniform="progress")
        self._build_job_progress_panel(
            wrap, "Tiến trình File 1",
            self.v_lines, self.v_chunks, self.v_cur, self.v_status,
            "pbar", "plbl", "preview", "chunk_info",
            0
        )
        self._build_job_progress_panel(
            wrap, "Tiến trình File 2",
            self.v_lines2, self.v_chunks2, self.v_cur2, self.v_status2,
            "pbar2", "plbl2", "preview2", "chunk_info2",
            1
        )

    def _build_job_progress_panel(self, parent, title, lines_var, chunks_var, cur_var, status_var, pbar_attr, label_attr, preview_attr, info_attr, col):
        box = tk.Frame(parent, bg=C["bg"], highlightthickness=1, highlightbackground=C["border"])
        box.grid(row=0, column=col, sticky="nsew", padx=(0, 4) if col == 0 else (4, 0))
        tk.Label(box, text=title, font=("Segoe UI Semibold", 10), fg=C["accent"], bg=C["bg"]).pack(anchor="w", padx=8, pady=(6, 2))
        sf = tk.Frame(box, bg=C["surface"], padx=8, pady=6)
        sf.pack(fill="x", padx=8, pady=(0, 6))
        for i, (lbl, var) in enumerate([
            ("Tổng dòng", lines_var),
            ("Tổng chunk", chunks_var),
            ("Chunk", cur_var),
            ("Trạng thái", status_var),
        ]):
            cell = tk.Frame(sf, bg=C["surface"])
            cell.grid(row=0, column=i, padx=(0, 12), sticky="w")
            tk.Label(cell, text=lbl, font=FS, fg=C["dim"], bg=C["surface"]).pack(anchor="w")
            tk.Label(cell, textvariable=var, font=("Segoe UI Semibold", 10), fg=C["accent"], bg=C["surface"]).pack(anchor="w")

        pf = tk.Frame(box, bg=C["bg"])
        pf.pack(fill="x", padx=8, pady=(0, 4))
        tk.Label(pf, text="Tiến Trình:", font=FS, fg=C["dim"], bg=C["bg"]).pack(anchor="w")
        pbar = ttk.Progressbar(pf, orient="horizontal", mode="determinate")
        pbar.pack(fill="x", pady=2)
        plbl = tk.Label(pf, text="0 / 0", font=FS, fg=C["dim"], bg=C["bg"])
        plbl.pack(anchor="e")

        pr = tk.Frame(box, bg=C["bg"])
        pr.pack(fill="x", padx=8)
        tk.Label(pr, text="📋 Nội Dung Chunk:", font=FS, fg=C["dim"], bg=C["bg"]).pack(side="left")
        info = tk.Label(pr, text="", font=FS, fg=C["accent"], bg=C["bg"])
        info.pack(side="right")
        preview = scrolledtext.ScrolledText(box, height=4, state="disabled",
                                             bg=C["surface2"], fg=C["text"], font=FC, relief="flat", bd=0)
        preview.pack(fill="x", expand=False, padx=8, pady=(2, 8))
        setattr(self, pbar_attr, pbar)
        setattr(self, label_attr, plbl)
        setattr(self, preview_attr, preview)
        setattr(self, info_attr, info)

    def _log_panel(self, p):
        logs = tk.Frame(p, bg=C["bg"])
        logs.pack(fill="both", expand=True)
        logs.grid_rowconfigure(0, weight=1)
        logs.grid_columnconfigure(0, weight=1, uniform="joblog")
        logs.grid_columnconfigure(1, weight=1, uniform="joblog")
        for col, (title, attr) in enumerate([("📜 Log File 1", "log_file1_w"), ("📜 Log File 2", "log_file2_w")]):
            box = tk.Frame(logs, bg=C["bg"])
            box.grid(row=0, column=col, sticky="nsew", padx=(0, 4) if col == 0 else (4, 0))
            tk.Label(box, text=title, font=FS, fg=C["dim"], bg=C["bg"]).pack(anchor="w")
            widget = scrolledtext.ScrolledText(box, height=12, state="disabled",
                                                bg="#0d0d1a", fg=C["text"], font=FC, relief="flat", bd=0)
            widget.pack(fill="both", expand=True)
            for tag, color in [("INFO",C["text"]),("SUCCESS",C["ok"]),("WARNING",C["warn"]),
                               ("ERROR",C["err"]),("DEBUG",C["dim"])]:
                widget.tag_config(tag, foreground=color)
            setattr(self, attr, widget)
        self.log_w = self.log_file1_w

    def _clear_job_logs(self):
        for attr in ("log_file1_w", "log_file2_w"):
            widget = getattr(self, attr, None)
            if not widget:
                continue
            widget.config(state="normal")
            widget.delete("1.0", "end")
            widget.config(state="disabled")

    # ─────────────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────────────

    def _sec(self, p, t):
        tk.Label(p, text=t, font=("Segoe UI Semibold",10), fg=C["accent"], bg=C["bg"]).pack(anchor="w", padx=8, pady=(10,2))

    def _pl_sec(self, p, t):
        tk.Label(p, text=t, font=("Segoe UI Semibold",9), fg=C["accent"], bg=C["bg"]).pack(anchor="w", padx=8, pady=(5,1))

    def _collapsible_panel(self, parent, title, default_open=False):
        box = tk.Frame(parent, bg=C["bg"], highlightthickness=1, highlightbackground=C["border"])
        header = tk.Label(
            box,
            text=f"{'▼' if default_open else '▶'} {title}",
            font=("Segoe UI Semibold", 10),
            fg=C["accent"],
            bg=C["bg"],
            cursor="hand2",
        )
        header.pack(fill="x", anchor="w", padx=8, pady=(6, 6))
        body = tk.Frame(box, bg=C["bg"])
        is_open = tk.BooleanVar(value=default_open)

        def toggle(_event=None):
            if is_open.get():
                body.pack_forget()
                header.config(text=f"▶ {title}")
                is_open.set(False)
            else:
                body.pack(fill="x")
                header.config(text=f"▼ {title}")
                is_open.set(True)

        header.bind("<Button-1>", toggle)
        if default_open:
            body.pack(fill="x")
        return {"box": box, "body": body, "toggle": toggle}

    def _row(self, p, lbl, var):
        row = tk.Frame(p, bg=C["bg"]); row.pack(fill="x", pady=1)
        tk.Label(row, text=lbl, font=FS, fg=C["dim"], bg=C["bg"], width=22, anchor="w").pack(side="left")
        _entry(row, var=var, width=8).pack(side="right")

    def _chk(self, p, lbl, var):
        row = tk.Frame(p, bg=C["bg"]); row.pack(fill="x", pady=2)
        tk.Checkbutton(row, text=lbl, variable=var,
                       bg=C["bg"], fg=C["text"], activebackground=C["bg"],
                       selectcolor=C["surface2"], font=FS).pack(anchor="w")

    def _bind_auto_save_settings(self):
        watched = [
            (self.v_model, "Gemini model"),
            (self.v_model2, "Gemini model 2"),
            (self.v_first, "First chunk size"),
            (self.v_first2, "First chunk size 2"),
            (self.v_next, "Next chunk size"),
            (self.v_next2, "Next chunk size 2"),
            (self.v_skip, "Skip empty lines"),
            (self.v_skip2, "Skip empty lines 2"),
            (self.v_delayt, "Delay between chunks"),
            (self.v_delayt2, "Delay between chunks 2"),
            (self.v_aud_model, "Whisper model"),
            (self.v_aud_engine, "Whisper engine"),
            (self.v_aud_lang, "Audio language"),
            (self.v_aud_beam, "Beam size"),
            (self.v_aud_first4, "Audio first 4 minutes chunk seconds"),
            (self.v_aud_model2, "Whisper model 2"),
            (self.v_aud_engine2, "Whisper engine 2"),
            (self.v_aud_lang2, "Audio language 2"),
            (self.v_aud_beam2, "Beam size 2"),
            (self.v_aud_first42, "Audio first 4 minutes chunk seconds 2"),
            (self.v_url2, "Preset file 2"),
            (self.v_do_audio, "Workflow step 1"),
            (self.v_do_gemini, "Workflow step 2"),
            (self.v_do_pipeline, "Workflow step 3"),
            (self.pl_host, "Pipeline host"),
            (self.pl_port, "Pipeline port"),
            (self.pl_api_key, "Pipeline API key"),
            (self.pl_img_model, "Pipeline image model"),
            (self.pl_vid_model, "Pipeline video model"),
            (self.pl_aspect, "Pipeline aspect ratio"),
            (self.pl_res, "Pipeline resolution"),
            (self.pl_category, "Pipeline reference type"),
            (self.pl_threads, "Pipeline image threads"),
            (self.pl_video_threads, "Pipeline video threads"),
            (self.pl_run_step1, "Pipeline run step 1"),
            (self.pl_run_step2, "Pipeline run step 2"),
            (self.pl_run_step3, "Pipeline run step 3"),
            (self.pl_ref_image, "Pipeline reference image"),
            (self.pl_img_model2, "Pipeline image model 2"),
            (self.pl_vid_model2, "Pipeline video model 2"),
            (self.pl_aspect2, "Pipeline aspect ratio 2"),
            (self.pl_res2, "Pipeline resolution 2"),
            (self.pl_category2, "Pipeline reference type 2"),
            (self.pl_threads2, "Pipeline image threads 2"),
            (self.pl_video_threads2, "Pipeline video threads 2"),
            (self.pl_run_step12, "Pipeline run step 1 file 2"),
            (self.pl_run_step22, "Pipeline run step 2 file 2"),
            (self.pl_run_step32, "Pipeline run step 3 file 2"),
            (self.pl_ref_image2, "Pipeline reference image 2"),
            (self.v_custom_browser_path, "Custom browser path"),
        ]

        for var, label in watched:
            var.trace_add("write", lambda *_args, v=var, l=label: self._on_saved_setting_changed(l, v.get()))

    def _on_saved_setting_changed(self, label: str, value: Any):
        if not getattr(self, "_ui_ready", False):
            return
        self._save_all_settings()
        display = self._format_saved_value(label, value)
        message = f"Đã lưu cài đặt: {label} = {display}"
        if label.startswith("Pipeline "):
            self._pl_log(message, "SUCCESS")
            if label.endswith(" 2") or " file 2" in label.lower():
                self.log_job("File 2", message, "DEBUG")
            elif label not in {"Pipeline host", "Pipeline port", "Pipeline API key"}:
                self.log_job("File 1", message, "DEBUG")
        elif label.endswith(" 2") or label in {"Audio file 2", "TXT file 2", "Preset file 2"}:
            self.log_job("File 2", message, "DEBUG")
        else:
            self.log_job("File 1", message, "DEBUG")

    def _format_saved_value(self, label: str, value: Any) -> str:
        text = str(value).strip()
        if not text:
            return "(trống)"
        if "API key" in label:
            return f"{text[:6]}...{text[-4:]}" if len(text) > 12 else "***"
        if "file" in label.lower() or "image" in label.lower():
            return Path(text).name or text
        return text

    # ─────────────────────────────────────────────
    # Log
    # ─────────────────────────────────────────────

    def _poll_log(self):
        while True:
            try:
                item = self._log_q.get_nowait()
                if len(item) == 3:
                    job_name, msg, level = item
                else:
                    msg, level = item
                    job_name = None
                ts = datetime.now().strftime("%H:%M:%S")
                targets = []
                if job_name == "File 2" and hasattr(self, "log_file2_w"):
                    targets = [self.log_file2_w]
                elif job_name == "File 1" and hasattr(self, "log_file1_w"):
                    targets = [self.log_file1_w]
                elif hasattr(self, "log_file1_w") and hasattr(self, "log_file2_w"):
                    targets = [self.log_file1_w, self.log_file2_w]
                elif hasattr(self, "log_w"):
                    targets = [self.log_w]

                for widget in targets:
                    try:
                        at_bottom = widget.yview()[1] >= 0.95
                    except Exception:
                        at_bottom = True
                    widget.config(state="normal")
                    widget.insert("end", f"[{ts}] {msg}\n", level)
                    if at_bottom:
                        widget.see("end")
                    widget.config(state="disabled")
            except queue.Empty:
                break
        self.after(100, self._poll_log)

    def log(self, msg: str, level: str = "INFO"):
        """Thread-safe log."""
        job_name = getattr(self._job_log_context, "job_name", None)
        self._log_q.put((job_name, msg, level))
        getattr(logger, {"SUCCESS":"info","WARNING":"warning","ERROR":"error","DEBUG":"debug"}.get(level,"info"))(msg)

    def log_job(self, job_name: str, msg: str, level: str = "INFO"):
        self._log_q.put((job_name, msg, level))
        getattr(logger, {"SUCCESS":"info","WARNING":"warning","ERROR":"error","DEBUG":"debug"}.get(level,"info"))(f"[{job_name}] {msg}")

    def _set_job_log_context(self, job_name: Optional[str]):
        previous = getattr(self._job_log_context, "job_name", None)
        self._job_log_context.job_name = job_name
        return previous

    def _set_preview(self, content: str, info: str = ""):
        job_name = getattr(self._job_log_context, "job_name", None)
        preview = self.preview2 if job_name == "File 2" and hasattr(self, "preview2") else self.preview
        chunk_info = self.chunk_info2 if job_name == "File 2" and hasattr(self, "chunk_info2") else self.chunk_info
        preview.config(state="normal")
        preview.delete("1.0","end")
        preview.insert("end", content[:2000])
        if len(content) > 2000:
            preview.insert("end", "\n...(rút gọn)")
        preview.config(state="disabled")
        chunk_info.config(text=info)

    def _set_progress(self, cur: int, total: int, job_name: Optional[str] = None):
        job_name = job_name or getattr(self._job_log_context, "job_name", None)
        cur_var = self.v_cur2 if job_name == "File 2" else self.v_cur
        pbar = self.pbar2 if job_name == "File 2" and hasattr(self, "pbar2") else self.pbar
        plbl = self.plbl2 if job_name == "File 2" and hasattr(self, "plbl2") else self.plbl
        cur_var.set(str(cur))
        pbar["maximum"] = total
        pbar["value"]   = cur
        plbl.config(text=f"{cur} / {total}")

    def _set_task_progress(self, cur: int, total: int, job_name: Optional[str] = None):
        job_name = job_name or getattr(self._job_log_context, "job_name", None)
        pbar = self.pbar2 if job_name == "File 2" and hasattr(self, "pbar2") else self.pbar
        plbl = self.plbl2 if job_name == "File 2" and hasattr(self, "plbl2") else self.plbl
        pbar["maximum"] = total
        pbar["value"]   = cur
        plbl.config(text=f"{cur} / {total}")

    # ─────────────────────────────────────────────
    # Button callbacks
    # ─────────────────────────────────────────────

    def _get_active_url(self) -> str:
        """Trả về URL thực sự thay vì tên gợi nhớ."""
        raw = self.v_url.get().strip()
        val = self._saved_urls.get(raw, {})
        return val.get("url", raw)

    def _get_url_for_job(self, job_name: Optional[str] = None) -> str:
        raw = (self.v_url2 if job_name == "File 2" else self.v_url).get().strip()
        val = self._saved_urls.get(raw, {})
        return val.get("url", raw)

    def _save_settings_for_preset(self, name):
        if not name: return
        preset = self._saved_urls.get(name, {})
        # KHÔNG ghi đè preset["url"] ở đây!
        # URL chỉ được thay đổi khi user bấm 'Thêm/Lưu Thể Loại' trong dialog.
        # Nếu ghi ở đây, khi switch A→B, v_url đã là B, nên A sẽ bị ghi URL của B (bug).
        preset["ref_image"] = self.pl_ref_image.get()
        preset["target_model_text"] = self.v_model.get()
        preset["first_chunk_size"] = self.v_first.get()
        preset["next_chunk_size"] = self.v_next.get()
        preset["skip_empty_lines"] = self.v_skip.get()
        preset["audio_first4min_chunk_seconds"] = self.v_aud_first4.get()
        preset["inter_chunk_delay_seconds"] = self.v_delayt.get()
        preset["pl_host"] = self.pl_host.get()
        preset["pl_port"] = self.pl_port.get()
        preset["pl_api_key"] = self.pl_api_key.get()
        preset["pl_img_model"] = self.pl_img_model.get()
        preset["pl_vid_model"] = self.pl_vid_model.get()
        preset["pl_aspect"] = self.pl_aspect.get()
        preset["pl_res"] = self.pl_res.get()
        preset["pl_category"] = self.pl_category.get()
        preset["pl_threads"] = self.pl_threads.get()
        preset["pl_image_threads"] = self.pl_threads.get()
        preset["pl_video_threads"] = self.pl_video_threads.get()
        preset["pl_run_step1"] = self.pl_run_step1.get()
        preset["pl_run_step2"] = self.pl_run_step2.get()
        preset["pl_run_step3"] = self.pl_run_step3.get()
        for key in ("pl_whisk_file", "pl_veo_file", "pl_luot2_file"):
            preset.pop(key, None)
        self._saved_urls[name] = preset

    def _on_url_selected(self, e=None):
        if hasattr(self, 'last_preset_name') and self.last_preset_name in self._saved_urls:
            self._save_settings_for_preset(self.last_preset_name)
            
        name = self.v_url.get().strip()
        self.last_preset_name = name
        
        val = self._saved_urls.get(name, {})
        url = val.get("url", name)
        
        self.config["gemini_url"] = url
        
        if "ref_image" in val: self.pl_ref_image.set(val["ref_image"])
        if "target_model_text" in val: self.v_model.set(val["target_model_text"])
        if "first_chunk_size" in val: self.v_first.set(val["first_chunk_size"])
        if "next_chunk_size" in val: self.v_next.set(val["next_chunk_size"])
        if "skip_empty_lines" in val: self.v_skip.set(val["skip_empty_lines"])
        if "audio_first4min_chunk_seconds" in val: self.v_aud_first4.set(str(val["audio_first4min_chunk_seconds"]))
        if "inter_chunk_delay_seconds" in val: self.v_delayt.set(val["inter_chunk_delay_seconds"])
        
        if "pl_host" in val: self.pl_host.set(val["pl_host"])
        if "pl_port" in val: self.pl_port.set(val["pl_port"])
        if "pl_api_key" in val: self.pl_api_key.set(val["pl_api_key"])
        if "pl_img_model" in val: self.pl_img_model.set(val["pl_img_model"])
        if "pl_vid_model" in val: self.pl_vid_model.set(_normalize_video_model(val["pl_vid_model"]))
        if "pl_aspect" in val: self.pl_aspect.set(val["pl_aspect"])
        if "pl_res" in val: self.pl_res.set(val["pl_res"])
        if "pl_category" in val: self.pl_category.set(val["pl_category"])
        if "pl_image_threads" in val:
            self.pl_threads.set(val["pl_image_threads"])
        elif "pl_threads" in val:
            self.pl_threads.set(val["pl_threads"])
        if "pl_video_threads" in val:
            self.pl_video_threads.set(val["pl_video_threads"])
        if "pl_run_step1" in val: self.pl_run_step1.set(val["pl_run_step1"])
        if "pl_run_step2" in val: self.pl_run_step2.set(val["pl_run_step2"])
        if "pl_run_step3" in val: self.pl_run_step3.set(val["pl_run_step3"])
        self.log(f"Đã nạp bộ tùy chỉnh của Thể Loại: {name}", "INFO")

    def _on_url2_selected(self, e=None):
        name = self.v_url2.get().strip()
        val = self._saved_urls.get(name, {})
        if "target_model_text" in val:
            self.v_model2.set(val["target_model_text"])
        if "first_chunk_size" in val:
            self.v_first2.set(val["first_chunk_size"])
        if "next_chunk_size" in val:
            self.v_next2.set(val["next_chunk_size"])
        if "skip_empty_lines" in val:
            self.v_skip2.set(val["skip_empty_lines"])
        if "inter_chunk_delay_seconds" in val:
            self.v_delayt2.set(val["inter_chunk_delay_seconds"])
        if "pl_img_model" in val: self.pl_img_model2.set(val["pl_img_model"])
        if "pl_vid_model" in val: self.pl_vid_model2.set(_normalize_video_model(val["pl_vid_model"]))
        if "pl_aspect" in val: self.pl_aspect2.set(val["pl_aspect"])
        if "pl_res" in val: self.pl_res2.set(val["pl_res"])
        if "pl_category" in val: self.pl_category2.set(val["pl_category"])
        if "pl_image_threads" in val:
            self.pl_threads2.set(val["pl_image_threads"])
        elif "pl_threads" in val:
            self.pl_threads2.set(val["pl_threads"])
        if "pl_video_threads" in val:
            self.pl_video_threads2.set(val["pl_video_threads"])
        if "pl_run_step1" in val: self.pl_run_step12.set(val["pl_run_step1"])
        if "pl_run_step2" in val: self.pl_run_step22.set(val["pl_run_step2"])
        if "pl_run_step3" in val: self.pl_run_step32.set(val["pl_run_step3"])
        if "ref_image" in val: self.pl_ref_image2.set(val["ref_image"])
        self.log_job("File 2", f"Đã nạp bộ tùy chỉnh của Thể Loại: {name}", "INFO")

    def _add_url(self):
        """Mở hộp thoại thêm/sửa Thể Loại (gồm URL, Ảnh Tham Chiếu & Config)."""
        raw_input = self.v_url.get().strip()
        val = self._saved_urls.get(raw_input, {})
        actual_url = val.get("url", raw_input)
        actual_ref_image = val.get("ref_image", "")
        suggested_name = raw_input if raw_input in self._saved_urls else ""

        dlg = tk.Toplevel(self)
        dlg.title("Gắn mác Thể Loại & Lưu Các Cài Đặt")
        dlg.geometry("520x320")
        dlg.configure(bg=C["bg"])
        dlg.grab_set()

        frm = tk.Frame(dlg, bg=C["bg"], padx=15, pady=15)
        frm.pack(fill="both", expand=True)

        tk.Label(frm, text="Tên Thể Loại:", font=FS, fg=C["text"], bg=C["bg"]).grid(row=0, column=0, sticky="w", pady=5)
        v_name = tk.StringVar(value=suggested_name)
        tk.Entry(frm, textvariable=v_name, font=FS, width=40).grid(row=0, column=1, sticky="w", pady=5)

        tk.Label(frm, text="URL Gemini:", font=FS, fg=C["text"], bg=C["bg"]).grid(row=1, column=0, sticky="w", pady=5)
        v_url = tk.StringVar(value=actual_url)
        tk.Entry(frm, textvariable=v_url, font=FS, width=40).grid(row=1, column=1, sticky="w", pady=5)

        tk.Label(frm, text="Ảnh tham chiếu B3:", font=FS, fg=C["text"], bg=C["bg"]).grid(row=2, column=0, sticky="w", pady=5)
        v_ref = tk.StringVar(value=actual_ref_image)
        ref_entry = tk.Frame(frm, bg=C["bg"])
        ref_entry.grid(row=2, column=1, sticky="w", pady=5)
        tk.Entry(ref_entry, textvariable=v_ref, font=FS, width=33).pack(side="left")
        
        def _br_ref():
            p = filedialog.askopenfilename(filetypes=[("Image files", "*.png;*.jpg;*.jpeg;*.webp")])
            if p: v_ref.set(p)
            
        tk.Button(ref_entry, text="...", command=_br_ref).pack(side="left", padx=2)

        tk.Label(frm, text="Giọng Mẫu (OmniVoice):", font=FS, fg=C["text"], bg=C["bg"]).grid(row=3, column=0, sticky="w", pady=5)
        saved_voices = self._get_omnivoice_saved_voices()
        current_voice = val.get("sample_voice", "")
        v_sample = tk.StringVar(value=current_voice)
        sample_entry = tk.Frame(frm, bg=C["bg"])
        sample_entry.grid(row=3, column=1, sticky="w", pady=5)
        voice_combo = ttk.Combobox(sample_entry, textvariable=v_sample, values=saved_voices, font=FS, width=30, state="readonly")
        voice_combo.pack(side="left")
        if current_voice and current_voice in saved_voices:
            voice_combo.set(current_voice)

        def _save():
            name = v_name.get().strip()
            url = v_url.get().strip()
            ref = v_ref.get().strip()
            sample = v_sample.get().strip()
            if not name or not url:
                messagebox.showwarning("Lỗi", "Tên và URL không được bỏ trống!")
                return
                
            if suggested_name and suggested_name != name and suggested_name in self._saved_urls:
                del self._saved_urls[suggested_name]
                
            self._saved_urls[name] = {"url": url, "ref_image": ref, "sample_voice": sample}
            self.url_combo["values"] = list(self._saved_urls.keys())
            if hasattr(self, "url_combo2"):
                self.url_combo2["values"] = list(self._saved_urls.keys())
            self.v_url.set(name)
            
            # Update UI variable TRƯỚC KHI gọi save_all_settings để nó không bị ghi đè mất
            self.pl_ref_image.set(ref)
            
            # Gán lại last_preset_name để khi sang Thể loại khác nó không lưu loạn,
            # và sau đó force save lại config hiện tại vào name mới.
            self.last_preset_name = name
            self._save_all_settings()  
            self.log(f"✅ Đã lưu Thể Loại (Preset): {name}", "SUCCESS")
            dlg.destroy()

        bbar = tk.Frame(frm, bg=C["bg"])
        bbar.grid(row=4, column=0, columnspan=2, pady=15)
        tk.Button(bbar, text="Lưu thông tin", bg=C["ok"], fg="white", font=FS, command=_save).pack(side="left", padx=(0,8))
        tk.Button(bbar, text="⚙️ OmniVoice", bg="#3a3a5c", fg="white", font=FS, command=self._open_omnivoice_settings).pack(side="left")

    def _remove_url(self):
        """Xóa URL hiện tại khỏi danh sách."""
        name_or_url = self.v_url.get().strip()
        if name_or_url in self._saved_urls:
            del self._saved_urls[name_or_url]
            self.url_combo["values"] = list(self._saved_urls.keys())
            if hasattr(self, "url_combo2"):
                self.url_combo2["values"] = list(self._saved_urls.keys())
            nxt = list(self._saved_urls.keys())[0] if self._saved_urls else "https://gemini.google.com/app"
            self.v_url.set(nxt)
            if self.v_url2.get() == name_or_url:
                self.v_url2.set(nxt)
            self._on_url_selected()
            self._save_all_settings()
            self.log(f"✖ Đã xóa URL: {name_or_url}", "WARNING")
        else:
            self.log("URL/Tên này không có trong danh sách.", "WARNING")

    LEGACY_OMNIVOICE_DIR = r"C:\Users\duong\.gemini\antigravity\scratch\OmniVoice"

    def _get_omnivoice_dir(self) -> str:
        env_dir = os.environ.get("Z115_OMNIVOICE_DIR", "").strip()
        if env_dir and os.path.exists(env_dir):
            return env_dir

        local_dir = Path(__file__).resolve().parents[1] / "OmniVoice"
        if local_dir.exists():
            return str(local_dir)

        if os.path.exists(self.LEGACY_OMNIVOICE_DIR):
            return self.LEGACY_OMNIVOICE_DIR

        return str(local_dir)

    def _get_omnivoice_saved_voices(self):
        """Lấy danh sách tên giọng đã lưu trong saved_voices/ của OmniVoice."""
        voices_dir = os.path.join(self._get_omnivoice_dir(), "saved_voices")
        if not os.path.exists(voices_dir):
            return []
        return sorted([f[:-4] for f in os.listdir(voices_dir) if f.endswith(".wav")])

    def _fix_omnivoice_text_setting(self, value):
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

    def _read_omnivoice_settings(self, job_id=1):
        """Đọc settings_1.json hoặc settings_2.json của OmniVoice."""
        settings_path = os.path.join(self._get_omnivoice_dir(), f"settings_{job_id}.json")
        defaults = {
            "num_step": 32, "guidance_scale": 2.0, "denoise": True,
            "speed": 1.0, "duration": 0, "preprocess": True,
            "postprocess": True, "seed": -1, "chunk_mode": "Không cắt",
            "chunk_words": 25, "num_threads": 1, "language": "Tự động", "sample_voice": ""
        }
        try:
            with open(settings_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                for k, v in defaults.items():
                    data.setdefault(k, v)
                for key in ("chunk_mode", "language"):
                    data[key] = self._fix_omnivoice_text_setting(data.get(key, defaults.get(key)))
                return data
        except Exception:
            return defaults

    def _write_omnivoice_settings(self, data, job_id=1):
        """Ghi settings_1.json hoặc settings_2.json cho OmniVoice."""
        omnivoice_dir = self._get_omnivoice_dir()
        os.makedirs(omnivoice_dir, exist_ok=True)
        settings_path = os.path.join(omnivoice_dir, f"settings_{job_id}.json")
        try:
            with open(settings_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        except Exception as e:
            self.log(f"Lỗi lưu OmniVoice settings: {e}", "ERROR")

    def _open_omnivoice_settings(self, job_id=1):
        """Mở hộp thoại chỉnh sửa thông số nâng cao OmniVoice."""
        self.log(f"Đang mở hộp thoại cài đặt OmniVoice (Luồng {job_id})...", "INFO")
        try:
            s = self._read_omnivoice_settings(job_id)

            dlg = tk.Toplevel(self)
            dlg.title(f"⚙️ Cài Đặt OmniVoice - Luồng {job_id}")
            dlg.geometry("500x620")
            dlg.configure(bg=C["bg"])
            dlg.grab_set()

            frm = tk.Frame(dlg, bg=C["bg"], padx=15, pady=10)
            frm.pack(fill="both", expand=True)

            # Title
            tk.Label(frm, text="🎙️ Thông số OmniVoice", font=(FS[0], 11, "bold"),
                     fg="#6C63FF", bg=C["bg"]).grid(row=0, column=0, columnspan=3, pady=(0, 10), sticky="w")

            row = 1
            # sample voice
            tk.Label(frm, text="Giọng mẫu:", font=FS, fg=C["text"], bg=C["bg"]).grid(row=row, column=0, sticky="w", pady=3)
            saved_voices = self._get_omnivoice_saved_voices()
            v_voice = tk.StringVar(value=s.get("sample_voice", ""))
            ttk.Combobox(
                frm,
                textvariable=v_voice,
                values=[""] + saved_voices,
                state="readonly",
                font=FS,
                width=24
            ).grid(row=row, column=1, sticky="w", pady=3)

            row += 1
            # num_step
            tk.Label(frm, text="Số bước khử nhiễu:", font=FS, fg=C["text"], bg=C["bg"]).grid(row=row, column=0, sticky="w", pady=3)
            v_ns = tk.IntVar(value=int(s["num_step"]))
            ns_scale = tk.Scale(frm, from_=4, to=128, resolution=4, orient="horizontal",
                                variable=v_ns, bg=C["bg"], fg=C["text"], highlightthickness=0, length=200)
            ns_scale.grid(row=row, column=1, columnspan=2, sticky="w", pady=3)

            row += 1
            # guidance_scale
            tk.Label(frm, text="Guidance Scale:", font=FS, fg=C["text"], bg=C["bg"]).grid(row=row, column=0, sticky="w", pady=3)
            v_gs = tk.DoubleVar(value=float(s["guidance_scale"]))
            gs_scale = tk.Scale(frm, from_=0.0, to=10.0, resolution=0.5, orient="horizontal",
                                variable=v_gs, bg=C["bg"], fg=C["text"], highlightthickness=0, length=200)
            gs_scale.grid(row=row, column=1, columnspan=2, sticky="w", pady=3)

            row += 1
            # speed
            tk.Label(frm, text="Tốc độ đọc:", font=FS, fg=C["text"], bg=C["bg"]).grid(row=row, column=0, sticky="w", pady=3)
            v_sp = tk.DoubleVar(value=float(s["speed"]))
            sp_scale = tk.Scale(frm, from_=0.5, to=2.0, resolution=0.1, orient="horizontal",
                                variable=v_sp, bg=C["bg"], fg=C["text"], highlightthickness=0, length=200)
            sp_scale.grid(row=row, column=1, columnspan=2, sticky="w", pady=3)

            row += 1
            # duration
            tk.Label(frm, text="Thời lượng (giây, 0=tự động):", font=FS, fg=C["text"], bg=C["bg"]).grid(row=row, column=0, sticky="w", pady=3)
            v_du = tk.DoubleVar(value=float(s.get("duration", 0)))
            du_scale = tk.Scale(frm, from_=0, to=60, resolution=0.5, orient="horizontal",
                                variable=v_du, bg=C["bg"], fg=C["text"], highlightthickness=0, length=200)
            du_scale.grid(row=row, column=1, columnspan=2, sticky="w", pady=3)

            row += 1
            # language
            tk.Label(frm, text="Ngôn ngữ OmniVoice:", font=FS, fg=C["text"], bg=C["bg"]).grid(row=row, column=0, sticky="w", pady=3)
            v_lang = tk.StringVar(value=s.get("language", "Tự động"))
            ttk.Combobox(
                frm,
                textvariable=v_lang,
                values=["Tự động", "Vietnamese", "Korean", "Japanese", "English", "Chinese"],
                state="readonly",
                font=FS,
                width=18
            ).grid(row=row, column=1, sticky="w", pady=3)

            row += 1
            # seed
            tk.Label(frm, text="Seed (-1 = ngẫu nhiên):", font=FS, fg=C["text"], bg=C["bg"]).grid(row=row, column=0, sticky="w", pady=3)
            v_se = tk.IntVar(value=int(s["seed"]))
            tk.Entry(frm, textvariable=v_se, font=FS, width=10).grid(row=row, column=1, sticky="w", pady=3)

            row += 1
            # chunk_mode
            tk.Label(frm, text="Cắt văn bản dài:", font=FS, fg=C["text"], bg=C["bg"]).grid(row=row, column=0, sticky="w", pady=3)
            v_cm = tk.StringVar(value=s["chunk_mode"])
            ttk.Combobox(frm, textvariable=v_cm, values=["Không cắt", "Theo dấu câu", "Theo xuống dòng", "Cắt gộp thông minh"],
                         state="readonly", font=FS, width=18).grid(row=row, column=1, sticky="w", pady=3)

            row += 1
            # chunk_words
            tk.Label(frm, text="Số từ tối đa (Cắt gộp):", font=FS, fg=C["text"], bg=C["bg"]).grid(row=row, column=0, sticky="w", pady=3)
            v_cw = tk.IntVar(value=int(s.get("chunk_words", 25)))
            cw_scale = tk.Scale(frm, from_=5, to=100, resolution=1, orient="horizontal",
                                variable=v_cw, bg=C["bg"], fg=C["text"], highlightthickness=0, length=200)
            cw_scale.grid(row=row, column=1, columnspan=2, sticky="w", pady=3)

            row += 1
            tk.Label(frm, text="So luong / batch (1-20):", font=FS, fg=C["text"], bg=C["bg"]).grid(row=row, column=0, sticky="w", pady=3)
            v_nt = tk.IntVar(value=max(1, min(20, int(s.get("num_threads", 1)))))
            nt_scale = tk.Scale(frm, from_=1, to=20, resolution=1, orient="horizontal",
                                variable=v_nt, bg=C["bg"], fg=C["text"], highlightthickness=0, length=200)
            nt_scale.grid(row=row, column=1, columnspan=2, sticky="w", pady=3)

            row += 1
            # Checkboxes
            chk_frm = tk.Frame(frm, bg=C["bg"])
            chk_frm.grid(row=row, column=0, columnspan=3, sticky="w", pady=8)
            v_dn = tk.BooleanVar(value=bool(s["denoise"]))
            v_pp = tk.BooleanVar(value=bool(s["preprocess"]))
            v_po = tk.BooleanVar(value=bool(s["postprocess"]))
            tk.Checkbutton(chk_frm, text="Khử nhiễu", variable=v_dn, font=FS, fg=C["text"],
                           bg=C["bg"], selectcolor=C["bg"], activebackground=C["bg"]).pack(side="left", padx=(0, 12))
            tk.Checkbutton(chk_frm, text="Tiền xử lý text", variable=v_pp, font=FS, fg=C["text"],
                           bg=C["bg"], selectcolor=C["bg"], activebackground=C["bg"]).pack(side="left", padx=(0, 12))
            tk.Checkbutton(chk_frm, text="Hậu xử lý audio", variable=v_po, font=FS, fg=C["text"],
                           bg=C["bg"], selectcolor=C["bg"], activebackground=C["bg"]).pack(side="left")

            row += 1

            def _save_ov():
                data = {
                    "num_step": v_ns.get(),
                    "guidance_scale": v_gs.get(),
                    "denoise": v_dn.get(),
                    "speed": v_sp.get(),
                    "duration": v_du.get(),
                    "language": v_lang.get(),
                    "preprocess": v_pp.get(),
                    "postprocess": v_po.get(),
                    "seed": v_se.get(),
                    "chunk_mode": v_cm.get(),
                    "chunk_words": v_cw.get(),
                    "num_threads": v_nt.get(),
                    "sample_voice": v_voice.get()
                }
                self._write_omnivoice_settings(data, job_id)
                self.log(f"✅ Đã lưu cài đặt OmniVoice (Luồng {job_id}).", "SUCCESS")
                dlg.destroy()

            bbar = tk.Frame(frm, bg=C["bg"])
            bbar.grid(row=row, column=0, columnspan=3, pady=(5, 0))
            tk.Button(bbar, text="💾 Lưu cài đặt", bg=C["ok"], fg="white", font=FS, command=_save_ov).pack(side="left", padx=(0, 8))
            tk.Button(bbar, text="Hủy", bg="#5a2020", fg="white", font=FS, command=dlg.destroy).pack(side="left")
            
        except Exception as e:
            self.log(f"Lỗi khi mở cài đặt: {e}", "ERROR")
            import traceback
            traceback.print_exc()


    def _save_all_settings(self):
        """Ghi cấu hình hiện tại vào Thể Loại đang hoạt động, rồi tổng hợp vào config.json."""
        current_name = self.v_url.get().strip()
        self._save_settings_for_preset(current_name)
        
        try:
            cfg_path = Path("config") / "config.json"
            cfg_path.parent.mkdir(parents=True, exist_ok=True)
            if cfg_path.exists():
                data = json.loads(cfg_path.read_text(encoding="utf-8"))
            else:
                data = {}
                
            data["saved_urls"] = self._saved_urls
            data["last_preset_name"] = current_name
            data["gemini_url"] = self._get_active_url()
            data["target_model_text"] = self.v_model.get()
            data["first_chunk_size"] = self.v_first.get()
            data["next_chunk_size"] = self.v_next.get()
            data["skip_empty_lines"] = self.v_skip.get()
            data["inter_chunk_delay_seconds"] = self.v_delayt.get()
            data["target_model_text_2"] = self.v_model2.get()
            data["first_chunk_size_2"] = self.v_first2.get()
            data["next_chunk_size_2"] = self.v_next2.get()
            data["skip_empty_lines_2"] = self.v_skip2.get()
            data["save_response_2"] = self.v_save2.get()
            data["inter_chunk_delay_enabled_2"] = self.v_delay2.get()
            data["inter_chunk_delay_seconds_2"] = self.v_delayt2.get()
            data.pop("last_audio_file", None)
            data.pop("last_audio_file_2", None)
            data["audio_model"] = self.v_aud_model.get()
            data["audio_engine"] = self.v_aud_engine.get()
            data["audio_language"] = self.v_aud_lang.get()
            data["audio_beam_size"] = self.v_aud_beam.get()
            data["audio_first4min_chunk_seconds"] = self.v_aud_first4.get()
            data["audio_model_2"] = self.v_aud_model2.get()
            data["audio_engine_2"] = self.v_aud_engine2.get()
            data["audio_language_2"] = self.v_aud_lang2.get()
            data["audio_beam_size_2"] = self.v_aud_beam2.get()
            data["audio_first4min_chunk_seconds_2"] = self.v_aud_first42.get()
            data.pop("last_txt_file", None)
            data.pop("last_txt_file_2", None)
            data["last_preset_name_2"] = self.v_url2.get()
            data["workflow_do_audio"] = self.v_do_audio.get()
            data["workflow_do_gemini"] = self.v_do_gemini.get()
            data["workflow_do_pipeline"] = self.v_do_pipeline.get()
            
            data["active_internal_profile"] = getattr(self, "v_internal_profile", tk.StringVar(value="Profile 1")).get()
            data["custom_browser_path"] = self.v_custom_browser_path.get()

            data["pl_host"] = self.pl_host.get()
            data["pl_port"] = self.pl_port.get()
            data["pl_api_key"] = self.pl_api_key.get()
            data["pl_img_model"] = self.pl_img_model.get()
            data["pl_vid_model"] = self.pl_vid_model.get()
            data["pl_aspect"] = self.pl_aspect.get()
            data["pl_res"] = self.pl_res.get()
            data["pl_category"] = self.pl_category.get()
            data["pl_threads"] = self.pl_threads.get()
            data["pl_image_threads"] = self.pl_threads.get()
            data["pl_video_threads"] = self.pl_video_threads.get()
            data["pl_run_step1"] = self.pl_run_step1.get()
            data["pl_run_step2"] = self.pl_run_step2.get()
            data["pl_run_step3"] = self.pl_run_step3.get()
            data.pop("pl_whisk_file", None)
            data.pop("pl_veo_file", None)
            data.pop("pl_luot2_file", None)
            data["pl_ref_image"] = self.pl_ref_image.get()
            data["pl_img_model_2"] = self.pl_img_model2.get()
            data["pl_vid_model_2"] = self.pl_vid_model2.get()
            data["pl_aspect_2"] = self.pl_aspect2.get()
            data["pl_res_2"] = self.pl_res2.get()
            data["pl_category_2"] = self.pl_category2.get()
            data["pl_threads_2"] = self.pl_threads2.get()
            data["pl_image_threads_2"] = self.pl_threads2.get()
            data["pl_video_threads_2"] = self.pl_video_threads2.get()
            data["pl_run_step1_2"] = self.pl_run_step12.get()
            data["pl_run_step2_2"] = self.pl_run_step22.get()
            data["pl_run_step3_2"] = self.pl_run_step32.get()
            data.pop("pl_whisk_file_2", None)
            data.pop("pl_veo_file_2", None)
            data.pop("pl_luot2_file_2", None)
            data["pl_ref_image_2"] = self.pl_ref_image2.get()
            
            cfg_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            self.config = data
        except Exception as e:
            self.log(f"⚠️ Không lưu được config: {e}", "WARNING")

    def _browse_audio(self):
        p = filedialog.askopenfilename(title="Chọn file Audio",
                                        filetypes=[("Audio", "*.mp3 *.wav *.m4a *.aac *.flac *.ogg"), ("All", "*.*")])
        if p:
            self.v_audio.set(p)
            self.v_file.set("")
            self._save_all_settings()
            self.log_job("File 1", f"Đã chọn audio: {p}")



    def _browse_audio2(self):
        p = filedialog.askopenfilename(title="Chọn file Audio 2",
                                        filetypes=[("Audio", "*.mp3 *.wav *.m4a *.aac *.flac *.ogg"), ("All", "*.*")])
        if p:
            self.v_audio2.set(p)
            self.v_file2.set("")
            self._save_all_settings()
            self.log_job("File 2", f"Đã chọn audio file 2: {p}")

    def _browse_script(self):
        p = filedialog.askopenfilename(title="Chọn file Kịch bản 1", filetypes=[("Text", "*.txt"), ("All", "*.*")])
        if p:
            self.v_script.set(p)
            self._save_all_settings()

    def _browse_script2(self):
        p = filedialog.askopenfilename(title="Chọn file Kịch bản 2", filetypes=[("Text", "*.txt"), ("All", "*.*")])
        if p:
            self.v_script2.set(p)
            self._save_all_settings()

    def _browse(self):
        p = filedialog.askopenfilename(title="Chọn file TXT",
                                        filetypes=[("Text","*.txt"),("All","*.*")])
        if p:
            self.v_file.set(p)
            self.v_audio.set("")
            self._save_all_settings()
            self.log_job("File 1", f"Đã chọn file TXT: {p}")
            self._preview_file(p, "File 1")

    def _browse2(self):
        p = filedialog.askopenfilename(title="Chọn file TXT 2",
                                        filetypes=[("Text","*.txt"),("All","*.*")])
        if p:
            self.v_file2.set(p)
            self.v_audio2.set("")
            self._save_all_settings()
            self.log_job("File 2", f"Đã chọn file TXT 2: {p}")
            self._preview_file(p, "File 2")

    def _preview_file(self, path: str, job_name: Optional[str] = None):
        try:
            from core.file_loader import load_lines
            from core.chunker import create_chunks
            settings = self._gemini_settings_for_job(job_name)
            lines  = load_lines(path, skip_empty=settings["skip"])
            chunks = create_chunks(lines, int(settings["first"]), int(settings["next"]))
            lines_var = self.v_lines2 if job_name == "File 2" else self.v_lines
            chunks_var = self.v_chunks2 if job_name == "File 2" else self.v_chunks
            lines_var.set(str(len(lines)))
            chunks_var.set(str(len(chunks)))
            self._chunks = chunks
            if chunks:
                c = chunks[0]
                previous_context = self._set_job_log_context(job_name)
                try:
                    self._set_preview(c.content, f"{c.label} ({c.line_count} dòng, {c.start_line}–{c.end_line})")
                finally:
                    self._set_job_log_context(previous_context)
            self.log_job(job_name or "File 1", f"File: {len(lines)} dòng → {len(chunks)} chunk.", "SUCCESS")
        except Exception as e:
            self.log_job(job_name or "File 1", f"Lỗi đọc file: {e}", "ERROR")

    def _known_edge_paths(self) -> List[Path]:
        paths = [
            Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        ]
        local_app = os.environ.get("LOCALAPPDATA")
        if local_app:
            paths.append(Path(local_app) / "Microsoft" / "Edge" / "Application" / "msedge.exe")
        return paths

    def _resolve_browser_path(self) -> Optional[Path]:
        configured = str(self.config.get("custom_browser_path", "")).strip().strip('"')
        if configured:
            path = Path(os.path.expandvars(os.path.expanduser(configured)))
            if path.exists():
                return path
            self.log(f"⚠️ Không tìm thấy trình duyệt đã cấu hình: {configured}", "WARNING")

        for path in self._known_edge_paths():
            if path.exists():
                self.config["custom_browser_path"] = str(path)
                return path
        return None

    def _open_gemini_browser(self, url: str, action: str = "mở Gemini") -> bool:
        try:
            browser_path = self._resolve_browser_path()
            if browser_path:
                self.log(f"🌐 Đang {action} bằng Edge/Chrome: {browser_path}", "INFO")
                subprocess.Popen([str(browser_path), "--new-window", "--start-maximized", url])
            else:
                self.log("🌐 Không tìm thấy Edge trong máy; mở bằng trình duyệt mặc định...", "WARNING")
                import webbrowser
                webbrowser.open(url)
            return True
        except Exception as e:
            self.log(f"❌ Lỗi {action}: {e}", "ERROR")
            return False

    def _launch(self):
        """Khởi động browser lần đầu."""
        url = self._get_active_url() or "https://gemini.google.com/app"
        self.config["gemini_url"] = url
        self.log(f"Đang khởi động trình duyệt → {url}")
        self.v_status.set("Đang mở trình duyệt...")

        url_with_param = f"{url}?z115_job=auto" if "?" not in url else f"{url}&z115_job=auto"

        def task():
            if self._open_gemini_browser(url_with_param, "mở tab Gemini"):
                self.log("✅ Browser đã mở thành công!", "SUCCESS")
                self.v_status.set("Browser sẵn sàng — đăng nhập nếu cần")
            else:
                self.v_status.set("Lỗi")
        threading.Thread(target=task, daemon=True).start()

    def _relaunch(self):
        """Reset controller rồi khởi động lại, có force-kill Chrome cũ."""
        self.log("🔄 Tính năng Đổi Hồ Sơ của Playwright không còn áp dụng vì App đã chuyển sang dùng Addon Extension. Đang mở tab mới...", "WARNING")
        self._launch()

    def _navigate(self):
        """Điều hướng browser tới URL trong ô."""
        url = self._get_active_url()
        if not url or not url.startswith("http"):
            messagebox.showwarning("URL không hợp lệ", "Vui lòng nhập URL hợp lệ (bắt đầu bằng http)")
            return
        self.config["gemini_url"] = url
        self.log(f"🔗 Đang mở: {url}")

        if not self._playwright_available:
            def fallback_task():
                if self._open_gemini_browser(url, "mở URL"):
                    self.log(f"✅ Đã mở bằng trình duyệt thường: {url}", "SUCCESS")
                    self.v_status.set("Đã mở URL")
            threading.Thread(target=fallback_task, daemon=True).start()
            return

        def task():
            try:
                self._bc("navigate_to_url", url, timeout=40)
                self.log(f"✅ Đã mở: {url}", "SUCCESS")
                self.v_status.set("Đã điều hướng")
            except Exception as e:
                self.log(f"❌ Lỗi mở URL: {e}", "ERROR")
        threading.Thread(target=task, daemon=True).start()

    def _start(self):
        """Bắt đầu chạy. Nếu có Audio -> Trích xuất -> Gửi. Nếu chỉ có TXT -> Gửi thẳng."""
        if self._worker and self._worker.is_alive():
            self.log("Đang có tiến trình chạy bận.", "WARNING"); return

        self.global_start_time = time.time()
        self._save_all_settings()
        self._clear_job_logs()

        has_file2 = bool(self.v_audio2.get().strip() or self.v_file2.get().strip() or self.v_script2.get().strip())
        if has_file2:
            self._start_consecutive()
            return
        
        audio_path = self.v_audio.get().strip()
        txt_path = self.v_file.get().strip()
        script_path = getattr(self, 'v_script', tk.StringVar()).get().strip()
        do_audio = self.v_do_audio.get()
        do_gemini = self.v_do_gemini.get()
        do_pipeline = self.v_do_pipeline.get()

        if script_path or audio_path:
            if not do_audio:
                messagebox.showwarning("Bước 1 đang tắt", "Bạn đang chọn file Audio nhưng đã tắt Bước 1. Bật Bước 1 hoặc chọn file TXT có sẵn.")
                return
            if not Path(audio_path).exists():
                messagebox.showwarning("Thiếu file", "Vui lòng chọn file Audio hợp lệ trước."); return
            self._stop.clear()
            self._pause.set()
            self._worker = threading.Thread(target=self._run_audio_worker, args=(audio_path,), kwargs={"script_path": script_path}, daemon=True, name="AudioWorker")
            self._worker.start()
        elif txt_path:
            if not do_gemini:
                if do_pipeline:
                    self.log("Bước 2 đang tắt. Chuyển sang chạy Bước 3 với file đã chọn ở tab Pipeline.", "INFO")
                    self.notebook.select(1)
                    self._pl_start()
                else:
                    messagebox.showwarning("Không có bước nào để chạy", "Bạn đã chọn TXT nhưng tắt Bước 2 và Bước 3.")
                return
            if not Path(txt_path).exists():
                messagebox.showwarning("Thiếu file", "Vui lòng chọn file TXT hợp lệ trước."); return
            self._stop.clear()
            self._pause.set()
            self._worker = threading.Thread(target=self._run_worker, args=(txt_path,), daemon=True, name="SendWorker")
            self._worker.start()
        elif do_pipeline:
            self.log("Không có Audio/TXT cho Bước 1-2. Chạy Bước 3 với file đã chọn ở tab Pipeline.", "INFO")
            self.notebook.select(1)
            self._pl_start()
        else:
            messagebox.showwarning("Thiếu file", "Vui lòng chọn file Audio/TXT hoặc bật Bước 3 với file pipeline đầu vào."); return

    def _start_consecutive(self):
        if self._worker and self._worker.is_alive():
            self.log("Đang có tiến trình chạy bận.", "WARNING")
            return

        jobs = [
            {"name": "File 1", "script": getattr(self, 'v_script', tk.StringVar()).get().strip(), "audio": self.v_audio.get().strip(), "txt": self.v_file.get().strip(), "preset": self.v_url.get().strip()},
            {"name": "File 2", "script": getattr(self, 'v_script2', tk.StringVar()).get().strip(), "audio": self.v_audio2.get().strip(), "txt": self.v_file2.get().strip(), "preset": self.v_url2.get().strip()},
        ]
        jobs = [job for job in jobs if job["audio"] or job["txt"] or job["script"]]
        if not jobs:
            messagebox.showwarning("Thiếu file", "Vui lòng chọn ít nhất 1 file Kịch bản, Audio hoặc TXT để chạy.")
            return

        if self.v_do_pipeline.get() and not self.v_do_gemini.get():
            messagebox.showwarning(
                "Thiếu Bước 2",
                "Chế độ nối tiếp cần bật Bước 2 để tự nạp output pipeline theo từng file. "
                "Nếu tắt Bước 2, hãy chạy Bước 3 thủ công trong tab Pipeline."
            )
            return

        for job in jobs:
            source = job["audio"] if self.v_do_audio.get() and job["audio"] else job["txt"]
            if not source or not Path(source).exists():
                messagebox.showwarning("Thiếu file", f"{job['name']} chưa có file hợp lệ.")
                return
            if job["preset"] not in self._saved_urls:
                messagebox.showwarning("Thiếu thể loại", f"{job['name']} chưa chọn thể loại hợp lệ.")
                return

        self._stop.clear()
        self._pause.set()
        self._worker = threading.Thread(target=self._run_consecutive_worker, args=(jobs,), daemon=True, name="ConsecutiveWorker")
        self._worker.start()

    def _apply_preset_for_job(self, preset_name: str, job_name: Optional[str] = None):
        if preset_name and preset_name in self._saved_urls:
            if job_name == "File 2":
                self.v_url2.set(preset_name)
                self._on_url2_selected()
            else:
                self.v_url.set(preset_name)
                self._on_url_selected()

    def _apply_preset_for_job_threadsafe(self, preset_name: str, job_name: Optional[str] = None):
        done = threading.Event()
        result = {"error": None}

        def apply():
            try:
                self._apply_preset_for_job(preset_name, job_name)
            except Exception as exc:
                result["error"] = exc
            finally:
                done.set()

        self.after(0, apply)
        done.wait()
        if result["error"]:
            raise result["error"]

    def _pl_auto_fill_outputs_threadsafe(self, job_name: Optional[str] = None, output_mgr=None):
        done = threading.Event()
        result = {"error": None}

        def apply():
            try:
                self._pl_auto_fill_outputs(job_name=job_name, output_mgr=output_mgr)
            except Exception as exc:
                result["error"] = exc
            finally:
                done.set()

        self.after(0, apply)
        done.wait()
        if result["error"]:
            raise result["error"]

    def _audio_settings_for_job(self, job_name: str) -> Dict[str, Any]:
        if job_name == "File 2":
            return {
                "model": self.v_aud_model2.get(),
                "engine": self.v_aud_engine2.get(),
                "language": self.v_aud_lang2.get(),
                "beam_size": self.v_aud_beam2.get(),
                "first4": self.v_aud_first42.get(),
            }
        return {
            "model": self.v_aud_model.get(),
            "engine": self.v_aud_engine.get(),
            "language": self.v_aud_lang.get(),
            "beam_size": self.v_aud_beam.get(),
            "first4": self.v_aud_first4.get(),
        }

    def _run_consecutive_worker(self, jobs: List[Dict[str, Any]]):
        job_count = len(jobs)
        self.log(f"Bắt đầu chế độ xen kẽ theo từng file ({job_count} file).", "INFO")
        if job_count < 2:
            self._run_single_job_pipeline(jobs[0])
            return

        locks = {
            "audio": threading.Lock(),
            "gemini": threading.Lock(),
            "pipeline": threading.Lock(),
        }
        gates = {
            "file2_audio": threading.Event(),
            "file2_gemini": threading.Event(),
            "file2_pipeline": threading.Event(),
        }
        failures: List[str] = []

        if not (self.v_do_audio.get() and (jobs[0].get("audio") or jobs[0].get("script"))):
            gates["file2_audio"].set()
        if not self.v_do_gemini.get():
            gates["file2_gemini"].set()
        if not self.v_do_pipeline.get():
            gates["file2_pipeline"].set()

        def wait_gate(job_name: str, gate_name: str, label: str) -> bool:
            gate = gates[gate_name]
            while not gate.wait(0.2):
                if self._stop.is_set():
                    self.log_job(job_name, f"Dừng khi đang chờ {label}.", "WARNING")
                    return False
            return not self._stop.is_set()

        def release_file2_gates():
            for gate in gates.values():
                gate.set()

        def run_job(job: Dict[str, Any]):
            job_name = job["name"]
            previous_context = self._set_job_log_context(job_name)
            try:
                if job_name == "File 2" and not wait_gate(job_name, "file2_audio", "File 1 rời Bước 1"):
                    return

                if self.v_do_audio.get():
                    if self._stop.is_set():
                        return
                    if job.get("audio") or job.get("script"):
                        with locks["audio"]:
                            if self._stop.is_set():
                                return
                            self._apply_preset_for_job_threadsafe(job["preset"], job_name)
                            self.log_job(job_name, "Bước 1: tạo/trích xuất audio.", "INFO")
                            txt_path = self._run_audio_worker(
                                job.get("audio", ""),
                                auto_continue=False,
                                audio_settings=self._audio_settings_for_job(job_name),
                                target_txt_var=self.v_file2 if job_name == "File 2" else self.v_file,
                                script_path=job.get("script", ""),
                                preset_name=job["preset"]
                            )
                        if not txt_path:
                            raise RuntimeError("Bước 1 không tạo được TXT.")
                        job["txt"] = txt_path
                    elif job.get("txt"):
                        self.log_job(job_name, "Bỏ qua Bước 1 vì đã có TXT.", "INFO")

                if job_name == "File 1":
                    gates["file2_audio"].set()

                if self.v_do_gemini.get():
                    if job_name == "File 2" and not wait_gate(job_name, "file2_gemini", "File 1 rời Bước 2"):
                        return
                    if self._stop.is_set():
                        return
                    txt_path = job.get("txt", "")
                    if not txt_path or not Path(txt_path).exists():
                        raise RuntimeError("Thiếu TXT cho Bước 2.")
                    with locks["gemini"]:
                        if self._stop.is_set():
                            return
                        self._apply_preset_for_job_threadsafe(job["preset"], job_name)
                        self.log_job(job_name, "Bước 2: gửi TXT lên Gemini.", "INFO")
                        output_mgr = self._run_worker(txt_path, auto_pipeline=False, job_config=self._gemini_settings_for_job(job_name))
                    if not output_mgr:
                        raise RuntimeError("Bước 2 thất bại.")
                    job["output_mgr"] = output_mgr

                if job_name == "File 1":
                    gates["file2_gemini"].set()

                if self.v_do_pipeline.get():
                    if job_name == "File 2" and not wait_gate(job_name, "file2_pipeline", "File 1 rời Bước 3"):
                        return
                    if self._stop.is_set():
                        return
                    with locks["pipeline"]:
                        if self._stop.is_set():
                            return
                        self._apply_preset_for_job_threadsafe(job["preset"], job_name)
                        if job.get("output_mgr"):
                            self._output_mgr = job["output_mgr"]
                            self._pl_auto_fill_outputs_threadsafe(job_name, job["output_mgr"])
                        self.log_job(job_name, "Bước 3: chạy pipeline.", "INFO")
                        ok = self._run_pipeline_blocking(job_name=job_name)
                    if not ok:
                        raise RuntimeError("Pipeline thất bại hoặc bị dừng.")

                if job_name == "File 1":
                    gates["file2_pipeline"].set()

                if not self._stop.is_set():
                    self.log_job(job_name, "Hoàn thành file.", "SUCCESS")
            except Exception as e:
                failures.append(f"{job_name}: {e}")
                self.log_job(job_name, f"Lỗi: {e}", "ERROR")
                if job_name == "File 1":
                    self._stop.set()
                    release_file2_gates()
                    self.log_job("File 2", "Dừng do File 1 lỗi.", "WARNING")
            finally:
                if job_name == "File 1":
                    release_file2_gates()
                self._set_job_log_context(previous_context)

        threads = [
            threading.Thread(target=run_job, args=(jobs[0],), daemon=True, name="File1FlowWorker"),
            threading.Thread(target=run_job, args=(jobs[1],), daemon=True, name="File2FlowWorker"),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        if failures or self._stop.is_set():
            self.after(0, self.v_status.set, "Dừng hoặc lỗi")
            self.log("Chế độ xen kẽ đã dừng hoặc có lỗi.", "ERROR" if failures else "WARNING")
            return

        self.after(0, self.v_status.set, f"Hoàn thành {job_count} file")
        self.log(f"Đã hoàn thành chế độ xen kẽ ({job_count} file).", "SUCCESS")
        elapsed = int(time.time() - self.global_start_time) if hasattr(self, "global_start_time") else 0
        self.after(500, self._show_final_popup, f"{elapsed // 60} phút {elapsed % 60} giây")

    def _run_single_job_pipeline(self, job: Dict[str, Any]):
        previous_context = self._set_job_log_context(job["name"])
        try:
            if self.v_do_audio.get() and job.get("audio"):
                txt_path = self._run_audio_worker(
                    job["audio"],
                    auto_continue=False,
                    audio_settings=self._audio_settings_for_job(job["name"]),
                    target_txt_var=self.v_file2 if job["name"] == "File 2" else self.v_file,
                )
                if not txt_path:
                    return
                job["txt"] = txt_path
            if self.v_do_gemini.get():
                output_mgr = self._run_worker(job.get("txt", ""), auto_pipeline=False, job_config=self._gemini_settings_for_job(job["name"]))
                if not output_mgr:
                    return
                job["output_mgr"] = output_mgr
            if self.v_do_pipeline.get() and job.get("output_mgr"):
                self._output_mgr = job["output_mgr"]
                self._pl_auto_fill_outputs_threadsafe(job["name"], job["output_mgr"])
                self._run_pipeline_blocking(job_name=job["name"])
        finally:
            self._set_job_log_context(previous_context)

    def _do_pause(self):
        self._pause.clear()
        self.v_status.set("Tạm dừng")
        self.log("⏸ Tạm dừng. Nhấn 'Tiếp Tục' để chạy lại.", "WARNING")

    def _do_resume(self):
        self._pause.set()
        self.v_status.set("Đang chạy")
        self.log("▶ Tiếp tục.", "INFO")

    def _do_stop(self):
        self._stop.set(); self._pause.set()
        self.v_status.set("Đã dừng")
        self.log("⏹ Yêu cầu dừng đã gửi.", "WARNING")

    def _restart_app(self):
        import os, sys, time
        self.log("🔄 Đang khởi động lại ứng dụng...", "WARNING")
        try:
            self._stop.set()
        except: pass
        
        time.sleep(0.5)
        self.destroy()
        
        try:
            os.execl(sys.executable, sys.executable, *sys.argv)
        except Exception as e:
            self.log(f"Lỗi khi khởi động lại: {e}", "ERROR")

    def _dry_run(self):
        path = self.v_file.get().strip()
        if not path:
            messagebox.showwarning("Thiếu file", "Vui lòng chọn file TXT trước."); return
        try:
            from core.file_loader import load_lines
            from core.chunker import create_chunks
            lines  = load_lines(path, skip_empty=self.v_skip.get())
            chunks = create_chunks(lines, self.v_first.get(), self.v_next.get())
            out = Path(self.config.get("output_dir","outputs")) / "dry_run"
            out.mkdir(parents=True, exist_ok=True)
            for c in chunks:
                (out / f"{c.label}.txt").write_text(c.content, encoding="utf-8")
            self.log(f"✅ Dry Run: {len(chunks)} chunk → '{out}'", "SUCCESS")
            if messagebox.askyesno("Dry Run xong", f"Đã chia {len(chunks)} chunk.\nMở thư mục?"):
                subprocess.Popen(f'explorer "{out.resolve()}"')
        except Exception as e:
            self.log(f"Lỗi dry run: {e}", "ERROR")

    def _test_sel(self):
        if not self._playwright_available:
            self.log("Test Selectors bằng Playwright đã tắt vì máy này đang chạy theo Addon Extension.", "WARNING")
            return
        self.log("Đang test selectors...")
        def task():
            try:
                res = self._bc("test_selectors", progress_cb=lambda m: self.log(m,"DEBUG"), timeout=60)
                found = sum(1 for v in res.values() if v["found"])
                self.log(f"Test xong: {found}/{len(res)} selector tìm thấy.", "SUCCESS")
            except Exception as e:
                self.log(f"Lỗi test: {e}", "ERROR")
        threading.Thread(target=task, daemon=True).start()

    def _sel_model(self):
        if not self._playwright_available:
            self.log("Chọn model bằng Playwright đã tắt. Hãy dùng luồng Addon Extension.", "WARNING")
            return
        m = self.v_model.get().strip() or "Pro"
        self.log(f"Đang chọn model '{m}'...")
        def task():
            try:
                ok = self._bc("ensure_pro_model", m, timeout=30)
                self.log(f"✅ Đã chọn '{m}'." if ok else f"❌ Không tìm thấy '{m}'.",
                         "SUCCESS" if ok else "ERROR")
            except Exception as e:
                self.log(f"Lỗi chọn model: {e}", "ERROR")
        threading.Thread(target=task, daemon=True).start()

    def _retry_chunk(self):
        self.log("🔁 Đã nhận lệnh: Bắt buộc gửi lại chunk đang bị kẹt...", "WARNING")
        with _bridge_lock:
            for jid in ["file1", "file2"]:
                job = BRIDGE_JOBS[jid]
                if job["pending_chunk"] and job["pending_chunk"].get("cmd") == "send_chunk":
                    job["result"] = {
                        "status": "failed_retry", 
                        "error": "Người dùng bấm nút Gửi Lại thủ công", 
                        "blocks": []
                    }
                    job["event"].set()

    def _open_output(self):
        d = Path(self.config.get("output_dir","outputs")).resolve()
        if self._output_mgr:
            d = self._output_mgr.get_session_dir()
        if d.exists():
            subprocess.Popen(f'explorer "{d}"')
        else:
            messagebox.showinfo("Chưa có output", "Chưa có thư mục output nào.")

    # ─────────────────────────────────────────────
    # Worker thread
    # ─────────────────────────────────────────────

    def _run_audio_worker(
        self,
        audio_path: str,
        auto_continue: bool = True,
        audio_settings: Optional[Dict[str, Any]] = None,
        target_txt_var: Optional[tk.StringVar] = None,
        script_path: str = "",
        preset_name: str = ""
    ):
        from core.audio_processor.split_audio import split_audio
        from core.audio_processor.transcribe_whisper import transcribe_audio
        import os
        import subprocess
        from pathlib import Path
        import time

        job_name = getattr(self._job_log_context, "job_name", None)
        status_var = self.v_status2 if job_name == "File 2" else self.v_status
        cur_var = self.v_cur2 if job_name == "File 2" else self.v_cur

        if script_path and os.path.exists(script_path):
            self.log(f"Bắt đầu tạo Audio từ kịch bản: {script_path}", "INFO")
            self.after(0, status_var.set, "Đang sinh Audio (OmniVoice)...")
            
            # Đọc settings OmniVoice
            job_id = 2 if job_name == "File 2" else 1
            ov_settings = self._read_omnivoice_settings(job_id)

            p_name = preset_name or (self.v_url.get().strip() if job_name != "File 2" else self.v_url2.get().strip())
            preset_voice = self._saved_urls.get(p_name, {}).get("sample_voice", "")
            sample_voice = str(ov_settings.get("sample_voice", "")).strip() or preset_voice
            if not sample_voice:
                self.log(f"Cảnh báo: Chưa chọn giọng mẫu OmniVoice cho Luồng {job_id}. Hãy chọn trong Cài đặt OmniVoice hoặc trong Thể Loại.", "WARNING")
                return None
                
            omnivoice_dir = self._get_omnivoice_dir()
            python_exe = os.path.join(omnivoice_dir, "venv", "Scripts", "python.exe")
            cli_script = os.path.join(omnivoice_dir, "run_omnivoice_cli.py")
            
            script_dir = os.path.dirname(script_path)
            script_name = os.path.splitext(os.path.basename(script_path))[0]
            out_wav = os.path.join(script_dir, f"{script_name}_audio.wav")
            
            cmd = [
                python_exe, "-u", cli_script,
                "--text_file", script_path,
                "--ref_audio", sample_voice,
                "--output", out_wav,
                "--num_step", str(int(ov_settings["num_step"])),
                "--guidance_scale", str(float(ov_settings["guidance_scale"])),
                "--speed", str(float(ov_settings["speed"])),
                "--duration", str(float(ov_settings.get("duration", 0))),
                "--language", str(ov_settings.get("language", "Tự động")),
                "--denoise", str(bool(ov_settings["denoise"])).lower(),
                "--preprocess", str(bool(ov_settings["preprocess"])).lower(),
                "--postprocess", str(bool(ov_settings["postprocess"])).lower(),
                "--seed", str(int(ov_settings["seed"])),
                "--chunk_mode", str(ov_settings["chunk_mode"]),
                "--chunk_words", str(int(ov_settings.get("chunk_words", 25))),
                "--num_threads", str(max(1, min(20, int(ov_settings.get("num_threads", 1)))))
            ]
            self.log(f"Đang chạy OmniVoice CLI (Luồng {job_id}) (steps={ov_settings['num_step']}, gs={ov_settings['guidance_scale']}, speed={ov_settings['speed']}, batch={ov_settings.get('num_threads', 1)})...", "INFO")
            
            # Using Popen to stream stdout/stderr
            process = subprocess.Popen(cmd, cwd=omnivoice_dir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', bufsize=1)
            for line in iter(process.stdout.readline, ''):
                line = line.strip()
                if line:
                    self.log(f"[OmniVoice] {line}", "DEBUG")
            process.stdout.close()
            return_code = process.wait()
            
            if return_code == 0 and os.path.exists(out_wav):
                self.log(f"Tạo Audio thành công: {out_wav}", "SUCCESS")
                audio_path = out_wav
                # Update UI
                if job_name == "File 2":
                    self.after(0, self.v_audio2.set, out_wav)
                else:
                    self.after(0, self.v_audio.set, out_wav)
            else:
                self.log(f"Lỗi tạo Audio (OmniVoice). Mã lỗi: {return_code}", "ERROR")
                return None

        self.log("═" * 40)
        self.log(f"Bắt đầu trích xuất Audio: {audio_path}")
        self.after(0, status_var.set, "Đang phân tích Audio...")
        self.after(0, cur_var.set, "—")
        
        try:
            audio_settings = audio_settings or {}
            chunk_sec = 10
            model = str(audio_settings.get("model") or self.v_aud_model.get()).strip() or "large-v3"
            engine = str(audio_settings.get("engine") or self.v_aud_engine.get()).strip().lower() or "openai-whisper"
            lang_display = str(audio_settings.get("language") or self.v_aud_lang.get()).strip()
            
            lang_map = {
                "Tiếng Hàn (ko)": "ko",
                "Tiếng Nhật (ja)": "ja",
                "Tiếng Anh (en)": "en",
                "Tiếng Trung Phồn Thể (zh-tw)": "zh",
                "Auto": None,
            }
            lang = lang_map.get(lang_display, None)
            try:
                beam = max(1, int(audio_settings.get("beam_size", self.v_aud_beam.get())))
            except (TypeError, ValueError):
                beam = 5
            first4_raw = str(audio_settings.get("first4", self.v_aud_first4.get())).strip()
            first4_sec = 8 if first4_raw not in {"8", "10"} else int(first4_raw)

            audio_dir = os.path.dirname(os.path.abspath(audio_path))
            folder_name = os.path.basename(audio_dir)
            out_chunks_dir = os.path.join(audio_dir, "chunks")
            os.makedirs(out_chunks_dir, exist_ok=True)

            target_pbar = self.pbar2 if job_name == "File 2" and hasattr(self, "pbar2") else self.pbar
            self.after(0, lambda: target_pbar.config(value=0))
            
            def cbp(done, total, stage):
                self.after(0, status_var.set, f"{stage} {done}/{total}")
                self.after(0, self._set_task_progress, done, total, job_name)
                
            self.log(f"Đang phân tách âm thanh: 4 phút đầu {first4_sec}s/đoạn, phần sau 10s/đoạn...", "INFO")
            chunks, base_name, time_str = split_audio(
                audio_path,
                out_chunks_dir,
                chunk_sec,
                cbp,
                target_sr=16000,
                mono=True,
                phase1_chunk_seconds=first4_sec,
            )
            
            if self._stop.is_set():
                self.log("⏹ Hủy trích xuất theo yêu cầu.", "WARNING")
                self.after(0, status_var.set, "Đã dừng")
                return None

            self.log(f"Đang dịch với model {model}...", "INFO")
            txt_path = transcribe_audio(
                chunks,
                audio_dir,
                folder_name,
                time_str,
                model,
                cbp,
                engine=engine,
                language=lang,
                beam_size=beam,
                vad_filter=True,
            )
            
            self.log(f"🎉 Hoàn tất trích xuất thành: {txt_path}", "SUCCESS")
            self.after(0, status_var.set, "Sẵn sàng chạy gửi TXT")
            
            # Tự động gắn kết quả txt này vào UI để chạy luôn
            target_var = target_txt_var or self.v_file
            self.after(0, target_var.set, txt_path)
            self.after(0, self._preview_file, txt_path, job_name)
            
            if not auto_continue:
                return txt_path
            
            # --- AUTO START TXT SENDING ---
            if self.v_do_gemini.get() and not self._stop.is_set():
                self.log("▶ Tự động chuyển tiếp sang bước gửi Gemini...", "INFO")
                # Clear audio path so it doesn't loop if they click start again
                self.after(0, self.v_audio.set, "")
                return self._run_worker(txt_path)
            elif self.v_do_pipeline.get() and not self._stop.is_set():
                self.log("Bước 2 đang tắt nên không gửi Gemini. Không tự chạy Bước 3 vì chưa có output Gemini.", "WARNING")
                self.log(f"File TXT đã tạo: {txt_path}", "SUCCESS")
                self.after(0, status_var.set, "Hoàn thành bước 1")
            else:
                self.log("Đã dừng sau Bước 1 theo lựa chọn workflow.", "SUCCESS")
                self.after(0, status_var.set, "Hoàn thành bước 1")
             
            return txt_path
             
        except Exception as e:
            self.log(f"❌ Lỗi xử lý Audio: {e}", "ERROR")
            self.after(0, status_var.set, "Lỗi")
            import traceback
            traceback.print_exc()
            return None

    def _gemini_settings_for_job(self, job_name: Optional[str] = None) -> Dict[str, Any]:
        if job_name == "File 2":
            return {
                "first": self.v_first2.get(),
                "next": self.v_next2.get(),
                "model": self.v_model2.get(),
                "skip": self.v_skip2.get(),
                "save": self.v_save2.get(),
                "delay": self.v_delay2.get(),
                "delay_seconds": self.v_delayt2.get(),
            }
        return {
            "first": self.v_first.get(),
            "next": self.v_next.get(),
            "model": self.v_model.get(),
            "skip": self.v_skip.get(),
            "save": self.v_save.get(),
            "delay": self.v_delay.get(),
            "delay_seconds": self.v_delayt.get(),
        }

    def _run_worker(self, file_path: str, auto_pipeline: bool = True, job_config: Optional[Dict[str, Any]] = None):
        """
        Toàn bộ logic gửi chunk. Giao tiếp với Playwright qua self._bc().
        Chạy trong SendWorker thread riêng, không block UI.
        """
        from core.file_loader import load_lines
        from core.chunker import create_chunks
        from core.state_manager import SessionState, OutputManager

        job_name = getattr(self._job_log_context, "job_name", None)
        settings = job_config or self._gemini_settings_for_job(job_name)
        lines_var = self.v_lines2 if job_name == "File 2" else self.v_lines
        chunks_var = self.v_chunks2 if job_name == "File 2" else self.v_chunks
        status_var = self.v_status2 if job_name == "File 2" else self.v_status
        cur_var = self.v_cur2 if job_name == "File 2" else self.v_cur

        self.log("═" * 40)
        self.log(f"Bắt đầu phiên: {file_path}")
        status_var.set("Đang chạy")

        # ── Đọc file & chia chunk ──
        try:
            lines = load_lines(file_path, skip_empty=settings["skip"])
        except Exception as e:
            self.log(f"❌ Không đọc được file: {e}", "ERROR")
            status_var.set("Lỗi"); return

        chunks = create_chunks(lines, int(settings["first"]), int(settings["next"]))
        chunk_dir = Path(file_path).parent / "geminichunk"
        chunk_dir.mkdir(parents=True, exist_ok=True)
        for old_chunk in chunk_dir.glob("chunk_*.txt"):
            try:
                old_chunk.unlink()
            except Exception as e:
                self.log(f"⚠️ Không xóa được chunk cũ {old_chunk.name}: {e}", "WARNING")

        chunk_files: Dict[int, Path] = {}
        for chunk in chunks:
            chunk_file = chunk_dir / f"{chunk.label}.txt"
            chunk_file.write_text(chunk.content, encoding="utf-8")
            chunk_files[chunk.index] = chunk_file

        self._chunks = chunks
        total = len(chunks)
        lines_var.set(str(len(lines)))
        chunks_var.set(str(total))
        self._set_progress(0, total)
        self.log(f"✅ {len(lines)} dòng → {total} chunk.", "SUCCESS")
        self.log(f"📄 Đã lưu {total} file chunk TXT tại: {chunk_dir}", "INFO")

        # ── Output & Session ──
        output_mgr = OutputManager(self.config.get("output_dir","outputs"), input_file_path=file_path)
        self._output_mgr = output_mgr
        session = SessionState(output_mgr.get_session_dir())
        self._session_state = session
        session.init_session(file_path, len(lines), total, self.config)

        # ── Hướng dẫn Người dùng sử dụng Extension Bridge ──
        url = self._get_url_for_job(job_name) or "https://gemini.google.com/app"
        
        # Thêm query param z115_job để Extension tự động nhận diện và gán luồng không cần click
        job_param = "file2" if job_name == "File 2" else "file1"
        if "?" in url:
            url_with_param = f"{url}&z115_job={job_param}"
        else:
            url_with_param = f"{url}?z115_job={job_param}"

        self.log("🔗 Vui lòng mở trình duyệt Edge/Chrome thật của bạn và truy cập Gemini.", "INFO")
        self.log(f"📌 Tab Gemini sẽ tự động liên kết với luồng: {'FILE 1' if job_name != 'File 2' else 'FILE 2'}", "SUCCESS")
        
        # Mở URL tự động bằng trình duyệt đã cấu hình hoặc mặc định
        job_id = "file2" if job_name == "File 2" else "file1"
        
        # Quét đúng tab của luồng hiện tại. File 2 không được dùng nhầm heartbeat của File 1.
        tab_is_alive = False
        with _bridge_lock:
            job_state = BRIDGE_JOBS[job_id]
            tab_is_alive = bool(
                job_state["designated_tab_id"]
                and (time.time() - job_state["last_heartbeat"]) < 15
            )
        
        if not tab_is_alive:
            self._open_gemini_browser(url_with_param, f"mở tab Gemini cho {job_name}")
            
            self.log("⏳ Đang chờ trình duyệt kết nối tự động...", "WARNING")
        
        # Vòng lặp chờ trình duyệt kết nối
        while True:
            tab_is_alive = False
            with _bridge_lock:
                job_state = BRIDGE_JOBS[job_id]
                tab_is_alive = bool(
                    job_state["designated_tab_id"]
                    and (time.time() - job_state["last_heartbeat"]) < 15
                )
            
            if tab_is_alive:
                break
                
            if self._stop.is_set():
                return
            time.sleep(1)

        self.log("✅ Đã tìm thấy Tab đang hoạt động! Đã khóa tab này thành Worker duy nhất.", "SUCCESS")
        
        # Luôn yêu cầu Tab bẻ lái sang đúng link của luồng hiện tại (dù là File 1 hay File 2)
        self.log(f"🌐 Yêu cầu Tab hiện tại bẻ lái sang Link của {job_name}...", "INFO")
        with _bridge_lock:
            # Gửi lệnh redirect (dùng url gốc, không cần param để tránh F5 liên tục)
            BRIDGE_JOBS[job_id]["pending_chunk"] = {
                "cmd": "redirect",
                "url": url
            }
        time.sleep(4)  # Chờ 4s cho tab bẻ lái xong

        status_var.set("Đang chạy")

        # ── Vòng lặp gửi chunk (ADDON STYLE: reload & resend khi sai số dòng) ──
        model   = str(settings["model"]).strip() or "Pro"
        tout    = 900  # Timeout 900s giống addon
        max_chunk_retries = 6
        all_chunks_ok = True
        job_context = getattr(self._job_log_context, "job_name", None)

        def gemini_progress(message: str):
            if job_context:
                self.log_job(job_context, message, "DEBUG")
            else:
                self.log(message, "DEBUG")

        def _count_nonempty_lines(text: str) -> int:
            return len([line for line in str(text).splitlines() if line.strip()])

        def _build_chunk_error_detail(chunk_obj, payload: Dict[str, Any]) -> str:
            reason = str(payload.get("text", "")).strip() or "Không rõ lỗi"
            blocks = payload.get("blocks", []) or []
            block_parts = []
            for idx, block_text in enumerate(blocks[:3], start=1):
                block_parts.append(f"block {idx}: {_count_nonempty_lines(block_text)} dòng")
            block_summary = f"{len(blocks)} code block"
            if block_parts:
                block_summary += " (" + ", ".join(block_parts) + ")"
            return (
                f"Chunk {chunk_obj.index} [{chunk_obj.start_line}–{chunk_obj.end_line}] lỗi: "
                f"{reason} | Nhận được {block_summary}"
            )

        for chunk in chunks:
            if self._stop.is_set():
                self.log("⏹ Dừng theo yêu cầu.", "WARNING")
                session.mark_session_stopped()
                status_var.set("Đã dừng")
                return

            if not self._pause.is_set():
                self.log(f"⏸ Đang tạm dừng trước chunk {chunk.index}...", "WARNING")
            self._pause.wait()

            if self._stop.is_set():
                break

            self.log(f"─── Chunk {chunk.index}/{total}: dòng {chunk.start_line}–{chunk.end_line} ({chunk.line_count} dòng) ───")
            cur_var.set(
                f"{chunk.index}/{total} - {chunk.label} | dòng {chunk.start_line}–{chunk.end_line} ({chunk.line_count} dòng)"
            )
            session.mark_chunk_started(chunk.index)

            sent_at  = datetime.now().isoformat()
            retries  = 0
            success  = False
            response_data = {"text": "", "blocks": [], "retry": False}

            # ── Vòng lặp retry: reload & resend khi sai số dòng (giống addon) ──
            while True:
                if self._stop.is_set():
                    break

                retries += 1
                self.log(f"Lần gửi {retries}/{max_chunk_retries} cho chunk {chunk.index}...", "INFO" if retries == 1 else "WARNING")

                job_id = "file2" if job_name == "File 2" else "file1"
                try:
                    initial_url = self._get_url_for_job(job_name) or "https://gemini.google.com/app"
                    success, response_data = self._send_chunk_via_addon(
                        job_id=job_id,
                        chunk_index=chunk.index,
                        content=chunk.content,
                        expected_lines=chunk.line_count,
                        baseline_text="",
                        initial_url=initial_url
                    )
                except Exception as e:
                    success = False
                    response_data = {"text": str(e), "blocks": [], "retry": False}

                if success:
                    # Chunk OK → lưu kết quả và thoát retry loop
                    break
                elif response_data.get("retry"):
                    detail = _build_chunk_error_detail(chunk, response_data)
                    self.log(
                        f"⚠️ Lỗi chunk {chunk.index} ở lần {retries}/{max_chunk_retries}: {detail}",
                        "WARNING"
                    )
                    if retries >= max_chunk_retries:
                        reason = response_data.get("text", "Không rõ lỗi")
                        self.log(
                            f"❌ Chunk {chunk.index} thất bại sau {max_chunk_retries} lần thử: {detail[:500]}",
                            "ERROR"
                        )
                        break
                    self.log(f"🔁 Extension đang tự động reload trang và gửi lại chunk {chunk.index}...", "WARNING")
                    time.sleep(3)
                    continue
                else:
                    # Lỗi thật sự → dừng
                    detail = _build_chunk_error_detail(chunk, response_data)
                    self.log(f"❌ Dừng do lỗi chunk {chunk.index}: {detail[:500]}", "ERROR")
                    break

            finished_at = datetime.now().isoformat()
            response_text = response_data.get("text", "")
            code_blocks = response_data.get("blocks", [])

            if success:
                # Chỉ lưu khi chunk thành công (giống addon)
                if settings["save"]:
                    output_mgr.save_chunk(chunk.index, chunk.content, response_text, code_blocks, {
                        "source_file": file_path,
                        "chunk_index": chunk.index,
                        "chunk_label": chunk.label,
                        "start_line":  chunk.start_line,
                        "end_line":    chunk.end_line,
                        "model_name":  model,
                        "sent_at":     sent_at,
                        "finished_at": finished_at,
                        "status":      "success",
                        "retry_count": retries,
                        "response_length": len(response_text),
                        "code_blocks_count": len(code_blocks),
                        "code_blocks": code_blocks
                    })
                session.mark_chunk_completed(chunk.index)
                self.log(f"✅ Chunk {chunk.index} xong (lần {retries}). {len(response_text)} ký tự | {len(code_blocks)} code block.", "SUCCESS")

                # Đi tiếp luôn (chỉ nghỉ nhẹ 5s cho Extension có thời gian ép lưu nháp)
                if chunk.index < total:
                    self.log("⏩ Đã nhận kết quả thành công, chuẩn bị gửi ngay chunk tiếp theo sau 5s...", "INFO")
                    time.sleep(5)
            else:
                detail = _build_chunk_error_detail(chunk, response_data)
                session.mark_chunk_failed(chunk.index, detail[:500])
                status_var.set(f"Lỗi chunk {chunk.index}")
                cur_var.set(f"Chunk {chunk.index} lỗi | {response_text[:140]}")
                self.log(f"❌ Chunk {chunk.index} thất bại: {detail[:500]}", "ERROR")
                all_chunks_ok = False
                break

            self._set_progress(chunk.index, total)

        if not all_chunks_ok:
            session.mark_session_stopped()
            status_var.set("Dừng do lỗi")
            self.log("Đã dừng ở chunk lỗi, không gửi tiếp và không build output cuối.", "ERROR")
            
            return

        session.mark_session_done()
        output_mgr.build_final_output()
        status_var.set("Hoàn thành ✅")
        self.log("═" * 40)
        self.log(f"🎉 Xong! {total} chunk đã gửi.", "SUCCESS")
        self.log(f"📁 Kết quả tại: {output_mgr.get_session_dir()}", "SUCCESS")



        # ── Chỉ chuyển Bước 3 khi TẤT CẢ chunks thành công ──
        if all_chunks_ok and self.v_do_pipeline.get() and auto_pipeline:
            self.after(0, self._notify_done, total, str(output_mgr.get_session_dir()))
        elif all_chunks_ok:
            self.log("Đã hoàn thành Bước 2. Bước 3 đang tắt nên không tự tạo ảnh/video.", "SUCCESS")
            self.log("Có thể bật Bước 3 và chạy tab Pipeline thủ công nếu cần.", "INFO")
        else:
            self.log("⚠️ Có chunk thất bại. KHÔNG tự động chuyển sang Bước 3.", "WARNING")
            self.log("→ Vui lòng kiểm tra kết quả và chạy lại nếu cần.", "WARNING")
        return output_mgr

    def _notify_done(self, total: int, session_dir: str):
        """Tự động chuyển tiếp sang Bước 3."""
        self.log("Đã gộp xong TXT, tự động chuyển sang Bước 3 (Pipeline)...", "INFO")
        
        self.notebook.select(1)
        self._pl_auto_fill_outputs()
        self._save_all_settings()

        self.after(500, self._pl_start)

    def _clear_browser_cache_files(self, folder_name: str):
        """Xóa các thư mục Cache, GPUCache để trình duyệt luôn sạch sẽ mà không mất Cookie login."""
        import shutil
        user_data_dir = Path(self.config.get("user_data_dir", folder_name)).resolve()
        
        if not user_data_dir.exists():
            return
            
        self.log(f"🧹 Đang quét dọn bộ nhớ đệm tại: {user_data_dir.name}...", "INFO")
        
        # Các thư mục cache phổ biến của Chromium
        cache_dirs = [
            "Cache",
            "Code Cache",
            "GPUCache",
            "Service Worker",
            "Default/Cache",
            "Default/Code Cache",
            "Default/GPUCache",
            "Default/Service Worker",
            "Default/IndexedDB"
        ]
        
        deleted_count = 0
        for sub_dir in cache_dirs:
            target = user_data_dir / sub_dir
            if target.exists():
                try:
                    if target.is_dir():
                        shutil.rmtree(target, ignore_errors=True)
                    else:
                        target.unlink(missing_ok=True)
                    deleted_count += 1
                except Exception as e:
                    logger.warning(f"Không thể xóa cache {sub_dir}: {e}")
                    
        self.log(f"✨ Đã dọn dẹp xong {deleted_count} vùng đệm dữ liệu thừa.", "SUCCESS")







    # ─────────────────────────────────────────────
    # STEP 3: PIPELINE AUTOMATION UI
    # ─────────────────────────────────────────────

    def _build_pipeline_ui(self, parent):
        container = tk.Frame(parent, bg=C["bg"])
        container.pack(fill="both", expand=True)

        canvas = tk.Canvas(container, bg=C["bg"], highlightthickness=0)
        scrollbar = ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
        body = tk.Frame(canvas, bg=C["bg"])
        
        def _on_frame_configure(event):
            canvas.configure(scrollregion=canvas.bbox("all"))
        body.bind("<Configure>", _on_frame_configure)
        
        canvas_window = canvas.create_window((0, 0), window=body, anchor="nw")
        
        def _on_canvas_configure(event):
            canvas.itemconfig(canvas_window, width=event.width)
        canvas.bind("<Configure>", _on_canvas_configure)
        
        canvas.configure(yscrollcommand=scrollbar.set)
        
        # Local mousewheel binding
        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1*(event.delta/120)), "units")
        canvas.bind("<Enter>", lambda e: canvas.bind_all("<MouseWheel>", _on_mousewheel))
        canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))

        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        # ── Top Section: Shared Settings & Controls ──
        top = tk.Frame(body, bg=C["bg"])
        top.pack(fill="x", padx=8, pady=(4, 6))
        
        # Server & API Key
        server_f = tk.Frame(top, bg=C["bg"], highlightthickness=1, highlightbackground=C["border"])
        server_f.pack(side="left", padx=(0, 6))
        tk.Label(server_f, text="⚙️ SERVER & API KEY", font=("Segoe UI Semibold", 8), fg=C["accent"], bg=C["bg"]).pack(anchor="w", padx=6, pady=(2, 1))
        sf = tk.Frame(server_f, bg=C["bg"]); sf.pack(fill="x", padx=6, pady=2)
        
        sg = tk.Frame(sf, bg=C["bg"])
        sg.pack(side="left")
        for i, (l, v) in enumerate([("Host:", self.pl_host), ("Port:", self.pl_port), ("API Key:", self.pl_api_key)]):
            tk.Label(sg, text=l, font=("Segoe UI", 8), fg=C["dim"], bg=C["bg"], width=5, anchor="w").grid(row=0, column=i*2, padx=(1, 0))
            _entry(sg, var=v, width=9 if i < 2 else 15).grid(row=0, column=i*2+1, padx=(1, 4))
        
        _btn(sf, "🔌 Test", self._pl_test_connection, bg=C["info"]).pack(side="left", padx=2)

        # Global Control (Pause/Stop)
        ctrl_f = tk.Frame(top, bg=C["bg"], highlightthickness=1, highlightbackground=C["border"])
        ctrl_f.pack(side="left", fill="y")
        tk.Label(ctrl_f, text="🎮 ĐIỀU KHIỂN CHUNG", font=("Segoe UI Semibold", 8), fg=C["accent"], bg=C["bg"]).pack(anchor="w", padx=6, pady=(2, 1))
        cf = tk.Frame(ctrl_f, bg=C["bg"]); cf.pack(fill="x", padx=6, pady=2)
        
        self.btn_pl_pause = _btn(cf, "⏸️ TẠM DỪNG", self._pl_toggle_pause, bg=C["warn"])
        self.btn_pl_pause.pack(side="left", padx=1)
        self.btn_pl_pause.config(state="disabled", width=10, font=("Segoe UI", 8))

        self.btn_pl_stop = _btn(cf, "⏹️ DỪNG", self._pl_stop, bg=C["err"])
        self.btn_pl_stop.pack(side="left", padx=1)
        self.btn_pl_stop.config(state="disabled", width=8, font=("Segoe UI", 8))

        # ── Main 2-Column Layout ──
        cols = tk.Frame(body, bg=C["bg"])
        cols.pack(fill="both", expand=True, padx=8)
        cols.grid_columnconfigure(0, weight=1)
        cols.grid_columnconfigure(1, weight=1)

        # File 1 Column
        col1 = tk.Frame(cols, bg=C["bg"])
        col1.grid(row=0, column=0, sticky="nsew", padx=(0, 2))
        self._build_pipeline_job_panel(col1, "File 1")

        # File 2 Column
        col2 = tk.Frame(cols, bg=C["bg"])
        col2.grid(row=0, column=1, sticky="nsew", padx=(2, 0))
        self._build_pipeline_job_panel(col2, "File 2")

    def _build_pipeline_job_panel(self, parent, job_name):
        is_f2 = job_name == "File 2"
        
        # 1. Config Section
        if not is_f2:
            self._build_pipeline_config_panel(
                parent, f"📁 Cài đặt {job_name}", 0,
                self.pl_img_model, self.pl_vid_model, self.pl_aspect, self.pl_res, self.pl_category, self.pl_threads, self.pl_video_threads,
                self.pl_run_step1, self.pl_run_step2, self.pl_run_step3,
                self.pl_whisk_file, self.pl_veo_file, self.pl_luot2_file, self.pl_ref_image,
                self._pl_auto_fill_outputs,
                lambda: self._pl_pick_and_fill_folder(job_name="File 1"),
                is_grid=False
            )
            self.btn_pl_start = _btn(parent, f"▶️ BẮT ĐẦU {job_name}", self._pl_start, bg=C["ok"])
            self.btn_pl_start.pack(fill="x", pady=2)
            self.btn_pl_start.config(font=("Segoe UI Semibold", 8))
        else:
            self._build_pipeline_config_panel(
                parent, f"📁 Cài đặt {job_name}", 0,
                self.pl_img_model2, self.pl_vid_model2, self.pl_aspect2, self.pl_res2, self.pl_category2, self.pl_threads2, self.pl_video_threads2,
                self.pl_run_step12, self.pl_run_step22, self.pl_run_step32,
                self.pl_whisk_file2, self.pl_veo_file2, self.pl_luot2_file2, self.pl_ref_image2,
                lambda: self._pl_auto_fill_outputs(job_name="File 2"),
                lambda: self._pl_pick_and_fill_folder(job_name="File 2"),
                is_grid=False
            )
            self.btn_pl_start2 = _btn(parent, f"▶️ BẮT ĐẦU {job_name}", self._pl_start_file2, bg=C["teal"])
            self.btn_pl_start2.pack(fill="x", pady=2)
            self.btn_pl_start2.config(font=("Segoe UI Semibold", 8))

        # 2. Progress Section
        prog_f = tk.Frame(parent, bg=C["bg"], highlightthickness=1, highlightbackground=C["border"])
        prog_f.pack(fill="x", pady=2)
        tk.Label(prog_f, text=f"📊 Tiến trình {job_name}", font=("Segoe UI Semibold", 8), fg=C["accent"], bg=C["bg"]).pack(anchor="w", padx=6, pady=2)
        
        def _pb(lbl_text):
            lbl = tk.Label(prog_f, text=f"{lbl_text}: 0/0", font=("Segoe UI", 7), fg=C["dim"], bg=C["bg"])
            lbl.pack(anchor="w", padx=6)
            pb = ttk.Progressbar(prog_f, orient="horizontal", mode="determinate")
            pb.pack(fill="x", padx=6, pady=(0, 2))
            return lbl, pb

        l_w, p_w = _pb("B1 (WHISK)")
        l_v, p_v = _pb("B2 (VEO)")
        l_l, p_l = _pb("B3 (Lượt 2)")

        if is_f2:
            self.lbl_p_w2, self.prog_w2 = l_w, p_w
            self.lbl_p_v2, self.prog_v2 = l_v, p_v
            self.lbl_p_l2, self.prog_l2 = l_l, p_l
        else:
            self.lbl_p_w, self.prog_w = l_w, p_w
            self.lbl_p_v, self.prog_v = l_v, p_v
            self.lbl_p_l, self.prog_l = l_l, p_l

        # 3. Failed items table
        fail_f = tk.Frame(parent, bg=C["bg"], highlightthickness=1, highlightbackground=C["border"])
        fail_f.pack(fill="x", pady=2)
        
        fh = tk.Frame(fail_f, bg=C["bg"])
        fh.pack(fill="x", padx=6, pady=2)
        tk.Label(fh, text="⚠️ Lỗi:", font=("Segoe UI", 8), fg=C["warn"], bg=C["bg"]).pack(side="left")
        
        retry_cmd = (lambda: self._pl_retry_failed(job_name="File 2")) if is_f2 else (lambda: self._pl_retry_failed(job_name="File 1"))
        btn_retry = _btn(fh, "▶ Chạy lại", retry_cmd, bg=C["accent2"])
        btn_retry.pack(side="right")
        btn_retry.config(state="disabled", font=("Segoe UI", 7), pady=1)
        
        if is_f2: self.btn_pl_retry_failed2 = btn_retry
        else: self.btn_pl_retry_failed = btn_retry

        tw = tk.Frame(fail_f, bg=C["border"])
        tw.pack(fill="x", padx=6, pady=(0, 4))
        tree = ttk.Treeview(tw, columns=("name", "prompt", "reason", "status", "retry"), show="headings", height=6)
        tree.heading("name", text="Tên")
        tree.heading("prompt", text="Prompt")
        tree.heading("reason", text="Lỗi")
        tree.heading("status", text="Trạng thái")
        tree.heading("retry", text="C.lại")
        tree.column("name", width=70)
        tree.column("prompt", width=180)
        tree.column("reason", width=100)
        tree.column("status", width=80)
        tree.column("retry", width=50, anchor="center")
        tree.pack(side="left", fill="both", expand=True)
        
        sb = ttk.Scrollbar(tw, orient="vertical", command=tree.yview)
        sb.pack(side="right", fill="y")
        tree.configure(yscrollcommand=sb.set)
        
        tree.bind("<Button-1>", self._pl_failed_tree_click)
        tree.bind("<Double-1>", self._pl_edit_failed_prompt)

        if is_f2: self.pl_failed_tree2 = tree
        else: self.pl_failed_tree = tree

        # 4. Log Panel
        log_f = tk.Frame(parent, bg=C["bg"], highlightthickness=1, highlightbackground=C["border"])
        log_f.pack(fill="both", expand=True, pady=2)
        tk.Label(log_f, text=f"📜 Log {job_name}:", font=("Segoe UI Semibold", 8), fg=C["dim"], bg=C["bg"]).pack(anchor="w", padx=6, pady=2)
        
        lw = scrolledtext.ScrolledText(log_f, height=12, state="normal", bg="#0d0d1a", fg=C["text"], font=FC, relief="flat", bd=0)
        lw.pack(fill="both", expand=True, padx=6, pady=(0, 4))
        for tag, col in [("INFO",C["text"]),("SUCCESS",C["ok"]),("WARNING",C["warn"]), ("ERROR",C["err"]),("DEBUG",C["dim"])]:
            lw.tag_config(tag, foreground=col)
        
        if is_f2: self.pl_log_w2 = lw
        else: self.pl_log_w = lw

    def _build_pipeline_config_panel(
        self, parent, title, col,
        img_model, vid_model, aspect, res, category, image_threads, video_threads,
        run_step1, run_step2, run_step3,
        whisk_file, veo_file, luot2_file, ref_image,
        auto_fill_cmd,
        folder_fill_cmd,
        is_grid=True
    ):
        box = tk.Frame(parent, bg=C["bg"], highlightthickness=1, highlightbackground=C["border"])
        if is_grid:
            box.grid(row=0, column=col, sticky="nsew", padx=(0, 2) if col == 0 else (2, 0))
        else:
            box.pack(fill="x", pady=2)
            
        # Header with toggle button
        header = tk.Frame(box, bg=C["bg"])
        header.pack(fill="x", padx=6, pady=2)
        tk.Label(header, text=title, font=("Segoe UI Semibold", 8), fg=C["accent"], bg=C["bg"]).pack(side="left")
        
        content_f = tk.Frame(box, bg=C["bg"])
        # Do not pack content_f yet
        
        def toggle():
            if content_f.winfo_viewable():
                content_f.pack_forget()
                btn_toggle.config(text="⚙️", bg=C["surface2"])
            else:
                content_f.pack(fill="x", after=header)
                btn_toggle.config(text="❌", bg=C["err"])
        
        btn_toggle = _btn(header, "⚙️", toggle, bg=C["surface2"], font=("Segoe UI", 7), pady=1, width=3)
        btn_toggle.pack(side="right")

        # ── Settings Content (Hidden by default) ──
        pf = tk.Frame(content_f, bg=C["bg"])
        pf.pack(fill="x", padx=6, pady=1)
        fields = [
            ("Image Model:", img_model, ["imagen4", "nano_banana", "nano_banana_pro", "nano_banana_2"]),
            ("Video Model:", vid_model, VIDEO_MODELS),
            ("Aspect Ratio:", aspect, ["16:9", "9:16", "1:1", "3:4", "4:3"]),
            ("Resolution:", res, ["720p", "1080p"]),
            ("Ref Type:", category, ["subject", "scene", "style"]),
            ("L.ảnh:", image_threads, ["1", "2", "3", "5", "10", "15", "20", "30", "50"]),
            ("L.video:", video_threads, ["1", "2", "3", "5", "10", "15", "20", "30", "50"]),
        ]
        for row, (label, var, values) in enumerate(fields):
            tk.Label(pf, text=label, font=("Segoe UI", 8), fg=C["dim"], bg=C["bg"]).grid(row=row, column=0, sticky="w")
            ttk.Combobox(pf, textvariable=var, values=values, state="readonly", width=12).grid(row=row, column=1, pady=1, sticky="e")

        # ── Always Visible Section ──
        visible_f = tk.Frame(box, bg=C["bg"])
        visible_f.pack(fill="x", padx=6, pady=2)

        steps_f = tk.Frame(visible_f, bg=C["bg"])
        steps_f.pack(fill="x", pady=1)
        for text, var in [("B1", run_step1), ("B2", run_step2), ("B3", run_step3)]:
            tk.Checkbutton(
                steps_f, text=text, variable=var,
                command=self._save_all_settings,
                bg=C["bg"], fg=C["text"], activebackground=C["bg"],
                selectcolor=C["surface2"], font=("Segoe UI", 8)
            ).pack(side="left", padx=(0, 4))
            
        self._file_row(visible_f, "30WHISK:", whisk_file)
        self._file_row(visible_f, "30VEO:", veo_file)
        self._file_row(visible_f, "Luot2:", luot2_file)
        self._file_row(visible_f, "Tham chiếu:", ref_image, is_image=True)

        # ── Buttons ──
        auto_f = tk.Frame(box, bg=C["bg"])
        auto_f.pack(fill="x", padx=6, pady=(0, 2))
        
        auto_btn = _btn(auto_f, "📁 Nạp Output", auto_fill_cmd, bg=C["teal"])
        auto_btn.config(pady=1, font=("Segoe UI", 8))
        auto_btn.pack(side="left", fill="x", expand=True, padx=(0, 2))
        
        folder_btn = _btn(auto_f, "📁 Chọn TXT", folder_fill_cmd, bg=C["surface2"])
        folder_btn.config(pady=1, font=("Segoe UI", 8))
        folder_btn.pack(side="left", fill="x", expand=True, padx=(2, 0))

    def _file_row(self, p, lbl, var, is_image=False):
        row = tk.Frame(p, bg=C["bg"]); row.pack(fill="x", pady=0)
        tk.Label(row, text=lbl, font=("Segoe UI", 8), fg=C["dim"], bg=C["bg"], width=10, anchor="w").pack(side="left")
        
        def _br():
            types = [("Image files", "*.png;*.jpg;*.jpeg;*.webp")] if is_image else [("Text files", "*.txt")]
            path = filedialog.askopenfilename(filetypes=types)
            if path: var.set(path)
            
        browse_btn = _btn(row, "...", _br, width=2)
        browse_btn.config(pady=0, font=("Segoe UI", 7))
        browse_btn.pack(side="right")
        _entry(row, var=var, font=("Segoe UI", 8)).pack(side="left", fill="x", expand=True, padx=2)

    def _pl_test_connection(self):
        from core.pipeline.api_client import APIClient
        client = APIClient(self.pl_host.get(), int(self.pl_port.get()), self.pl_api_key.get(), log_cb=self._pl_log)
        if client.check_health():
            messagebox.showinfo("Success", "Kết nối tới Webhook Server thành công!")
            self._pl_log("Test connection successful.", "SUCCESS")
        else:
            messagebox.showerror("Error", "Không thể kết nối. Kiểm tra URL, Port hoặc API Key.")
            self._pl_log("Test connection failed.", "ERROR")
            
    def _pl_auto_fill_outputs(self, job_name: Optional[str] = None, output_mgr=None):
        # Scan thư mục outputs cho session mới nhất
        out_root = Path(self.config.get("output_dir","outputs"))
        if not out_root.exists():
            self._pl_log("Chưa có thư mục outputs.", "WARNING")
            return
            
        # Tìm thư mục output
        # Trong output_mgr, nếu có file gốc, nó đẩy ra thư mục của file gốc, nếu khônh có thì nằm trong session.
        # Ở đây scan trong `out_root`
        out_dir = out_root
        mgr = output_mgr or self._output_mgr
        if mgr and mgr.input_file_path:
             out_dir = Path(mgr.input_file_path).parent

        self._pl_fill_pipeline_files_from_dir(out_dir, job_name=job_name)

    def _pl_pick_and_fill_folder(self, job_name: Optional[str] = None):
        path = filedialog.askdirectory(title="Chọn thư mục chứa file TXT pipeline")
        if not path:
            return
        self._pl_fill_pipeline_files_from_dir(Path(path), job_name=job_name)

    def _pl_fill_pipeline_files_from_dir(self, out_dir: Path, job_name: Optional[str] = None):
        target_whisk = self.pl_whisk_file2 if job_name == "File 2" else self.pl_whisk_file
        target_veo = self.pl_veo_file2 if job_name == "File 2" else self.pl_veo_file
        target_luot2 = self.pl_luot2_file2 if job_name == "File 2" else self.pl_luot2_file
        label = job_name or "File 1"

        if not out_dir.exists():
            self._pl_log(f"{label}: Thư mục không tồn tại: {out_dir}", "WARNING")
            return
        if not out_dir.is_dir():
            self._pl_log(f"{label}: Đường dẫn không phải thư mục: {out_dir}", "WARNING")
            return

        self._pl_log(f"{label}: Đang scan thư mục TXT: {out_dir}", "INFO")

        files = sorted(
            [f for f in out_dir.iterdir() if f.is_file() and f.suffix.lower() == ".txt"],
            key=lambda x: x.stat().st_mtime,
            reverse=True,
        )
        if not files:
            self._pl_log(f"{label}: Không tìm thấy file TXT nào.", "WARNING")
            return

        matches = {"whisk": None, "veo": None, "luot2": None}
        for f in files:
            fname = f.name.lower()
            if matches["whisk"] is None and "30whisk" in fname:
                matches["whisk"] = f
            if matches["veo"] is None and ("30veo" in fname or "veo3" in fname):
                matches["veo"] = f
            if matches["luot2"] is None and "luot2" in fname:
                matches["luot2"] = f

        if matches["whisk"]:
            target_whisk.set(str(matches["whisk"]))
            self._pl_log(f"{label}: Đã nạp file B1 (WHISK): {matches['whisk'].name}", "SUCCESS")
        else:
            self._pl_log(f"{label}: Thiếu file TXT chứa '30whisk'.", "WARNING")

        if matches["veo"]:
            target_veo.set(str(matches["veo"]))
            self._pl_log(f"{label}: Đã nạp file B2 (VEO): {matches['veo'].name}", "SUCCESS")
        else:
            self._pl_log(f"{label}: Thiếu file TXT chứa '30veo' hoặc 'veo3'.", "WARNING")

        if matches["luot2"]:
            target_luot2.set(str(matches["luot2"]))
            self._pl_log(f"{label}: Đã nạp file B3 (LUOT2): {matches['luot2'].name}", "SUCCESS")
        else:
            self._pl_log(f"{label}: Thiếu file TXT chứa 'luot2'.", "WARNING")

    def _pl_log(self, msg: str, level: str = "INFO", job_name: Optional[str] = None):
        self.after(0, self._pl_log_ui, msg, level, job_name)
        
    def _pl_log_ui(self, msg, level, job_name=None):
        ts = datetime.now().strftime("%H:%M:%S")
        # Route strictly: File 2 → pl_log_w2, everything else → pl_log_w
        if job_name == "File 2" and hasattr(self, "pl_log_w2"):
            w = self.pl_log_w2
        elif hasattr(self, "pl_log_w"):
            w = self.pl_log_w
        else:
            return
        w.insert("end", f"[{ts}] {msg}\n", level)
        w.see("end")
        
    def _pl_progress_cb(self, section, current, total, job_name=None):
        self.after(0, self._pl_progress_ui, section, current, total, job_name)
        
    def _pl_progress_ui(self, section, current, total, job_name=None):
        pct = (current / total * 100) if total > 0 else 0
        is_f2 = job_name == "File 2"
        
        l_w = self.lbl_p_w2 if is_f2 and hasattr(self, "lbl_p_w2") else self.lbl_p_w
        p_w = self.prog_w2 if is_f2 and hasattr(self, "prog_w2") else self.prog_w
        l_v = self.lbl_p_v2 if is_f2 and hasattr(self, "lbl_p_v2") else self.lbl_p_v
        p_v = self.prog_v2 if is_f2 and hasattr(self, "prog_v2") else self.prog_v
        l_l = self.lbl_p_l2 if is_f2 and hasattr(self, "lbl_p_l2") else self.lbl_p_l
        p_l = self.prog_l2 if is_f2 and hasattr(self, "prog_l2") else self.prog_l

        if section == "WHISK_IMAGES":
            l_w.config(text=f"Bước 1 (WHISK): {current}/{total}")
            p_w["maximum"] = 100
            p_w["value"] = pct
        elif section == "VEO_VIDEOS":
            l_v.config(text=f"Bước 2 (VEO): {current}/{total}")
            p_v["maximum"] = 100
            p_v["value"] = pct
        elif section == "LUOT2_IMAGES":
            l_l.config(text=f"Bước 3 (Lượt 2): {current}/{total}")
            p_l["maximum"] = 100
            p_l["value"] = pct

    def _pl_current_settings(self, job_name: Optional[str] = None):
        is_file2 = job_name == "File 2"
        image_threads = int((self.pl_threads2 if is_file2 else self.pl_threads).get())
        video_threads = int((self.pl_video_threads2 if is_file2 else self.pl_video_threads).get())
        return {
            'image_model': (self.pl_img_model2 if is_file2 else self.pl_img_model).get(),
            'video_model': (self.pl_vid_model2 if is_file2 else self.pl_vid_model).get(),
            'aspect_ratio': (self.pl_aspect2 if is_file2 else self.pl_aspect).get(),
            'resolution': (self.pl_res2 if is_file2 else self.pl_res).get(),
            'category': (self.pl_category2 if is_file2 else self.pl_category).get(),
            'threads': image_threads,
            'image_threads': image_threads,
            'video_threads': video_threads,
            'run_step1': (self.pl_run_step12 if is_file2 else self.pl_run_step1).get(),
            'run_step2': (self.pl_run_step22 if is_file2 else self.pl_run_step2).get(),
            'run_step3': (self.pl_run_step32 if is_file2 else self.pl_run_step3).get()
        }

    def _pl_clear_failed_table(self, job_name=None):
        is_f2 = job_name == "File 2"
        items = self.pl_failed_items2 if is_f2 else self.pl_failed_items
        running = self.pl_retry_running_items2 if is_f2 else self.pl_retry_running_items
        tree = self.pl_failed_tree2 if is_f2 else self.pl_failed_tree
        btn = self.btn_pl_retry_failed2 if is_f2 else self.btn_pl_retry_failed
        
        items.clear()
        running.clear()
        if tree:
            for iid in tree.get_children():
                tree.delete(iid)
        if btn:
            btn.config(state="disabled")

    def _pl_failed_item_cb(self, item, job_name=None):
        self.after(0, self._pl_add_failed_item_ui, item, job_name)

    def _pl_add_failed_item_ui(self, item, job_name=None):
        is_f2 = job_name == "File 2"
        tree = self.pl_failed_tree2 if is_f2 else self.pl_failed_tree
        items_dict = self.pl_failed_items2 if is_f2 else self.pl_failed_items
        running_set = self.pl_retry_running_items2 if is_f2 else self.pl_retry_running_items
        btn = self.btn_pl_retry_failed2 if is_f2 else self.btn_pl_retry_failed
        
        if not tree: return
        iid = item.get("id") or f"{item.get('section')}:{item.get('index')}:{item.get('name')}"
        item["id"] = iid
        existing = items_dict.get(iid, {})
        status = existing.get("status") if iid in running_set else item.get("status", existing.get("status", "Lỗi"))
        action = "Đang chạy" if iid in running_set else "▶ Chạy lại"
        item["status"] = status
        reason = item.get("reason", existing.get("reason", ""))
        item["reason"] = reason
        items_dict[iid] = dict(item)
        values = (item.get("name", ""), item.get("prompt", ""), reason, status, action)
        if tree.exists(iid):
            tree.item(iid, values=values)
        else:
            tree.insert("", "end", iid=iid, values=values)
        if btn and not self.pl_retry_is_running:
            btn.config(state="normal")

    def _pl_retry_success_cb(self, item, job_name=None):
        self.after(0, self._pl_retry_success_ui, item, job_name)

    def _pl_retry_success_ui(self, item, job_name=None):
        is_f2 = job_name == "File 2"
        iid = item.get("id") or f"{item.get('section')}:{item.get('index')}:{item.get('name')}"
        running_set = self.pl_retry_running_items2 if is_f2 else self.pl_retry_running_items
        items_dict = self.pl_failed_items2 if is_f2 else self.pl_failed_items
        tree = self.pl_failed_tree2 if is_f2 else self.pl_failed_tree
        btn = self.btn_pl_retry_failed2 if is_f2 else self.btn_pl_retry_failed

        running_set.discard(iid)
        if iid in items_dict:
            items_dict[iid]["status"] = "Đã chạy lại"
        if tree and tree.exists(iid):
            vals = list(tree.item(iid, "values"))
            while len(vals) < 5: vals.append("")
            vals[3] = "Đã chạy lại"
            vals[4] = "Xong"
            tree.item(iid, values=vals)
            self.after(2000, lambda: self._pl_remove_failed_item_ui(iid, job_name))
        if btn:
            state = "normal" if items_dict and not self.pl_retry_is_running else "disabled"
            btn.config(state=state)

    def _pl_remove_failed_item_ui(self, iid, job_name=None):
        is_f2 = job_name == "File 2"
        items_dict = self.pl_failed_items2 if is_f2 else self.pl_failed_items
        running_set = self.pl_retry_running_items2 if is_f2 else self.pl_retry_running_items
        tree = self.pl_failed_tree2 if is_f2 else self.pl_failed_tree
        btn = self.btn_pl_retry_failed2 if is_f2 else self.btn_pl_retry_failed

        items_dict.pop(iid, None)
        running_set.discard(iid)
        if tree and tree.exists(iid):
            tree.delete(iid)
        if btn:
            state = "normal" if items_dict and not self.pl_retry_is_running else "disabled"
            btn.config(state=state)

    def _pl_failed_tree_click(self, event):
        tree = event.widget
        if not tree: return
        region = tree.identify("region", event.x, event.y)
        column = tree.identify_column(event.x)
        iid = tree.identify_row(event.y)
        if region == "cell" and column == "#5" and iid:
            job_name = "File 2" if tree == self.pl_failed_tree2 else "File 1"
            self._pl_retry_single_failed(iid, job_name)
            return "break"

    def _pl_edit_failed_prompt(self, event):
        tree = event.widget
        if not tree: return
        region = tree.identify("region", event.x, event.y)
        column = tree.identify_column(event.x)
        iid = tree.identify_row(event.y)
        if region != "cell" or column != "#2" or not iid:
            return
        bbox = tree.bbox(iid, column)
        if not bbox: return
        if self._pl_edit_entry: self._pl_edit_entry.destroy()
        x, y, width, height = bbox
        current = tree.set(iid, "prompt")
        entry = tk.Entry(tree, bg="#ffffff", fg="#000000", relief="solid", bd=1)
        entry.insert(0, current)
        entry.select_range(0, "end")
        entry.focus_set()
        entry.place(x=x, y=y, width=width, height=height)
        self._pl_edit_entry = entry

        def commit(_event=None):
            if not entry.winfo_exists(): return
            new_prompt = entry.get()
            tree.set(iid, "prompt", new_prompt)
            job_name = "File 2" if tree == self.pl_failed_tree2 else "File 1"
            items_dict = self.pl_failed_items2 if job_name == "File 2" else self.pl_failed_items
            if iid in items_dict:
                items_dict[iid]["prompt"] = new_prompt
            entry.destroy()
            self._pl_edit_entry = None

        entry.bind("<Return>", commit)
        entry.bind("<FocusOut>", lambda e: commit())
        entry.bind("<Escape>", lambda e: entry.destroy())

    def _pl_retry_failed(self, job_name="File 1"):
        if self.pl_retry_is_running: return
        is_f2 = job_name == "File 2"
        items_dict = self.pl_failed_items2 if is_f2 else self.pl_failed_items
        running_set = self.pl_retry_running_items2 if is_f2 else self.pl_retry_running_items
        
        if not items_dict:
            messagebox.showinfo("Không có mục lỗi", f"Hiện không có ảnh/video lỗi cho {job_name} để chạy lại.")
            return
        items = [dict(item) for iid, item in items_dict.items() if iid not in running_set]
        if not items: return

        self._pl_start_retry_items(items, mode="all", job_name=job_name)

    def _pl_retry_single_failed(self, iid, job_name="File 1"):
        is_f2 = job_name == "File 2"
        running_set = self.pl_retry_running_items2 if is_f2 else self.pl_retry_running_items
        items_dict = self.pl_failed_items2 if is_f2 else self.pl_failed_items
        
        if iid in running_set: return
        item = items_dict.get(iid)
        if not item: return
        self._pl_start_retry_items([dict(item)], mode="single", job_name=job_name)

    def _pl_start_retry_items(self, items, mode="all", job_name="File 1"):
        is_f2 = job_name == "File 2"
        ref_image = (self.pl_ref_image2 if is_f2 else self.pl_ref_image).get().strip()
        needs_ref = any(item.get("section") in ("WHISK_IMAGES", "LUOT2_IMAGES") for item in items)
        if needs_ref and (not ref_image or not Path(ref_image).exists()):
            messagebox.showwarning("Thiếu ảnh tham chiếu", f"Các mục ảnh của {job_name} cần ảnh tham chiếu hợp lệ để chạy lại.")
            return

        from core.pipeline.api_client import APIClient
        from core.pipeline.engine import PipelineEngine

        client = APIClient(self.pl_host.get(), int(self.pl_port.get()), self.pl_api_key.get(), 
                          log_cb=lambda m, l="INFO": self._pl_log(m, l, job_name=job_name))
        
        retry_iids = []
        running_set = self.pl_retry_running_items2 if is_f2 else self.pl_retry_running_items
        items_dict = self.pl_failed_items2 if is_f2 else self.pl_failed_items
        tree = self.pl_failed_tree2 if is_f2 else self.pl_failed_tree
        btn = self.btn_pl_retry_failed2 if is_f2 else self.btn_pl_retry_failed

        for item in items:
            iid = item.get("id") or f"{item.get('section')}:{item.get('index')}:{item.get('name')}"
            item["id"] = iid
            retry_iids.append(iid)
            running_set.add(iid)
            if iid in items_dict:
                items_dict[iid]["status"] = "Đang chạy lại"
            if tree and tree.exists(iid):
                vals = list(tree.item(iid, "values"))
                while len(vals) < 5: vals.append("")
                vals[3] = "Đang chạy lại"; vals[4] = "Đang chạy"
                tree.item(iid, values=vals)

        callbacks = {
            'log': lambda m, l="INFO": self._pl_log(m, l, job_name=job_name),
            'progress': lambda s, c, t: None,
            'completed': lambda: self.after(0, self._pl_retry_done_ui, retry_iids, mode, job_name),
            'error': lambda err: self.after(0, self._pl_retry_error_ui, retry_iids, err, mode, job_name),
            'failed_item': lambda it: self._pl_failed_item_cb(it, job_name=job_name),
            'retry_success': lambda it: self._pl_retry_success_cb(it, job_name=job_name)
        }

        if mode == "all": self.pl_retry_is_running = True
        if btn and self.pl_retry_is_running: btn.config(state="disabled")
        self._save_all_settings()
        self._pl_log(f"Chạy lại {len(items)} mục lỗi ({job_name}).", "INFO", job_name=job_name)
        retry_engine = PipelineEngine(client, callbacks)
        
        if mode == "all": self.pl_retry_engine = retry_engine
        else:
            for iid in retry_iids:
                if is_f2: self.pl_retry_engines2[iid] = retry_engine
                else: self.pl_retry_engines[iid] = retry_engine
        
        retry_engine.run_retry_items(items, ref_image, self._pl_current_settings(job_name))

    def _pl_retry_done_ui(self, retry_iids, mode, job_name="File 1"):
        is_f2 = job_name == "File 2"
        if mode == "all":
            self.pl_retry_is_running = False
            self.pl_retry_engine = None
        
        running_set = self.pl_retry_running_items2 if is_f2 else self.pl_retry_running_items
        items_dict = self.pl_failed_items2 if is_f2 else self.pl_failed_items
        tree = self.pl_failed_tree2 if is_f2 else self.pl_failed_tree
        btn = self.btn_pl_retry_failed2 if is_f2 else self.btn_pl_retry_failed
        engines = self.pl_retry_engines2 if is_f2 else self.pl_retry_engines

        for iid in retry_iids:
            engines.pop(iid, None)
            if iid in running_set:
                running_set.discard(iid)
                if iid in items_dict: items_dict[iid]["status"] = "Vẫn lỗi"
                if tree and tree.exists(iid):
                    vals = list(tree.item(iid, "values"))
                    while len(vals) < 5: vals.append("")
                    vals[3] = "Vẫn lỗi"; vals[4] = "▶ Chạy lại"
                    tree.item(iid, values=vals)
        if btn:
            state = "normal" if items_dict and not self.pl_retry_is_running else "disabled"
            btn.config(state=state)

    def _pl_retry_error_ui(self, retry_iids, err, mode, job_name="File 1"):
        self._pl_log(f"Retry lỗi ({job_name}): {err}", "ERROR", job_name=job_name)
        self._pl_retry_done_ui(retry_iids, mode, job_name)
            
    def _run_pipeline_blocking(self, job_name: Optional[str] = None) -> bool:
        job_name = job_name or "File 1"
        is_f2 = job_name == "File 2"
        w_file = (self.pl_whisk_file2 if is_f2 else self.pl_whisk_file).get().strip()
        v_file = (self.pl_veo_file2 if is_f2 else self.pl_veo_file).get().strip()
        l_file = (self.pl_luot2_file2 if is_f2 else self.pl_luot2_file).get().strip()
        ref_image = (self.pl_ref_image2 if is_f2 else self.pl_ref_image).get().strip()
        run_step1 = (self.pl_run_step12 if is_f2 else self.pl_run_step1).get()
        run_step2 = (self.pl_run_step22 if is_f2 else self.pl_run_step2).get()
        run_step3 = (self.pl_run_step32 if is_f2 else self.pl_run_step3).get()
        needs_step1 = run_step1 or run_step2
        needs_ref = needs_step1 or run_step3

        if needs_step1 and not w_file:
            self._pl_log(f"Thiếu file 30WHISK cho pipeline {job_name}.", "ERROR", job_name=job_name)
            return False
        if run_step2 and not v_file:
            self._pl_log(f"Thiếu file 30VEO cho pipeline {job_name}.", "ERROR", job_name=job_name)
            return False
        if run_step3 and not l_file:
            self._pl_log(f"Thiếu file Luot2 cho pipeline {job_name}.", "ERROR", job_name=job_name)
            return False
        if needs_ref and (not ref_image or not Path(ref_image).exists()):
            self._pl_log(f"Thiếu ảnh tham chiếu hợp lệ cho pipeline {job_name}.", "ERROR", job_name=job_name)
            return False

        from core.pipeline.api_client import APIClient
        from core.pipeline.engine import PipelineEngine

        done = threading.Event()
        result = {"ok": False}

        def completed():
            result["ok"] = True
            done.set()

        def failed(err):
            result["ok"] = False
            self._pl_log(f"Pipeline lỗi ({job_name}): {err}", "ERROR", job_name=job_name)
            done.set()

        client = APIClient(self.pl_host.get(), int(self.pl_port.get()), self.pl_api_key.get(), 
                          log_cb=lambda m, l="INFO": self._pl_log(m, l, job_name=job_name))
        
        callbacks = {
            'log': lambda m, l="INFO": self._pl_log(m, l, job_name=job_name),
            'progress': lambda s, c, t: self._pl_progress_cb(s, c, t, job_name=job_name),
            'completed': completed,
            'error': failed,
            'failed_item': lambda it: self._pl_failed_item_cb(it, job_name=job_name),
            'retry_success': lambda it: self._pl_retry_success_cb(it, job_name=job_name)
        }

        # --- PHASE 1: MAIN RUN ---
        self.pl_is_running = True
        engine = PipelineEngine(client, callbacks)
        self.pl_engines[job_name] = engine
        engine.run_pipeline(w_file, v_file, l_file, ref_image, self._pl_current_settings(job_name))
        
        while not done.wait(0.5):
            if self._stop.is_set():
                engine.stop()
                self.after(0, lambda: self._pl_restore_ui_done(job_name=job_name))
                return False
        
        # --- PHASE 2: AUTO RETRY (IF FAILED) ---
        if not self._stop.is_set():
            items_dict = self.pl_failed_items2 if is_f2 else self.pl_failed_items
            if items_dict:
                self._pl_log(f"⚠️ Phát hiện {len(items_dict)} mục lỗi. Tự động chạy lại 1 lần...", "WARNING", job_name=job_name)
                done.clear()
                
                # Setup items for retry
                retry_items = [dict(item) for item in items_dict.values()]
                running_set = self.pl_retry_running_items2 if is_f2 else self.pl_retry_running_items
                tree = self.pl_failed_tree2 if is_f2 else self.pl_failed_tree
                
                for item in retry_items:
                    iid = item.get("id")
                    running_set.add(iid)
                    if iid in items_dict:
                        items_dict[iid]["status"] = "Đang chạy lại (Auto)"
                    if tree and tree.exists(iid):
                        vals = list(tree.item(iid, "values"))
                        if len(vals) >= 4:
                            vals[3] = "Đang chạy lại (Auto)"
                            tree.item(iid, values=vals)

                retry_engine = PipelineEngine(client, callbacks)
                self.pl_engines[f"{job_name}_retry"] = retry_engine
                retry_engine.run_retry_items(retry_items, ref_image, self._pl_current_settings(job_name))
                
                while not done.wait(0.5):
                    if self._stop.is_set():
                        retry_engine.stop()
                        break
                
                del self.pl_engines[f"{job_name}_retry"]
                
                # Final cleanup of running set
                for item in retry_items:
                    running_set.discard(item.get("id"))

        self.after(0, lambda: self._pl_restore_ui_done(job_name=job_name))
        return result["ok"]

    def _pl_start_file2(self):
        self._pl_start(job_name="File 2")

    def _pl_start(self, job_name: Optional[str] = None):
        job_name = job_name or "File 1"
        is_file2 = job_name == "File 2"
        
        # UI Reset logic
        btn_start = self.btn_pl_start2 if is_file2 else self.btn_pl_start
        btn_start.config(state="disabled")
        self.btn_pl_pause.config(state="normal", text="⏸️ TẠM DỪNG")
        self.btn_pl_stop.config(state="normal")
        
        if is_file2:
            self.prog_w2["value"] = self.prog_v2["value"] = self.prog_l2["value"] = 0
            self.pl_log_w2.delete("1.0", "end")
            self._pl_clear_failed_table(job_name="File 2")
        else:
            self.prog_w["value"] = self.prog_v["value"] = self.prog_l["value"] = 0
            self.pl_log_w.delete("1.0", "end")
            self._pl_clear_failed_table(job_name="File 1")
            
        if not hasattr(self, 'global_start_time'):
            self.global_start_time = time.time()
            
        self._save_all_settings()
        
        # Start in thread using unified blocking method
        threading.Thread(target=self._run_pipeline_blocking, args=(job_name,), daemon=True).start()
        
    def _pl_toggle_pause(self):
        if not self.pl_engines: return
        if self.pl_is_paused:
            for engine in self.pl_engines.values():
                engine.resume()
            self.pl_is_paused = False
            self.btn_pl_pause.config(text="⏸️ TẠM DỪNG")
            self._pl_log("▶ Đã tiếp tục tất cả pipeline.", "INFO")
        else:
            for engine in self.pl_engines.values():
                engine.pause()
            self.pl_is_paused = True
            self.btn_pl_pause.config(text="▶️ TIẾP TỤC")
            self._pl_log("⏸ Đã tạm dừng tất cả pipeline.", "WARNING")
            
    def _pl_stop(self):
        if not self.pl_engines: return
        for engine in list(self.pl_engines.values()):
            engine.stop()
        self.pl_engines.clear()
        self.pl_is_running = False
        self.btn_pl_start.config(state="normal")
        if hasattr(self, "btn_pl_start2"):
            self.btn_pl_start2.config(state="normal")
        self.btn_pl_pause.config(state="disabled")
        self.btn_pl_stop.config(state="disabled")
        self._pl_log("⏹ Đã dừng tất cả pipeline theo yêu cầu.", "WARNING")
        

    def _show_final_popup(self, time_str: str):
        """Phát âm thanh và hiện custom dialog hoàn tất cuối cùng."""
        try:
            import winsound
            winsound.MessageBeep(winsound.MB_ICONASTERISK)
        except Exception:
            pass

        dlg = tk.Toplevel(self)
        dlg.title("✅ Hoàn thành TOÀN BỘ!")
        dlg.configure(bg=C["bg"])
        dlg.resizable(False, False)
        dlg.grab_set()          # modal
        dlg.focus_force()

        dlg.update_idletasks()
        w, h = 420, 200
        sx = self.winfo_screenwidth()
        sy = self.winfo_screenheight()
        dlg.geometry(f"{w}x{h}+{(sx - w) // 2}+{(sy - h) // 2}")

        tk.Label(dlg, text="🎉 TẤT CẢ TÁC VỤ ĐÃ HOÀN TẤT!",
                 font=("Segoe UI Semibold", 14), fg=C["ok"], bg=C["bg"]).pack(pady=(18, 4))

        tk.Label(dlg,
                 text=f"Tiến trình từ khi bắt đầu đã kết thúc.\nTổng thời gian hoàn thành: {time_str}",
                 font=("Segoe UI", 10), fg=C["text"], bg=C["bg"],
                 wraplength=390, justify="center").pack(pady=(0, 16))

        btn_row = tk.Frame(dlg, bg=C["bg"])
        btn_row.pack()

        def _ok():
            dlg.destroy()

        def _restart():
            dlg.destroy()
            import sys, subprocess
            subprocess.Popen([sys.executable] + sys.argv)
            self.after(300, self.destroy)

        _btn(btn_row, "✔  Tốt Lắm!", _ok, bg=C["ok"], width=12).pack(side="left", padx=10)
        _btn(btn_row, "🔄  Chạy Phiên Mới", _restart, bg=C["accent2"], width=16).pack(side="left", padx=10)

        dlg.protocol("WM_DELETE_WINDOW", _ok)
        
    def _pl_on_complete(self, job_name=None):
        self.after(0, lambda: self._pl_restore_ui_done(job_name))

    def _pl_on_error(self, err, job_name=None):
        self.after(0, lambda: self._pl_restore_ui_done(job_name))
        messagebox.showerror(f"Pipeline Error ({job_name or 'Unknown'})", err)

    def _pl_restore_ui_done(self, job_name=None):
        # Cleanup engine for this job
        if job_name in self.pl_engines:
            del self.pl_engines[job_name]
            
        # Only reset global running flag if all pipelines are finished
        if not self.pl_engines:
            self.pl_is_running = False
            self.btn_pl_pause.config(state="disabled")
            self.btn_pl_stop.config(state="disabled")
            
            # Show completion popup when EVERYTHING is done
            elapsed_time = 0
            if hasattr(self, 'global_start_time'):
                elapsed_time = int(time.time() - self.global_start_time)
                mins = elapsed_time // 60
                secs = elapsed_time % 60
                time_str = f"{mins} phút {secs} giây"
            else:
                time_str = "Không rõ"
            self.after(500, lambda: self._show_final_popup(time_str))

        is_f2 = job_name == "File 2"
        btn_start = self.btn_pl_start2 if is_f2 else self.btn_pl_start
        btn_start.config(state="normal", text="▶️ CHẠY LẠI")
        
        btn_retry = self.btn_pl_retry_failed2 if is_f2 else self.btn_pl_retry_failed
        items_dict = self.pl_failed_items2 if is_f2 else self.pl_failed_items
        if btn_retry:
            state = "normal" if items_dict and not self.pl_retry_is_running else "disabled"
            btn_retry.config(state=state)




