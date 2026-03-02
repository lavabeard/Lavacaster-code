#!/bin/bash
# =============================================================================
#  LavaCast 40 v8 — One-shot Installer
#  Clones the repo, installs system deps, Python venv, and systemd auto-start.
#
#  Usage:
#    bash <(curl -fsSL https://raw.githubusercontent.com/lavabeard/Lavacaster-code/main/install.sh)
#  or after cloning:
#    bash install.sh
# =============================================================================

set -e

ORANGE='\033[0;33m'; GREEN='\033[0;32m'; RED='\033[0;31m'
BOLD='\033[1m'; NC='\033[0m'

log()    { echo -e "${GREEN}[✔]${NC} $1"; }
warn()   { echo -e "${ORANGE}[!]${NC} $1"; }
fail()   { echo -e "${RED}[✘] ERROR: $1${NC}"; exit 1; }
header() {
  echo -e "\n${ORANGE}${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
  echo -e "${ORANGE}${BOLD}  $1${NC}"
  echo -e "${ORANGE}${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
}

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
REPO_URL="https://github.com/lavabeard/Lavacaster-code.git"
INSTALL_DIR="${LAVACAST_DIR:-$HOME/lavacast40}"
APP_DIR="$INSTALL_DIR"
VENV_DIR="$INSTALL_DIR/venv"
LOG_DIR="$INSTALL_DIR/logs"
LOG_FILE="$LOG_DIR/lavacast40.log"
SERVICE_NAME="lavacast40"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
CURRENT_USER="$(whoami)"
PYTHON_BIN="$VENV_DIR/bin/python3"

# ---------------------------------------------------------------------------
header "STEP 1: System Check"
command -v apt &>/dev/null || fail "Requires Ubuntu/Debian with apt."
ping -c1 -W2 8.8.8.8 &>/dev/null || fail "No internet connection."
log "Ubuntu/Debian OK | User: $CURRENT_USER"

# ---------------------------------------------------------------------------
header "STEP 2: System Dependencies"
sudo apt update -qq
sudo apt install -y git python3 python3-pip python3-venv ffmpeg -qq
log "git, python3, ffmpeg ready."

# ---------------------------------------------------------------------------
header "STEP 3: Install / Update"
DATE=$(date +%Y-%m-%d)
BACKUP_DIR="$HOME/lavacast40-old-$DATE"
if [ -d "$BACKUP_DIR" ]; then
  BACKUP_DIR="${BACKUP_DIR}-$(date +%H%M%S)"
fi

if [ -d "$INSTALL_DIR" ]; then
  echo ""
  warn "Existing LavaCast 40 installation found at $INSTALL_DIR"
  echo -e "  ${ORANGE}This will stop the running service, back up the current install,"
  echo -e "  pull the latest code from the repository, and reinstall dependencies.${NC}"
  echo ""
  read -rp "$(echo -e "${ORANGE}${BOLD}Upgrade existing installation? [y/N]: ${NC}")" _confirm_upgrade
  if [[ ! "$_confirm_upgrade" =~ ^[Yy]$ ]]; then
    echo -e "\n${RED}Upgrade cancelled. Existing installation unchanged.${NC}\n"
    exit 0
  fi

  # Shut down service and wait for it to fully stop before touching files
  warn "Stopping LavaCast 40 service..."
  sudo systemctl stop "${SERVICE_NAME}.service" 2>/dev/null || true
  for _i in {1..15}; do
    sudo systemctl is-active --quiet "${SERVICE_NAME}.service" 2>/dev/null || break
    sleep 1
  done
  # Also kill any stray python processes still running app.py
  pkill -f "$INSTALL_DIR/app.py" 2>/dev/null || true
  sleep 1
  log "Service stopped."

  # Preserve user-editable files before the backup rotation
  if [ -d "$INSTALL_DIR/media" ]; then
    mv "$INSTALL_DIR/media" /tmp/lavacast40_media_save
    log "Media saved temporarily (will be restored after clone)"
  fi
  if [ -f "$INSTALL_DIR/lavacast.config.json" ]; then
    cp "$INSTALL_DIR/lavacast.config.json" /tmp/lavacast40_config_save.json
    log "lavacast.config.json saved temporarily (will be restored after clone)"
  fi
  if [ -f "$INSTALL_DIR/lavacast_channels.json" ]; then
    cp "$INSTALL_DIR/lavacast_channels.json" /tmp/lavacast40_channels_save.json
    log "lavacast_channels.json saved temporarily (will be restored after clone)"
  fi
  # Rotate old install to dated backup folder
  mv "$INSTALL_DIR" "$BACKUP_DIR"
  log "Old install → $BACKUP_DIR"
  # Fresh clone
  git clone "$REPO_URL" "$INSTALL_DIR"
  # Restore preserved files
  if [ -d /tmp/lavacast40_media_save ]; then
    mv /tmp/lavacast40_media_save "$INSTALL_DIR/media"
    log "Media restored → $INSTALL_DIR/media/"
  fi
  if [ -f /tmp/lavacast40_config_save.json ]; then
    mv /tmp/lavacast40_config_save.json "$INSTALL_DIR/lavacast.config.json"
    log "lavacast.config.json restored (your custom settings kept)"
  fi
  if [ -f /tmp/lavacast40_channels_save.json ]; then
    mv /tmp/lavacast40_channels_save.json "$INSTALL_DIR/lavacast_channels.json"
    log "lavacast_channels.json restored (your channel assignments kept)"
  fi
  log "Code updated from repository."
else
  git clone "$REPO_URL" "$INSTALL_DIR"
  log "Repository cloned to $INSTALL_DIR"
fi

# ---------------------------------------------------------------------------
header "STEP 4: Project Directories"
mkdir -p "$APP_DIR"/{media/{originals,transcoded},frontend/static/thumbnails} "$LOG_DIR"
touch "$LOG_FILE"
log "Directories created."
log "Originals:  $APP_DIR/media/originals"
log "Transcoded: $APP_DIR/media/transcoded"

# Write a default lavacast_channels.json if one does not already exist.
# (Upgrades preserve the existing file earlier in Step 3.)
if [ ! -f "$APP_DIR/lavacast_channels.json" ]; then
  cat > "$APP_DIR/lavacast_channels.json" << 'CHANJSON'
{
  "_readme": "LavaCast 40 v8 — channel assignments and runtime settings. Edit manually then restart to apply. Keys starting with '_' are comments; they are ignored on load.",
  "_hint": "Channel IDs are 0-based internally. _label shows the display name (id 0 = CH01, id 39 = CH40).",
  "global_transcode": {
    "_readme": "Default transcode profile applied to uploads and re-transcodes",
    "codec": "h264",
    "preset": "fast",
    "vbitrate": "8M",
    "abitrate": "192k",
    "resolution": "1080p",
    "fps": "original"
  },
  "global_streaming": {
    "_readme": "Streaming output settings — NIC, bitrate cap, media path, auto-start",
    "global_bitrate": "",
    "selected_nic": "",
    "monitor_nic": "",
    "media_path": "~/lavacast40/media",
    "auto_start": false
  },
  "channels": {}
}
CHANJSON
  log "lavacast_channels.json created with defaults."
fi

# ---------------------------------------------------------------------------
header "STEP 5: Python Virtual Environment"
python3 -m venv "$VENV_DIR"
source "$VENV_DIR/bin/activate"
pip install --upgrade pip -q
pip install -r "$APP_DIR/requirements.txt" -q
log "Python dependencies installed."

# ---------------------------------------------------------------------------
header "STEP 6: systemd Service"
sudo tee "$SERVICE_FILE" > /dev/null << SVCEOF
[Unit]
Description=LavaCast 40 v8 Multicast Streamer
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$CURRENT_USER
WorkingDirectory=$APP_DIR
ExecStartPre=+/bin/bash -c "ip route show | grep -q '239.0.0.0/8' || ip route add 239.0.0.0/8 dev lo"
ExecStart=$PYTHON_BIN $APP_DIR/app.py
Restart=on-failure
RestartSec=10
StandardOutput=append:$LOG_FILE
StandardError=append:$LOG_FILE
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
SVCEOF
sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE_NAME}.service"
log "systemd service enabled."

# ---------------------------------------------------------------------------
header "STEP 7: @reboot Cron Fallback"
CRON="@reboot sleep 20 && $VENV_DIR/bin/python3 $APP_DIR/app.py >> $LOG_FILE 2>&1"
if crontab -l 2>/dev/null | grep -q "lavacast40"; then
  warn "Cron entry exists — skipping."
else
  (crontab -l 2>/dev/null; echo "$CRON") | crontab -
  log "@reboot cron installed."
fi

# ---------------------------------------------------------------------------
header "STEP 8: Multicast Route (live)"
ip route show | grep -q "239.0.0.0/8" || \
  sudo ip route add 239.0.0.0/8 dev lo 2>/dev/null && \
  log "Multicast route added." || warn "Route may need manual setup (requires root)."

# ---------------------------------------------------------------------------
LOCAL_IP=$(hostname -I | awk '{print $1}')
header "LavaCast 40 v8 — Installation Complete"
echo ""
echo -e "  ${GREEN}Install dir:${NC}     $INSTALL_DIR"
echo -e "  ${GREEN}Python files:${NC}    $INSTALL_DIR/*.py"
echo -e "  ${GREEN}Logs:${NC}            $LOG_DIR/"
echo -e "  ${GREEN}Media:${NC}           $INSTALL_DIR/media/"
echo -e "  ${GREEN}Web GUI:${NC}         http://$LOCAL_IP:5000"
echo -e "  ${GREEN}Start service:${NC}   sudo systemctl start $SERVICE_NAME"
echo ""
read -rp "$(echo -e "${ORANGE}Start now? [y/N]: ${NC}")" go
if [[ "$go" =~ ^[Yy]$ ]]; then
  sudo systemctl start "${SERVICE_NAME}.service"
  sleep 2
  sudo systemctl status "${SERVICE_NAME}.service" --no-pager -l || true
  echo -e "\n  ${GREEN}Running → http://$LOCAL_IP:5000${NC}\n"
fi
