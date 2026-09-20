import struct
from unittest import mock

from lite_recorder import discovery, discovery_v4l2


class FakeV4L2Node:
    """Simulates ioctl() responses for one /dev/videoN capture device."""

    def __init__(self, driver, card, bus_info, formats):
        self.driver = driver
        self.card = card
        self.bus_info = bus_info
        # formats: list of (pixel_format:str, [(w,h,[fps,...]), ...])
        self.formats = formats

    def ioctl(self, request, buf):
        if request == discovery_v4l2.VIDIOC_QUERYCAP:
            return struct.pack(
                discovery_v4l2._CAPABILITY_FMT,
                self.driver.encode(),
                self.card.encode(),
                self.bus_info.encode(),
                0,
                discovery_v4l2.V4L2_CAP_VIDEO_CAPTURE,
                0,
            )
        if request == discovery_v4l2.VIDIOC_ENUM_FMT:
            (index, _type, _flags, _desc, _pf, _mbus) = struct.unpack(discovery_v4l2._FMTDESC_FMT, buf)
            if index >= len(self.formats):
                raise OSError("EINVAL")
            pf_str, _ = self.formats[index]
            return struct.pack(
                discovery_v4l2._FMTDESC_FMT, index, 1, 0, b"\x00" * 32, discovery_v4l2._str_to_fourcc(pf_str), 0
            )
        if request == discovery_v4l2.VIDIOC_ENUM_FRAMESIZES:
            # v4l2_frmsizeenum carries a 6-u32 union after index/format/type;
            # a discrete entry uses only its first two words (width, height).
            (index, pf, _type, *_union) = struct.unpack(discovery_v4l2._FRMSIZE_FMT, buf)
            pf_str = discovery_v4l2._fourcc_to_str(pf)
            sizes = next((sizes for name, sizes in self.formats if name == pf_str), [])
            if index >= len(sizes):
                raise OSError("EINVAL")
            w, h, _rates = sizes[index]
            return struct.pack(
                discovery_v4l2._FRMSIZE_FMT,
                index,
                pf,
                discovery_v4l2.V4L2_FRMSIZE_TYPE_DISCRETE,
                w,
                h,
                0,
                0,
                0,
                0,
            )
        if request == discovery_v4l2.VIDIOC_ENUM_FRAMEINTERVALS:
            (index, pf, w, h, _type, _n, _d) = struct.unpack(discovery_v4l2._FRMIVAL_FMT, buf)
            pf_str = discovery_v4l2._fourcc_to_str(pf)
            sizes = next((sizes for name, sizes in self.formats if name == pf_str), [])
            entry = next((s for s in sizes if s[0] == w and s[1] == h), None)
            rates = entry[2] if entry else []
            if index >= len(rates):
                raise OSError("EINVAL")
            fps = rates[index]
            return struct.pack(discovery_v4l2._FRMIVAL_FMT, index, pf, w, h, discovery_v4l2.V4L2_FRMIVAL_TYPE_DISCRETE, 1, fps)
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
        cam = discovery_v4l2.probe_device("/dev/video0")

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
        cam = discovery_v4l2.probe_device("/dev/video10")

    assert cam is not None
    assert cam.source == "csi"


def test_probe_device_rejects_non_capture_node():
    node = FakeV4L2Node(driver="uvcvideo", card="meta", bus_info="usb-1", formats=[])
    with mock.patch("os.open", return_value=3), mock.patch("os.close"), _patched(node):
        cam = discovery_v4l2.probe_device("/dev/video1")
    assert cam is None


def test_probe_device_handles_open_failure():
    with mock.patch("os.open", side_effect=OSError("no such device")):
        cam = discovery_v4l2.probe_device("/dev/video99")
    assert cam is None


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
        cams = discovery_v4l2.discover_cameras()

    assert [c.device_node for c in cams] == ["/dev/video0"]


def test_report_explains_missing_device_nodes():
    report = discovery.DiscoveryReport(cameras=[], rejected=[])
    with mock.patch("os.path.isdir", return_value=True):
        assert "no /dev/video* devices exist" in report.explain_empty()


def test_report_explains_permission_denied():
    report = discovery.DiscoveryReport(
        cameras=[], rejected=[("/dev/video0", "permission denied (is your user in the 'video' group?)")]
    )
    explanation = report.explain_empty()
    assert "/dev/video0" in explanation
    assert "video" in explanation


def test_report_explains_non_capture_nodes():
    report = discovery.DiscoveryReport(
        cameras=[], rejected=[("/dev/video1", "not a video-capture node (metadata/output/subdev)")]
    )
    assert "not a video-capture node" in report.explain_empty()


def test_report_has_no_explanation_when_cameras_found():
    report = discovery.DiscoveryReport(
        cameras=[
            discovery.CameraDevice(
                id="cam", device_node="/dev/video0", name="c", source="usb", driver="uvcvideo"
            )
        ]
    )
    assert report.explain_empty() == ""


def test_platform_dispatch_uses_v4l2_on_linux(monkeypatch):
    monkeypatch.setattr(discovery.sys, "platform", "linux")
    sentinel = discovery.DiscoveryReport(cameras=[])
    monkeypatch.setattr(discovery_v4l2, "discover_cameras_report", lambda: sentinel)
    assert discovery.discover_cameras_report() is sentinel


def test_platform_dispatch_uses_dshow_on_windows(monkeypatch):
    from lite_recorder import discovery_dshow

    monkeypatch.setattr(discovery.sys, "platform", "win32")
    sentinel = discovery.DiscoveryReport(cameras=[])
    calls = []

    def fake_report(ffmpeg_bin):
        calls.append(ffmpeg_bin)
        return sentinel

    monkeypatch.setattr(discovery_dshow, "discover_cameras_report", fake_report)
    assert discovery.discover_cameras_report(ffmpeg_bin="myffmpeg") is sentinel
    assert calls == ["myffmpeg"]
