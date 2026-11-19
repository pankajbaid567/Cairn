"""Fixed-window rate limits for the demo deployment (PD1-26; Property 29).

Counters live in memory, keyed by ``HMAC-SHA-256(audit_hash_key, client address)[:16]``, where the
client address is the first ``X-Forwarded-For`` entry the Pages Function sets. Client addresses
and keys never reach logs, spans or the audit log. A global counter caps answers per UTC day.
The limiter is inactive outside the demo deployment and resets when the process restarts.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True)
class Decision:
    allowed: bool
    retry_after_s: int = 0


class FixedWindowLimiter:
    def __init__(self, key: bytes, *, clock: Callable[[], float] = time.time) -> None:
        self._key = key
        self._clock = clock
        self._windows: dict[tuple[str, str, int], int] = {}

    def client_key(self, forwarded_for: str | None, fallback: str = "unknown") -> str:
        first = (forwarded_for or fallback).split(",")[0].strip() or fallback
        return hmac.new(self._key, first.encode(), hashlib.sha256).hexdigest()[:16]

    def check(self, client: str, bucket: str, limit: int, window_s: int) -> Decision:
        """Admit the request if fewer than ``limit`` were admitted in the current window."""
        now = self._clock()
        index = int(now // window_s)
        self._windows = {
            k: v for k, v in self._windows.items() if k[2] >= index - 1 or k[1] == "day"
        }
        key = (client, bucket, index)
        if self._windows.get(key, 0) >= limit:
            return Decision(False, max(1, int((index + 1) * window_s - now)))
        self._windows[key] = self._windows.get(key, 0) + 1
        return Decision(True)

    def check_daily(self, bucket: str, limit: int) -> Decision:
        """A global per-UTC-day counter (answers per day)."""
        now = datetime.fromtimestamp(self._clock(), UTC)
        day = int(now.strftime("%Y%m%d"))
        key = ("*", "day:" + bucket, day)
        self._windows = {
            k: v for k, v in self._windows.items() if not k[1].startswith("day:") or k[2] == day
        }
        if self._windows.get(key, 0) >= limit:
            tomorrow = datetime.fromtimestamp(self._clock(), UTC).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            return Decision(False, max(1, int(86400 - (now - tomorrow).total_seconds())))
        self._windows[key] = self._windows.get(key, 0) + 1
        return Decision(True)
