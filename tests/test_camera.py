"""CameraWorker process-lifecycle tests.

These focus on the invariant that matters most on a multi-camera rig: a
worker must never have two ffmpeg processes open on its /dev/videoN at
once. The second one loses with "Device or resource busy", goes to ERROR,
and schedules another retry - a self-inflicted loop that gets more likely
the more cameras (and therefore more in-flight retries and rescans) there
are.
"""
import subprocess
import threading
import time
from unittest import mock

import pytest

from lite_recorder.camera import STATE_ERROR, CameraSettings, CameraWorker
from lite_recorder.discovery import CameraDevice, FrameFormat


@pytest.fixture()
def worker():
    device = CameraDevice(
        id="usb-Cam-A-video-index0",
        device_node="/dev/video0",
        name="Cam A",
        source="usb",
        driver="uvcvideo",
        formats=[FrameFormat("MJPG", 1280, 720, [30.0])],
    )
    w = CameraWorker(
        device=device,
        settings=CameraSettings(label="cam-a"),
        ffmpeg_bin="ffmpeg",
        preview_width=640,
        preview_fps=10,
    )
    yield w
    w.stop()


class _FakeStdin:
    """ffmpeg exits when CameraWorker writes 'q' for a graceful stop."""

    def __init__(self, proc):
        self._proc = proc

    def write(self, _data):
        self._proc._finish()

    def flush(self):
        pass


class FakeProc:
    """Stands in for a running ffmpeg, and tracks how many are alive.

    Like the real thing it stays alive until it is told to stop, so
    wait() blocks - which is what lets these tests observe two processes
    being alive on the same device at once.
    """

    live = 0
    max_live = 0
    lock = threading.Lock()
    pid = 4242

    def __init__(self, *_args, **_kwargs):
        self.returncode = None
        self.stdin = _FakeStdin(self)
        self.stdout = None
        self.stderr = None
        self._exited = threading.Event()
        with FakeProc.lock:
            FakeProc.live += 1
            FakeProc.max_live = max(FakeProc.max_live, FakeProc.live)
        # Give a racing spawn a window to be observed as overlapping.
        time.sleep(0.02)

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if not self._exited.wait(timeout=timeout):
            raise subprocess.TimeoutExpired("ffmpeg", timeout or 0)
        return self.returncode

    def terminate(self):
        self._finish()

    def kill(self):
        self._finish()

    def _finish(self):
        with FakeProc.lock:
            if self.returncode is None:
                self.returncode = 0
                FakeProc.live -= 1
        self._exited.set()
        return self.returncode


@pytest.fixture(autouse=True)
def _reset_fakeproc():
    FakeProc.live = 0
    FakeProc.max_live = 0
    yield


def test_concurrent_spawns_never_overlap_on_the_device(worker):
    """Several threads racing to (re)start the same camera - as a retry
    timer and an HTTP-triggered rescan do - must be serialized."""
    with mock.patch("subprocess.Popen", FakeProc):
        threads = [threading.Thread(target=worker.start_preview) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    assert FakeProc.max_live == 1


def test_superseded_retry_is_discarded(worker):
    """A retry timer that had already begun running when a newer spawn
    took over must not launch a second ffmpeg on the node."""
    with mock.patch("subprocess.Popen", FakeProc):
        worker.start_preview()
        stale_seq = worker._spawn_seq - 1
        with worker._lock:
            worker._state = STATE_ERROR

        worker._retry_preview(stale_seq)
        assert FakeProc.max_live == 1

        # The current sequence number is still honoured.
        worker._retry_preview(worker._spawn_seq)
        assert FakeProc.max_live == 1
        assert FakeProc.live == 1


def test_stop_prevents_later_spawns(worker):
    """A retry firing after shutdown must not resurrect the process and
    leave an orphan ffmpeg holding the device node."""
    with mock.patch("subprocess.Popen", FakeProc):
        worker.start_preview()
        worker.stop()
        assert FakeProc.live == 0

        worker.start_preview()
        worker._retry_preview(worker._spawn_seq)

    assert FakeProc.live == 0


def test_release_frees_the_node_but_allows_restart(worker):
    with mock.patch("subprocess.Popen", FakeProc):
        worker.start_preview()
        assert FakeProc.live == 1

        worker.release()
        assert FakeProc.live == 0

        worker.start_preview()
        assert FakeProc.live == 1


class WedgedProc(FakeProc):
    """An ffmpeg blocked in an uninterruptible USB ioctl on a wedged
    camera: it ignores 'q', SIGTERM and SIGKILL, so every wait() times
    out. The kernel keeps its /dev/videoN open until that ioctl returns.
    """

    def _finish(self):
        return None

    def wait(self, timeout=None):
        raise subprocess.TimeoutExpired("ffmpeg", timeout or 0)


def test_release_never_raises_when_ffmpeg_cannot_be_killed(worker):
    """Teardown that throws is what corrupted the registry: it escaped
    through release()/stop() and aborted whichever loop (rescan,
    shutdown) was walking the workers, part-way through."""
    with mock.patch("subprocess.Popen", WedgedProc):
        worker.start_preview()
        assert worker.release() is False          # reported, not raised
    assert worker.state == STATE_ERROR
    assert "/dev/video0" in worker.status().error
    # The node is known to be still pinned, so nothing may reopen it.
    assert worker.stuck_nodes() == {"/dev/video0"}


def test_no_second_ffmpeg_is_started_on_a_pinned_node(worker):
    """Starting another capture on a node our own unkillable ffmpeg still
    holds could only ever fail with "Device or resource busy"."""
    with mock.patch("subprocess.Popen", WedgedProc):
        worker.start_preview()
        worker.release()
        before = WedgedProc.live
        worker.start_preview()
        assert WedgedProc.live == before, "spawned a doomed second ffmpeg"
    assert worker.state == STATE_ERROR


def test_worker_recovers_once_the_wedged_process_finally_exits(worker):
    """The node frees up when the stuck ioctl returns; the camera must
    come back by itself rather than staying in error forever."""
    with mock.patch("subprocess.Popen", WedgedProc):
        worker.start_preview()
        worker.release()
        stuck = worker._unkillable[0][0]
    assert worker.stuck_nodes() == {"/dev/video0"}

    stuck.returncode = 0                           # the ioctl returned
    assert worker.stuck_nodes() == set()
    with mock.patch("subprocess.Popen", FakeProc):
        worker.start_preview()
    assert worker.state == "preview"


# --- EBUSY caused by another node of the SAME physical camera ------------


def _worker_with_siblings(siblings):
    device = CameraDevice(
        id="rkisp_mainpath",
        device_node="/dev/video0",
        name="rkisp_mainpath",
        source="csi",
        driver="rkisp",
        formats=[FrameFormat("NV12", 1920, 1080, [30.0])],
        physical_key="sysfs:/sys/devices/platform/rkisp-vir0",
        sibling_nodes=siblings,
    )
    return CameraWorker(
        device=device,
        settings=CameraSettings(label="cam-a"),
        ffmpeg_bin="ffmpeg",
        preview_width=640,
        preview_fps=10,
    )


def test_busy_hint_names_a_sibling_node_holding_the_same_device():
    """/proc shows nothing holding *this* node, because the conflict is on
    another capture path of the same camera. Reporting that as "the device
    is wedged" is what makes this look like an app lifecycle bug."""
    worker = _worker_with_siblings(["/dev/video1"])
    holders = {"/dev/video0": [], "/dev/video1": ["991 (ffmpeg)"]}

    with mock.patch("lite_recorder.camera.device_holders", side_effect=lambda n: holders[n]):
        hint = worker._busy_hint()

    assert "/dev/video1" in hint
    assert "991 (ffmpeg)" in hint
    assert "same physical camera" in hint
    assert "wedged" not in hint


def test_busy_hint_still_reports_a_direct_holder_first():
    worker = _worker_with_siblings(["/dev/video1"])
    holders = {"/dev/video0": ["42 (ffmpeg)"], "/dev/video1": ["991 (ffmpeg)"]}

    with mock.patch("lite_recorder.camera.device_holders", side_effect=lambda n: holders[n]):
        hint = worker._busy_hint()

    assert "/dev/video0 is held by: 42 (ffmpeg)" in hint


def test_busy_hint_falls_back_when_nothing_holds_any_node():
    worker = _worker_with_siblings(["/dev/video1"])

    with mock.patch("lite_recorder.camera.device_holders", return_value=[]):
        hint = worker._busy_hint()

    assert "wedged" in hint
