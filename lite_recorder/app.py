"""FastAPI application: web UI + REST API + MJPEG preview streaming."""
from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.requests import Request

from . import recordings
from .camera import CameraStatus
from .config import Settings
from .manager import CameraManager

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent


def _camera_status_dict(status: CameraStatus) -> dict:
    return {
        "id": status.id,
        "label": status.label,
        "state": status.state,
        "error": status.error,
        "device_node": status.device_node,
        "source": status.source,
        "width": status.width,
        "height": status.height,
        "fps": status.fps,
        "recording_path": status.recording_path,
        "frame_count": status.frame_count,
        "last_frame_at": status.last_frame_at,
    }


class CameraPatch(BaseModel):
    label: str | None = None
    width: int | None = None
    height: int | None = None
    fps: int | None = None
    pixel_format: str | None = None
    bitrate_kbps: int | None = None
    enabled: bool | None = None


def create_app(settings: Settings) -> FastAPI:
    settings.ensure_dirs()
    manager = CameraManager(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        manager.shutdown()

    app = FastAPI(title="Lite-Recorder", lifespan=lifespan)
    templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
    app.mount(
        "/media",
        StaticFiles(directory=str(settings.recordings_root)),
        name="media",
    )

    app.state.manager = manager
    app.state.settings = settings

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        return templates.TemplateResponse(request, "index.html")

    @app.get("/api/system")
    def api_system():
        return {
            "encoder": manager.encoder.to_dict(),
            "simulate": settings.simulate,
            "recordings_root": str(settings.recordings_root),
            "camera_count": len(manager.list_cameras()),
        }

    @app.get("/api/cameras")
    def api_list_cameras():
        return [_camera_status_dict(s) for s in manager.list_cameras()]

    @app.patch("/api/cameras/{camera_id}")
    def api_update_camera(camera_id: str, patch: CameraPatch):
        try:
            status = manager.update_camera(camera_id, patch.model_dump(exclude_none=True))
        except KeyError:
            raise HTTPException(status_code=404, detail="camera not found")
        return _camera_status_dict(status)

    @app.post("/api/cameras/rescan")
    def api_rescan():
        manager.rescan()
        return [_camera_status_dict(s) for s in manager.list_cameras()]

    def _mjpeg_generator(camera_id: str):
        worker = manager.get_worker(camera_id)
        if worker is None:
            return
        boundary = b"--frame\r\n"
        while True:
            worker = manager.get_worker(camera_id)
            if worker is None:
                break
            frame = worker.latest_frame(timeout=2.0)
            if frame is None:
                time.sleep(0.1)
                continue
            yield boundary + b"Content-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"

    @app.get("/api/cameras/{camera_id}/stream")
    def api_camera_stream(camera_id: str):
        if manager.get_worker(camera_id) is None:
            raise HTTPException(status_code=404, detail="camera not found")
        return StreamingResponse(
            _mjpeg_generator(camera_id),
            media_type="multipart/x-mixed-replace; boundary=frame",
        )

    @app.get("/api/cameras/{camera_id}/snapshot.jpg")
    def api_camera_snapshot(camera_id: str):
        worker = manager.get_worker(camera_id)
        if worker is None:
            raise HTTPException(status_code=404, detail="camera not found")
        frame = worker.latest_frame(timeout=2.0)
        if frame is None:
            raise HTTPException(status_code=503, detail="no frame available yet")
        return StreamingResponse(iter([frame]), media_type="image/jpeg")

    @app.post("/api/recording/start")
    def api_start_recording():
        try:
            return manager.start_recording()
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    @app.post("/api/recording/stop")
    def api_stop_recording():
        try:
            return manager.stop_recording()
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    @app.get("/api/recording/status")
    def api_recording_status():
        return manager.recording_status()

    @app.get("/api/recordings")
    def api_list_recordings():
        sessions = recordings.list_sessions(settings.recordings_root)
        return [
            {
                "date": s.date,
                "time": s.time,
                "dir": s.dir_rel,
                "started_at": s.started_at,
                "ended_at": s.ended_at,
                "files": [
                    {
                        "camera_id": f.camera_id,
                        "label": f.label,
                        "filename": f.filename,
                        "url": f"/media/{f.path}",
                        "thumbnail_url": f"/api/recordings/thumbnail/{f.path}",
                        "size_bytes": f.size_bytes,
                        "status": f.status,
                        "error": f.error,
                    }
                    for f in s.files
                ],
            }
            for s in sessions
        ]

    @app.get("/api/recordings/thumbnail/{rel_path:path}")
    def api_recording_thumbnail(rel_path: str):
        video_path = (settings.recordings_root / rel_path).resolve()
        root = settings.recordings_root.resolve()
        if root not in video_path.parents or not video_path.is_file():
            raise HTTPException(status_code=404, detail="recording not found")
        thumb_path = settings.state_dir / "thumbnails" / (rel_path.replace("/", "_") + ".jpg")
        if not recordings.generate_thumbnail(settings.ffmpeg_bin, video_path, thumb_path):
            raise HTTPException(status_code=404, detail="thumbnail unavailable")
        return FileResponse(thumb_path, media_type="image/jpeg")

    @app.delete("/api/recordings/{rel_path:path}")
    def api_delete_recording(rel_path: str):
        try:
            recordings.delete_recording(settings.recordings_root, rel_path)
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return JSONResponse({"deleted": rel_path})

    return app
