#!/bin/bash
# Installs Lite-Recorder as a systemd-managed appliance on a Radxa
# Rock 5B+ (or any Debian/Ubuntu-based board). Run as root:
#   sudo ./scripts/install.sh
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "install.sh must be run as root (sudo ./scripts/install.sh)" >&2
  exit 1
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_DIR=/opt/lite-recorder

echo "==> Installing system packages"
apt-get update
apt-get install -y --no-install-recommends \
  ffmpeg hostapd dnsmasq v4l-utils python3-venv python3-pip iproute2 rfkill

# hostapd/dnsmasq ship disabled by default on Debian/Ubuntu; we drive
# them via our own oneshot unit + generated configs.
systemctl unmask hostapd.service dnsmasq.service 2>/dev/null || true
systemctl disable hostapd.service dnsmasq.service 2>/dev/null || true
sed -i 's|^DAEMON_CONF=.*|DAEMON_CONF="/etc/lite-recorder/hostapd.conf"|' /etc/default/hostapd 2>/dev/null || \
  echo 'DAEMON_CONF="/etc/lite-recorder/hostapd.conf"' >> /etc/default/hostapd

echo "==> Creating lite-recorder user"
id -u lite-recorder &>/dev/null || useradd --system --home "$INSTALL_DIR" --shell /usr/sbin/nologin lite-recorder
usermod -aG video lite-recorder

echo "==> Copying application to $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
rsync -a --delete \
  --exclude venv --exclude __pycache__ --exclude '*.pyc' --exclude .git \
  "$REPO_DIR"/ "$INSTALL_DIR"/

echo "==> Creating Python virtualenv"
python3 -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install --upgrade pip
"$INSTALL_DIR/venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt"

echo "==> Writing config"
mkdir -p /etc/lite-recorder
[[ -f /etc/lite-recorder/ap.env ]] || cp "$INSTALL_DIR/config/ap.env.example" /etc/lite-recorder/ap.env
[[ -f /etc/lite-recorder/app.env ]] || cp "$INSTALL_DIR/config/lite-recorder.env.example" /etc/lite-recorder/app.env

echo "==> Creating recordings/state directories"
mkdir -p /var/lib/lite-recorder/recordings
chown -R lite-recorder:lite-recorder /var/lib/lite-recorder

chown -R root:root "$INSTALL_DIR"
chmod +x "$INSTALL_DIR/scripts/"*.sh

echo "==> Installing systemd units"
cp "$INSTALL_DIR/systemd/lite-recorder-ap.service" /etc/systemd/system/
cp "$INSTALL_DIR/systemd/lite-recorder.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable lite-recorder-ap.service lite-recorder.service

cat <<MSG

Install complete.

Before starting, edit /etc/lite-recorder/ap.env - in particular
WIFI_IFACE (see the README for how to pick the right interface on your
board), SSID, and PASSPHRASE.

Then start everything with:
  sudo systemctl start lite-recorder-ap.service
  sudo systemctl start lite-recorder.service

Or reboot; both are enabled to start on boot.
MSG
