"""
core/pipeline/config.py
Configuration defaults for DHTNVIPPRO Pipeline Automation (Step 3).
"""

# ─── App Info ───────────────────────────────────────────
APP_NAME = "DHTNVIPPRO Pipeline"
APP_VERSION = "1.0.0"

# ─── Server Defaults ───────────────────────────────────
DEFAULT_SERVER_HOST = "127.0.0.1"
DEFAULT_SERVER_PORT = 8777
DEFAULT_API_KEY = ""

# ─── Available Models ──────────────────────────────────
IMAGE_MODELS = [
    "imagen4",
    "nano_banana",
    "nano_banana_pro",
    "nano_banana_2",
]

VIDEO_MODELS = [
    "veo_31_fast_relaxed",
    "veo_31_lite_relaxed",
    "veo_31_fast",
    "veo_31_lite",
    "veo_31_quality",
]

ASPECT_RATIOS = ["16:9", "9:16", "1:1", "3:4", "4:3"]
RESOLUTIONS = ["720p", "1080p"]
VIDEO_MODES = ["start_image", "text_to_video", "start_end_image", "components"]
CATEGORIES = ["subject", "scene", "style"]

# ─── Default Selections ───────────────────────────────
DEFAULT_IMAGE_MODEL = "imagen4"
DEFAULT_VIDEO_MODEL = "veo_31_fast_relaxed"
DEFAULT_ASPECT_RATIO = "16:9"
DEFAULT_RESOLUTION = "1080p"
DEFAULT_VIDEO_MODE = "start_image"
DEFAULT_CATEGORY = "scene"

# ─── Pipeline Settings ────────────────────────────────
POLL_INTERVAL_SECONDS = 5
MAX_RETRIES = 3
TASK_TIMEOUT_SECONDS = 600   # 10 minutes per task
MAX_CONCURRENT_TASKS = 1     # Sequential by default
