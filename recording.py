"""
Continuous recording, rotated into fixed-length segments, with mic audio
muxed in afterwards.

The original script used moviepy to merge video + audio, which fully
re-encodes the video stream — expensive CPU work competing with the live
detection loop every time a segment rotates. Here we shell out to ffmpeg
with `-c:v copy` (stream copy, no re-encoding of the already-correct
video) and only encode the audio track. This finishes in a fraction of
a second instead of tens of seconds, and moviepy is no longer a
dependency at all.
"""
import os
import glob
import time
import wave
import threading
import subprocess
from datetime import datetime

import sounddevice as sd

import config as cfg
import backup


def cleanup_captures():
    """Trims static/captures so it never grows unbounded, across all
    cameras (filenames are prefixed with the camera id)."""
    try:
        pattern = os.path.join(cfg.CAPTURES_DIR, "*")
        files = [
            f for f in glob.glob(pattern)
            if os.path.isfile(f)
            and not os.path.basename(f).startswith("temp_")
            and "_temp" not in os.path.basename(f)
        ]
        files.sort(key=os.path.getmtime)

        def total_mb():
            return sum(os.path.getsize(f) for f in files) / (1024 * 1024)

        while files and (len(files) > cfg.MAX_CAPTURE_FILES or total_mb() > cfg.MAX_CAPTURES_MB):
            oldest = files.pop(0)
            try:
                os.remove(oldest)
                print(f"[CLEANUP] Removed old capture: {oldest}")
            except OSError as e:
                print(f"[CLEANUP] Could not remove {oldest}: {e}")
    except Exception as e:
        print(f"[CLEANUP] Error during captures cleanup: {e}")


def record_audio_thread(duration, filename):
    """Records `duration` seconds of mic audio to a temp WAV file."""
    try:
        audio_data = sd.rec(int(duration * cfg.AUDIO_SAMPLE_RATE), samplerate=cfg.AUDIO_SAMPLE_RATE, channels=1, dtype="int16")
        sd.wait()
        temp_wav = filename.replace(".mp4", "_temp.wav")
        with wave.open(temp_wav, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(cfg.AUDIO_SAMPLE_RATE)
            wf.writeframes(audio_data.tobytes())
        return temp_wav
    except Exception as e:
        print(f"[AUDIO] Error recording audio: {e}")
        return None


def finalize_clip(temp_video, temp_wav, final_output):
    """Muxes silent video + mic audio into the final MP4 via ffmpeg
    stream-copy — no video re-encoding, so this is near-instant even for
    a five-minute segment."""
    try:
        if not os.path.exists(temp_wav):
            # Audio thread hasn't flushed yet; give it one more moment.
            time.sleep(1)
        if not os.path.exists(temp_wav):
            print(f"[MERGE] No audio file found for {final_output}, saving video-only.")
            os.replace(temp_video, final_output)
            cleanup_captures()
            backup.upload_clip_async(final_output)
            return

        result = subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", temp_video,
                "-i", temp_wav,
                "-c:v", "copy",
                "-c:a", "aac",
                "-shortest",
                final_output,
            ],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"[MERGE] ffmpeg error, saving video-only: {result.stderr[-500:]}")
            os.replace(temp_video, final_output)
        else:
            os.remove(temp_video)

        if os.path.exists(temp_wav):
            os.remove(temp_wav)

        print(f"[SUCCESS] Final clip saved: {final_output}")
        cleanup_captures()
        backup.upload_clip_async(final_output)
    except Exception as e:
        print(f"[MERGE] Error merging audio/video: {e}")


class SegmentRecorder:
    """Owns the continuous-recording state for one camera: the current
    video writer, when the current segment started, and rotating into
    a new segment once it hits VIDEO_SEGMENT_SECONDS."""

    def __init__(self, camera_id, frame_width, frame_height):
        self.camera_id = camera_id
        self.frame_width = frame_width
        self.frame_height = frame_height
        self.writer = None
        self.segment_start_time = 0
        self.segment_frame_count = 0
        self.current_fps = cfg.CAMERA_FALLBACK_FPS
        self.temp_video_path = ""
        self.final_video_path = ""

    def start_new_segment(self):
        import cv2
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self.final_video_path = os.path.join(cfg.CAPTURES_DIR, f"{self.camera_id}_clip_{timestamp}.mp4")
        self.temp_video_path = os.path.join(cfg.CAPTURES_DIR, f"temp_{self.camera_id}_{timestamp}.mp4")
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        self.writer = cv2.VideoWriter(self.temp_video_path, fourcc, self.current_fps, (self.frame_width, self.frame_height))
        self.segment_start_time = time.time()
        self.segment_frame_count = 0
        threading.Thread(
            target=lambda: record_audio_thread(cfg.VIDEO_SEGMENT_SECONDS, self.final_video_path),
            daemon=True,
        ).start()
        print(f"[RECORDING] {self.camera_id}: new segment at ~{self.current_fps:.1f}fps -> {self.final_video_path}")

    def write(self, frame):
        if self.writer is not None:
            self.writer.write(frame)
            self.segment_frame_count += 1

    def maybe_rotate(self):
        """Call once per frame. Rotates the segment if it's run long
        enough, re-measuring real fps so playback duration matches real
        elapsed time even if processing speed drifted mid-segment."""
        elapsed = time.time() - self.segment_start_time
        if elapsed < cfg.VIDEO_SEGMENT_SECONDS:
            return
        finished_temp, finished_final = self.temp_video_path, self.final_video_path
        if self.writer is not None:
            self.writer.release()
            self.writer = None
        if elapsed > 0 and self.segment_frame_count > 0:
            measured_fps = self.segment_frame_count / elapsed
            self.current_fps = max(1.0, min(30.0, measured_fps))

        def delayed_merge():
            time.sleep(1)
            temp_wav = finished_final.replace(".mp4", "_temp.wav")
            finalize_clip(finished_temp, temp_wav, finished_final)

        threading.Thread(target=delayed_merge, daemon=True).start()
        self.start_new_segment()

    def close(self):
        if self.writer is not None:
            self.writer.release()
            self.writer = None
            temp_wav = self.final_video_path.replace(".mp4", "_temp.wav")
            finalize_clip(self.temp_video_path, temp_wav, self.final_video_path)
