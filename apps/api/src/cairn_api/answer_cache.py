"""A small in-process answer cache keyed by who is asking (plan 3.9, 3.21; layer L7).

The key is the normalized question, the principal set (the sorted group ids the snapshot holds) and
the ACL version. Two consequences: an answer cached for one principal set is never served to
another, and any permission sync, content apply or deletion purge changes the version and so
invalidates every entry. Entries expire after a TTL and the cache is bounded (LRU). It lives in
memory of one process only: nothing is written to disk and nothing is shared between processes.
"""

from __future__ import annotations

import hashlib
import time
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CachedAnswer:
    body: dict[str, Any]
    returned_item_ids: tuple[int, ...]
    model: str | None


class AnswerCache:
    def __init__(
        self, size: int, ttl_s: float, clock: Callable[[], float] = time.monotonic
    ) -> None:
        if size < 1:
            raise ValueError("size must be at least 1")
        self._size, self._ttl, self._clock = size, ttl_s, clock
        self._items: OrderedDict[str, tuple[float, CachedAnswer]] = OrderedDict()

    @staticmethod
    def key(question: str, groups: Sequence[int], acl_version: int) -> str:
        """Hash of the normalized question, the sorted principal set and the ACL version."""
        normalized = " ".join(question.casefold().split())
        material = f"{normalized}\x1f{','.join(map(str, sorted(set(groups))))}\x1f{acl_version}"
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def get(self, key: str) -> CachedAnswer | None:
        entry = self._items.get(key)
        if entry is None:
            return None
        stored_at, value = entry
        if self._clock() - stored_at > self._ttl:
            del self._items[key]
            return None
        self._items.move_to_end(key)
        return value

    def put(self, key: str, value: CachedAnswer) -> None:
        self._items[key] = (self._clock(), value)
        self._items.move_to_end(key)
        while len(self._items) > self._size:
            self._items.popitem(last=False)

    def __len__(self) -> int:
        return len(self._items)
