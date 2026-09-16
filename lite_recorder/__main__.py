"""Entrypoint: `python -m lite_recorder [--camera-mode M] [--host H] [--port P]`."""
from __future__ import annotations

import argparse
import logging
import os

import uvicorn

from .config import CAMERA_MODE_REAL, CAMERA_MODE_SIMULATE, CAMERA_MODES, Settings


def main() -> None:
    parser = argparse.ArgumentParser(prog="lite_recorder")
    parser.add_argument("--host", default=None, help="Bind host (default: env or 0.0.0.0)")
    parser.add_argument("--port", type=int, default=None, help="Bind port (default: env or 80)")
    parser.add_argument(
        "--camera-mode",
        choices=CAMERA_MODES,
        default=None,
        help=(
            "auto (default): record from real V4L2 cameras, falling back to "
            "synthetic ones only if none are found; real: only real cameras; "
            "simulate: only synthetic test-pattern cameras"
        ),
    )
    parser.add_argument(
        "--real",
        dest="camera_mode",
        action="store_const",
        const=CAMERA_MODE_REAL,
        help="Shorthand for --camera-mode real",
    )
    parser.add_argument(
        "--simulate",
        dest="camera_mode",
        action="store_const",
        const=CAMERA_MODE_SIMULATE,
        help="Shorthand for --camera-mode simulate",
    )
    parser.add_argument(
        "--recordings-root", default=None, help="Override recordings storage root"
    )
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()

    logging.basicConfig(level=args.log_level.upper())

    if args.camera_mode:
        os.environ["LITE_RECORDER_CAMERA_MODE"] = args.camera_mode
        # Keep the legacy env var consistent for anything reading it.
        os.environ["LITE_RECORDER_SIMULATE"] = (
            "1" if args.camera_mode == CAMERA_MODE_SIMULATE else "0"
        )
    if args.recordings_root:
        os.environ["LITE_RECORDER_RECORDINGS_ROOT"] = args.recordings_root

    settings = Settings()
    if args.host:
        settings.host = args.host
    if args.port:
        settings.port = args.port

    from .app import create_app

    app = create_app(settings)
    uvicorn.run(app, host=settings.host, port=settings.port, log_level=args.log_level)


if __name__ == "__main__":
    main()
