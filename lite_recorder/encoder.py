"""H.264 encoder selection and ffmpeg command construction.

Prefers the RK3588 hardware encoder (h264_rkmpp), falls back to the
generic V4L2 M2M encoder, then to software libx264 — and validates
whatever gets picked with a tiny real encode so a present-but-broken
hardware path doesn't silently break every recording. The chosen
encoder's status is surfaced to the UI via EncoderInfo so a software
fallback is never silent.
"""
from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_HARDWARE_ENCODERS = ["h264_rkmpp", "h264_v4l2m2m"]
_SOFTWARE_ENCODER = "libx264"


@dataclass
class EncoderInfo:
    name: str
    kind: str  # "hardware" | "software"
    degraded: bool
    reason: str = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "kind": self.kind, "degraded": self.degraded, "reason": self.reason}


def _list_available_encoders(ffmpeg_bin: str) -> set[str]:
    try:
        result = subprocess.run(
            [ffmpeg_bin, "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("could not list ffmpeg encoders: %s", exc)
        return set()
    names = set()
    for line in result.stdout.splitlines():
        parts = line.split()
        # lines look like " V..... h264_rkmpp    Rockchip MPP ..."
        if len(parts) >= 2 and parts[0].startswith(("V", "A", "S")):
            names.add(parts[1])
    return names


def _validate_encoder(ffmpeg_bin: str, encoder: str) -> tuple[bool, str]:
    """Run a 1-second synthetic encode to confirm the encoder actually
    works (a listed encoder can still fail to open the underlying
    hardware device, e.g. no /dev/mpp_service or no permissions)."""
    cmd = [
        ffmpeg_bin,
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "testsrc=size=320x240:rate=10:duration=1",
        "-c:v",
        encoder,
        "-f",
        "null",
        "-",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    if result.returncode != 0:
        return False, result.stderr.strip().splitlines()[-1] if result.stderr else "unknown error"
    return True, ""


def select_encoder(ffmpeg_bin: str = "ffmpeg", force: str | None = None) -> EncoderInfo:
    """Pick the best working H.264 encoder. `force` (from config/env) can
    pin a specific encoder, e.g. for testing the degraded-banner path."""
    available = _list_available_encoders(ffmpeg_bin)

    candidates = [force] if force else _HARDWARE_ENCODERS + [_SOFTWARE_ENCODER]
    tried: list[str] = []
    for name in candidates:
        if available and name not in available:
            continue
        ok, reason = _validate_encoder(ffmpeg_bin, name)
        if ok:
            kind = "hardware" if name in _HARDWARE_ENCODERS else "software"
            degraded = kind == "software"
            degrade_reason = ""
            if degraded:
                if tried:
                    degrade_reason = (
                        f"Hardware encoder(s) unavailable ({', '.join(tried)}); "
                        f"using software encoder {name}. Concurrent-camera capacity is reduced."
                    )
                else:
                    degrade_reason = (
                        f"No hardware encoder detected; using software encoder {name}. "
                        "Concurrent-camera capacity is reduced."
                    )
            return EncoderInfo(name=name, kind=kind, degraded=degraded, reason=degrade_reason)
        tried.append(name)

    # Nothing validated — fall back to libx264 name anyway so the app can
    # still start and report a clear per-camera error when ffmpeg fails.
    return EncoderInfo(
        name=_SOFTWARE_ENCODER,
        kind="software",
        degraded=True,
        reason=f"No working encoder found (tried: {', '.join(tried) or candidates}); "
        "recordings will likely fail until this is resolved.",
    )


def build_input_args(device: str, pixel_format: str, width: int, height: int, fps: int, simulate: bool) -> list[str]:
    if simulate:
        return [
            "-f",
            "lavfi",
            "-i",
            f"testsrc=size={width}x{height}:rate={fps}",
        ]
    fmt = "mjpeg" if pixel_format == "MJPG" else "yuyv422"
    return [
        "-f",
        "v4l2",
        "-input_format",
        fmt,
        "-video_size",
        f"{width}x{height}",
        "-framerate",
        str(fps),
        "-i",
        device,
    ]


def build_preview_args(preview_width: int, preview_fps: int) -> list[str]:
    return [
        "-map",
        "0:v",
        "-vf",
        f"scale={preview_width}:-2",
        "-r",
        str(preview_fps),
        "-f",
        "mjpeg",
        "-q:v",
        "7",
        "pipe:1",
    ]


def build_record_args(encoder: EncoderInfo, fps: int, bitrate_kbps: int, output_path: str) -> list[str]:
    return [
        "-map",
        "0:v",
        "-c:v",
        encoder.name,
        "-b:v",
        f"{bitrate_kbps}k",
        "-g",
        str(max(1, fps * 2)),
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        output_path,
    ]


def build_ffmpeg_command(
    ffmpeg_bin: str,
    device: str,
    pixel_format: str,
    width: int,
    height: int,
    fps: int,
    preview_width: int,
    preview_fps: int,
    simulate: bool,
    encoder: EncoderInfo | None = None,
    output_path: str | None = None,
    bitrate_kbps: int = 4000,
) -> list[str]:
    """Build the full ffmpeg command. When `encoder`/`output_path` are
    given, the command tees to both the MP4 file and the MJPEG preview
    pipe; otherwise it is preview-only."""
    # Deliberately not -nostdin: CameraWorker sends 'q' on stdin for a
    # graceful stop so the MP4 moov atom is finalized before exit.
    cmd = [ffmpeg_bin, "-hide_banner", "-loglevel", "error"]
    cmd += build_input_args(device, pixel_format, width, height, fps, simulate)
    if encoder is not None and output_path is not None:
        cmd += build_record_args(encoder, fps, bitrate_kbps, output_path)
    cmd += build_preview_args(preview_width, preview_fps)
    return cmd
