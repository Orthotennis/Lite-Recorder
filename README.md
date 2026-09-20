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
one ffmpeg process (a capture device can only be opened once) that
tees its output to both a downscaled MJPEG preview and, while
recording, the MP4 file — so live preview keeps working during a take.
Cameras are discovered through the platform's native capture API: V4L2
on Linux (the board, and Linux dev machines), DirectShow on Windows.

## Quick start (run locally, no install needed)

```
./scripts/run-local.sh
```

Open `http://127.0.0.1:8080`. The script creates a virtualenv in
`./.venv`, installs the Python dependencies, and **records from
whatever real cameras are attached** — a USB webcam, a laptop's
built-in camera, a MIPI CSI sensor — exactly as it does on the board.
Recordings and state are kept under `./.local/` inside the repo;
nothing is written to `/opt`, `/etc` or `/var`. Delete `.venv` and
`.local` to clean up.

If no camera is found, it falls back to synthetic test patterns so the
UI can still be exercised, and says so both on the console and in a
banner in the web UI — a run never *looks* like real footage when it
isn't.

Camera options:

- `--real` — real cameras only. No camera means no cameras in the UI
  and a clear error when you press Record, instead of a silent fallback.
- `--simulate` — synthetic test patterns only, no camera needed.
- `--camera-mode auto|real|simulate` — the long form of the above
  (`auto` is the default).

Also `--port N` and `--host H`; any other arguments are passed through
to `python -m lite_recorder`.

**If you get test patterns when you expected your webcam**, the console
output names the reason. On Linux/WSL, the usual ones:

- No `/dev/video*` devices at all — nothing is plugged in, or (on WSL)
  the webcam has not been attached to the Linux VM with `usbipd`.
- Permission denied — add yourself to the `video` group with
  `sudo usermod -aG video $USER`, then log out and back in.
- The nodes exist but none is capture-capable — some devices expose
  metadata-only nodes; check `v4l2-ctl --list-devices`.

On Windows (native, via DirectShow):

- No DirectShow video devices found — nothing is plugged in, or a
  laptop's built-in camera is disabled in Device Manager.
- A device is listed but reports no usable formats — check Windows
  Settings → Privacy & security → Camera and allow desktop apps
  access, and make sure no other app (Teams, OBS, the Camera app...)
  already has it open.

**Windows note:** run `scripts/run-local.sh` from Git Bash (not
PowerShell/cmd — it's a bash script). Cameras are picked up natively
through DirectShow, the same way OBS or Teams finds them; no WSL, no
`usbipd`, no extra setup. WSL also works, but since WSL's Linux kernel
has no native USB camera support, real capture there needs `usbipd`
*and* a custom WSL kernel with the USB video class driver built in —
running from Git Bash instead is far less work for the same result.

If the script fails with `: invalid option name: set: pipefail` (or
`bash\r: bad interpreter`), the files were checked out with CRLF line
endings by Git for Windows (`core.autocrlf=true`). `.gitattributes` now
forces LF, so a fresh clone is fine; to fix an existing clone, run from
the repo root:

```
git rm -r --cached -q . && git reset --hard
```

Under WSL specifically, running the app from the Linux filesystem
(e.g. `~/Lite-Recorder`) rather than `/mnt/c/...` also avoids this and
is much faster.

Or by hand:

```
python3 -m venv venv
source venv/bin/activate
pip install -r requirements-dev.txt
python -m lite_recorder --port 8080          # real cameras, sim fallback
python -m lite_recorder --real --port 8080  # real cameras only
```

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

### Uninstalling

```
sudo ./scripts/uninstall.sh
```

Stops and disables both services, removes the systemd unit files, and
deletes the installed app (`/opt/lite-recorder`) and the service user.
By default it **keeps** your config (`/etc/lite-recorder`) and your
recordings/app state (`/var/lib/lite-recorder`) — nothing you've
recorded is touched. Add flags to remove more:

- `--purge-config` — also delete `/etc/lite-recorder` (AP/app config).
- `--purge-data` — also delete `/var/lib/lite-recorder`, **including
  all recordings**. Prompts for confirmation unless `-y` is given.
- `--purge-packages` — also remove `ffmpeg`, `hostapd`, `dnsmasq`,
  `v4l-utils` (skipped by default in case other things on the board
  use them).
- `-y` / `--yes` — skip the `--purge-data` confirmation prompt.

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
  host/port, `LITE_RECORDER_CAMERA_MODE` (`real` on the board, so a
  missing camera is never quietly replaced by a test pattern),
  `LITE_RECORDER_FORCE_ENCODER` (pin a specific encoder). See
  `config/lite-recorder.env.example`.
- `/var/lib/lite-recorder/cameras.json` — persisted per-camera
  label/resolution/fps/bitrate/enabled settings (managed by the UI).

## Encoder fallback

On startup the app probes for a hardware H.264 encoder — `h264_rkmpp`
(RK3588) then `h264_v4l2m2m` on Linux, or `h264_nvenc` (NVIDIA) then
`h264_qsv` (Intel Quick Sync) then `h264_amf` (AMD) on Windows —
falling back to software `libx264` if none work. It validates the
chosen encoder with a real test encode, so a present-but-broken
hardware path doesn't silently break every recording. If software
encoding is in use, the web UI shows a persistent banner explaining
why and warning that concurrent-camera capacity is reduced. Check
`GET /api/system` for the current encoder status, or set
`LITE_RECORDER_FORCE_ENCODER=libx264` to test the degraded path
deliberately.

## Repository layout

```
lite_recorder/     application package (discovery, encoding, camera
                    process management, recording sessions, web API/UI)
scripts/            run-local.sh, install.sh, uninstall.sh, ap-up.sh, ap-down.sh
systemd/            lite-recorder.service, lite-recorder-ap.service
config/             hostapd/dnsmasq templates, .env.example files
tests/              pytest suite
```
