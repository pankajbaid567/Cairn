"""Core profile RAM report (``make ram-report``; design §17, Requirement 20).

The script starts the core profile (``docker compose --profile core up --wait``), warms it
up for 60 s (20 calls to ``/readyz`` and at least one worker heartbeat), then samples
``docker stats --no-stream`` for the project's db, api, worker and web containers every 5 s
for 60 s. It writes ``docs/evidence/phase-0/core-ram-<date>.md`` and ``.json``.

What is measured, per container and per sample:

- ``usage``: the figure ``docker stats`` shows, which is the cgroup usage minus the inactive
  file cache. The total peak, the comparison with the estimate and the exit status use it.
- ``anon``, ``file`` and ``shmem`` from the container's cgroup ``memory.stat`` (read with
  ``docker exec``). ``docker stats`` alone over-reports file cache that the kernel can drop
  (task 10.1 saw a 256m collector at 252 MiB of which 47 MiB was anonymous), so the report
  also gives ``anon + shmem``, the memory that cannot be dropped. Postgres shared buffers
  are ``shmem``, which cgroup v2 counts inside ``file``, so ``shmem`` is added back to ``anon``.

The pure functions (parsing, summarizing, rendering) hold the logic and are unit-tested in
``tests/scripts/test_measure_ram.py``. Only ``main`` and the ``_docker_*`` helpers shell out.

Units: 1 GB in the thresholds is 1 GiB (1024**3 bytes), the unit Docker prints. The ceiling is
6 GiB and the estimate about 4 GiB.

Exit codes: 0 the total peak is within the ceiling, 1 the total peak exceeds 6 GiB,
2 Docker or the stack is not usable. Standard library only. Output names containers and
numbers; no setting value is printed.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Protocol

REPO_ROOT = Path(__file__).resolve().parent.parent
EVIDENCE_DIR = REPO_ROOT / "docs" / "evidence" / "phase-0"

PROJECT = "cairn"
CORE_SERVICES = ("db", "api", "worker", "web")

KIB = 1024
MIB = KIB**2
GIB = KIB**3
CEILING_BYTES = 6 * GIB  # Requirement 20.4
ESTIMATE_BYTES = 4 * GIB  # Requirement 20.3

WARMUP_S = 60
DURATION_S = 60
INTERVAL_S = 5
READYZ_CALLS = 20
READYZ_URL = "http://127.0.0.1:8000/readyz"

EXIT_OK = 0
EXIT_OVER_CEILING = 1
EXIT_UNUSABLE = 2

CGROUP_DIR = "/sys/fs/cgroup"

_UNITS: dict[str, int] = {
    "b": 1,
    "kb": 1000,
    "mb": 1000**2,
    "gb": 1000**3,
    "tb": 1000**4,
    "kib": KIB,
    "mib": MIB,
    "gib": GIB,
    "tib": KIB**4,
}
_SIZE = re.compile(r"^\s*(?P<number>[0-9]+(?:\.[0-9]+)?)\s*(?P<unit>[A-Za-z]*)\s*$")


class RamReportError(Exception):
    """Docker or the stack cannot be measured (exit code 2)."""


# --- Data ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Reading:
    """One container at one sample; the cgroup fields are None if ``memory.stat`` was unreadable."""

    usage: int
    limit: int
    anon: int | None = None
    file: int | None = None
    shmem: int | None = None

    @property
    def unreclaimable(self) -> int | None:
        """``anon + shmem``: what the kernel cannot drop under pressure."""
        if self.anon is None or self.shmem is None:
            return None
        return self.anon + self.shmem


@dataclass(frozen=True)
class Sample:
    """All core containers at one moment, ``elapsed_s`` after sampling began."""

    elapsed_s: float
    readings: Mapping[str, Reading]


@dataclass(frozen=True)
class ContainerSummary:
    name: str
    limit: int
    samples: int
    mean: float
    peak: int
    anon_mean: float | None
    file_mean: float | None
    shmem_mean: float | None
    unreclaimable_mean: float | None
    unreclaimable_peak: int | None
    cgroup_peak: int | None = None  # memory.peak since container start, filled by main


@dataclass(frozen=True)
class Summary:
    containers: list[ContainerSummary]
    samples: int
    total_mean: float
    total_peak: int
    unreclaimable_total_peak: int | None
    ceiling: int
    estimate: int

    @property
    def exceeds_ceiling(self) -> bool:
        """Requirement 20.4: strictly more than the ceiling."""
        return self.total_peak > self.ceiling

    @property
    def peak_vs_estimate(self) -> float:
        """Total peak divided by the estimate (1.0 means equal)."""
        return self.total_peak / self.estimate


@dataclass(frozen=True)
class ImageInfo:
    container: str
    reference: str
    image_id: str
    repo_digests: tuple[str, ...] = ()


@dataclass
class Environment:
    """Facts about the machine and the run, for the report."""

    host_memory: int | None = None
    docker_memory: int | None = None
    docker_version: str | None = None
    images: list[ImageInfo] = field(default_factory=list)
    other_containers: list[str] = field(default_factory=list)
    readyz_ok: int = 0
    readyz_calls: int = 0
    heartbeat_lines: int | None = None
    warmup_s: int = WARMUP_S
    duration_s: int = DURATION_S
    interval_s: int = INTERVAL_S
    started_at: str = ""


# --- Parsing (pure) -----------------------------------------------------------------------


def parse_size(text: str) -> int:
    """Parse a size as ``docker stats`` prints it (``114.6MiB``, ``1GiB``, ``0B``, ``2.5kB``)."""
    match = _SIZE.match(text)
    if match is None:
        raise ValueError(f"not a size: {text!r}")
    unit = match["unit"].lower() or "b"
    if unit not in _UNITS:
        raise ValueError(f"unknown size unit {match['unit']!r} in {text!r}")
    return round(float(match["number"]) * _UNITS[unit])


def parse_stats_output(text: str) -> dict[str, tuple[int, int]]:
    """Parse ``docker stats --no-stream --format '{{json .}}'`` into ``{name: (usage, limit)}``.

    Blank lines are skipped. A line that is not JSON, or lacks ``Name`` or ``MemUsage``, raises
    ``ValueError``, so a changed docker format fails loudly instead of yielding zeros.
    """
    result: dict[str, tuple[int, int]] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
            name = record["Name"]
            usage_text, limit_text = str(record["MemUsage"]).split("/")
        except (json.JSONDecodeError, KeyError, ValueError, TypeError) as exc:
            raise ValueError(f"unreadable docker stats line: {line[:120]!r}") from exc
        result[str(name)] = (parse_size(usage_text), parse_size(limit_text))
    return result


def parse_memory_stat(text: str) -> dict[str, int]:
    """Parse a cgroup v2 ``memory.stat`` file (``key value`` per line)."""
    stat: dict[str, int] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].isdigit():
            stat[parts[0]] = int(parts[1])
    return stat


def parse_compose_ps(text: str) -> dict[str, str]:
    """Parse ``<service>\\t<name>`` lines (``docker ps --format``) into ``{service: name}``."""
    result: dict[str, str] = {}
    for line in text.splitlines():
        service, _, name = line.partition("\t")
        if service.strip() and name.strip():
            result[service.strip()] = name.strip()
    return result


def parse_images(container_json: str, image_json: str) -> list[ImageInfo]:
    """Image reference, id and registry digests for each container.

    ``container_json`` is ``docker inspect <containers>`` and ``image_json`` is
    ``docker image inspect <image ids>``. Locally built images have no registry digest.
    """
    digests: dict[str, tuple[str, ...]] = {
        image["Id"]: tuple(image.get("RepoDigests") or ()) for image in json.loads(image_json)
    }
    infos = [
        ImageInfo(
            container=str(container["Name"]).lstrip("/"),
            reference=str(container["Config"]["Image"]),
            image_id=str(container["Image"]),
            repo_digests=digests.get(str(container["Image"]), ()),
        )
        for container in json.loads(container_json)
    ]
    return sorted(infos, key=lambda info: info.container)


def reading_from(usage: int, limit: int, memory_stat: Mapping[str, int] | None) -> Reading:
    """Combine a ``docker stats`` pair with the cgroup ``memory.stat`` of the same container."""
    if memory_stat is None:
        return Reading(usage=usage, limit=limit)
    return Reading(
        usage=usage,
        limit=limit,
        anon=memory_stat.get("anon"),
        file=memory_stat.get("file"),
        shmem=memory_stat.get("shmem"),
    )


# --- Summary (pure) -----------------------------------------------------------------------


def _mean_or_none(values: Sequence[int]) -> float | None:
    return statistics.fmean(values) if values else None


def summarize(
    samples: Sequence[Sample],
    *,
    ceiling: int = CEILING_BYTES,
    estimate: int = ESTIMATE_BYTES,
) -> Summary:
    """Per-container mean and peak, and the total peak.

    The total of a sample is the sum of its containers' ``usage``; the total peak is the highest
    such sum. It is not the sum of the per-container peaks, which would overstate it whenever
    the peaks happen at different times.
    """
    if not samples:
        raise ValueError("no samples to summarize")
    names = sorted({name for sample in samples for name in sample.readings})
    containers: list[ContainerSummary] = []
    for name in names:
        readings = [s.readings[name] for s in samples if name in s.readings]
        anon = [r.anon for r in readings if r.anon is not None]
        file = [r.file for r in readings if r.file is not None]
        shmem = [r.shmem for r in readings if r.shmem is not None]
        unreclaimable = [r.unreclaimable for r in readings if r.unreclaimable is not None]
        containers.append(
            ContainerSummary(
                name=name,
                limit=max(r.limit for r in readings),
                samples=len(readings),
                mean=statistics.fmean(r.usage for r in readings),
                peak=max(r.usage for r in readings),
                anon_mean=_mean_or_none(anon),
                file_mean=_mean_or_none(file),
                shmem_mean=_mean_or_none(shmem),
                unreclaimable_mean=_mean_or_none(unreclaimable),
                unreclaimable_peak=max(unreclaimable) if unreclaimable else None,
            )
        )
    totals = [sum(r.usage for r in s.readings.values()) for s in samples]
    unreclaimable_totals = [
        sum(u for r in s.readings.values() if (u := r.unreclaimable) is not None) for s in samples
    ]
    have_cgroup = any(r.unreclaimable is not None for s in samples for r in s.readings.values())
    return Summary(
        containers=containers,
        samples=len(samples),
        total_mean=statistics.fmean(totals),
        total_peak=max(totals),
        unreclaimable_total_peak=max(unreclaimable_totals) if have_cgroup else None,
        ceiling=ceiling,
        estimate=estimate,
    )


# --- Rendering (pure) ---------------------------------------------------------------------


def fmt_mib(value: float | int | None) -> str:
    return "n/a" if value is None else f"{value / MIB:,.1f}"


def fmt_gib(value: float | int | None) -> str:
    return "n/a" if value is None else f"{value / GIB:,.2f}"


def verdict(summary: Summary) -> str:
    peak = fmt_gib(summary.total_peak)
    ceiling = fmt_gib(summary.ceiling)
    if summary.exceeds_ceiling:
        return f"FAIL: total peak {peak} GiB exceeds the {ceiling} GiB ceiling"
    return f"PASS: total peak {peak} GiB is within the {ceiling} GiB ceiling"


def estimate_comparison(summary: Summary) -> str:
    peak = fmt_gib(summary.total_peak)
    estimate = fmt_gib(summary.estimate)
    ratio = summary.peak_vs_estimate
    if ratio <= 1:
        return (
            f"The total peak of {peak} GiB is {ratio:.0%} of the about {estimate} GiB estimate, "
            f"so the estimate is not exceeded."
        )
    return (
        f"The total peak of {peak} GiB is {ratio:.0%} of the about {estimate} GiB estimate, "
        f"so the estimate is exceeded."
    )


def render_json(summary: Summary, env: Environment) -> str:
    """The machine-readable report (stable key order, bytes as integers)."""
    document = {
        "schema": 1,
        "generated_at": env.started_at,
        "result": "fail" if summary.exceeds_ceiling else "pass",
        "verdict": verdict(summary),
        "parameters": {
            "project": PROJECT,
            "warmup_s": env.warmup_s,
            "duration_s": env.duration_s,
            "interval_s": env.interval_s,
            "samples": summary.samples,
            "readyz_calls": env.readyz_calls,
            "readyz_ok": env.readyz_ok,
            "worker_heartbeat_log_lines_in_warmup": env.heartbeat_lines,
        },
        "host_memory_bytes": env.host_memory,
        "docker_vm_memory_bytes": env.docker_memory,
        "docker_server_version": env.docker_version,
        "totals": {
            "mean_bytes": round(summary.total_mean),
            "peak_bytes": summary.total_peak,
            "anon_plus_shmem_peak_bytes": summary.unreclaimable_total_peak,
            "sum_of_limits_bytes": sum(c.limit for c in summary.containers),
        },
        "comparison": {
            "estimate_bytes": summary.estimate,
            "ceiling_bytes": summary.ceiling,
            "peak_over_estimate": round(summary.peak_vs_estimate, 4),
            "exceeds_estimate": summary.total_peak > summary.estimate,
            "exceeds_ceiling": summary.exceeds_ceiling,
        },
        "containers": [
            {
                "name": c.name,
                "limit_bytes": c.limit,
                "samples": c.samples,
                "mean_bytes": round(c.mean),
                "peak_bytes": c.peak,
                "anon_mean_bytes": None if c.anon_mean is None else round(c.anon_mean),
                "file_mean_bytes": None if c.file_mean is None else round(c.file_mean),
                "shmem_mean_bytes": None if c.shmem_mean is None else round(c.shmem_mean),
                "anon_plus_shmem_mean_bytes": (
                    None if c.unreclaimable_mean is None else round(c.unreclaimable_mean)
                ),
                "anon_plus_shmem_peak_bytes": c.unreclaimable_peak,
                "cgroup_peak_since_start_bytes": c.cgroup_peak,
            }
            for c in summary.containers
        ],
        "images": [
            {
                "container": i.container,
                "reference": i.reference,
                "image_id": i.image_id,
                "repo_digests": list(i.repo_digests),
            }
            for i in env.images
        ],
        "other_running_containers_not_counted": env.other_containers,
    }
    return json.dumps(document, indent=2) + "\n"


def render_markdown(summary: Summary, env: Environment, *, day: str) -> str:
    """The human-readable report."""
    lines: list[str] = []
    add = lines.append
    add(f"# Core profile RAM report, {day}")
    add("")
    add(f"**{verdict(summary)}.**")
    add("")
    add(estimate_comparison(summary))
    add("")
    add("## Host and Docker memory")
    add("")
    add("| | Bytes | GiB |")
    add("|---|---:|---:|")
    add(f"| Host (`sysctl hw.memsize`) | {env.host_memory or 'n/a'} | {fmt_gib(env.host_memory)} |")
    add(
        f"| Docker VM (`docker info` MemTotal) | {env.docker_memory or 'n/a'} "
        f"| {fmt_gib(env.docker_memory)} |"
    )
    add(
        f"| Sum of the core containers' limits | {sum(c.limit for c in summary.containers)} "
        f"| {fmt_gib(sum(c.limit for c in summary.containers))} |"
    )
    peak = summary.total_peak
    add(f"| Core total peak (`docker stats`) | {peak} | {fmt_gib(peak)} |")
    add(f"| Estimate (Requirement 20.3) | {summary.estimate} | {fmt_gib(summary.estimate)} |")
    add(f"| Ceiling (Requirement 20.4) | {summary.ceiling} | {fmt_gib(summary.ceiling)} |")
    add("")
    if env.host_memory and env.docker_memory:
        share = env.docker_memory / env.host_memory
        add(
            f"Docker Desktop gets {share:.0%} of the host's memory. The core profile's peak is "
            f"{summary.total_peak / env.docker_memory:.0%} of the Docker VM."
        )
        add("")
    if env.docker_memory is not None and env.docker_memory < summary.ceiling:
        add(
            f"The Docker VM ({fmt_gib(env.docker_memory)} GiB) is smaller than the "
            f"{fmt_gib(summary.ceiling)} GiB ceiling, so on this machine the ceiling cannot be "
            "reached unless Docker Desktop's memory allocation is raised; the VM would start "
            "killing containers first."
        )
        add("")
    add("## Per container (MiB)")
    add("")
    add(
        f"{summary.samples} samples, {env.interval_s} s apart, after a {env.warmup_s} s warm-up. "
        "`Mean` and `Peak` are the `docker stats` figure. `anon`, `file` and `shmem` are the means "
        "from the cgroup `memory.stat`; `anon + shmem` is the memory the kernel cannot drop. "
        "`cgroup peak` is `memory.peak` since the container started, so it can include start-up."
    )
    add("")
    add(
        "| Container | Limit | Mean | Peak | anon | file | shmem | anon + shmem (mean) "
        "| anon + shmem (peak) | cgroup peak |"
    )
    add("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for c in summary.containers:
        add(
            f"| {c.name} | {fmt_mib(c.limit)} | {fmt_mib(c.mean)} | {fmt_mib(c.peak)} "
            f"| {fmt_mib(c.anon_mean)} | {fmt_mib(c.file_mean)} | {fmt_mib(c.shmem_mean)} "
            f"| {fmt_mib(c.unreclaimable_mean)} | {fmt_mib(c.unreclaimable_peak)} "
            f"| {fmt_mib(c.cgroup_peak)} |"
        )
    add(
        f"| **Total** | **{fmt_mib(sum(c.limit for c in summary.containers))}** "
        f"| **{fmt_mib(summary.total_mean)}** | **{fmt_mib(summary.total_peak)}** "
        f"| | | | | **{fmt_mib(summary.unreclaimable_total_peak)}** | |"
    )
    add("")
    add(
        "The total peak is the highest sum over one sample, not the sum of the per-container "
        "peaks. In the `Total` row, the `anon + shmem (peak)` cell is likewise the highest "
        "`anon + shmem` sum over one sample."
    )
    add("")
    add("## Images")
    add("")
    add("| Container | Image | Image id | Repo digest |")
    add("|---|---|---|---|")
    for image in env.images:
        digest = ", ".join(f"`{d}`" for d in image.repo_digests) or "none"
        add(f"| {image.container} | `{image.reference}` | `{image.image_id}` | {digest} |")
    add("")
    add("## Run")
    add("")
    add(f"- Started: {env.started_at}")
    add(f"- `/readyz` calls during warm-up: {env.readyz_ok} of {env.readyz_calls} returned 200")
    if env.heartbeat_lines is not None:
        add(f"- Worker log lines mentioning a heartbeat during warm-up: {env.heartbeat_lines}")
    if env.docker_version:
        add(f"- Docker server: {env.docker_version}")
    if env.other_containers:
        add(
            "- Other running containers that share the Docker VM and are not counted: "
            + ", ".join(env.other_containers)
        )
    add("")
    add("## Notes")
    add("")
    add(
        "- Container cgroup memory excludes the Docker Desktop VM's own overhead (the Linux "
        "kernel, the Docker daemon and the file-sharing layer), so the VM needs more than the "
        "total shown here."
    )
    add(
        "- `docker stats` subtracts only the inactive file cache. Active file cache and memory-"
        "mapped binaries still count towards it, which is why the `anon + shmem` columns are "
        "given next to it."
    )
    add("- One GB in the estimate and the ceiling is one GiB (1024^3 bytes), as Docker prints it.")
    add("")
    return "\n".join(lines)


# --- Docker access (shells out) -----------------------------------------------------------


class Runner(Protocol):
    """Runs a command, returns stdout, raises ``RamReportError`` on failure."""

    def __call__(self, args: Sequence[str], *, timeout: float = ...) -> str: ...


def _run(args: Sequence[str], *, cwd: Path = REPO_ROOT, timeout: float = 120) -> str:
    """Run a command and return stdout; raise ``RamReportError`` naming the command on failure."""
    try:
        done = subprocess.run(
            list(args), capture_output=True, text=True, check=False, cwd=cwd, timeout=timeout
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RamReportError(f"{' '.join(args[:3])}: {exc}") from exc
    if done.returncode != 0:
        detail = done.stderr.strip().splitlines()[-1:] or [""]
        raise RamReportError(f"{' '.join(args[:3])} exited {done.returncode}: {detail[0]}")
    return done.stdout


def find_core_containers(run: Runner) -> dict[str, str]:
    """Running containers of the compose project for the core services, ``{service: name}``."""
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
    running = parse_compose_ps(out)
    missing = [s for s in CORE_SERVICES if s not in running]
    if missing:
        raise RamReportError(f"core service not running: {', '.join(missing)} (run `make up`)")
    return {service: running[service] for service in CORE_SERVICES}


def collect_sample(run: Runner, names: Sequence[str], elapsed_s: float) -> Sample:
    """One sample: ``docker stats`` for all containers, then ``memory.stat`` of each."""
    stats = parse_stats_output(
        run(["docker", "stats", "--no-stream", "--format", "{{json .}}", *names])
    )
    readings: dict[str, Reading] = {}
    for name in names:
        if name not in stats:
            raise RamReportError(f"docker stats returned nothing for {name}")
        usage, limit = stats[name]
        try:
            memory_stat: dict[str, int] | None = parse_memory_stat(
                run(["docker", "exec", name, "cat", f"{CGROUP_DIR}/memory.stat"])
            )
        except RamReportError:
            memory_stat = None
        readings[name] = reading_from(usage, limit, memory_stat or None)
    return Sample(elapsed_s=elapsed_s, readings=readings)


def read_cgroup_peak(run: Runner, name: str) -> int | None:
    try:
        return int(run(["docker", "exec", name, "cat", f"{CGROUP_DIR}/memory.peak"]).strip())
    except (RamReportError, ValueError):
        return None


def collect_images(run: Runner, names: Sequence[str]) -> list[ImageInfo]:
    container_json = run(["docker", "inspect", *names])
    ids = sorted({str(c["Image"]) for c in json.loads(container_json)})
    return parse_images(container_json, run(["docker", "image", "inspect", *ids]))


def other_running_containers(run: Runner, counted: Sequence[str]) -> list[str]:
    out = run(["docker", "ps", "--format", "{{.Names}}"])
    return sorted(n for n in out.split() if n not in counted)


def warm_up(run: Runner, names: Sequence[str], env: Environment, *, url: str) -> None:
    """Call ``/readyz`` ``READYZ_CALLS`` times over the first third of the warm-up, then wait."""
    if not url.startswith("http://127.0.0.1:"):
        raise RamReportError(f"refusing a readyz url that is not on 127.0.0.1: {url}")
    start = time.monotonic()
    pause = env.warmup_s / 3 / READYZ_CALLS
    for _ in range(READYZ_CALLS):
        env.readyz_calls += 1
        try:
            with urllib.request.urlopen(url, timeout=10) as response:  # noqa: S310
                env.readyz_ok += int(response.status == 200)
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(pause)
    remaining = env.warmup_s - (time.monotonic() - start)
    if remaining > 0:
        time.sleep(remaining)
    worker = next((n for n in names if n.endswith("-worker-1")), None)
    if worker is not None:
        try:
            logs = run(["docker", "logs", "--since", f"{env.warmup_s}s", worker])
        except RamReportError:
            return
        env.heartbeat_lines = sum("heartbeat" in line.lower() for line in logs.splitlines())


# --- Command line -------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="measure_ram", description="Measure the core profile's memory (Requirement 20)."
    )
    parser.add_argument("--warmup", type=int, default=WARMUP_S, help="warm-up seconds (60)")
    parser.add_argument("--duration", type=int, default=DURATION_S, help="sampling seconds (60)")
    parser.add_argument(
        "--interval", type=int, default=INTERVAL_S, help="seconds between samples (5)"
    )
    parser.add_argument("--out-dir", type=Path, default=EVIDENCE_DIR, help="report directory")
    parser.add_argument("--date", default=None, help="date in the file names (default: today)")
    parser.add_argument("--build", action="store_true", help="pass --build to `compose up`")
    parser.add_argument(
        "--no-up", action="store_true", help="do not run `compose up`; measure what is running"
    )
    parser.add_argument("--readyz-url", default=READYZ_URL)
    return parser


def _collect_environment(run: Runner, env: Environment) -> None:
    env.docker_memory = int(run(["docker", "info", "--format", "{{.MemTotal}}"]).strip())
    env.docker_version = run(["docker", "info", "--format", "{{.ServerVersion}}"]).strip() or None
    try:
        env.host_memory = int(run(["sysctl", "-n", "hw.memsize"]).strip())
    except (RamReportError, ValueError):
        env.host_memory = None  # not macOS


def measure(args: argparse.Namespace, run: Runner = _run) -> tuple[Summary, Environment]:
    """Bring the stack up, warm it, sample it. Raises ``RamReportError`` when it cannot."""
    if args.interval < 1 or args.duration < args.interval or args.warmup < 0:
        raise RamReportError("need interval >= 1, duration >= interval and warmup >= 0")
    env = Environment(
        warmup_s=args.warmup,
        duration_s=args.duration,
        interval_s=args.interval,
        started_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    _collect_environment(run, env)
    if not args.no_up:
        command = ["docker", "compose", "--profile", "core", "up", "--wait"]
        if args.build:
            command.append("--build")
        print("ram-report: " + " ".join(command), flush=True)
        run(command, timeout=1500)
    containers = find_core_containers(run)
    names = list(containers.values())
    env.images = collect_images(run, names)
    env.other_containers = other_running_containers(run, names)
    print(f"ram-report: warming up for {args.warmup} s", flush=True)
    warm_up(run, names, env, url=args.readyz_url)
    count = args.duration // args.interval
    print(f"ram-report: {count} samples, {args.interval} s apart", flush=True)
    begin = time.monotonic()
    samples: list[Sample] = []
    for index in range(count):
        due = begin + index * args.interval
        time.sleep(max(0.0, due - time.monotonic()))
        samples.append(collect_sample(run, names, time.monotonic() - begin))
    summary = summarize(samples)
    peaks = {name: read_cgroup_peak(run, name) for name in names}
    summary = replace(
        summary,
        containers=[replace(c, cgroup_peak=peaks.get(c.name)) for c in summary.containers],
    )
    return summary, env


def _display(path: Path) -> str:
    return str(path.relative_to(REPO_ROOT)) if path.is_relative_to(REPO_ROOT) else str(path)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        summary, env = measure(args)
    except RamReportError as exc:
        print(f"ram-report: {exc}", file=sys.stderr)
        return EXIT_UNUSABLE
    day = args.date or date.today().isoformat()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    md_path = args.out_dir / f"core-ram-{day}.md"
    json_path = args.out_dir / f"core-ram-{day}.json"
    md_path.write_text(render_markdown(summary, env, day=day), encoding="utf-8")
    json_path.write_text(render_json(summary, env), encoding="utf-8")
    print(f"ram-report: {verdict(summary)}")
    print(f"ram-report: wrote {_display(md_path)}")
    print(f"ram-report: wrote {_display(json_path)}")
    return EXIT_OVER_CEILING if summary.exceeds_ceiling else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
