"""Container healthcheck: ``python -m cairn_api.healthcheck`` (design §4).

Exit 0 when ``GET /readyz`` answers 200, exit 1 otherwise. Uses only the standard library,
because the runtime image has no dev dependencies (no httpx). The URL defaults to the
container's own port and can be overridden with ``CAIRN_API_HEALTHCHECK_URL``. When
``CAIRN_PROXY_TOKEN`` is set the request carries it in ``X-Cairn-Proxy-Token`` (FT-16); the
token is never printed.
"""

from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request
from collections.abc import Sequence
from typing import Final

DEFAULT_URL: Final = "http://127.0.0.1:8000/readyz"
TIMEOUT_S: Final = 5.0
ENV_KEY: Final = "CAIRN_PROXY_TOKEN"
HEADER_NAME: Final = "X-Cairn-Proxy-Token"


def check(
    url: str = DEFAULT_URL, timeout_s: float = TIMEOUT_S, proxy_token: str | None = None
) -> bool:
    """True when ``url`` answers 200. Any HTTP error, connection error or timeout is False.

    ``proxy_token``, when given and not empty, is sent as ``X-Cairn-Proxy-Token``."""
    if not url.startswith(("http://", "https://")):
        return False
    request = urllib.request.Request(url, method="GET")  # noqa: S310
    if proxy_token:
        request.add_header(HEADER_NAME, proxy_token)
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:  # noqa: S310
            return bool(response.status == 200)
    except (urllib.error.URLError, OSError, ValueError):
        # HTTPError (503 and friends) is a URLError subclass.
        return False


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    url = args[0] if args else os.environ.get("CAIRN_API_HEALTHCHECK_URL", DEFAULT_URL)
    ok = check(url, proxy_token=os.environ.get(ENV_KEY))
    if not ok:
        print("unhealthy: /readyz did not return 200", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
