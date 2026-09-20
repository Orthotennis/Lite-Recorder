from unittest import mock

from lite_recorder import discovery_dshow

_LIST_DEVICES_OUTPUT = r"""[dshow @ 0000020a1a2b3c40] DirectShow video devices (some may be both video and audio devices)
[dshow @ 0000020a1a2b3c40]  "Integrated Webcam"
[dshow @ 0000020a1a2b3c40]     Alternative name "@device_pnp_\\?\usb#vid_04f2&pid_b5eb&mi_00#7&1a2b3c4d&0&0000#{65e8773d-8f56-11d0-a3b9-00a0c9223196}\global"
[dshow @ 0000020a1a2b3c40]  "OBS Virtual Camera"
[dshow @ 0000020a1a2b3c40]     Alternative name "@device_sw_{860BB310-5D01-11D0-BD3B-00A0C911CE86}\{A3FCE0F5-3493-419F-958A-ABA1250EC20B}"
[dshow @ 0000020a1a2b3c40] DirectShow audio devices
[dshow @ 0000020a1a2b3c40]  "Microphone (Realtek Audio)"
dummy: Immediate exit requested
"""

_LIST_OPTIONS_OUTPUT = """[dshow @ 0000020a1a2b3c40]   vcodec=mjpeg  min s=640x480 fps=5 max s=1280x720 fps=30
[dshow @ 0000020a1a2b3c40]   pixel_format=yuyv422  min s=640x480 fps=5 max s=640x480 fps=30
video=Integrated Webcam: Immediate exit requested
"""


def _run(stdout="", stderr=""):
    result = mock.Mock()
    result.stdout = stdout
    result.stderr = stderr
    return result


def test_list_device_names_parses_video_section_only():
    with mock.patch("subprocess.run", return_value=_run(stderr=_LIST_DEVICES_OUTPUT)):
        devices = discovery_dshow._list_device_names("ffmpeg")

    assert [name for name, _alt in devices] == ["Integrated Webcam", "OBS Virtual Camera"]
    alt = dict(devices)["Integrated Webcam"]
    assert alt.startswith("@device_pnp_")
    assert "usb#" in alt


def test_probe_formats_parses_mjpeg_and_pixel_format_lines():
    with mock.patch("subprocess.run", return_value=_run(stderr=_LIST_OPTIONS_OUTPUT)):
        formats = discovery_dshow._probe_formats("ffmpeg", "Integrated Webcam")

    assert len(formats) == 2
    mjpeg = next(f for f in formats if f.pixel_format == "MJPG")
    assert (mjpeg.width, mjpeg.height) == (1280, 720)
    assert mjpeg.framerates == [30.0]
    yuyv = next(f for f in formats if f.pixel_format == "YUYV422")
    assert (yuyv.width, yuyv.height) == (640, 480)


def test_classify_source_detects_usb_from_alt_name():
    assert discovery_dshow._classify_source(r"@device_pnp_\?\usb#vid_04f2&pid_b5eb") == "usb"
    assert discovery_dshow._classify_source("@device_sw_{...}") == "unknown"
    assert discovery_dshow._classify_source("") == "unknown"


def test_probe_device_returns_none_without_formats():
    with mock.patch("subprocess.run", return_value=_run(stderr="video=Nope: Immediate exit requested\n")):
        cam = discovery_dshow.probe_device("Nope")
    assert cam is None


def test_probe_device_builds_camera_with_alt_name_as_id():
    with mock.patch("subprocess.run", return_value=_run(stderr=_LIST_OPTIONS_OUTPUT)):
        cam = discovery_dshow.probe_device(
            "Integrated Webcam",
            alt_name=r"@device_pnp_\?\usb#vid_04f2&pid_b5eb",
        )
    assert cam is not None
    assert cam.id == r"@device_pnp_\?\usb#vid_04f2&pid_b5eb"
    assert cam.device_node == "Integrated Webcam"
    assert cam.source == "usb"
    assert cam.driver == "dshow"


def test_discover_cameras_report_probes_every_listed_device():
    def fake_run(cmd, capture_output, text, timeout):
        if "-list_devices" in cmd:
            return _run(stderr=_LIST_DEVICES_OUTPUT)
        # OBS Virtual Camera reports no usable formats (e.g. blocked or busy).
        if "video=OBS Virtual Camera" in cmd:
            return _run(stderr="video=OBS Virtual Camera: Immediate exit requested\n")
        return _run(stderr=_LIST_OPTIONS_OUTPUT)

    with mock.patch("subprocess.run", side_effect=fake_run):
        report = discovery_dshow.discover_cameras_report("ffmpeg")

    assert [c.name for c in report.cameras] == ["Integrated Webcam"]
    assert len(report.rejected) == 1
    assert report.rejected[0][0] == "OBS Virtual Camera"


def test_discover_cameras_report_survives_ffmpeg_missing():
    with mock.patch("subprocess.run", side_effect=FileNotFoundError("no ffmpeg")):
        report = discovery_dshow.discover_cameras_report("ffmpeg")
    assert report.cameras == []
    assert report.rejected == []
