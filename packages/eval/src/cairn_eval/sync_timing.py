"""Timing harness for live sync (plan 3.13, 3.18, 3.23).

A *trial* makes one scripted change, then probes every query path until the outcome is settled:

* **revocation**: from the change until the send time of the first probe after which no probe on any
  path returns the content (so a probe that returns 503 ``permissions_syncing`` counts as "not
  returned", and the final clean pass is re-checked on every path);
* **freshness**: from the change until a probe returns the new content, so a wake counts in full.

Each trial records whether the API was warm or asleep at the change. Cold trials are in every
reported p95, and warm and cold p95 are also reported separately. The harness is transport
agnostic: it takes callables, so tests drive the in-process API and a deployment run drives HTTP.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

import pyarrow as pa
import pyarrow.parquet as pq

from cairn_eval.metrics import percentile

Kind = Literal["revocation", "freshness"]
Probe = Callable[[], str]
"""Sends one request on one query path and returns the response text."""


@dataclass(frozen=True)
class Trial:
    kind: Kind
    trial: int
    api_state: Literal["warm", "asleep"]
    seconds: float
    probes: int
    settled: bool


def _settled_clean(probes: Mapping[str, Probe], needle: str) -> bool:
    return all(needle not in probe() for probe in probes.values())


def measure_revocation(
    change: Callable[[], None],
    probes: Mapping[str, Probe],
    needle: str,
    *,
    trial: int,
    api_state: Literal["warm", "asleep"],
    timeout_s: float = 90.0,
    interval_s: float = 0.05,
    clock: Callable[[], float] = time.monotonic,
) -> Trial:
    """Seconds from ``change()`` to the send time of the first probe that starts a clean pass."""
    start = clock()
    change()
    sent = 0
    while clock() - start < timeout_s:
        t_send = clock()
        sent += 1
        if _settled_clean(probes, needle):
            sent += len(probes) - 1
            # a second full pass guards against a probe that was clean only by luck
            if _settled_clean(probes, needle):
                return Trial("revocation", trial, api_state, t_send - start, sent, True)
        time.sleep(interval_s)
    return Trial("revocation", trial, api_state, timeout_s, sent, False)


def measure_freshness(
    change: Callable[[], None],
    probes: Mapping[str, Probe],
    needle: str,
    *,
    trial: int,
    api_state: Literal["warm", "asleep"],
    timeout_s: float = 90.0,
    interval_s: float = 0.05,
    clock: Callable[[], float] = time.monotonic,
) -> Trial:
    """Seconds from ``change()`` until a probe on any path returns the new content."""
    start = clock()
    change()
    sent = 0
    while clock() - start < timeout_s:
        for probe in probes.values():
            sent += 1
            if needle in probe():
                return Trial("freshness", trial, api_state, clock() - start, sent, True)
        time.sleep(interval_s)
    return Trial("freshness", trial, api_state, timeout_s, sent, False)


def summarize(trials: Sequence[Trial]) -> dict[str, dict[str, float | int]]:
    """p50/p95/max per kind, overall and split by API state. Unsettled trials count at the timeout."""  # noqa: E501
    out: dict[str, dict[str, float | int]] = {}
    for kind in ("revocation", "freshness"):
        for label, subset in (
            ("all", [t for t in trials if t.kind == kind]),
            ("warm", [t for t in trials if t.kind == kind and t.api_state == "warm"]),
            ("asleep", [t for t in trials if t.kind == kind and t.api_state == "asleep"]),
        ):
            if not subset:
                continue
            xs = [t.seconds for t in subset]
            out[f"{kind}.{label}"] = {
                "n": len(xs),
                "p50_s": round(percentile(xs, 50), 3),
                "p95_s": round(percentile(xs, 95), 3),
                "max_s": round(max(xs), 3),
                "unsettled": sum(1 for t in subset if not t.settled),
            }
    return out


def write_parquet(path: Path, trials: Sequence[Trial]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [asdict(t) for t in trials]
    pq.write_table(pa.Table.from_pylist(rows), path)
