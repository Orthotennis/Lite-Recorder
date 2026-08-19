"""CameraWorker: owns the single ffmpeg process for one camera.

A V4L2 device can only be opened by one process, so preview and
recording are not separate processes — one ffmpeg process always tees
its capture to a live MJPEG pipe (for the preview grid) and, while
recording, simultaneously writes the MP4 file. Starting/stopping a
recording therefore means restarting this process with a different
command line (a ~1s preview gap is acceptable).
"""
from __future__ import annotations

import collections
import logging
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import encoder as encoder_mod
from .discovery import CameraDevice, FrameFormat

logger = logging.getLogger(__name__)

_SOI = b"\xff\xd8"
_EOI = b"\xff\xd9"

STATE_IDLE = "idle"
STATE_PREVIEW = "preview"
STATE_RECORDING = "recording"
STATE_ERROR = "error"


@dataclass
class CameraSettings:
    label: str
    width: int = 1280
    height: int = 720
    fps: int = 30
    pixel_format: str = "MJPG"
    bitrate_kbps: int = 4000
    enabled: bool = True


@dataclass
class CameraStatus:
    id: str
    label: str
    state: str
    error: str = ""
    device_node: str = ""
    source: str = "unknown"
    width: int = 0
    height: int = 0
    fps: int = 0
    recording_path: str = ""
    frame_count: int = 0
    last_frame_at: float = 0.0


class CameraWorker:
    """Runs and supervises the ffmpeg process for a single camera."""

    def __init__(
        self,
        device: CameraDevice,
        settings: CameraSettings,
        ffmpeg_bin: str,
        preview_width: int,
        preview_fps: int,
        simulate: bool = False,
    ):
        self.device = device
        self.settings = settings
        self._ffmpeg_bin = ffmpeg_bin
        self._preview_width = preview_width
        self._preview_fps = preview_fps
        self._simulate = simulate

        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._stdout_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._latest_frame: bytes | None = None
        self._frame_condition = threading.Condition()
        self._stderr_tail: collections.deque[str] = collections.deque(maxlen=20)

        self._state = STATE_IDLE
        self._error = ""
        self._recording_path: str | None = None
        self._frame_count = 0
        self._last_frame_at = 0.0
        self._stopping = False

    # -- public API ---------------------------------------------------

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def status(self) -> CameraStatus:
        with self._lock:
            return CameraStatus(
                id=self.device.id,
                label=self.settings.label,
                state=self._state,
                error=self._error,
                device_node=self.device.device_node,
                source=self.device.source,
                width=self.settings.width,
                height=self.settings.height,
                fps=self.settings.fps,
                recording_path=self._recording_path or "",
                frame_count=self._frame_count,
                last_frame_at=self._last_frame_at,
            )

    def latest_frame(self, timeout: float = 2.0) -> bytes | None:
        with self._frame_condition:
            if self._latest_frame is None:
                self._frame_condition.wait(timeout=timeout)
            return self._latest_frame

    def start_preview(self) -> None:
        self._spawn(record_path=None)

    def start_recording(self, output_path: Path, encoder: encoder_mod.EncoderInfo) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        self._spawn(record_path=output_path, encoder=encoder)

    def stop_recording(self) -> None:
        """Drop back to preview-only (keeps the camera live)."""
        self._spawn(record_path=None)

    def stop(self) -> None:
        """Fully stop this camera's ffmpeg process."""
        self._stopping = True
        self._terminate_process()
        with self._lock:
            self._state = STATE_IDLE
            self._recording_path = None

    # -- internals ------------------------------------------------------

    def _spawn(self, record_path: Path | None, encoder: encoder_mod.EncoderInfo | None = None) -> None:
        self._stopping = False
        self._terminate_process()

        fmt = self._select_format()
        cmd = encoder_mod.build_ffmpeg_command(
            ffmpeg_bin=self._ffmpeg_bin,
            device=self.device.device_node,
            pixel_format=fmt.pixel_format if fmt else self.settings.pixel_format,
            width=self.settings.width,
            height=self.settings.height,
            fps=self.settings.fps,
            preview_width=self._preview_width,
            preview_fps=self._preview_fps,
            simulate=self._simulate,
            encoder=encoder,
            output_path=str(record_path) if record_path else None,
            bitrate_kbps=self.settings.bitrate_kbps,
        )
        logger.info("camera %s: starting ffmpeg: %s", self.device.id, " ".join(cmd))
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
        except OSError as exc:
            with self._lock:
                self._state = STATE_ERROR
                self._error = f"failed to launch ffmpeg: {exc}"
            return

        with self._lock:
            self._proc = proc
            self._state = STATE_RECORDING if record_path else STATE_PREVIEW
            self._error = ""
            self._recording_path = str(record_path) if record_path else None
            self._frame_count = 0
            self._stderr_tail.clear()

        self._stdout_thread = threading.Thread(
            target=self._read_stdout, args=(proc,), daemon=True
        )
        self._stderr_thread = threading.Thread(
            target=self._read_stderr, args=(proc,), daemon=True
        )
        self._stdout_thread.start()
        self._stderr_thread.start()

        watchdog = threading.Thread(target=self._watch_exit, args=(proc,), daemon=True)
        watchdog.start()

    def _select_format(self) -> FrameFormat | None:
        for f in self.device.formats:
            if (
                f.pixel_format == self.settings.pixel_format
                and f.width == self.settings.width
                and f.height == self.settings.height
            ):
                return f
        return self.device.best_effort_default_format()

    def _read_stdout(self, proc: subprocess.Popen) -> None:
        buf = b""
        stdout = proc.stdout
        if stdout is None:
            return
        while True:
            chunk = stdout.read(4096)
            if not chunk:
                break
            buf += chunk
            while True:
                start = buf.find(_SOI)
                if start == -1:
                    buf = b""
                    break
                end = buf.find(_EOI, start + 2)
                if end == -1:
                    if start > 0:
                        buf = buf[start:]
                    break
                frame = buf[start : end + 2]
                buf = buf[end + 2 :]
                with self._frame_condition:
                    self._latest_frame = frame
                    self._frame_condition.notify_all()
                with self._lock:
                    self._frame_count += 1
                    self._last_frame_at = time.time()

    def _read_stderr(self, proc: subprocess.Popen) -> None:
        stderr = proc.stderr
        if stderr is None:
            return
        for line in iter(stderr.readline, b""):
            text = line.decode(errors="replace").rstrip()
            if text:
                self._stderr_tail.append(text)

    def _watch_exit(self, proc: subprocess.Popen) -> None:
        proc.wait()
        with self._lock:
            if self._proc is not proc:
                return  # superseded by a newer process
            if self._stopping:
                self._state = STATE_IDLE
                return
            if proc.returncode not in (0, None):
                self._state = STATE_ERROR
                self._error = "\n".join(self._stderr_tail) or f"ffmpeg exited with code {proc.returncode}"
                logger.warning("camera %s: ffmpeg exited unexpectedly: %s", self.device.id, self._error)

    def _terminate_process(self) -> None:
        proc = self._proc
        if proc is None:
            return
        self._proc = None
        if proc.poll() is not None:
            return
        try:
            if proc.stdin:
                try:
                    proc.stdin.write(b"q")
                    proc.stdin.flush()
                except (BrokenPipeError, OSError):
                    pass
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            logger.warning("camera %s: ffmpeg did not exit gracefully, sending SIGTERM", self.device.id)
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                logger.warning("camera %s: ffmpeg ignored SIGTERM, sending SIGKILL", self.device.id)
                proc.kill()
                proc.wait(timeout=5)
