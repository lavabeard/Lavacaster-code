"""
streaming/streamer.py — UDP/RTP multicast stream lifecycle management.

StreamChannel   wraps a single FFmpeg process streaming one file to one address.
StreamManager   owns up to 40 channels, auto-assigns IPs/ports, manages state.

The StreamManager also owns TranscodeJob references so the two concerns
(transcoding + streaming) are coordinated in one place, but transcode logic
lives entirely in transcoding/transcoder.py.
"""

import json
import os
import socket
import struct
import fcntl
import subprocess
import threading

import logger
from transcoder import TranscodeJob

_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "channel_state.json")

# ---------------------------------------------------------------------------
# Bitrate preset list (used by UI and upload layer)
# ---------------------------------------------------------------------------
BITRATE_PRESETS = [
    ("Passthrough (copy)", ""),
    ("1 Mbps",  "1M"),  ("2 Mbps",  "2M"),  ("4 Mbps",  "4M"),
    ("6 Mbps",  "6M"),  ("8 Mbps",  "8M"),  ("10 Mbps", "10M"),
    ("15 Mbps", "15M"), ("20 Mbps", "20M"),
]


# ---------------------------------------------------------------------------
# StreamChannel
# ---------------------------------------------------------------------------

class StreamChannel:
    """
    Manages a single FFmpeg streaming process for one channel.

    Parameters
    ----------
    cid            : int     channel index (0-based)
    filepath       : str     path to the media file to stream (.ts preferred)
    ip             : str     multicast destination IP
    port           : int     destination UDP/RTP port
    encap          : str     "udp" or "rtp"
    bitrate        : str     e.g. "4M" — only used for copy-mode streams
    loop           : bool    restart FFmpeg when the file ends
    nic            : str     NIC name to bind the multicast source address
    pre_transcoded : bool    if True, always -c copy at stream time
    """

    def __init__(
        self,
        cid: int,
        filepath: str,
        ip: str,
        port: int,
        encap: str = "udp",
        bitrate: str = None,
        loop: bool = True,
        nic: str = None,
        pre_transcoded: bool = False,
    ):
        self.cid            = cid
        self.filepath       = filepath
        self.ip             = ip
        self.port           = port
        self.encap          = encap
        self.bitrate        = bitrate
        self.loop           = loop
        self.nic            = nic
        self.pre_transcoded = pre_transcoded
        self.process        = None
        self.running        = False
        self._thread        = None

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def start(self, on_stop=None):
        if self.running:
            return
        self.running = True
        self._thread = threading.Thread(
            target=self._run, args=(on_stop,), daemon=True
        )
        self._thread.start()
        logger.stream(
            f"CH{self.cid + 1:02d} started",
            {
                "file":    os.path.basename(self.filepath),
                "dest":    f"{self.encap}://{self.ip}:{self.port}",
                "bitrate": self.bitrate or "passthrough",
                "nic":     self.nic or "default",
                "pre_tc":  self.pre_transcoded,
            },
        )

    def stop(self):
        self.running = False
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=3)
            except Exception:
                self.process.kill()
            self.process = None

    def update_settings(
        self,
        ip=None, port=None, encap=None,
        bitrate=None, loop=None, nic=None,
    ) -> bool:
        """Apply new settings; returns True if channel was running (caller must restart)."""
        was = self.running
        if was:
            self.stop()
        if ip      is not None: self.ip      = ip
        if port    is not None: self.port    = int(port)
        if encap   is not None: self.encap   = encap
        if bitrate is not None: self.bitrate = bitrate or None
        if loop    is not None: self.loop    = loop
        if nic     is not None: self.nic     = nic or None
        return was

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _build_cmd(self) -> list:
        params = "pkt_size=1316&ttl=10"
        nic_ip = self._nic_ip()
        if nic_ip:
            params += f"&localaddr={nic_ip}"

        if self.encap == "rtp":
            url = f"rtp://{self.ip}:{self.port}?{params}"
            fmt = "rtp_mpegts"
        else:
            url = f"udp://{self.ip}:{self.port}?{params}"
            fmt = "mpegts"

        cmd = ["ffmpeg", "-re"]
        if self.loop:
            cmd += ["-stream_loop", "-1"]
        cmd += ["-i", self.filepath]
        if self.pre_transcoded or not self.bitrate:
            cmd += ["-c", "copy"]
        else:
            kbps = self._to_kbps()
            cmd += [
                "-b:v", self.bitrate,
                "-maxrate", self.bitrate,
                "-bufsize", f"{kbps * 2}k",
            ]
        cmd += ["-f", fmt, url]
        return cmd

    def _nic_ip(self) -> str | None:
        if not self.nic:
            return None
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            result = fcntl.ioctl(
                s.fileno(), 0x8915,
                struct.pack("256s", self.nic[:15].encode("utf-8")),
            )
            return socket.inet_ntoa(result[20:24])
        except Exception:
            return None

    def _to_kbps(self) -> int:
        b = str(self.bitrate or "4M").upper()
        if b.endswith("M"): return int(float(b[:-1]) * 1000)
        if b.endswith("K"): return int(b[:-1])
        return 4000

    def _run(self, on_stop):
        try:
            self.process = subprocess.Popen(
                self._build_cmd(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.process.wait()
        except Exception as e:
            logger.error(f"CH{self.cid + 1:02d} FFmpeg: {e}")
        self.running = False
        logger.stream(f"CH{self.cid + 1:02d} stopped")
        if on_stop:
            on_stop(self.cid)


# ---------------------------------------------------------------------------
# StreamManager
# ---------------------------------------------------------------------------

class StreamManager:
    """
    Owns all 40 StreamChannel instances plus in-flight TranscodeJobs.

    Channel IDs are 0-based integers.  The UI uses 1-based CH labels.
    """

    MAX_CHANNELS = 40
    BASE_PORT    = 5100

    def __init__(self):
        self.channels       = {}          # cid -> StreamChannel
        self.metadata       = {}          # cid -> dict of UI-visible state
        self.transcode_jobs = {}          # cid -> TranscodeJob (in-flight only)
        self.global_bitrate = None
        self.selected_nic   = None
        self.media_path     = os.path.expanduser("~/lavacast40/media")
        logger.system("StreamManager v8 initialized")
        self._load_state()

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    def _save_state(self):
        state = {
            "global_bitrate": self.global_bitrate,
            "selected_nic":   self.selected_nic,
            "media_path":     self.media_path,
            "channels":       {str(cid): m for cid, m in self.metadata.items()},
        }
        try:
            with open(_STATE_FILE, "w") as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            logger.error(f"State save failed: {e}")

    def _load_state(self):
        if not os.path.exists(_STATE_FILE):
            return
        try:
            with open(_STATE_FILE) as f:
                state = json.load(f)
        except Exception as e:
            logger.error(f"State load failed: {e}")
            return

        self.global_bitrate = state.get("global_bitrate")
        self.selected_nic   = state.get("selected_nic")
        if state.get("media_path"):
            self.media_path = state["media_path"]

        for cid_str, m in state.get("channels", {}).items():
            cid      = int(cid_str)
            filepath = m.get("filepath", "")
            if not filepath or not os.path.exists(filepath):
                logger.warn(
                    f"CH{cid + 1:02d} restore skipped — file missing: {filepath}"
                )
                continue
            self.channels[cid] = StreamChannel(
                cid, filepath,
                ip             = m.get("ip",             self._auto_ip(cid)),
                port           = m.get("port",            self._auto_port(cid)),
                encap          = m.get("encap",           "udp"),
                bitrate        = self.global_bitrate,
                loop           = m.get("loop",            True),
                nic            = self.selected_nic,
                pre_transcoded = m.get("pre_transcoded",  False),
            )
            self.metadata[cid] = {**m, "running": False}
            logger.info(f"CH{cid + 1:02d} restored: {m.get('filename', '?')}")

    # ------------------------------------------------------------------
    # Address helpers
    # ------------------------------------------------------------------

    def _auto_ip(self, cid: int) -> str:
        return f"239.1.1.{(cid % 254) + 1}"

    def _auto_port(self, cid: int) -> int:
        return self.BASE_PORT + (cid * 2)

    # ------------------------------------------------------------------
    # Channel lifecycle
    # ------------------------------------------------------------------

    def add_channel(
        self,
        cid: int,
        filepath: str,
        filename: str,
        pre_transcoded: bool = False,
        src_path: str = None,
        codec: str = "copy",
    ) -> tuple[str, int]:
        """Register or update a channel.  Returns (ip, port)."""
        ip   = self._auto_ip(cid)
        port = self._auto_port(cid)
        prev = self.metadata.get(cid, {})

        if cid in self.channels:
            self.channels[cid].filepath       = filepath
            self.channels[cid].pre_transcoded = pre_transcoded
        else:
            self.channels[cid] = StreamChannel(
                cid, filepath, ip, port,
                encap          = prev.get("encap", "udp"),
                bitrate        = self.global_bitrate,
                loop           = prev.get("loop", True),
                nic            = self.selected_nic,
                pre_transcoded = pre_transcoded,
            )

        self.metadata[cid] = dict(
            filename       = filename,
            filepath       = filepath,
            src_path       = src_path or filepath,
            ip             = ip,
            port           = port,
            encap          = prev.get("encap", "udp"),
            bitrate        = self.global_bitrate or "",
            loop           = prev.get("loop", True),
            codec          = codec,
            preset         = prev.get("preset",   "fast"),
            vbitrate       = prev.get("vbitrate",  "6M"),
            abitrate       = prev.get("abitrate",  "192k"),
            running        = False,
            pre_transcoded = pre_transcoded,
            thumb          = f"/static/thumbnails/ch{cid}.jpg",
        )
        logger.info(
            f"CH{cid + 1:02d} loaded: {filename}",
            {"ip": ip, "port": port, "pre_tc": pre_transcoded},
        )
        self._save_state()
        return ip, port

    def remove_channel(self, cid: int):
        fname = self.metadata.get(cid, {}).get("filename", "?")
        self.cancel_transcode(cid)
        self.stop(cid)
        self.channels.pop(cid, None)
        self.metadata.pop(cid, None)
        logger.info(f"CH{cid + 1:02d} removed: {fname}")
        self._save_state()

    _NET_KEYS = {"ip", "port", "encap", "bitrate", "loop", "nic"}

    def update_channel(self, cid: int, **kw) -> bool:
        """Update settings on an existing channel.  Returns was_running."""
        ch = self.channels.get(cid)
        if not ch:
            return False
        # update_settings only understands network-level keys
        was = ch.update_settings(**{k: v for k, v in kw.items() if k in self._NET_KEYS})
        m   = self.metadata[cid]
        for k, v in kw.items():
            if k in m:
                m[k] = int(v) if k == "port" else (v or "")
        self._save_state()
        return was

    # ------------------------------------------------------------------
    # Stream start / stop
    # ------------------------------------------------------------------

    def start(self, cid: int, on_stop=None):
        ch = self.channels.get(cid)
        if ch:
            ch.nic = self.selected_nic
            ch.start(on_stop=on_stop)
            self.metadata[cid]["running"] = True

    def stop(self, cid: int):
        ch = self.channels.get(cid)
        if ch:
            ch.stop()
            if cid in self.metadata:
                self.metadata[cid]["running"] = False

    def start_all(self, on_stop=None):
        count = sum(1 for c in self.channels if not self.is_running(c))
        for cid in self.channels:
            self.start(cid, on_stop=on_stop)
        logger.stream(f"Start All: {count} streams launched")

    def stop_all(self):
        count = sum(1 for c in self.channels if self.is_running(c))
        for cid in list(self.channels):
            self.stop(cid)
        logger.stream(f"Stop All: {count} streams halted")

    def is_running(self, cid: int) -> bool:
        ch = self.channels.get(cid)
        return ch.running if ch else False

    # ------------------------------------------------------------------
    # Global settings
    # ------------------------------------------------------------------

    def set_nic(self, nic: str):
        self.selected_nic = nic or None
        for ch in self.channels.values():
            ch.nic = self.selected_nic
        logger.info(f"Streaming NIC set to: {nic or 'default'}")
        self._save_state()

    def apply_global_bitrate(self, bitrate: str):
        self.global_bitrate = bitrate or None
        for cid, ch in self.channels.items():
            if not self.metadata[cid].get("pre_transcoded"):
                ch.bitrate = self.global_bitrate
            self.metadata[cid]["bitrate"] = self.global_bitrate or ""
        logger.info("Global bitrate", {"bitrate": self.global_bitrate or "passthrough"})
        self._save_state()

    # ------------------------------------------------------------------
    # Transcode coordination
    # ------------------------------------------------------------------

    def start_transcode(
        self,
        cid: int,
        src: str,
        dst: str,
        codec: str,
        preset: str,
        vbitrate: str,
        abitrate: str,
        resolution: str,
        fps: str,
        on_progress,
        on_complete,
        on_error,
    ):
        self.cancel_transcode(cid)
        job = TranscodeJob(cid, src, dst, codec, preset, vbitrate, abitrate, resolution, fps)
        self.transcode_jobs[cid] = job
        job.start(on_progress, on_complete, on_error)

    def cancel_transcode(self, cid: int):
        job = self.transcode_jobs.pop(cid, None)
        if job:
            job.cancel()

    # ------------------------------------------------------------------
    # Status snapshot (for /api/status)
    # ------------------------------------------------------------------

    def get_status(self) -> dict:
        return {
            str(cid): {**m, "running": self.is_running(cid)}
            for cid, m in self.metadata.items()
        }
