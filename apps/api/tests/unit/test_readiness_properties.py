"""Property test for readiness evaluation (task 5.2, design Property 2).

Feature: cairn-phase-0-foundations, Property 2: Readiness is the conjunction of its checks

**Validates: Requirements 7.2, 7.4, 7.5, 7.6, 7.7**

Every example draws a ``DbProbe`` and an expected schema head, then compares
``evaluate_readiness`` and the ``GET /readyz`` response (through ``build_app`` with an
injected probe) with a hand-written oracle:

- the status is 200 if and only if the server major is 18, both required extensions are
  present and ``vector`` is at least 0.8.0 (retarget FT-03: a missing extension gives
  ``extensions_missing``, otherwise one below its floor gives ``extensions_outdated``), the
  role is neither a superuser nor BYPASSRLS, and the schema version equals the head;
- every failing condition shows up under its own check with its own error code, and a passing
  check has none;
- ``extensions`` lists exactly the required extensions that are present.

The generators lean towards passing values (each condition is good about three times in
four), so ready probes and single-fault probes both occur often. ``DbUnavailable`` always gives
503 with a single ``database`` check. The app is built once per module with
``ApiSettings.model_validate`` (aliases, no environment) and a fixed head; the probe it calls is
read from a holder, because Hypothesis does not reset function-scoped fixtures between
examples.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from cairn_api.main import build_app
from cairn_core.bootstrap import REQUIRED_EXTENSIONS
from cairn_core.config import ApiSettings
from cairn_core.health import (
    DbProbe,
    DbUnavailable,
    ProbeResult,
    ReadyReport,
    evaluate_readiness,
    postgres_version_text,
)

CHECK_NAMES = ("database", "postgres", "extensions", "role", "schema")
UNAVAILABLE_CODES = ("db_unreachable", "db_timeout", "probe_failed")

versions = st.from_regex(r"[0-9]{1,2}\.[0-9]{1,2}(\.[0-9]{1,2})?", fullmatch=True)
extra_extension_names = st.sampled_from(["plpgsql", "pg_trgm", "uuid-ossp", "hstore"])


def mostly(good: st.SearchStrategy[Any], bad: st.SearchStrategy[Any]) -> st.SearchStrategy[Any]:
    """Three draws in four come from ``good``."""
    return st.one_of(good, good, good, bad)


@st.composite
def server_versions(draw: st.DrawFn) -> int:
    major = draw(mostly(st.just(18), st.integers(9, 22)))
    return int(major) * 10000 + draw(st.integers(0, 99))


@st.composite
def extension_maps(draw: st.DrawFn) -> dict[str, str]:
    names = draw(
        mostly(
            st.just(list(REQUIRED_EXTENSIONS)),
            st.lists(st.sampled_from(REQUIRED_EXTENSIONS), unique=True, max_size=3),
        )
    )
    others = draw(st.lists(extra_extension_names, unique=True, max_size=2))
    return {name: draw(versions) for name in [*names, *others]}


@st.composite
def probes(draw: st.DrawFn, head: int) -> DbProbe:
    schema_version = draw(mostly(st.just(head), st.one_of(st.none(), st.integers(0, 6))))
    return DbProbe(
        server_version_num=draw(server_versions()),
        extensions=draw(extension_maps()),
        role_name=draw(st.text(st.characters(codec="ascii", categories=["L", "N"]), min_size=1)),
        role_is_superuser=bool(draw(mostly(st.just(False), st.booleans()))),
        role_bypasses_rls=bool(draw(mostly(st.just(False), st.booleans()))),
        schema_version=schema_version,
        latency_ms=draw(st.floats(0, 60_000, allow_nan=False, allow_infinity=False)),
    )


@st.composite
def cases(draw: st.DrawFn) -> tuple[DbProbe, int]:
    head = draw(st.integers(1, 5))
    return draw(probes(head)), head


APP_HEAD = 3
CURRENT: dict[str, ProbeResult] = {}


async def injected_probe() -> ProbeResult:
    return CURRENT["result"]


CLIENT = TestClient(
    build_app(
        ApiSettings.model_validate(
            {
                "CAIRN_DB_HOST": "127.0.0.1",
                "CAIRN_DB_PORT": 1,
                "CAIRN_APP_DB_PASSWORD": "readiness-property-pw",
                "CAIRN_PROXY_TOKEN": None,  # explicit: a local .env may hold a token
            }
        ),
        probe=injected_probe,
        expected_head=APP_HEAD,
    )
)


@dataclass(frozen=True)
class Expected:
    """What the oracle says about one probe, written from the requirements, not from the code."""

    major_ok: bool
    missing: frozenset[str]
    outdated: bool
    role_ok: bool
    schema_error: str | None  # None: the version equals the head

    @property
    def codes(self) -> dict[str, str | None]:
        return {
            "database": None,
            "postgres": None if self.major_ok else "postgres_major_mismatch",
            "extensions": (
                "extensions_missing"
                if self.missing
                else ("extensions_outdated" if self.outdated else None)
            ),
            "role": None if self.role_ok else "role_unsafe",
            "schema": self.schema_error,
        }

    @property
    def ready(self) -> bool:
        return all(code is None for code in self.codes.values())


def vector_outdated(extensions: Mapping[str, str]) -> bool:
    """From Requirement 2.3: a present ``vector`` below 0.8.0 (or unreadable) is outdated."""
    text = extensions.get("vector")
    if text is None:
        return False
    parts = text.split(".")
    if not all(part.isdigit() for part in parts):
        return True
    numbers = [int(part) for part in parts] + [0] * (3 - len(parts))
    return numbers < [0, 8, 0]


def oracle(probe: DbProbe, head: int) -> Expected:
    version = 0 if probe.schema_version is None else probe.schema_version
    schema_error = None
    if version < head:
        schema_error = "schema_behind"
    elif version > head:
        schema_error = "schema_ahead"
    return Expected(
        major_ok=probe.server_version_num // 10000 == 18,
        missing=frozenset(set(REQUIRED_EXTENSIONS) - set(probe.extensions)),
        outdated=vector_outdated(probe.extensions),
        role_ok=not probe.role_is_superuser and not probe.role_bypasses_rls,
        schema_error=schema_error,
    )


def check_report(status_code: int, report: ReadyReport, probe: DbProbe, head: int) -> None:
    want = oracle(probe, head)
    assert status_code == (200 if want.ready else 503)
    assert report.status == ("ready" if want.ready else "not_ready")
    assert tuple(report.checks) == CHECK_NAMES
    assert {name: check.error for name, check in report.checks.items()} == want.codes
    assert {name: check.ok for name, check in report.checks.items()} == {
        name: code is None for name, code in want.codes.items()
    }
    present = {
        name: probe.extensions[name] for name in REQUIRED_EXTENSIONS if name in probe.extensions
    }
    assert report.extensions == present
    if want.missing:
        missing = report.checks["extensions"].detail["missing"]
        assert isinstance(missing, str)
        assert set(missing.split(",")) == want.missing
    assert report.checks["postgres"].detail["version"] == postgres_version_text(
        probe.server_version_num
    )
    assert report.checks["schema"].detail["expected"] == head


@given(case=cases())
def test_status_is_the_conjunction_of_the_checks(case: tuple[DbProbe, int]) -> None:
    """Feature: cairn-phase-0-foundations, Property 2: Readiness is the conjunction of its
    checks (evaluate_readiness agrees with the oracle on status, codes and extensions)."""
    probe, head = case
    status_code, report = evaluate_readiness(probe, head)
    check_report(status_code, report, probe, head)


@given(head=st.integers(1, 5), data=st.data())
def test_a_probe_that_meets_every_condition_is_ready(head: int, data: st.DataObject) -> None:
    """Feature: cairn-phase-0-foundations, Property 2: Readiness is the conjunction of its
    checks (all conditions true: 200, no error code anywhere)."""
    good = replace(
        data.draw(probes(head)),
        server_version_num=data.draw(server_versions().filter(lambda n: n // 10000 == 18)),
        extensions={**data.draw(extension_maps()), **dict.fromkeys(REQUIRED_EXTENSIONS, "1.0")},
        role_is_superuser=False,
        role_bypasses_rls=False,
        schema_version=head,
    )
    status_code, report = evaluate_readiness(good, head)
    assert status_code == 200
    assert report.status == "ready"
    assert all(check.ok and check.error is None for check in report.checks.values())
    assert set(REQUIRED_EXTENSIONS) == set(report.extensions)


@given(head=st.integers(1, 5), fault=st.sampled_from(CHECK_NAMES[1:]), data=st.data())
def test_one_failing_condition_gives_503_with_exactly_its_own_code(
    head: int, fault: str, data: st.DataObject
) -> None:
    """Feature: cairn-phase-0-foundations, Property 2: Readiness is the conjunction of its
    checks (one condition false: 503 and only that check carries a code)."""
    good = replace(
        data.draw(probes(head)),
        server_version_num=180000 + data.draw(st.integers(0, 99)),
        extensions=dict.fromkeys(REQUIRED_EXTENSIONS, "1.0"),
        role_is_superuser=False,
        role_bypasses_rls=False,
        schema_version=head,
    )
    if fault == "postgres":
        bad = replace(
            good, server_version_num=data.draw(st.integers(9, 22).filter(lambda m: m != 18)) * 10000
        )
        code = "postgres_major_mismatch"
    elif fault == "extensions":
        dropped = data.draw(st.lists(st.sampled_from(REQUIRED_EXTENSIONS), min_size=1, unique=True))
        bad = replace(
            good, extensions={k: v for k, v in good.extensions.items() if k not in dropped}
        )
        code = "extensions_missing"
    elif fault == "role":
        superuser = data.draw(st.booleans())
        bad = replace(
            good,
            role_is_superuser=superuser,
            role_bypasses_rls=not superuser or data.draw(st.booleans()),
        )
        code = "role_unsafe"
    else:
        other = data.draw(
            st.one_of(st.none(), st.integers(0, 6)).filter(lambda v: (v or 0) != head)
        )
        bad = replace(good, schema_version=other)
        code = "schema_behind" if (other or 0) < head else "schema_ahead"
    status_code, report = evaluate_readiness(bad, head)
    assert status_code == 503
    assert {name: check.error for name, check in report.checks.items() if check.error} == {
        fault: code
    }
    assert [name for name, check in report.checks.items() if not check.ok] == [fault]


@given(code=st.sampled_from(UNAVAILABLE_CODES), head=st.integers(1, 5))
def test_an_unavailable_database_gives_503_with_a_single_database_check(
    code: str, head: int
) -> None:
    """Feature: cairn-phase-0-foundations, Property 2: Readiness is the conjunction of its
    checks (no probe result: 503, one failing database check, no extensions)."""
    status_code, report = evaluate_readiness(DbUnavailable(code), head)  # type: ignore[arg-type]
    assert status_code == 503
    assert report.status == "not_ready"
    assert list(report.checks) == ["database"]
    assert report.checks["database"].ok is False
    assert report.checks["database"].error == code
    assert report.extensions == {}


@settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(probe=probes(APP_HEAD), unavailable=st.none() | st.sampled_from(UNAVAILABLE_CODES))
def test_readyz_response_matches_the_oracle(probe: DbProbe, unavailable: str | None) -> None:
    """Feature: cairn-phase-0-foundations, Property 2: Readiness is the conjunction of its
    checks (GET /readyz carries the evaluated status code and body)."""
    head = APP_HEAD
    result: ProbeResult = probe if unavailable is None else DbUnavailable(unavailable)  # type: ignore[arg-type]
    CURRENT["result"] = result
    response = CLIENT.get("/readyz")
    status_code, report = evaluate_readiness(result, head)
    assert response.status_code == status_code
    assert response.json() == report.to_body()
    if isinstance(result, DbProbe):
        check_report(response.status_code, report, result, head)
        body = response.json()
        assert body["status"] == ("ready" if response.status_code == 200 else "not_ready")
        assert body["extensions"] == report.extensions
    else:
        assert response.status_code == 503
        assert list(response.json()["checks"]) == ["database"]
