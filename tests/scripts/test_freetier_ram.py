"""Free-tier RAM check: verdict, sampler, load, writers and exit codes (Requirement 12)."""

import json
import threading
from collections.abc import Mapping, Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from scripts import freetier_ram
from scripts.freetier_ram import (
    EXIT_OK,
    EXIT_OVER_BUDGET,
    EXIT_UNUSABLE,
    LOAD_CYCLE,
    PROXY_HEADER,
    Phase,
    RamBudget,
    RamCheckError,
    http_get,
    main,
    parse_container_facts,
    percentile,
    read_dotenv_value,
    refuse_core_containers,
    sample_phase,
    start_load,
    verdict,
)
from scripts.measure_ram import RamReportError

BUDGET = RamBudget()
TOKEN = "CANARY-proxy-token-0123456789abcdef0123456789"
APP = "cairn-ft-app-1"


def idle_phase(*samples: float) -> Phase:
    return Phase("idle", tuple(samples))


def load_phase(*samples: float, requests: int = 100, errors: int = 0) -> Phase:
    return Phase("load", tuple(samples), requests=requests, errors=errors)


def check(
    idle: Phase | None = None,
    load: Phase | None = None,
    *,
    oom: bool = False,
    restarts: int = 0,
) -> tuple[bool, list[str]]:
    return verdict(
        BUDGET,
        idle or idle_phase(100.0, 120.0),
        load or load_phase(150.0, 200.0),
        oom,
        restarts,
    )


# --- verdict ------------------------------------------------------------------------------


def test_the_defaults_are_the_ft_18_budget() -> None:
    assert (BUDGET.limit_mib, BUDGET.idle_peak_mib, BUDGET.load_peak_mib) == (512.0, 300.0, 384.0)
    assert BUDGET.max_error_ratio == 0.02


def test_a_run_inside_every_budget_passes_with_no_reasons() -> None:
    assert check() == (True, [])


@pytest.mark.parametrize(
    ("peak", "passes"), [(299.99, True), (300.0, True), (300.01, False), (512.0, False)]
)
def test_idle_peak_budget_is_300_mib_inclusive(peak: float, passes: bool) -> None:
    ok, reasons = check(idle=idle_phase(50.0, peak, 60.0))
    assert ok is passes
    assert bool(reasons) is (not passes)
    if not passes:
        assert "idle peak" in reasons[0]
        assert "300" in reasons[0]


@pytest.mark.parametrize(
    ("peak", "passes"), [(383.99, True), (384.0, True), (384.01, False), (500.0, False)]
)
def test_load_peak_budget_is_384_mib_inclusive(peak: float, passes: bool) -> None:
    ok, reasons = check(load=load_phase(100.0, peak))
    assert ok is passes
    if not passes:
        assert "load peak" in reasons[0]
        assert "384" in reasons[0]


@pytest.mark.parametrize(
    ("requests", "errors", "passes"),
    [(100, 0, True), (100, 2, True), (100, 3, False), (50, 1, True), (50, 2, False)],
)
def test_failed_requests_are_at_most_two_percent(requests: int, errors: int, passes: bool) -> None:
    ok, reasons = check(load=load_phase(150.0, requests=requests, errors=errors))
    assert ok is passes
    if not passes:
        assert f"{errors} of {requests}" in reasons[0]
        assert "2%" in reasons[0]


def test_an_oom_kill_fails_even_when_the_numbers_are_low() -> None:
    ok, reasons = check(oom=True)
    assert not ok
    assert reasons == ["ft-app was OOM-killed"]


@pytest.mark.parametrize("restarts", [1, 3])
def test_any_restart_fails(restarts: int) -> None:
    ok, reasons = check(restarts=restarts)
    assert not ok
    assert reasons == [f"ft-app restart count is {restarts}, expected 0"]


def test_empty_idle_samples_fail_and_say_so() -> None:
    ok, reasons = check(idle=idle_phase())
    assert not ok
    assert reasons == ["idle phase has no samples"]


def test_empty_load_samples_fail_and_say_so() -> None:
    ok, reasons = check(load=load_phase())
    assert not ok
    assert reasons == ["load phase has no samples"]


def test_every_failed_rule_gets_its_own_reason() -> None:
    ok, reasons = check(
        idle=idle_phase(301.0),
        load=load_phase(385.0, requests=10, errors=5),
        oom=True,
        restarts=2,
    )
    assert not ok
    assert len(reasons) == 5
    joined = " | ".join(reasons)
    for word in ("idle peak", "load peak", "load requests failed", "OOM", "restart"):
        assert word in joined


# --- small pure helpers -------------------------------------------------------------------


def test_percentile_is_nearest_rank() -> None:
    values = [float(n) for n in range(1, 101)]
    assert percentile(values, 50) == 50.0
    assert percentile(values, 95) == 95.0
    assert percentile([7.0], 95) == 7.0
    assert percentile([], 50) is None


def test_dotenv_reader_handles_quotes_comments_export_and_the_last_assignment(
    tmp_path: Path,
) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "# comment\nA=1\nexport B='two words'\nC=\"quoted\"\nD=plain # trailing\nA=2\nE=\n",
        encoding="utf-8",
    )
    assert read_dotenv_value(env, "A") == "2"
    assert read_dotenv_value(env, "B") == "two words"
    assert read_dotenv_value(env, "C") == "quoted"
    assert read_dotenv_value(env, "D") == "plain"
    assert read_dotenv_value(env, "E") == ""
    assert read_dotenv_value(env, "MISSING") is None
    assert read_dotenv_value(tmp_path / "absent", "A") is None


def inspect_json(
    *,
    running: bool = True,
    status: str = "running",
    health: str | None = "healthy",
    oom: bool = False,
    restarts: int = 0,
    exit_code: int = 0,
) -> str:
    state: dict[str, Any] = {
        "Running": running,
        "Status": status,
        "ExitCode": exit_code,
        "OOMKilled": oom,
    }
    if health is not None:
        state["Health"] = {"Status": health}
    return json.dumps(
        [
            {
                "Name": f"/{APP}",
                "Image": "sha256:0123abcd",
                "RestartCount": restarts,
                "State": state,
                "HostConfig": {"Memory": 536870912, "NanoCpus": 100000000},
                "Config": {"Image": "cairn-combined:local"},
            }
        ]
    )


def test_container_facts_come_from_docker_inspect() -> None:
    facts = parse_container_facts(inspect_json(oom=True, restarts=2, health="unhealthy"))
    assert facts.name == APP
    assert facts.running
    assert facts.oom_killed
    assert facts.restart_count == 2
    assert facts.health == "unhealthy"
    assert facts.memory_limit_bytes == 512 * 1024 * 1024
    assert facts.nano_cpus == 100_000_000
    assert facts.image_reference == "cairn-combined:local"


@pytest.mark.parametrize("text", ["", "[]", "{}", '[{"State": {}}]'])
def test_unreadable_inspect_output_is_an_unusable_profile(text: str) -> None:
    with pytest.raises(RamCheckError):
        parse_container_facts(text)


def test_core_containers_are_refused_and_named() -> None:
    refuse_core_containers({"ft-db": "cairn-ft-db-1", "ft-app": APP})
    with pytest.raises(RamCheckError, match=r"api, db.*make down"):
        refuse_core_containers({"db": "cairn-db-1", "api": "cairn-api-1", "ft-app": APP})


# --- sampler ------------------------------------------------------------------------------


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def test_sampler_takes_one_sample_per_interval_and_counts_failures() -> None:
    clock = FakeClock()
    values = iter([10.0, 20.0, None, 40.0, 50.0])

    def read() -> float:
        value = next(values)
        clock.sleep(0.5)  # a slow `docker stats`
        if value is None:
            raise RamReportError("docker stats returned nothing")
        return value

    run = sample_phase(
        "idle", read, duration_s=10, interval_s=2, clock=clock.monotonic, sleep=clock.sleep
    )
    assert run.samples_mib == (10.0, 20.0, 40.0, 50.0)
    assert run.sample_failures == 1
    assert run.times_s == (0.0, 2.0, 6.0, 8.0)  # sample 3 was due at 4.0 and failed


def test_sampler_always_takes_at_least_one_sample() -> None:
    clock = FakeClock()
    run = sample_phase(
        "idle", lambda: 1.0, duration_s=0, interval_s=2, clock=clock.monotonic, sleep=clock.sleep
    )
    assert run.samples_mib == (1.0,)


# --- load ---------------------------------------------------------------------------------


class RecordingFetch:
    """A fetch that answers as ft-app does and remembers what it was asked."""

    def __init__(self, *, fail_with: int | None = None, raise_on: str | None = None) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.lock = threading.Lock()
        self.fail_with = fail_with
        self.raise_on = raise_on

    def __call__(self, url: str, headers: Mapping[str, str], timeout_s: float) -> int:
        with self.lock:
            self.calls.append((url, dict(headers)))
        if self.raise_on is not None and url.endswith(self.raise_on):
            raise OSError(f"connection to {url} with {dict(headers)} failed")
        if self.fail_with is not None:
            return self.fail_with
        if url.endswith("/healthz") or (url.endswith("/readyz") and headers.get(PROXY_HEADER)):
            return 200
        return 404


def test_load_uses_two_clients_the_three_paths_and_the_header_where_needed() -> None:
    fetch = RecordingFetch()
    collect = start_load(
        fetch,
        base_url="http://127.0.0.1:1",
        token=TOKEN,
        duration_s=0.3,
        clients=2,
        pause_s=0.01,
        timeout_s=5,
    )
    records = collect()
    assert len(records) == len(fetch.calls) > 6
    assert {r.path for r in records} == {path for path, _, _ in LOAD_CYCLE}
    assert all(r.ok for r in records)
    for url, headers in fetch.calls:
        if url.endswith("/healthz"):
            assert PROXY_HEADER not in headers
        else:
            assert headers == {PROXY_HEADER: TOKEN}


def test_load_counts_wrong_statuses_and_missing_responses_as_errors_by_class_name() -> None:
    fetch = RecordingFetch(raise_on="/readyz")
    records = start_load(
        fetch,
        base_url="http://127.0.0.1:1",
        token=TOKEN,
        duration_s=0.2,
        clients=2,
        pause_s=0.01,
        timeout_s=5,
    )()
    failed = [r for r in records if not r.ok]
    assert failed
    assert {r.path for r in failed} == {"/readyz"}
    assert {r.error for r in failed} == {"OSError"}
    assert all(r.status is None for r in failed)
    assert all(TOKEN not in repr(r) for r in records)


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        allowed = self.path == "/healthz" or (
            self.path == "/readyz" and self.headers.get(PROXY_HEADER) == "t"
        )
        code = 200 if allowed else 404
        self.send_response(code)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, format: str, *args: object) -> None:
        pass


def test_http_get_returns_the_status_including_error_statuses() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        assert http_get(base + "/healthz", {}, 5) == 200
        assert http_get(base + "/readyz", {PROXY_HEADER: "t"}, 5) == 200
        assert http_get(base + "/readyz", {}, 5) == 404
        assert http_get(base + "/nope", {PROXY_HEADER: "t"}, 5) == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_http_get_refuses_anything_but_loopback() -> None:
    with pytest.raises(ValueError, match=r"127\.0\.0\.1"):
        http_get("http://example.invalid/readyz", {PROXY_HEADER: TOKEN}, 1)


# --- main: exit codes with fake runners ---------------------------------------------------


class FakeDocker:
    """Answers the docker commands of the script; records every call."""

    def __init__(
        self,
        *,
        stats: Sequence[float] = (80.0, 82.0, 85.0),
        inspect: str | None = None,
        inspect_after_wait: str | None = None,
        ps: str = f"ft-db\tcairn-ft-db-1\nft-app\t{APP}\n",
        info_error: bool = False,
        up_error: bool = False,
    ) -> None:
        self.stats = list(stats)
        self.inspect = inspect if inspect is not None else inspect_json()
        self.inspect_after_wait = inspect_after_wait
        self.inspects = 0
        self.ps = ps
        self.info_error = info_error
        self.up_error = up_error
        self.calls: list[list[str]] = []
        self.index = 0

    def __call__(self, args: Sequence[str], *, timeout: float = 120) -> str:
        argv = list(args)
        self.calls.append(argv)
        verb = argv[1]
        if verb == "info":
            if self.info_error:
                raise RamReportError("docker info exited 1: Cannot connect to the Docker daemon")
            return "3830000000\n" if "MemTotal" in argv[-1] else "28.5.1\n"
        if verb == "ps":
            return self.ps
        if verb == "compose":
            if self.up_error:
                raise RamReportError("docker compose --profile freetier exited 1: unhealthy")
            return ""
        if verb == "stats":
            mib = self.stats[self.index % len(self.stats)]
            self.index += 1
            return json.dumps({"Name": APP, "MemUsage": f"{mib}MiB / 512MiB"}) + "\n"
        if verb == "inspect":
            self.inspects += 1
            if self.inspects > 1 and self.inspect_after_wait is not None:
                return self.inspect_after_wait
            return self.inspect
        if verb == "exec":
            if argv[-1].endswith("memory.peak"):
                return "150000000\n"
            return "anon 60000000\nfile 20000000\nshmem 0\n"
        raise AssertionError(f"unexpected docker call: {argv}")

    def verbs(self) -> list[str]:
        return [call[1] for call in self.calls]


def invoke(
    tmp_path: Path,
    docker: FakeDocker,
    fetch: RecordingFetch | None = None,
    *,
    token: str | None = TOKEN,
    extra: Sequence[str] = (),
) -> int:
    env = tmp_path / ".env"
    lines = ["CAIRN_FREETIER_APP_PORT=8123"]
    if token is not None:
        lines.append(f"CAIRN_PROXY_TOKEN={token}")
    env.write_text("\n".join(lines) + "\n", encoding="utf-8")
    argv = [
        "--idle-s=0.2",
        "--load-s=0.2",
        "--interval-s=0.05",
        "--pause-s=0.01",
        f"--out-dir={tmp_path / 'evidence'}",
        f"--env-file={env}",
        "--date=2026-11-02",
        *extra,
    ]
    return main(argv, run=docker, fetch=fetch or RecordingFetch())


@pytest.fixture(autouse=True)
def _no_ambient_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CAIRN_PROXY_TOKEN", raising=False)
    monkeypatch.delenv("CAIRN_FREETIER_APP_PORT", raising=False)


def evidence(tmp_path: Path) -> tuple[Path, Path]:
    base = tmp_path / "evidence"
    return base / "freetier-ram-2026-11-02.md", base / "freetier-ram-2026-11-02.json"


def test_a_run_inside_the_budget_exits_0_and_writes_both_files(tmp_path: Path) -> None:
    docker = FakeDocker(stats=(80.0, 82.0, 85.0))
    fetch = RecordingFetch()
    assert invoke(tmp_path, docker, fetch) == EXIT_OK
    md, js = evidence(tmp_path)
    report = json.loads(js.read_text(encoding="utf-8"))
    assert report["result"] == "pass"
    assert report["reasons"] == []
    assert 3 <= len(report["idle"]["samples"]) <= 4
    assert report["idle"]["peak_mib"] == 85.0
    assert report["load"]["peak_mib"] == 85.0
    assert report["load"]["requests"] == len(fetch.calls) > 0
    assert report["load"]["errors"] == 0
    assert report["load"]["latency_ms"]["p50"] is not None
    assert report["load"]["latency_ms"]["p95"] is not None
    assert set(report["load"]["by_path"]) == {path for path, _, _ in LOAD_CYCLE}
    assert report["container"]["restart_count"] == 0
    assert report["cgroup_at_end"]["memory_peak_since_start_bytes"] == 150000000
    text = md.read_text(encoding="utf-8")
    assert text.startswith("# Free-tier RAM check, 2026-11-02")
    assert "PASS" in text
    assert "| Idle peak (MiB) | 300 |" in text
    assert "| Load peak (MiB) | 384 |" in text


def test_the_script_brings_the_freetier_profile_up_and_calls_the_app_on_the_env_port(
    tmp_path: Path,
) -> None:
    docker = FakeDocker()
    fetch = RecordingFetch()
    invoke(tmp_path, docker, fetch)
    assert [
        "docker",
        "compose",
        "--profile",
        "freetier",
        "up",
        "--wait",
    ] in docker.calls
    assert docker.verbs().index("compose") > docker.verbs().index("ps")
    assert all(url.startswith("http://127.0.0.1:8123/") for url, _ in fetch.calls)


def test_no_up_skips_compose(tmp_path: Path) -> None:
    docker = FakeDocker()
    assert invoke(tmp_path, docker, extra=["--no-up"]) == EXIT_OK
    assert "compose" not in docker.verbs()


def test_an_idle_peak_over_300_exits_1_and_still_writes_the_evidence(tmp_path: Path) -> None:
    assert invoke(tmp_path, FakeDocker(stats=(100.0, 301.0, 100.0))) == EXIT_OVER_BUDGET
    md, js = evidence(tmp_path)
    report = json.loads(js.read_text(encoding="utf-8"))
    assert report["result"] == "fail"
    assert any("idle peak" in reason for reason in report["reasons"])
    assert "FAIL" in md.read_text(encoding="utf-8")


def test_an_oom_kill_exits_1(tmp_path: Path) -> None:
    docker = FakeDocker(inspect=inspect_json(oom=True))
    assert invoke(tmp_path, docker) == EXIT_OVER_BUDGET
    report = json.loads(evidence(tmp_path)[1].read_text(encoding="utf-8"))
    assert "ft-app was OOM-killed" in report["reasons"]
    assert report["container"]["oom_killed"] is True


def test_a_restart_exits_1(tmp_path: Path) -> None:
    assert invoke(tmp_path, FakeDocker(inspect=inspect_json(restarts=1))) == EXIT_OVER_BUDGET


def test_failed_requests_over_two_percent_exit_1(tmp_path: Path) -> None:
    assert invoke(tmp_path, FakeDocker(), RecordingFetch(fail_with=500)) == EXIT_OVER_BUDGET
    report = json.loads(evidence(tmp_path)[1].read_text(encoding="utf-8"))
    assert report["load"]["errors"] == report["load"]["requests"] > 0
    assert report["load"]["status_counts"] == {"500": report["load"]["requests"]}


def test_a_container_that_is_gone_at_the_end_exits_1(tmp_path: Path) -> None:
    # The first inspect (the health wait) sees it healthy, the one after the load does not.
    gone = inspect_json(running=False, status="exited", health=None, exit_code=137)
    assert invoke(tmp_path, FakeDocker(inspect_after_wait=gone)) == EXIT_OVER_BUDGET
    report = json.loads(evidence(tmp_path)[1].read_text(encoding="utf-8"))
    assert any("not running at the end" in reason for reason in report["reasons"])


def test_docker_not_reachable_exits_2_without_evidence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert invoke(tmp_path, FakeDocker(info_error=True)) == EXIT_UNUSABLE
    assert "Docker is not usable" in capsys.readouterr().err
    assert not evidence(tmp_path)[0].exists()


def test_a_failing_compose_up_exits_2(tmp_path: Path) -> None:
    assert invoke(tmp_path, FakeDocker(up_error=True)) == EXIT_UNUSABLE


def test_a_missing_ft_app_exits_2(tmp_path: Path) -> None:
    assert invoke(tmp_path, FakeDocker(ps="ft-db\tcairn-ft-db-1\n")) == EXIT_UNUSABLE


def test_an_unhealthy_ft_app_exits_2(tmp_path: Path) -> None:
    assert invoke(tmp_path, FakeDocker(inspect=inspect_json(health="unhealthy"))) == EXIT_UNUSABLE


def test_running_core_containers_exit_2_before_anything_is_started(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    docker = FakeDocker(ps="db\tcairn-db-1\napi\tcairn-api-1\n")
    assert invoke(tmp_path, docker) == EXIT_UNUSABLE
    assert "never run together" in capsys.readouterr().err
    assert "compose" not in docker.verbs()
    assert "stats" not in docker.verbs()


def test_a_missing_token_exits_2_before_docker_is_touched(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    docker = FakeDocker()
    assert invoke(tmp_path, docker, token=None) == EXIT_UNUSABLE
    assert "CAIRN_PROXY_TOKEN" in capsys.readouterr().err
    assert docker.calls == []


# --- the token never leaves the process except as a header -------------------------------


def test_the_token_is_in_no_output_and_no_evidence_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The fetch raises messages that contain the headers, as a careless client library might.
    fetch = RecordingFetch(raise_on="/readyz")
    code = invoke(tmp_path, FakeDocker(), fetch)
    assert code == EXIT_OVER_BUDGET  # every /readyz call failed: a third of the requests
    captured = capsys.readouterr()
    md, js = evidence(tmp_path)
    everything = captured.out + captured.err + md.read_text() + js.read_text()
    assert TOKEN not in everything
    assert TOKEN[:12] not in everything
    assert any(PROXY_HEADER in headers for _, headers in fetch.calls)  # it was sent


def test_a_token_from_the_environment_wins_and_is_not_printed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    other = "ENVTOKEN-abcdefghijklmnopqrstuvwxyz012345"
    monkeypatch.setenv("CAIRN_PROXY_TOKEN", other)
    fetch = RecordingFetch()
    assert invoke(tmp_path, FakeDocker(), fetch) == EXIT_OK
    assert any(headers.get(PROXY_HEADER) == other for _, headers in fetch.calls)
    captured = capsys.readouterr()
    assert other not in captured.out + captured.err
    assert TOKEN not in captured.out + captured.err


def test_the_module_keeps_the_names_the_design_gives() -> None:
    for name in ("RamBudget", "Phase", "verdict"):
        assert hasattr(freetier_ram, name)
