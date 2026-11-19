"""Property test for S1 batch-size independence (task 14.3, requirement 15.3).

Feature: cairn-phase-0-foundations, Property 14: S1 data is independent of batch size

**Validates: Requirements 15.3**

For a small random S1 configuration and two random batch sizes, the generated chunks are
identical in value and order, the queries are identical, and the manifests report the same
``content_sha256`` and per-principal counts. Only the row-group count may differ, and it
equals ``ceil(rows / batch_rows)``.
"""

from __future__ import annotations

import math
import tempfile
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
from hypothesis import given, settings
from hypothesis import strategies as st

from cairn_eval.spikes.s1_acl_corpus import S1Config, generate

_SELECTIVITIES = st.lists(
    st.sampled_from([0.05, 0.1, 0.2, 0.25, 0.5, 0.75, 1.0]),
    min_size=1,
    max_size=3,
    unique=True,
).map(tuple)


@st.composite
def configs(draw: st.DrawFn) -> tuple[S1Config, int, int]:
    """A tiny configuration and two batch sizes, often straddling the row count."""
    rows = draw(st.integers(min_value=1, max_value=60))
    batch = st.integers(min_value=1, max_value=rows + 10)
    cfg = S1Config(
        rows=rows,
        dim=draw(st.integers(min_value=2, max_value=6)),
        clusters=draw(st.integers(min_value=1, max_value=6)),
        selectivities=draw(_SELECTIVITIES),
        acl_mode=draw(st.sampled_from(["uncorrelated", "clustered"])),
        noise_groups=draw(st.integers(min_value=0, max_value=5)),
        tokens_per_chunk=draw(st.integers(min_value=4, max_value=8)),
        vocab=draw(st.integers(min_value=1, max_value=50)),
        sigma=draw(st.sampled_from([0.0, 0.1, 0.35])),
        queries=draw(st.integers(min_value=1, max_value=4)),
        seed=draw(st.integers(min_value=0, max_value=2**64 - 1)),
    )
    return cfg, draw(batch), draw(batch)


def _with_batch(cfg: S1Config, batch_rows: int) -> S1Config:
    return S1Config(**{**cfg.to_dict(), "batch_rows": batch_rows})


def _table(path: Path, name: str) -> dict[str, Any]:
    return pq.read_table(path / name).to_pydict()


@settings(deadline=None, max_examples=40)
@given(configs())
def test_s1_data_does_not_depend_on_batch_size(drawn: tuple[S1Config, int, int]) -> None:
    cfg, batch_a, batch_b = drawn
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        first = generate(_with_batch(cfg, batch_a), root / "a")
        second = generate(_with_batch(cfg, batch_b), root / "b")

        assert first.content_sha256 == second.content_sha256
        assert first.principals == second.principals
        assert first.noise_groups == second.noise_groups
        assert _table(root / "a", "chunks.parquet") == _table(root / "b", "chunks.parquet")
        assert _table(root / "a", "queries.parquet") == _table(root / "b", "queries.parquet")

        assert first.row_groups == math.ceil(cfg.rows / batch_a)
        assert second.row_groups == math.ceil(cfg.rows / batch_b)
