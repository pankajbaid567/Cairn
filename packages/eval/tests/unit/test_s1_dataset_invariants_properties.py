"""Property 15: S1 datasets satisfy their row and manifest invariants.

**Validates: Requirements 15.5, 15.6**

For any small configuration, every embedding has the configured dimension and unit
length within 1e-5, every ACL array is sorted and duplicate-free with the noise groups
disjoint from every principal group, and the manifest's per-principal visible counts
and selectivities equal a recount of ``chunks.parquet``.
"""

from __future__ import annotations

import math
import tempfile
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow.parquet as pq
from hypothesis import given, settings
from hypothesis import strategies as st

from cairn_eval.spikes.s1_acl_corpus import S1Config, generate, principal_names, read_manifest
from cairn_eval.spikes.s1_acl_corpus.config import (
    NO_ACCESS_PRINCIPAL,
    NOISE_GROUP_BASE,
    SELECTIVITY_GROUP_BASE,
)

UNIT_TOLERANCE = 1e-5

TARGETS = (0.005, 0.01, 0.05, 0.125, 0.20, 0.50, 0.90, 1.0)


@st.composite
def small_configs(draw: st.DrawFn) -> S1Config:
    rows = draw(st.integers(min_value=1, max_value=60))
    return S1Config(
        rows=rows,
        dim=draw(st.integers(min_value=2, max_value=12)),
        clusters=draw(st.integers(min_value=1, max_value=6)),
        selectivities=tuple(
            draw(st.lists(st.sampled_from(TARGETS), min_size=1, max_size=4, unique=True))
        ),
        acl_mode=draw(st.sampled_from(("uncorrelated", "clustered"))),
        noise_groups=draw(st.integers(min_value=0, max_value=8)),
        tokens_per_chunk=draw(st.integers(min_value=4, max_value=10)),
        vocab=draw(st.integers(min_value=1, max_value=50)),
        batch_rows=draw(st.integers(min_value=1, max_value=rows + 5)),
        queries=draw(st.integers(min_value=1, max_value=3)),
        seed=draw(st.integers(min_value=0, max_value=2**64 - 1)),
    )


@settings(deadline=None, max_examples=40)
@given(cfg=small_configs())
def test_rows_and_manifest_satisfy_the_dataset_invariants(cfg: S1Config) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "ds"
        manifest = generate(cfg, out)
        data: dict[str, Any] = pq.read_table(out / "chunks.parquet").to_pydict()
        assert read_manifest(out) == manifest

    names = principal_names(cfg.selectivities)
    principal_groups = {SELECTIVITY_GROUP_BASE + k for k in range(len(names))}
    noise_range = range(NOISE_GROUP_BASE, NOISE_GROUP_BASE + cfg.noise_groups)

    # Requirement 15.5: row invariants.
    assert len(data["chunk_id"]) == cfg.rows
    for vector in data["embedding"]:
        assert len(vector) == cfg.dim
        assert math.isclose(float(np.linalg.norm(vector)), 1.0, abs_tol=UNIT_TOLERANCE)
    for acl in data["acl"]:
        assert acl == sorted(set(acl))
        noise = {g for g in acl if g >= NOISE_GROUP_BASE}
        selectivity = set(acl) - noise
        assert noise <= set(noise_range)
        assert selectivity <= principal_groups
        assert noise.isdisjoint(principal_groups)

    # Requirement 15.6: manifest counts equal a recount of chunks.parquet.
    assert manifest.noise_groups == {"first": NOISE_GROUP_BASE, "count": cfg.noise_groups}
    assert set(manifest.principals) == {*names, NO_ACCESS_PRINCIPAL}
    for k, name in enumerate(names):
        group = SELECTIVITY_GROUP_BASE + k
        recount = sum(group in acl for acl in data["acl"])
        info = manifest.principals[name]
        assert info["groups"] == [group]
        assert info["visible_rows"] == recount
        assert info["actual_selectivity"] == recount / cfg.rows
    none = manifest.principals[NO_ACCESS_PRINCIPAL]
    assert none["groups"] == []
    assert none["visible_rows"] == 0
    assert none["actual_selectivity"] == 0.0
