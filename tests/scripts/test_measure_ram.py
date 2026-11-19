"""RAM report: parsing, summarizing and rendering (Requirement 20)."""

import json
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from scripts import measure_ram
from scripts.measure_ram import (
    CEILING_BYTES,
    ESTIMATE_BYTES,
    GIB,
    MIB,
    Environment,
    Reading,
    Sample,
    Summary,
    parse_compose_ps,
    parse_images,
    parse_memory_stat,
    parse_size,
    parse_stats_output,
    render_json,
    render_markdown,
    summarize,
    verdict,
)

# Recorded from `docker stats --no-stream --format '{{json .}}'` on the live core stack.
RECORDED_STATS = """\
{"BlockIO":"0B / 0B","CPUPerc":"0.00%","Container":"998bbfb2fc09","ID":"998bbfb2fc09","MemPerc":"3.94%","MemUsage":"2.52MiB / 64MiB","Name":"cairn-web-1","NetIO":"872B / 126B","PIDs":"2"}
{"BlockIO":"0B / 0B","CPUPerc":"4.03%","Container":"de7eb3f0e071","ID":"de7eb3f0e071","MemPerc":"16.15%","MemUsage":"62MiB / 384MiB","Name":"cairn-worker-1","NetIO":"462kB / 544kB","PIDs":"11"}
{"BlockIO":"0B / 0B","CPUPerc":"0.51%","Container":"db42fee186ec","ID":"db42fee186ec","MemPerc":"13.04%","MemUsage":"50.09MiB / 384MiB","Name":"cairn-api-1","NetIO":"28.5kB / 26.9kB","PIDs":"2"}
{"BlockIO":"70.2MB / 10.5MB","CPUPerc":"1.07%","Container":"90ffde9ddc4e","ID":"90ffde9ddc4e","MemPerc":"11.19%","MemUsage":"114.6MiB / 1GiB","Name":"cairn-db-1","NetIO":"2.83MB / 2.44MB","PIDs":"15"}
"""  # noqa: E501

# Recorded from `docker exec cairn-db-1 cat /sys/fs/cgroup/memory.stat` (first lines).
RECORDED_MEMORY_STAT = """\
anon 23027712
file 109957120
kernel_stack 163840
shmem 38666240
file_mapped 62197760
inactive_file 23281664
active_file 47923200
"""


def reading(usage_mib: float, limit_mib: float = 1024, **cgroup_mib: float) -> Reading:
    return Reading(
        usage=round(usage_mib * MIB),
        limit=round(limit_mib * MIB),
        **{key: round(value * MIB) for key, value in cgroup_mib.items()},
    )


def constant_sample(total_bytes: int, at: float = 0.0) -> Sample:
    """A sample whose total is exactly ``total_bytes``, split over db and api."""
    half = total_bytes // 2
    return Sample(
        at,
        {
            "cairn-db-1": Reading(usage=half, limit=GIB),
            "cairn-api-1": Reading(usage=total_bytes - half, limit=GIB),
        },
    )


class TestParseSize:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("0B", 0),
            ("872B", 872),
            ("2.52MiB", round(2.52 * MIB)),
            ("1GiB", GIB),
            ("114.6MiB", round(114.6 * MIB)),
            ("5.5KiB", 5632),
            ("462kB", 462_000),
            ("70.2MB", 70_200_000),
            ("1.5GB", 1_500_000_000),
            ("64MiB", 64 * MIB),
        ],
    )
    def test_docker_units(self, text: str, expected: int) -> None:
        assert parse_size(text) == expected

    @pytest.mark.parametrize("text", ["", "MiB", "12 parsecs", "-1MiB", "1.2.3GiB"])
    def test_rejects_garbage(self, text: str) -> None:
        with pytest.raises(ValueError, match="size"):
            parse_size(text)

    @given(st.integers(min_value=0, max_value=64 * GIB))
    def test_whole_mebibytes_round_trip(self, value: int) -> None:
        mebibytes = value // MIB
        assert parse_size(f"{mebibytes}MiB") == mebibytes * MIB


class TestParseStats:
    def test_recorded_lines(self) -> None:
        stats = parse_stats_output(RECORDED_STATS)
        assert set(stats) == {"cairn-web-1", "cairn-worker-1", "cairn-api-1", "cairn-db-1"}
        assert stats["cairn-db-1"] == (round(114.6 * MIB), GIB)
        assert stats["cairn-web-1"] == (round(2.52 * MIB), 64 * MIB)

    def test_blank_lines_are_skipped(self) -> None:
        assert parse_stats_output("\n  \n") == {}

    @pytest.mark.parametrize(
        "line",
        ["not json", '{"Name": "x"}', '{"Name": "x", "MemUsage": "5MiB"}', "[1, 2]"],
    )
    def test_unreadable_line_raises(self, line: str) -> None:
        with pytest.raises(ValueError, match="unreadable"):
            parse_stats_output(line)


class TestParseOther:
    def test_memory_stat(self) -> None:
        stat = parse_memory_stat(RECORDED_MEMORY_STAT)
        assert stat["anon"] == 23_027_712
        assert stat["shmem"] == 38_666_240
        assert stat["file"] == 109_957_120

    def test_memory_stat_ignores_malformed_lines(self) -> None:
        assert parse_memory_stat("anon 5\nweird\nfile x\n") == {"anon": 5}

    def test_compose_ps(self) -> None:
        text = "db\tcairn-db-1\napi\tcairn-api-1\n\t\nnolabel\t\n"
        assert parse_compose_ps(text) == {"db": "cairn-db-1", "api": "cairn-api-1"}

    def test_images_join_container_and_registry_digest(self) -> None:
        containers = json.dumps(
            [
                {
                    "Name": "/cairn-web-1",
                    "Config": {"Image": "cairn-web:local"},
                    "Image": "sha256:b",
                },
                {"Name": "/cairn-db-1", "Config": {"Image": "pg:1@sha256:d"}, "Image": "sha256:a"},
            ]
        )
        images = json.dumps(
            [
                {"Id": "sha256:a", "RepoDigests": ["pg@sha256:d"]},
                {"Id": "sha256:b", "RepoDigests": None},
            ]
        )
        infos = parse_images(containers, images)
        assert [i.container for i in infos] == ["cairn-db-1", "cairn-web-1"]
        assert infos[0].repo_digests == ("pg@sha256:d",)
        assert infos[1].repo_digests == ()


class TestSummarize:
    def test_recorded_stack_mean_and_peak(self) -> None:
        stats = parse_stats_output(RECORDED_STATS)
        sample = Sample(
            0.0,
            {name: Reading(usage=usage, limit=limit) for name, (usage, limit) in stats.items()},
        )
        summary = summarize([sample])
        assert summary.total_peak == sum(usage for usage, _ in stats.values())
        assert summary.total_peak < ESTIMATE_BYTES
        assert not summary.exceeds_ceiling
        assert [c.name for c in summary.containers] == sorted(stats)

    def test_per_container_mean_and_peak(self) -> None:
        samples = [
            Sample(0, {"a": reading(100), "b": reading(10)}),
            Sample(5, {"a": reading(300), "b": reading(10)}),
            Sample(10, {"a": reading(200), "b": reading(40)}),
        ]
        by_name = {c.name: c for c in summarize(samples).containers}
        assert by_name["a"].mean == pytest.approx(200 * MIB)
        assert by_name["a"].peak == 300 * MIB
        assert by_name["b"].mean == pytest.approx(20 * MIB)
        assert by_name["b"].peak == 40 * MIB

    def test_total_peak_is_the_highest_single_sample_not_the_sum_of_peaks(self) -> None:
        samples = [
            Sample(0, {"a": reading(500), "b": reading(100)}),
            Sample(5, {"a": reading(100), "b": reading(500)}),
        ]
        summary = summarize(samples)
        assert summary.total_peak == 600 * MIB  # the sum of the peaks would be 1000 MiB
        assert summary.total_mean == pytest.approx(600 * MIB)

    def test_anon_plus_shmem_is_reported_apart_from_docker_stats_usage(self) -> None:
        samples = [
            Sample(0, {"db": reading(115, anon=22, file=105, shmem=37)}),
            Sample(5, {"db": reading(120, anon=24, file=105, shmem=37)}),
        ]
        summary = summarize(samples)
        db = summary.containers[0]
        assert db.unreclaimable_peak == 61 * MIB
        assert db.unreclaimable_mean == pytest.approx(60 * MIB)
        assert summary.unreclaimable_total_peak == 61 * MIB

    def test_missing_cgroup_data_gives_none_not_zero(self) -> None:
        summary = summarize([Sample(0, {"a": reading(10)})])
        assert summary.containers[0].anon_mean is None
        assert summary.unreclaimable_total_peak is None

    def test_no_samples_is_an_error(self) -> None:
        with pytest.raises(ValueError, match="no samples"):
            summarize([])


class TestCeiling:
    """Requirement 20.4: only a total peak strictly above 6 GiB fails."""

    def test_just_below(self) -> None:
        summary = summarize([constant_sample(CEILING_BYTES - MIB)])
        assert not summary.exceeds_ceiling
        assert verdict(summary).startswith("PASS")

    def test_exactly_at_the_ceiling_passes(self) -> None:
        assert not summarize([constant_sample(CEILING_BYTES)]).exceeds_ceiling

    def test_just_above(self) -> None:
        summary = summarize([constant_sample(CEILING_BYTES + MIB)])
        assert summary.exceeds_ceiling
        assert verdict(summary).startswith("FAIL")

    def test_one_spike_above_the_ceiling_fails_even_if_the_mean_is_low(self) -> None:
        samples = [constant_sample(GIB, 0), constant_sample(CEILING_BYTES + MIB, 5)]
        summary = summarize(samples)
        assert summary.total_mean < CEILING_BYTES
        assert summary.exceeds_ceiling

    @given(st.integers(min_value=1, max_value=12 * GIB))
    def test_verdict_matches_the_threshold(self, total: int) -> None:
        summary = summarize([constant_sample(total)])
        assert summary.total_peak == total
        assert summary.exceeds_ceiling == (total > CEILING_BYTES)


class TestRendering:
    def environment(self, docker: int | None = 4_109_672_448) -> Environment:
        return Environment(
            host_memory=8_589_934_592,
            docker_memory=docker,
            docker_version="29.0.0",
            other_containers=["openwa-api"],
            readyz_ok=20,
            readyz_calls=20,
            heartbeat_lines=2,
            started_at="2026-10-30T18:00:00Z",
        )

    def summary(self, total_mib: int = 240) -> Summary:
        return summarize(
            [
                Sample(
                    0,
                    {
                        "cairn-db-1": reading(total_mib * 0.5, anon=20, file=100, shmem=35),
                        "cairn-api-1": reading(total_mib * 0.5, 384, anon=50, file=0, shmem=0),
                    },
                )
            ]
        )

    def test_markdown_has_host_docker_estimate_and_rows(self) -> None:
        text = render_markdown(self.summary(), self.environment(), day="2026-10-30")
        assert "PASS" in text
        assert "Host (`sysctl hw.memsize`) | 8589934592 | 8.00" in text
        assert "Docker VM (`docker info` MemTotal) | 4109672448 | 3.83" in text
        assert "about 4.00 GiB estimate" in text
        assert "| cairn-db-1 |" in text
        assert "cairn-api-1" in text
        assert "openwa-api" in text
        assert "20 of 20" in text
        assert "excludes the Docker Desktop VM" in text

    def test_markdown_warns_when_the_docker_vm_is_smaller_than_the_ceiling(self) -> None:
        text = render_markdown(self.summary(), self.environment(), day="d")
        assert "smaller than the 6.00 GiB ceiling" in text

    def test_markdown_flags_an_exceeded_estimate_and_ceiling(self) -> None:
        text = render_markdown(
            summarize([constant_sample(CEILING_BYTES + MIB)]), self.environment(None), day="d"
        )
        assert "FAIL" in text
        assert "estimate is exceeded" in text
        assert "n/a" in text

    def test_json_round_trips_the_numbers(self) -> None:
        summary = self.summary()
        document = json.loads(render_json(summary, self.environment()))
        assert document["result"] == "pass"
        assert document["host_memory_bytes"] == 8_589_934_592
        assert document["docker_vm_memory_bytes"] == 4_109_672_448
        assert document["totals"]["peak_bytes"] == summary.total_peak
        assert document["comparison"]["estimate_bytes"] == ESTIMATE_BYTES
        assert document["comparison"]["ceiling_bytes"] == CEILING_BYTES
        assert document["comparison"]["exceeds_ceiling"] is False
        assert {c["name"] for c in document["containers"]} == {"cairn-db-1", "cairn-api-1"}

    def test_json_result_is_fail_over_the_ceiling(self) -> None:
        summary = summarize([constant_sample(CEILING_BYTES + MIB)])
        document = json.loads(render_json(summary, self.environment()))
        assert document["result"] == "fail"
        assert document["comparison"]["exceeds_ceiling"] is True


class TestMainExitCodes:
    def test_over_the_ceiling_exits_1_and_writes_both_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        summary = summarize([constant_sample(CEILING_BYTES + MIB)])
        monkeypatch.setattr(measure_ram, "measure", lambda args: (summary, Environment()))
        code = measure_ram.main(["--out-dir", str(tmp_path), "--date", "2026-01-02"])
        assert code == measure_ram.EXIT_OVER_CEILING
        assert (tmp_path / "core-ram-2026-01-02.md").is_file()
        assert (tmp_path / "core-ram-2026-01-02.json").is_file()

    def test_unusable_docker_exits_2_and_writes_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def broken(args: object) -> None:
            raise measure_ram.RamReportError("docker info exited 1")

        monkeypatch.setattr(measure_ram, "measure", broken)
        code = measure_ram.main(["--out-dir", str(tmp_path)])
        assert code == measure_ram.EXIT_UNUSABLE
        assert list(tmp_path.iterdir()) == []
