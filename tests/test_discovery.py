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
