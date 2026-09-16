"""Generic V4L2 camera discovery.

Enumerates /dev/video* nodes and filters down to genuine video-capture
devices using VIDIOC_QUERYCAP / VIDIOC_ENUM_FMT ioctls (pure stdlib —
fcntl + struct — so no dependency on v4l2-ctl or python-v4l2 is needed).
This intentionally does not hardcode which nodes are cameras: every
/dev/video* is probed and classified generically, so any mix of MIPI CSI
(rkisp/rkcif) and USB (uvcvideo) sources is picked up the same way.
"""
from __future__ import annotations

import fcntl
import glob
import logging
import os
import struct
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# --- V4L2 ioctl / struct definitions (linux/videodev2.h) -----------------

VIDIOC_QUERYCAP = 0x80685600
VIDIOC_ENUM_FMT = 0xC0405602
# _IOWR('V', 74, struct v4l2_frmsizeenum) / _IOWR('V', 75, struct v4l2_frmivalenum)
VIDIOC_ENUM_FRAMESIZES = 0xC02C564A
VIDIOC_ENUM_FRAMEINTERVALS = 0xC034564B

V4L2_CAP_VIDEO_CAPTURE = 0x00000001
V4L2_CAP_DEVICE_CAPS = 0x80000000

V4L2_BUF_TYPE_VIDEO_CAPTURE = 1
V4L2_FRMSIZE_TYPE_DISCRETE = 1
V4L2_FRMSIZE_TYPE_CONTINUOUS = 2
V4L2_FRMSIZE_TYPE_STEPWISE = 3
V4L2_FRMIVAL_TYPE_DISCRETE = 1

# struct v4l2_capability { u8 driver[16]; u8 card[32]; u8 bus_info[32];
#   u32 version; u32 capabilities; u32 device_caps; u32 reserved[3]; }
_CAPABILITY_FMT = "16s32s32sIII12x"
_CAPABILITY_SIZE = struct.calcsize(_CAPABILITY_FMT)

# struct v4l2_fmtdesc { u32 index; u32 type; u32 flags; u8 description[32];
#   u32 pixelformat; u32 mbus_code; u32 reserved[3]; }
_FMTDESC_FMT = "III32sII12x"
_FMTDESC_SIZE = struct.calcsize(_FMTDESC_FMT)

# struct v4l2_frmsizeenum { u32 index; u32 pixel_format; u32 type;
#   union { struct discrete{width,height}; struct stepwise{min_width,
#   max_width,step_width,min_height,max_height,step_height}; }; u32 reserved[2]; }
# The 6 trailing u32s are read generically and reinterpreted per `type`:
# discrete uses only the first two (width, height); stepwise/continuous use
# all six (the discrete width/height alias min_width/max_width).
_FRMSIZE_FMT = "IIIIIIIII8x"
_FRMSIZE_SIZE = struct.calcsize(_FRMSIZE_FMT)

# struct v4l2_frmivalenum { u32 index; u32 pixel_format; u32 width;
#   u32 height; u32 type; u32 numerator; u32 denominator;
#   u32 stepwise_pad[4]; u32 reserved[2]; }
_FRMIVAL_FMT = "IIIIIII16x8x"
_FRMIVAL_SIZE = struct.calcsize(_FRMIVAL_FMT)


def _fourcc_to_str(value: int) -> str:
    return bytes(
        [(value >> 0) & 0xFF, (value >> 8) & 0xFF, (value >> 16) & 0xFF, (value >> 24) & 0xFF]
    ).decode("ascii", errors="replace")


def _str_to_fourcc(s: str) -> int:
    b = s.encode("ascii")
    return b[0] | (b[1] << 8) | (b[2] << 16) | (b[3] << 24)


@dataclass
class FrameFormat:
    pixel_format: str
    width: int
    height: int
    framerates: list[float] = field(default_factory=list)


@dataclass
class CameraDevice:
    id: str  # stable identity, e.g. by-id symlink name or fallback
    device_node: str  # e.g. /dev/video0
    name: str  # human-readable card/sensor name from sysfs/querycap
    source: str  # "csi" | "usb" | "unknown"
    driver: str
    formats: list[FrameFormat] = field(default_factory=list)

    def best_effort_default_format(self) -> FrameFormat | None:
        # Prefer MJPEG (saves USB bandwidth with many webcams), then
        # highest resolution among the remaining formats.
        if not self.formats:
            return None
        mjpeg = [f for f in self.formats if f.pixel_format == "MJPG"]
        candidates = mjpeg or self.formats
        return max(candidates, key=lambda f: f.width * f.height)


def _ioctl_struct(fd: int, request: int, fmt: str, *values) -> tuple:
    buf = bytearray(struct.pack(fmt, *values))
    fcntl.ioctl(fd, request, buf)
    return struct.unpack(fmt, buf)


def _query_cap(fd: int) -> dict | None:
    try:
        buf = bytearray(_CAPABILITY_SIZE)
        fcntl.ioctl(fd, VIDIOC_QUERYCAP, buf)
        driver, card, bus_info, version, capabilities, device_caps = struct.unpack(
            _CAPABILITY_FMT, buf
        )
    except OSError:
        return None
    caps = capabilities
    if capabilities & V4L2_CAP_DEVICE_CAPS:
        caps = device_caps
    return {
        "driver": driver.split(b"\x00", 1)[0].decode(errors="replace"),
        "card": card.split(b"\x00", 1)[0].decode(errors="replace"),
        "bus_info": bus_info.split(b"\x00", 1)[0].decode(errors="replace"),
        "capabilities": caps,
    }


def _enum_formats(fd: int) -> list[str]:
    pixel_formats = []
    index = 0
    while True:
        try:
            buf = bytearray(
                struct.pack(
                    _FMTDESC_FMT, index, V4L2_BUF_TYPE_VIDEO_CAPTURE, 0, b"\x00" * 32, 0, 0
                )
            )
            fcntl.ioctl(fd, VIDIOC_ENUM_FMT, buf)
            _, _, _, _description, pixelformat, _mbus = struct.unpack(_FMTDESC_FMT, buf)
        except OSError:
            break
        pixel_formats.append(_fourcc_to_str(pixelformat))
        index += 1
        if index > 64:
            break
    return pixel_formats


def _enum_framesizes(fd: int, pixel_format: str) -> list[tuple[int, int]]:
    sizes = []
    index = 0
    pf = _str_to_fourcc(pixel_format)
    while True:
        try:
            buf = bytearray(struct.pack(_FRMSIZE_FMT, index, pf, 0, 0, 0, 0, 0, 0, 0))
            fcntl.ioctl(fd, VIDIOC_ENUM_FRAMESIZES, buf)
            _, _, ftype, f1, f2, f3, f4, f5, f6 = struct.unpack(_FRMSIZE_FMT, buf)
        except OSError:
            break
        if ftype == V4L2_FRMSIZE_TYPE_DISCRETE:
            sizes.append((f1, f2))
        elif ftype in (V4L2_FRMSIZE_TYPE_STEPWISE, V4L2_FRMSIZE_TYPE_CONTINUOUS):
            # Only a single entry describes the whole min/max/step range
            # (index > 0 always fails), so report the max size and stop.
            sizes.append((f2, f5))
            break
        index += 1
        if index > 64:
            break
    return sizes


def _enum_frameintervals(fd: int, pixel_format: str, width: int, height: int) -> list[float]:
    rates = []
    index = 0
    pf = _str_to_fourcc(pixel_format)
    while True:
        try:
            buf = bytearray(struct.pack(_FRMIVAL_FMT, index, pf, width, height, 0, 0, 0))
            fcntl.ioctl(fd, VIDIOC_ENUM_FRAMEINTERVALS, buf)
            _, _, _, _, ftype, num, den = struct.unpack(_FRMIVAL_FMT, buf)
        except OSError:
            break
        if ftype == V4L2_FRMIVAL_TYPE_DISCRETE and num:
            rates.append(round(den / num, 2))
        index += 1
        if index > 64:
            break
    return rates


def _classify_source(driver: str, bus_info: str) -> str:
    driver_l = driver.lower()
    if "uvcvideo" in driver_l or bus_info.lower().startswith("usb"):
        return "usb"
    if any(k in driver_l for k in ("rkisp", "rkcif", "rockchip")):
        return "csi"
    return "unknown"


def _stable_id(device_node: str) -> str:
    """Prefer a /dev/v4l/by-id symlink (survives replug/reboot), falling
    back to by-path, then the raw device node name."""
    name = os.path.basename(device_node)
    for base in ("/dev/v4l/by-id", "/dev/v4l/by-path"):
        if not os.path.isdir(base):
            continue
        try:
            for entry in sorted(os.listdir(base)):
                target = os.path.realpath(os.path.join(base, entry))
                if os.path.basename(target) == name:
                    return entry
        except OSError:
            continue
    return name


def _probe_device_detail(device_node: str) -> tuple[CameraDevice | None, str]:
    """Probe one node, returning (device, reason_it_was_rejected)."""
    try:
        fd = os.open(device_node, os.O_RDWR | os.O_NONBLOCK)
    except PermissionError:
        return None, "permission denied (is your user in the 'video' group?)"
    except OSError as exc:
        logger.debug("cannot open %s: %s", device_node, exc)
        return None, f"cannot open ({exc.strerror or exc})"
    try:
        cap = _query_cap(fd)
        if cap is None:
            return None, "not a V4L2 device (VIDIOC_QUERYCAP failed)"
        if not (cap["capabilities"] & V4L2_CAP_VIDEO_CAPTURE):
            return None, "not a video-capture node (metadata/output/subdev)"
        pixel_formats = _enum_formats(fd)
        if not pixel_formats:
            return None, "no capture pixel formats reported"
        formats = []
        for pf in pixel_formats:
            for width, height in _enum_framesizes(fd, pf):
                rates = _enum_frameintervals(fd, pf, width, height)
                formats.append(
                    FrameFormat(pixel_format=pf, width=width, height=height, framerates=rates)
                )
        if not formats:
            return None, "no frame sizes reported"
        source = _classify_source(cap["driver"], cap["bus_info"])
        device = CameraDevice(
            id=_stable_id(device_node),
            device_node=device_node,
            name=cap["card"] or cap["driver"],
            source=source,
            driver=cap["driver"],
            formats=formats,
        )
        return device, ""
    finally:
        os.close(fd)


def probe_device(device_node: str) -> CameraDevice | None:
    """Open a single /dev/videoN node and return a CameraDevice if it is
    a genuine capture device, else None (metadata nodes, subdevs, etc.)."""
    device, _reason = _probe_device_detail(device_node)
    return device


@dataclass
class DiscoveryReport:
    """What a scan of /dev/video* found, including why nodes were skipped.

    Kept alongside the camera list so the app can explain an empty result
    ("no /dev/video* nodes at all" vs "found one but permission denied")
    instead of just showing an empty grid.
    """

    cameras: list[CameraDevice] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)

    @property
    def nodes_seen(self) -> int:
        return len(self.cameras) + len(self.rejected)

    def explain_empty(self) -> str:
        """A one-line, actionable reason why no cameras were found."""
        if self.cameras:
            return ""
        if not self.rejected:
            if not os.path.isdir("/dev"):
                return "no /dev directory — V4L2 capture is Linux-only."
            return (
                "no /dev/video* devices exist on this machine. Plug in a USB "
                "webcam (on WSL, attach it with usbipd; on a Rock 5B+, enable "
                "the CSI overlay with rsetup)."
            )
        if all("permission denied" in reason for _node, reason in self.rejected):
            return (
                "found "
                + ", ".join(node for node, _ in self.rejected)
                + " but could not open them: permission denied. Add your user to "
                "the 'video' group (sudo usermod -aG video $USER) and log back in."
            )
        details = "; ".join(f"{node}: {reason}" for node, reason in self.rejected)
        return f"no capture-capable camera among the /dev/video* nodes ({details})."


def discover_cameras_report() -> DiscoveryReport:
    """Enumerate every /dev/video* node, returning the capture devices
    found plus the reason each other node was skipped."""
    nodes = sorted(glob.glob("/dev/video*"), key=lambda p: int("".join(filter(str.isdigit, p)) or 0))
    report = DiscoveryReport()
    for node in nodes:
        try:
            cam, reason = _probe_device_detail(node)
        except Exception as exc:  # noqa: BLE001 - one bad node must not kill the scan
            logger.exception("failed to probe %s, skipping", node)
            report.rejected.append((node, f"probe raised {exc.__class__.__name__}: {exc}"))
            continue
        if cam is not None:
            report.cameras.append(cam)
        else:
            report.rejected.append((node, reason))
    return report


def discover_cameras() -> list[CameraDevice]:
    """Enumerate every /dev/video* node and return the subset that are
    genuine capture devices, sorted by device node for stable ordering."""
    return discover_cameras_report().cameras
