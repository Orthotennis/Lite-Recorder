"""Generic V4L2 camera discovery.

Enumerates /dev/video* nodes and filters down to genuine video-capture
devices using VIDIOC_QUERYCAP / VIDIOC_ENUM_FMT ioctls (pure stdlib —
fcntl + struct — so no dependency on v4l2-ctl or python-v4l2 is needed).
This intentionally does not hardcode which nodes are cameras: every
/dev/video* is probed and classified generically, so any mix of MIPI CSI
(rkisp/rkcif) and USB (uvcvideo) sources is picked up the same way.
"""
from __future__ import annotations

import errno
import fcntl
import glob
import logging
import os
import struct
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


class DeviceBusyError(Exception):
    """The node exists but cannot be opened because something already has
    it open exclusively - normally our own ffmpeg capture process. This is
    explicitly *not* "this is not a camera": treating it as such would drop
    a live camera from the registry on every rescan."""

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
    # The physical device this node belongs to, and the *other* capture
    # nodes of that same device. Only `device_node` is ever opened; the
    # siblings are kept because they are the explanation for an EBUSY
    # that no amount of retrying can clear.
    physical_key: str = ""
    sibling_nodes: list[str] = field(default_factory=list)

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


# Pixel formats something can actually be previewed and recorded from.
# A capture node advertising none of these is an auxiliary path of a
# pipeline (e.g. the rkisp raw-capture nodes, which only emit Bayer), so
# it is never the node to capture from when a real one shares its device.
_USABLE_PIXEL_FORMATS = {
    "MJPG", "JPEG", "YUYV", "YVYU", "UYVY", "VYUY", "NV12", "NV16", "NV21",
    "NV24", "YU12", "YV12", "422P", "RGB3", "BGR3", "RGBP", "GREY", "H264", "HEVC",
}


def _sysfs_device_path(device_node: str) -> str | None:
    """The sysfs device a /dev/videoN node hangs off.

    For USB this is the UVC interface directory (e.g. .../1-1.3:1.0), so
    two genuinely independent camera functions inside one composite device
    keep separate identities. For a platform pipeline (rkisp/rkcif) it is
    the pipeline's platform device, which every video node it exposes
    shares.
    """
    link = os.path.join("/sys/class/video4linux", os.path.basename(device_node), "device")
    try:
        if not os.path.exists(link):
            return None
        return os.path.realpath(link)
    except OSError:
        return None


def _physical_device_key(device_node: str, bus_info: str) -> str:
    """Identify the physical device behind a node, so that several nodes
    of one camera are never mistaken for several cameras."""
    path = _sysfs_device_path(device_node)
    if path:
        return f"sysfs:{path}"
    # No sysfs entry (unusual). A USB bus_info still identifies exactly one
    # physical device, so grouping on it is safe. Anything else can be
    # shared by independent sensors, so fall back to not grouping at all:
    # a duplicate camera is a bug, but merging two real ones loses footage.
    if bus_info.lower().startswith("usb-"):
        return f"bus:{bus_info}"
    return f"node:{device_node}"


def _node_number(device_node: str) -> int:
    digits = "".join(filter(str.isdigit, os.path.basename(device_node)))
    return int(digits) if digits else 0


def _capture_rank(device: CameraDevice, previously_used: bool) -> tuple:
    """How suitable a node is as *the* node to capture its device from.
    Sorted descending, so the best node comes first.

    `previously_used` ranks first purely for stability: re-picking the node
    we already capture from keeps the camera's identity (and therefore its
    saved settings) fixed across rescans.
    """
    usable = any(f.pixel_format in _USABLE_PIXEL_FORMATS for f in device.formats)
    max_area = max((f.width * f.height for f in device.formats), default=0)
    return (previously_used, usable, max_area, -_node_number(device.device_node))


def probe_device(device_node: str) -> CameraDevice | None:
    """Open a single /dev/videoN node and return a CameraDevice if it is
    a genuine capture device, else None (metadata nodes, subdevs, etc.)."""
    try:
        fd = os.open(device_node, os.O_RDWR | os.O_NONBLOCK)
    except OSError as exc:
        if exc.errno == errno.EBUSY:
            raise DeviceBusyError(device_node) from exc
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
            physical_key=_physical_device_key(device_node, cap["bus_info"]),
        )
    finally:
        os.close(fd)


def device_holders(device_node: str) -> list[str]:
    """Which processes currently have `device_node` open, as "pid (name)".

    "Device or resource busy" from ffmpeg means some other file
    descriptor already has this V4L2 node streaming, but ffmpeg cannot
    say whose. Resolving it through /proc turns that dead end into an
    answer - typically a leftover ffmpeg from a previous run.
    """
    holders = []
    for proc_dir in glob.glob("/proc/[0-9]*"):
        pid = os.path.basename(proc_dir)
        try:
            for fd in os.listdir(os.path.join(proc_dir, "fd")):
                if os.path.realpath(os.path.join(proc_dir, "fd", fd)) == device_node:
                    try:
                        name = open(os.path.join(proc_dir, "comm")).read().strip()
                    except OSError:
                        name = "?"
                    holders.append(f"{pid} ({name})")
                    break
        except OSError:
            continue  # process exited, or not ours to inspect
    return holders


def _collapse_to_physical_devices(
    probed: list[CameraDevice],
    known: dict[str, CameraDevice],
    skipped: set[str] | None = None,
) -> list[CameraDevice]:
    """Reduce the probed nodes to one per physical device.

    One physical camera routinely exposes several capture-capable
    /dev/videoN nodes: the alternate output paths of an ISP pipeline on
    RK3588 (mainpath / selfpath / rawwrN), or a second streaming
    interface on a webcam. They are paths into one piece of hardware, not
    separate cameras - streaming through one is exactly what makes the
    others return EBUSY, for as long as capture runs.

    Registering a node per path therefore manufactures cameras that are
    *permanently* "Device or resource busy", held by our own ffmpeg on a
    sibling node, and no retry, re-ordering or teardown fix can ever clear
    that: the device is legitimately in use. Only the best node of each
    device becomes a camera; the rest are recorded as its siblings.
    """
    skipped = skipped or set()
    groups: dict[str, list[CameraDevice]] = {}
    for device in probed:
        key = device.physical_key or f"node:{device.device_node}"
        groups.setdefault(key, []).append(device)

    cameras: list[CameraDevice] = []
    for key, members in groups.items():
        members.sort(key=lambda d: _capture_rank(d, d.device_node in known), reverse=True)
        primary, *rest = members
        # Nodes left unprobed on purpose (below) are absent from `members`,
        # so they have to be carried over rather than dropped from the
        # device they are already known to belong to.
        siblings = {d.device_node for d in rest}
        siblings |= {s for s in primary.sibling_nodes if s in skipped}
        primary.sibling_nodes = sorted(siblings, key=_node_number)
        if rest or primary.sibling_nodes:
            logger.info(
                "%s exposes %d capture nodes (%s) - these are alternate paths "
                "of one device, so capturing from %s only; opening the others "
                "could report nothing but 'Device or resource busy'",
                primary.name or key,
                1 + len(primary.sibling_nodes),
                ", ".join([primary.device_node, *primary.sibling_nodes]),
                primary.device_node,
            )
        cameras.append(primary)
    cameras.sort(key=lambda d: _node_number(d.device_node))
    return cameras


def video_nodes() -> list[str]:
    """Every /dev/video* node, in kernel numbering order."""
    return sorted(
        glob.glob("/dev/video*"),
        key=lambda p: int("".join(filter(str.isdigit, p)) or 0),
    )


def _still_the_same_device(node: str, previous: CameraDevice) -> bool:
    """Whether a busy node is still the device it was last probed as.

    A busy node cannot be opened, so its identity has to be carried over
    from the last successful probe - but /dev/videoN gets reassigned when
    hardware comes and goes, and carrying a stale identity forward is how
    a worker ends up pointed at another camera's node. sysfs answers this
    without opening anything, so it works on a node we are streaming from.
    """
    path = _sysfs_device_path(node)
    if path is None or not previous.physical_key.startswith("sysfs:"):
        return True  # nothing to compare against - trust the last probe
    return previous.physical_key == f"sysfs:{path}"


def _probe_node(
    node: str, known: dict[str, CameraDevice]
) -> tuple[CameraDevice | None, bool]:
    """Probe one node. Returns (device, busy), where `busy` means the node
    exists but is already open - normally by our own capture process."""
    try:
        return probe_device(node), False
    except DeviceBusyError:
        previous = known.get(node)
        if previous is None:
            logger.warning("%s is busy and was never probed successfully; skipping", node)
            return None, True
        if not _still_the_same_device(node, previous):
            # Dropping it here lets the worker be torn down and the node
            # released, so the next pass can probe it for real.
            logger.warning(
                "%s is busy but sysfs says it is no longer the device %s was "
                "probed on; discarding the stale identity",
                node,
                previous.id,
            )
            return None, True
        logger.debug("%s is busy (in use); keeping known device %s", node, previous.id)
        return previous, True
    except Exception:
        logger.exception("failed to probe %s, skipping", node)
        return None, False


def discover_cameras(known: dict[str, CameraDevice] | None = None) -> list[CameraDevice]:
    """Enumerate every /dev/video* node and return one CameraDevice per
    physical camera, sorted by device node for stable ordering.

    `known` maps device_node -> previously discovered CameraDevice. A node
    we are already capturing from can refuse a second open() with EBUSY on
    drivers that enforce exclusive access; without the fallback such a node
    would look like "not a camera" and the live camera would be torn down
    and recreated on every rescan. It also keeps the choice of node stable
    for a device that exposes more than one."""
    nodes = video_nodes()
    known = known or {}
    present = set(nodes)
    probed: dict[str, CameraDevice] = {}
    skipped: set[str] = set()

    # Pass 1: the nodes we already capture from, so that the cameras that
    # are live right now are identified before anything decides whether to
    # open their remaining nodes.
    for node in nodes:
        if node not in known:
            continue
        device, busy = _probe_node(node, known)
        if device is None:
            continue
        probed[node] = device
        if busy:
            # This camera is streaming. Its other nodes are the same piece
            # of hardware and we already know it, so opening them tells us
            # nothing - while a stray open() on a streaming ISP pipeline is
            # exactly the kind of poke that disturbs it. Leave them shut.
            skipped |= {s for s in device.sibling_nodes if s in present}

    # Pass 2: everything else, in node order.
    for node in nodes:
        if node in probed or node in skipped:
            continue
        device, _busy = _probe_node(node, known)
        if device is not None:
            probed[node] = device

    ordered = [probed[node] for node in nodes if node in probed]
    return _collapse_to_physical_devices(ordered, known, skipped)


@dataclass
class NodeReport:
    """What one /dev/videoN node turned out to be, for diagnostics."""

    node: str
    status: str  # "capture" | "busy" | "other" | "error"
    detail: str = ""
    camera_id: str = ""  # the camera it was attributed to, if any


def describe_nodes() -> tuple[list[CameraDevice], list[NodeReport]]:
    """One probe pass over every /dev/video*, for diagnostics.

    Unlike discover_cameras() this reports the nodes it could *not* use,
    busy ones above all. A busy node is the normal case while the recorder
    is running, and dropping it silently would hide the very camera
    someone is trying to diagnose.

    This opens every node, including the alternate paths of a device that
    is streaming - which discover_cameras() deliberately avoids - so it is
    best run with the recorder stopped.
    """
    probed: list[CameraDevice] = []
    reports: list[NodeReport] = []
    for node in video_nodes():
        try:
            cam = probe_device(node)
        except DeviceBusyError:
            holders = device_holders(node)
            reports.append(
                NodeReport(node, "busy", ", ".join(holders) if holders else "holder unknown")
            )
            continue
        except Exception as exc:  # noqa: BLE001 - a diagnostic reports, never raises
            reports.append(NodeReport(node, "error", str(exc)))
            continue
        if cam is None:
            reports.append(NodeReport(node, "other", "not a capture node (metadata/subdev)"))
        else:
            probed.append(cam)
            reports.append(NodeReport(node, "capture"))

    cameras = _collapse_to_physical_devices(probed, {})
    owner = {}
    for cam in cameras:
        for node in [cam.device_node, *cam.sibling_nodes]:
            owner[node] = cam.id
    for report in reports:
        report.camera_id = owner.get(report.node, "")
    return cameras, reports
