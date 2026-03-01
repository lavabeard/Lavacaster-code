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

import json
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


# Resolution and FPS lookup tables used by specs_match()
_RES_MAP = {
    "720p":  (1280,  720),
    "1080p": (1920, 1080),
    "1440p": (2560, 1440),
    "4k":    (3840, 2160),
}
_FPS_FLOAT = {
    "23.976": 24000 / 1001,
    "29.97":  30000 / 1001,
    "59.94":  60000 / 1001,
}


def _parse_bitrate(s: str) -> int:
    """Convert '6M' / '192k' / '4000000' → bits-per-second int, or 0 on error."""
    s = str(s).strip().upper()
    try:
        if s.endswith("M"):
            return int(float(s[:-1]) * 1_000_000)
        if s.endswith("K"):
            return int(float(s[:-1]) * 1_000)
        return int(s)
    except Exception:
        return 0


def probe_video_info(filepath: str) -> dict:
    """
    Return a dict describing the first video + audio stream in *filepath*.

    Keys: vcodec, width, height, fps (float), vbitrate_bps (int),
          acodec, abitrate_bps (int).

    Returns {} on any error; caller treats that as "specs do not match".
    """
    try:
        r = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-print_format", "json",
                "-show_streams", "-show_format",
                filepath,
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
        data = json.loads(r.stdout)
    except Exception:
        return {}

    info = {
        "vcodec":       None,
        "width":        0,
        "height":       0,
        "fps":          0.0,
        "vbitrate_bps": 0,
        "acodec":       None,
        "abitrate_bps": 0,
    }

    for s in data.get("streams", []):
        ctype = s.get("codec_type")
        if ctype == "video" and info["vcodec"] is None:
            info["vcodec"] = s.get("codec_name", "")
            info["width"]  = int(s.get("width",  0))
            info["height"] = int(s.get("height", 0))
            rfr = s.get("r_frame_rate", "0/1")
            try:
                n, d = rfr.split("/")
                info["fps"] = float(n) / float(d) if float(d) else 0.0
            except Exception:
                info["fps"] = 0.0
            try:
                info["vbitrate_bps"] = int(s.get("bit_rate") or 0)
            except Exception:
                pass
        elif ctype == "audio" and info["acodec"] is None:
            info["acodec"] = s.get("codec_name", "")
            try:
                info["abitrate_bps"] = int(s.get("bit_rate") or 0)
            except Exception:
                pass

    # Fallback: use container-level bitrate if stream-level is unavailable
    if not info["vbitrate_bps"]:
        try:
            info["vbitrate_bps"] = int(data.get("format", {}).get("bit_rate") or 0)
        except Exception:
            pass

    return info


def specs_match(
    info: dict,
    codec: str,
    resolution: str,
    fps: str,
    vbitrate: str,
    abitrate: str,
) -> bool:
    """
    Return True if *info* (from probe_video_info) satisfies the target
    transcode settings — meaning a stream-copy remux is sufficient.

    Matching rules
    --------------
    - vcodec    : h264→h264, h265→hevc
    - acodec    : must already be aac (our encode target)
    - resolution: skipped when "original"; otherwise width×height must match
    - fps       : skipped when "original"; otherwise within ±0.1 fps
    - vbitrate  : source ≤ target × 1.2  (20 % headroom)
    - abitrate  : source ≤ target × 1.2
    """
    if not info or not info.get("vcodec"):
        return False

    # Video codec
    expected = "h264" if codec == "h264" else "hevc"
    if info["vcodec"].lower() != expected:
        return False

    # Audio codec must already be AAC (what we encode to)
    if (info.get("acodec") or "").lower() != "aac":
        return False

    # Resolution
    if resolution != "original":
        target_wh = _RES_MAP.get(resolution)
        if target_wh and (info["width"], info["height"]) != target_wh:
            return False

    # Frame rate
    if fps != "original":
        target_fps = float(_FPS_FLOAT.get(fps, fps))
        src_fps    = info.get("fps", 0.0)
        if target_fps and src_fps and abs(src_fps - target_fps) > 0.1:
            return False

    # Video bitrate (allow source up to 20 % over target)
    t_vbps = _parse_bitrate(vbitrate)
    if t_vbps and info.get("vbitrate_bps"):
        if info["vbitrate_bps"] > t_vbps * 1.2:
            return False

    # Audio bitrate
    t_abps = _parse_bitrate(abitrate)
    if t_abps and info.get("abitrate_bps"):
        if info["abitrate_bps"] > t_abps * 1.2:
            return False

    return True


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
        cmd = ["ffmpeg", "-y", "-i", self.src]
        if self.codec == "copy":
            # Smart remux: stream-copy all tracks into MPEG-TS, no re-encode
            cmd += ["-c", "copy"]
        else:
            vcodec = "libx264" if self.codec == "h264" else "libx265"
            cmd += [
                "-c:v",    vcodec,
                "-preset", self.preset,
                "-b:v",    self.vbitrate,
                "-maxrate", self.vbitrate,
                "-bufsize", self._bufsize(),
            ]
            cmd += self._vf_args()
            cmd += self._fps_args()
            cmd += ["-c:a", "aac", "-b:a", self.abitrate]
        cmd += ["-f", "mpegts", "-progress", "pipe:1", "-nostats", self.dst]
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

            # FFmpeg -progress writes key=value lines in blocks ended by "progress=..."
            block: dict = {}
            for line in self.process.stdout:
                if not self.active:
                    break
                line = line.strip()
                if "=" not in line:
                    continue
                key, _, val = line.partition("=")
                block[key] = val.strip()

                if key == "progress":           # full block received — emit
                    try:
                        us = int(block.get("out_time_us", 0))
                    except (ValueError, TypeError):
                        us = 0
                    if duration > 0 and us > 0:
                        pct     = min(99, int(us / (duration * 1_000_000) * 100))
                        elapsed = time.time() - start_ts
                        eta     = int((elapsed / pct) * (100 - pct)) if pct > 0 else 0
                    else:
                        pct = eta = 0
                    fps   = block.get("fps",   "")
                    speed = block.get("speed", "")
                    if on_progress:
                        on_progress(self.cid, pct, eta, fps=fps, speed=speed)
                    block = {}

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
