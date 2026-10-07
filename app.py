import os
import re
import time
import threading
import subprocess
from flask import Flask, render_template, Response, request, redirect, url_for, jsonify, flash
from flask_httpauth import HTTPBasicAuth

import config as cfg
import users as user_store
import rclone_manager
from camera_worker import CameraWorker

app = Flask(__name__)
app.secret_key = os.urandom(24)  # only used for one-time flash messages, not real auth
auth = HTTPBasicAuth()


@auth.verify_password
def verify_password(username, password):
    return user_store.verify(username, password)


# ---------- camera registry ----------

workers = {}  # camera_id -> CameraWorker


def start_all_cameras():
    conf = cfg.load_config()
    for cam_conf in conf["cameras"]:
        worker = CameraWorker(cam_conf)
        workers[worker.id] = worker
        worker.start()


def restart_camera(camera_id):
    cam_conf = cfg.get_camera_config(camera_id)
    if cam_conf is None:
        return False
    if camera_id in workers:
        workers[camera_id].reload_config(cam_conf)
    else:
        worker = CameraWorker(cam_conf)
        workers[camera_id] = worker
        worker.start()
    return True


def _watchdog_loop():
    """Each camera already retries its own dropped connections
    internally (see camera_worker.py) — this only catches the rarer
    case of the worker thread itself dying from an unhandled bug, and
    brings it back up rather than leaving that camera dark until
    someone notices and restarts the whole app."""
    while True:
        time.sleep(cfg.WATCHDOG_INTERVAL_SECONDS)
        for camera_id, worker in list(workers.items()):
            if worker.crashed or not worker.is_healthy():
                print(f"[WATCHDOG] {camera_id} isn't running, restarting it.")
                restart_camera(camera_id)


def start_watchdog():
    threading.Thread(target=_watchdog_loop, daemon=True).start()


# ---------- pages ----------

@app.route("/")
@auth.login_required
def index():
    conf = cfg.load_config()
    cameras = []
    for cam_conf in conf["cameras"]:
        worker = workers.get(cam_conf["id"])
        cameras.append({
            "id": cam_conf["id"],
            "name": cam_conf.get("name", cam_conf["id"]),
            "stats": worker.stats if worker else {"status": "offline"},
        })
    return render_template("index.html", cameras=cameras)


@app.route("/camera/<camera_id>")
@auth.login_required
def camera_detail(camera_id):
    cam_conf = cfg.get_camera_config(camera_id)
    if cam_conf is None:
        return "Camera not found", 404
    worker = workers.get(camera_id)
    return render_template(
        "camera.html",
        camera=cam_conf,
        stats=worker.stats if worker else {"status": "offline"},
        class_names=cfg.CLASS_NAMES,
    )


@app.route("/gallery")
@app.route("/gallery/<camera_id>")
@auth.login_required
def gallery(camera_id=None):
    conf = cfg.load_config()
    camera_ids = [c["id"] for c in conf["cameras"]]

    media_files = []
    if os.path.exists(cfg.CAPTURES_DIR):
        all_files = sorted(os.listdir(cfg.CAPTURES_DIR), reverse=True)
        for f in all_files:
            if not (f.endswith(".mp4") or f.endswith(".jpg")) or f.startswith("temp_"):
                continue
            owner = next((cid for cid in camera_ids if f.startswith(cid + "_")), None)
            if camera_id and owner != camera_id:
                continue
            media_files.append({"file": f, "camera_id": owner or "unknown"})

    return render_template("gallery.html", media_files=media_files, cameras=conf["cameras"], selected_camera=camera_id)


@app.route("/settings", methods=["GET"])
@auth.login_required
def settings():
    conf = cfg.load_config()
    return render_template(
        "settings.html",
        cameras=conf["cameras"],
        class_names=cfg.CLASS_NAMES,
        usernames=user_store.list_usernames(),
        backup=conf.get("backup", {"enabled": False, "remote": ""}),
        rclone_installed=rclone_manager.find_rclone() is not None,
    )


@app.route("/settings/users/add", methods=["POST"])
@auth.login_required
def add_user():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    if username and password:
        user_store.add_user(username, password)
    return redirect(url_for("settings"))


@app.route("/settings/users/remove", methods=["POST"])
@auth.login_required
def remove_user():
    username = request.form.get("username", "").strip()
    if username:
        user_store.remove_user(username)
    return redirect(url_for("settings"))


# ---------- backup / OneDrive connect ----------

@app.route("/settings/backup/connect_onedrive", methods=["POST"])
@auth.login_required
def connect_onedrive():
    """One-click OneDrive setup: makes sure rclone exists (downloading it
    if needed), runs its OAuth login (opens your browser for the
    Microsoft login), saves the resulting remote, and turns backup on —
    no terminal wizard required."""
    try:
        rclone_bin = rclone_manager.ensure_rclone()
    except Exception as e:
        flash(f"Couldn't set up rclone automatically: {e}. You can still install it yourself from rclone.org.", "error")
        return redirect(url_for("settings"))

    remote_name = "onedrive"

    try:
        # `rclone authorize` opens a local browser window for the
        # Microsoft login and prints back a token once you approve it —
        # this is the one part that genuinely can't be skipped, since
        # it's you granting permission, not us.
        auth_result = subprocess.run(
            [rclone_bin, "authorize", "onedrive"],
            capture_output=True, text=True, timeout=300,
        )
        if auth_result.returncode != 0:
            flash(f"OneDrive login didn't complete: {auth_result.stderr[-400:]}", "error")
            return redirect(url_for("settings"))

        match = re.search(r"(\{.*\})", auth_result.stdout, re.DOTALL)
        if not match:
            flash("OneDrive login finished but the token couldn't be read back. Try again.", "error")
            return redirect(url_for("settings"))
        token_json = match.group(1)

        create_result = subprocess.run(
            [rclone_bin, "config", "create", remote_name, "onedrive",
             "token", token_json, "config_is_local", "false"],
            capture_output=True, text=True, timeout=60,
        )
        if create_result.returncode != 0:
            flash(f"Saving the OneDrive connection failed: {create_result.stderr[-400:]}", "error")
            return redirect(url_for("settings"))

        remote_path = f"{remote_name}:argus-clips"
        cfg.save_backup_config(enabled=True, remote=remote_path)
        flash(f"OneDrive connected — clips will back up to {remote_path}.", "success")

    except subprocess.TimeoutExpired:
        flash("OneDrive login timed out — make sure you completed the sign-in in the browser tab that opened.", "error")
    except Exception as e:
        flash(f"OneDrive connection failed: {e}", "error")

    return redirect(url_for("settings"))


@app.route("/settings/backup/save", methods=["POST"])
@auth.login_required
def save_backup_settings():
    """Manual path, for any other rclone-supported provider you've
    already configured yourself (Google Drive, S3, Dropbox, etc.)."""
    enabled = request.form.get("enabled") == "on"
    remote = request.form.get("remote", "").strip()
    cfg.save_backup_config(enabled=enabled, remote=remote)
    flash("Backup settings saved.", "success")
    return redirect(url_for("settings"))


@app.route("/settings/backup/test", methods=["POST"])
@auth.login_required
def test_backup():
    backup_cfg = cfg.get_backup_config()
    remote = backup_cfg.get("remote", "").strip()
    if not remote:
        flash("Set a backup destination first.", "error")
        return redirect(url_for("settings"))

    rclone_bin = rclone_manager.find_rclone()
    if not rclone_bin:
        flash("rclone isn't set up yet — use Connect OneDrive, or save a remote you've configured yourself.", "error")
        return redirect(url_for("settings"))

    test_file = os.path.join(cfg.BASE_DIR, "_argus_backup_test.txt")
    with open(test_file, "w") as f:
        f.write("Argus backup connection test.\n")

    try:
        result = subprocess.run(
            [rclone_bin, "copy", test_file, remote, "--no-traverse"],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode == 0:
            flash(f"Test upload to {remote} succeeded — check for _argus_backup_test.txt there.", "success")
        else:
            flash(f"Test upload failed: {result.stderr[-400:]}", "error")
    except Exception as e:
        flash(f"Test upload failed: {e}", "error")
    finally:
        if os.path.exists(test_file):
            os.remove(test_file)

    return redirect(url_for("settings"))


@app.route("/settings/<camera_id>", methods=["POST"])
@auth.login_required
def update_settings(camera_id):
    form = request.form

    name = form.get("name", camera_id).strip()
    classes = [int(c) for c in form.getlist("classes")]
    if not classes:
        classes = [0]

    zone_names = form.getlist("zone_name")
    zone_x1 = form.getlist("zone_x1")
    zone_y1 = form.getlist("zone_y1")
    zone_x2 = form.getlist("zone_x2")
    zone_y2 = form.getlist("zone_y2")

    zones = []
    for i in range(len(zone_names)):
        if not zone_names[i].strip():
            continue
        try:
            zones.append({
                "name": zone_names[i].strip(),
                "x1": max(0.0, min(1.0, float(zone_x1[i]))),
                "y1": max(0.0, min(1.0, float(zone_y1[i]))),
                "x2": max(0.0, min(1.0, float(zone_x2[i]))),
                "y2": max(0.0, min(1.0, float(zone_y2[i]))),
            })
        except (ValueError, IndexError):
            continue

    if not zones:
        zones = [{"name": "Full frame", "x1": 0.0, "y1": 0.0, "x2": 1.0, "y2": 1.0}]

    cfg.upsert_camera_config(camera_id, {"name": name, "classes": classes, "zones": zones})
    restart_camera(camera_id)
    return redirect(url_for("settings"))


@app.route("/settings/<camera_id>/add", methods=["POST"])
@auth.login_required
def add_camera(camera_id):
    """Adds a brand-new camera entry with sane defaults; edit it properly
    from the settings form afterwards."""
    conf = cfg.load_config()
    if any(c["id"] == camera_id for c in conf["cameras"]):
        return "Camera id already exists", 400
    try:
        source = int(request.form.get("source", "0"))
    except ValueError:
        source = request.form.get("source", "0")  # e.g. an RTSP URL string
    conf["cameras"].append({
        "id": camera_id,
        "name": request.form.get("name", camera_id),
        "source": source,
        "classes": [0],
        "zones": [{"name": "Full frame", "x1": 0.0, "y1": 0.0, "x2": 1.0, "y2": 1.0}],
    })
    cfg.save_config(conf)
    restart_camera(camera_id)
    return redirect(url_for("settings"))


@app.route("/settings/<camera_id>/restart", methods=["POST"])
@auth.login_required
def restart_camera_route(camera_id):
    restart_camera(camera_id)
    return redirect(request.referrer or url_for("settings"))


# ---------- streaming & api ----------

def _mjpeg_generator(camera_id):
    while True:
        worker = workers.get(camera_id)
        if worker is None:
            time.sleep(0.5)
            continue
        frame_bytes = worker.get_jpeg()
        if frame_bytes is not None:
            yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + frame_bytes + b"\r\n")
        time.sleep(1 / 20)  # cap stream at ~20fps regardless of capture rate


@app.route("/feed/<camera_id>")
@auth.login_required
def video_feed(camera_id):
    return Response(_mjpeg_generator(camera_id), mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/api/stats")
@auth.login_required
def api_stats():
    return jsonify({cid: w.stats for cid, w in workers.items()})


if __name__ == "__main__":
    start_all_cameras()
    start_watchdog()

    ssl_context = None
    scheme = "http"
    if cfg.HTTPS_ENABLED:
        import certs
        cert_path, key_path = certs.ensure_self_signed_cert()
        ssl_context = (cert_path, key_path)
        scheme = "https"

    print(f"Starting Argus on {scheme}://localhost:5000 ...")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True, ssl_context=ssl_context)
