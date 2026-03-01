"""
uploading/uploader.py — File upload handling, validation, and pipeline kickoff.

Responsibilities
----------------
- Validate file extension against ALLOWED set
- Save uploaded file to media/originals/
- Generate a thumbnail via FFmpeg (video frame or waveform for audio)
- Decide copy vs. transcode path and delegate to StreamManager
- Emit Socket.IO events throughout the pipeline

This module is intentionally free of Flask imports so it can be unit-tested
independently.  The Flask route in app.py wires it up.
"""

import os
import subprocess
import threading
import time

import logger


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ALLOWED_EXTENSIONS = {
    ".mp4", ".mkv", ".avi", ".mov", ".ts", ".m2ts",
    ".mp3", ".wav", ".flac", ".aac", ".m4a", ".ogg",
}

AUDIO_EXTENSIONS = {".mp3", ".wav", ".flac", ".aac", ".m4a", ".ogg"}


# ---------------------------------------------------------------------------
# Thumbnail generation
# ---------------------------------------------------------------------------

def generate_thumbnail(filepath: str, cid: int, thumb_dir: str):
    """
    Create a 320×180 JPEG thumbnail for `filepath` and save to thumb_dir.

    Audio files → waveform image.
    Video files → frame at 10% of duration.
    """
    thumb = os.path.join(thumb_dir, f"ch{cid}.jpg")
    ext   = os.path.splitext(filepath)[1].lower()

    try:
        if ext in AUDIO_EXTENSIONS:
            subprocess.run(
                [
                    "ffmpeg", "-y", "-i", filepath,
                    "-filter_complex", "showwavespic=s=320x180:colors=#ff6a00",
                    "-frames:v", "1", thumb,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
            )
        else:
            probe = subprocess.run(
                [
                    "ffprobe", "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    filepath,
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            dur  = float(probe.stdout.strip() or "10")
            seek = max(0, dur * 0.1)
            subprocess.run(
                [
                    "ffmpeg", "-y", "-ss", str(seek), "-i", filepath,
                    "-vframes", "1",
                    "-vf",
                    "scale=320:180:force_original_aspect_ratio=decrease,"
                    "pad=320:180:(ow-iw)/2:(oh-ih)/2:black",
                    thumb,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=45,
            )
        logger.info(f"Thumbnail ready CH{cid + 1:02d}")
    except Exception as e:
        logger.error(f"Thumbnail CH{cid + 1:02d}: {e}")


# ---------------------------------------------------------------------------
# Upload pipeline
# ---------------------------------------------------------------------------

def validate_extension(filename: str) -> bool:
    ext = os.path.splitext(filename)[1].lower()
    return ext in ALLOWED_EXTENSIONS


def process_upload(
    cid: int,
    file_storage,           # werkzeug FileStorage object
    orig_dir: str,
    trans_dir: str,
    thumb_dir: str,
    global_tc: dict,
    manager,                # StreamManager instance
    socketio,               # Flask-SocketIO instance
):
    """
    Save the uploaded file, then run thumbnail + transcode/copy pipeline.

    Called from the Flask route; heavy work is pushed to a daemon thread
    so the HTTP response returns immediately.

    Returns (True, None) on success, (False, error_msg) on validation fail.
    """
    filename = file_storage.filename
    ext      = os.path.splitext(filename)[1].lower()

    if ext not in ALLOWED_EXTENSIONS:
        return False, f"Unsupported file type: {ext}"

    codec      = global_tc.get("codec",      "h264")
    preset     = global_tc.get("preset",     "fast")
    vbitrate   = global_tc.get("vbitrate",   "6M")
    abitrate   = global_tc.get("abitrate",   "192k")
    resolution = global_tc.get("resolution", "1080p")
    fps        = global_tc.get("fps",        "original")

    src_path = os.path.join(orig_dir, f"CH{cid + 1:02d}_{filename}")

    if os.path.exists(src_path):
        logger.info(f"CH{cid + 1:02d} reusing existing file: {filename}")
    else:
        file_storage.save(src_path)
        size_mb = round(os.path.getsize(src_path) / 1_048_576, 1)
        logger.info(
            f"CH{cid + 1:02d} uploaded: {filename}",
            {"size_mb": size_mb, "codec": codec},
        )

    def _pipeline():
        generate_thumbnail(src_path, cid, thumb_dir)
        ts = time.time()

        if codec == "copy":
            ip, port = manager.add_channel(
                cid, src_path, filename,
                pre_transcoded=False, src_path=src_path, codec="copy",
            )
            m = manager.metadata[cid]
            socketio.emit("channel_ready", {
                "cid":      cid,
                "filename": filename,
                "ip":       ip,
                "port":     port,
                "encap":    m.get("encap", "udp"),
                "bitrate":  manager.global_bitrate or "",
                "loop":     m.get("loop", True),
                "codec":    "copy",
                "preset":   m.get("preset",   "fast"),
                "vbitrate": m.get("vbitrate",  "6M"),
                "abitrate": m.get("abitrate",  "192k"),
                "thumb":    f"/api/thumbnail/{cid}?t={ts}",
            })
        else:
            dst_path = os.path.join(trans_dir, f"ch{cid}.ts")
            socketio.emit("transcode_start", {
                "cid":    cid,
                "codec":  codec,
                "preset": preset,
            })

            def on_progress(cid, pct, eta_secs=0):
                socketio.emit("transcode_progress", {
                    "cid":      cid,
                    "pct":      pct,
                    "eta_secs": eta_secs,
                })

            def on_complete(cid, filepath):
                ip, port = manager.add_channel(
                    cid, filepath, filename,
                    pre_transcoded=True, src_path=src_path, codec=codec,
                )
                m = manager.metadata[cid]
                socketio.emit("channel_ready", {
                    "cid":      cid,
                    "filename": filename,
                    "ip":       ip,
                    "port":     port,
                    "encap":    m.get("encap", "udp"),
                    "bitrate":  m.get("bitrate", ""),
                    "loop":     m.get("loop", True),
                    "codec":    codec,
                    "preset":   m.get("preset",   "fast"),
                    "vbitrate": m.get("vbitrate",  "6M"),
                    "abitrate": m.get("abitrate",  "192k"),
                    "thumb":    f"/api/thumbnail/{cid}?t={time.time()}",
                })

            def on_error(cid, msg):
                socketio.emit("transcode_error", {"cid": cid, "error": msg})

            manager.start_transcode(
                cid, src_path, dst_path,
                codec, preset, vbitrate, abitrate, resolution, fps,
                on_progress, on_complete, on_error,
            )

    threading.Thread(target=_pipeline, daemon=True).start()
    return True, None
