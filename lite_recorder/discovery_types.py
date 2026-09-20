"""Shared, platform-neutral camera discovery data types.

Both the Linux V4L2 backend (`discovery_v4l2.py`) and the Windows
DirectShow backend (`discovery_dshow.py`) report results using these
types, so nothing above `discovery.py` -- camera.py, manager.py, the web
API -- ever has to know which platform it's running on.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field


@dataclass
class FrameFormat:
    pixel_format: str
    width: int
    height: int
    framerates: list[float] = field(default_factory=list)


@dataclass
class CameraDevice:
    id: str  # stable identity: V4L2 by-id symlink, or DirectShow device path
    device_node: str  # V4L2 device node (/dev/videoN) or DirectShow device name
    name: str  # human-readable name
    source: str  # "csi" | "usb" | "unknown"
    driver: str
    formats: list[FrameFormat] = field(default_factory=list)

    def best_effort_default_format(self) -> FrameFormat | None:
        # Prefer MJPEG (saves USB bandwidth with many webcams), then
        # highest resolution among the remaining formats.
        if not self.formats:
            return None
        mjpeg = [f for f in self.formats if f.pixel_format == "MJPG"]
        candidates = mjpeg or self.formats
        return max(candidates, key=lambda f: f.width * f.height)


@dataclass
class DiscoveryReport:
    """What a scan of the system's cameras found, including why any
    candidate devices were skipped.

    Kept alongside the camera list so the app can explain an empty result
    ("no devices at all" vs "found one but couldn't open it") instead of
    just showing an empty grid. `explain_empty()` phrases that reason for
    whichever platform/backend actually ran the scan.
    """

    cameras: list[CameraDevice] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)

    @property
    def nodes_seen(self) -> int:
        return len(self.cameras) + len(self.rejected)

    def explain_empty(self) -> str:
        """A one-line, actionable reason why no cameras were found."""
        if self.cameras:
            return ""
        if sys.platform.startswith("win"):
            return self._explain_empty_windows()
        return self._explain_empty_linux()

    def _explain_empty_linux(self) -> str:
        if not self.rejected:
            if not os.path.isdir("/dev"):
                return "no /dev directory — V4L2 capture is Linux-only."
            return (
                "no /dev/video* devices exist on this machine. Plug in a USB "
                "webcam (on WSL, attach it with usbipd; on a Rock 5B+, enable "
                "the CSI overlay with rsetup)."
            )
        if all("permission denied" in reason for _node, reason in self.rejected):
            return (
                "found "
                + ", ".join(node for node, _ in self.rejected)
                + " but could not open them: permission denied. Add your user to "
                "the 'video' group (sudo usermod -aG video $USER) and log back in."
            )
        details = "; ".join(f"{node}: {reason}" for node, reason in self.rejected)
        return f"no capture-capable camera among the /dev/video* nodes ({details})."

    def _explain_empty_windows(self) -> str:
        if not self.rejected:
            return (
                "no DirectShow video devices found. Plug in a USB webcam, or "
                "check Device Manager if a laptop's built-in camera should be "
                "present but isn't listed."
            )
        if all("privacy" in reason for _node, reason in self.rejected):
            return (
                "found "
                + ", ".join(node for node, _ in self.rejected)
                + " but could not open them. Check Windows Settings > Privacy "
                "& security > Camera and allow desktop apps to access the "
                "camera (and that no other app has it open)."
            )
        details = "; ".join(f"{node}: {reason}" for node, reason in self.rejected)
        return f"no capture-capable camera among the DirectShow devices found ({details})."
