"""
shared/logger.py — Structured JSON-line logger.

Keeps a rolling log file capped at MAX_LINES entries.
Provides typed helpers: info, warn, error, stream, system.
"""
import json
import os
import threading
from datetime import datetime

LOG_PATH  = os.path.join(os.path.expanduser("~"), "lavacast40", "logs", "lavacast40.log")
MAX_LINES = 2000
_lock     = threading.Lock()


def _write(level: str, msg: str, data: dict = None):
    entry = {
        "ts":    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "level": level,
        "msg":   msg,
    }
    if data:
        entry["data"] = data

    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)

    with _lock:
        # Rolling truncation: drop the oldest half when over limit
        try:
            with open(LOG_PATH, "r") as f:
                lines = f.readlines()
            if len(lines) >= MAX_LINES:
                with open(LOG_PATH, "w") as f:
                    f.writelines(lines[MAX_LINES // 2:])
        except FileNotFoundError:
            pass

        with open(LOG_PATH, "a") as f:
            f.write(json.dumps(entry) + "\n")


def info(msg,   data=None): _write("INFO",   msg, data)
def warn(msg,   data=None): _write("WARN",   msg, data)
def error(msg,  data=None): _write("ERROR",  msg, data)
def stream(msg, data=None): _write("STREAM", msg, data)
def system(msg, data=None): _write("SYSTEM", msg, data)


def read_log(last_n=300):
    """Return the last `last_n` log entries as a list of dicts."""
    try:
        with open(LOG_PATH, "r") as f:
            lines = f.readlines()
        out = []
        for line in lines[-last_n:]:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                out.append({"ts": "?", "level": "RAW", "msg": line})
        return out
    except FileNotFoundError:
        return []


def clear_log():
    with _lock:
        try:
            open(LOG_PATH, "w").close()
        except Exception:
            pass
