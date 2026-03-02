"""
config.py — Load lavacast.config.json and expose typed section dicts.

Usage
-----
    from config import SERVER, STREAMING, TRANSCODE

Keys missing from the JSON file fall back to the built-in defaults below,
so the application always starts even if the config file is absent or
partially edited.
"""

import json
import os

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lavacast.config.json")

# ---------------------------------------------------------------------------
# Built-in defaults (lowest priority)
# ---------------------------------------------------------------------------

_DEFAULTS: dict = {
    "server": {
        "port":          5000,
        "max_upload_gb": 20,
        "secret_key":    "lavacast40-v8",
    },
    "streaming": {
        "max_channels":    40,
        "base_port":       1234,
        "multicast_base":  "239.252.100",
        "default_encap":   "udp",
        "default_loop":    True,
        "default_bitrate": "",
        "selected_nic":    "",
        "media_path":      "~/lavacast40/media",
    },
    "transcode": {
        "codec":      "h264",
        "preset":     "fast",
        "vbitrate":   "8M",
        "abitrate":   "192k",
        "resolution": "1080p",
        "fps":        "original",
    },
}

# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def _merge(base: dict, over: dict) -> dict:
    """Shallow-merge *over* into *base*, one level deep; skip '_*' comment keys."""
    out = dict(base)
    for k, v in over.items():
        if str(k).startswith("_"):
            continue
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = {
                ik: iv
                for ik, iv in {**out[k], **v}.items()
                if not str(ik).startswith("_")
            }
        else:
            out[k] = v
    return out


def _load() -> dict:
    cfg = {s: dict(v) for s, v in _DEFAULTS.items()}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as fh:
                cfg = _merge(cfg, json.load(fh))
            _status["loaded"] = True
        except Exception as exc:
            _status["error"] = str(exc)
    return cfg


# Track load outcome so app.py can log it after the logger is ready
_status: dict = {"loaded": False, "error": None}

_cfg      = _load()
SERVER    = _cfg["server"]
STREAMING = _cfg["streaming"]
TRANSCODE = _cfg["transcode"]
