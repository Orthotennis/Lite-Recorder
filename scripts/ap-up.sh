#!/bin/bash
# Brings up the Lite-Recorder Wi-Fi access point interface and renders
# hostapd/dnsmasq config from templates. Run as root by
# systemd/lite-recorder-ap.service before hostapd.service/dnsmasq.service.
set -euo pipefail

ENV_FILE="${LITE_RECORDER_AP_ENV:-/etc/lite-recorder/ap.env}"
TEMPLATE_DIR="${LITE_RECORDER_TEMPLATE_DIR:-/opt/lite-recorder/config}"
OUT_DIR=/etc/lite-recorder

if [[ ! -f "$ENV_FILE" ]]; then
  echo "ap-up.sh: missing $ENV_FILE (copy config/ap.env.example there first)" >&2
  exit 1
fi
# shellcheck disable=SC1090
source "$ENV_FILE"

: "${WIFI_IFACE:?WIFI_IFACE must be set in $ENV_FILE}"
: "${SSID:?SSID must be set in $ENV_FILE}"
: "${PASSPHRASE:?PASSPHRASE must be set in $ENV_FILE}"
: "${CHANNEL:=6}"
: "${COUNTRY:=US}"
: "${AP_ADDR:=192.168.4.1}"
: "${AP_NETMASK:=255.255.255.0}"
: "${AP_DHCP_RANGE_START:=192.168.4.10}"
: "${AP_DHCP_RANGE_END:=192.168.4.100}"

mkdir -p "$OUT_DIR"

render() {
  local template="$1" out="$2"
  sed \
    -e "s|__WIFI_IFACE__|${WIFI_IFACE}|g" \
    -e "s|__SSID__|${SSID}|g" \
    -e "s|__PASSPHRASE__|${PASSPHRASE}|g" \
    -e "s|__CHANNEL__|${CHANNEL}|g" \
    -e "s|__COUNTRY__|${COUNTRY}|g" \
    -e "s|__AP_ADDR__|${AP_ADDR}|g" \
    -e "s|__AP_NETMASK__|${AP_NETMASK}|g" \
    -e "s|__AP_DHCP_RANGE_START__|${AP_DHCP_RANGE_START}|g" \
    -e "s|__AP_DHCP_RANGE_END__|${AP_DHCP_RANGE_END}|g" \
    "$template" > "$out"
}

render "$TEMPLATE_DIR/hostapd.conf.template" "$OUT_DIR/hostapd.conf"
render "$TEMPLATE_DIR/dnsmasq.conf.template" /etc/dnsmasq.d/lite-recorder.conf

# Debian/Ubuntu's hostapd.service ships with
# ConditionFileNotEmpty=/etc/hostapd/hostapd.conf hardcoded into the unit
# (so it can no-op instead of failing for users who never configured it).
# That condition is checked against the hardcoded default path, not the
# DAEMON_CONF override in /etc/default/hostapd, so without this symlink
# hostapd.service silently skips ("Condition check failed") even though
# DAEMON_CONF correctly points at our rendered config.
mkdir -p /etc/hostapd
ln -sf "$OUT_DIR/hostapd.conf" /etc/hostapd/hostapd.conf

echo "ap-up.sh: unblocking rfkill and preparing $WIFI_IFACE"
rfkill unblock wifi || true

# Take the interface away from NetworkManager (if present) so it doesn't
# fight hostapd for control of it.
if command -v nmcli >/dev/null 2>&1; then
  nmcli device set "$WIFI_IFACE" managed no || true
fi

ip link set "$WIFI_IFACE" down || true
ip addr flush dev "$WIFI_IFACE" || true
ip addr add "${AP_ADDR}/24" dev "$WIFI_IFACE"
ip link set "$WIFI_IFACE" up

echo "ap-up.sh: $WIFI_IFACE configured with $AP_ADDR, SSID '$SSID'"
