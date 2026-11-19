"""Which database hosts count as local (PD1-29).

Commands that fill a database (``cairn-import``, ``cairn-s1 bench``, ``cairn-eval``) refuse a
host that is not a loopback address or a compose service name unless ``--allow-remote`` is
given, so a wrong ``.env`` can never fill Neon's free 1 GB. Same rules as the spike guard
(free-tier FT-28): loopback addresses, ``localhost``, ``db`` and ``ft-db``.
"""

from __future__ import annotations

import ipaddress
from typing import Final

LOCAL_HOST_NAMES: Final = frozenset({"localhost"})
COMPOSE_SERVICE_NAMES: Final = frozenset({"db", "ft-db"})
ALLOW_REMOTE_FLAG: Final = "--allow-remote"
REMOTE_EXIT_STATUS: Final = 2


def is_local_host(host: str) -> bool:
    """True when ``host`` is a loopback address, ``localhost`` or a compose service name."""
    name = host.lower()
    if name in LOCAL_HOST_NAMES or name in COMPOSE_SERVICE_NAMES:
        return True
    if name.startswith("[") and name.endswith("]"):
        name = name[1:-1]
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def require_local_or_allowed(command: str, host: str, *, allow_remote: bool) -> None:
    """Raise ``SystemExit(2)`` with a message unless ``host`` is local or ``allow_remote``.

    Runs before any connection is opened. A host name is not a secret, so it may be printed.
    """
    if allow_remote or is_local_host(host):
        return
    raise SystemExit(
        f"{command}: refusing database host {host!r}: it is not a loopback address or a compose "
        f"service name; pass {ALLOW_REMOTE_FLAG} to run against it anyway (exit {REMOTE_EXIT_STATUS})"  # noqa: E501
    )
