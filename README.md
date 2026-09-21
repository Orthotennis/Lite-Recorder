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
the MP4 file — so live preview keeps working during a take. Discovery
resolves `/dev/video*` nodes down to one per *physical* device, since a
single camera commonly exposes several capture nodes that cannot stream
at the same time.

## Quick start (run locally, no install needed)

```
./scripts/run-local.sh
```

Open `http://127.0.0.1:8080`. The script creates a virtualenv in
`./.venv`, installs the Python dependencies, and starts the app with
synthetic test-pattern cameras (`--simulate`), so the full UI —
preview, recording, gallery, playback — can be exercised on any
machine with `python3` and `ffmpeg` installed. Recordings and state
are kept under `./.local/` inside the repo; nothing is written to
`/opt`, `/etc` or `/var`. Delete `.venv` and `.local` to clean up.

Options: `--port N`, `--host H`, `--real` (use actual V4L2 cameras
instead of simulated ones). Any other arguments are passed through to
`python -m lite_recorder`.

**Windows / WSL note:** if the script fails with
`: invalid option name: set: pipefail` (or `bash\r: bad interpreter`),
the files were checked out with CRLF line endings by Git for Windows
(`core.autocrlf=true`). `.gitattributes` now forces LF, so a fresh clone
is fine; to fix an existing clone, run from the repo root:

```
git rm -r --cached -q . && git reset --hard
```

Running the app from the Linux filesystem (e.g. `~/Lite-Recorder`)
rather than `/mnt/c/...` also avoids this and is much faster.

Or by hand:

```
python3 -m venv venv
source venv/bin/activate
pip install -r requirements-dev.txt
python -m lite_recorder --simulate --port 8080
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

### Phone/laptop associates but never gets an IP

If `sudo systemctl status hostapd.service` shows clients authenticating
and associating but the client never gets an address (or
`sudo systemctl status dnsmasq.service` shows `unknown interface
<WIFI_IFACE>` / `FAILED to start up`), `dnsmasq` started before
`hostapd` finished switching the radio into AP mode and raced the
interface. `lite-recorder-ap.service` only orders itself before both
`hostapd.service` and `dnsmasq.service`, not those two relative to each
other, so a fresh install needs the `dnsmasq.service.d/lite-recorder.conf`
drop-in (installed automatically by `install.sh`) that makes `dnsmasq`
wait for `hostapd`. On an existing install missing it:

```
sudo mkdir -p /etc/systemd/system/dnsmasq.service.d
sudo cp /opt/lite-recorder/systemd/dnsmasq.service.d/lite-recorder.conf \
  /etc/systemd/system/dnsmasq.service.d/
sudo systemctl daemon-reload
sudo systemctl restart hostapd.service dnsmasq.service
```

If that alone doesn't fix it and `dnsmasq` still reports `unknown
interface <WIFI_IFACE>` even when run standalone
(`sudo dnsmasq --no-daemon --conf-file=/etc/dnsmasq.d/lite-recorder.conf`)
well after `hostapd` is confirmed running, check whether
NetworkManager still owns the interface:

```
nmcli device status
```

If `WIFI_IFACE` shows as `wifi` / `connected` or `disconnected`
(**managed**) rather than `unmanaged`, NetworkManager is able to grab
and reset the interface out from under `hostapd` after boot. This
happens because `ap-up.sh` runs at `network-pre.target`, before
NetworkManager has necessarily started, so its runtime
`nmcli device set managed no` call can silently fail
(`Could not create NMClient object`) - NetworkManager then takes the
interface once it starts. `ap-up.sh` now also writes a persistent
`/etc/NetworkManager/conf.d/lite-recorder.conf` marking the interface
unmanaged, which isn't racy against NetworkManager's own startup order.
On an existing install missing that fix, apply it manually:

```
sudo tee /etc/NetworkManager/conf.d/lite-recorder.conf <<'EOF'
[keyfile]
unmanaged-devices=interface-name:WIFI_IFACE
EOF
sudo systemctl reload-or-restart NetworkManager.service
sudo systemctl restart hostapd.service dnsmasq.service
```

(replace `WIFI_IFACE` with the actual interface name from
`/etc/lite-recorder/ap.env`), then confirm with `nmcli device status`
that it now shows `unmanaged`.

### A camera shows "Device or resource busy"

Each camera is opened by exactly one ffmpeg process, so this means
something else already holds the hardware behind that `/dev/videoN`.

**The usual cause is the device itself, not contention.** A single
physical camera often exposes *several* capture-capable `/dev/video*`
nodes — on RK3588 an ISP pipeline publishes `mainpath`, `selfpath` and
`rawwrN` for one sensor, and some webcams publish a second streaming
interface. Those are alternate paths into one piece of hardware: while
one is streaming, opening another fails with "Device or resource busy"
permanently and by design. Registering a camera per node therefore
produces a camera that can never start, and no amount of retrying will
clear it — which looks exactly like a bug in the app.

Discovery groups nodes by their physical device and captures from one
node per device. To see what your board actually exposes, which nodes
were grouped together, and what every remaining node is:

```
sudo systemctl stop lite-recorder
/opt/lite-recorder/venv/bin/python -m lite_recorder --list-devices
sudo systemctl start lite-recorder
```

A node listed under "also exposes" is a second path into a camera that
is already listed — not a missing camera. If the camera count matches
what is physically attached, the grouping is right.

Stopping the service first is worth it: a node the recorder already has
open can only be reported as `busy`, and this command opens every node
to identify it — including paths into a camera that is mid-recording,
which normal operation deliberately avoids.

If a camera that *should* work is busy, the holder is outside the app.
Check with:

```
sudo fuser -v /dev/video*
```

If that names a process (another capture tool, a leftover `ffmpeg` from
a killed run), stop it. If nothing holds the node or any of its
siblings, suspect USB bandwidth rather than contention: several cameras
on one controller at high resolution/framerate can fail to start.
Confirm by lowering the resolution or framerate for the affected
cameras in the UI, and prefer spreading cameras across separate USB
controllers over a single hub.

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
scripts/            run-local.sh, install.sh, uninstall.sh, ap-up.sh, ap-down.sh
systemd/            lite-recorder.service, lite-recorder-ap.service
config/             hostapd/dnsmasq templates, .env.example files
tests/              pytest suite
```
