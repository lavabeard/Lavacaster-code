"""
app.py — LavaCast 40 v8  |  Flask + Socket.IO entry point.

This file is intentionally thin: it wires together the four modules
(uploading, transcoding, streaming, frontend) and exposes the REST API.

Module layout
─────────────
  shared/metrics.py     → /proc-based system metrics
  shared/logger.py      → JSON structured log
  transcoding/          → FFmpeg H.264/H.265 pre-transcode pipeline
  streaming/streamer.py → StreamChannel + StreamManager
  uploading/uploader.py → file receive, thumbnail, pipeline kick-off
  frontend/templates/   → index.html
"""

import os
import re
import sys
import threading
import time

from flask import Flask, jsonify, render_template, request, send_from_directory
from flask_socketio import SocketIO

import config as cfg
from streamer import StreamManager, BITRATE_PRESETS
from transcoder import VALID_CODECS, VALID_PRESETS, VALID_RESOLUTIONS, VALID_FPS
from uploader import process_upload, validate_extension
import logger
from metrics import collect, read_mem

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

# Regex for valid bitrate strings: e.g. "6M", "192k", "1.5M"
_BITRATE_RE = re.compile(r"^\d+(\.\d+)?[kKmM]$")

BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
ORIG_DIR  = os.path.join(BASE_DIR, "media", "originals")
TRANS_DIR = os.path.join(BASE_DIR, "media", "transcoded")
THUMB_DIR = os.path.join(BASE_DIR, "frontend", "static", "thumbnails")

for d in (ORIG_DIR, TRANS_DIR, THUMB_DIR):
    os.makedirs(d, exist_ok=True)

app = Flask(
    __name__,
    template_folder=BASE_DIR,
    static_folder=os.path.join(BASE_DIR, "frontend", "static"),
)
app.config["SECRET_KEY"]         = cfg.SERVER["secret_key"]
app.config["MAX_CONTENT_LENGTH"] = cfg.SERVER["max_upload_gb"] * 1024 ** 3

socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")
manager  = StreamManager(
    max_channels    = cfg.STREAMING["max_channels"],
    base_port       = cfg.STREAMING["base_port"],
    multicast_base  = cfg.STREAMING["multicast_base"],
    default_encap   = cfg.STREAMING["default_encap"],
    default_loop    = cfg.STREAMING["default_loop"],
    default_bitrate = cfg.STREAMING["default_bitrate"],
    selected_nic    = cfg.STREAMING["selected_nic"],
    media_path      = cfg.STREAMING["media_path"],
)

# Global transcode profile — seeded from config, then overridden by any saved runtime state
GLOBAL_TC: dict = dict(cfg.TRANSCODE)
if manager.global_tc:                       # state file had a saved TC profile
    GLOBAL_TC.update(manager.global_tc)
manager.global_tc = dict(GLOBAL_TC)        # keep manager in sync for the first save


def _persist_global_tc():
    """Sync GLOBAL_TC into the manager and flush channel_state.json."""
    manager.global_tc = dict(GLOBAL_TC)
    manager._save_state()


# Bootstrap lavacast_channels.json on first launch so the file exists
# before any channel is added (upgrades preserve the existing file).
if not os.path.exists(os.path.join(BASE_DIR, "lavacast_channels.json")):
    _persist_global_tc()
    logger.system("Created lavacast_channels.json with default settings")


# ---------------------------------------------------------------------------
# Background: metrics loop (real OS thread — safe under eventlet)
# ---------------------------------------------------------------------------

_last_metrics: dict = {}

# Seed memory stats immediately at startup so the REST endpoint has something
# to return before the background thread completes its first 5-second sample.
try:
    _m_pct, _m_used, _m_total = read_mem()
    _last_metrics.update({"cpu": 0.0, "mem": _m_pct,
                          "mem_used_gb": _m_used, "mem_total_gb": _m_total, "nics": {}})
except Exception:
    pass

if cfg._status["loaded"]:
    logger.system("Config loaded", {"file": cfg.CONFIG_FILE})
elif cfg._status["error"]:
    logger.warn(f"Config parse error — using defaults: {cfg._status['error']}")
else:
    logger.system("Config file absent — using built-in defaults", {"expected": cfg.CONFIG_FILE})


def _metrics_loop():
    logger.system("Metrics thread started (real OS thread, reads /proc)")
    while True:
        try:
            data = collect(interval=5.0)
            _last_metrics.update(data)
            socketio.emit("metrics", data)
        except Exception as e:
            logger.error(f"Metrics error: {e}")

threading.Thread(target=_metrics_loop, daemon=True).start()


def _on_stop(cid):
    socketio.emit("stream_stopped", {"cid": cid})


# ---------------------------------------------------------------------------
# Routes — Frontend
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html", max_channels=manager.max_channels)


# ---------------------------------------------------------------------------
# Routes — Status / Config
# ---------------------------------------------------------------------------

@app.route("/api/metrics")
def metrics_api():
    """Return the most recently sampled system metrics (updated every ~5 s)."""
    return jsonify(_last_metrics)


@app.route("/api/status")
def status():
    nics = []
    try:
        with open("/proc/net/dev") as f:
            for line in f.readlines()[2:]:
                parts = line.split()
                if len(parts) >= 2:
                    nics.append(parts[0].rstrip(":"))
    except Exception:
        pass

    return jsonify({
        "channels":        manager.get_status(),
        "global_bitrate":  manager.global_bitrate or "",
        "media_path":      manager.media_path,
        "bitrate_presets": BITRATE_PRESETS,
        "nics":            nics,
        "selected_nic":    manager.selected_nic or "",
        "monitor_nic":     manager.monitor_nic  or "",
        "auto_start":      manager.auto_start,
        "global_tc":       GLOBAL_TC,
    })


@app.route("/api/global_transcode", methods=["GET", "POST"])
def global_transcode_api():
    if request.method == "POST":
        d = request.get_json(silent=True) or {}
        for key in ("codec", "preset", "vbitrate", "abitrate", "resolution", "fps"):
            if key in d:
                GLOBAL_TC[key] = str(d[key])
        _persist_global_tc()
        logger.info("Global transcode settings updated", GLOBAL_TC)
        return jsonify({"status": "ok", "global_tc": GLOBAL_TC})
    return jsonify(GLOBAL_TC)


@app.route("/api/settings/global", methods=["POST"])
def global_settings():
    d = request.get_json(silent=True) or {}
    if "bitrate" in d:
        manager.apply_global_bitrate(d["bitrate"])
    if "media_path" in d:
        p = d["media_path"].strip()
        if p and not os.path.isdir(p):
            return jsonify({"error": f"Path not found: {p}"}), 400
        if p:
            manager.media_path = p
    if "nic" in d:
        manager.set_nic(d["nic"])
    if "monitor_nic" in d:
        manager.monitor_nic = d["monitor_nic"] or ""
        manager._save_state()
    if "auto_start" in d:
        manager.auto_start = bool(d["auto_start"])
        manager._save_state()
        logger.info(f"Auto-start {'enabled' if manager.auto_start else 'disabled'}")
    return jsonify({
        "global_bitrate": manager.global_bitrate or "",
        "media_path":     manager.media_path,
        "selected_nic":   manager.selected_nic or "",
        "monitor_nic":    manager.monitor_nic  or "",
        "auto_start":     manager.auto_start,
    })


# ---------------------------------------------------------------------------
# Routes — Upload
# ---------------------------------------------------------------------------

@app.route("/api/upload/<int:cid>", methods=["POST"])
def upload(cid):
    if not 0 <= cid < manager.max_channels:
        return jsonify({"error": "Invalid channel"}), 400
    if "file" not in request.files:
        return jsonify({"error": "No file"}), 400

    f = request.files["file"]

    # Per-upload overrides (fall back to global profile)
    tc = {
        "codec":      request.form.get("codec",      GLOBAL_TC["codec"]),
        "preset":     request.form.get("preset",     GLOBAL_TC["preset"]),
        "vbitrate":   request.form.get("vbitrate",   GLOBAL_TC["vbitrate"]),
        "abitrate":   request.form.get("abitrate",   GLOBAL_TC["abitrate"]),
        "resolution": request.form.get("resolution", GLOBAL_TC["resolution"]),
        "fps":        request.form.get("fps",        GLOBAL_TC["fps"]),
    }
    # Sanitise
    if tc["codec"]      not in VALID_CODECS:               tc["codec"]      = "copy"
    if tc["preset"]     not in VALID_PRESETS:              tc["preset"]     = "fast"
    if tc["resolution"] not in VALID_RESOLUTIONS:          tc["resolution"] = "original"
    if tc["fps"]        not in VALID_FPS:                  tc["fps"]        = "original"
    if not _BITRATE_RE.match(str(tc["vbitrate"])):         tc["vbitrate"]   = GLOBAL_TC["vbitrate"]
    if not _BITRATE_RE.match(str(tc["abitrate"])):         tc["abitrate"]   = GLOBAL_TC["abitrate"]

    overwrite = request.form.get("overwrite", "false").lower() == "true"

    status, data = process_upload(
        cid, f,
        ORIG_DIR, TRANS_DIR, THUMB_DIR,
        tc, manager, socketio,
        overwrite=overwrite,
    )
    if status == "error":
        return jsonify({"error": data}), 400
    if status == "exists":
        return jsonify({"exists": True, "filename": data}), 409
    return jsonify({"status": "uploading"})


# ---------------------------------------------------------------------------
# Routes — Re-Transcode
# ---------------------------------------------------------------------------

@app.route("/api/retranscode/<int:cid>", methods=["POST"])
def retranscode(cid):
    """Re-transcode an already-uploaded original with new codec settings."""
    meta = manager.metadata.get(cid)
    if not meta:
        return jsonify({"error": "Channel not loaded"}), 404

    src_path = meta.get("src_path")
    if not src_path or not os.path.exists(src_path):
        return jsonify({"error": "Original file not found on server"}), 404

    d          = request.get_json(silent=True) or {}
    codec      = d.get("codec",      GLOBAL_TC["codec"])
    preset     = d.get("preset",     GLOBAL_TC["preset"])
    vbitrate   = d.get("vbitrate",   GLOBAL_TC["vbitrate"])
    abitrate   = d.get("abitrate",   GLOBAL_TC["abitrate"])
    resolution = d.get("resolution", GLOBAL_TC["resolution"])
    fps        = d.get("fps",        GLOBAL_TC["fps"])

    if codec      not in VALID_CODECS:      return jsonify({"error": f"Invalid codec: {codec}"}), 400
    if preset     not in VALID_PRESETS:     preset     = GLOBAL_TC["preset"]
    if resolution not in VALID_RESOLUTIONS: resolution = GLOBAL_TC["resolution"]
    if fps        not in VALID_FPS:         fps        = GLOBAL_TC["fps"]
    if not _BITRATE_RE.match(str(vbitrate)): vbitrate  = GLOBAL_TC["vbitrate"]
    if not _BITRATE_RE.match(str(abitrate)): abitrate  = GLOBAL_TC["abitrate"]

    was_running = manager.is_running(cid)
    if was_running:
        manager.stop(cid)

    if codec == "copy":
        manager.add_channel(
            cid, src_path, meta["filename"],
            pre_transcoded=False, src_path=src_path,
            codec="copy", preset=preset,
            vbitrate=vbitrate, abitrate=abitrate,
        )
        m = manager.metadata[cid]
        socketio.emit("channel_ready", {
            "cid":      cid,
            "filename": meta["filename"],
            "ip":       m["ip"],
            "port":     m["port"],
            "encap":    m.get("encap", "udp"),
            "bitrate":  m.get("bitrate", ""),
            "loop":     m.get("loop", True),
            "codec":    "copy",
            "preset":   preset,
            "vbitrate": vbitrate,
            "abitrate": abitrate,
            "thumb":    f"/api/thumbnail/{cid}?t={time.time()}",
        })
        if was_running:
            manager.start(cid, on_stop=_on_stop)
        logger.info(f"CH{cid + 1:02d} switched to copy (passthrough)")
        return jsonify({"status": "switched_to_copy"})

    stem     = os.path.splitext(meta["filename"])[0]
    dst_path = os.path.join(TRANS_DIR, f"CH{cid + 1:02d}_{stem}.ts")
    socketio.emit("transcode_start", {"cid": cid, "codec": codec, "preset": preset})

    def on_progress(cid, pct, eta_secs=0):
        socketio.emit("transcode_progress", {"cid": cid, "pct": pct, "eta_secs": eta_secs})

    def on_complete(cid, filepath):
        manager.add_channel(
            cid, filepath, meta["filename"],
            pre_transcoded=True, src_path=src_path,
            codec=codec, preset=preset,
            vbitrate=vbitrate, abitrate=abitrate,
        )
        m = manager.metadata[cid]
        socketio.emit("channel_ready", {
            "cid":      cid,
            "filename": meta["filename"],
            "ip":       m["ip"],
            "port":     m["port"],
            "encap":    m.get("encap", "udp"),
            "bitrate":  m.get("bitrate", ""),
            "loop":     m.get("loop", True),
            "codec":    codec,
            "preset":   preset,
            "vbitrate": vbitrate,
            "abitrate": abitrate,
            "thumb":    f"/api/thumbnail/{cid}?t={time.time()}",
        })
        if was_running:
            manager.start(cid, on_stop=_on_stop)

    def on_error(cid, msg):
        socketio.emit("transcode_error", {"cid": cid, "error": msg})

    manager.start_transcode(
        cid, src_path, dst_path,
        codec, preset, vbitrate, abitrate, resolution, fps,
        on_progress, on_complete, on_error,
    )
    logger.info(f"CH{cid + 1:02d} re-transcode started", {"codec": codec, "preset": preset})
    return jsonify({"status": "transcoding"})


# ---------------------------------------------------------------------------
# Routes — Channel controls
# ---------------------------------------------------------------------------

@app.route("/api/channel/<int:cid>/settings", methods=["POST"])
def channel_settings(cid):
    d   = request.get_json(silent=True) or {}
    was = manager.update_channel(
        cid, **{k: v for k, v in d.items() if v is not None}
    )
    m = manager.metadata.get(cid, {})
    if was:
        manager.start(cid, on_stop=_on_stop)
        socketio.emit("stream_restarted", {"cid": cid, "meta": m})
    return jsonify({"status": "updated", "meta": m})


@app.route("/api/start/<int:cid>",  methods=["POST"])
def start_ch(cid):
    manager.start(cid, on_stop=_on_stop)
    return jsonify({"status": "started"})


@app.route("/api/stop/<int:cid>",   methods=["POST"])
def stop_ch(cid):
    manager.stop(cid)
    return jsonify({"status": "stopped"})


@app.route("/api/start_all", methods=["POST"])
def start_all():
    manager.start_all(on_stop=_on_stop)
    return jsonify({"status": "ok"})


@app.route("/api/stop_all", methods=["POST"])
def stop_all():
    manager.stop_all()
    socketio.emit("all_stopped", {})
    return jsonify({"status": "ok"})


@app.route("/api/remove/<int:cid>", methods=["DELETE"])
def remove(cid):
    meta = manager.metadata.get(cid, {})
    manager.remove_channel(cid)
    # Delete original and transcoded media files (CLAUDE.md rule #7)
    for path in {meta.get("src_path"), meta.get("filepath")}:
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except Exception as e:
                logger.warn(f"CH{cid + 1:02d} remove: could not delete file: {e}")
    t = os.path.join(THUMB_DIR, f"ch{cid}.jpg")
    if os.path.exists(t):
        os.remove(t)
    return jsonify({"status": "removed"})


@app.route("/api/thumbnail/<int:cid>")
def get_thumbnail(cid):
    path = os.path.join(THUMB_DIR, f"ch{cid}.jpg")
    if not os.path.exists(path):
        return jsonify({"error": "not found"}), 404
    resp = send_from_directory(THUMB_DIR, f"ch{cid}.jpg", max_age=0)
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ---------------------------------------------------------------------------
# Routes — System (restart / shutdown)
# ---------------------------------------------------------------------------

@app.route("/api/system/restart", methods=["POST"])
def system_restart():
    """Stop all streams, then hot-restart the server process."""
    manager.stop_all()
    logger.system("Server restart requested by user")
    def _restart():
        time.sleep(0.7)
        os.execv(sys.executable, [sys.executable] + sys.argv)
    threading.Thread(target=_restart, daemon=False).start()
    return jsonify({"status": "restarting"})


@app.route("/api/system/shutdown", methods=["POST"])
def system_shutdown():
    """Stop all streams, then exit the server process."""
    manager.stop_all()
    logger.system("Server shutdown requested by user")
    def _shutdown():
        time.sleep(0.7)
        os._exit(0)
    threading.Thread(target=_shutdown, daemon=False).start()
    return jsonify({"status": "shutting_down"})


# ---------------------------------------------------------------------------
# Routes — Logs
# ---------------------------------------------------------------------------

@app.route("/api/logs")
def get_logs():
    n = request.args.get("n", 300, type=int)
    return jsonify({"entries": logger.read_log(n)})


@app.route("/api/logs/clear", methods=["POST"])
def clear_logs():
    logger.clear_log()
    logger.system("Log cleared by user")
    return jsonify({"status": "cleared"})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import socket as _s
    try:
        local_ip = _s.gethostbyname(_s.gethostname())
    except Exception:
        local_ip = "0.0.0.0"
    port = cfg.SERVER["port"]
    logger.system("LavaCast 40 v8 starting", {"host": local_ip, "port": port, "url": f"http://{local_ip}:{port}"})

    if manager.auto_start and manager.channels:
        def _auto_start():
            time.sleep(2.5)  # give the server a moment to finish binding
            manager.start_all(on_stop=_on_stop)
            logger.system(f"Auto-start: launched {len(manager.channels)} channel(s)")
        threading.Thread(target=_auto_start, daemon=True).start()

    # Regenerate any missing thumbnails for channels restored from state file
    if manager.metadata:
        from uploader import generate_thumbnail
        def _regen_thumbs():
            time.sleep(1.5)  # let the server finish binding first
            for cid, m in list(manager.metadata.items()):
                thumb = os.path.join(THUMB_DIR, f"ch{cid}.jpg")
                if not os.path.exists(thumb):
                    src = m.get("src_path") or m.get("filepath", "")
                    if src and os.path.exists(src):
                        logger.info(f"CH{cid + 1:02d} regenerating missing thumbnail")
                        generate_thumbnail(src, cid, THUMB_DIR)
        threading.Thread(target=_regen_thumbs, daemon=True).start()

    socketio.run(app, host="0.0.0.0", port=port, debug=False)
