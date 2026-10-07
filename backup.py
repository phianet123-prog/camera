"""
Offsite backup of finished clips/snapshots, via rclone.

Why rclone instead of a cloud SDK: it already supports OneDrive, S3,
Google Drive, Dropbox, Backblaze B2, and dozens more, so this file
doesn't need provider-specific upload code — it just shells out to
whichever remote is configured. rclone itself is located or downloaded
automatically by rclone_manager.py, so there's no separate install step.

Settings (enabled / which remote) live in config.json, editable from
the Settings page, and are re-read on every upload rather than cached
at startup — so turning backup on/off or changing the remote takes
effect immediately, no restart needed.
"""
import os
import subprocess
import threading

import config as cfg
import rclone_manager


def upload_clip_async(local_path):
    """Fire-and-forget upload of one finished file to the configured
    rclone remote. Safe to call unconditionally — it's a no-op unless
    backup is turned on and a remote is set."""
    backup_cfg = cfg.get_backup_config()
    if not backup_cfg.get("enabled"):
        return
    remote = backup_cfg.get("remote", "").strip()
    if not remote:
        print("[BACKUP] Backup is enabled but no remote is configured — skipping.")
        return

    def _run():
        try:
            rclone_bin = rclone_manager.find_rclone()
            if rclone_bin is None:
                print("[BACKUP] rclone isn't installed yet — connect a backup destination from Settings first.")
                return
            result = subprocess.run(
                [rclone_bin, "copy", local_path, remote, "--no-traverse"],
                capture_output=True, text=True, timeout=300,
            )
            if result.returncode != 0:
                print(f"[BACKUP] rclone failed for {os.path.basename(local_path)}: {result.stderr[-400:]}")
            else:
                print(f"[BACKUP] Uploaded {os.path.basename(local_path)} to {remote}")
        except Exception as e:
            print(f"[BACKUP] Error uploading {local_path}: {e}")

    threading.Thread(target=_run, daemon=True).start()
