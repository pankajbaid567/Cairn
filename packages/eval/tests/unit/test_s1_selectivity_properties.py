"""Property test for S1 selectivity bounds and clustered mode (task 14.5, requirements 15.7, 15.8).

Feature: cairn-phase-0-foundations, Property 16: S1 selectivity stays within binomial bounds,
and clustered mode keeps clusters whole

**Validates: Requirements 15.7, 15.8**

For small generated configurations in both ACL modes, each query principal's actual
selectivity (as the manifest records it and as a recount of ``chunks.parquet`` gives it) is
within ``5 * sqrt(target * (1 - target) / rows) + 1 / rows`` of its target. In clustered
mode, for every selectivity group at most one cluster is split between inside and outside
the group.
"""

from __future__ import annotations

import math
import tempfile
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from cairn_eval.spikes.s1_acl_corpus import S1Config, generate, principal_names
from cairn_eval.spikes.s1_acl_corpus.config import SELECTIVITY_GROUP_BASE

# Whole percents keep principal names unique and the targets inside (0, 1].
percentages = st.lists(st.integers(min_value=1, max_value=100), min_size=1, max_size=4, unique=True)


@st.composite
def configs(draw: st.DrawFn) -> S1Config:
    return S1Config(
        rows=draw(st.integers(min_value=100, max_value=500)),
        dim=draw(st.integers(min_value=2, max_value=6)),
        clusters=draw(st.integers(min_value=1, max_value=12)),
        selectivities=tuple(p / 100 for p in draw(percentages)),
        acl_mode=draw(st.sampled_from(["uncorrelated", "clustered"])),
        noise_groups=draw(st.integers(min_value=0, max_value=5)),
        tokens_per_chunk=4,
        vocab=10,
        batch_rows=draw(st.integers(min_value=50, max_value=500)),
        queries=1,
        seed=draw(st.integers(min_value=0, max_value=2**32)),
    )


def selectivity_bound(target: float, rows: int) -> float:
    return 5 * math.sqrt(target * (1 - target) / rows) + 1 / rows


@settings(max_examples=30, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(cfg=configs())
def test_selectivity_within_binomial_bounds_and_clusters_stay_whole(cfg: S1Config) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        manifest = generate(cfg, out)
        data: dict[str, Any] = pq.read_table(
            out / "chunks.parquet", columns=["cluster", "acl"]
        ).to_pydict()

    for k, (name, target) in enumerate(
        zip(principal_names(cfg.selectivities), cfg.selectivities, strict=True)
    ):
        group = SELECTIVITY_GROUP_BASE + k
        recount = sum(group in acl for acl in data["acl"]) / cfg.rows
        bound = selectivity_bound(target, cfg.rows)
        info = manifest.principals[name]
        assert info["actual_selectivity"] == recount
        assert abs(recount - target) <= bound, (name, target, recount, bound)

        if cfg.acl_mode == "clustered":
            flags: dict[int, set[bool]] = {}
            for cluster, acl in zip(data["cluster"], data["acl"], strict=True):
                flags.setdefault(cluster, set()).add(group in acl)
            split = [c for c, seen in flags.items() if len(seen) > 1]
            assert len(split) <= 1, (name, split)
