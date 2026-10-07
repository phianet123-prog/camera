"""
CameraWorker runs one camera end-to-end in its own background thread:
capture -> night mode -> tamper check -> detection -> per-zone filtering
-> face boxes -> recording -> alerts -> latest-frame buffer for the
live stream. The Flask app only ever reads `worker.get_jpeg()` and
`worker.stats` — it never touches OpenCV directly, so any number of
browser tabs can view the same feed without spinning up extra capture
loops.
"""
import os
import time
import threading
from datetime import datetime

import cv2

import config as cfg
import detection
import recording
import alerts
import backup


class CameraWorker:
    def __init__(self, camera_config):
        self.id = camera_config["id"]
        self.name = camera_config.get("name", self.id)
        self.source = camera_config["source"]
        self.classes = camera_config.get("classes", [0])
        self.zones = camera_config.get("zones", [{"name": "Full frame", "x1": 0.0, "y1": 0.0, "x2": 1.0, "y2": 1.0}])

        self._stop_event = threading.Event()
        self._frame_lock = threading.Lock()
        self._latest_jpeg = None
        self._thread = None

        self.stats = {
            "status": "starting",     # starting | ok | night | tamper | reconnecting | offline
            "started_at": None,
            "last_detection_at": None,
            "last_alert_at": None,
            "detections_today": 0,
            "frame_width": None,
            "frame_height": None,
        }
        # Set True only when the loop exits from an unexpected failure
        # (not a deliberate stop()), so the watchdog knows to restart it.
        self.crashed = False

    # ---------- lifecycle ----------

    def start(self):
        self._stop_event.clear()
        self.crashed = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def reload_config(self, camera_config):
        """Applies new zones/classes by restarting the capture loop. The
        camera device itself is reopened, so this briefly interrupts the
        live feed — acceptable, since settings changes are infrequent."""
        self.name = camera_config.get("name", self.id)
        self.source = camera_config["source"]
        self.classes = camera_config.get("classes", [0])
        self.zones = camera_config.get("zones", self.zones)
        self.stop()
        self.start()

    def get_jpeg(self):
        with self._frame_lock:
            return self._latest_jpeg

    def is_healthy(self):
        """True if the worker's thread is doing its job — either running
        normally or in the middle of a deliberate reconnect attempt.
        False means it exited unexpectedly and the watchdog should
        restart it."""
        return self._thread is not None and self._thread.is_alive()

    # ---------- main loop ----------

    def _run(self):
        try:
            self._run_with_reconnect()
        except Exception as e:
            # Anything escaping here is a genuine bug, not a normal
            # camera hiccup (those are handled inside the reconnect
            # loop already). Flag it so the watchdog restarts us.
            print(f"[{self.id}] Worker thread crashed unexpectedly: {e}")
            self.stats["status"] = "offline"
            self.crashed = True

    def _run_with_reconnect(self):
        reconnect_delay = cfg.CAMERA_RECONNECT_DELAY_SECONDS

        while not self._stop_event.is_set():
            camera = cv2.VideoCapture(self.source)
            if not camera.isOpened():
                self.stats["status"] = "reconnecting"
                print(f"[{self.id}] Could not open source {self.source!r}, retrying in {reconnect_delay:.0f}s...")
                camera.release()
                if self._stop_event.wait(reconnect_delay):
                    return
                reconnect_delay = min(reconnect_delay * 2, cfg.CAMERA_MAX_RECONNECT_DELAY_SECONDS)
                continue

            # Successfully opened — reset backoff and run until it drops.
            reconnect_delay = cfg.CAMERA_RECONNECT_DELAY_SECONDS
            dropped = self._run_capture_loop(camera)
            camera.release()

            if self._stop_event.is_set():
                return
            if dropped:
                self.stats["status"] = "reconnecting"
                print(f"[{self.id}] Lost connection, retrying in {reconnect_delay:.0f}s...")
                alerts.send_tamper_alert(self.name, "Camera stopped responding — attempting to reconnect.", _now_str())
                if self._stop_event.wait(reconnect_delay):
                    return
                reconnect_delay = min(reconnect_delay * 2, cfg.CAMERA_MAX_RECONNECT_DELAY_SECONDS)

    def _run_capture_loop(self, camera):
        """Runs the detection/recording loop against an already-open
        camera. Returns True if it exited because the camera dropped
        (caller should try to reconnect), False if it exited because
        stop() was called."""
        frame_width = int(camera.get(cv2.CAP_PROP_FRAME_WIDTH) or 640)
        frame_height = int(camera.get(cv2.CAP_PROP_FRAME_HEIGHT) or 480)
        self.stats["frame_width"] = frame_width
        self.stats["frame_height"] = frame_height
        self.stats["started_at"] = self.stats["started_at"] or _now_str()
        self.stats["status"] = "ok"

        zones_px = [
            {
                "name": z["name"],
                "rect": (int(z["x1"] * frame_width), int(z["y1"] * frame_height),
                         int(z["x2"] * frame_width), int(z["y2"] * frame_height)),
                "last_alert_time": 0,
                "streak_cls": None,
                "streak_count": 0,
            }
            for z in self.zones
        ]
        any_zone_restricts = any(not (z["x1"] == 0.0 and z["y1"] == 0.0 and z["x2"] == 1.0 and z["y2"] == 1.0) for z in self.zones)

        self._tamper_scratch = (0, False, 0)
        recorder = recording.SegmentRecorder(self.id, frame_width, frame_height)
        recorder.start_new_segment()

        dropped = False
        while not self._stop_event.is_set():
            success, frame = camera.read()
            if not success:
                print(f"[{self.id}] Lost connection to webcam.")
                dropped = True
                break

            try:
                self._process_frame(frame, frame_width, frame_height, zones_px, any_zone_restricts, recorder)
            except Exception as e:
                print(f"[{self.id}] Error during frame processing: {e}")
                dropped = True
                break

        recorder.close()
        return dropped

    def _process_frame(self, frame, frame_width, frame_height, zones_px, any_zone_restricts, recorder):
        tamper_frame_count, tamper_alert_active, last_tamper_alert_time = self._tamper_scratch
        current_time = time.time()
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

        # --- night mode ---
        brightness = gray.mean()
        is_night_mode = brightness < cfg.NIGHT_MODE_THRESHOLD
        detection_frame = detection.enhance_low_light(frame) if is_night_mode else frame

        # --- tamper detection ---
        frame_std = gray.std()
        if frame_std < cfg.TAMPER_STD_THRESHOLD:
            tamper_frame_count += 1
        else:
            if tamper_alert_active:
                print(f"[{self.id}] Camera view restored to normal.")
                tamper_alert_active = False
            tamper_frame_count = 0

        if (tamper_frame_count == cfg.TAMPER_CONSECUTIVE_FRAMES
                and current_time - last_tamper_alert_time > cfg.TAMPER_ALERT_COOLDOWN_SECONDS):
            last_tamper_alert_time = current_time
            tamper_alert_active = True
            print(f"[{self.id}] Possible tamper: view obstructed or blacked out.")
            alerts.send_tamper_alert(self.name, "Camera view is blank or obstructed (low image variance).", _now_str())

        self.stats["status"] = "tamper" if tamper_alert_active else ("night" if is_night_mode else "ok")
        self._tamper_scratch = (tamper_frame_count, tamper_alert_active, last_tamper_alert_time)

        # --- detection ---
        results = detection.run_detection(detection_frame, self.classes)
        boxes = results.boxes
        annotated_frame = results.plot() if len(boxes) > 0 else detection_frame.copy()

        current_timestamp_str = _now_str()
        _draw_overlays(annotated_frame, current_timestamp_str, is_night_mode, tamper_alert_active, zones_px, any_zone_restricts)

        # --- per-zone filtering, grouped by (zone, class) ---
        all_dets = []  # (cls_id, x1,y1,x2,y2)
        if len(boxes) > 0:
            for box in boxes:
                cls_id = int(box.cls[0].item())
                x1, y1, x2, y2 = box.xyxy[0].tolist()
                all_dets.append((cls_id, x1, y1, x2, y2))

        # face boxes only inside person detections
        person_boxes = [(d[1], d[2], d[3], d[4]) for d in all_dets if d[0] == 0]
        for (fx, fy, fw, fh) in detection.find_faces(detection_frame, person_boxes):
            cv2.rectangle(annotated_frame, (fx, fy), (fx + fw, fy + fh), (255, 222, 89), 1)

        triggered_zone = None
        triggered_label = None
        farthest_box = None

        for zone in zones_px:
            zx1, zy1, zx2, zy2 = zone["rect"]
            zone_dets = []
            for (cls_id, x1, y1, x2, y2) in all_dets:
                cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
                if zx1 <= cx <= zx2 and zy1 <= cy <= zy2:
                    zone_dets.append((cls_id, x1, y1, x2, y2))

            if not zone_dets:
                zone["streak_cls"] = None
                zone["streak_count"] = 0
                continue

            candidates = [d for d in zone_dets if (d[4] - d[2]) < frame_height * cfg.FAR_DETECTION_HEIGHT_RATIO]
            if candidates:
                far = min(candidates, key=lambda d: (d[3] - d[1]) * (d[4] - d[2]))
                farthest_box = (far[1], far[2], far[3], far[4])

            # Persistence check: the same class has to keep showing up in
            # this zone for DETECTION_PERSISTENCE_FRAMES frames in a row
            # before it counts as a real detection — filters out a single
            # flickery misclassification rather than alerting on it.
            this_cls = zone_dets[0][0]
            if zone["streak_cls"] == this_cls:
                zone["streak_count"] += 1
            else:
                zone["streak_cls"] = this_cls
                zone["streak_count"] = 1

            if zone["streak_count"] < cfg.DETECTION_PERSISTENCE_FRAMES:
                continue

            if current_time - zone["last_alert_time"] > cfg.ALERT_COOLDOWN_SECONDS:
                zone["last_alert_time"] = current_time
                triggered_zone = zone["name"]
                triggered_label = cfg.CLASS_NAMES.get(this_cls, f"class {this_cls}")

        # --- zoom inset (live PiP) ---
        frame_for_zoom_crop = None
        if farthest_box is not None:
            frame_for_zoom_crop = annotated_frame.copy()
            inset = detection.make_zoomed_crop(annotated_frame, farthest_box, frame_width, frame_height, target_size=cfg.ZOOM_INSET_SIZE)
            if inset is not None and frame_width > cfg.ZOOM_INSET_SIZE + 20 and frame_height > cfg.ZOOM_INSET_SIZE + 20:
                ix, iy = frame_width - cfg.ZOOM_INSET_SIZE - 10, 10
                annotated_frame[iy:iy + cfg.ZOOM_INSET_SIZE, ix:ix + cfg.ZOOM_INSET_SIZE] = inset
                cv2.rectangle(annotated_frame, (ix, iy), (ix + cfg.ZOOM_INSET_SIZE, iy + cfg.ZOOM_INSET_SIZE), (0, 200, 255), 2)
                cv2.putText(annotated_frame, "ZOOM", (ix, iy - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 200, 255), 2, cv2.LINE_AA)

        # --- continuous recording, never gated by detection ---
        recorder.write(annotated_frame)
        recorder.maybe_rotate()

        # --- snapshot + alerts, only when a zone actually triggered ---
        if triggered_zone is not None:
            self.stats["last_detection_at"] = current_timestamp_str
            self.stats["last_alert_at"] = current_timestamp_str
            self.stats["detections_today"] = self.stats.get("detections_today", 0) + 1

            snap_timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            snapshot_filename = os.path.join(cfg.CAPTURES_DIR, f"{self.id}_snapshot_{snap_timestamp}.jpg")
            cv2.imwrite(snapshot_filename, annotated_frame)
            backup.upload_clip_async(snapshot_filename)

            zoom_filename = None
            if farthest_box is not None and frame_for_zoom_crop is not None:
                zoom_crop = detection.make_zoomed_crop(frame_for_zoom_crop, farthest_box, frame_width, frame_height)
                if zoom_crop is not None:
                    zoom_filename = os.path.join(cfg.CAPTURES_DIR, f"{self.id}_snapshot_zoom_{snap_timestamp}.jpg")
                    cv2.imwrite(zoom_filename, zoom_crop)

            threading.Thread(
                target=alerts.send_email_alert,
                args=(self.name, triggered_label, triggered_zone, snapshot_filename, current_timestamp_str, zoom_filename),
                daemon=True,
            ).start()
            threading.Thread(
                target=alerts.send_whatsapp_alert,
                args=(self.name, triggered_label, triggered_zone, current_timestamp_str),
                daemon=True,
            ).start()

            print(f"[{self.id}] {triggered_label} detected in zone '{triggered_zone}'. Alerts dispatched.")

        # --- publish latest frame for the MJPEG stream ---
        ret, buffer = cv2.imencode(".jpg", annotated_frame, [cv2.IMWRITE_JPEG_QUALITY, cfg.JPEG_QUALITY])
        if ret:
            with self._frame_lock:
                self._latest_jpeg = buffer.tobytes()


def _draw_overlays(frame, timestamp_str, is_night_mode, tamper_active, zones_px, any_zone_restricts):
    cv2.putText(frame, timestamp_str, (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2, cv2.LINE_AA)
    y = 70
    if is_night_mode:
        cv2.putText(frame, "NIGHT MODE", (20, y), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 0, 255), 2, cv2.LINE_AA)
        y += 30
    if tamper_active:
        cv2.putText(frame, "TAMPER ALERT", (20, y), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2, cv2.LINE_AA)
    if any_zone_restricts:
        for zone in zones_px:
            zx1, zy1, zx2, zy2 = zone["rect"]
            cv2.rectangle(frame, (zx1, zy1), (zx2, zy2), (0, 200, 255), 2)
            cv2.putText(frame, zone["name"], (zx1 + 4, max(18, zy1 + 18)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 200, 255), 1, cv2.LINE_AA)


def _now_str():
    return datetime.now().strftime("%Y-%m-%d  %H:%M:%S")
