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
