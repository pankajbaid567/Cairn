"""Spike host guard (free-tier retarget FT-28, Requirement 3.8).

``cairn-s1 load`` and ``cairn-s2 run`` create and fill databases. One accidental run against a
hosted database (Neon free has 1 GB) would fill it, so both commands refuse a database host
that is neither a loopback address nor a compose service name unless ``--allow-remote`` is
given. They check before opening any connection and exit with status 2.

Accepted without the flag:

- loopback IP addresses: ``127.0.0.0/8`` and ``::1``, also written in brackets (``[::1]``);
- ``localhost``;
- the compose service names ``db`` (core profile) and ``ft-db`` (freetier profile).

Names compare case-insensitively, as DNS does. Anything else is refused, including an empty
host, a socket directory and a comma-separated host list.
"""

from __future__ import annotations

import ipaddress
from typing import Final

LOCAL_HOST_NAMES: Final = frozenset({"localhost"})
COMPOSE_SERVICE_NAMES: Final = frozenset({"db", "ft-db"})
ALLOW_REMOTE_FLAG: Final = "--allow-remote"


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


def host_allowed(host: str, *, allow_remote: bool) -> bool:
    """The guard: any host with ``--allow-remote``, otherwise only a local one."""
    return allow_remote or is_local_host(host)


def refusal(command: str, host: str) -> str:
    """The message printed when the guard refuses ``host`` (a host name is not a secret)."""
    return (
        f"{command}: refusing database host {host!r}: it is not a loopback address or a "
        f"compose service name ({', '.join(sorted(COMPOSE_SERVICE_NAMES))}); spike commands "
        f"create and fill databases, so pass {ALLOW_REMOTE_FLAG} to run against it anyway"
    )
