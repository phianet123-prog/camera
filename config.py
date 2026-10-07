"""
Central configuration: environment variables (.env) for secrets, and
config.json for camera topology (sources, detection classes, zones).
Keeping the two separate means secrets never end up in a file you might
accidentally commit or share, while camera/zone setup — which you'll want
to edit often through the Settings page — lives in plain, readable JSON.
"""
import os
import sys
import json
import threading
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
CAPTURES_DIR = os.path.join(BASE_DIR, "static", "captures")

# COCO class ids relevant to a security camera. Extend freely — YOLOv8n
# knows all 80 COCO classes, these are just the sensible defaults to
# expose in the UI.
CLASS_NAMES = {
    0: "person",
    1: "bicycle",
    2: "car",
    3: "motorcycle",
    5: "bus",
    7: "truck",
}


def _require_env(name):
    value = os.environ.get(name)
    if not value:
        sys.exit(
            f"Missing required environment variable: {name}\n"
            f"Copy .env.example to .env and fill in your values."
        )
    return value


ADMIN_USERNAME = _require_env("ADMIN_USERNAME")
ADMIN_PASSWORD = _require_env("ADMIN_PASSWORD")

SENDER_EMAIL = _require_env("SENDER_EMAIL")
SENDER_PASSWORD = _require_env("SENDER_PASSWORD")
RECEIVER_EMAIL = _require_env("RECEIVER_EMAIL")

TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_FROM = os.environ.get("TWILIO_WHATSAPP_FROM")
TWILIO_WHATSAPP_TO = os.environ.get("TWILIO_WHATSAPP_TO")
WHATSAPP_ENABLED = all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_FROM, TWILIO_WHATSAPP_TO])
if not WHATSAPP_ENABLED:
    print("[WHATSAPP] Twilio credentials not fully set in .env — WhatsApp alerts are disabled.")

MAX_CAPTURE_FILES = int(os.environ.get("MAX_CAPTURE_FILES", "200"))
MAX_CAPTURES_MB = int(os.environ.get("MAX_CAPTURES_MB", "1024"))
NIGHT_MODE_THRESHOLD = int(os.environ.get("NIGHT_MODE_THRESHOLD", "70"))
TAMPER_STD_THRESHOLD = float(os.environ.get("TAMPER_STD_THRESHOLD", "8"))
TAMPER_CONSECUTIVE_FRAMES = int(os.environ.get("TAMPER_CONSECUTIVE_FRAMES", "30"))
TAMPER_ALERT_COOLDOWN_SECONDS = int(os.environ.get("TAMPER_ALERT_COOLDOWN_SECONDS", "300"))
FAR_DETECTION_HEIGHT_RATIO = float(os.environ.get("FAR_DETECTION_HEIGHT_RATIO", "0.25"))
ZOOM_CROP_PADDING = float(os.environ.get("ZOOM_CROP_PADDING", "0.6"))
ZOOM_INSET_SIZE = int(os.environ.get("ZOOM_INSET_SIZE", "220"))
VIDEO_SEGMENT_SECONDS = int(os.environ.get("VIDEO_SEGMENT_SECONDS", "300"))
ALERT_COOLDOWN_SECONDS = int(os.environ.get("ALERT_COOLDOWN_SECONDS", "25"))
CAMERA_FALLBACK_FPS = float(os.environ.get("CAMERA_FALLBACK_FPS", "15"))
YOLO_IMGSZ = int(os.environ.get("YOLO_IMGSZ", "640"))
JPEG_QUALITY = int(os.environ.get("JPEG_QUALITY", "80"))
AUDIO_SAMPLE_RATE = 44100

# --- Reliability: reconnect + persistence-based alerting ---
CAMERA_RECONNECT_DELAY_SECONDS = float(os.environ.get("CAMERA_RECONNECT_DELAY_SECONDS", "5"))
CAMERA_MAX_RECONNECT_DELAY_SECONDS = float(os.environ.get("CAMERA_MAX_RECONNECT_DELAY_SECONDS", "60"))
WATCHDOG_INTERVAL_SECONDS = float(os.environ.get("WATCHDOG_INTERVAL_SECONDS", "15"))
# A zone must see the same kind of detection for this many consecutive
# frames before an alert fires — cuts down on single-frame flicker
# (a shadow, a moving leaf briefly classified as something) triggering
# a false alarm.
DETECTION_PERSISTENCE_FRAMES = int(os.environ.get("DETECTION_PERSISTENCE_FRAMES", "3"))

# --- Security ---
HTTPS_ENABLED = os.environ.get("HTTPS_ENABLED", "false").lower() == "true"

_config_lock = threading.Lock()

DEFAULT_CONFIG = {
    "cameras": [
        {
            "id": "cam1",
            "name": "Camera 1",
            "source": 0,
            "classes": [0],
            "zones": [
                {"name": "Full frame", "x1": 0.0, "y1": 0.0, "x2": 1.0, "y2": 1.0}
            ],
        }
    ],
    # Offsite backup settings live here (not .env) so the "Connect
    # OneDrive" button can turn this on for you without needing an app
    # restart.
    "backup": {
        "enabled": False,
        "remote": "",   # e.g. "onedrive:argus-clips"
    },
}


def load_config():
    with _config_lock:
        if not os.path.exists(CONFIG_PATH):
            save_config(DEFAULT_CONFIG)
            return json.loads(json.dumps(DEFAULT_CONFIG))
        with open(CONFIG_PATH, "r") as f:
            conf = json.load(f)
    # Fill in any keys added since this config.json was first written
    # (e.g. upgrading from a version before "backup" existed), without
    # clobbering what's already there.
    changed = False
    if "backup" not in conf:
        conf["backup"] = json.loads(json.dumps(DEFAULT_CONFIG["backup"]))
        changed = True
    if changed:
        save_config(conf)
    return conf


def save_config(cfg):
    with _config_lock:
        with open(CONFIG_PATH, "w") as f:
            json.dump(cfg, f, indent=2)


def get_camera_config(camera_id):
    cfg = load_config()
    for cam in cfg["cameras"]:
        if cam["id"] == camera_id:
            return cam
    return None


def upsert_camera_config(camera_id, updates):
    """Merge `updates` into the named camera's config and persist it."""
    cfg = load_config()
    for cam in cfg["cameras"]:
        if cam["id"] == camera_id:
            cam.update(updates)
            save_config(cfg)
            return cam
    raise KeyError(f"No camera with id '{camera_id}'")


def get_backup_config():
    return load_config().get("backup", {"enabled": False, "remote": ""})


def save_backup_config(enabled, remote):
    cfg = load_config()
    cfg["backup"] = {"enabled": bool(enabled), "remote": remote or ""}
    save_config(cfg)


os.makedirs(CAPTURES_DIR, exist_ok=True)
os.makedirs(os.path.join(BASE_DIR, "templates"), exist_ok=True)
