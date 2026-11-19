"""Smoke test of the running core profile.

Design §4 and §15; Requirements 4.2, 4.5, 4.9, 7.2, 17.6.

Run it with `make smoke`, which starts the stack first (`make up`). Run by hand with
`uv run pytest -m smoke tests/smoke` it checks whatever is up, and skips when none of
api, worker and web is running. Once any of them runs, a missing or unhealthy piece is a
failure, not a skip.

The tests read the live containers of the compose project `cairn` through the docker CLI
and call the published ports on 127.0.0.1. Nothing is started, stopped or changed, and no
secret value is read or printed.

The page text is rendered by the browser, so the tests never look for widget text in the
raw HTML; the document title is the most they check.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Mapping
from typing import Any, Final

import httpx
import pytest

pytestmark = pytest.mark.smoke

PROJECT: Final = "cairn"
API_URL: Final = "http://127.0.0.1:8000"
WEB_URL: Final = "http://127.0.0.1:8080"
HTTP_TIMEOUT_S: Final = 5.0

LONG_RUNNING: Final = ("db", "api", "worker", "web")
ONE_SHOT: Final = ("db-init",)
REQUIRED_EXTENSIONS: Final = ("vector", "btree_gist")
MEMORY_BUDGET_BYTES: Final = 4 * 1024**3  # Requirement 4.5: at most 4 GB


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
    return result.stdout


def _service(container: Mapping[str, Any]) -> str:
    return str(container["Config"]["Labels"].get("com.docker.compose.service", "?"))


@pytest.fixture(scope="module")
def containers() -> dict[str, dict[str, Any]]:
    """`docker inspect` output of every container of the compose project, by service name."""
    if shutil.which("docker") is None:
        pytest.skip("docker CLI not found; the core profile cannot be inspected")
    try:
        ids = _docker(
            "ps", "-aq", "--filter", f"label=com.docker.compose.project={PROJECT}"
        ).split()
    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        pytest.skip(f"docker is not reachable: {exc}")
    inspected: list[dict[str, Any]] = json.loads(_docker("inspect", *ids)) if ids else []
    by_service = {_service(c): c for c in inspected}
    running = [name for name in ("api", "worker", "web") if _is_running(by_service.get(name))]
    if not running:
        pytest.skip("the core profile is not running (start it with `make up`)")
    return by_service


def _is_running(container: Mapping[str, Any] | None) -> bool:
    return container is not None and bool(container["State"]["Running"])


def _get(url: str) -> httpx.Response:
    try:
        return httpx.get(url, timeout=HTTP_TIMEOUT_S, follow_redirects=False)
    except httpx.HTTPError as exc:  # fail with the URL, not a bare connection error
        pytest.fail(f"GET {url} failed: {type(exc).__name__}")


def test_readyz_is_200_and_lists_exactly_the_two_extensions(
    containers: dict[str, dict[str, Any]],
) -> None:
    response = _get(f"{API_URL}/readyz")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    for name in REQUIRED_EXTENSIONS:
        assert body["extensions"].get(name), f"{name} missing from /readyz extensions"
    # Retarget Requirement 2.5: btree_gist and vector only.
    assert sorted(body["extensions"]) == ["btree_gist", "vector"]


def test_web_page_is_served(containers: dict[str, dict[str, Any]]) -> None:
    response = _get(f"{WEB_URL}/")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    # The heading and the widget are rendered client-side; only the static title is in the HTML.
    assert "<title>Cairn</title>" in response.text


def test_readyz_through_the_web_proxy_is_200(containers: dict[str, dict[str, Any]]) -> None:
    response = _get(f"{WEB_URL}/api/readyz")

    assert response.status_code == 200
    assert response.json()["status"] == "ready"


def test_every_container_is_healthy(containers: dict[str, dict[str, Any]]) -> None:
    problems: list[str] = []
    for name in LONG_RUNNING:
        container = containers.get(name)
        if container is None:
            problems.append(f"{name}: no container")
            continue
        state = container["State"]
        health = state.get("Health", {}).get("Status", "none")
        if not state["Running"] or health != "healthy":
            problems.append(f"{name}: status={state['Status']} health={health}")
    for name in ONE_SHOT:
        container = containers.get(name)
        if container is None:
            problems.append(f"{name}: no container")
        elif container["State"]["Status"] != "exited" or container["State"]["ExitCode"] != 0:
            state = container["State"]
            problems.append(f"{name}: status={state['Status']} exit={state['ExitCode']}")
    extra = sorted(set(containers) - set(LONG_RUNNING) - set(ONE_SHOT))
    for name in extra:  # any other service of the project must be healthy too
        state = containers[name]["State"]
        if state["Running"] and state.get("Health", {}).get("Status") != "healthy":
            problems.append(f"{name}: not healthy")

    assert not problems, "; ".join(problems)


def test_memory_limits_are_set_and_sum_under_four_gb(
    containers: dict[str, dict[str, Any]],
) -> None:
    unlimited = [n for n, c in containers.items() if int(c["HostConfig"]["Memory"]) <= 0]
    assert not unlimited, f"no memory limit on: {', '.join(sorted(unlimited))}"

    long_running = {n: int(containers[n]["HostConfig"]["Memory"]) for n in LONG_RUNNING}
    total = sum(long_running.values())
    assert total <= MEMORY_BUDGET_BYTES, f"core limits sum to {total} bytes: {long_running}"


def test_only_loopback_ports_are_published(containers: dict[str, dict[str, Any]]) -> None:
    exposed: list[str] = []
    for name, container in containers.items():
        bindings: dict[str, list[dict[str, str]] | None] = (
            container["HostConfig"].get("PortBindings") or {}
        )
        live: dict[str, list[dict[str, str]] | None] = (
            container["NetworkSettings"].get("Ports") or {}
        )
        for source in (bindings, live):
            for port, hosts in source.items():
                for host in hosts or []:
                    if host.get("HostIp") != "127.0.0.1":
                        exposed.append(f"{name} {port} on {host.get('HostIp') or 'all interfaces'}")

    assert not exposed, "; ".join(sorted(set(exposed)))
    # Published ports that must exist: api, web and db (the worker has none).
    assert containers["api"]["HostConfig"]["PortBindings"]
    assert containers["web"]["HostConfig"]["PortBindings"]
    assert not containers["worker"]["HostConfig"].get("PortBindings")
