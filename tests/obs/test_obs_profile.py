"""Integration check of the running obs profile.

Design §4 and §9; Requirements 9.8 and 9.9.

Run it with `make obs-check`, which starts core and obs first (`make up-obs`). Run by hand
with `uv run pytest -m obs tests/obs` it checks whatever is up and skips when the collector
is not running. Once the collector runs, a stack that does not export spans is a failure.

The flow: one `/readyz` call carrying a unique query value, then the collector log must show
that span (debug exporter), and Prometheus must return a spanmetrics series for `cairn-api`
(the collector flushes every 15 s and Prometheus scrapes on its own interval, so the test
polls). Grafana must list the provisioned Prometheus data source; that call logs in with
GRAFANA_ADMIN_PASSWORD from the environment or `.env`, and the value is never printed.

The tests read the live containers of the compose project `cairn` through the docker CLI
and call published ports on 127.0.0.1. Nothing is started, stopped or changed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Final

import httpx
import pytest

pytestmark = pytest.mark.obs

PROJECT: Final = "cairn"
API_URL: Final = "http://127.0.0.1:8000"
PROMETHEUS_URL: Final = f"http://127.0.0.1:{os.environ.get('CAIRN_PROMETHEUS_PORT', '9090')}"
GRAFANA_URL: Final = f"http://127.0.0.1:{os.environ.get('CAIRN_GRAFANA_PORT', '3000')}"
HTTP_TIMEOUT_S: Final = 5.0
# Collector flush (15 s) + Prometheus scrape interval + slack.
WAIT_S: Final = 90.0
POLL_S: Final = 3.0
ENV_FILE: Final = Path(__file__).resolve().parents[2] / ".env"


def _docker(*args: str) -> str:
    result = subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"docker {args[0]} failed: {result.stderr.strip()}")
    # `docker logs` writes the container's stderr (where the collector logs) to stderr.
    return result.stdout + result.stderr if args[0] == "logs" else result.stdout


def _container_id(service: str) -> str | None:
    ids = _docker(
        "ps",
        "-q",
        "--filter",
        f"label=com.docker.compose.project={PROJECT}",
        "--filter",
        f"label=com.docker.compose.service={service}",
    ).split()
    return ids[0] if ids else None


@pytest.fixture(scope="module")
def collector_id() -> str:
    if shutil.which("docker") is None:
        pytest.skip("docker CLI not found; the obs profile cannot be inspected")
    try:
        container = _container_id("otel-collector")
    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        pytest.skip(f"docker is not reachable: {exc}")
    if container is None:
        pytest.skip("the obs profile is not running (start it with `make up-obs`)")
    return container


@pytest.fixture(scope="module")
def exporting_api(collector_id: str) -> None:
    """The api must export OTLP; otherwise this is `make up`, not `make up-obs`."""
    api = _container_id("api")
    assert api is not None, "api is not running"
    env: list[str] = json.loads(_docker("inspect", "--format", "{{json .Config.Env}}", api))
    assert "OTEL_TRACES_EXPORTER=otlp" in env, (
        "api runs without OTEL_TRACES_EXPORTER=otlp; start the stack with `make up-obs`"
    )


@pytest.fixture(scope="module")
def probe(exporting_api: None) -> str:
    """Make one /readyz call and return the unique value carried in its query string."""
    token = uuid.uuid4().hex
    try:
        response = httpx.get(f"{API_URL}/readyz", params={"probe": token}, timeout=HTTP_TIMEOUT_S)
    except httpx.HTTPError as exc:
        pytest.fail(f"GET {API_URL}/readyz failed: {type(exc).__name__}")
    assert response.status_code == 200
    return token


def _wait_for(description: str, check: Any) -> Any:
    deadline = time.monotonic() + WAIT_S
    last: Any = None
    while True:
        last = check()
        if last:
            return last
        if time.monotonic() >= deadline:
            pytest.fail(f"timed out after {WAIT_S:.0f} s waiting for {description}")
        time.sleep(POLL_S)


def test_collector_log_contains_the_readyz_span(collector_id: str, probe: str) -> None:
    def seen() -> bool:
        return probe in _docker("logs", collector_id)

    _wait_for("the /readyz span in the collector log", seen)


def _spanmetric_series(service: str) -> list[dict[str, Any]]:
    try:
        response = httpx.get(
            f"{PROMETHEUS_URL}/api/v1/query",
            params={"query": f'traces_span_metrics_calls_total{{service_name="{service}"}}'},
            timeout=HTTP_TIMEOUT_S,
        )
        response.raise_for_status()
    except httpx.HTTPError:
        return []  # Prometheus may still be starting; the caller keeps polling
    body = response.json()
    assert body["status"] == "success"
    result: list[dict[str, Any]] = body["data"]["result"]
    return result


def test_prometheus_returns_a_spanmetrics_series_for_cairn_api(
    collector_id: str, probe: str
) -> None:
    def readyz_series() -> list[dict[str, Any]]:
        return [
            s
            for s in _spanmetric_series("cairn-api")
            if "/readyz" in s["metric"].get("span_name", "") and float(s["value"][1]) > 0
        ]

    series = _wait_for("a /readyz spanmetrics series for cairn-api", readyz_series)

    assert {s["metric"]["service_name"] for s in series} == {"cairn-api"}


def _grafana_password() -> str | None:
    value = os.environ.get("GRAFANA_ADMIN_PASSWORD")
    if value:
        return value
    if ENV_FILE.is_file():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            key, _, rest = line.partition("=")
            if key.strip() == "GRAFANA_ADMIN_PASSWORD" and rest.strip():
                return rest.strip().strip("'\"")
    return None


def test_grafana_has_the_provisioned_prometheus_data_source(collector_id: str) -> None:
    password = _grafana_password()
    if password is None:
        pytest.skip("GRAFANA_ADMIN_PASSWORD is not set in the environment or .env")
    try:
        response = httpx.get(
            f"{GRAFANA_URL}/api/datasources",
            auth=("admin", password),
            timeout=HTTP_TIMEOUT_S,
        )
    except httpx.HTTPError as exc:
        pytest.fail(f"GET {GRAFANA_URL}/api/datasources failed: {type(exc).__name__}")
    assert response.status_code == 200, f"Grafana answered {response.status_code}"
    sources = [(d["type"], d["uid"]) for d in response.json()]
    assert ("prometheus", "cairn-prometheus") in sources, f"data sources: {sources}"
