# CLAUDE.md — LavaCast 40 v8

AI assistant reference for the LavaCast 40 v8 codebase. Read this before making any changes.

---

## Project Overview

**LavaCast 40 v8** is a 40-channel UDP/RTP multicast streaming server.

- Users upload video/audio files via a browser UI
- Files can be pre-transcoded (FFmpeg H.264/H.265) or streamed as-is
- Each of the 40 channels streams to an auto-assigned multicast IP/port
- Real-time metrics (CPU, RAM, NIC bandwidth) are pushed to the browser via WebSocket

**Stack:** Python 3 (Flask + Flask-SocketIO + eventlet) · FFmpeg · Vanilla JS single-page frontend

---

## Repository Layout

```
Lavacaster-code/
├── app.py           # Flask app, REST API, SocketIO metrics, global state
├── streamer.py      # StreamChannel + StreamManager classes (FFmpeg streaming)
├── transcoder.py    # TranscodeJob class (FFmpeg pre-transcode pipeline)
├── uploader.py      # File upload, thumbnail generation, transcode trigger
├── logger.py        # Thread-safe structured JSON logger
├── metrics.py       # CPU/RAM/NIC sampling from /proc (eventlet-safe)
├── index.html       # Single-page frontend (inline CSS + vanilla JS)
├── install.sh       # One-shot Ubuntu/Debian installer + systemd setup
└── requirements.txt # Python pip dependencies
```

No `src/` directory, no subdirectories for Python modules — everything lives in the project root.

---

## Key Constants & Defaults

| Constant | Value | Location |
|---|---|---|
| `MAX_CHANNELS` | 40 | `app.py` |
| `BASE_PORT` | 1234 | `config.py` |
| Server port | 5000 | `app.py` (socketio.run) |
| Multicast base | `239.252.100.x` | `config.py` / `streamer.py` |
| Max upload size | 20 GB | `app.py` |
| Log max lines | 2000 | `logger.py` |
| Thumbnail size | 320×180 px | `uploader.py` |

**Channel ID (`cid`):** 0-based integer (0–39). The UI displays channels as `CH01`–`CH40` (1-based).

**Auto-assigned multicast:**
- IP: `239.252.100.{cid + 1}` — CH01 = `.1`, CH40 = `.40`
- Port: `1234` (same for all channels — receivers distinguish streams by joining the per-channel multicast group)

---

## File Paths at Runtime

```
~/lavacast40/
├── logs/lavacast40.log        # JSON log file
└── media/
    ├── originals/             # Uploaded source files (ch<cid>_<filename>)
    └── transcoded/            # Pre-transcoded .ts files (ch<cid>.ts)

frontend/static/thumbnails/    # Thumbnail JPEGs (ch<cid>.jpg)
```

Naming convention: files are always prefixed with `ch<cid>_` (originals) or `ch<cid>` (transcoded/thumbnails).

---

## Module Responsibilities

### `app.py`
- Initialises Flask, configures CORS and 20 GB upload limit
- Owns all REST API routes (`/api/...`)
- Owns global state: `channels` dict, `metadata` dict, `transcode_jobs` dict, `selected_nic`, global transcode settings
- Runs a background **OS thread** (not eventlet green thread) to emit metrics every 1 s
- Do not add blocking calls to routes; use daemon threads for slow work

### `streamer.py`
- `StreamChannel`: wraps a single FFmpeg streaming subprocess; handles UDP vs RTP encapsulation, multicast IP/port, NIC binding, loop mode, bitrate regulation
- `StreamManager`: owns up to 40 `StreamChannel` instances; coordinates start/stop/transcode state; provides `start_all()` / `stop_all()` helpers
- Subprocess teardown: `terminate()` with 3-second timeout, then `kill()`

### `transcoder.py`
- `TranscodeJob`: runs FFmpeg to convert a source file to MPEG-TS; tracks progress via `pipe:1`; supports cancellation
- `probe_duration(filepath)`: calls `ffprobe` to get duration in seconds
- Validation sets: `VALID_CODECS`, `VALID_PRESETS`, `VALID_RESOLUTIONS`, `VALID_FPS` — always validate against these before constructing FFmpeg arguments

### `uploader.py`
- Validates file extension, saves to `originals/`, generates thumbnail in background, then either calls `manager.add_channel()` directly (copy mode) or `manager.start_transcode()`
- Thumbnail: video → frame at 10% duration; audio → orange (`#ff6a00`) waveform

### `logger.py`
- Write logs with `log.info()`, `log.warn()`, `log.error()`, `log.stream()`, `log.system()`
- All log calls are thread-safe; the file is auto-truncated at 2000 lines (oldest 50% dropped)
- Import: `from logger import log`

### `metrics.py`
- Reads `/proc/stat`, `/proc/meminfo`, `/proc/net/dev` — intentionally avoids `psutil.cpu_percent()` which conflicts with eventlet monkey-patching
- Do not switch to psutil for CPU/memory without resolving the eventlet conflict first

### `index.html`
- Entirely self-contained (inline `<style>` + `<script>`)
- Uses Socket.IO client CDN and Google Fonts CDN
- Design tokens: lava orange `#ff6a00`, mystic purple `#b04aff`, dark background `#1a1a2e`
- Do not split into separate CSS/JS files unless explicitly asked

---

## Threading Model

| Thread type | Used for | Notes |
|---|---|---|
| eventlet green threads | Flask routes, SocketIO handlers | Default; do not block |
| Real OS daemon thread | Metrics broadcast | Avoids eventlet `/proc` conflict |
| Daemon threads | Upload processing, transcode | One per operation |
| Subprocess | Each FFmpeg instance | Managed by `StreamChannel` / `TranscodeJob` |

**Rule:** Never call `time.sleep()` or blocking I/O on the main eventlet thread. Use `eventlet.sleep()` in green threads, or spawn a real daemon thread for anything blocking.

---

## REST API Reference

| Method | Path | Description |
|---|---|---|
| GET | `/` | Serve `index.html` |
| POST | `/api/upload/<cid>` | Upload file to channel |
| POST | `/api/retranscode/<cid>` | Re-transcode existing original |
| POST | `/api/start/<cid>` | Start streaming channel |
| POST | `/api/stop/<cid>` | Stop streaming channel |
| POST | `/api/start_all` | Start all configured channels |
| POST | `/api/stop_all` | Stop all channels |
| DELETE | `/api/remove/<cid>` | Remove channel and delete files |
| POST | `/api/cancel_transcode/<cid>` | Cancel in-progress transcode |
| GET | `/api/status` | Return full state of all channels |
| POST | `/api/settings/<cid>` | Update per-channel settings |
| POST | `/api/settings/global` | Update global transcode profile |
| POST | `/api/settings/bitrate` | Apply global bitrate limit |
| POST | `/api/settings/nic` | Set active NIC |
| GET | `/api/logs` | Return log lines (JSON) |
| POST | `/api/logs/clear` | Clear log file |
| POST | `/api/system/restart` | Restart the Flask process |
| POST | `/api/system/shutdown` | Shut down the Flask process |
| GET | `/thumbnails/<filename>` | Serve thumbnail image |

---

## SocketIO Events

| Direction | Event | Payload |
|---|---|---|
| Server → Client | `metrics` | `{cpu, ram, nics: [{name, rx_mbps, tx_mbps}]}` |
| Server → Client | `transcode_progress` | `{cid, pct, eta}` |
| Server → Client | `transcode_complete` | `{cid, ...channel metadata}` |
| Server → Client | `transcode_error` | `{cid, error}` |
| Server → Client | `channel_update` | `{cid, ...channel metadata}` |
| Server → Client | `stream_started` | `{cid}` |
| Server → Client | `stream_stopped` | `{cid}` |
| Server → Client | `upload_complete` | `{cid, ...}` |

---

## Supported Formats & Validation

**Uploadable extensions:** `.mp4`, `.mkv`, `.avi`, `.mov`, `.ts`, `.m2ts`, `.mp3`, `.wav`, `.flac`, `.aac`, `.m4a`, `.ogg`

**Transcode codecs:** `copy`, `h264`, `h265`

**Presets:** `ultrafast`, `superfast`, `fast`, `medium`, `slow`

**Resolutions:** `original`, `720p`, `1080p`, `1440p`, `4k`

**FPS options:** `original`, `23.976`, `24`, `25`, `29.97`, `30`, `50`, `59.94`, `60`

**Bitrate presets (streaming):** Passthrough (copy), 1M, 2M, 4M, 6M, 8M, 10M, 15M, 20M

Always validate against the sets in `transcoder.py` before constructing FFmpeg arguments.

---

## Development Workflow

### Running locally

```bash
# Install system deps (Ubuntu/Debian)
sudo apt install ffmpeg python3 python3-venv

# Create venv and install
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Run
python3 app.py
# Open http://localhost:5000
```

### Running via install.sh (production)

```bash
bash install.sh
sudo systemctl start lavacast40
sudo systemctl status lavacast40
```

### Logs

```bash
tail -f ~/lavacast40/logs/lavacast40.log
# OR via API:
curl http://localhost:5000/api/logs
```

---

## Conventions to Follow

1. **Logging:** Always use `log.info()` / `log.warn()` / `log.error()` from `logger.py`. Never use `print()` in production code paths.

2. **Error responses:** Return JSON `{"error": "<message>"}` with an appropriate HTTP status code (400 for client errors, 500 for server errors).

3. **Channel IDs:** Accept `cid` as an integer from URL routes; validate `0 <= cid < MAX_CHANNELS` before use.

4. **FFmpeg arguments:** Build as a Python list (never shell-interpolated strings) to prevent injection. Pass to `subprocess.Popen` with `shell=False`.

5. **Thread safety:** Access to shared dicts (`channels`, `metadata`, `transcode_jobs`) in `app.py` is currently unguarded — keep mutations to route handlers (single-threaded SocketIO context) or add a lock if touching from daemon threads.

6. **Frontend updates:** After any state change in a route, emit a `channel_update` SocketIO event so the UI reflects the change without requiring a page reload.

7. **File cleanup:** When removing a channel (`DELETE /api/remove/<cid>`), delete originals, transcoded `.ts`, and thumbnail from disk.

8. **No new dependencies** unless strictly necessary. The stack is intentionally lightweight.

9. **Inline HTML:** Keep all CSS and JS inside `index.html`. Do not create separate static files.

10. **Do not change the secret key** (`"lavacast40-v8"`) or server port (5000) without updating `install.sh` and documenting the change.

---

## Common Pitfalls

- **eventlet monkey-patching:** `eventlet.monkey_patch()` is called at the top of `app.py` before any other imports. Do not move it or add imports before it.
- **psutil + eventlet:** `psutil.cpu_percent()` blocks in a way that breaks eventlet. Use `/proc` reads (see `metrics.py`) for CPU/memory.
- **Subprocess leaks:** Always terminate FFmpeg subprocesses before removing a channel. `StreamManager` handles this, but custom subprocess use must follow the same pattern.
- **Port conflicts:** Streaming ports start at 5100. The web server is on 5000. Do not use ports in either of these ranges for new features.
- **Multicast route:** The installer adds `239.0.0.0/8` via loopback. On a fresh dev machine you may need: `sudo ip route add 239.0.0.0/8 dev lo`

---

## Git Conventions

- Branch names follow: `claude/<short-description>-<id>`
- Commit messages: imperative mood, concise, e.g. `Fix: thumbnail missing after retranscode`
- No force-pushes to `master` / `main`
- PRs are merged from feature branches
