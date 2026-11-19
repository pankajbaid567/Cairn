"""Property test for readiness with the version floor and the jobs check (task 5.3).

Feature: cairn-free-tier-retarget, Property 1: Readiness is the conjunction of its checks, with
the version floor and the jobs check

**Validates: Requirements 2.2, 2.3, 8.6**

Every example draws a probe result (a ``DbProbe`` with arbitrary server version, extension map
with arbitrary version strings, role flags and schema version, or a ``DbUnavailable``), an
expected head and an optional ``JobsStatus``, and compares ``evaluate_readiness`` with a
hand-written oracle:

- the status is 200 if and only if the server major is 18, ``vector`` and ``btree_gist`` are
  present, ``vector`` parses to at least 0.8.0, the role is neither a superuser nor BYPASSRLS,
  the schema version equals the head, and (when a jobs status is given) DBOS has launched;
- a missing extension gives ``extensions_missing``, otherwise a ``vector`` below the floor (or
  with a version that is not dotted digits) gives ``extensions_outdated``;
- the ``jobs`` check exists if and only if a jobs status was given, also when the database did
  not answer, and fails with ``jobs_not_launched``;
- ``extensions`` holds exactly the present required extensions;
- a check carries an error code if and only if it failed.

The generators lean towards passing values (each condition is good about three times in four),
so ready results and single-fault results both occur often. No database is involved.

The oracle reads the requirements, not ``cairn_core.health``: it has its own version parser and
its own floor, so a change to either in the code shows up here.
"""

from __future__ import annotations

from typing import Any

from hypothesis import given
from hypothesis import strategies as st

from cairn_core.bootstrap import MIN_EXTENSION_VERSIONS, REQUIRED_EXTENSIONS
from cairn_core.health import (
    DbProbe,
    DbUnavailable,
    JobsStatus,
    ProbeResult,
    ReadyReport,
    evaluate_readiness,
)

DB_CHECKS = ("database", "postgres", "extensions", "role", "schema")
UNAVAILABLE_CODES = ("db_unreachable", "db_timeout", "probe_failed")
VECTOR_FLOOR = (0, 8, 0)

NEAR_THE_FLOOR = [
    "0.7.4",
    "0.7.99",
    "0.8",
    "0.8.0",
    "0.8.1",
    "0.8.6",
    "0.10.0",
    "1.0",
    "1",
    "0.8.0.1",
    "0.0.0",
    "",
    "x",
    "0.8.0-rc1",
    "1..2",
    ".8",
    "0.8.",
]


def mostly(good: st.SearchStrategy[Any], bad: st.SearchStrategy[Any]) -> st.SearchStrategy[Any]:
    """Three draws in four come from ``good``."""
    return st.one_of(good, good, good, bad)


vector_versions = st.one_of(
    st.sampled_from(NEAR_THE_FLOOR),
    st.from_regex(r"[0-9]{1,2}(\.[0-9]{1,2}){0,3}", fullmatch=True),
    st.text(alphabet="0123456789.-x", max_size=8),
)
passing_vector_versions = st.sampled_from(["0.8", "0.8.0", "0.8.6", "0.10.0", "1.0", "0.8.0.1"])
any_versions = st.one_of(vector_versions, st.text(max_size=6))


@st.composite
def extension_maps(draw: st.DrawFn) -> dict[str, str]:
    """Required extensions present or not, with arbitrary versions, plus some unrelated ones."""
    result: dict[str, str] = {}
    if draw(mostly(st.just(True), st.booleans())):
        result["vector"] = draw(mostly(passing_vector_versions, vector_versions))
    if draw(mostly(st.just(True), st.booleans())):
        result["btree_gist"] = draw(any_versions)  # no floor: any text is accepted
    for other in draw(st.lists(st.sampled_from(["plpgsql", "pg_trgm", "hstore"]), unique=True)):
        result[other] = draw(any_versions)
    return result


@st.composite
def probes(draw: st.DrawFn, head: int) -> DbProbe:
    major = draw(mostly(st.just(18), st.integers(9, 22)))
    return DbProbe(
        server_version_num=int(major) * 10000 + draw(st.integers(0, 99)),
        extensions=draw(extension_maps()),
        role_name=draw(st.text(st.characters(codec="ascii", categories=["L", "N"]), min_size=1)),
        role_is_superuser=bool(draw(mostly(st.just(False), st.booleans()))),
        role_bypasses_rls=bool(draw(mostly(st.just(False), st.booleans()))),
        schema_version=draw(mostly(st.just(head), st.one_of(st.none(), st.integers(0, 6)))),
        latency_ms=draw(st.floats(0, 60_000, allow_nan=False, allow_infinity=False)),
    )


jobs_statuses = st.builds(
    JobsStatus,
    launched=mostly(st.just(True), st.just(False)),
    attempts=st.integers(0, 50),
)


@st.composite
def cases(draw: st.DrawFn) -> tuple[ProbeResult, int, JobsStatus | None]:
    head = draw(st.integers(1, 5))
    result: ProbeResult = draw(
        mostly(probes(head), st.sampled_from(UNAVAILABLE_CODES).map(DbUnavailable))  # type: ignore[arg-type]
    )
    jobs = draw(st.none() | jobs_statuses)
    return result, head, jobs


def floor_state(version_text: str) -> bool:
    """Requirement 2.3: True when ``vector`` at this version meets 0.8.0. Text that is not
    dotted ASCII digits cannot be shown to meet it, so it does not."""
    parts = version_text.split(".")
    if not all(part and all(c in "0123456789" for c in part) for part in parts):
        return False
    numbers = [int(part) for part in parts]
    numbers += [0] * (len(VECTOR_FLOOR) - len(numbers))
    return tuple(numbers) >= VECTOR_FLOOR


def expected_codes(
    result: ProbeResult, head: int, jobs: JobsStatus | None
) -> dict[str, str | None]:
    """check name -> expected error code (None: the check passes), in report order."""
    codes: dict[str, str | None] = {}
    if isinstance(result, DbUnavailable):
        codes["database"] = result.code
    else:
        extensions = result.extensions
        if any(name not in extensions for name in REQUIRED_EXTENSIONS):
            extension_code: str | None = "extensions_missing"
        elif not floor_state(extensions["vector"]):
            extension_code = "extensions_outdated"
        else:
            extension_code = None
        version = result.schema_version or 0
        codes.update(
            {
                "database": None,
                "postgres": None
                if result.server_version_num // 10000 == 18
                else "postgres_major_mismatch",
                "extensions": extension_code,
                "role": (
                    "role_unsafe" if result.role_is_superuser or result.role_bypasses_rls else None
                ),
                "schema": (
                    None
                    if version == head
                    else ("schema_behind" if version < head else "schema_ahead")
                ),
            }
        )
    if jobs is not None:
        codes["jobs"] = None if jobs.launched else "jobs_not_launched"
    return codes


def present_required(result: ProbeResult) -> dict[str, str]:
    if isinstance(result, DbUnavailable):
        return {}
    return {
        name: result.extensions[name] for name in REQUIRED_EXTENSIONS if name in result.extensions
    }


def evaluate(
    case: tuple[ProbeResult, int, JobsStatus | None],
) -> tuple[int, ReadyReport, dict[str, str | None]]:
    result, head, jobs = case
    status_code, report = evaluate_readiness(result, head, jobs)
    return status_code, report, expected_codes(result, head, jobs)


def test_the_oracle_floor_matches_the_configured_floor() -> None:
    """The oracle hard-codes 0.8.0 on purpose; this pins it to the shipped constant."""
    assert MIN_EXTENSION_VERSIONS == {"vector": VECTOR_FLOOR}
    assert REQUIRED_EXTENSIONS == ("vector", "btree_gist")


@given(case=cases())
def test_status_is_200_iff_every_check_passes(
    case: tuple[ProbeResult, int, JobsStatus | None],
) -> None:
    """Feature: cairn-free-tier-retarget, Property 1: Readiness is the conjunction of its checks,
    with the version floor and the jobs check (status, report status, failing codes, order)."""
    status_code, report, codes = evaluate(case)
    ready = all(code is None for code in codes.values())
    assert status_code == (200 if ready else 503)
    assert report.status == ("ready" if ready else "not_ready")
    assert tuple(report.checks) == tuple(codes)
    assert {name: check.error for name, check in report.checks.items()} == codes
    assert [name for name, check in report.checks.items() if not check.ok] == [
        name for name, code in codes.items() if code is not None
    ]


@given(case=cases())
def test_extensions_field_lists_exactly_the_present_required_extensions(
    case: tuple[ProbeResult, int, JobsStatus | None],
) -> None:
    """Feature: cairn-free-tier-retarget, Property 1: Readiness is the conjunction of its checks,
    with the version floor and the jobs check (the extensions field)."""
    _, report, _ = evaluate(case)
    assert report.extensions == present_required(case[0])
    assert set(report.extensions) <= set(REQUIRED_EXTENSIONS)


@given(head=st.integers(1, 5), data=st.data())
def test_extension_codes_split_missing_from_outdated(head: int, data: st.DataObject) -> None:
    """Feature: cairn-free-tier-retarget, Property 1: Readiness is the conjunction of its checks,
    with the version floor and the jobs check (extensions_missing wins; extensions_outdated only
    when vector is present below the floor; names in the detail)."""
    probe = data.draw(probes(head))
    status_code, report = evaluate_readiness(probe, head)
    check = report.checks["extensions"]
    missing = [n for n in REQUIRED_EXTENSIONS if n not in probe.extensions]
    outdated = "vector" in probe.extensions and not floor_state(probe.extensions["vector"])
    if missing:
        assert check.error == "extensions_missing"
        assert check.detail["missing"] == ",".join(missing)
    elif outdated:
        assert check.error == "extensions_outdated"
        assert "missing" not in check.detail
    else:
        assert check.ok
        assert check.error is None
    if outdated:
        assert check.detail["outdated"] == "vector"
    else:
        assert "outdated" not in check.detail
    if check.error is not None:
        assert status_code == 503


@given(
    version=vector_versions,
    other=any_versions,
    head=st.integers(1, 5),
    jobs=st.none() | jobs_statuses,
)
def test_vector_version_decides_readiness_when_everything_else_is_good(
    version: str, other: str, head: int, jobs: JobsStatus | None
) -> None:
    """Feature: cairn-free-tier-retarget, Property 1: Readiness is the conjunction of its checks,
    with the version floor and the jobs check (the floor on vector alone; btree_gist has none)."""
    probe = DbProbe(
        server_version_num=180002,
        extensions={"vector": version, "btree_gist": other},
        role_name="cairn_app",
        role_is_superuser=False,
        role_bypasses_rls=False,
        schema_version=head,
        latency_ms=1.0,
    )
    status_code, report = evaluate_readiness(probe, head, jobs)
    jobs_ok = jobs is None or jobs.launched
    assert status_code == (200 if floor_state(version) and jobs_ok else 503)
    assert report.checks["extensions"].ok is floor_state(version)
    assert report.extensions == {"vector": version, "btree_gist": other}


@given(case=cases())
def test_jobs_check_exists_exactly_when_jobs_is_given(
    case: tuple[ProbeResult, int, JobsStatus | None],
) -> None:
    """Feature: cairn-free-tier-retarget, Property 1: Readiness is the conjunction of its checks,
    with the version floor and the jobs check (jobs check presence, code and attempts detail,
    also when the database is unavailable)."""
    result, head, jobs = case
    _, report = evaluate_readiness(result, head, jobs)
    assert ("jobs" in report.checks) is (jobs is not None)
    if jobs is not None:
        check = report.checks["jobs"]
        assert check.ok is jobs.launched
        assert check.error == (None if jobs.launched else "jobs_not_launched")
        assert check.detail == {"attempts": jobs.attempts}
        assert list(report.checks)[-1] == "jobs"
    if isinstance(result, DbUnavailable):
        assert next(iter(report.checks)) == "database"
        assert set(report.checks) == ({"database", "jobs"} if jobs is not None else {"database"})
        assert report.extensions == {}


@given(case=cases())
def test_a_check_carries_a_code_exactly_when_it_failed(
    case: tuple[ProbeResult, int, JobsStatus | None],
) -> None:
    """Feature: cairn-free-tier-retarget, Property 1: Readiness is the conjunction of its checks,
    with the version floor and the jobs check (no check carries a code when ok)."""
    _, report, _ = evaluate(case)
    for check in report.checks.values():
        assert (check.error is None) is check.ok
        assert check.to_body().keys() >= {"ok"}
        assert ("error" in check.to_body()) is (not check.ok)
