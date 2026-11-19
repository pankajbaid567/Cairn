"""The S1 row model: every row is a pure function of (seed, row index) (design §13, PD-15).

Randomness comes from Philox, keyed by the seed. The 256-bit counter is four 64-bit words
``[0, sub, role, index]``: the row index sits in the top word, so the streams of two rows
(or of two roles of one row) are 2**128 or more blocks apart and never overlap. Three
roles per row keep the parts independent of each other:

- content: cluster and body tokens,
- embedding: the Gaussian noise around the cluster center,
- acl: one uniform per selectivity, then the noise groups.

So changing ``dim`` never changes the ACL labels, and changing ``batch_rows`` changes
nothing at all, because no draw depends on which batch a row falls in.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Final

import numpy as np
import numpy.typing as npt

from cairn_eval.spikes.s1_acl_corpus.config import (
    NOISE_GROUP_BASE,
    SELECTIVITY_GROUP_BASE,
    TOPIC_TOKENS,
    S1Config,
)

GENERATOR_VERSION: Final = "s1-1"
"""Bump when a change alters the rows a given config produces."""

ZIPF_EXPONENT: Final = 1.1
MAX_NOISE_GROUPS_PER_ROW: Final = 3

_ROLE_CONTENT: Final = 0
_ROLE_EMBEDDING: Final = 1
_ROLE_ACL: Final = 2
_ROLE_CENTERS: Final = 3
_ROLE_RANKS: Final = 4
_ROLE_QUERY: Final = 5

FloatArray = npt.NDArray[np.float64]
Float32Array = npt.NDArray[np.float32]
IntArray = npt.NDArray[np.int64]


def stream(seed: int, role: int, index: int = 0, sub: int = 0) -> np.random.Generator:
    """The Philox stream for (seed, role, index, sub)."""
    counter = np.array([0, sub, role, index], dtype=np.uint64)
    return np.random.Generator(np.random.Philox(key=seed, counter=counter))


@dataclass(frozen=True)
class Plan:
    """Everything a row needs besides its index: built once per run."""

    cfg: S1Config
    centers: FloatArray
    """``(clusters, dim)`` unit vectors."""
    ranks: IntArray
    """``(len(selectivities), clusters)``: where each cluster sits in that selectivity's order."""
    zipf_cdf: FloatArray
    noise_scale: float
    """Per-dimension standard deviation of the embedding noise: ``sigma / sqrt(dim)``."""


def make_plan(cfg: S1Config) -> Plan:
    """Cluster centers, per-selectivity cluster ranks and the Zipf table."""
    centers = stream(cfg.seed, _ROLE_CENTERS).standard_normal((cfg.clusters, cfg.dim))
    centers /= np.linalg.norm(centers, axis=1, keepdims=True)
    ranks = np.empty((len(cfg.selectivities), cfg.clusters), dtype=np.int64)
    for k in range(len(cfg.selectivities)):
        order = stream(cfg.seed, _ROLE_RANKS, sub=k).permutation(cfg.clusters)
        ranks[k, order] = np.arange(cfg.clusters)
    weights = 1.0 / np.arange(1, cfg.vocab + 1, dtype=np.float64) ** ZIPF_EXPONENT
    cdf = np.cumsum(weights)
    cdf /= cdf[-1]
    return Plan(
        cfg=cfg,
        centers=centers,
        ranks=ranks,
        zipf_cdf=cdf,
        noise_scale=cfg.sigma / float(np.sqrt(cfg.dim)),
    )


@dataclass(frozen=True)
class Row:
    chunk_id: int
    cluster: int
    body: str
    embedding: Float32Array
    acl: list[int]


def topic_tokens(cluster: int) -> list[str]:
    return [f"t{cluster}_{j}" for j in range(TOPIC_TOKENS)]


def _embedding(plan: Plan, rng: np.random.Generator, cluster: int) -> Float32Array:
    """Cluster center plus Gaussian noise, scaled back to unit length."""
    vec = plan.centers[cluster] + plan.noise_scale * rng.standard_normal(plan.cfg.dim)
    vec /= np.linalg.norm(vec)
    return vec.astype(np.float32)


def row_at(plan: Plan, index: int) -> Row:
    """Row ``index``. Pure: the same plan and index always give the same row."""
    cfg = plan.cfg
    content = stream(cfg.seed, _ROLE_CONTENT, index)
    cluster = int(content.integers(0, cfg.clusters))
    zipf_count = cfg.tokens_per_chunk - TOPIC_TOKENS
    picks = np.searchsorted(plan.zipf_cdf, content.random(zipf_count), side="right")
    picks = np.minimum(picks, cfg.vocab - 1)
    tokens = [f"w{int(p):04d}" for p in picks] + topic_tokens(cluster)
    body = " ".join(tokens[int(i)] for i in content.permutation(len(tokens)))

    embedding = _embedding(plan, stream(cfg.seed, _ROLE_EMBEDDING, index), cluster)

    acl_rng = stream(cfg.seed, _ROLE_ACL, index)
    draws = acl_rng.random(len(cfg.selectivities))
    groups: list[int] = []
    for k, target in enumerate(cfg.selectivities):
        if cfg.acl_mode == "clustered":
            joins = (int(plan.ranks[k, cluster]) + float(draws[k])) / cfg.clusters < target
        else:
            joins = float(draws[k]) < target
        if joins:
            groups.append(SELECTIVITY_GROUP_BASE + k)
    if cfg.noise_groups > 0:
        wanted = int(acl_rng.integers(1, MAX_NOISE_GROUPS_PER_ROW + 1))
        chosen = acl_rng.choice(cfg.noise_groups, size=min(wanted, cfg.noise_groups), replace=False)
        groups.extend(NOISE_GROUP_BASE + int(g) for g in sorted(chosen))
    return Row(chunk_id=index, cluster=cluster, body=body, embedding=embedding, acl=groups)


def row_bytes(row: Row) -> bytes:
    """Canonical form of a row for ``content_sha256`` (little-endian, length-prefixed)."""
    body = row.body.encode("utf-8")
    return b"".join(
        (
            struct.pack("<qhI", row.chunk_id, row.cluster, len(body)),
            body,
            row.embedding.astype("<f4", copy=False).tobytes(),
            struct.pack(f"<I{len(row.acl)}i", len(row.acl), *row.acl),
        )
    )


@dataclass(frozen=True)
class Query:
    query_id: int
    cluster: int
    embedding: Float32Array
    topic_terms: list[str]


def query_at(plan: Plan, query_id: int) -> Query:
    """Query ``query_id``: a vector near a cluster center, and that cluster's topic terms."""
    rng = stream(plan.cfg.seed, _ROLE_QUERY, query_id)
    cluster = int(rng.integers(0, plan.cfg.clusters))
    return Query(query_id, cluster, _embedding(plan, rng, cluster), topic_tokens(cluster))
