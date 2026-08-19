"""Configuration and persisted paths for Lite-Recorder."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from threading import Lock
from typing import Any


def _env_path(name: str, default: str) -> Path:
    return Path(os.environ.get(name, default))


@dataclass
class Settings:
    """Runtime settings, sourced from environment variables (with sane
    defaults for local/dev use). All paths are created on first access."""

    recordings_root: Path = field(
        default_factory=lambda: _env_path(
            "LITE_RECORDER_RECORDINGS_ROOT", "/var/lib/lite-recorder/recordings"
        )
    )
    state_dir: Path = field(
        default_factory=lambda: _env_path(
            "LITE_RECORDER_STATE_DIR", "/var/lib/lite-recorder"
        )
    )
    host: str = field(default_factory=lambda: os.environ.get("LITE_RECORDER_HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: int(os.environ.get("LITE_RECORDER_PORT", "80")))
    simulate: bool = field(
        default_factory=lambda: os.environ.get("LITE_RECORDER_SIMULATE", "") == "1"
    )
    force_encoder: str | None = field(
        default_factory=lambda: os.environ.get("LITE_RECORDER_FORCE_ENCODER") or None
    )
    preview_width: int = 640
    preview_fps: int = 10
    ffmpeg_bin: str = field(default_factory=lambda: os.environ.get("FFMPEG_BIN", "ffmpeg"))
    ffprobe_bin: str = field(default_factory=lambda: os.environ.get("FFPROBE_BIN", "ffprobe"))

    @property
    def cameras_config_path(self) -> Path:
        return self.state_dir / "cameras.json"

    def ensure_dirs(self) -> None:
        self.recordings_root.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)


class CameraConfigStore:
    """Persists per-camera settings (label, resolution, fps, bitrate,
    enabled) to a JSON file, keyed by the camera's stable id."""

    def __init__(self, path: Path):
        self._path = path
        self._lock = Lock()
        self._data: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            try:
                self._data = json.loads(self._path.read_text())
            except (json.JSONDecodeError, OSError):
                self._data = {}
        else:
            self._data = {}

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._data, indent=2, sort_keys=True))
        tmp.replace(self._path)

    def get(self, camera_id: str) -> dict[str, Any]:
        with self._lock:
            return dict(self._data.get(camera_id, {}))

    def update(self, camera_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            entry = dict(self._data.get(camera_id, {}))
            entry.update(patch)
            self._data[camera_id] = entry
            self._save()
            return dict(entry)

    def all(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {k: dict(v) for k, v in self._data.items()}

