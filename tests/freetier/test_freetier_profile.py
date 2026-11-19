"""Smoke test of the running freetier profile (free-tier design §9; Requirements 5.10, 5.11,
6.3, 6.6, 7.1, 8.7, 10.8, 11.1 to 11.6).

Run it with `make freetier-smoke` after `make freetier-up`. Run by hand with
`uv run pytest -m freetier tests/freetier` it checks whatever is up, and skips when the
`ft-app` container is not running. Once it runs, a missing or unhealthy piece is a failure,
not a skip.

What it touches: the docker CLI (inspect, and `compose stop ft-app` in the last test), the
published ports on 127.0.0.1 (ft-app on `CAIRN_FREETIER_APP_PORT`, ft-db on
`CAIRN_FREETIER_DB_PORT`) and read-only queries on ft-db as `cairn_dbos` and `cairn_admin`.
Secrets (the proxy token and the two passwords) come from the environment or `.env`, are
kept in `SecretStr` and never printed. Container environment variables are read by name
only; no value is stored, compared or shown.

ft-app runs at 0.1 CPU (FV13), so everything is slow: HTTP calls use a 60 s timeout and the
checks that wait for a background job poll against a generous deadline instead of sleeping.

The last test stops ft-app, so it must stay at the end of the module. `make freetier-check`
runs the managed matrix next, which needs only ft-db, and the RAM check brings ft-app up
again.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Final

import httpx
import psycopg
import pytest
from pydantic import Field, SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

pytestmark = pytest.mark.freetier

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
PROJECT: Final = "cairn"
HOST: Final = "127.0.0.1"
HTTP_TIMEOUT_S: Final = 60.0
PROXY_HEADER: Final = "X-Cairn-Proxy-Token"
SCHEDULE_NAME: Final = "cairn-heartbeat"
HEARTBEAT_DEADLINE_S: Final = 240.0
"""Requirement 11.3 says three 10 s intervals; at 0.1 CPU the first run can come late, so the
poll keeps looking for this long and the test passes on the first SUCCESS row."""
READY_DEADLINE_S: Final = 120.0
POLL_S: Final = 5.0
STOP_BUDGET_S: Final = 20.0  # Requirement 8.7

READY_CHECKS: Final = frozenset({"database", "postgres", "extensions", "role", "schema", "jobs"})
REQUIRED_EXTENSIONS: Final = frozenset({"btree_gist", "vector"})

MIB: Final = 1024 * 1024
FT_APP_SECRET_KEYS: Final = frozenset(
    {
        "CAIRN_APP_DB_PASSWORD",
        "CAIRN_DBOS_DB_PASSWORD",
        "CAIRN_INGEST_DB_PASSWORD",
        "CAIRN_PROXY_TOKEN",
        "CAIRN_SESSION_SECRET",  # signs the Phase 1 session cookie
    }
)
FORBIDDEN_ON_FT_APP: Final = (
    "POSTGRES_PASSWORD",
    "CAIRN_OWNER_DB_PASSWORD",
    "CAIRN_DB_ADMIN_PASSWORD",
    "CAIRN_MANAGED_ADMIN_PASSWORD",
)
SECRET_LIKE: Final = re.compile(r"PASSWORD|TOKEN|SECRET|CREDENTIAL|API_?KEY|PRIVATE", re.IGNORECASE)

NOT_UP_MESSAGE: Final = "the freetier profile is not running (start it with `make freetier-up`)"


class FreetierEnv(BaseSettings):
    """Ports and secrets of the freetier profile, from the environment, then `.env`."""

    model_config = SettingsConfigDict(
        env_file=".env", env_ignore_empty=True, extra="ignore", frozen=True
    )
    app_port: int = Field(8010, alias="CAIRN_FREETIER_APP_PORT", ge=1, le=65535)
    db_port: int = Field(55433, alias="CAIRN_FREETIER_DB_PORT", ge=1, le=65535)
    proxy_token: SecretStr | None = Field(None, alias="CAIRN_PROXY_TOKEN")
    dbos_password: SecretStr | None = Field(None, alias="CAIRN_DBOS_DB_PASSWORD")
    admin_password: SecretStr | None = Field(None, alias="CAIRN_MANAGED_ADMIN_PASSWORD")

    def secret(self, name: str) -> str:
        """The value of a secret field, or a skip that names the `.env` key (never a value)."""
        value: SecretStr | None = getattr(self, name)
        if value is None or not value.get_secret_value():
            pytest.skip(f"{name} is not set; run `make env-update`")
        return value.get_secret_value()


def _docker(*args: str) -> str:
    result = subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=120, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(f"docker {args[0]} failed: {result.stderr.strip()}")
    return result.stdout


def _service(container: dict[str, Any]) -> str:
    return str(container["Config"]["Labels"].get("com.docker.compose.service", "?"))


def _inspect_project() -> dict[str, dict[str, Any]]:
    ids = _docker("ps", "-aq", "--filter", f"label=com.docker.compose.project={PROJECT}").split()
    inspected: list[dict[str, Any]] = json.loads(_docker("inspect", *ids)) if ids else []
    return {_service(c): c for c in inspected}


def _env_names(container: dict[str, Any]) -> set[str]:
    """Names of the container's environment variables. The values are dropped here."""
    return {str(entry).split("=", 1)[0] for entry in container["Config"].get("Env") or []}


@pytest.fixture(scope="module")
def env() -> FreetierEnv:
    try:
        return FreetierEnv()
    except ValidationError as exc:
        keys = sorted({str(e["loc"][0]) for e in exc.errors(include_input=False)})
        pytest.skip(f"freetier settings are invalid ({', '.join(keys)}); run `make env-update`")


@pytest.fixture(scope="module")
def containers() -> dict[str, dict[str, Any]]:
    """`docker inspect` output of every container of project `cairn`, by service name."""
    if shutil.which("docker") is None:
        pytest.skip("docker CLI not found; the freetier profile cannot be inspected")
    try:
        by_service = _inspect_project()
    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        pytest.skip(f"docker is not reachable: {exc}")
    app = by_service.get("ft-app")
    if app is None or not app["State"]["Running"]:
        pytest.skip(NOT_UP_MESSAGE)
    return by_service


def _get(env: FreetierEnv, path: str, *, token: str | None = None) -> httpx.Response:
    headers = {PROXY_HEADER: token} if token is not None else {}
    url = f"http://{HOST}:{env.app_port}{path}"
    try:
        return httpx.get(url, headers=headers, timeout=HTTP_TIMEOUT_S, follow_redirects=False)
    except httpx.HTTPError as exc:  # fail with the URL, not a bare connection error
        pytest.fail(f"GET {url} failed: {type(exc).__name__}")


def _connect(env: FreetierEnv, *, user: str, password: str, dbname: str) -> psycopg.Connection[Any]:
    return psycopg.connect(
        host=HOST,
        port=env.db_port,
        dbname=dbname,
        user=user,
        password=password,
        connect_timeout=10,
        application_name="cairn-freetier-smoke",
        autocommit=True,
    )


# --- HTTP: proxy token and readiness -----------------------------------------------------------


def test_healthz_is_200_without_the_proxy_header(
    env: FreetierEnv, containers: dict[str, dict[str, Any]]
) -> None:
    response = _get(env, "/healthz")

    assert response.status_code == 200


def test_readyz_is_404_without_the_header_and_for_a_wrong_one(
    env: FreetierEnv, containers: dict[str, dict[str, Any]]
) -> None:
    token = env.secret("proxy_token")
    wrong = "x" * len(token)  # same length, so only the comparison can reject it

    missing = _get(env, "/readyz")
    mismatch = _get(env, "/readyz", token=wrong)

    for response in (missing, mismatch):
        assert response.status_code == 404
        assert response.json() == {"error": "not_found"}
        assert response.headers["cache-control"] == "no-store"


def test_readyz_is_200_with_the_header_and_every_check_passes(
    env: FreetierEnv, containers: dict[str, dict[str, Any]]
) -> None:
    token = env.secret("proxy_token")
    deadline = time.monotonic() + READY_DEADLINE_S
    while True:
        response = _get(env, "/readyz", token=token)
        if response.status_code == 200 or time.monotonic() >= deadline:
            break
        time.sleep(POLL_S)  # the jobs check turns ok once DBOS has launched

    body = response.json()
    failing = {
        name: check.get("error") for name, check in body["checks"].items() if not check["ok"]
    }
    assert response.status_code == 200, f"/readyz not ready: {failing}"
    assert body["status"] == "ready"
    assert set(body["checks"]) == READY_CHECKS
    assert not failing
    assert set(body["extensions"]) == REQUIRED_EXTENSIONS  # Requirement 2.5, no pg_search


# --- the heartbeat schedule --------------------------------------------------------------------


def test_heartbeat_has_a_success_run_in_the_dbos_database(
    env: FreetierEnv, containers: dict[str, dict[str, Any]]
) -> None:
    password = env.secret("dbos_password")
    deadline = time.monotonic() + HEARTBEAT_DEADLINE_S
    successes = 0
    while True:
        # A fresh connection per poll, as the runtime role of the DBOS database itself.
        with _connect(env, user="cairn_dbos", password=password, dbname="cairn_dbos") as conn:
            try:
                row = conn.execute(
                    "SELECT count(*) FROM dbos.workflow_status "
                    "WHERE schedule_name = %s AND status = 'SUCCESS'",
                    (SCHEDULE_NAME,),
                ).fetchone()
            except psycopg.errors.UndefinedTable:
                row = (0,)  # DBOS has not created its tables yet
        successes = int(row[0]) if row else 0
        if successes >= 1 or time.monotonic() >= deadline:
            break
        time.sleep(POLL_S)

    assert successes >= 1, f"no SUCCESS run of {SCHEDULE_NAME} within {HEARTBEAT_DEADLINE_S:.0f} s"


# --- ft-db: no pg_search, the admin is a provider-style owner role -----------------------------


def test_pg_search_is_neither_installed_nor_available(
    env: FreetierEnv, containers: dict[str, dict[str, Any]]
) -> None:
    password = env.secret("admin_password")

    with _connect(env, user="cairn_admin", password=password, dbname="postgres") as conn:
        available = conn.execute(
            "SELECT count(*) FROM pg_available_extensions WHERE name = 'pg_search'"
        ).fetchone()
    assert available == (0,)

    for dbname in ("postgres", "cairn"):
        with _connect(env, user="cairn_admin", password=password, dbname=dbname) as conn:
            installed = conn.execute(
                "SELECT count(*) FROM pg_extension WHERE extname = 'pg_search'"
            ).fetchone()
        assert installed == (0,), f"pg_search is installed in database {dbname}"


def test_cairn_admin_is_not_a_superuser_and_bypasses_rls(
    env: FreetierEnv, containers: dict[str, dict[str, Any]]
) -> None:
    password = env.secret("admin_password")

    with _connect(env, user="cairn_admin", password=password, dbname="postgres") as conn:
        row = conn.execute(
            "SELECT rolsuper, rolbypassrls, rolcreaterole, rolcreatedb, rolreplication "
            "FROM pg_roles WHERE rolname = 'cairn_admin'"
        ).fetchone()

    # NOSUPERUSER, BYPASSRLS, CREATEROLE, CREATEDB, NOREPLICATION: the shape of neondb_owner.
    assert row == (False, True, True, True, False)


# --- container configuration: Render free limits and secret hygiene (names only) ---------------


def test_ft_app_has_the_render_free_limits(containers: dict[str, dict[str, Any]]) -> None:
    app = containers["ft-app"]
    host_config = app["HostConfig"]

    assert int(host_config["Memory"]) == 512 * MIB
    assert int(host_config["MemorySwap"]) == 512 * MIB
    assert int(host_config["NanoCpus"]) == 100_000_000  # cpus: 0.1
    assert host_config["ReadonlyRootfs"] is True
    assert str(app["Config"]["User"]).split(":")[0] == "10001"
    assert app["State"]["OOMKilled"] is False


def test_ft_app_environment_names_only_the_allowed_secrets(
    containers: dict[str, dict[str, Any]],
) -> None:
    names = _env_names(containers["ft-app"])

    secret_like = {name for name in names if SECRET_LIKE.search(name)}
    assert secret_like == FT_APP_SECRET_KEYS, (
        f"unexpected secret-like keys: {sorted(secret_like - FT_APP_SECRET_KEYS)}; "
        f"missing: {sorted(FT_APP_SECRET_KEYS - secret_like)}"
    )
    leaked = sorted(name for name in FORBIDDEN_ON_FT_APP if name in names)
    assert not leaked, f"ft-app carries admin, owner or superuser keys: {leaked}"


def test_only_ft_db_and_ft_admin_init_hold_the_superuser_password(
    containers: dict[str, dict[str, Any]],
) -> None:
    holders = sorted(
        service
        for service, container in containers.items()
        if service.startswith("ft-") and "POSTGRES_PASSWORD" in _env_names(container)
    )

    assert holders == ["ft-admin-init", "ft-db"]
    assert "POSTGRES_PASSWORD" not in _env_names(containers["ft-db-init"])


def test_the_one_shot_init_containers_exited_cleanly(
    containers: dict[str, dict[str, Any]],
) -> None:
    problems = [
        f"{name}: status={containers[name]['State']['Status']} "
        f"exit={containers[name]['State']['ExitCode']}"
        for name in ("ft-admin-init", "ft-db-init")
        if containers[name]["State"]["Status"] != "exited"
        or containers[name]["State"]["ExitCode"] != 0
    ]

    assert not problems, "; ".join(problems)


def test_only_loopback_ports_are_published(containers: dict[str, dict[str, Any]]) -> None:
    exposed: list[str] = []
    for service in ("ft-db", "ft-app"):
        bindings: dict[str, list[dict[str, str]] | None] = (
            containers[service]["HostConfig"].get("PortBindings") or {}
        )
        for port, hosts in bindings.items():
            for host in hosts or []:
                if host.get("HostIp") != HOST:
                    exposed.append(f"{service} {port} on {host.get('HostIp') or 'all interfaces'}")

    assert not exposed, "; ".join(sorted(exposed))
    assert containers["ft-app"]["HostConfig"]["PortBindings"]
    assert containers["ft-db"]["HostConfig"]["PortBindings"]


# --- graceful stop (keep this test last: it stops ft-app) --------------------------------------


def test_zz_stopping_ft_app_takes_under_20_seconds(containers: dict[str, dict[str, Any]]) -> None:
    """Requirement 8.7. uvicorn re-raises SIGTERM after a clean shutdown, so the container exit
    status is 143 even on success (task 5.1); only the elapsed time and the final state count."""
    started = time.monotonic()
    result = subprocess.run(
        ["docker", "compose", "--profile", "freetier", "stop", "ft-app"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    elapsed = time.monotonic() - started

    state = _inspect_project()["ft-app"]["State"]
    assert state["Running"] is False, f"ft-app still running; compose said: {result.stderr[-200:]}"
    assert elapsed < STOP_BUDGET_S, (
        f"stopping ft-app took {elapsed:.1f} s (budget {STOP_BUDGET_S} s)"
    )
