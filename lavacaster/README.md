# LavaCast 40 v8

A 40-channel multicast streaming server with H.264/H.265 pre-transcode pipeline, real-time WebSocket UI, and per-channel controls.

## Repository Structure

```
lavacast40/
├── frontend/           # Web UI (Flask templates + static assets)
│   ├── templates/
│   │   └── index.html
│   └── static/
│       └── thumbnails/     (generated at runtime)
│
├── transcoding/        # FFmpeg transcode pipeline
│   └── transcoder.py       # TranscodeJob class, probe_duration
│
├── streaming/          # Stream channel management
│   └── streamer.py         # StreamChannel, StreamManager
│
├── uploading/          # File upload + processing logic
│   └── uploader.py         # upload handling, thumbnail generation
│
├── shared/             # Shared utilities (used across all modules)
│   ├── metrics.py          # /proc-based CPU/RAM/NIC metrics
│   └── logger.py           # JSON-line structured logger
│
├── app.py              # Flask app entry point + route wiring
├── scripts/
│   ├── install.sh          # Full system setup (apt, venv, systemd)
│   ├── launch.sh           # Manual start helper
│   └── boot_launch.sh      # Boot-time launcher
├── systemd/
│   └── lavacast40.service  # systemd unit file
└── requirements.txt
```

## Quick Start

```bash
# Full install (Ubuntu/Debian)
bash scripts/install.sh

# Manual start
bash scripts/launch.sh
```

Web GUI: `http://<your-ip>:5000`

## Modules

| Module | Responsibility |
|--------|---------------|
| `frontend/` | HTML/CSS/JS single-page UI, Socket.IO client |
| `transcoding/` | FFmpeg H.264/H.265 pre-transcode with progress |
| `streaming/` | FFmpeg UDP/RTP multicast stream lifecycle |
| `uploading/` | File receive, validation, thumbnail, pipeline kick-off |
| `shared/` | Logging and system metrics used by all modules |
| `app.py` | Flask + SocketIO wiring, all REST routes |

## v8 Features

- Global Transcode panel: codec, resolution (720p–4K), FPS, bitrate slider
- Per-channel Re-Transcode from saved original
- Real-time transcode progress + ETA countdown
- UDP/RTP multicast, 40 channels, auto IP/port assignment
- `/proc`-based CPU, RAM, and NIC metrics (eventlet-safe)
- JSON structured log with live log viewer
