"""``S1Config``: every knob of the S1 generator, validated at construction (design §13)."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Final, Literal

AclMode = Literal["uncorrelated", "clustered"]
ACL_MODES: Final[tuple[str, ...]] = ("uncorrelated", "clustered")

TOPIC_TOKENS: Final = 4
"""Topic tokens ``t<cluster>_<j>`` in every body, so a lexical query can target a cluster."""

SELECTIVITY_GROUP_BASE: Final = 1000
"""Selectivity ``k`` is ACL group ``1000 + k``."""

NOISE_GROUP_BASE: Final = 2000
"""Noise groups are ``2000 .. 2000 + noise_groups - 1``; no query principal holds one."""

MAX_SELECTIVITIES: Final = NOISE_GROUP_BASE - SELECTIVITY_GROUP_BASE
MAX_CLUSTERS: Final = 32_767
"""``cluster`` is stored as int16."""

MAX_SEED: Final = 2**64 - 1

NO_ACCESS_PRINCIPAL: Final = "p_none"
"""The principal that holds no group and therefore sees no row."""


@dataclass(frozen=True)
class S1Config:
    """Generator settings. Defaults are the laptop run: 50,000 rows of 384 dimensions.

    ``batch_rows`` changes Parquet row-group boundaries and peak memory, never the data.
    """

    rows: int = 50_000
    dim: int = 384
    clusters: int = 64
    selectivities: tuple[float, ...] = (0.01, 0.05, 0.20, 0.50)
    acl_mode: AclMode = "uncorrelated"
    noise_groups: int = 200
    tokens_per_chunk: int = 32
    vocab: int = 5_000
    sigma: float = 0.35
    batch_rows: int = 5_000
    queries: int = 100
    seed: int = 20261027

    def __post_init__(self) -> None:
        # Frozen dataclass: normalise a list (for example from JSON) to a tuple of floats.
        object.__setattr__(self, "selectivities", tuple(float(s) for s in self.selectivities))
        problems = self.problems()
        if problems:
            raise ValueError("invalid S1 configuration: " + "; ".join(problems))

    def problems(self) -> list[str]:
        """Every reason this configuration is unusable, empty when it is fine."""
        found: list[str] = []
        if self.rows < 1:
            found.append("rows must be at least 1")
        if self.dim < 2:
            found.append("dim must be at least 2")
        if not 1 <= self.clusters <= MAX_CLUSTERS:
            found.append(f"clusters must be between 1 and {MAX_CLUSTERS}")
        if not 1 <= len(self.selectivities) <= MAX_SELECTIVITIES:
            found.append(f"selectivities must hold between 1 and {MAX_SELECTIVITIES} values")
        if any(not 0.0 < s <= 1.0 for s in self.selectivities):
            found.append("every selectivity must be in (0, 1]")
        if len(set(self.selectivities)) != len(self.selectivities):
            found.append("selectivities must be distinct")
        if self.acl_mode not in ACL_MODES:
            found.append(f"acl_mode must be one of {', '.join(ACL_MODES)}")
        if self.noise_groups < 0:
            found.append("noise_groups must not be negative")
        if self.tokens_per_chunk < TOPIC_TOKENS:
            found.append(f"tokens_per_chunk must be at least {TOPIC_TOKENS} (the topic tokens)")
        if self.vocab < 1:
            found.append("vocab must be at least 1")
        if self.sigma < 0.0:
            found.append("sigma must not be negative")
        if self.batch_rows < 1:
            found.append("batch_rows must be at least 1")
        if self.queries < 1:
            found.append("queries must be at least 1")
        if not 0 <= self.seed <= MAX_SEED:
            found.append(f"seed must be between 0 and {MAX_SEED}")
        return found

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready form for the manifest."""
        data = asdict(self)
        data["selectivities"] = list(self.selectivities)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> S1Config:
        """Inverse of ``to_dict``."""
        return cls(**data)


def principal_names(selectivities: tuple[float, ...]) -> tuple[str, ...]:
    """Query principal per selectivity: ``p01``, ``p05``, ``p20``, ``p50`` for the defaults.

    The name is the target in percent, zero-padded to two digits; a fractional percent
    keeps its digits with ``_`` for the point (``p0_5``). Names are unique because the
    selectivities are.
    """
    names: list[str] = []
    for target in selectivities:
        pct = target * 100
        rounded = round(pct)
        if abs(pct - rounded) < 1e-9:
            names.append(f"p{rounded:02d}")
        else:
            names.append("p" + f"{pct:.6g}".replace(".", "_"))
    return tuple(names)
