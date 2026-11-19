"""Worker healthcheck: ``python -m cairn_worker.healthcheck``.

Exit status 0 when the liveness file exists and is younger than 30 seconds, 1 otherwise
(Requirement 8.6). Standard library only, so the check starts fast and works in the runtime
image without importing DBOS. The decision is a pure function of (now, stamp, max age).
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Final

ALIVE_FILE: Final = Path("/tmp/cairn-worker/alive")  # noqa: S108
MAX_AGE_S: Final = 30.0


def is_fresh(now: float, stamp: float | None, max_age_s: float = MAX_AGE_S) -> bool:
    """True when ``stamp`` exists and is strictly younger than ``max_age_s`` at ``now``.

    ``stamp`` is the file's modification time in seconds since the epoch, or ``None`` when the
    file is missing. A stamp slightly in the future (clock skew) counts as fresh.
    """
    if stamp is None:
        return False
    return (now - stamp) < max_age_s


def read_stamp(path: Path) -> float | None:
    """Modification time of ``path``, or ``None`` if it is missing or unreadable."""
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


def main(
    argv: Sequence[str] | None = None,
    *,
    path: Path = ALIVE_FILE,
    clock: Callable[[], float] = time.time,
) -> int:
    """Return the process exit status. ``argv`` is accepted for symmetry and ignored."""
    del argv
    stamp = read_stamp(path)
    if is_fresh(clock(), stamp):
        return 0
    reason = "liveness file missing" if stamp is None else "liveness file is stale"
    print(f"unhealthy: {reason}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
