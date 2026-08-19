"""Gallery: lists past recording sessions from the filesystem.

The filesystem layout (recordings/<DD-MM-YYYY>/<HH-MM-SS>/<label>.mp4
+ session.json) is itself the source of truth — the UI reads it back
directly rather than keeping a separate database, so recordings stay
browsable and correct even if the app was reinstalled or the media was
moved to another machine.
"""
from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class RecordingFile:
    camera_id: str
    label: str
    filename: str
    path: str  # path relative to recordings_root, for URL building
    size_bytes: int
    status: str = "complete"
    error: str = ""


@dataclass
class Session:
    date: str  # DD-MM-YYYY
    time: str  # HH-MM-SS
    dir_rel: str  # "<date>/<time>" relative to recordings_root
    started_at: str | None
    ended_at: str | None
    files: list[RecordingFile] = field(default_factory=list)


def list_sessions(recordings_root: Path) -> list[Session]:
    sessions: list[Session] = []
    if not recordings_root.is_dir():
        return sessions
    for date_dir in sorted(recordings_root.iterdir(), reverse=True):
        if not date_dir.is_dir():
            continue
        for time_dir in sorted(date_dir.iterdir(), reverse=True):
            if not time_dir.is_dir():
                continue
            manifest_path = time_dir / "session.json"
            started_at = ended_at = None
            camera_meta: dict = {}
            if manifest_path.exists():
                try:
                    manifest = json.loads(manifest_path.read_text())
                    started_at = manifest.get("started_at")
                    ended_at = manifest.get("ended_at")
                    camera_meta = manifest.get("cameras", {})
                except (json.JSONDecodeError, OSError):
                    pass

            meta_by_filename = {v.get("file"): (k, v) for k, v in camera_meta.items() if v.get("file")}
            files = []
            for mp4 in sorted(time_dir.glob("*.mp4")):
                camera_id, meta = meta_by_filename.get(mp4.name, (mp4.stem, {}))
                files.append(
                    RecordingFile(
                        camera_id=camera_id,
                        label=meta.get("label", mp4.stem),
                        filename=mp4.name,
                        path=f"{date_dir.name}/{time_dir.name}/{mp4.name}",
                        size_bytes=mp4.stat().st_size,
                        status=meta.get("status", "complete"),
                        error=meta.get("error", ""),
                    )
                )
            if not files:
                continue
            sessions.append(
                Session(
                    date=date_dir.name,
                    time=time_dir.name,
                    dir_rel=f"{date_dir.name}/{time_dir.name}",
                    started_at=started_at,
                    ended_at=ended_at,
                    files=files,
                )
            )
    return sessions


def delete_recording(recordings_root: Path, rel_path: str) -> None:
    target = (recordings_root / rel_path).resolve()
    root = recordings_root.resolve()
    if root not in target.parents:
        raise ValueError("path escapes recordings root")
    if not target.is_file():
        raise FileNotFoundError(rel_path)
    target.unlink()


def generate_thumbnail(ffmpeg_bin: str, video_path: Path, thumb_path: Path) -> bool:
    if thumb_path.exists():
        return True
    thumb_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg_bin,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        "0.5",
        "-i",
        str(video_path),
        "-frames:v",
        "1",
        "-vf",
        "scale=320:-2",
        str(thumb_path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("thumbnail generation failed for %s: %s", video_path, exc)
        return False
    return result.returncode == 0 and thumb_path.exists()
