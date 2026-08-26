#!/bin/bash
# Removes the Lite-Recorder systemd services, app, and (optionally)
# its config/data. Run as root:
#   sudo ./scripts/uninstall.sh [--purge-config] [--purge-data] [--purge-packages] [-y]
#
# By default this keeps /etc/lite-recorder (your AP/app config) and
# /var/lib/lite-recorder/recordings (your footage) untouched, and
# leaves system packages (ffmpeg, hostapd, dnsmasq, ...) installed
# since other things on the board may depend on them.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "uninstall.sh must be run as root (sudo ./scripts/uninstall.sh)" >&2
  exit 1
fi

INSTALL_DIR=/opt/lite-recorder
CONFIG_DIR=/etc/lite-recorder
STATE_DIR=/var/lib/lite-recorder

PURGE_CONFIG=0
PURGE_DATA=0
PURGE_PACKAGES=0
ASSUME_YES=0

for arg in "$@"; do
  case "$arg" in
    --purge-config) PURGE_CONFIG=1 ;;
    --purge-data) PURGE_DATA=1 ;;
    --purge-packages) PURGE_PACKAGES=1 ;;
    -y|--yes) ASSUME_YES=1 ;;
    -h|--help)
      sed -n '2,9p' "${BASH_SOURCE[0]}" | sed 's/^# \?//'
      exit 0
      ;;
    *)
      echo "unknown option: $arg" >&2
      exit 1
      ;;
  esac
done

if [[ $PURGE_DATA -eq 1 && $ASSUME_YES -ne 1 ]]; then
  read -r -p "This will permanently delete all recordings in $STATE_DIR/recordings. Continue? [y/N] " reply
  [[ "$reply" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 1; }
fi

echo "==> Stopping services"
systemctl stop lite-recorder.service 2>/dev/null || true
systemctl stop hostapd.service dnsmasq.service 2>/dev/null || true
systemctl stop lite-recorder-ap.service 2>/dev/null || true

echo "==> Disabling services"
systemctl disable lite-recorder.service lite-recorder-ap.service 2>/dev/null || true

echo "==> Removing systemd unit files"
rm -f /etc/systemd/system/lite-recorder.service
rm -f /etc/systemd/system/lite-recorder-ap.service
systemctl daemon-reload 2>/dev/null || true
systemctl reset-failed 2>/dev/null || true

if [[ -f "$CONFIG_DIR/ap.env" ]]; then
  echo "==> Releasing Wi-Fi interface back to NetworkManager (if present)"
  # shellcheck disable=SC1090
  WIFI_IFACE=$(source "$CONFIG_DIR/ap.env" 2>/dev/null && echo "$WIFI_IFACE" || true)
  if [[ -n "${WIFI_IFACE:-}" ]] && command -v nmcli >/dev/null 2>&1; then
    nmcli device set "$WIFI_IFACE" managed yes 2>/dev/null || true
  fi
fi

echo "==> Removing installed application ($INSTALL_DIR)"
rm -rf "$INSTALL_DIR"

if [[ $PURGE_CONFIG -eq 1 ]]; then
  echo "==> Removing config ($CONFIG_DIR)"
  rm -rf "$CONFIG_DIR"
  rm -f /etc/dnsmasq.d/lite-recorder.conf
  [[ -L /etc/hostapd/hostapd.conf ]] && rm -f /etc/hostapd/hostapd.conf
else
  echo "==> Keeping config in $CONFIG_DIR (use --purge-config to remove)"
fi

if [[ $PURGE_DATA -eq 1 ]]; then
  echo "==> Removing recordings and app state ($STATE_DIR)"
  rm -rf "$STATE_DIR"
else
  echo "==> Keeping recordings/state in $STATE_DIR (use --purge-data to remove)"
fi

if id -u lite-recorder &>/dev/null; then
  echo "==> Removing lite-recorder service user"
  userdel lite-recorder 2>/dev/null || true
fi

if [[ $PURGE_PACKAGES -eq 1 ]]; then
  echo "==> Removing system packages (ffmpeg hostapd dnsmasq v4l-utils)"
  apt-get remove -y ffmpeg hostapd dnsmasq v4l-utils || true
  apt-get autoremove -y || true
else
  echo "==> Leaving system packages installed (use --purge-packages to remove)"
fi

cat <<MSG

Uninstall complete.
MSG
