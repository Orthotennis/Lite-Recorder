import pytest

from lite_recorder.config import CameraConfigStore, Settings


def test_settings_defaults(monkeypatch):
    monkeypatch.delenv("LITE_RECORDER_RECORDINGS_ROOT", raising=False)
    s = Settings()
    assert str(s.recordings_root) == "/var/lib/lite-recorder/recordings"
    assert s.port == 80


def test_settings_env_override(monkeypatch):
    monkeypatch.setenv("LITE_RECORDER_PORT", "9000")
    monkeypatch.setenv("LITE_RECORDER_SIMULATE", "1")
    s = Settings()
    assert s.port == 9000
    assert s.simulate is True


def test_camera_config_store_roundtrip(tmp_path):
    path = tmp_path / "cameras.json"
    store = CameraConfigStore(path)
    store.update("cam1", {"label": "Front Door", "fps": 24})
    assert store.get("cam1") == {"label": "Front Door", "fps": 24}

    # reload from disk
    store2 = CameraConfigStore(path)
    assert store2.get("cam1") == {"label": "Front Door", "fps": 24}


def test_camera_config_store_missing_id_returns_empty(tmp_path):
    store = CameraConfigStore(tmp_path / "cameras.json")
    assert store.get("nope") == {}


def test_camera_config_store_handles_corrupt_file(tmp_path):
    path = tmp_path / "cameras.json"
    path.write_text("not json{{{")
    store = CameraConfigStore(path)
    assert store.all() == {}


def test_camera_mode_defaults_to_auto(monkeypatch):
    monkeypatch.delenv("LITE_RECORDER_CAMERA_MODE", raising=False)
    monkeypatch.delenv("LITE_RECORDER_SIMULATE", raising=False)
    assert Settings().camera_mode == "auto"


def test_legacy_simulate_env_still_selects_simulate_mode(monkeypatch):
    monkeypatch.delenv("LITE_RECORDER_CAMERA_MODE", raising=False)
    monkeypatch.setenv("LITE_RECORDER_SIMULATE", "1")
    settings = Settings()
    assert settings.camera_mode == "simulate"
    assert settings.simulate is True


def test_camera_mode_env_overrides_legacy_simulate(monkeypatch):
    monkeypatch.setenv("LITE_RECORDER_CAMERA_MODE", "real")
    monkeypatch.setenv("LITE_RECORDER_SIMULATE", "1")
    settings = Settings()
    assert settings.camera_mode == "real"
    assert settings.simulate is False


def test_simulate_kwarg_forces_simulate_mode(monkeypatch):
    monkeypatch.setenv("LITE_RECORDER_CAMERA_MODE", "auto")
    settings = Settings(simulate=True)
    assert settings.camera_mode == "simulate"


def test_invalid_camera_mode_rejected():
    with pytest.raises(ValueError):
        Settings(camera_mode="nonsense")
