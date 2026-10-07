# Argus — AI Security Camera

Rebuilt from the original single-file script into a small multi-camera
project with a real dashboard. Everything still runs as one Flask app —
no database, no external services beyond email/WhatsApp/ffmpeg.

## What changed from the original script

**Performance**
- YOLO now runs at a fixed `imgsz=640` instead of native camera
  resolution — the single biggest speed win on CPU.
- `results.plot()` is only called when something was actually detected;
  an empty frame skips the expensive draw call.
- Segment muxing (`recording.py`) now shells out to `ffmpeg -c:v copy`
  instead of re-encoding the whole clip with moviepy. A 5-minute segment
  used to take real CPU time to finalize; now it's a near-instant
  container mux. **moviepy is no longer a dependency at all.**
- The MJPEG stream is JPEG-quality-capped (`JPEG_QUALITY`, default 80)
  instead of full quality on every frame.

**Multi-camera**
- `config.json` lists any number of cameras, each with its own `source`
  (a webcam index like `0`, or an RTSP/HTTP URL for an IP camera).
- Each camera runs in its own `CameraWorker` thread
  (`camera_worker.py`), with its own recording, alerts, and stats — one
  camera going offline doesn't affect the others.
- The YOLO model itself is still shared (one copy in memory, inference
  calls serialized with a lock) rather than one model per camera, which
  would multiply RAM use for no benefit on typical hardware.

**Detection**
- Any COCO class can now be enabled per camera, not just "person" —
  vehicles (`car`, `motorcycle`, `bus`, `truck`, `bicycle`) are in the
  Settings page as checkboxes.
- Face boxes are drawn inside person detections using OpenCV's built-in
  Haar cascade — no new heavy dependency. This flags "a face is visible
  here" for the zoomed snapshot; it does **not** identify who the
  person is. Recognizing specific known people is a materially bigger
  feature (face encodings, enrollment flow, a "known faces" store) —
  say if you want that built out next.
- Each camera supports multiple named zones instead of one. An alert
  names which zone and which class triggered it.

**Dashboard** (all pages require the same login as before)
- `/` — grid of every camera's live feed with status (OK / night /
  tamper / offline) and today's detection count.
- `/camera/<id>` — single camera, larger feed, full stats, quick links.
- `/gallery` and `/gallery/<id>` — clips and snapshots, filterable by
  camera.
- `/settings` — add cameras, edit their detection classes and zones,
  and save (which restarts just that camera's thread to apply changes).
- `/api/stats` — JSON stats for all cameras, if you want to build
  something else on top later.

## Running it

```bash
pip install -r requirements.txt
sudo apt install ffmpeg portaudio19-dev   # or brew install ffmpeg portaudio on macOS
cp .env.example .env   # then fill in your real values
python app.py
```

Open `http://localhost:5000`, log in with `ADMIN_USERNAME` /
`ADMIN_PASSWORD` from `.env`.

## Adding a second camera

Easiest path: go to **Settings → Add a camera**, give it an id (e.g.
`back_door`), a name, and a source (a second webcam is usually index
`1`; an IP camera is typically an RTSP URL like
`rtsp://user:pass@192.168.1.20:554/stream1`). It starts immediately —
no restart of the whole app needed.

## Known limitations worth knowing about

- Inference is serialized across cameras through one lock. Fine for a
  handful of cameras on typical hardware; if you scale up and inference
  becomes the bottleneck, the fix is one YOLO instance per thread
  (trades more RAM for parallel inference).
- Face boxes are detection only, not recognition — see the note above.
- Editing zones/classes restarts that camera's capture loop, so the
  live feed on that camera briefly drops for a second or two.

## Reliability, security, and backup (added after the first version)

**Auto-reconnect.** A camera that drops (unplugged, RTSP hiccup) no
longer kills that camera's thread permanently — it retries with
increasing backoff (`CAMERA_RECONNECT_DELAY_SECONDS`, capped at
`CAMERA_MAX_RECONNECT_DELAY_SECONDS`) until it comes back. You'll see
status `reconnecting` on the dashboard while this happens, plus one
tamper-style alert when it first drops.

**Watchdog.** A separate background thread checks every
`WATCHDOG_INTERVAL_SECONDS` that each camera's worker thread is still
alive, and restarts any that died from a genuine bug (not a normal
camera hiccup — those are handled by the reconnect logic above).

**Fewer false alarms.** A zone only fires an alert once the same class
has shown up for `DETECTION_PERSISTENCE_FRAMES` consecutive frames
(default 3), instead of alerting on a single frame. Raise this if you
still get flickery alerts; lower it if real detections feel delayed.

**Multiple users.** `/settings` now has a Users section — add or
remove dashboard logins there, or from the command line:
```bash
python manage_users.py add alice
python manage_users.py remove alice
python manage_users.py list
```
Passwords are hashed in `users.json` (not in `.env`). Your original
`ADMIN_USERNAME`/`ADMIN_PASSWORD` in `.env` still works as a bootstrap
account even after you've added others.

**Optional HTTPS.** Set `HTTPS_ENABLED=true` in `.env` and install the
`cryptography` package (`pip install cryptography`, or it's already in
requirements.txt). A self-signed certificate is generated once and
reused. Your browser will show a one-time "not secure" warning since
it's not from a public certificate authority — that's expected for a
LAN device and safe to accept; if you ever expose this beyond your own
network, put a real certificate (e.g. via a reverse proxy with Let's
Encrypt) in front of it instead.

**Optional offsite backup.** Go to Settings → Offsite backup and click
**Connect OneDrive**. This handles everything: it downloads `rclone`
automatically if it's not already installed (a few seconds, one time
only), opens your browser for a Microsoft login, and saves the
connection — no terminal commands needed. For any other provider
(Google Drive, S3, Dropbox, Backblaze, etc.), configure it yourself
once with `rclone config` in a terminal, then paste the remote name
into the "use a different provider" box on the same page (e.g.
`gdrive:argus-clips`). Either way, every finished clip and alert
snapshot gets pushed there in the background as soon as it's saved;
local files are still trimmed by the existing
`MAX_CAPTURE_FILES`/`MAX_CAPTURES_MB` cleanup regardless of whether
backup is on. A "Send a test file" button on the same page confirms
the connection actually works.
