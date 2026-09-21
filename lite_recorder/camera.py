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
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import encoder as encoder_mod
from .discovery import CameraDevice, FrameFormat, device_holders

logger = logging.getLogger(__name__)

_SOI = b"\xff\xd8"
_EOI = b"\xff\xd9"

STATE_IDLE = "idle"
STATE_PREVIEW = "preview"
STATE_RECORDING = "recording"
STATE_ERROR = "error"

_STUCK_MSG = (
    "ffmpeg could not be stopped and still holds {node}; the camera is "
    "wedged (replug it, or reset its USB port). Retrying until it frees up."
)


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
        # Serializes the whole terminate-then-launch sequence. Without it a
        # retry timer that has already started running (so _cancel_retry can
        # no longer stop it) can race a rescan/user action and put two ffmpeg
        # processes on the same /dev/videoN - the loser dies with "Device or
        # resource busy" and schedules another retry, looping indefinitely.
        self._spawn_lock = threading.RLock()
        self._spawn_seq = 0
        self._closed = False
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

        # ffmpeg processes that survived SIGKILL, with the node each
        # still pins, so nothing is started on that node meanwhile.
        self._unkillable: list[tuple[subprocess.Popen, str]] = []
        self._retry_timer: threading.Timer | None = None
        self._retry_delay = 2.0
        self._retry_delay_max = 30.0

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

    def release(self) -> bool:
        """Terminate the ffmpeg process and free the device node, leaving
        the worker reusable (used when a camera's /dev/videoN is being
        reassigned and must be released before anything re-opens it).

        Returns True when the node is confirmed free. Never raises - a
        caller walking every worker must not be derailed by one of them.
        """
        with self._spawn_lock:
            self._stopping = True
            self._cancel_retry()
            released = self._terminate_process()
            with self._lock:
                self._recording_path = None
                if released:
                    self._state = STATE_IDLE
                    self._error = ""
                else:
                    self._state = STATE_ERROR
                    self._error = _STUCK_MSG.format(node=self.device.device_node)
            return released

    def stop(self) -> bool:
        """Permanently stop this camera's ffmpeg process."""
        with self._spawn_lock:
            self._closed = True
            return self.release()

    def mark_blocked(self) -> None:
        """Report that this camera's node is pinned by an ffmpeg that
        would not die, so capture was deliberately not started."""
        with self._lock:
            self._state = STATE_ERROR
            self._error = _STUCK_MSG.format(node=self.device.device_node)

    # -- internals ------------------------------------------------------

    def _cancel_retry(self) -> None:
        if self._retry_timer is not None:
            self._retry_timer.cancel()
            self._retry_timer = None

    def _spawn(
        self,
        record_path: Path | None,
        encoder: encoder_mod.EncoderInfo | None = None,
        is_retry: bool = False,
        retry_seq: int | None = None,
    ) -> None:
        with self._spawn_lock:
            self._spawn_locked(record_path, encoder, is_retry, retry_seq)

    def _spawn_locked(
        self,
        record_path: Path | None,
        encoder: encoder_mod.EncoderInfo | None,
        is_retry: bool,
        retry_seq: int | None,
    ) -> None:
        if self._closed:
            return
        self._reap_unkillable()
        with self._lock:
            # A retry timer that had already started running when a newer
            # spawn took over must not launch a second ffmpeg on this node.
            if retry_seq is not None and retry_seq != self._spawn_seq:
                logger.debug("camera %s: discarding superseded retry", self.device.id)
                return
            self._spawn_seq += 1
        self._stopping = False
        self._cancel_retry()
        if not is_retry:
            self._retry_delay = 2.0
        # Either our current ffmpeg refuses to die, or an earlier one is
        # still pinning this node (release() already cleared _proc, so
        # terminating alone would report success). Launching a second
        # capture either way can only produce "Device or resource busy",
        # so report the real reason and wait for the node to free up.
        if not self._terminate_process() or self.device.device_node in self.stuck_nodes():
            with self._lock:
                self._state = STATE_ERROR
                self._error = _STUCK_MSG.format(node=self.device.device_node)
            self._schedule_retry()
            return

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
        try:
            proc.wait()
        except Exception:  # noqa: BLE001 - a dead watchdog means no retry, ever
            logger.exception(
                "camera %s: watchdog failed waiting on ffmpeg", self.device.id
            )
            return
        should_retry = False
        with self._lock:
            if self._proc is not proc:
                return  # superseded by a newer process
            if self._stopping:
                self._state = STATE_IDLE
                return
            if proc.returncode not in (0, None):
                self._state = STATE_ERROR
                self._error = "\n".join(self._stderr_tail) or f"ffmpeg exited with code {proc.returncode}"
                if "busy" in self._error.lower():
                    self._error += self._busy_hint()
                logger.warning("camera %s: ffmpeg exited unexpectedly: %s", self.device.id, self._error)
                # Only auto-retry preview failures (e.g. a transient "Device
                # or resource busy" while another process is still releasing
                # the node) - never silently restart a failed recording.
                should_retry = self._recording_path is None
        if should_retry:
            self._schedule_retry()

    def _busy_hint(self) -> str:
        """Name whoever is holding the node, so "Device or resource busy"
        points at a process instead of being a dead end.

        A holder of a *sibling* node counts: the other capture paths of one
        physical camera share its hardware, so streaming through any one of
        them is enough to make this one report EBUSY - while /proc shows
        nothing at all holding this node. Reporting that as "the device is
        wedged" is what makes this error look like a lifecycle bug in the
        app when it is really two nodes of one camera.
        """
        node = self.device.device_node
        try:
            mine = str(os.getpid())
            holders = [h for h in device_holders(node) if h.split()[0] != mine]
            siblings = {
                sibling: [h for h in device_holders(sibling) if h.split()[0] != mine]
                for sibling in self.device.sibling_nodes
            }
        except Exception:  # noqa: BLE001 - diagnostics must never mask the real error
            return ""
        if holders:
            return f"\n({node} is held by: {', '.join(holders)})"
        busy_siblings = {s: h for s, h in siblings.items() if h}
        if busy_siblings:
            detail = "; ".join(f"{s} by {', '.join(h)}" for s, h in busy_siblings.items())
            return (
                f"\n(nothing holds {node} itself, but it is another capture path "
                f"of the same physical camera as {detail} - one device cannot "
                f"stream through two paths at once)"
            )
        return f"\n(nothing else holds {node}; the device or its USB link is wedged)"

    def _schedule_retry(self) -> None:
        delay = self._retry_delay
        self._retry_delay = min(self._retry_delay * 2, self._retry_delay_max)
        with self._lock:
            seq = self._spawn_seq
        logger.info("camera %s: retrying preview in %.0fs", self.device.id, delay)
        timer = threading.Timer(delay, self._retry_preview, args=(seq,))
        timer.daemon = True
        self._retry_timer = timer
        timer.start()

    def _retry_preview(self, seq: int) -> None:
        if self._stopping or self._closed:
            return
        with self._lock:
            if self._state != STATE_ERROR or seq != self._spawn_seq:
                return
        self._spawn(record_path=None, is_retry=True, retry_seq=seq)

    @staticmethod
    def _wait_for_exit(proc: subprocess.Popen) -> bool:
        try:
            proc.wait(timeout=5)
            return True
        except subprocess.TimeoutExpired:
            return False
        except OSError:
            return proc.poll() is not None

    def _signal(self, proc: subprocess.Popen, action) -> None:
        try:
            action()
        except OSError as exc:
            logger.debug("camera %s: signalling ffmpeg failed: %s", self.device.id, exc)

    def _terminate_process(self) -> bool:
        """Stop the current ffmpeg and free its device node. Returns True
        only when the node is confirmed released.

        This never raises. ffmpeg blocked in an uninterruptible USB ioctl
        on a wedged camera survives even SIGKILL, and the old code let the
        resulting TimeoutExpired escape - which aborted whichever bulk
        operation was walking the workers (rescan, shutdown) partway
        through, leaving the registry half-updated with two workers bound
        to one /dev/videoN. The loser of that fight then reported "Device
        or resource busy" forever.
        """
        with self._lock:
            proc = self._proc
            self._proc = None
        if proc is None:
            return True
        if proc.poll() is not None:
            return True

        if proc.stdin:
            try:
                proc.stdin.write(b"q")
                proc.stdin.flush()
            except (BrokenPipeError, OSError):
                pass
        if self._wait_for_exit(proc):
            return True

        logger.warning("camera %s: ffmpeg did not exit gracefully, sending SIGTERM", self.device.id)
        self._signal(proc, proc.terminate)
        if self._wait_for_exit(proc):
            return True

        logger.warning("camera %s: ffmpeg ignored SIGTERM, sending SIGKILL", self.device.id)
        self._signal(proc, proc.kill)
        if self._wait_for_exit(proc):
            return True

        # Unkillable. The kernel keeps the node open until that ioctl
        # returns, so remember it rather than pretending the node is free:
        # anything started on it now could only fail with EBUSY.
        node = self.device.device_node
        logger.error(
            "camera %s: ffmpeg (pid %s) survived SIGKILL and still holds %s",
            self.device.id,
            proc.pid,
            node,
        )
        with self._lock:
            self._unkillable.append((proc, node))
        return False

    def _reap_unkillable(self) -> None:
        """Drop processes that have since exited, freeing their nodes."""
        with self._lock:
            self._unkillable = [(p, n) for p, n in self._unkillable if p.poll() is None]

    def stuck_nodes(self) -> set[str]:
        """Device nodes still pinned by an ffmpeg that would not die."""
        self._reap_unkillable()
        with self._lock:
            return {node for _, node in self._unkillable}
