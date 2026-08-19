# Lite-Recorder

Self-contained multi-camera field recorder for a Radxa Rock 5B+
(RK3588). Power it on, it raises its own Wi-Fi access point, and a
phone or laptop can connect and control it from a browser — no
existing network required. Footage is saved locally as plain MP4
files per camera, ready to be pulled off and processed back at the
office.

## Features

- Up to 8 cameras: any mix of onboard MIPI CSI (up to 2) and USB
  webcams, auto-discovered — nothing hardcoded.
- Live MJPEG preview grid of every connected camera.
- Per-camera label, resolution, framerate, bitrate, and
  enable/disable-for-recording controls.
- Start/Stop recording with a live elapsed timer; each camera records
  to its own H.264 MP4 file (never multiplexed).
- Hardware H.264 encoding (RK3588 `h264_rkmpp`) with automatic,
  **visibly reported** fallback to software encoding if unavailable.
- Gallery tab to browse and play back past recordings directly in the
  browser, grouped by take.
- Recordings are also just ordinary files/folders on disk — browsable
  and playable outside the app (SSH, SFTP, a mounted drive) with no
  proprietary format.
- No storage cleanup, rotation, or auto-delete — everything is kept.

## How it works

Wi-Fi AP (`hostapd` + `dnsmasq`) → static IP `192.168.4.1` → FastAPI
web app. `ffmpeg` does all capture/encoding; each camera has exactly
one ffmpeg process (a V4L2 device can only be opened once) that tees
its output to both a downscaled MJPEG preview and, while recording,
the MP4 file — so live preview keeps working during a take.

## Quick start (development / testing, no camera hardware needed)

```
python3 -m venv venv
source venv/bin/activate
pip install -r requirements-dev.txt
python -m lite_recorder --simulate --port 8080
```

Open `http://localhost:8080`. `--simulate` substitutes synthetic
test-pattern cameras for real V4L2 devices, so the full UI — preview,
recording, gallery, playback — can be exercised on any machine with
`ffmpeg` installed.

Run the test suite with `pytest` (requires `ffmpeg` on `PATH`).

## Installing on the Rock 5B+

```
sudo ./scripts/install.sh
```

This installs `ffmpeg`, `hostapd`, `dnsmasq`, `v4l-utils`; creates an
unprivileged `lite-recorder` service user; sets up a Python
virtualenv under `/opt/lite-recorder`; and installs+enables two
systemd units:

- `lite-recorder-ap.service` — brings up the Wi-Fi AP interface and
  renders `hostapd`/`dnsmasq` config from `/etc/lite-recorder/ap.env`.
- `lite-recorder.service` — runs the web app on port 80.

**Before first start**, edit `/etc/lite-recorder/ap.env`:

```
sudo nano /etc/lite-recorder/ap.env
sudo systemctl start lite-recorder-ap.service lite-recorder.service
```

or just `sudo reboot` — both are enabled on boot.

### Picking the right `WIFI_IFACE`

This is the one setting that reliably needs adjusting per-board:

- List interfaces: `ls /sys/class/net` or `iw dev`.
- Onboard Rock 5B+ Wi-Fi is commonly `wlan0`. A USB Wi-Fi dongle
  typically shows up as `wlan1` (or `wlxAABBCCDDEEFF` if predictable
  naming is enabled).
- Confirm AP-mode support: `iw list | grep -A8 "Supported interface modes"`
  should list `AP`. **Some onboard RK3588 Wi-Fi chipsets have weak or
  missing AP-mode driver support** — if `hostapd` fails to start
  (`sudo systemctl status hostapd.service`), the most reliable fix is
  a known-good USB Wi-Fi dongle instead of the onboard radio.
- `COUNTRY` in `ap.env` sets the wireless regulatory domain (2-letter
  ISO code) — required for legal channel/power selection, particularly
  if you ever move off the default 2.4 GHz channel 6.

### Enabling the CSI cameras

MIPI CSI sensors need their device-tree overlay enabled before
`/dev/videoN` nodes for them appear at all — use `rsetup` (Radxa's
config tool) to enable the camera overlay for your specific sensor,
then reboot. Once enabled, discovery picks them up the same generic
way as any USB camera; no code changes needed. If a CSI sensor still
doesn't produce a capture-capable `/dev/video*` node, check
`media-ctl -p` to confirm its capture pipeline is linked.

## Storage layout

```
/var/lib/lite-recorder/recordings/
  <DD-MM-YYYY>/
    <HH-MM-SS>/           one folder per take (per Start/Stop press)
      front-door.mp4
      back-yard.mp4
      session.json        per-camera device/resolution/fps/encoder + timestamps
```

Every camera in a take writes its own H.264 MP4 (standard container,
no muxing). Override the root with `LITE_RECORDER_RECORDINGS_ROOT` in
`/etc/lite-recorder/app.env` — point it at an external SSD if the
onboard storage is too small. Nothing is ever auto-deleted.

## Configuration reference

- `/etc/lite-recorder/ap.env` — Wi-Fi AP: interface, SSID, passphrase,
  channel, country, static IP/DHCP range. See
  `config/ap.env.example`.
- `/etc/lite-recorder/app.env` — app: recordings root, state dir,
  host/port, `LITE_RECORDER_FORCE_ENCODER` (pin a specific encoder).
  See `config/lite-recorder.env.example`.
- `/var/lib/lite-recorder/cameras.json` — persisted per-camera
  label/resolution/fps/bitrate/enabled settings (managed by the UI).

## Encoder fallback

On startup the app probes for `h264_rkmpp` (RK3588 hardware encoder),
falling back to `h264_v4l2m2m`, then software `libx264` — and
validates the chosen encoder with a real test encode, so a
present-but-broken hardware path doesn't silently break every
recording. If software encoding is in use, the web UI shows a
persistent banner explaining why and warning that concurrent-camera
capacity is reduced. Check `GET /api/system` for the current encoder
status, or set `LITE_RECORDER_FORCE_ENCODER=libx264` to test the
degraded path deliberately.

## Repository layout

```
lite_recorder/     application package (discovery, encoding, camera
                    process management, recording sessions, web API/UI)
scripts/            install.sh, ap-up.sh, ap-down.sh
systemd/            lite-recorder.service, lite-recorder-ap.service
config/             hostapd/dnsmasq templates, .env.example files
tests/              pytest suite
```
