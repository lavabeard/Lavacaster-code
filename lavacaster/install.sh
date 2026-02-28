#!/bin/bash
# =============================================================================
#  LavaCast 40 v8 — Installer
#  Installs system dependencies, Python venv, and sets up systemd auto-start.
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
# Paths (repo root is one level up from this script)
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
VENV_DIR="$PROJECT_DIR/venv"
LOG_DIR="$PROJECT_DIR/logs"
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
sudo apt install -y python3 python3-pip python3-venv ffmpeg -qq
log "python3, ffmpeg ready."

# ---------------------------------------------------------------------------
header "STEP 3: Project Directories"
mkdir -p "$PROJECT_DIR"/{media/{originals,transcoded},logs,frontend/static/thumbnails}
touch "$LOG_FILE"
log "Directories created."
log "Originals: $PROJECT_DIR/media/originals"
log "Transcoded: $PROJECT_DIR/media/transcoded"

# ---------------------------------------------------------------------------
header "STEP 4: Python Virtual Environment"
python3 -m venv "$VENV_DIR"
source "$VENV_DIR/bin/activate"
pip install --upgrade pip -q
pip install -r "$PROJECT_DIR/requirements.txt" -q
log "Python dependencies installed."

# ---------------------------------------------------------------------------
header "STEP 5: systemd Service"
sudo tee "$SERVICE_FILE" > /dev/null << SVCEOF
[Unit]
Description=LavaCast 40 v8 Multicast Streamer
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$CURRENT_USER
WorkingDirectory=$PROJECT_DIR
ExecStartPre=/bin/bash -c "ip route show | grep -q '239.0.0.0/8' || ip route add 239.0.0.0/8 dev lo"
ExecStart=$PYTHON_BIN $PROJECT_DIR/app.py
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
header "STEP 6: @reboot Cron Fallback"
CRON="@reboot sleep 20 && bash $PROJECT_DIR/scripts/boot_launch.sh >> $LOG_FILE 2>&1"
if crontab -l 2>/dev/null | grep -q "lavacast40"; then
  warn "Cron entry exists — skipping."
else
  (crontab -l 2>/dev/null; echo "$CRON") | crontab -
  log "@reboot cron installed."
fi

# ---------------------------------------------------------------------------
header "STEP 7: Multicast Route (live)"
ip route show | grep -q "239.0.0.0/8" || \
  sudo ip route add 239.0.0.0/8 dev lo 2>/dev/null && \
  log "Multicast route added." || warn "Route may need manual setup (requires root)."

# ---------------------------------------------------------------------------
LOCAL_IP=$(hostname -I | awk '{print $1}')
header "LavaCast 40 v8 — Installation Complete"
echo ""
echo -e "  ${GREEN}Web GUI:${NC}         http://$LOCAL_IP:5000"
echo -e "  ${GREEN}Logs:${NC}            $LOG_FILE"
echo -e "  ${GREEN}Launch (manual):${NC} bash $PROJECT_DIR/scripts/launch.sh"
echo ""
read -rp "$(echo -e "${ORANGE}Start now? [y/N]: ${NC}")" go
if [[ "$go" =~ ^[Yy]$ ]]; then
  sudo systemctl start "${SERVICE_NAME}.service"
  sleep 2
  sudo systemctl status "${SERVICE_NAME}.service" --no-pager -l || true
  echo -e "\n  ${GREEN}Running → http://$LOCAL_IP:5000${NC}\n"
fi
