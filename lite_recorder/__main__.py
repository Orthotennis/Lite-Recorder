"""Entrypoint: `python -m lite_recorder [--simulate] [--host H] [--port P]`."""
from __future__ import annotations

import argparse
import logging
import os

import uvicorn

from .config import Settings


def main() -> None:
    parser = argparse.ArgumentParser(prog="lite_recorder")
    parser.add_argument("--host", default=None, help="Bind host (default: env or 0.0.0.0)")
    parser.add_argument("--port", type=int, default=None, help="Bind port (default: env or 80)")
    parser.add_argument(
        "--simulate",
        action="store_true",
        help="Use synthetic test-pattern cameras instead of real V4L2 devices",
    )
    parser.add_argument(
        "--recordings-root", default=None, help="Override recordings storage root"
    )
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()

    logging.basicConfig(level=args.log_level.upper())

    if args.simulate:
        os.environ["LITE_RECORDER_SIMULATE"] = "1"
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
