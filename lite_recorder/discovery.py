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
VIDIOC_ENUM_FRAMESIZES = 0xC02C5604
VIDIOC_ENUM_FRAMEINTERVALS = 0xC0345603

V4L2_CAP_VIDEO_CAPTURE = 0x00000001
V4L2_CAP_DEVICE_CAPS = 0x80000000

V4L2_BUF_TYPE_VIDEO_CAPTURE = 1
V4L2_FRMSIZE_TYPE_DISCRETE = 1
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
#   u32 width; u32 height; u32 stepwise_pad[4]; u32 reserved[2]; }
# (only the discrete branch of the union is used/read)
_FRMSIZE_FMT = "IIIII16x8x"
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
    buf = struct.pack(fmt, *values)
    buf = fcntl.ioctl(fd, request, bytearray(buf))
    return struct.unpack(fmt, buf)


def _query_cap(fd: int) -> dict | None:
    try:
        buf = bytearray(_CAPABILITY_SIZE)
        buf = fcntl.ioctl(fd, VIDIOC_QUERYCAP, buf)
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
            buf = bytearray(_FMTDESC_SIZE)
            packed = struct.pack(
                _FMTDESC_FMT, index, V4L2_BUF_TYPE_VIDEO_CAPTURE, 0, b"\x00" * 32, 0, 0
            )
            buf = fcntl.ioctl(fd, VIDIOC_ENUM_FMT, bytearray(packed))
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
            packed = struct.pack(_FRMSIZE_FMT, index, pf, 0, 0, 0)
            buf = fcntl.ioctl(fd, VIDIOC_ENUM_FRAMESIZES, bytearray(packed))
            _, _, ftype, width, height = struct.unpack(_FRMSIZE_FMT, buf)
        except OSError:
            break
        if ftype == V4L2_FRMSIZE_TYPE_DISCRETE:
            sizes.append((width, height))
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
            packed = struct.pack(_FRMIVAL_FMT, index, pf, width, height, 0, 0, 0)
            buf = fcntl.ioctl(fd, VIDIOC_ENUM_FRAMEINTERVALS, bytearray(packed))
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


def probe_device(device_node: str) -> CameraDevice | None:
    """Open a single /dev/videoN node and return a CameraDevice if it is
    a genuine capture device, else None (metadata nodes, subdevs, etc.)."""
    try:
        fd = os.open(device_node, os.O_RDWR | os.O_NONBLOCK)
    except OSError as exc:
        logger.debug("cannot open %s: %s", device_node, exc)
        return None
    try:
        cap = _query_cap(fd)
        if cap is None:
            return None
        if not (cap["capabilities"] & V4L2_CAP_VIDEO_CAPTURE):
            return None
        pixel_formats = _enum_formats(fd)
        if not pixel_formats:
            return None
        formats = []
        for pf in pixel_formats:
            for width, height in _enum_framesizes(fd, pf):
                rates = _enum_frameintervals(fd, pf, width, height)
                formats.append(
                    FrameFormat(pixel_format=pf, width=width, height=height, framerates=rates)
                )
        if not formats:
            return None
        source = _classify_source(cap["driver"], cap["bus_info"])
        return CameraDevice(
            id=_stable_id(device_node),
            device_node=device_node,
            name=cap["card"] or cap["driver"],
            source=source,
            driver=cap["driver"],
            formats=formats,
        )
    finally:
        os.close(fd)


def discover_cameras() -> list[CameraDevice]:
    """Enumerate every /dev/video* node and return the subset that are
    genuine capture devices, sorted by device node for stable ordering."""
    nodes = sorted(glob.glob("/dev/video*"), key=lambda p: int("".join(filter(str.isdigit, p)) or 0))
    cameras: list[CameraDevice] = []
    for node in nodes:
        cam = probe_device(node)
        if cam is not None:
            cameras.append(cam)
    return cameras
