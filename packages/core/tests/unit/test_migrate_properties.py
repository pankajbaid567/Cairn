"""Property test for migration planning (design Property 1).

Feature: cairn-phase-0-foundations, Property 1: Migration planning returns the pending
suffix or refuses

**Validates: Requirements 6.1, 6.3, 6.4, 6.5**

``plan`` is pure, so these tests need no database. Migrations are built with
``parse_migration``, so the checksums are the real sha256 of the generated file bytes.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from hypothesis import given
from hypothesis import strategies as st

from cairn_core.migrate import (
    AppliedMigration,
    ChecksumMismatch,
    Migration,
    MigrationGap,
    UnknownAppliedMigration,
    parse_migration,
    plan,
    status,
)

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)

names = st.from_regex(r"[a-z][a-z0-9_]{0,15}", fullmatch=True)
sql_texts = st.text(
    min_size=1,
    max_size=60,
).filter(lambda s: bool(s.strip()))


def make(version: int, name: str, sql: str) -> Migration:
    return parse_migration(f"{version:04d}_{name}.sql", sql.encode())


def record(m: Migration, *, checksum: str | None = None) -> AppliedMigration:
    return AppliedMigration(m.version, m.name, checksum or m.checksum, NOW)


def other_checksum(m: Migration) -> str:
    """A valid sha256 hex string that differs from the migration's checksum."""
    return hashlib.sha256(m.sql.encode() + b"\nedited").hexdigest()


@dataclass(frozen=True)
class Case:
    """Bundled files 1..n (shuffled) and recorded rows 1..k with matching checksums (shuffled)."""

    sorted_files: list[Migration]
    files: list[Migration]
    k: int
    applied: list[AppliedMigration]

    @property
    def n(self) -> int:
        return len(self.sorted_files)


@st.composite
def cases(draw: st.DrawFn, *, min_n: int = 0, min_k: int = 0) -> Case:
    n = draw(st.integers(min_value=min_n, max_value=10))
    k = draw(st.integers(min_value=min(min_k, n), max_value=n))
    parts = draw(st.lists(st.tuples(names, sql_texts), min_size=n, max_size=n))
    sorted_files = [make(i + 1, name, sql) for i, (name, sql) in enumerate(parts)]
    files = draw(st.permutations(sorted_files))
    applied = draw(st.permutations([record(m) for m in sorted_files[:k]]))
    return Case(sorted_files, list(files), k, list(applied))


@given(case=cases())
def test_plan_returns_pending_suffix_in_ascending_order(case: Case) -> None:
    """Property 1: recorded prefix 1..k of bundled 1..n gives exactly k+1..n, ascending."""
    result = plan(case.files, case.applied)
    assert result == case.sorted_files[case.k :]
    assert [m.version for m in result] == list(range(case.k + 1, case.n + 1))


@given(case=cases(), data=st.data())
def test_plan_ignores_input_order_and_leaves_inputs_unchanged(
    case: Case, data: st.DataObject
) -> None:
    files_before = list(case.files)
    applied_before = list(case.applied)
    first = plan(case.files, case.applied)
    assert case.files == files_before
    assert case.applied == applied_before

    files2 = data.draw(st.permutations(case.files))
    applied2 = data.draw(st.permutations(case.applied))
    assert plan(files2, applied2) == first


@given(case=cases())
def test_plan_is_idempotent_after_applying_the_plan(case: Case) -> None:
    pending = plan(case.files, case.applied)
    after = [*case.applied, *(record(m) for m in pending)]
    assert plan(case.files, after) == []


@given(case=cases())
def test_status_agrees_with_plan(case: Case) -> None:
    result = status(case.files, case.applied)
    assert list(result.pending) == plan(case.files, case.applied)
    assert result.head == case.n
    assert result.current == case.k
    assert [a.version for a in result.applied] == list(range(1, case.k + 1))


@given(case=cases(min_n=1, min_k=1), data=st.data())
def test_edited_checksum_is_refused_naming_the_version(case: Case, data: st.DataObject) -> None:
    index = data.draw(st.integers(min_value=0, max_value=case.k - 1))
    victim = case.sorted_files[index]
    applied = [
        record(m, checksum=other_checksum(m)) if m.version == victim.version else record(m)
        for m in case.sorted_files[: case.k]
    ]
    applied = data.draw(st.permutations(applied))
    with pytest.raises(ChecksumMismatch) as info:
        plan(case.files, applied)
    exc = info.value
    assert exc.version == victim.version
    assert str(victim.version) in str(exc)


@given(case=cases(), extra=st.integers(min_value=1, max_value=5), data=st.data())
def test_recorded_version_beyond_bundled_is_refused_naming_it(
    case: Case, extra: int, data: st.DataObject
) -> None:
    ahead = record(make(case.n + extra, "ahead", "SELECT 1;"))
    applied = data.draw(st.permutations([*case.applied, ahead]))
    with pytest.raises(UnknownAppliedMigration) as info:
        plan(case.files, applied)
    exc = info.value
    assert exc.version == ahead.version
    assert str(ahead.version) in str(exc)


@given(case=cases(min_n=2), data=st.data())
def test_gap_in_bundled_versions_is_refused(case: Case, data: st.DataObject) -> None:
    missing = data.draw(st.integers(min_value=1, max_value=case.n - 1))
    kept = [m for m in case.files if m.version != missing]
    # Recorded rows are drawn from the kept files, so only the bundled gap is a defect.
    applied = [record(m) for m in kept if m.version <= case.k and m.version != missing]
    with pytest.raises(MigrationGap) as info:
        plan(kept, applied)
    exc = info.value
    assert exc.source == "bundled"
    assert exc.versions == tuple(sorted(m.version for m in kept))


@given(case=cases(min_n=1), data=st.data(), sql=sql_texts)
def test_duplicate_bundled_version_is_refused(case: Case, data: st.DataObject, sql: str) -> None:
    dup_version = data.draw(st.integers(min_value=1, max_value=case.n))
    duplicate = make(dup_version, "duplicate", sql)
    files = data.draw(st.permutations([*case.files, duplicate]))
    with pytest.raises(MigrationGap) as info:
        plan(files, case.applied)
    exc = info.value
    assert exc.source == "bundled"
    assert exc.versions == tuple(sorted(m.version for m in files))
    assert exc.versions.count(dup_version) == 2


@given(case=cases(min_n=2, min_k=2), data=st.data())
def test_gap_in_recorded_versions_is_refused(case: Case, data: st.DataObject) -> None:
    # Drop a recorded row below the highest one, so the recorded versions have a hole.
    hole = data.draw(st.integers(min_value=1, max_value=case.k - 1))
    applied = data.draw(
        st.permutations([a for a in case.applied if a.version != hole]),
    )
    with pytest.raises(MigrationGap) as info:
        plan(case.files, applied)
    exc = info.value
    assert exc.source == "recorded"
    assert exc.versions == tuple(sorted(a.version for a in applied))
    assert hole not in exc.versions
