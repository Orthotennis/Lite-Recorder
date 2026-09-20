r"""Windows DirectShow camera discovery backend.

Uses ffmpeg itself to enumerate and probe DirectShow video devices
(`-f dshow -list_devices true` / `-list_options true`) — the Windows
equivalent of how discovery_v4l2.py talks to V4L2 directly on Linux, and
with the same goal: no extra dependency (no pywin32, no WMI, nothing
beyond the ffmpeg binary the app already requires).

ffmpeg reports device/format listings as informational lines on stderr,
regardless of exit code, e.g.:

    [dshow @ 0000...] DirectShow video devices (some may be both video
    and audio devices)
    [dshow @ 0000...]  "Integrated Webcam"
    [dshow @ 0000...]     Alternative name "@device_pnp_\\?\usb#vid_..."

and, for `-list_options true -i video="Integrated Webcam"`:

    [dshow @ 0000...]   vcodec=mjpeg  min s=640x480 fps=5 max s=1280x720 fps=30
    [dshow @ 0000...]   pixel_format=yuyv422  min s=640x480 fps=5 max s=640x480 fps=30

Each `-list_options` line only gives a min/max range, not a discrete
list the way V4L2 does — so, like this app's V4L2 backend already does
for V4L2 "stepwise"/"continuous" size ranges, each line becomes a
single FrameFormat at its reported maximum size/rate.
"""
from __future__ import annotations

import logging
import re
import subprocess

from .discovery_types import CameraDevice, DiscoveryReport, FrameFormat

logger = logging.getLogger(__name__)

_VIDEO_HEADER_RE = re.compile(r"DirectShow video devices")
_AUDIO_HEADER_RE = re.compile(r"DirectShow audio devices")
_DEVICE_NAME_RE = re.compile(r'^\[dshow[^\]]*\]\s+"(?P<name>.+)"\s*$')
_ALT_NAME_RE = re.compile(r'^\[dshow[^\]]*\]\s+Alternative name "(?P<alt>.+)"\s*$')

# vcodec=mjpeg  min s=640x480 fps=5 max s=1280x720 fps=30
# pixel_format=yuyv422  min s=640x480 fps=5 max s=640x480 fps=30
_FORMAT_LINE_RE = re.compile(
    r"(?:vcodec=(?P<vcodec>\w+)|pixel_format=(?P<pixfmt>\w+))"
    r".*?max\s+s=(?P<width>\d+)x(?P<height>\d+)\s+fps=(?P<fps>[\d.]+)"
)


def _run_ffmpeg(ffmpeg_bin: str, args: list[str], timeout: float = 15) -> str:
    """Run ffmpeg and return its stderr text. dshow always reports device
    and format listings to stderr, whatever the process exit code (it
    exits non-zero here since `-i dummy`/a bare device name is never a
    playable input on its own)."""
    cmd = [ffmpeg_bin, "-hide_banner", *args]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("ffmpeg dshow probe failed: %s", exc)
        return ""
    return result.stderr or ""


def _list_device_names(ffmpeg_bin: str) -> list[tuple[str, str]]:
    """Return [(friendly_name, alt_name_or_empty), ...] for video devices."""
    output = _run_ffmpeg(ffmpeg_bin, ["-f", "dshow", "-list_devices", "true", "-i", "dummy"])
    devices: list[tuple[str, str]] = []
    in_video_section = False
    pending_name: str | None = None
    for line in output.splitlines():
        if _VIDEO_HEADER_RE.search(line):
            in_video_section = True
            continue
        if _AUDIO_HEADER_RE.search(line):
            if pending_name is not None:
                devices.append((pending_name, ""))
                pending_name = None
            in_video_section = False
            continue
        if not in_video_section:
            continue
        m = _DEVICE_NAME_RE.match(line)
        if m:
            if pending_name is not None:
                devices.append((pending_name, ""))
            pending_name = m.group("name")
            continue
        m = _ALT_NAME_RE.match(line)
        if m and pending_name is not None:
            devices.append((pending_name, m.group("alt")))
            pending_name = None
    if pending_name is not None:
        devices.append((pending_name, ""))
    return devices


def _probe_formats(ffmpeg_bin: str, device_name: str) -> list[FrameFormat]:
    output = _run_ffmpeg(
        ffmpeg_bin,
        ["-f", "dshow", "-list_options", "true", "-i", f"video={device_name}"],
    )
    formats: list[FrameFormat] = []
    for line in output.splitlines():
        m = _FORMAT_LINE_RE.search(line)
        if not m:
            continue
        if m.group("vcodec"):
            if m.group("vcodec").lower() != "mjpeg":
                # Other compressed vcodecs (h264 webcams, etc.) aren't
                # handled by the ffmpeg command lite_recorder builds today.
                continue
            pixel_format = "MJPG"
        else:
            pixel_format = m.group("pixfmt").upper()
        formats.append(
            FrameFormat(
                pixel_format=pixel_format,
                width=int(m.group("width")),
                height=int(m.group("height")),
                framerates=[float(m.group("fps"))],
            )
        )
    return formats


def _classify_source(alt_name: str) -> str:
    # DirectShow doesn't expose a bus type directly, but the device
    # instance path in the alternative name does for USB devices
    # (`@device_pnp_\?\usb#vid_...`). There's no MIPI-CSI equivalent
    # to detect on Windows, so anything else is just "unknown".
    return "usb" if "usb#" in alt_name.lower() else "unknown"


def probe_device(device_name: str, ffmpeg_bin: str = "ffmpeg", alt_name: str = "") -> CameraDevice | None:
    """Probe one DirectShow device by its friendly name, returning a
    CameraDevice if ffmpeg reports at least one usable capture format."""
    formats = _probe_formats(ffmpeg_bin, device_name)
    if not formats:
        return None
    return CameraDevice(
        id=alt_name or device_name,
        device_node=device_name,
        name=device_name,
        source=_classify_source(alt_name),
        driver="dshow",
        formats=formats,
    )


def discover_cameras_report(ffmpeg_bin: str = "ffmpeg") -> DiscoveryReport:
    """Enumerate DirectShow video devices via ffmpeg and probe each one
    for usable capture formats."""
    report = DiscoveryReport()
    try:
        devices = _list_device_names(ffmpeg_bin)
    except Exception as exc:  # noqa: BLE001 - one bad listing must not kill the scan
        logger.exception("failed to list DirectShow devices")
        report.rejected.append(("(enumeration)", f"ffmpeg dshow listing failed: {exc}"))
        return report
    for name, alt_name in devices:
        try:
            cam = probe_device(name, ffmpeg_bin=ffmpeg_bin, alt_name=alt_name)
        except Exception as exc:  # noqa: BLE001
            logger.exception("failed to probe DirectShow device %s", name)
            report.rejected.append((name, f"probe raised {exc.__class__.__name__}: {exc}"))
            continue
        if cam is not None:
            report.cameras.append(cam)
        else:
            report.rejected.append(
                (
                    name,
                    "no usable formats reported — likely a Windows camera "
                    "privacy setting blocking access, or the device is "
                    "already open in another app",
                )
            )
    return report


def discover_cameras(ffmpeg_bin: str = "ffmpeg") -> list[CameraDevice]:
    """Enumerate DirectShow video devices and return the subset that are
    genuine, currently-openable capture devices."""
    return discover_cameras_report(ffmpeg_bin).cameras
