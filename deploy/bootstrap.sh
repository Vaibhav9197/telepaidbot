#!/usr/bin/env bash
#
# Prepares a fresh Oracle Cloud Ampere A1 instance (Ubuntu 22.04/24.04, arm64)
# to run the bot, and installs it as a systemd service.
#
#   curl -fsSL https://raw.githubusercontent.com/Vaibhav9197/telepaidbot/main/deploy/bootstrap.sh | bash
#
# or, from a checkout:  bash deploy/bootstrap.sh
#
# Safe to re-run: every step checks before acting, and an existing config.env is
# never overwritten.

set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/Vaibhav9197/telepaidbot.git}"
APP_DIR="${APP_DIR:-/opt/rcdl}"
DATA_DIR="${DATA_DIR:-/var/lib/rcdl/tmp}"
TZ_NAME="${TZ_NAME:-Asia/Kolkata}"

say() { printf '\n\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m warning:\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m error:\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] && die "run this as a normal user with sudo, not as root"
sudo -v >/dev/null 2>&1 || die "this user needs sudo"

arch="$(uname -m)"
[[ $arch == aarch64 || $arch == arm64 || $arch == x86_64 ]] ||
  die "unsupported architecture: $arch"

say "Setting timezone to $TZ_NAME"
sudo timedatectl set-timezone "$TZ_NAME"

say "Installing base packages"
sudo apt-get update -qq
sudo apt-get install -y -qq ca-certificates curl git gnupg

if ! command -v docker >/dev/null 2>&1; then
  say "Installing Docker from the official repository"
  sudo install -m 0755 -d /etc/apt/keyrings
  # The keyring is what lets apt verify the repo; without it apt refuses it.
  sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
    -o /etc/apt/keyrings/docker.asc
  sudo chmod a+r /etc/apt/keyrings/docker.asc
  # dpkg's arch name (arm64) is what Docker's repo is indexed by, not uname's.
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" |
    sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
  sudo apt-get update -qq
  sudo apt-get install -y -qq docker-ce docker-ce-cli containerd.io \
    docker-buildx-plugin docker-compose-plugin
else
  say "Docker already installed ($(docker --version))"
fi

sudo systemctl enable --now docker

if ! id -nG "$USER" | tr ' ' '\n' | grep -qx docker; then
  say "Adding $USER to the docker group"
  sudo usermod -aG docker "$USER"
  NEEDS_RELOGIN=1
fi

say "Creating $DATA_DIR for in-flight downloads"
sudo mkdir -p "$DATA_DIR"
# The container runs as root, so this only has to exist and be writable by it.
sudo chmod 0777 "$DATA_DIR"

if [[ -d $APP_DIR/.git ]]; then
  say "Updating existing checkout at $APP_DIR"
  sudo git -C "$APP_DIR" pull --ff-only
else
  say "Cloning $REPO_URL into $APP_DIR"
  sudo mkdir -p "$(dirname "$APP_DIR")"
  sudo git clone --depth 1 "$REPO_URL" "$APP_DIR"
fi
sudo chown -R "$USER":"$USER" "$APP_DIR"

if [[ -f $APP_DIR/config.env ]]; then
  say "config.env already present, leaving it alone"
  CONFIG_READY=1
else
  say "Scaffolding config.env from the Oracle template"
  cp "$APP_DIR/deploy/config.env.oracle" "$APP_DIR/config.env"
  chmod 600 "$APP_DIR/config.env"
  CONFIG_READY=0
fi

say "Installing the systemd unit"
sudo cp "$APP_DIR/deploy/rcdl.service" /etc/systemd/system/rcdl.service
sudo systemctl daemon-reload
sudo systemctl enable rcdl.service >/dev/null

echo
if [[ ${CONFIG_READY:-0} -eq 0 ]]; then
  cat <<EOF
Setup done, but the bot is NOT started yet -- it has no credentials.

  1. Fill in the four secrets:   nano $APP_DIR/config.env
     (API_ID, API_HASH, BOT_TOKEN, SESSION_STRING)
  2. Start it:                   sudo systemctl start rcdl
  3. Watch the first build:      docker compose -f $APP_DIR/docker-compose.yml \\
                                   -f $APP_DIR/deploy/docker-compose.vps.yml logs -f
EOF
else
  say "Starting the bot"
  sudo systemctl restart rcdl
  echo "Running. Check it with: systemctl status rcdl"
fi

if [[ ${NEEDS_RELOGIN:-0} -eq 1 ]]; then
  echo
  warn "log out and back in before running docker without sudo"
fi
