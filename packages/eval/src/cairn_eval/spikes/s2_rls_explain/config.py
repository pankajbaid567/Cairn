"""``S2Config``: every knob of the S2 harness, validated at construction (design §14)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final

SPIKE_DB_PREFIX: Final = "cairn_spike_"
"""The harness drops and recreates its database, so it only touches names with this prefix."""
ITERATIVE_SCAN_VALUES: Final = ("off", "relaxed_order", "strict_order")
"""Values of pgvector's ``hnsw.iterative_scan`` (pgvector 0.8)."""
PRINCIPAL_SETTING: Final = "cairn.principal_groups"
"""The setting the RLS policy reads; applied with ``SET LOCAL`` per transaction."""
SPIKE_SCHEMA: Final = "s2"
SPIKE_TABLE: Final = "chunks"
RESULT_LIMIT: Final = 10
"""Rows each query asks for, and the ``k`` of recall at 10."""
RRF_DEPTH: Final = 50
"""Rows each side of the hybrid query contributes before fusion."""
RRF_K: Final = 60

_DATABASE_NAME = re.compile(r"^cairn_spike_[a-z0-9_]{1,40}$")
_MEMORY_SIZE = re.compile(r"^[1-9][0-9]*(kB|MB|GB)$")


@dataclass(frozen=True)
class S2Config:
    """Harness settings. Defaults are the laptop run (design §14).

    ``dataset`` is an S1 dataset directory; ``None`` generates ``rows`` x ``dim`` with the
    S1 defaults (20,000 rows of 384 dimensions) into a temporary directory.
    ``database`` must start with ``cairn_spike_``: it is dropped and recreated.
    ``maintenance_work_mem`` is set on the index-building session only.
    """

    dataset: Path | None = None
    database: str = "cairn_spike_s2"
    principals: tuple[str, ...] = ("p01", "p05", "p20", "p50")
    iterative_scan: tuple[str, ...] = ITERATIVE_SCAN_VALUES
    ef_search: int = 40
    repeats: int = 3
    out_dir: Path = Path("docs/spikes/S2")
    rows: int = 20_000
    dim: int = 384
    recall_queries: int = 10
    maintenance_work_mem: str = "256MB"
    drop_after: bool = False
    compose_file: Path | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "principals", tuple(self.principals))
        object.__setattr__(self, "iterative_scan", tuple(self.iterative_scan))
        problems = self.problems()
        if problems:
            raise ValueError("invalid S2 configuration: " + "; ".join(problems))

    def problems(self) -> list[str]:
        """Every reason this configuration is unusable, empty when it is fine."""
        found: list[str] = []
        if not _DATABASE_NAME.match(self.database):
            found.append(f"database must match {SPIKE_DB_PREFIX}[a-z0-9_]+ (at most 52 characters)")
        if not self.principals:
            found.append("at least one principal is needed")
        if len(set(self.principals)) != len(self.principals):
            found.append("principals must be distinct")
        if not self.iterative_scan:
            found.append("at least one iterative_scan value is needed")
        bad = [v for v in self.iterative_scan if v not in ITERATIVE_SCAN_VALUES]
        if bad:
            found.append(f"iterative_scan values must be among {', '.join(ITERATIVE_SCAN_VALUES)}")
        if len(set(self.iterative_scan)) != len(self.iterative_scan):
            found.append("iterative_scan values must be distinct")
        if self.ef_search < 1:
            found.append("ef_search must be at least 1")
        if self.repeats < 1:
            found.append("repeats must be at least 1")
        if self.recall_queries < 1:
            found.append("recall_queries must be at least 1")
        if self.rows < 1:
            found.append("rows must be at least 1")
        if self.dim < 2:
            found.append("dim must be at least 2")
        if not _MEMORY_SIZE.match(self.maintenance_work_mem):
            found.append("maintenance_work_mem must look like 256MB (kB, MB or GB)")
        return found
