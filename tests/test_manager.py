import time

import pytest

from lite_recorder import manager as manager_mod
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


def test_rescan_refreshes_stale_device_node(manager, monkeypatch):
    """A camera's stable id (by-id/by-path) survives replug, but the
    /dev/videoN it resolves to can change (e.g. plugging in another
    camera shifts kernel numbering). rescan() must update the worker's
    device, or it keeps launching ffmpeg against a stale node that may
    now belong to a different camera's already-running process."""
    cams = manager.list_cameras()
    cam_id = cams[0].id
    worker = manager.get_worker(cam_id)
    original_node = worker.device.device_node

    original_simulated = manager_mod._simulated_devices

    def renumbered(count: int = 4):
        devices = original_simulated(count)
        for device in devices:
            if device.id == cam_id:
                device.device_node = original_node + "-renumbered"
        return devices

    monkeypatch.setattr(manager_mod, "_simulated_devices", renumbered)
    manager.rescan()

    assert worker.device.device_node == original_node + "-renumbered"


def test_rescan_releases_node_before_reassigning_it(manager, monkeypatch):
    """Two cameras can swap /dev/videoN across a replug. The worker losing
    a node must release it before the worker gaining it re-opens it, or the
    second open fails with EBUSY."""
    cams = manager.list_cameras()
    a_id, b_id = cams[0].id, cams[1].id
    worker_a, worker_b = manager.get_worker(a_id), manager.get_worker(b_id)
    node_a, node_b = worker_a.device.device_node, worker_b.device.device_node

    events = []
    for worker, name in ((worker_a, "a"), (worker_b, "b")):
        monkeypatch.setattr(
            worker, "release", lambda n=name: events.append(("release", n))
        )
        monkeypatch.setattr(
            worker, "start_preview", lambda n=name: events.append(("start", n))
        )

    original_simulated = manager_mod._simulated_devices

    def swapped(count: int = 4):
        devices = original_simulated(count)
        for device in devices:
            if device.id == a_id:
                device.device_node = node_b
            elif device.id == b_id:
                device.device_node = node_a
        return devices

    monkeypatch.setattr(manager_mod, "_simulated_devices", swapped)
    manager.rescan()

    assert worker_a.device.device_node == node_b
    assert worker_b.device.device_node == node_a
    # Both nodes released before either is re-opened.
    assert events.index(("release", "a")) < events.index(("start", "a"))
    assert events.index(("release", "a")) < events.index(("start", "b"))
    assert events.index(("release", "b")) < events.index(("start", "a"))
    assert events.index(("release", "b")) < events.index(("start", "b"))


def test_rescan_does_not_restart_recording_camera(manager, monkeypatch):
    cams = manager.list_cameras()
    cam_id = cams[0].id
    worker = manager.get_worker(cam_id)
    node = worker.device.device_node

    monkeypatch.setattr(worker, "_state", "recording")
    calls = []
    monkeypatch.setattr(worker, "release", lambda: calls.append("release"))
    monkeypatch.setattr(worker, "start_preview", lambda: calls.append("start"))

    original_simulated = manager_mod._simulated_devices

    def renumbered(count: int = 4):
        devices = original_simulated(count)
        for device in devices:
            if device.id == cam_id:
                device.device_node = node + "-renumbered"
        return devices

    monkeypatch.setattr(manager_mod, "_simulated_devices", renumbered)
    manager.rescan()

    assert calls == []


def test_update_camera_label_does_not_restart_capture(manager, monkeypatch):
    """The UI PATCHes every field on any edit; renaming a live camera must
    not tear down and re-open its device."""
    cams = manager.list_cameras()
    cam_id = cams[0].id
    worker = manager.get_worker(cam_id)
    monkeypatch.setattr(worker, "_state", "preview")

    restarts = []
    monkeypatch.setattr(worker, "start_preview", lambda: restarts.append(1))

    manager.update_camera(
        cam_id,
        {
            "label": "renamed",
            "width": worker.settings.width,
            "height": worker.settings.height,
            "fps": worker.settings.fps,
            "bitrate_kbps": 4000,
            "enabled": True,
        },
    )
    assert restarts == []
    assert worker.settings.label == "renamed"

    manager.update_camera(cam_id, {"width": worker.settings.width + 160})
    assert len(restarts) == 1


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
