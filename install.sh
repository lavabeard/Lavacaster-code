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
INSTALL_DIR="${LAVACAST_DIR:-/opt/lavacast40}"
APP_DIR="$INSTALL_DIR/lavacaster"
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
header "STEP 3: Clone / Update Repository"
if [ -d "$INSTALL_DIR/.git" ]; then
  warn "Repo already exists at $INSTALL_DIR — pulling latest changes."
  git -C "$INSTALL_DIR" fetch origin main
  git -C "$INSTALL_DIR" reset --hard origin/main
  log "Repository updated."
elif [ -d "$INSTALL_DIR" ] && [ -f "$APP_DIR/app.py" ]; then
  warn "$INSTALL_DIR exists but is not a git repo — using as-is."
else
  sudo mkdir -p "$(dirname "$INSTALL_DIR")"
  sudo chown "$CURRENT_USER":"$CURRENT_USER" "$(dirname "$INSTALL_DIR")"
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
ExecStartPre=/bin/bash -c "ip route show | grep -q '239.0.0.0/8' || ip route add 239.0.0.0/8 dev lo"
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
echo -e "  ${GREEN}Web GUI:${NC}         http://$LOCAL_IP:5000"
echo -e "  ${GREEN}Logs:${NC}            $LOG_FILE"
echo -e "  ${GREEN}Start service:${NC}   sudo systemctl start $SERVICE_NAME"
echo ""
read -rp "$(echo -e "${ORANGE}Start now? [y/N]: ${NC}")" go
if [[ "$go" =~ ^[Yy]$ ]]; then
  sudo systemctl start "${SERVICE_NAME}.service"
  sleep 2
  sudo systemctl status "${SERVICE_NAME}.service" --no-pager -l || true
  echo -e "\n  ${GREEN}Running → http://$LOCAL_IP:5000${NC}\n"
fi
