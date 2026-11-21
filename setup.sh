#!/usr/bin/env bash
# ============================================================================
# Aircraft Alert Telegram Bot — Automated Linux VM Setup Script
# ============================================================================
# This script sets up a complete single-VM Linux deployment:
#   1. System packages & build prerequisites
#   2. Python 3 (3.11 / 3.12) with venv support
#   3. MongoDB 7.0 Community Edition (enabled & running)
#   4. Python Virtual Environment + dependencies
#   5. Environment configuration (.env setup)
#   6. Automated Systemd Services installation (bot & worker)
#   7. Enables and starts both services in the background
#   8. Health verification
#
# Supported OS:
#   - Ubuntu 24.04 LTS (noble)
#   - Ubuntu 22.04 LTS (jammy)
#   - Ubuntu 20.04 LTS (focal)
#   - Debian 12 (bookworm) / 11 (bullseye)
#   - Architectures: x86_64 (amd64) and aarch64 (arm64, e.g. Oracle Ampere)
# ============================================================================

set -euo pipefail

# ── Color Definitions ───────────────────────────────────────────────────────
BOLD='\033[1m'
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# ── Check Platform ─────────────────────────────────────────────────────────
if [ "$(uname -s)" != "Linux" ]; then
    echo -e "${RED}Error: This setup script is designed specifically for Linux.${NC}"
    exit 1
fi

# ── Ensure Root Privileges ─────────────────────────────────────────────────
if [ "${EUID}" -ne 0 ]; then
    echo -e "${YELLOW}Notice: Root privileges are required. Re-running with sudo...${NC}"
    exec sudo bash "$0" "$@"
fi

# ── Determine Execution Context ────────────────────────────────────────────
# Find real non-root user who invoked sudo (or fallback to current user)
ACTUAL_USER="${SUDO_USER:-$(whoami)}"
if [ "$ACTUAL_USER" = "root" ]; then
    # Try to find a standard non-root user (e.g. ubuntu, debian)
    for u in ubuntu debian ec2-user admin; do
        if id "$u" &>/dev/null; then
            ACTUAL_USER="$u"
            break
        fi
    done
fi
ACTUAL_GROUP=$(id -gn "$ACTUAL_USER")

# Directory of this script (defaults to the cloned repository root)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# If invoked from deploy/ subdirectory, locate parent directory
if [ -f "$SCRIPT_DIR/../worker.py" ] && [ -d "$SCRIPT_DIR/../app" ]; then
    APP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
else
    APP_DIR="$SCRIPT_DIR"
fi

echo -e "${CYAN}============================================================${NC}"
echo -e "${CYAN}${BOLD}  ✈️  Aircraft Alert Telegram Bot — Single-VM Linux Setup  ${NC}"
echo -e "${CYAN}============================================================${NC}"
echo -e "  Installation Directory : ${BOLD}${APP_DIR}${NC}"
echo -e "  Service User           : ${BOLD}${ACTUAL_USER}:${ACTUAL_GROUP}${NC}"
echo -e "  Architecture           : ${BOLD}$(uname -m)${NC}"
echo -e "${CYAN}------------------------------------------------------------${NC}"
echo ""

# ── Detect Linux Distribution ──────────────────────────────────────────────
DISTRO_ID="ubuntu"
CODENAME="jammy"
if [ -f /etc/os-release ]; then
    # shellcheck source=/dev/null
    . /etc/os-release
    DISTRO_ID="${ID:-ubuntu}"
    CODENAME="${VERSION_CODENAME:-jammy}"
fi
echo -e "${BLUE}[1/7] Detected Distribution: ${DISTRO_ID^} (${CODENAME})${NC}"

# ── 1. Update Packages & Install Tools ─────────────────────────────────────
echo -e "${BLUE}[2/7] Updating apt repositories & installing base tools...${NC}"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
    ca-certificates \
    curl \
    gnupg \
    git \
    build-essential \
    software-properties-common >/dev/null

# ── 2. Install Python 3 & venv ─────────────────────────────────────────────
echo -e "${BLUE}[3/7] Setting up Python 3 with virtual environment support...${NC}"

# Find best Python version available
PYTHON_BIN=""
for candidate in python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
        PYTHON_BIN="$candidate"
        break
    fi
done

# Ensure python3-venv and python3-pip are installed
if [ "$DISTRO_ID" = "ubuntu" ] || [ "$DISTRO_ID" = "debian" ]; then
    if [ "$PYTHON_BIN" = "python3.12" ]; then
        apt-get install -y -qq python3.12 python3.12-venv python3.12-dev python3-pip >/dev/null || true
    elif [ "$PYTHON_BIN" = "python3.11" ]; then
        apt-get install -y -qq python3.11 python3.11-venv python3.11-dev python3-pip >/dev/null || true
    else
        apt-get install -y -qq python3 python3-venv python3-dev python3-pip >/dev/null || true
    fi
fi

PYTHON_VER=$($PYTHON_BIN --version 2>&1 || echo "Unknown")
echo -e "  ${GREEN}✓ Using ${PYTHON_VER} (${PYTHON_BIN})${NC}"

# ── 3. Install & Start MongoDB ─────────────────────────────────────────────
echo -e "${BLUE}[4/7] Checking MongoDB service...${NC}"

if command -v mongod >/dev/null 2>&1; then
    echo -e "  ${GREEN}✓ MongoDB is already installed: $(mongod --version | head -n 1)${NC}"
else
    echo -e "  Installing MongoDB 7.0 Community Edition for ${DISTRO_ID} (${CODENAME})..."
    ARCH="$(dpkg --print-architecture)"
    KEYRING_DIR="/usr/share/keyrings"
    mkdir -p "$KEYRING_DIR"

    # Import official MongoDB GPG key
    curl -fsSL https://www.mongodb.org/static/pgp/server-7.0.asc | \
        gpg --batch --yes --dearmor -o "$KEYRING_DIR/mongodb-server-7.0.gpg"

    # Determine repo URL based on distro
    if [ "$DISTRO_ID" = "debian" ]; then
        REPO_CODENAME="$CODENAME"
        [ "$CODENAME" != "bookworm" ] && [ "$CODENAME" != "bullseye" ] && REPO_CODENAME="bookworm"
        echo "deb [ arch=${ARCH} signed-by=${KEYRING_DIR}/mongodb-server-7.0.gpg ] https://repo.mongodb.org/apt/debian ${REPO_CODENAME}/mongodb-org/7.0 main" | \
            tee /etc/apt/sources.list.d/mongodb-org-7.0.list >/dev/null
    else
        REPO_CODENAME="$CODENAME"
        [ "$CODENAME" != "noble" ] && [ "$CODENAME" != "jammy" ] && [ "$CODENAME" != "focal" ] && REPO_CODENAME="jammy"
        # Ubuntu 24.04 noble or 22.04 jammy
        echo "deb [ arch=${ARCH} signed-by=${KEYRING_DIR}/mongodb-server-7.0.gpg ] https://repo.mongodb.org/apt/ubuntu ${REPO_CODENAME}/mongodb-org/7.0 multiverse" | \
            tee /etc/apt/sources.list.d/mongodb-org-7.0.list >/dev/null
    fi

    apt-get update -qq || true
    if ! apt-get install -y -qq mongodb-org >/dev/null 2>&1; then
        echo -e "  ${YELLOW}Notice: mongodb-org package not directly available for ${CODENAME}; installing standard mongodb package...${NC}"
        apt-get install -y -qq mongodb >/dev/null 2>&1 || true
    fi
fi

# Ensure mongod service is enabled and active
if systemctl list-unit-files | grep -q mongod; then
    systemctl daemon-reload
    systemctl enable mongod >/dev/null 2>&1 || true
    systemctl restart mongod >/dev/null 2>&1 || true
    echo -e "  ${GREEN}✓ mongod service enabled and started.${NC}"
elif systemctl list-unit-files | grep -q mongodb; then
    systemctl daemon-reload
    systemctl enable mongodb >/dev/null 2>&1 || true
    systemctl restart mongodb >/dev/null 2>&1 || true
    echo -e "  ${GREEN}✓ mongodb service enabled and started.${NC}"
else
    echo -e "  ${YELLOW}⚠ MongoDB service not managed by standard systemd unit. Please ensure MongoDB is running.${NC}"
fi

# ── 4. Setup Python Virtual Environment ────────────────────────────────────
echo -e "${BLUE}[5/7] Creating virtual environment & installing dependencies...${NC}"
VENV_DIR="$APP_DIR/venv"

if [ ! -d "$VENV_DIR" ]; then
    sudo -u "$ACTUAL_USER" "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

VENV_PIP="$VENV_DIR/bin/pip"
VENV_PY="$VENV_DIR/bin/python"

# Upgrade pip and install requirements
sudo -u "$ACTUAL_USER" "$VENV_PIP" install --upgrade pip setuptools wheel -q
if [ -f "$APP_DIR/requirements.txt" ]; then
    echo -e "  Installing packages from requirements.txt..."
    sudo -u "$ACTUAL_USER" "$VENV_PIP" install -r "$APP_DIR/requirements.txt" -q
    echo -e "  ${GREEN}✓ Python dependencies successfully installed.${NC}"
else
    echo -e "  ${RED}Error: requirements.txt not found in ${APP_DIR}!${NC}"
    exit 1
fi

# ── 5. Setup Environment File (.env) ───────────────────────────────────────
echo -e "${BLUE}[6/7] Configuring application environment (.env)...${NC}"
ENV_FILE="$APP_DIR/.env"
ENV_EXAMPLE="$APP_DIR/.env.example"

if [ ! -f "$ENV_FILE" ]; then
    if [ -f "$ENV_EXAMPLE" ]; then
        cp "$ENV_EXAMPLE" "$ENV_FILE"
        chown "$ACTUAL_USER:$ACTUAL_GROUP" "$ENV_FILE"
        chmod 600 "$ENV_FILE"
        echo -e "  ${GREEN}✓ Created .env from .env.example${NC}"
    else
        touch "$ENV_FILE"
        chown "$ACTUAL_USER:$ACTUAL_GROUP" "$ENV_FILE"
        chmod 600 "$ENV_FILE"
    fi
fi

# Check if TELEGRAM_BOT_TOKEN is set
CURRENT_TOKEN=$(grep -E "^TELEGRAM_BOT_TOKEN=" "$ENV_FILE" | cut -d '=' -f2- | tr -d ' "' || true)
if [ -z "$CURRENT_TOKEN" ] || [ "$CURRENT_TOKEN" = "your_bot_token_from_botfather" ]; then
    echo ""
    echo -e "  ${YELLOW}------------------------------------------------------------${NC}"
    echo -e "  ${YELLOW}${BOLD}Telegram Bot Token Setup${NC}"
    echo -e "  You need a Telegram Bot Token from @BotFather."
    if [ -t 0 ]; then
        # Interactive prompt
        read -r -p "  Enter your Telegram Bot Token (or press Enter to configure later): " USER_TOKEN
        if [ -n "$USER_TOKEN" ]; then
            if grep -q "^TELEGRAM_BOT_TOKEN=" "$ENV_FILE"; then
                sed -i "s|^TELEGRAM_BOT_TOKEN=.*|TELEGRAM_BOT_TOKEN=${USER_TOKEN}|" "$ENV_FILE"
            else
                echo "TELEGRAM_BOT_TOKEN=${USER_TOKEN}" >> "$ENV_FILE"
            fi
            echo -e "  ${GREEN}✓ Token saved to .env${NC}"
        else
            echo -e "  ${YELLOW}⚠ Remember to add your TELEGRAM_BOT_TOKEN into $ENV_FILE${NC}"
        fi
    else
        echo -e "  ${YELLOW}⚠ Non-interactive shell: Remember to add TELEGRAM_BOT_TOKEN into $ENV_FILE${NC}"
    fi
    echo -e "  ${YELLOW}------------------------------------------------------------${NC}"
    echo ""
fi

# Ensure ownership of all files
chown -R "$ACTUAL_USER:$ACTUAL_GROUP" "$APP_DIR"

# ── 6. Install Systemd Services ────────────────────────────────────────────
echo -e "${BLUE}[7/7] Installing Systemd services & starting background daemon...${NC}"

SYSTEMD_DIR="/etc/systemd/system"
BOT_SVC="aircraft-bot.service"
WORKER_SVC="aircraft-worker.service"
TARGET_SVC="aircraft.target"

# Template generator helper
install_service_unit() {
    local src_file="$1"
    local dest_file="$2"

    sed \
        -e "s|{{USER}}|${ACTUAL_USER}|g" \
        -e "s|{{GROUP}}|${ACTUAL_GROUP}|g" \
        -e "s|{{APP_DIR}}|${APP_DIR}|g" \
        "$src_file" > "$dest_file"
}

# 1. aircraft-bot.service
if [ -f "$APP_DIR/deploy/$BOT_SVC" ]; then
    install_service_unit "$APP_DIR/deploy/$BOT_SVC" "$SYSTEMD_DIR/$BOT_SVC"
else
    cat <<EOF > "$SYSTEMD_DIR/$BOT_SVC"
[Unit]
Description=Aircraft Alert Telegram Bot & Web Server
After=network.target mongod.service
Wants=mongod.service

[Service]
Type=simple
User=${ACTUAL_USER}
Group=${ACTUAL_GROUP}
WorkingDirectory=${APP_DIR}
Environment="PATH=${APP_DIR}/venv/bin:/usr/local/bin:/usr/bin"
EnvironmentFile=${APP_DIR}/.env
ExecStart=${APP_DIR}/venv/bin/python -m app.main
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=aircraft-bot

[Install]
WantedBy=multi-user.target
EOF
fi

# 2. aircraft-worker.service
if [ -f "$APP_DIR/deploy/$WORKER_SVC" ]; then
    install_service_unit "$APP_DIR/deploy/$WORKER_SVC" "$SYSTEMD_DIR/$WORKER_SVC"
else
    cat <<EOF > "$SYSTEMD_DIR/$WORKER_SVC"
[Unit]
Description=Aircraft Alert Monitor Worker
After=network.target mongod.service
Wants=mongod.service

[Service]
Type=simple
User=${ACTUAL_USER}
Group=${ACTUAL_GROUP}
WorkingDirectory=${APP_DIR}
Environment="PATH=${APP_DIR}/venv/bin:/usr/local/bin:/usr/bin"
EnvironmentFile=${APP_DIR}/.env
ExecStart=${APP_DIR}/venv/bin/python worker.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=aircraft-worker

[Install]
WantedBy=multi-user.target
EOF
fi

# 3. aircraft.target
if [ -f "$APP_DIR/deploy/$TARGET_SVC" ]; then
    cp "$APP_DIR/deploy/$TARGET_SVC" "$SYSTEMD_DIR/$TARGET_SVC"
else
    cat <<EOF > "$SYSTEMD_DIR/$TARGET_SVC"
[Unit]
Description=Aircraft Alert System (Bot + Worker)
Wants=aircraft-bot.service aircraft-worker.service

[Install]
WantedBy=multi-user.target
EOF
fi

# Reload systemd daemon
systemctl daemon-reload

# Automatically enable services at boot
systemctl enable aircraft-bot.service aircraft-worker.service aircraft.target >/dev/null 2>&1
echo -e "  ${GREEN}✓ Enabled services at boot: aircraft-bot, aircraft-worker, aircraft.target${NC}"

# Automatically start services in background
echo -e "  Starting background services..."
systemctl restart aircraft-bot.service aircraft-worker.service

# Allow services 2 seconds to initialize
sleep 2

# Verify statuses
echo ""
echo -e "${CYAN}============================================================${NC}"
echo -e "${CYAN}${BOLD}  Deployment Verification & Background Service Status       ${NC}"
echo -e "${CYAN}============================================================${NC}"

BOT_ACTIVE=false
WORKER_ACTIVE=false

if systemctl is-active --quiet aircraft-bot; then
    echo -e "  ${GREEN}● ACTIVE${NC}  aircraft-bot.service    (Web Server & Telegram Bot)"
    BOT_ACTIVE=true
else
    echo -e "  ${RED}○ FAILED${NC}  aircraft-bot.service    (Check: journalctl -u aircraft-bot -n 30)"
fi

if systemctl is-active --quiet aircraft-worker; then
    echo -e "  ${GREEN}● ACTIVE${NC}  aircraft-worker.service (Background 5s Trajectory Monitor)"
    WORKER_ACTIVE=true
else
    echo -e "  ${RED}○ FAILED${NC}  aircraft-worker.service (Check: journalctl -u aircraft-worker -n 30)"
fi

# Test local HTTP health endpoint
echo ""
echo -e "  ${BLUE}Testing local health endpoint:${NC}"
HTTP_RESPONSE=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/ 2>/dev/null || echo "000")
if [ "$HTTP_RESPONSE" = "200" ]; then
    echo -e "  ${GREEN}✓ HTTP GET http://localhost:8000/ returned 200 OK${NC}"
    HEALTH_JSON=$(curl -s http://localhost:8000/health 2>/dev/null || echo "{}")
    echo -e "  ${CYAN}Health status: ${HEALTH_JSON}${NC}"
else
    echo -e "  ${YELLOW}⚠ Port 8000 did not respond immediately (HTTP status: ${HTTP_RESPONSE}).${NC}"
    echo -e "    Check logs: journalctl -u aircraft-bot -n 20"
fi

echo ""
echo -e "${GREEN}============================================================${NC}"
echo -e "${GREEN}${BOLD}  🎉 Installation and Background Systemd Setup Complete!   ${NC}"
echo -e "${GREEN}============================================================${NC}"
echo -e "  Everything is now running in the background as systemd services on this VM."
echo ""
echo -e "${BOLD}Management Commands:${NC}"
echo -e "  • Check status : ${CYAN}./manage.sh status${NC}  (or sudo systemctl status aircraft-bot aircraft-worker)"
echo -e "  • Restart both : ${CYAN}./manage.sh restart${NC} (or sudo systemctl restart aircraft.target)"
echo -e "  • Live logs    : ${CYAN}./manage.sh logs${NC}    (or sudo journalctl -u aircraft-bot -u aircraft-worker -f)"
echo -e "  • Run tests    : ${CYAN}./manage.sh test${NC}"
echo ""
echo -e "${BOLD}Web Dashboard:${NC}"
echo -e "  • Admin panel  : ${CYAN}http://<your-vm-ip>:8000/admin${NC}"
echo -e "  • Health check : ${CYAN}http://<your-vm-ip>:8000/health${NC}"
echo -e "${GREEN}============================================================${NC}"
echo ""
