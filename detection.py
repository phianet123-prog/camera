"""
Detection helpers shared by every camera worker.

The YOLO model is loaded once and shared across all camera threads —
loading one copy per camera would multiply memory and startup time for
no benefit. A single lock serializes inference calls: ultralytics'
model objects aren't guaranteed thread-safe for concurrent .predict()
calls, so cameras take turns rather than racing. With a handful of
cameras on typical hardware this is not the bottleneck; if you scale to
many cameras and inference becomes the bottleneck, the fix is one model
instance per thread (more memory) rather than removing the lock.
"""
import threading
import cv2
import numpy as np
from ultralytics import YOLO

import config as cfg

print("[DETECTION] Loading YOLO model...")
_model = YOLO("yolov8n.pt")
_model_lock = threading.Lock()

# Face boxes are a nice-to-have overlay, not core to detection — some
# OpenCV builds/versions don't ship the Haar cascade data the same way,
# so this degrades gracefully instead of crashing the whole app.
_face_cascade = None
try:
    _cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
    _candidate = cv2.CascadeClassifier(_cascade_path)
    if not _candidate.empty():
        _face_cascade = _candidate
    else:
        print(f"[DETECTION] Haar cascade file not found at {_cascade_path} — face boxes disabled.")
except AttributeError as e:
    print(f"[DETECTION] Face detection unavailable on this OpenCV build ({e}) — face boxes disabled, everything else still works.")


def run_detection(frame, classes):
    """Runs YOLO on `frame`, restricted to `classes` (list of COCO ids).
    Returns the ultralytics Results object. Thread-safe across cameras."""
    with _model_lock:
        return _model(frame, classes=classes, verbose=False, imgsz=cfg.YOLO_IMGSZ)[0]


def find_faces(frame, person_boxes_xyxy):
    """Runs a lightweight Haar-cascade face detector, but only inside each
    person box rather than the full frame — cheaper, and avoids false
    positives from wall art, patterned fabric, etc. elsewhere in the shot.
    Returns a list of (x, y, w, h) face boxes in full-frame coordinates.
    Returns an empty list (no crash) if face detection isn't available
    on this OpenCV build."""
    if _face_cascade is None or not person_boxes_xyxy:
        return []
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    faces = []
    for (bx1, by1, bx2, by2) in person_boxes_xyxy:
        x1, y1, x2, y2 = int(max(0, bx1)), int(max(0, by1)), int(bx2), int(by2)
        if x2 <= x1 or y2 <= y1:
            continue
        roi = gray[y1:y2, x1:x2]
        if roi.size == 0:
            continue
        detected = _face_cascade.detectMultiScale(roi, scaleFactor=1.1, minNeighbors=5, minSize=(24, 24))
        for (fx, fy, fw, fh) in detected:
            faces.append((x1 + fx, y1 + fy, fw, fh))
    return faces


def enhance_low_light(frame):
    """CLAHE-based brightening on the lightness channel."""
    lab = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB)
    l_channel, a_channel, b_channel = cv2.split(lab)
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    l_enhanced = clahe.apply(l_channel)
    lab_enhanced = cv2.merge((l_enhanced, a_channel, b_channel))
    return cv2.cvtColor(lab_enhanced, cv2.COLOR_LAB2BGR)


def make_zoomed_crop(frame, box_xyxy, frame_width, frame_height, target_size=None):
    """Crops around a detection box with padding, upscales for visibility.
    Returns None if the crop ends up empty."""
    x1, y1, x2, y2 = box_xyxy
    box_w, box_h = x2 - x1, y2 - y1
    pad_x, pad_y = box_w * cfg.ZOOM_CROP_PADDING, box_h * cfg.ZOOM_CROP_PADDING

    cx1 = max(0, int(x1 - pad_x))
    cy1 = max(0, int(y1 - pad_y))
    cx2 = min(frame_width, int(x2 + pad_x))
    cy2 = min(frame_height, int(y2 + pad_y))

    if cx2 <= cx1 or cy2 <= cy1:
        return None

    crop = frame[cy1:cy2, cx1:cx2]
    if crop.size == 0:
        return None

    if target_size:
        return cv2.resize(crop, (target_size, target_size), interpolation=cv2.INTER_CUBIC)

    scale = min(4.0, max(1.5, (frame_height * cfg.FAR_DETECTION_HEIGHT_RATIO) / max(box_h, 1)))
    new_w, new_h = int(crop.shape[1] * scale), int(crop.shape[0] * scale)
    return cv2.resize(crop, (new_w, new_h), interpolation=cv2.INTER_CUBIC)
