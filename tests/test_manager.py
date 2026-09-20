import time

import pytest

from lite_recorder.config import Settings
from lite_recorder.manager import CameraManager, slugify


@pytest.fixture()
def manager(tmp_path, monkeypatch):
    monkeypatch.setenv("LITE_RECORDER_SIMULATE", "1")
    settings = Settings(
        recordings_root=tmp_path / "recordings",
        state_dir=tmp_path / "state",
        simulate=True,
    )
    settings.ensure_dirs()
    mgr = CameraManager(settings)
    yield mgr
    mgr.shutdown()


def test_slugify():
    assert slugify("Front Door") == "Front-Door"
    assert slugify("cam #1!!") == "cam-1"
    assert slugify("   ") == "camera"


def test_rescan_populates_simulated_cameras(manager):
    cams = manager.list_cameras()
    assert len(cams) == 4
    assert {c.state for c in cams} == {"preview"}


def test_update_camera_persists_label(manager):
    cams = manager.list_cameras()
    cam_id = cams[0].id
    updated = manager.update_camera(cam_id, {"label": "front-door", "enabled": True})
    assert updated.label == "front-door"
    # persisted to disk
    stored = manager.config_store.get(cam_id)
    assert stored["label"] == "front-door"


def test_update_camera_unknown_id_raises(manager):
    with pytest.raises(KeyError):
        manager.update_camera("does-not-exist", {"label": "x"})


def test_start_stop_recording_creates_session_dir_and_files(manager):
    result = manager.start_recording()
    assert all(r["ok"] for r in result["results"])
    session_dir = result["session_dir"]

    status = manager.recording_status()
    assert status["recording"] is True

    time.sleep(1.5)
    stop_result = manager.stop_recording()
    assert stop_result["session_dir"] == session_dir

    from pathlib import Path
    d = Path(session_dir)
    mp4s = list(d.glob("*.mp4"))
    assert len(mp4s) == 4
    assert (d / "session.json").exists()

    status_after = manager.recording_status()
    assert status_after["recording"] is False


def test_double_start_raises(manager):
    manager.start_recording()
    try:
        with pytest.raises(RuntimeError):
            manager.start_recording()
    finally:
        manager.stop_recording()


def test_stop_without_start_raises(manager):
    with pytest.raises(RuntimeError):
        manager.stop_recording()


def test_session_dedupes_duplicate_labels(manager):
    cams = manager.list_cameras()
    manager.update_camera(cams[0].id, {"label": "cam"})
    manager.update_camera(cams[1].id, {"label": "cam"})
    result = manager.start_recording()
    time.sleep(1.0)
    stop_result = manager.stop_recording()

    from pathlib import Path
    d = Path(result["session_dir"])
    names = sorted(p.name for p in d.glob("*.mp4"))
    assert "cam.mp4" in names
    assert "cam-2.mp4" in names


def _fake_device(dev_id="cam0"):
    from lite_recorder import discovery

    return discovery.CameraDevice(
        id=dev_id,
        device_node="/dev/video0",
        name="Fake Cam",
        source="usb",
        driver="uvcvideo",
        formats=[
            discovery.FrameFormat(pixel_format="MJPG", width=640, height=480, framerates=[30.0])
        ],
    )


def _manager_with_report(tmp_path, monkeypatch, mode, report):
    from lite_recorder import discovery

    monkeypatch.setattr(discovery, "discover_cameras_report", lambda: report)
    settings = Settings(
        recordings_root=tmp_path / "recordings",
        state_dir=tmp_path / "state",
        camera_mode=mode,
    )
    settings.ensure_dirs()
    return CameraManager(settings)


def test_auto_mode_uses_real_cameras_when_present(tmp_path, monkeypatch):
    from lite_recorder import discovery

    report = discovery.DiscoveryReport(cameras=[_fake_device()])
    mgr = _manager_with_report(tmp_path, monkeypatch, "auto", report)
    try:
        assert mgr.simulated is False
        assert mgr.camera_notice == ""
        assert [c.device_node for c in mgr.list_cameras()] == ["/dev/video0"]
        # The worker must capture from V4L2, not from a synthetic source.
        worker = mgr.get_worker("cam0")
        assert worker is not None and worker._simulate is False
    finally:
        mgr.shutdown()


def test_auto_mode_falls_back_to_simulated_with_a_reason(tmp_path, monkeypatch):
    from lite_recorder import discovery

    report = discovery.DiscoveryReport(
        cameras=[], rejected=[("/dev/video0", "permission denied (is your user in the 'video' group?)")]
    )
    mgr = _manager_with_report(tmp_path, monkeypatch, "auto", report)
    try:
        assert mgr.simulated is True
        assert "permission denied" in mgr.camera_notice
        assert len(mgr.list_cameras()) == 4
    finally:
        mgr.shutdown()


def test_real_mode_reports_no_cameras_instead_of_simulating(tmp_path, monkeypatch):
    from lite_recorder import discovery

    report = discovery.DiscoveryReport(cameras=[], rejected=[])
    mgr = _manager_with_report(tmp_path, monkeypatch, "real", report)
    try:
        assert mgr.simulated is False
        assert mgr.list_cameras() == []
        assert "No cameras found" in mgr.camera_notice
        with pytest.raises(RuntimeError):
            mgr.start_recording()
    finally:
        mgr.shutdown()


def test_simulate_mode_ignores_real_cameras(tmp_path, monkeypatch):
    from lite_recorder import discovery

    def _boom():
        raise AssertionError("simulate mode must not scan for real cameras")

    monkeypatch.setattr(discovery, "discover_cameras_report", _boom)
    settings = Settings(
        recordings_root=tmp_path / "recordings",
        state_dir=tmp_path / "state",
        camera_mode="simulate",
    )
    settings.ensure_dirs()
    mgr = CameraManager(settings)
    try:
        assert mgr.simulated is True
        assert len(mgr.list_cameras()) == 4
    finally:
        mgr.shutdown()
