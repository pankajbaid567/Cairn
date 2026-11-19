"""Property test for the free-tier RAM verdict (Requirement 12.2).

The oracle below restates the rules of the design in plain Python and does not
call or import any helper of ``scripts.freetier_ram`` except the unit under
test (``verdict``) and its two data classes. Budgets are the defaults
(300 / 384 MiB, 2 %) or custom values with idle peak <= load peak <= limit.
Sample values sit exactly on, one float step either side of, and far from each
budget; error counts sit on, one below and one above the allowed count; sample
lists may be empty; requests may be 0.

**Validates: Requirements 12.2**
"""

# Feature: cairn-free-tier-retarget, Property 12: The RAM verdict follows its rules

import math
from dataclasses import dataclass

from hypothesis import given, settings
from hypothesis import strategies as st

from scripts.freetier_ram import Phase, RamBudget, verdict

RULES = ("idle", "load", "errors", "oom", "restarts")


@st.composite
def budgets(draw: st.DrawFn) -> RamBudget:
    if draw(st.booleans()):
        return RamBudget(limit_mib=512.0, idle_peak_mib=300.0, load_peak_mib=384.0)
    idle = draw(st.floats(min_value=1, max_value=2000, allow_nan=False))
    load = draw(st.floats(min_value=idle, max_value=2500, allow_nan=False))
    limit = draw(st.floats(min_value=load, max_value=4096, allow_nan=False))
    ratio = draw(st.sampled_from([0.0, 0.01, 0.02, 0.05, 0.5]))
    return RamBudget(limit_mib=limit, idle_peak_mib=idle, load_peak_mib=load, max_error_ratio=ratio)


@st.composite
def samples(draw: st.DrawFn, threshold: float) -> tuple[float, ...]:
    near = st.sampled_from(
        [
            threshold,
            math.nextafter(threshold, 0.0),
            math.nextafter(threshold, math.inf),
            threshold - 1.0,
            threshold + 1.0,
        ]
    )
    anywhere = st.floats(min_value=0, max_value=3000, allow_nan=False)
    below = st.floats(min_value=0, max_value=threshold, allow_nan=False)
    return tuple(draw(st.lists(st.one_of(near, below, anywhere), max_size=8)))


latencies = st.lists(st.floats(min_value=0, max_value=60_000, allow_nan=False), max_size=6).map(
    tuple
)


@dataclass(frozen=True)
class Case:
    budget: RamBudget
    idle_samples: tuple[float, ...]
    load_samples: tuple[float, ...]
    requests: int
    errors: int
    oom: bool
    restarts: int


@st.composite
def cases(draw: st.DrawFn) -> Case:
    budget = draw(budgets())
    requests = draw(st.integers(min_value=0, max_value=500))
    allowed = math.floor(budget.max_error_ratio * requests)
    errors = draw(
        st.one_of(
            st.integers(min_value=0, max_value=requests),
            st.sampled_from([allowed - 1, allowed, allowed + 1]).map(
                lambda n: min(max(n, 0), requests)
            ),
        )
    )
    return Case(
        budget=budget,
        idle_samples=draw(samples(budget.idle_peak_mib)),
        load_samples=draw(samples(budget.load_peak_mib)),
        requests=requests,
        errors=errors,
        oom=draw(st.booleans()),
        restarts=draw(st.one_of(st.just(0), st.integers(min_value=0, max_value=5))),
    )


def run(
    case: Case,
    idle_samples: tuple[float, ...] | None = None,
    load_samples: tuple[float, ...] | None = None,
    idle_latencies: tuple[float, ...] = (),
    load_latencies: tuple[float, ...] = (),
    budget: RamBudget | None = None,
) -> tuple[bool, list[str]]:
    idle = Phase(
        "idle",
        case.idle_samples if idle_samples is None else idle_samples,
        latencies_ms=idle_latencies,
    )
    load = Phase(
        "load",
        case.load_samples if load_samples is None else load_samples,
        requests=case.requests,
        errors=case.errors,
        latencies_ms=load_latencies,
    )
    return verdict(budget or case.budget, idle, load, case.oom, case.restarts)


def failed_rules(case: Case) -> set[str]:
    """The rules of the design, written out by hand."""
    b = case.budget
    failed: set[str] = set()
    if not case.idle_samples or max(case.idle_samples) > b.idle_peak_mib:
        failed.add("idle")
    if not case.load_samples or max(case.load_samples) > b.load_peak_mib:
        failed.add("load")
    if not case.errors <= b.max_error_ratio * case.requests:
        failed.add("errors")
    if case.oom:
        failed.add("oom")
    if case.restarts != 0:
        failed.add("restarts")
    return failed


def rule_of(reason: str) -> set[str]:
    """Which rules a reason text names (keyword match on the lower-cased text)."""
    text = reason.lower()
    named: set[str] = set()
    if text.startswith("idle"):
        named.add("idle")
    if text.startswith("load"):
        named.add("load")
    if "request" in text:
        named.add("errors")
    if "oom" in text:
        named.add("oom")
    if "restart" in text:
        named.add("restarts")
    return named


@settings(max_examples=300)
@given(cases())
def test_verdict_passes_iff_every_rule_holds(case: Case) -> None:
    ok, reasons = run(case)
    failed = failed_rules(case)
    assert ok is (not failed)
    assert (reasons == []) is ok

    # One reason per failed rule, each naming exactly one rule, none for a rule that held.
    assert len(reasons) == len(failed)
    named = [rule_of(reason) for reason in reasons]
    assert all(len(n) == 1 for n in named)
    assert {next(iter(n)) for n in named} == failed


@settings(max_examples=200)
@given(cases(), st.data())
def test_verdict_ignores_sample_order_and_latencies(case: Case, data: st.DataObject) -> None:
    idle = tuple(data.draw(st.permutations(case.idle_samples)))
    load = tuple(data.draw(st.permutations(case.load_samples)))
    idle_latencies = tuple(data.draw(latencies))
    load_latencies = tuple(data.draw(latencies))
    assert run(case, idle, load, idle_latencies, load_latencies) == run(case)


@settings(max_examples=200)
@given(cases())
def test_custom_budgets_behave_like_the_default_ones(case: Case) -> None:
    """Same outcome for equal values, however the budget object was built."""
    b = case.budget
    rebuilt = RamBudget(
        max_error_ratio=b.max_error_ratio,
        load_peak_mib=b.load_peak_mib,
        idle_peak_mib=b.idle_peak_mib,
        limit_mib=b.limit_mib,
    )
    assert run(case, budget=rebuilt) == run(case)
    # The limit is not a verdict rule: moving it (keeping the order) changes nothing.
    wider = RamBudget(
        limit_mib=b.limit_mib + 100.0,
        idle_peak_mib=b.idle_peak_mib,
        load_peak_mib=b.load_peak_mib,
        max_error_ratio=b.max_error_ratio,
    )
    assert run(case, budget=wider) == run(case)


def test_default_budget_boundaries() -> None:
    """Exact figures: 300 and 384 MiB pass, the next float fails; 2 of 100 passes, 3 fails."""
    budget = RamBudget()
    idle = Phase("idle", (300.0,))
    load = Phase("load", (384.0,), requests=100, errors=2)
    assert verdict(budget, idle, load, False, 0) == (True, [])
    assert (
        verdict(budget, Phase("idle", (math.nextafter(300.0, 400.0),)), load, False, 0)[0] is False
    )
    assert (
        verdict(budget, idle, Phase("load", (math.nextafter(384.0, 500.0),), 100, 2), False, 0)[0]
        is False
    )
    assert verdict(budget, idle, Phase("load", (384.0,), 100, 3), False, 0)[0] is False
    assert verdict(budget, idle, Phase("load", (384.0,), 0, 0), False, 0) == (True, [])
    ok, reasons = verdict(budget, Phase("idle", ()), Phase("load", ()), True, 2)
    assert ok is False
    assert len(reasons) == 4  # idle, load, OOM, restarts; 0 errors of 0 requests holds
