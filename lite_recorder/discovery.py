"""Camera discovery: dispatches to the platform-appropriate backend.

Linux talks to V4L2 directly via raw ioctls (`discovery_v4l2.py`);
Windows asks ffmpeg to enumerate DirectShow devices (`discovery_dshow.py`).
Both backends report results through the same CameraDevice / FrameFormat /
DiscoveryReport types (re-exported here from `discovery_types.py`), so
nothing above this module — camera.py, manager.py, the web API — ever has
to know which platform or capture API it's actually talking to.
"""
from __future__ import annotations

import sys

from .discovery_types import CameraDevice, DiscoveryReport, FrameFormat

__all__ = [
    "CameraDevice",
    "DiscoveryReport",
    "FrameFormat",
    "discover_cameras",
    "discover_cameras_report",
    "probe_device",
]


def _is_windows() -> bool:
    return sys.platform.startswith("win")


def discover_cameras_report(ffmpeg_bin: str = "ffmpeg") -> DiscoveryReport:
    """Enumerate every camera the current platform can see, returning the
    capture devices found plus the reason each other candidate was
    skipped. `ffmpeg_bin` is only used on Windows, where ffmpeg itself
    does the enumeration."""
    if _is_windows():
        from . import discovery_dshow

        return discovery_dshow.discover_cameras_report(ffmpeg_bin)
    from . import discovery_v4l2

    return discovery_v4l2.discover_cameras_report()


def discover_cameras(ffmpeg_bin: str = "ffmpeg") -> list[CameraDevice]:
    """Enumerate every camera the current platform can see and return the
    subset that are genuine, currently-usable capture devices."""
    return discover_cameras_report(ffmpeg_bin).cameras


def probe_device(identifier: str, ffmpeg_bin: str = "ffmpeg") -> CameraDevice | None:
    """Probe a single device: a /dev/videoN node on Linux, or a
    DirectShow device name on Windows."""
    if _is_windows():
        from . import discovery_dshow

        return discovery_dshow.probe_device(identifier, ffmpeg_bin=ffmpeg_bin)
    from . import discovery_v4l2

    return discovery_v4l2.probe_device(identifier)
