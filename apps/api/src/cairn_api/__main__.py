"""Serve the API: ``python -m cairn_api`` or the ``cairn-api`` console script.

Runs uvicorn with the ``create_app`` factory, its access log off (the app logs one JSON line per
request itself) and ``log_config=None``, so uvicorn installs no logging config of its own and its
records propagate to the root JSON handler that ``create_app`` sets up. The CLI flag
``--log-config none`` from design §7 does not exist in uvicorn 0.54: the option takes a file path.

The default bind address is 127.0.0.1. Nothing in Phase 0 is authenticated, so exposing the
port is an explicit choice: the container sets ``CAIRN_API_HOST=0.0.0.0`` (or passes
``--host 0.0.0.0``) and compose publishes the port on 127.0.0.1 only.
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Sequence

import uvicorn

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
APP = "cairn_api.main:create_app"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="cairn-api", description="Serve the Cairn API.")
    parser.add_argument("--host", default=os.environ.get("CAIRN_API_HOST", DEFAULT_HOST))
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("CAIRN_API_PORT", DEFAULT_PORT))
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    uvicorn.run(
        APP,
        factory=True,
        host=args.host,
        port=args.port,
        log_config=None,
        access_log=False,
        server_header=False,
    )


if __name__ == "__main__":
    main()
