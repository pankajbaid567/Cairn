"""S1 corpus generator (Requirement 15.1 to 15.9; design §13) and the spike host guard of
``cairn-s1 load`` (free-tier retarget FT-28, Requirement 3.8)."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow.parquet as pq
import pytest

from cairn_eval.spikes.s1_acl_corpus import (
    OutputDirectoryNotEmpty,
    S1Config,
    generate,
    principal_names,
    read_manifest,
)
from cairn_eval.spikes.s1_acl_corpus.cli import main
from cairn_eval.spikes.s1_acl_corpus.config import NOISE_GROUP_BASE, SELECTIVITY_GROUP_BASE
from cairn_eval.spikes.s1_acl_corpus.load import table_identifier

SMALL: dict[str, Any] = {
    "rows": 600,
    "dim": 16,
    "clusters": 8,
    "noise_groups": 12,
    "queries": 10,
    "batch_rows": 250,
}


def small(**overrides: Any) -> S1Config:
    return S1Config(**{**SMALL, **overrides})


def read_chunks(path: Path) -> dict[str, Any]:
    return pq.read_table(path / "chunks.parquet").to_pydict()


def test_defaults_match_the_laptop_run() -> None:
    cfg = S1Config()
    assert (cfg.rows, cfg.dim, cfg.clusters) == (50_000, 384, 64)
    assert cfg.selectivities == (0.01, 0.05, 0.20, 0.50)
    assert cfg.acl_mode == "uncorrelated"
    assert (cfg.noise_groups, cfg.tokens_per_chunk, cfg.vocab) == (200, 32, 5_000)
    assert (cfg.sigma, cfg.batch_rows, cfg.queries, cfg.seed) == (0.35, 5_000, 100, 20261027)
    assert principal_names(cfg.selectivities) == ("p01", "p05", "p20", "p50")


@pytest.mark.parametrize(
    "overrides",
    [{"rows": 0}, {"dim": 1}, {"selectivities": (0.0,)}, {"selectivities": (0.1, 0.1)},
     {"batch_rows": 0}, {"acl_mode": "other"}, {"tokens_per_chunk": 3}],
)  # fmt: skip
def test_invalid_config_is_rejected(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="invalid S1 configuration"):
        S1Config(**overrides)


def test_example_run_writes_the_three_files_and_a_consistent_manifest(tmp_path: Path) -> None:
    cfg = small()
    manifest = generate(cfg, tmp_path)

    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "chunks.parquet",
        "manifest.json",
        "queries.parquet",
    ]
    assert read_manifest(tmp_path) == manifest
    assert json.loads((tmp_path / "manifest.json").read_text("utf-8"))["rows"] == cfg.rows
    assert pq.ParquetFile(tmp_path / "chunks.parquet").num_row_groups == 3  # ceil(600 / 250)
    assert pq.read_table(tmp_path / "queries.parquet").num_rows == cfg.queries

    data = read_chunks(tmp_path)
    assert data["chunk_id"] == list(range(cfg.rows))
    names = principal_names(cfg.selectivities)
    for k, name in enumerate(names):
        group = SELECTIVITY_GROUP_BASE + k
        recount = sum(group in acl for acl in data["acl"])
        assert manifest.principals[name]["visible_rows"] == recount
        assert manifest.principals[name]["actual_selectivity"] == recount / cfg.rows
    assert manifest.principals["p_none"]["visible_rows"] == 0
    for vector in data["embedding"]:
        assert len(vector) == cfg.dim
        assert math.isclose(float(np.linalg.norm(vector)), 1.0, abs_tol=1e-5)
    for acl in data["acl"]:
        assert acl == sorted(set(acl))
        noise = [g for g in acl if g >= NOISE_GROUP_BASE]
        assert noise
        assert all(NOISE_GROUP_BASE <= g < NOISE_GROUP_BASE + cfg.noise_groups for g in noise)
    for body, cluster in zip(data["body"], data["cluster"], strict=True):
        tokens = body.split()
        assert len(tokens) == cfg.tokens_per_chunk
        assert {f"t{cluster}_{j}" for j in range(4)} <= set(tokens)


def test_batch_size_changes_row_groups_but_not_the_data(tmp_path: Path) -> None:
    first = generate(small(batch_rows=100), tmp_path / "a")
    second = generate(small(batch_rows=600), tmp_path / "b")
    assert first.row_groups == 6
    assert second.row_groups == 1
    assert first.content_sha256 == second.content_sha256
    assert first.principals == second.principals
    assert read_chunks(tmp_path / "a") == read_chunks(tmp_path / "b")


def test_a_different_seed_gives_different_data(tmp_path: Path) -> None:
    first = generate(small(), tmp_path / "a")
    second = generate(small(seed=1), tmp_path / "b")
    assert first.content_sha256 != second.content_sha256


def test_writer_receives_at_most_batch_rows_rows_per_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sizes: list[int] = []

    class Recording(pq.ParquetWriter):
        def write_table(self, table: Any, row_group_size: int | None = None) -> None:
            sizes.append(table.num_rows)
            super().write_table(table, row_group_size=row_group_size)

    monkeypatch.setattr(pq, "ParquetWriter", Recording)
    generate(small(rows=1_050, batch_rows=400), tmp_path)
    assert sizes == [400, 400, 250]


def test_a_non_empty_output_directory_is_refused_without_writing(tmp_path: Path) -> None:
    (tmp_path / "keep.txt").write_text("mine", "utf-8")
    with pytest.raises(OutputDirectoryNotEmpty):
        generate(small(), tmp_path)
    assert [p.name for p in tmp_path.iterdir()] == ["keep.txt"]

    generate(small(), tmp_path, overwrite=True)
    assert (tmp_path / "keep.txt").read_text("utf-8") == "mine"
    assert (tmp_path / "manifest.json").is_file()
    assert not list(tmp_path.glob("*.tmp"))


def test_cli_refuses_with_status_1_then_overwrites(tmp_path: Path) -> None:
    out = tmp_path / "ds"
    args = ["generate", "--out", str(out), "--rows", "50", "--dim", "8", "--clusters", "4"]
    assert main(args) == 0
    before = sorted(p.name for p in out.iterdir())
    assert main(args) == 1
    assert sorted(p.name for p in out.iterdir()) == before
    assert main([*args, "--overwrite"]) == 0
    assert main(["hash", str(out)]) == 0


def test_cli_rejects_an_invalid_configuration(tmp_path: Path) -> None:
    assert main(["generate", "--out", str(tmp_path / "x"), "--rows", "0"]) == 2
    assert not (tmp_path / "x").exists()


def test_clustered_mode_keeps_all_but_one_cluster_whole(tmp_path: Path) -> None:
    cfg = small(rows=2_000, acl_mode="clustered", selectivities=(0.05, 0.25, 0.50))
    generate(cfg, tmp_path)
    data = read_chunks(tmp_path)
    for k in range(len(cfg.selectivities)):
        group = SELECTIVITY_GROUP_BASE + k
        inside: dict[int, set[bool]] = {}
        for cluster, acl in zip(data["cluster"], data["acl"], strict=True):
            inside.setdefault(cluster, set()).add(group in acl)
        mixed = [c for c, flags in inside.items() if len(flags) > 1]
        assert len(mixed) <= 1


def test_realized_selectivity_is_within_binomial_bounds(tmp_path: Path) -> None:
    for mode in ("uncorrelated", "clustered"):
        cfg = small(rows=3_000, acl_mode=mode)
        manifest = generate(cfg, tmp_path / mode)
        for info in manifest.principals.values():
            target = info["target_selectivity"]
            bound = 5 * math.sqrt(target * (1 - target) / cfg.rows) + 1 / cfg.rows
            assert abs(info["actual_selectivity"] - target) <= bound


REMOTE_HOST = "ep-quiet-forest-123456.ap-southeast-1.aws.neon.tech"


@pytest.fixture
def load_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[list[str], Path]:
    """A small dataset and settings without a ``.env``; ``connect_admin`` records the host it
    would connect to and stops there, as an unreachable database would (exit 2)."""
    import cairn_core.testing

    dataset = tmp_path / "ds"
    assert main(["generate", "--out", str(dataset), "--rows", "50", "--dim", "8"]) == 0
    monkeypatch.chdir(tmp_path)
    for key, value in {
        "CAIRN_TEST_ADMIN_HOST": REMOTE_HOST,
        "POSTGRES_PASSWORD": "test-only-superuser",
        "CAIRN_OWNER_DB_PASSWORD": "test-only-owner",
        "CAIRN_APP_DB_PASSWORD": "test-only-app",
        "CAIRN_DBOS_DB_PASSWORD": "test-only-dbos",
    }.items():
        monkeypatch.setenv(key, value)
    hosts: list[str] = []

    def connect_admin(config: Any, *_args: Any, **_kwargs: Any) -> Any:
        hosts.append(config.target.host)
        raise cairn_core.testing.DatabaseUnavailable("stopped by the test before connecting")

    monkeypatch.setattr(cairn_core.testing, "connect_admin", connect_admin)
    return hosts, dataset


def test_load_refuses_a_remote_host_with_status_2_before_connecting(
    load_env: tuple[list[str], Path], capsys: pytest.CaptureFixture[str]
) -> None:
    hosts, dataset = load_env
    assert main(["load", str(dataset)]) == 2
    assert hosts == []
    err = capsys.readouterr().err
    assert f"refusing database host '{REMOTE_HOST}'" in err
    assert "--allow-remote" in err
    assert "test-only" not in err


def test_load_with_allow_remote_goes_on_to_connect(load_env: tuple[list[str], Path]) -> None:
    hosts, dataset = load_env
    assert main(["load", str(dataset), "--allow-remote"]) == 2
    assert hosts == [REMOTE_HOST]


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "db", "ft-db"])
def test_load_accepts_local_hosts_without_the_flag(
    load_env: tuple[list[str], Path], monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    hosts, dataset = load_env
    monkeypatch.setenv("CAIRN_TEST_ADMIN_HOST", host)
    assert main(["load", str(dataset)]) == 2
    assert hosts == [host]


def test_table_identifier_accepts_plain_and_schema_names_only() -> None:
    assert table_identifier("s1_chunks")
    assert table_identifier("public.s1_chunks")
    for bad in ("", "Bad", "a;drop", "a.b.c", "1abc", "a b"):
        with pytest.raises(ValueError, match="invalid table name"):
            table_identifier(bad)
