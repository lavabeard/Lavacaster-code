"""
transcoding/transcoder.py — FFmpeg pre-transcode pipeline.

Converts uploaded source files to MPEG-TS (H.264 or H.265) before streaming.
Reports 0–100% progress and ETA via callbacks; runs in a daemon thread.

Public API
----------
TranscodeJob(cid, src, dst, codec, preset, vbitrate, abitrate, resolution, fps)
    .start(on_progress, on_complete, on_error)
    .cancel()

probe_duration(filepath) -> float   # seconds via ffprobe
"""

import os
import subprocess
import threading
import time

import logger


# ---------------------------------------------------------------------------
# Validation constants (also imported by streaming and app layers)
# ---------------------------------------------------------------------------
VALID_CODECS      = {"copy", "h264", "h265"}
VALID_PRESETS     = {"ultrafast", "superfast", "fast", "medium", "slow"}
VALID_RESOLUTIONS = {"original", "720p", "1080p", "1440p", "4k"}
VALID_FPS         = {
    "original", "23.976", "24", "25", "29.97", "30", "50", "59.94", "60"
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def probe_duration(filepath: str) -> float:
    """Return media duration in seconds (float) via ffprobe, or 0.0 on error."""
    try:
        r = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                filepath,
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return float(r.stdout.strip())
    except Exception:
        return 0.0


# ---------------------------------------------------------------------------
# TranscodeJob
# ---------------------------------------------------------------------------

class TranscodeJob:
    """
    Pre-transcodes a source media file to MPEG-TS.

    Parameters
    ----------
    cid        : int     channel index (0-based)
    src        : str     path to the original uploaded file
    dst        : str     destination path for the .ts output
    codec      : str     "h264" or "h265"
    preset     : str     ffmpeg preset (ultrafast … slow)
    vbitrate   : str     video bitrate string, e.g. "6M"
    abitrate   : str     audio bitrate string, e.g. "192k"
    resolution : str     "original" | "720p" | "1080p" | "1440p" | "4k"
    fps        : str     "original" | "23.976" | "24" | … | "60"
    """

    _SCALE_MAP = {
        "720p":  "1280:720",
        "1080p": "1920:1080",
        "1440p": "2560:1440",
        "4k":    "3840:2160",
    }
    _FPS_MAP = {
        "23.976": "24000/1001",
        "29.97":  "30000/1001",
        "59.94":  "60000/1001",
    }

    def __init__(
        self,
        cid: int,
        src: str,
        dst: str,
        codec: str = "h264",
        preset: str = "fast",
        vbitrate: str = "6M",
        abitrate: str = "192k",
        resolution: str = "original",
        fps: str = "original",
    ):
        self.cid        = cid
        self.src        = src
        self.dst        = dst
        self.codec      = codec
        self.preset     = preset
        self.vbitrate   = vbitrate
        self.abitrate   = abitrate
        self.resolution = resolution
        self.fps        = fps
        self.process    = None
        self.active     = False

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def start(self, on_progress=None, on_complete=None, on_error=None):
        """Launch the transcode in a daemon thread."""
        self.active = True
        threading.Thread(
            target=self._run,
            args=(on_progress, on_complete, on_error),
            daemon=True,
        ).start()

    def cancel(self):
        """Terminate the running FFmpeg process."""
        self.active = False
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=3)
            except Exception:
                self.process.kill()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _bufsize(self) -> str:
        b = str(self.vbitrate).upper()
        try:
            if b.endswith("M"):
                return f"{int(float(b[:-1]) * 2000)}k"
            if b.endswith("K"):
                return f"{int(b[:-1]) * 2}k"
        except Exception:
            pass
        return "8000k"

    def _vf_args(self) -> list:
        s = self._SCALE_MAP.get(self.resolution)
        if not s:
            return []
        return [
            "-vf",
            f"scale={s}:force_original_aspect_ratio=decrease,"
            f"pad={s}:(ow-iw)/2:(oh-ih)/2",
        ]

    def _fps_args(self) -> list:
        r = self._FPS_MAP.get(self.fps, self.fps)
        if not r or r == "original":
            return []
        return ["-r", r]

    def _build_cmd(self) -> list:
        vcodec = "libx264" if self.codec == "h264" else "libx265"
        cmd = [
            "ffmpeg", "-y", "-i", self.src,
            "-c:v",     vcodec,
            "-preset",  self.preset,
            "-b:v",     self.vbitrate,
            "-maxrate", self.vbitrate,
            "-bufsize",  self._bufsize(),
        ]
        cmd += self._vf_args()
        cmd += self._fps_args()
        cmd += [
            "-c:a",     "aac",
            "-b:a",     self.abitrate,
            "-f",       "mpegts",
            "-progress", "pipe:1",
            "-nostats",
            self.dst,
        ]
        return cmd

    # ------------------------------------------------------------------
    # Main thread
    # ------------------------------------------------------------------

    def _run(self, on_progress, on_complete, on_error):
        try:
            duration = probe_duration(self.src)
            start_ts = time.time()

            self.process = subprocess.Popen(
                self._build_cmd(),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )

            for line in self.process.stdout:
                if not self.active:
                    break
                line = line.strip()
                if line.startswith("out_time_us="):
                    try:
                        us = int(line.split("=", 1)[1])
                        if duration > 0 and us > 0:
                            pct     = min(99, int(us / (duration * 1_000_000) * 100))
                            elapsed = time.time() - start_ts
                            eta     = int((elapsed / pct) * (100 - pct)) if pct > 0 else 0
                            if on_progress:
                                on_progress(self.cid, pct, eta)
                    except (ValueError, ZeroDivisionError):
                        pass

            self.process.wait()
            rc = self.process.returncode

            if rc == 0 and self.active:
                if on_progress:
                    on_progress(self.cid, 100, 0)
                logger.info(
                    f"CH{self.cid + 1:02d} transcode complete",
                    {"codec": self.codec, "dst": os.path.basename(self.dst)},
                )
                if on_complete:
                    on_complete(self.cid, self.dst)
            elif self.active:
                msg = f"FFmpeg exited with code {rc}"
                logger.error(f"CH{self.cid + 1:02d} transcode failed: {msg}")
                if on_error:
                    on_error(self.cid, msg)

        except Exception as e:
            logger.error(f"CH{self.cid + 1:02d} transcode exception: {e}")
            if on_error:
                on_error(self.cid, str(e))
        finally:
            self.active = False
