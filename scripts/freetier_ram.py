"""Free-tier RAM check (``make freetier-ram``; free-tier design §10, FT-18, Requirement 12).

The script brings the ``freetier`` profile up (``docker compose --profile freetier up --wait``,
which also restarts an ``ft-app`` that the smoke module stopped), waits until ``ft-app`` is
healthy and then samples its memory with ``docker stats --no-stream``:

1. idle: 60 s, one sample every 2 s;
2. load: 60 s, one sample every 2 s while two clients send requests to ``/readyz`` (with the
   proxy header), ``/healthz`` and an unknown path, pausing 0.2 s after each request.

It then reads ``State.OOMKilled`` and ``RestartCount`` from ``docker inspect`` and writes
``docs/evidence/free-tier/freetier-ram-<date>.md`` and ``.json`` (samples, peaks, request counts,
latency percentiles, verdict). The figure is the one ``docker stats`` shows: the cgroup usage
minus the inactive file cache, the same figure ``scripts/measure_ram.py`` uses for the core
profile. The cgroup's own ``memory.peak`` and ``memory.stat`` are read once at the end for the
report; they do not enter the verdict.

Pass only if the idle peak is at most 300 MiB, the load peak at most 384 MiB, at most 2% of the
load requests failed, ``ft-app`` was not OOM-killed and its restart count is 0 (``verdict``).
A run also fails when ``ft-app`` is no longer running at the end or no load request was sent;
those two checks live in ``main`` because ``verdict`` stays the pure function of the design.

The profile and the core profile never run together (the Docker VM has 3.8 GiB), so the script
refuses to start while any non-freetier container of project ``cairn`` is running.

The proxy token is read from the environment or ``.env`` (``CAIRN_PROXY_TOKEN``), is sent only as
the ``X-Cairn-Proxy-Token`` request header and is never printed or written. Failed requests are
reported by exception class name only.

Exit codes: 0 pass, 1 a budget rule failed (the evidence is still written), 2 Docker or the
profile is not usable (or the token is missing). Standard library only; the ``docker stats``
parsing comes from ``scripts/measure_ram.py``. At 0.1 CPU everything inside ``ft-app`` is slow
(FV13): bring-up takes about 2.5 minutes, a ``/readyz`` call about 1.4 s, and the request
timeout is 60 s.
"""

from __future__ import annotations

import argparse
import contextlib
import http.client
import json
import math
import os
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal, Protocol

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:  # `python scripts/freetier_ram.py` has scripts/ first
    sys.path.insert(0, str(REPO_ROOT))

from scripts.measure_ram import (  # noqa: E402
    MIB,
    RamReportError,
    parse_compose_ps,
    parse_memory_stat,
    parse_stats_output,
)
from scripts.measure_ram import _run as run_command  # noqa: E402

EVIDENCE_DIR = REPO_ROOT / "docs" / "evidence" / "free-tier"
ENV_FILE = REPO_ROOT / ".env"

PROJECT = "cairn"
SERVICE = "ft-app"
FREETIER_PREFIX = "ft-"
HOST = "127.0.0.1"
DEFAULT_APP_PORT = 8010
APP_PORT_KEY = "CAIRN_FREETIER_APP_PORT"
TOKEN_KEY = "CAIRN_PROXY_TOKEN"  # noqa: S105
PROXY_HEADER = "X-Cairn-Proxy-Token"

IDLE_S = 60.0
LOAD_S = 60.0
INTERVAL_S = 2.0
PAUSE_S = 0.2
CLIENTS = 2
REQUEST_TIMEOUT_S = 60.0
HEALTH_TIMEOUT_S = 600.0
HEALTH_POLL_S = 5.0
UP_TIMEOUT_S = 1500.0
CGROUP_DIR = "/sys/fs/cgroup"

EXIT_OK = 0
EXIT_OVER_BUDGET = 1
EXIT_UNUSABLE = 2

# (path, send the proxy header, expected status). `/healthz` is exempt from the token; the
# unknown path gets the header so that it reaches routing and answers 404 there.
LOAD_CYCLE: tuple[tuple[str, bool, int], ...] = (
    ("/readyz", True, 200),
    ("/healthz", False, 200),
    ("/cairn-ram-check-unknown", True, 404),
)


class RamCheckError(Exception):
    """Docker, the profile or the configuration cannot be used (exit code 2)."""


# --- Budget, phases, verdict (pure; design §10) -------------------------------------------


@dataclass(frozen=True)
class RamBudget:
    limit_mib: float = 512.0
    idle_peak_mib: float = 300.0
    load_peak_mib: float = 384.0
    max_error_ratio: float = 0.02


@dataclass(frozen=True)
class Phase:
    name: Literal["idle", "load"]
    samples_mib: tuple[float, ...]
    requests: int = 0
    errors: int = 0
    latencies_ms: tuple[float, ...] = ()


def verdict(
    budget: RamBudget, idle: Phase, load: Phase, oom_killed: bool, restarts: int
) -> tuple[bool, list[str]]:
    """Pass iff every rule holds; the reasons name each rule that does not."""
    reasons: list[str] = []
    for phase, limit in ((idle, budget.idle_peak_mib), (load, budget.load_peak_mib)):
        if not phase.samples_mib:
            reasons.append(f"{phase.name} phase has no samples")
        elif max(phase.samples_mib) > limit:
            reasons.append(
                f"{phase.name} peak {max(phase.samples_mib):.2f} MiB is above the "
                f"{limit:g} MiB budget"
            )
    if load.errors > budget.max_error_ratio * load.requests:
        reasons.append(
            f"{load.errors} of {load.requests} load requests failed, above the "
            f"{budget.max_error_ratio:.0%} budget"
        )
    if oom_killed:
        reasons.append("ft-app was OOM-killed")
    if restarts != 0:
        reasons.append(f"ft-app restart count is {restarts}, expected 0")
    return (not reasons, reasons)


def percentile(values: Sequence[float], p: float) -> float | None:
    """Nearest-rank percentile, ``None`` for no values."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(p / 100 * len(ordered)))
    return ordered[rank - 1]


# --- Run data -----------------------------------------------------------------------------


@dataclass(frozen=True)
class RequestRecord:
    path: str
    status: int | None  # None: no HTTP response (timeout, connection error)
    latency_ms: float
    ok: bool
    error: str | None = None  # exception class name, never a message


@dataclass(frozen=True)
class PhaseRun:
    """What one phase measured: the samples with their times, and (load only) the requests."""

    name: Literal["idle", "load"]
    times_s: tuple[float, ...]
    samples_mib: tuple[float, ...]
    sample_failures: int
    requests: tuple[RequestRecord, ...] = ()

    @property
    def phase(self) -> Phase:
        return Phase(
            name=self.name,
            samples_mib=self.samples_mib,
            requests=len(self.requests),
            errors=sum(not r.ok for r in self.requests),
            latencies_ms=tuple(r.latency_ms for r in self.requests if r.status is not None),
        )


@dataclass(frozen=True)
class ContainerFacts:
    """``docker inspect`` of ft-app: state, restarts, health and what it runs."""

    name: str
    running: bool
    status: str
    exit_code: int
    oom_killed: bool
    restart_count: int
    health: str | None
    memory_limit_bytes: int
    nano_cpus: int
    image_reference: str
    image_id: str


@dataclass(frozen=True)
class Measurement:
    started_at: str
    docker_memory: int
    docker_version: str
    idle: PhaseRun
    load: PhaseRun
    final: ContainerFacts
    cgroup: Mapping[str, int] = field(default_factory=dict)  # peak, anon, file, shmem


@dataclass(frozen=True)
class Settings:
    idle_s: float = IDLE_S
    load_s: float = LOAD_S
    interval_s: float = INTERVAL_S
    pause_s: float = PAUSE_S
    clients: int = CLIENTS
    request_timeout_s: float = REQUEST_TIMEOUT_S
    health_timeout_s: float = HEALTH_TIMEOUT_S
    base_url: str = f"http://{HOST}:{DEFAULT_APP_PORT}"
    up: bool = True
    build: bool = False
    budget: RamBudget = field(default_factory=RamBudget)


class Runner(Protocol):
    def __call__(self, args: Sequence[str], *, timeout: float = ...) -> str: ...


Fetch = Callable[[str, Mapping[str, str], float], int]
"""``fetch(url, headers, timeout_s)`` returns the HTTP status; raises ``OSError`` or
``http.client.HTTPException`` when there is no response."""


# --- Environment file (the token never leaves this module except as a header) -------------


def read_dotenv_value(path: Path, key: str) -> str | None:
    """The value of ``key`` in a ``.env`` file, or None. Handles ``export``, quotes, comments."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    found: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = line.removeprefix("export ").lstrip()
        name, sep, value = line.partition("=")
        if not sep or name.strip() != key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        found = value  # the last assignment wins, like a shell
    return found


def lookup(key: str, env_file: Path) -> str | None:
    """Environment first, then ``.env``; empty counts as unset."""
    return os.environ.get(key) or read_dotenv_value(env_file, key) or None


# --- Docker access ------------------------------------------------------------------------


def parse_container_facts(text: str) -> ContainerFacts:
    """Parse ``docker inspect <container>`` (one container)."""
    try:
        record = json.loads(text)[0]
        state = record["State"]
        return ContainerFacts(
            name=str(record["Name"]).lstrip("/"),
            running=bool(state["Running"]),
            status=str(state["Status"]),
            exit_code=int(state.get("ExitCode", 0)),
            oom_killed=bool(state["OOMKilled"]),
            restart_count=int(record["RestartCount"]),
            health=(state.get("Health") or {}).get("Status"),
            memory_limit_bytes=int(record["HostConfig"].get("Memory", 0)),
            nano_cpus=int(record["HostConfig"].get("NanoCpus", 0)),
            image_reference=str(record["Config"]["Image"]),
            image_id=str(record["Image"]),
        )
    except (json.JSONDecodeError, IndexError, KeyError, TypeError, ValueError) as exc:
        raise RamCheckError("unreadable `docker inspect` output for ft-app") from exc


def project_containers(run: Runner) -> dict[str, str]:
    """Running containers of project ``cairn``, ``{service: name}``."""
    out = run(
        [
            "docker",
            "ps",
            "--filter",
            f"label=com.docker.compose.project={PROJECT}",
            "--format",
            '{{.Label "com.docker.compose.service"}}\t{{.Names}}',
        ]
    )
    return parse_compose_ps(out)


def refuse_core_containers(running: Mapping[str, str]) -> None:
    others = sorted(s for s in running if not s.startswith(FREETIER_PREFIX))
    if others:
        raise RamCheckError(
            f"containers of project {PROJECT} that are not part of the freetier profile are "
            f"running ({', '.join(others)}). Core and freetier never run together on this "
            "host (3.8 GiB Docker VM): run `make down` first"
        )


def inspect_ft_app(run: Runner, name: str) -> ContainerFacts:
    return parse_container_facts(run(["docker", "inspect", name]))


def wait_until_healthy(
    run: Runner,
    name: str,
    *,
    timeout_s: float,
    poll_s: float = HEALTH_POLL_S,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> ContainerFacts:
    deadline = clock() + timeout_s
    while True:
        facts = inspect_ft_app(run, name)
        if not facts.running:
            raise RamCheckError(f"{name} is not running (status {facts.status})")
        if facts.health == "healthy":
            return facts
        if facts.health == "unhealthy":
            raise RamCheckError(f"{name} is unhealthy")
        if clock() >= deadline:
            raise RamCheckError(f"{name} did not become healthy within {timeout_s:.0f} s")
        sleep(poll_s)


def read_memory_mib(run: Runner, name: str) -> float:
    """One ``docker stats --no-stream`` sample: the usage in MiB."""
    stats = parse_stats_output(
        run(["docker", "stats", "--no-stream", "--format", "{{json .}}", name])
    )
    if name not in stats:
        raise RamReportError(f"docker stats returned nothing for {name}")
    return stats[name][0] / MIB


def read_cgroup(run: Runner, name: str) -> dict[str, int]:
    """``memory.peak`` and ``memory.stat`` of the container; empty when unreadable."""
    result: dict[str, int] = {}
    with contextlib.suppress(RamReportError, ValueError):
        result["peak"] = int(run(["docker", "exec", name, "cat", f"{CGROUP_DIR}/memory.peak"]))
    try:
        stat = parse_memory_stat(run(["docker", "exec", name, "cat", f"{CGROUP_DIR}/memory.stat"]))
    except RamReportError:
        stat = {}
    result.update({key: stat[key] for key in ("anon", "file", "shmem") if key in stat})
    return result


# --- Sampler and load ---------------------------------------------------------------------


def sample_phase(
    name: Literal["idle", "load"],
    read: Callable[[], float],
    *,
    duration_s: float,
    interval_s: float,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> PhaseRun:
    """Take ``duration_s / interval_s`` samples, each due ``interval_s`` after the previous one's
    due time. A sample that fails is counted and skipped; the phase goes on."""
    count = max(1, round(duration_s / interval_s))
    begin = clock()
    times: list[float] = []
    values: list[float] = []
    failures = 0
    for index in range(count):
        sleep(max(0.0, begin + index * interval_s - clock()))
        elapsed = clock() - begin
        try:
            values.append(read())
        except (RamReportError, ValueError):
            failures += 1
        else:
            times.append(elapsed)
    return PhaseRun(
        name=name,
        times_s=tuple(times),
        samples_mib=tuple(values),
        sample_failures=failures,
    )


def http_get(url: str, headers: Mapping[str, str], timeout_s: float) -> int:
    """GET ``url`` and return the status (error statuses included). Loopback only."""
    if not url.startswith(f"http://{HOST}:"):
        raise ValueError("the RAM check talks to 127.0.0.1 only")
    request = urllib.request.Request(url, headers=dict(headers))  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:  # noqa: S310
            response.read()
            return int(response.status)
    except urllib.error.HTTPError as exc:
        exc.read()
        return int(exc.code)


def _client(
    fetch: Fetch,
    base_url: str,
    token: str,
    deadline: float,
    start_index: int,
    pause_s: float,
    timeout_s: float,
    out: list[RequestRecord],
    clock: Callable[[], float],
) -> None:
    index = start_index
    while clock() < deadline:
        path, with_token, expected = LOAD_CYCLE[index % len(LOAD_CYCLE)]
        index += 1
        headers = {PROXY_HEADER: token} if with_token else {}
        began = clock()
        status: int | None = None
        error: str | None = None
        try:
            status = fetch(base_url + path, headers, timeout_s)
        except (OSError, http.client.HTTPException, ValueError) as exc:
            error = type(exc).__name__  # the message could carry the URL or headers
        latency_ms = (clock() - began) * 1000
        out.append(
            RequestRecord(
                path=path,
                status=status,
                latency_ms=latency_ms,
                ok=status == expected,
                error=error,
            )
        )
        remaining = deadline - clock()
        if remaining > 0:
            time.sleep(min(pause_s, remaining))


def start_load(
    fetch: Fetch,
    *,
    base_url: str,
    token: str,
    duration_s: float,
    clients: int,
    pause_s: float,
    timeout_s: float,
    clock: Callable[[], float] = time.monotonic,
) -> Callable[[], list[RequestRecord]]:
    """Start ``clients`` client threads and return at once. The returned function waits for the
    threads (each stops at the deadline after its in-flight request) and gives all records."""
    deadline = clock() + duration_s
    buckets: list[list[RequestRecord]] = [[] for _ in range(clients)]
    threads = [
        threading.Thread(
            target=_client,
            args=(fetch, base_url, token, deadline, i, pause_s, timeout_s, buckets[i], clock),
            name=f"freetier-ram-client-{i}",
            daemon=True,
        )
        for i in range(clients)
    ]
    for thread in threads:
        thread.start()

    def collect() -> list[RequestRecord]:
        for thread in threads:
            thread.join(timeout=timeout_s + pause_s + 5)
        return [record for bucket in buckets for record in bucket]

    return collect


# --- Measuring ----------------------------------------------------------------------------


def log(message: str) -> None:
    print(f"freetier-ram: {message}", flush=True)


def measure(
    settings: Settings,
    token: str,
    run: Runner,
    fetch: Fetch,
) -> Measurement:
    """Bring the profile up, sample idle, sample under load, read the container state.

    Raises ``RamCheckError`` when Docker or the profile is not usable.
    """
    started_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        docker_memory = int(run(["docker", "info", "--format", "{{.MemTotal}}"]).strip())
        docker_version = run(["docker", "info", "--format", "{{.ServerVersion}}"]).strip()
    except (RamReportError, ValueError) as exc:
        raise RamCheckError(f"Docker is not usable: {exc}") from exc

    try:
        refuse_core_containers(project_containers(run))
        if settings.up:
            command = ["docker", "compose", "--profile", "freetier", "up", "--wait"]
            if settings.build:
                command.append("--build")
            log(" ".join(command) + " (about 2.5 minutes at 0.1 CPU)")
            run(command, timeout=UP_TIMEOUT_S)
        name = project_containers(run).get(SERVICE)
        if name is None:
            raise RamCheckError(
                f"{SERVICE} is not running; start the profile with `make freetier-up`"
            )
        log(f"waiting for {name} to be healthy")
        wait_until_healthy(run, name, timeout_s=settings.health_timeout_s)

        def read() -> float:
            return read_memory_mib(run, name)

        log(f"idle phase: {settings.idle_s:g} s, a sample every {settings.interval_s:g} s")
        idle = sample_phase(
            "idle", read, duration_s=settings.idle_s, interval_s=settings.interval_s
        )
        log(
            f"load phase: {settings.load_s:g} s, {settings.clients} clients, "
            f"{settings.pause_s:g} s between requests"
        )
        collect = start_load(
            fetch,
            base_url=settings.base_url,
            token=token,
            duration_s=settings.load_s,
            clients=settings.clients,
            pause_s=settings.pause_s,
            timeout_s=settings.request_timeout_s,
        )
        sampled = sample_phase(
            "load", read, duration_s=settings.load_s, interval_s=settings.interval_s
        )
        load = replace(sampled, requests=tuple(collect()))
        final = inspect_ft_app(run, name)
        cgroup = read_cgroup(run, name) if final.running else {}
    except RamReportError as exc:
        raise RamCheckError(str(exc)) from exc
    return Measurement(
        started_at=started_at,
        docker_memory=docker_memory,
        docker_version=docker_version,
        idle=idle,
        load=load,
        final=final,
        cgroup=cgroup,
    )


def evaluate(budget: RamBudget, m: Measurement) -> tuple[bool, list[str]]:
    """``verdict`` plus the two run-level checks that are not part of the pure rule set."""
    ok, reasons = verdict(
        budget, m.idle.phase, m.load.phase, m.final.oom_killed, m.final.restart_count
    )
    reasons = list(reasons)
    if not m.final.running:
        reasons.append(
            f"ft-app is not running at the end (status {m.final.status}, "
            f"exit code {m.final.exit_code})"
        )
    if not m.load.requests:
        reasons.append("no load request was sent")
    return (ok and not reasons, reasons)


# --- Rendering (pure) ---------------------------------------------------------------------


def _round(value: float | None, digits: int = 2) -> float | None:
    return None if value is None else round(value, digits)


def _peak(phase: Phase) -> float | None:
    return max(phase.samples_mib) if phase.samples_mib else None


def _mean(phase: Phase) -> float | None:
    return statistics.fmean(phase.samples_mib) if phase.samples_mib else None


def _latency_stats(latencies: Sequence[float]) -> dict[str, float | None]:
    return {
        "p50": _round(percentile(latencies, 50), 1),
        "p95": _round(percentile(latencies, 95), 1),
        "max": _round(max(latencies), 1) if latencies else None,
    }


@dataclass(frozen=True)
class PathStats:
    path: str
    expected_status: int
    requests: int
    errors: int
    latency_ms: dict[str, float | None]


def _by_path(requests: Sequence[RequestRecord]) -> list[PathStats]:
    stats: list[PathStats] = []
    for path, _, expected in LOAD_CYCLE:
        mine = [r for r in requests if r.path == path]
        stats.append(
            PathStats(
                path=path,
                expected_status=expected,
                requests=len(mine),
                errors=sum(not r.ok for r in mine),
                latency_ms=_latency_stats([r.latency_ms for r in mine if r.status is not None]),
            )
        )
    return stats


def _result_line(ok: bool, reasons: Sequence[str]) -> str:
    return "PASS: every budget rule holds" if ok else "FAIL: " + "; ".join(reasons)


def render_json(
    budget: RamBudget,
    m: Measurement,
    ok: bool,
    reasons: Sequence[str],
    settings: Settings,
    *,
    day: str,
) -> str:
    """The machine-readable report. It holds no token and no setting value."""
    idle, load = m.idle.phase, m.load.phase
    requests = m.load.requests
    status_counts = Counter(str(r.status) for r in requests if r.status is not None)
    error_kinds = Counter(r.error for r in requests if r.error is not None)

    def samples(run: PhaseRun) -> list[dict[str, float]]:
        return [
            {"t_s": round(t, 1), "mib": round(v, 2)}
            for t, v in zip(run.times_s, run.samples_mib, strict=True)
        ]

    document = {
        "schema": 1,
        "date": day,
        "generated_at": m.started_at,
        "result": "pass" if ok else "fail",
        "verdict": _result_line(ok, reasons),
        "reasons": list(reasons),
        "budget": {
            "limit_mib": budget.limit_mib,
            "idle_peak_mib": budget.idle_peak_mib,
            "load_peak_mib": budget.load_peak_mib,
            "max_error_ratio": budget.max_error_ratio,
        },
        "parameters": {
            "idle_s": settings.idle_s,
            "load_s": settings.load_s,
            "interval_s": settings.interval_s,
            "clients": settings.clients,
            "pause_s": settings.pause_s,
            "request_timeout_s": settings.request_timeout_s,
            "figure": "docker stats --no-stream MemUsage (cgroup usage minus inactive file cache)",
        },
        "container": {
            "name": m.final.name,
            "image": m.final.image_reference,
            "image_id": m.final.image_id,
            "memory_limit_bytes": m.final.memory_limit_bytes,
            "nano_cpus": m.final.nano_cpus,
            "state": m.final.status,
            "exit_code": m.final.exit_code,
            "oom_killed": m.final.oom_killed,
            "restart_count": m.final.restart_count,
        },
        "docker": {"vm_memory_bytes": m.docker_memory, "server_version": m.docker_version},
        "idle": {
            "samples": samples(m.idle),
            "sample_failures": m.idle.sample_failures,
            "peak_mib": _round(_peak(idle)),
            "mean_mib": _round(_mean(idle)),
        },
        "load": {
            "samples": samples(m.load),
            "sample_failures": m.load.sample_failures,
            "peak_mib": _round(_peak(load)),
            "mean_mib": _round(_mean(load)),
            "requests": load.requests,
            "errors": load.errors,
            "error_ratio": _round(load.errors / load.requests, 4) if load.requests else None,
            "status_counts": dict(sorted(status_counts.items())),
            "error_kinds": dict(sorted(error_kinds.items())),
            "latency_ms": _latency_stats(load.latencies_ms),
            "by_path": {
                p.path: {
                    "expected_status": p.expected_status,
                    "requests": p.requests,
                    "errors": p.errors,
                    "latency_ms": p.latency_ms,
                }
                for p in _by_path(requests)
            },
        },
        "cgroup_at_end": {
            "memory_peak_since_start_bytes": m.cgroup.get("peak"),
            "anon_bytes": m.cgroup.get("anon"),
            "file_bytes": m.cgroup.get("file"),
            "shmem_bytes": m.cgroup.get("shmem"),
        },
    }
    return json.dumps(document, indent=2) + "\n"


def _fmt(value: float | None, digits: int = 1) -> str:
    return "n/a" if value is None else f"{value:,.{digits}f}"


def _mib(value: float | None) -> str:
    return "n/a" if value is None else f"{value / MIB:,.1f}"


def render_markdown(
    budget: RamBudget,
    m: Measurement,
    ok: bool,
    reasons: Sequence[str],
    settings: Settings,
    *,
    day: str,
) -> str:
    """The human-readable report. It holds no token and no setting value."""
    idle, load = m.idle.phase, m.load.phase
    requests = m.load.requests
    lines: list[str] = []
    add = lines.append
    add(f"# Free-tier RAM check, {day}")
    add("")
    add(f"**{_result_line(ok, reasons)}.**")
    add("")
    add(
        f"`ft-app` (the combined process, limit {budget.limit_mib:g} MiB, 0.1 CPU) measured with "
        "`docker stats --no-stream` (cgroup usage minus the inactive file cache): an idle phase of "
        f"{settings.idle_s:g} s and a load phase of {settings.load_s:g} s, a sample every "
        f"{settings.interval_s:g} s. Budgets are FT-18 (Proposed target)."
    )
    add("")
    add("## Rules")
    add("")
    add("| Rule | Budget | Measured | Result |")
    add("|---|---:|---:|---|")
    idle_peak, load_peak = _peak(idle), _peak(load)
    add(
        f"| Idle peak (MiB) | {budget.idle_peak_mib:g} | {_fmt(idle_peak, 2)} "
        f"| {'ok' if idle_peak is not None and idle_peak <= budget.idle_peak_mib else 'FAIL'} |"
    )
    add(
        f"| Load peak (MiB) | {budget.load_peak_mib:g} | {_fmt(load_peak, 2)} "
        f"| {'ok' if load_peak is not None and load_peak <= budget.load_peak_mib else 'FAIL'} |"
    )
    ratio = load.errors / load.requests if load.requests else None
    add(
        f"| Failed load requests | at most {budget.max_error_ratio:.0%} "
        f"| {load.errors} of {load.requests} "
        f"({'n/a' if ratio is None else f'{ratio:.1%}'}) "
        f"| {'ok' if load.errors <= budget.max_error_ratio * load.requests else 'FAIL'} |"
    )
    add(
        f"| OOM-killed | no | {'yes' if m.final.oom_killed else 'no'} "
        f"| {'FAIL' if m.final.oom_killed else 'ok'} |"
    )
    add(
        f"| Restart count | 0 | {m.final.restart_count} "
        f"| {'ok' if m.final.restart_count == 0 else 'FAIL'} |"
    )
    add("")
    if reasons:
        add("Failed rules:")
        add("")
        for reason in reasons:
            add(f"- {reason}")
        add("")
    add("## Memory (MiB)")
    add("")
    add("| Phase | Samples | Failed samples | Mean | Peak |")
    add("|---|---:|---:|---:|---:|")
    for run, phase in ((m.idle, idle), (m.load, load)):
        add(
            f"| {run.name} | {len(phase.samples_mib)} | {run.sample_failures} "
            f"| {_fmt(_mean(phase), 2)} | {_fmt(_peak(phase), 2)} |"
        )
    add("")
    add(
        f"Headroom to the {budget.limit_mib:g} MiB limit at the load peak: "
        f"{_fmt(None if load_peak is None else budget.limit_mib - load_peak, 1)} MiB."
    )
    add("")
    add("## Load")
    add("")
    add(
        f"{settings.clients} clients, each pausing {settings.pause_s:g} s after a request, "
        f"request timeout {settings.request_timeout_s:g} s. Latencies are in milliseconds and "
        "cover requests that got an HTTP response; they are for information, not a rule."
    )
    add("")
    add("| Path | Expected | Requests | Errors | p50 | p95 | Max |")
    add("|---|---:|---:|---:|---:|---:|---:|")
    for p in _by_path(requests):
        add(
            f"| `{p.path}` | {p.expected_status} | {p.requests} | {p.errors} "
            f"| {_fmt(p.latency_ms['p50'])} | {_fmt(p.latency_ms['p95'])} "
            f"| {_fmt(p.latency_ms['max'])} |"
        )
    overall = _latency_stats(load.latencies_ms)
    add(
        f"| **All** | | **{load.requests}** | **{load.errors}** | {_fmt(overall['p50'])} "
        f"| {_fmt(overall['p95'])} | {_fmt(overall['max'])} |"
    )
    add("")
    status_counts = Counter(str(r.status) for r in requests if r.status is not None)
    error_kinds = Counter(r.error for r in requests if r.error is not None)
    add(
        "Status codes: "
        + (", ".join(f"{k} x {v}" for k, v in sorted(status_counts.items())) or "none")
        + ". Requests without a response: "
        + (", ".join(f"{k} x {v}" for k, v in sorted(error_kinds.items())) or "none")
        + "."
    )
    add("")
    add("## Container")
    add("")
    add(
        f"- Name: {m.final.name}; state at the end: {m.final.status} "
        f"(exit code {m.final.exit_code})"
    )
    add(f"- Image: `{m.final.image_reference}` (`{m.final.image_id}`)")
    add(f"- Limits: memory {_mib(m.final.memory_limit_bytes)} MiB, CPU {m.final.nano_cpus / 1e9:g}")
    add(
        f"- cgroup at the end (read once, not in the verdict): `memory.peak` since start "
        f"{_mib(m.cgroup.get('peak'))} MiB (includes start-up), "
        f"anon {_mib(m.cgroup.get('anon'))} MiB, "
        f"file {_mib(m.cgroup.get('file'))} MiB, shmem {_mib(m.cgroup.get('shmem'))} MiB"
    )
    add(f"- Docker VM memory: {_mib(m.docker_memory)} MiB; server {m.docker_version or 'n/a'}")
    add(f"- Started: {m.started_at}")
    add("")
    for run in (m.idle, m.load):
        add(f"## Samples: {run.name} (MiB)")
        add("")
        add("| t (s) | MiB |")
        add("|---:|---:|")
        for t, v in zip(run.times_s, run.samples_mib, strict=True):
            add(f"| {t:.1f} | {v:.2f} |")
        add("")
    return "\n".join(lines)


# --- Command line -------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="freetier_ram", description="Measure ft-app memory against FT-18 (Requirement 12)."
    )
    parser.add_argument("--idle-s", type=float, default=IDLE_S, help="idle seconds (60)")
    parser.add_argument("--load-s", type=float, default=LOAD_S, help="load seconds (60)")
    parser.add_argument("--interval-s", type=float, default=INTERVAL_S, help="sample spacing (2)")
    parser.add_argument("--pause-s", type=float, default=PAUSE_S, help="pause per client (0.2)")
    parser.add_argument("--clients", type=int, default=CLIENTS, help="load clients (2)")
    parser.add_argument("--request-timeout-s", type=float, default=REQUEST_TIMEOUT_S)
    parser.add_argument("--health-timeout-s", type=float, default=HEALTH_TIMEOUT_S)
    parser.add_argument("--out-dir", type=Path, default=EVIDENCE_DIR, help="report directory")
    parser.add_argument("--env-file", type=Path, default=ENV_FILE, help=".env path")
    parser.add_argument("--date", default=None, help="date in the file names (default: today)")
    parser.add_argument("--build", action="store_true", help="pass --build to `compose up`")
    parser.add_argument(
        "--no-up", action="store_true", help="do not run `compose up`; measure what is running"
    )
    return parser


def _display(path: Path) -> str:
    return str(path.relative_to(REPO_ROOT)) if path.is_relative_to(REPO_ROOT) else str(path)


def _settings(args: argparse.Namespace, port: int) -> Settings:
    if args.interval_s <= 0 or args.idle_s < 0 or args.load_s < 0 or args.clients < 1:
        raise RamCheckError("need interval > 0, idle >= 0, load >= 0 and at least one client")
    return Settings(
        idle_s=args.idle_s,
        load_s=args.load_s,
        interval_s=args.interval_s,
        pause_s=args.pause_s,
        clients=args.clients,
        request_timeout_s=args.request_timeout_s,
        health_timeout_s=args.health_timeout_s,
        base_url=f"http://{HOST}:{port}",
        up=not args.no_up,
        build=args.build,
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    run: Runner = run_command,
    fetch: Fetch = http_get,
) -> int:
    args = build_parser().parse_args(argv)
    budget = RamBudget()
    try:
        token = lookup(TOKEN_KEY, args.env_file)
        if token is None:
            raise RamCheckError(
                f"{TOKEN_KEY} is not set in {args.env_file.name}; run `make env-update`"
            )
        raw_port = lookup(APP_PORT_KEY, args.env_file)
        try:
            port = int(raw_port) if raw_port else DEFAULT_APP_PORT
        except ValueError as exc:
            raise RamCheckError(f"{APP_PORT_KEY} is not a number") from exc
        settings = _settings(args, port)
        measurement = measure(settings, token, run, fetch)
    except RamCheckError as exc:
        print(f"freetier-ram: {exc}", file=sys.stderr)
        return EXIT_UNUSABLE

    ok, reasons = evaluate(budget, measurement)
    day = args.date or date.today().isoformat()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    md_path = args.out_dir / f"freetier-ram-{day}.md"
    json_path = args.out_dir / f"freetier-ram-{day}.json"
    md_path.write_text(
        render_markdown(budget, measurement, ok, reasons, settings, day=day), encoding="utf-8"
    )
    json_path.write_text(
        render_json(budget, measurement, ok, reasons, settings, day=day), encoding="utf-8"
    )
    log(
        f"idle peak {_fmt(_peak(measurement.idle.phase), 2)} MiB "
        f"(budget {budget.idle_peak_mib:g}), "
        f"load peak {_fmt(_peak(measurement.load.phase), 2)} MiB "
        f"(budget {budget.load_peak_mib:g}), "
        f"{measurement.load.phase.errors} of {measurement.load.phase.requests} requests failed"
    )
    log(_result_line(ok, reasons))
    log(f"wrote {_display(md_path)}")
    log(f"wrote {_display(json_path)}")
    return EXIT_OK if ok else EXIT_OVER_BUDGET


if __name__ == "__main__":
    sys.exit(main())
