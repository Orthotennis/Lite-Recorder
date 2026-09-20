"""CameraManager: owns camera discovery, per-camera workers, and
multi-camera recording sessions."""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from . import discovery
from .camera import CameraSettings, CameraStatus, CameraWorker, STATE_ERROR, STATE_RECORDING
from .config import CameraConfigStore, Settings
from .encoder import EncoderInfo, select_encoder

logger = logging.getLogger(__name__)

_SLUG_RE = re.compile(r"[^a-zA-Z0-9_-]+")

# Settings that change how the device itself is opened; anything else
# (label, enabled, bitrate) can be applied without restarting capture.
_CAPTURE_FIELDS = {"width", "height", "fps", "pixel_format"}


def slugify(label: str) -> str:
    slug = _SLUG_RE.sub("-", label.strip()).strip("-")
    return slug or "camera"


def _simulated_devices(count: int = 4) -> list[discovery.CameraDevice]:
    formats = [
        discovery.FrameFormat(pixel_format="MJPG", width=1280, height=720, framerates=[30.0]),
        discovery.FrameFormat(pixel_format="MJPG", width=640, height=480, framerates=[30.0]),
    ]
    devices = []
    for i in range(count):
        devices.append(
            discovery.CameraDevice(
                id=f"sim{i}",
                device_node=f"sim://{i}",
                name=f"Simulated Camera {i}",
                source="csi" if i < 2 else "usb",
                driver="simulate",
                formats=list(formats),
            )
        )
    return devices


class RecordingSession:
    def __init__(self, session_dir: Path, started_at: float):
        self.session_dir = session_dir
        self.started_at = started_at
        self.ended_at: float | None = None
        self.cameras: dict[str, dict] = {}

    def elapsed(self) -> float:
        end = self.ended_at or time.time()
        return end - self.started_at

    def write_manifest(self) -> None:
        manifest = {
            "started_at": datetime.fromtimestamp(self.started_at).isoformat(),
            "ended_at": datetime.fromtimestamp(self.ended_at).isoformat() if self.ended_at else None,
            "cameras": self.cameras,
        }
        (self.session_dir / "session.json").write_text(json.dumps(manifest, indent=2))


class CameraManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.config_store = CameraConfigStore(settings.cameras_config_path)
        self.encoder: EncoderInfo = select_encoder(settings.ffmpeg_bin, settings.force_encoder)
        self._lock = threading.RLock()
        self._workers: dict[str, CameraWorker] = {}
        self._devices: dict[str, discovery.CameraDevice] = {}
        self._session: RecordingSession | None = None
        self.rescan()

    # -- discovery / registry -------------------------------------------

    def _settings_for(self, device: discovery.CameraDevice) -> CameraSettings:
        stored = self.config_store.get(device.id)
        default_fmt = device.best_effort_default_format()
        return CameraSettings(
            label=stored.get("label", device.id),
            width=stored.get("width", default_fmt.width if default_fmt else 1280),
            height=stored.get("height", default_fmt.height if default_fmt else 720),
            fps=stored.get("fps", int(default_fmt.framerates[0]) if default_fmt and default_fmt.framerates else 30),
            pixel_format=stored.get("pixel_format", default_fmt.pixel_format if default_fmt else "MJPG"),
            bitrate_kbps=stored.get("bitrate_kbps", 4000),
            enabled=stored.get("enabled", True),
        )

    def rescan(self) -> None:
        with self._lock:
            if self.settings.simulate:
                devices = _simulated_devices()
            else:
                devices = discovery.discover_cameras(
                    known={d.device_node: d for d in self._devices.values()}
                )
            seen_ids = set()
            to_start: list[CameraWorker] = []
            for device in devices:
                seen_ids.add(device.id)
                cam_settings = self._settings_for(device)
                self._devices[device.id] = device
                worker = self._workers.get(device.id)
                if worker is None:
                    worker = CameraWorker(
                        device=device,
                        settings=cam_settings,
                        ffmpeg_bin=self.settings.ffmpeg_bin,
                        preview_width=self.settings.preview_width,
                        preview_fps=self.settings.preview_fps,
                        simulate=self.settings.simulate,
                    )
                    self._workers[device.id] = worker
                    to_start.append(worker)
                    continue

                # The stable id (by-id/by-path symlink) survives
                # replug/renumbering, but the underlying /dev/videoN it
                # resolves to can change (e.g. plugging in another camera
                # shifts kernel numbering) - refresh it, or the worker keeps
                # launching ffmpeg against a stale node that now belongs to
                # a different camera's process (surfacing as a persistent
                # "Device or resource busy").
                node_changed = worker.device.device_node != device.device_node
                worker.device = device
                worker.settings = cam_settings
                if worker.state == STATE_RECORDING:
                    continue
                if node_changed:
                    # Release the old node up front: two cameras can swap
                    # /dev/videoN, and re-opening one while the other still
                    # holds its former node fails with EBUSY.
                    worker.release()
                    to_start.append(worker)
                elif worker.state == STATE_ERROR:
                    to_start.append(worker)

            # Drop workers for cameras that disappeared (e.g. unplugged).
            for gone_id in set(self._workers) - seen_ids:
                self._workers[gone_id].stop()
                del self._workers[gone_id]
                del self._devices[gone_id]

            # Every reassigned node has been released by now, so these can
            # open their new nodes without racing each other.
            for worker in to_start:
                worker.start_preview()

    def list_cameras(self) -> list[CameraStatus]:
        with self._lock:
            return [w.status() for w in self._workers.values()]

    def update_camera(self, camera_id: str, patch: dict) -> CameraStatus:
        with self._lock:
            worker = self._workers.get(camera_id)
            if worker is None:
                raise KeyError(camera_id)
            allowed = {"label", "width", "height", "fps", "pixel_format", "bitrate_kbps", "enabled"}
            clean_patch = {k: v for k, v in patch.items() if k in allowed}
            changed = {
                k for k, v in clean_patch.items() if getattr(worker.settings, k) != v
            }
            self.config_store.update(camera_id, clean_patch)
            for key, value in clean_patch.items():
                setattr(worker.settings, key, value)
            # Only a capture-format change actually requires re-opening the
            # device. The UI PATCHes every field on any edit, so restarting
            # unconditionally tore down and re-opened a live camera just to
            # rename it - needless churn, and every re-open is a chance to
            # lose the race for the device node.
            needs_restart = bool(changed & _CAPTURE_FIELDS) or worker.state == STATE_ERROR
            if needs_restart and worker.state != STATE_RECORDING:
                worker.start_preview()
            return worker.status()

    def get_worker(self, camera_id: str) -> CameraWorker | None:
        with self._lock:
            return self._workers.get(camera_id)

    # -- recording sessions -----------------------------------------------

    def start_recording(self) -> dict:
        with self._lock:
            if self._session is not None:
                raise RuntimeError("recording already in progress")
            now = time.time()
            dt = datetime.fromtimestamp(now)
            session_dir = (
                self.settings.recordings_root / dt.strftime("%d-%m-%Y") / dt.strftime("%H-%M-%S")
            )
            session_dir.mkdir(parents=True, exist_ok=True)
            session = RecordingSession(session_dir, now)

            used_slugs: set[str] = set()
            results = []
            for camera_id, worker in self._workers.items():
                if not worker.settings.enabled:
                    continue
                slug = slugify(worker.settings.label)
                base_slug = slug
                n = 2
                while slug in used_slugs:
                    slug = f"{base_slug}-{n}"
                    n += 1
                used_slugs.add(slug)
                output_path = session_dir / f"{slug}.mp4"
                try:
                    worker.start_recording(output_path, self.encoder)
                    session.cameras[camera_id] = {
                        "label": worker.settings.label,
                        "file": output_path.name,
                        "device_node": worker.device.device_node,
                        "width": worker.settings.width,
                        "height": worker.settings.height,
                        "fps": worker.settings.fps,
                        "encoder": self.encoder.name,
                        "status": "recording",
                    }
                    results.append({"camera_id": camera_id, "ok": True})
                except Exception as exc:  # noqa: BLE001 - report, don't abort the take
                    logger.exception("camera %s failed to start recording", camera_id)
                    session.cameras[camera_id] = {
                        "label": worker.settings.label,
                        "status": "failed",
                        "error": str(exc),
                    }
                    results.append({"camera_id": camera_id, "ok": False, "error": str(exc)})

            session.write_manifest()
            self._session = session
            return {"session_dir": str(session_dir), "results": results}

    def stop_recording(self) -> dict:
        with self._lock:
            if self._session is None:
                raise RuntimeError("no recording in progress")
            session = self._session
            for camera_id, worker in self._workers.items():
                if camera_id in session.cameras and session.cameras[camera_id].get("status") == "recording":
                    worker.stop_recording()
                    status = worker.status()
                    session.cameras[camera_id]["status"] = "error" if status.state == STATE_ERROR else "complete"
                    if status.error:
                        session.cameras[camera_id]["error"] = status.error
            session.ended_at = time.time()
            session.write_manifest()
            self._session = None
            return {"session_dir": str(session.session_dir), "duration": session.elapsed()}

    def recording_status(self) -> dict:
        with self._lock:
            if self._session is None:
                return {"recording": False}
            return {
                "recording": True,
                "session_dir": str(self._session.session_dir),
                "started_at": self._session.started_at,
                "elapsed": self._session.elapsed(),
                "cameras": self._session.cameras,
            }

    def shutdown(self) -> None:
        with self._lock:
            for worker in self._workers.values():
                worker.stop()
