"""Stream an S1 dataset to disk: ``chunks.parquet``, ``queries.parquet``, ``manifest.json``.

One batch of rows is in memory at a time (design §13: 5,000 x 384 x 4 bytes is about
7.7 MB) and each batch becomes one Parquet row group. Files are written under a
``.tmp`` name and renamed at the end, so an interrupted run never leaves a half-written
dataset next to an old manifest.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from cairn_eval.spikes.s1_acl_corpus.config import (
    NO_ACCESS_PRINCIPAL,
    NOISE_GROUP_BASE,
    SELECTIVITY_GROUP_BASE,
    S1Config,
    principal_names,
)
from cairn_eval.spikes.s1_acl_corpus.rows import (
    GENERATOR_VERSION,
    Plan,
    make_plan,
    query_at,
    row_at,
    row_bytes,
)

CHUNKS_FILE: Final = "chunks.parquet"
QUERIES_FILE: Final = "queries.parquet"
MANIFEST_FILE: Final = "manifest.json"
DATASET_FILES: Final = (CHUNKS_FILE, QUERIES_FILE, MANIFEST_FILE)
PARQUET_COMPRESSION: Final = "zstd"
_HASH_CHUNK: Final = 1 << 20


class OutputDirectoryNotEmpty(Exception):
    """The output directory has content and ``overwrite`` was not asked for."""


class DatasetError(Exception):
    """A dataset directory is missing a file or its manifest does not match its files."""


@dataclass(frozen=True)
class Manifest:
    """What ``manifest.json`` records. ``content_sha256`` ignores ``batch_rows``."""

    generator_version: str
    config: S1Config
    rows: int
    dim: int
    row_groups: int
    noise_groups: dict[str, int]
    principals: dict[str, dict[str, Any]]
    content_sha256: str
    files: dict[str, dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "generator_version": self.generator_version,
            "config": self.config.to_dict(),
            "rows": self.rows,
            "dim": self.dim,
            "row_groups": self.row_groups,
            "noise_groups": self.noise_groups,
            "principals": self.principals,
            "content_sha256": self.content_sha256,
            "files": self.files,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Manifest:
        return cls(
            generator_version=str(data["generator_version"]),
            config=S1Config.from_dict(data["config"]),
            rows=int(data["rows"]),
            dim=int(data["dim"]),
            row_groups=int(data["row_groups"]),
            noise_groups=dict(data["noise_groups"]),
            principals=dict(data["principals"]),
            content_sha256=str(data["content_sha256"]),
            files=dict(data["files"]),
        )


def chunks_schema(dim: int) -> pa.Schema:
    fields: list[Any] = [
        pa.field("chunk_id", pa.int64(), nullable=False),
        pa.field("cluster", pa.int16(), nullable=False),
        pa.field("body", pa.string(), nullable=False),
        pa.field("embedding", pa.list_(pa.float32(), dim), nullable=False),
        pa.field("acl", pa.list_(pa.int32()), nullable=False),
    ]
    return pa.schema(fields)


def queries_schema(dim: int) -> pa.Schema:
    fields: list[Any] = [
        pa.field("query_id", pa.int32(), nullable=False),
        pa.field("cluster", pa.int16(), nullable=False),
        pa.field("embedding", pa.list_(pa.float32(), dim), nullable=False),
        pa.field("topic_terms", pa.list_(pa.string()), nullable=False),
    ]
    return pa.schema(fields)


def _embedding_array(vectors: list[Any], dim: int) -> Any:
    flat = pa.array(np.concatenate(vectors).astype(np.float32, copy=False), type=pa.float32())
    return pa.FixedSizeListArray.from_arrays(flat, dim)


def _list_array(lists: Sequence[Sequence[Any]], value_type: pa.DataType) -> Any:
    offsets = np.zeros(len(lists) + 1, dtype=np.int32)
    np.cumsum([len(x) for x in lists], out=offsets[1:])
    values = pa.array([v for x in lists for v in x], type=value_type)
    return pa.ListArray.from_arrays(pa.array(offsets, type=pa.int32()), values)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(_HASH_CHUNK):
            digest.update(block)
    return digest.hexdigest()


def _refuse_unless_empty(out_dir: Path, overwrite: bool) -> None:
    if out_dir.exists() and not out_dir.is_dir():
        raise OutputDirectoryNotEmpty(f"{out_dir} exists and is not a directory")
    if not overwrite and out_dir.is_dir() and any(out_dir.iterdir()):
        raise OutputDirectoryNotEmpty(
            f"{out_dir} is not empty; pass overwrite to replace the dataset files in it"
        )


def _batches(cfg: S1Config) -> Iterator[tuple[int, int]]:
    for start in range(0, cfg.rows, cfg.batch_rows):
        yield start, min(start + cfg.batch_rows, cfg.rows)


def _write_chunks(plan: Plan, path: Path) -> tuple[str, list[int]]:
    """Write ``chunks.parquet``; return ``content_sha256`` and visible rows per selectivity."""
    cfg = plan.cfg
    schema = chunks_schema(cfg.dim)
    content = hashlib.sha256()
    visible = [0] * len(cfg.selectivities)
    with pq.ParquetWriter(path, schema, compression=PARQUET_COMPRESSION) as writer:
        for start, stop in _batches(cfg):
            batch = [row_at(plan, i) for i in range(start, stop)]
            for row in batch:
                content.update(row_bytes(row))
                for group in row.acl:
                    if group < NOISE_GROUP_BASE:
                        visible[group - SELECTIVITY_GROUP_BASE] += 1
            table = pa.Table.from_arrays(
                [
                    pa.array([r.chunk_id for r in batch], type=pa.int64()),
                    pa.array([r.cluster for r in batch], type=pa.int16()),
                    pa.array([r.body for r in batch], type=pa.string()),
                    _embedding_array([r.embedding for r in batch], cfg.dim),
                    _list_array([r.acl for r in batch], pa.int32()),
                ],
                schema=schema,
            )
            writer.write_table(table, row_group_size=cfg.batch_rows)
    return content.hexdigest(), visible


def _write_queries(plan: Plan, path: Path) -> None:
    cfg = plan.cfg
    queries = [query_at(plan, q) for q in range(cfg.queries)]
    table = pa.Table.from_arrays(
        [
            pa.array([q.query_id for q in queries], type=pa.int32()),
            pa.array([q.cluster for q in queries], type=pa.int16()),
            _embedding_array([q.embedding for q in queries], cfg.dim),
            _list_array([q.topic_terms for q in queries], pa.string()),
        ],
        schema=queries_schema(cfg.dim),
    )
    pq.write_table(table, path, compression=PARQUET_COMPRESSION)


def _principals(cfg: S1Config, visible: list[int]) -> dict[str, dict[str, Any]]:
    principals: dict[str, dict[str, Any]] = {}
    for k, (name, target) in enumerate(
        zip(principal_names(cfg.selectivities), cfg.selectivities, strict=True)
    ):
        principals[name] = {
            "groups": [SELECTIVITY_GROUP_BASE + k],
            "target_selectivity": target,
            "visible_rows": visible[k],
            "actual_selectivity": visible[k] / cfg.rows,
        }
    principals[NO_ACCESS_PRINCIPAL] = {
        "groups": [],
        "target_selectivity": 0.0,
        "visible_rows": 0,
        "actual_selectivity": 0.0,
    }
    return principals


def write_manifest(manifest: Manifest, path: Path) -> None:
    path.write_text(json.dumps(manifest.to_dict(), indent=2, sort_keys=True) + "\n", "utf-8")


def read_manifest(dataset: Path) -> Manifest:
    """Load ``manifest.json`` from a dataset directory."""
    path = dataset / MANIFEST_FILE
    try:
        return Manifest.from_dict(json.loads(path.read_text("utf-8")))
    except FileNotFoundError:
        raise DatasetError(f"{path} does not exist") from None
    except (ValueError, KeyError, TypeError) as exc:
        raise DatasetError(f"{path} is not a valid S1 manifest ({type(exc).__name__})") from None


def generate(cfg: S1Config, out_dir: Path, *, overwrite: bool = False) -> Manifest:
    """Write the dataset for ``cfg`` into ``out_dir`` and return its manifest.

    Raises ``OutputDirectoryNotEmpty``, before writing anything, when ``out_dir`` has
    content and ``overwrite`` is false. With ``overwrite`` only the three dataset files
    are replaced; anything else in the directory is left alone.
    """
    out_dir = Path(out_dir)
    _refuse_unless_empty(out_dir, overwrite)
    out_dir.mkdir(parents=True, exist_ok=True)
    plan = make_plan(cfg)
    temp = {name: out_dir / f"{name}.tmp" for name in DATASET_FILES}
    try:
        content_sha256, visible = _write_chunks(plan, temp[CHUNKS_FILE])
        _write_queries(plan, temp[QUERIES_FILE])
        files = {
            name: {"sha256": sha256_file(temp[name]), "bytes": temp[name].stat().st_size}
            for name in (CHUNKS_FILE, QUERIES_FILE)
        }
        manifest = Manifest(
            generator_version=GENERATOR_VERSION,
            config=cfg,
            rows=cfg.rows,
            dim=cfg.dim,
            row_groups=pq.ParquetFile(temp[CHUNKS_FILE]).num_row_groups,
            noise_groups={"first": NOISE_GROUP_BASE, "count": cfg.noise_groups},
            principals=_principals(cfg, visible),
            content_sha256=content_sha256,
            files=files,
        )
        write_manifest(manifest, temp[MANIFEST_FILE])
        for name in DATASET_FILES:
            os.replace(temp[name], out_dir / name)
    finally:
        for leftover in temp.values():
            leftover.unlink(missing_ok=True)
    return manifest
