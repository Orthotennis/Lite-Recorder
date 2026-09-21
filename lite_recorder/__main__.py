"""Entrypoint: `python -m lite_recorder [--simulate] [--host H] [--port P]`."""
from __future__ import annotations

import argparse
import logging
import os

import uvicorn

from .config import Settings


def _list_devices() -> None:
    """Show what the hardware actually exposes.

    One physical camera can present several capture-capable /dev/videoN
    nodes, and only one of them can stream at a time - so a node listed
    under "also exposes" is *expected* to report "Device or resource busy"
    and is deliberately not opened. Run this on the box to confirm the
    camera count matches the cameras physically attached.
    """
    from . import discovery

    cameras = discovery.discover_cameras()
    if not cameras:
        print("No capture devices found under /dev/video*")
        return
    print(f"{len(cameras)} camera(s):\n")
    for cam in cameras:
        print(f"  {cam.id}")
        print(f"    name        {cam.name}  ({cam.source}, {cam.driver})")
        print(f"    capture on  {cam.device_node}")
        if cam.sibling_nodes:
            print(
                "    also exposes "
                + ", ".join(cam.sibling_nodes)
                + "  (same device - not opened)"
            )
        best = cam.best_effort_default_format()
        if best:
            print(f"    default     {best.pixel_format} {best.width}x{best.height}")
        for node in [cam.device_node, *cam.sibling_nodes]:
            holders = discovery.device_holders(node)
            if holders:
                print(f"    in use      {node} held by {', '.join(holders)}")
        print()


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
    parser.add_argument(
        "--list-devices",
        action="store_true",
        help="Print the cameras discovery finds, and the /dev/video* nodes "
        "it decided belong to each one, then exit",
    )
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

    if args.list_devices:
        _list_devices()
        return

    from .app import create_app

    app = create_app(settings)
    uvicorn.run(app, host=settings.host, port=settings.port, log_level=args.log_level)


if __name__ == "__main__":
    main()
