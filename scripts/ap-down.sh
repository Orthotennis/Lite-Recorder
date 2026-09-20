#!/bin/bash
# Tears down the Lite-Recorder access point (used on stop/uninstall).
set -euo pipefail

ENV_FILE="${LITE_RECORDER_AP_ENV:-/etc/lite-recorder/ap.env}"
if [[ -f "$ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  source "$ENV_FILE"
fi
WIFI_IFACE="${WIFI_IFACE:-wlan0}"

systemctl stop hostapd.service dnsmasq.service 2>/dev/null || true
ip addr flush dev "$WIFI_IFACE" 2>/dev/null || true
ip link set "$WIFI_IFACE" down 2>/dev/null || true

rm -f /etc/NetworkManager/conf.d/lite-recorder.conf
if command -v nmcli >/dev/null 2>&1; then
  if systemctl is-active --quiet NetworkManager.service 2>/dev/null; then
    nmcli general reload conf 2>/dev/null || systemctl reload-or-restart NetworkManager.service 2>/dev/null || true
  fi
  nmcli device set "$WIFI_IFACE" managed yes || true
fi
echo "ap-down.sh: $WIFI_IFACE torn down"
