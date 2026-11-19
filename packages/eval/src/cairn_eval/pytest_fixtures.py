"""Pytest fixtures that need a seeded Kestrel Pay database (re-exported by the root conftest)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest

from cairn_core.testing import DbTestConfig, FreshDb, temporary_database
from cairn_eval.seed import SeededCorpus, seed_database


@pytest.fixture(scope="session")
def seeded(
    admin_conn: psycopg.Connection[Any],
    admin_config: DbTestConfig,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[FreshDb, SeededCorpus]]:
    """A throwaway database holding the whole rendered Kestrel Pay corpus with fake embeddings."""
    if admin_config.settings.ingest_db_password is None:
        pytest.skip("CAIRN_INGEST_DB_PASSWORD is not set (make env-update)")
    with temporary_database(admin_conn, admin_config) as db:
        assert db.ingest_dsn is not None  # noqa: S101
        corpus = seed_database(db.ingest_dsn, tmp_path_factory.mktemp("kestrel"))
        yield db, corpus


__all__ = ["Path", "seeded"]
