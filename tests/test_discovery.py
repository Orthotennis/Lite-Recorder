import struct
from unittest import mock

import pytest

from lite_recorder import discovery


class FakeV4L2Node:
    """Simulates ioctl() responses for one /dev/videoN capture device."""

    def __init__(self, driver, card, bus_info, formats):
        self.driver = driver
        self.card = card
        self.bus_info = bus_info
        # formats: list of (pixel_format:str, [(w,h,[fps,...]), ...])
        self.formats = formats

    def ioctl(self, request, buf):
        if request == discovery.VIDIOC_QUERYCAP:
            return struct.pack(
                discovery._CAPABILITY_FMT,
                self.driver.encode(),
                self.card.encode(),
                self.bus_info.encode(),
                0,
                discovery.V4L2_CAP_VIDEO_CAPTURE,
                0,
            )
        if request == discovery.VIDIOC_ENUM_FMT:
            (index, _type, _flags, _desc, _pf, _mbus) = struct.unpack(discovery._FMTDESC_FMT, buf)
            if index >= len(self.formats):
                raise OSError("EINVAL")
            pf_str, _ = self.formats[index]
            return struct.pack(
                discovery._FMTDESC_FMT, index, 1, 0, b"\x00" * 32, discovery._str_to_fourcc(pf_str), 0
            )
        if request == discovery.VIDIOC_ENUM_FRAMESIZES:
            # v4l2_frmsizeenum: index, pixel_format, type, then a 6-u32
            # union (discrete uses the first two as width/height).
            (index, pf, _type, *_union) = struct.unpack(discovery._FRMSIZE_FMT, buf)
            pf_str = discovery._fourcc_to_str(pf)
            sizes = next((sizes for name, sizes in self.formats if name == pf_str), [])
            if index >= len(sizes):
                raise OSError("EINVAL")
            w, h, _rates = sizes[index]
            return struct.pack(
                discovery._FRMSIZE_FMT,
                index,
                pf,
                discovery.V4L2_FRMSIZE_TYPE_DISCRETE,
                w,
                h,
                0,
                0,
                0,
                0,
            )
        if request == discovery.VIDIOC_ENUM_FRAMEINTERVALS:
            (index, pf, w, h, _type, _n, _d) = struct.unpack(discovery._FRMIVAL_FMT, buf)
            pf_str = discovery._fourcc_to_str(pf)
            sizes = next((sizes for name, sizes in self.formats if name == pf_str), [])
            entry = next((s for s in sizes if s[0] == w and s[1] == h), None)
            rates = entry[2] if entry else []
            if index >= len(rates):
                raise OSError("EINVAL")
            fps = rates[index]
            return struct.pack(discovery._FRMIVAL_FMT, index, pf, w, h, discovery.V4L2_FRMIVAL_TYPE_DISCRETE, 1, fps)
        raise OSError(f"unhandled request {request:#x}")


def _patched(node: FakeV4L2Node):
    def fake_ioctl(fd, request, buf):
        result = node.ioctl(request, bytes(buf))
        buf[: len(result)] = result
        return 0

    return mock.patch("fcntl.ioctl", side_effect=fake_ioctl)


def test_probe_device_usb_camera():
    node = FakeV4L2Node(
        driver="uvcvideo",
        card="USB 2.0 Camera",
        bus_info="usb-0000:00:14.0-1",
        formats=[("MJPG", [(1280, 720, [30, 15]), (640, 480, [30])])],
    )
    with mock.patch("os.open", return_value=42), mock.patch("os.close"), _patched(node):
        cam = discovery.probe_device("/dev/video0")

    assert cam is not None
    assert cam.source == "usb"
    assert cam.driver == "uvcvideo"
    assert len(cam.formats) == 2
    best = cam.best_effort_default_format()
    assert best.pixel_format == "MJPG"
    assert (best.width, best.height) == (1280, 720)
    assert best.framerates == [30.0, 15.0]


def test_probe_device_csi_camera_classified():
    node = FakeV4L2Node(
        driver="rkisp1",
        card="rkisp1-isp",
        bus_info="platform:rkisp1",
        formats=[("YUYV", [(1920, 1080, [30])])],
    )
    with mock.patch("os.open", return_value=7), mock.patch("os.close"), _patched(node):
        cam = discovery.probe_device("/dev/video10")

    assert cam is not None
    assert cam.source == "csi"


def test_probe_device_rejects_non_capture_node():
    node = FakeV4L2Node(driver="uvcvideo", card="meta", bus_info="usb-1", formats=[])
    with mock.patch("os.open", return_value=3), mock.patch("os.close"), _patched(node):
        cam = discovery.probe_device("/dev/video1")
    assert cam is None


def test_probe_device_handles_open_failure():
    with mock.patch("os.open", side_effect=OSError("no such device")):
        cam = discovery.probe_device("/dev/video99")
    assert cam is None


def test_probe_device_raises_on_busy():
    """EBUSY means "already open" (normally by our own capture process),
    not "not a camera" - the caller must be able to tell them apart."""
    import errno

    with mock.patch("os.open", side_effect=OSError(errno.EBUSY, "Device or resource busy")):
        with pytest.raises(discovery.DeviceBusyError):
            discovery.probe_device("/dev/video1")


def test_discover_cameras_keeps_known_device_when_busy():
    """A node we are already capturing from can refuse a second open().
    It must not drop out of the registry, or rescan would tear down and
    recreate every live camera."""
    import errno

    known = discovery.CameraDevice(
        id="usb-Cam-A-video-index0",
        device_node="/dev/video0",
        name="Cam A",
        source="usb",
        driver="uvcvideo",
        formats=[discovery.FrameFormat("MJPG", 640, 480, [30.0])],
    )

    with mock.patch("glob.glob", return_value=["/dev/video0"]), mock.patch(
        "os.open", side_effect=OSError(errno.EBUSY, "Device or resource busy")
    ):
        cams = discovery.discover_cameras(known={"/dev/video0": known})
        without_known = discovery.discover_cameras()

    assert cams == [known]
    assert without_known == []


def test_discover_cameras_filters_and_orders(tmp_path):
    good = FakeV4L2Node("uvcvideo", "Cam A", "usb-1", [("MJPG", [(640, 480, [30])])])
    bad = FakeV4L2Node("uvcvideo", "meta", "usb-1", [])

    def fake_open(path, *_args, **_kwargs):
        return {"video0": 1, "video1": 2}.get(path.rsplit("/", 1)[-1], 1)

    def fake_ioctl(fd, request, buf):
        node = good if fd == 1 else bad
        result = node.ioctl(request, bytes(buf))
        buf[: len(result)] = result
        return 0

    with mock.patch("glob.glob", return_value=["/dev/video1", "/dev/video0"]), mock.patch(
        "os.open", side_effect=fake_open
    ), mock.patch("os.close"), mock.patch("fcntl.ioctl", side_effect=fake_ioctl):
        cams = discovery.discover_cameras()

    assert [c.device_node for c in cams] == ["/dev/video0"]


# --- one camera, several capture nodes -----------------------------------
#
# A single physical camera routinely exposes more than one capture-capable
# /dev/videoN: the alternate output paths of an RK3588 ISP pipeline
# (mainpath / selfpath / rawwrN), or a second streaming interface on a
# webcam. Registering one camera per node means the second worker opens a
# node whose hardware the first is already streaming, which fails with
# "Device or resource busy" permanently - correctly, and for as long as
# capture runs. No retry or re-ordering can fix that; discovery must not
# create the duplicate in the first place.


def _fake_v4l2_tree(nodes: dict, sysfs: dict):
    """Patch glob/open/ioctl/sysfs for a set of {node: FakeV4L2Node}."""
    fds = {node: i + 1 for i, node in enumerate(sorted(nodes))}
    by_fd = {fd: nodes[node] for node, fd in fds.items()}

    def fake_open(path, *_args, **_kwargs):
        return fds[path]

    def fake_ioctl(fd, request, buf):
        result = by_fd[fd].ioctl(request, bytes(buf))
        buf[: len(result)] = result
        return 0

    def fake_exists(path):
        return path.rsplit("/", 2)[-2] in {n.rsplit("/", 1)[-1] for n in sysfs}

    def fake_realpath(path):
        video = path.rsplit("/", 2)[-2]
        return sysfs[f"/dev/{video}"]

    return (
        mock.patch("glob.glob", return_value=sorted(nodes)),
        mock.patch("os.open", side_effect=fake_open),
        mock.patch("os.close"),
        mock.patch("fcntl.ioctl", side_effect=fake_ioctl),
        mock.patch("os.path.exists", side_effect=fake_exists),
        mock.patch("os.path.realpath", side_effect=fake_realpath),
    )


def _discover(nodes, sysfs, known=None):
    patches = _fake_v4l2_tree(nodes, sysfs)
    for p in patches:
        p.start()
    try:
        return discovery.discover_cameras(known=known)
    finally:
        for p in patches:
            p.stop()


def test_isp_pipeline_paths_become_one_camera():
    """rkisp exposes mainpath/selfpath/rawwr for ONE sensor. Three cameras
    here means two of them are permanently EBUSY against the third."""
    nodes = {
        "/dev/video0": FakeV4L2Node(
            "rkisp", "rkisp_mainpath", "platform:rkisp-vir0",
            [("NV12", [(1920, 1080, [30])])],
        ),
        "/dev/video1": FakeV4L2Node(
            "rkisp", "rkisp_selfpath", "platform:rkisp-vir0",
            [("NV12", [(1280, 720, [30])])],
        ),
        "/dev/video2": FakeV4L2Node(
            "rkisp", "rkisp_rawwr0", "platform:rkisp-vir0",
            [("BG10", [(1920, 1080, [30])])],
        ),
    }
    sysfs = dict.fromkeys(nodes, "/sys/devices/platform/rkisp-vir0")

    cams = _discover(nodes, sysfs)

    assert [c.device_node for c in cams] == ["/dev/video0"]
    # The highest-resolution path that emits a usable pixel format wins;
    # the raw Bayer node is never a capture candidate.
    assert cams[0].sibling_nodes == ["/dev/video1", "/dev/video2"]


def test_two_isp_pipelines_stay_two_cameras():
    """Grouping must not merge genuinely separate sensors: losing a real
    camera is worse than the duplicate this is fixing."""
    nodes = {
        "/dev/video0": FakeV4L2Node(
            "rkisp", "rkisp_mainpath", "platform:rkisp-vir0", [("NV12", [(1920, 1080, [30])])]
        ),
        "/dev/video1": FakeV4L2Node(
            "rkisp", "rkisp_selfpath", "platform:rkisp-vir0", [("NV12", [(1280, 720, [30])])]
        ),
        "/dev/video2": FakeV4L2Node(
            "rkisp", "rkisp_mainpath", "platform:rkisp-vir1", [("NV12", [(1920, 1080, [30])])]
        ),
        "/dev/video3": FakeV4L2Node(
            "rkisp", "rkisp_selfpath", "platform:rkisp-vir1", [("NV12", [(1280, 720, [30])])]
        ),
    }
    sysfs = {
        "/dev/video0": "/sys/devices/platform/rkisp-vir0",
        "/dev/video1": "/sys/devices/platform/rkisp-vir0",
        "/dev/video2": "/sys/devices/platform/rkisp-vir1",
        "/dev/video3": "/sys/devices/platform/rkisp-vir1",
    }

    cams = _discover(nodes, sysfs)

    assert [c.device_node for c in cams] == ["/dev/video0", "/dev/video2"]


def test_usb_cameras_on_separate_interfaces_stay_separate():
    """Two webcams are two cameras even though both are 'usb'."""
    nodes = {
        "/dev/video0": FakeV4L2Node(
            "uvcvideo", "Cam A", "usb-xhci-hcd.0.auto-1.1", [("MJPG", [(1280, 720, [30])])]
        ),
        "/dev/video2": FakeV4L2Node(
            "uvcvideo", "Cam B", "usb-xhci-hcd.0.auto-1.2", [("MJPG", [(1280, 720, [30])])]
        ),
    }
    sysfs = {
        "/dev/video0": "/sys/devices/platform/usb1/1-1/1-1.1/1-1.1:1.0",
        "/dev/video2": "/sys/devices/platform/usb1/1-1/1-1.2/1-1.2:1.0",
    }

    cams = _discover(nodes, sysfs)

    assert [c.device_node for c in cams] == ["/dev/video0", "/dev/video2"]
    assert all(c.sibling_nodes == [] for c in cams)


def test_node_already_in_use_stays_the_chosen_one():
    """The chosen node decides the camera's id, and therefore which saved
    settings it gets. It must not drift between rescans."""
    nodes = {
        "/dev/video0": FakeV4L2Node(
            "rkisp", "rkisp_mainpath", "platform:rkisp-vir0", [("NV12", [(1920, 1080, [30])])]
        ),
        "/dev/video1": FakeV4L2Node(
            "rkisp", "rkisp_selfpath", "platform:rkisp-vir0", [("NV12", [(1280, 720, [30])])]
        ),
    }
    sysfs = dict.fromkeys(nodes, "/sys/devices/platform/rkisp-vir0")

    already_capturing = discovery.CameraDevice(
        id="selfpath", device_node="/dev/video1", name="rkisp_selfpath",
        source="csi", driver="rkisp",
        formats=[discovery.FrameFormat("NV12", 1280, 720, [30.0])],
        physical_key="sysfs:/sys/devices/platform/rkisp-vir0",
    )

    cams = _discover(nodes, sysfs, known={"/dev/video1": already_capturing})

    assert [c.device_node for c in cams] == ["/dev/video1"]


def test_physical_key_does_not_merge_platform_devices_without_sysfs():
    """Without sysfs, only USB bus_info identifies one physical device.
    A shared platform bus_info must not collapse distinct sensors."""
    with mock.patch("os.path.exists", return_value=False):
        a = discovery._physical_device_key("/dev/video0", "platform:rkcif")
        b = discovery._physical_device_key("/dev/video1", "platform:rkcif")
        usb_a = discovery._physical_device_key("/dev/video2", "usb-xhci-hcd.0.auto-1.1")
        usb_b = discovery._physical_device_key("/dev/video3", "usb-xhci-hcd.0.auto-1.1")

    assert a != b
    assert usb_a == usb_b
