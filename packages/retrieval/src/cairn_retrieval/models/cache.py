"""On-disk cache of embeddings and rerank scores for laptop tools (design §14; PD1-27).

One sqlite3 file in WAL mode. Keys are ``(model_key, kind, sha256(text))`` (and the query hash for
rerank scores); values are float32 bytes or a float. Text is never stored. The API and the
combined process never open it. ``read_only`` makes no provider call: a miss raises
:class:`CacheMiss`, so a cached run cannot reach the network by construction.
"""

from __future__ import annotations

import hashlib
import sqlite3
import struct
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from cairn_retrieval.models.protocols import Embedder, Reranker


class CacheMiss(Exception):
    """Code ``cache_miss``. Names the model key and the count, never text."""

    code = "cache_miss"

    def __init__(self, model_key: str, count: int) -> None:
        super().__init__(f"cache_miss model={model_key} missing={count}")
        self.model_key = model_key
        self.count = count


def _h(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ModelCache:
    def __init__(self, path: str | Path) -> None:
        p = Path(path)
        if str(path) != ":memory:":
            p.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path))
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS vec (model TEXT NOT NULL, kind TEXT NOT NULL, "
            "h TEXT NOT NULL, v BLOB NOT NULL, PRIMARY KEY (model, kind, h))"
        )
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS score (model TEXT NOT NULL, q TEXT NOT NULL, "
            "p TEXT NOT NULL, s REAL NOT NULL, PRIMARY KEY (model, q, p))"
        )
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    def forget_texts(self, texts: Sequence[str]) -> int:
        """Remove every cached vector and rerank score for these texts (deletion purge, plan 3.11).

        Vectors are keyed by the text hash, scores by the passage hash, so deleted content cannot
        linger in the laptop cache. Returns the number of rows removed.
        """
        removed = 0
        for text in texts:
            h = _h(text)
            removed += self._db.execute("DELETE FROM vec WHERE h = ?", (h,)).rowcount
            removed += self._db.execute("DELETE FROM score WHERE p = ? OR q = ?", (h, h)).rowcount
        self._db.commit()
        return removed

    def get_vectors(
        self, model_key: str, kind: str, texts: Sequence[str]
    ) -> list[list[float] | None]:
        out: list[list[float] | None] = []
        for t in texts:
            row = self._db.execute(
                "SELECT v FROM vec WHERE model=? AND kind=? AND h=?", (model_key, kind, _h(t))
            ).fetchone()
            out.append(None if row is None else list(struct.unpack(f"{len(row[0]) // 4}f", row[0])))
        return out

    def put_vectors(
        self, model_key: str, kind: str, texts: Sequence[str], vectors: Sequence[Sequence[float]]
    ) -> None:
        for t, v in zip(texts, vectors, strict=True):
            self._db.execute(
                "INSERT OR REPLACE INTO vec VALUES (?,?,?,?)",
                (model_key, kind, _h(t), struct.pack(f"{len(v)}f", *v)),
            )
        self._db.commit()

    def get_scores(self, model_key: str, query: str, passages: Sequence[str]) -> list[float | None]:
        out: list[float | None] = []
        for p in passages:
            row = self._db.execute(
                "SELECT s FROM score WHERE model=? AND q=? AND p=?", (model_key, _h(query), _h(p))
            ).fetchone()
            out.append(None if row is None else float(row[0]))
        return out

    def put_scores(
        self, model_key: str, query: str, passages: Sequence[str], scores: Sequence[float]
    ) -> None:
        for p, s in zip(passages, scores, strict=True):
            self._db.execute(
                "INSERT OR REPLACE INTO score VALUES (?,?,?,?)", (model_key, _h(query), _h(p), s)
            )
        self._db.commit()


class CachingEmbedder:
    def __init__(
        self,
        inner: Embedder | None,
        cache: ModelCache,
        mode: Literal["read_write", "read_only"],
        *,
        model_key: str | None = None,
        dim: int | None = None,
    ) -> None:
        if inner is None and mode != "read_only":
            raise ValueError("an embedder is required unless the cache is read_only")
        self._inner, self._cache, self._mode = inner, cache, mode
        self.model_key = inner.model_key if inner is not None else str(model_key)
        self.dim = inner.dim if inner is not None else int(dim or 0)

    async def _many(self, kind: str, texts: Sequence[str]) -> list[list[float]]:
        found = self._cache.get_vectors(self.model_key, kind, texts)
        missing = [i for i, v in enumerate(found) if v is None]
        if missing:
            if self._mode == "read_only" or self._inner is None:
                raise CacheMiss(self.model_key, len(missing))
            todo = [texts[i] for i in missing]
            fetch = (
                self._inner.embed_documents
                if kind == "doc"
                else (lambda ts: _each_query(self._inner, ts))
            )
            vectors = await fetch(todo)
            self._cache.put_vectors(self.model_key, kind, todo, vectors)
            for i, v in zip(missing, vectors, strict=True):
                found[i] = v
        return [v for v in found if v is not None]

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return await self._many("doc", texts)

    async def embed_query(self, text: str) -> list[float]:
        return (await self._many("query", [text]))[0]


async def _each_query(inner: Embedder, texts: Sequence[str]) -> list[list[float]]:
    return [await inner.embed_query(t) for t in texts]


class CachingReranker:
    def __init__(
        self,
        inner: Reranker | None,
        cache: ModelCache,
        mode: Literal["read_write", "read_only"],
        *,
        model_key: str | None = None,
    ) -> None:
        if inner is None and mode != "read_only":
            raise ValueError("a reranker is required unless the cache is read_only")
        self._inner, self._cache, self._mode = inner, cache, mode
        self.model_key = inner.model_key if inner is not None else str(model_key)

    async def score(self, query: str, passages: Sequence[str]) -> list[float]:
        found = self._cache.get_scores(self.model_key, query, passages)
        missing = [i for i, s in enumerate(found) if s is None]
        if missing:
            if self._mode == "read_only" or self._inner is None:
                raise CacheMiss(self.model_key, len(missing))
            todo = [passages[i] for i in missing]
            scores = await self._inner.score(query, todo)  # one call for all missed passages
            self._cache.put_scores(self.model_key, query, todo, scores)
            for i, s in zip(missing, scores, strict=True):
                found[i] = s
        return [s for s in found if s is not None]
