"""Daily neuron budget for Workers AI (design §12, FT-22; Requirement 15).

Workers AI gives a free allocation of 10,000 neurons per UTC day. This guard keeps the project
under ``CAIRN_WORKERS_AI_DAILY_NEURONS`` (default 9,000, 90% of the allocation):

- Before every attempt the client reserves an *estimate* of its neurons for the current UTC day.
  A reservation that would take the day's total above the cap is refused and changes nothing.
- Reservations are never released, even when the call then fails. The guard is conservative on
  purpose: it may stop short of the cap, and it never spends past it.
- The total survives restarts when the store is Postgres (``cairn_meta.provider_usage``,
  migration 0002). Render's free service sleeps and restarts, so an in-memory counter alone
  would reset. One conditional upsert makes concurrent reservations safe.
- A model without a price in ``NEURON_RATES`` raises ``UnpricedModel`` before any call: no call
  without a price.

Estimates (Requirement 15.3): ``tokens_in`` is ``max(1, ceil(UTF-8 bytes / 3))`` per input text,
summed. That counts more tokens than English text has (about four characters per token), so the
estimate leans high. Callers pass ``max_tokens_out`` as the output bound; for a reasoning model
(``glm-4.7-flash``, ``gemma-4-26b-a4b-it``) the bound has to cover the reasoning tokens too (FV9).

A Postgres failure (unreachable, timeout, permission) propagates as the psycopg exception, and
the caller makes no call: the guard fails closed.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from fractions import Fraction
from typing import Any, Final, Protocol

import psycopg

from cairn_core.config import ApiSettings, CommonSettings, libpq_tls

__all__ = [
    "NEURON_RATES",
    "BudgetExhausted",
    "InMemoryUsageStore",
    "NeuronBudget",
    "NeuronRate",
    "PostgresUsageStore",
    "UnpricedModel",
    "UsageStore",
    "estimate_input_tokens",
    "estimate_neurons",
    "estimate_tokens",
    "utc_today",
]

log = logging.getLogger(__name__)

PROVIDER_PATTERN: Final = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
"""Same rule as the CHECK constraint on ``cairn_meta.provider_usage.provider``. The in-memory
store applies it too, so both stores refuse the same input."""

_NEURONS_PER_UNIT: Final = 1_000_000
"""Rates are neurons per million tokens."""


# --- the price table -----------------------------------------------------------------------


@dataclass(frozen=True)
class NeuronRate:
    """Neurons per million tokens. Embedders and the reranker have no output price."""

    input_per_m: float
    output_per_m: float = 0.0


NEURON_RATES: Final[Mapping[str, NeuronRate]] = {
    # Workers AI pricing page, read 3 Nov 2026 (FV9). 0.011 USD per 1,000 neurons.
    # bge-small: the page lists 1841 (the 30 Oct design had 1836); the higher value is kept.
    "@cf/baai/bge-small-en-v1.5": NeuronRate(1841),  # $0.020 per M input tokens
    "@cf/baai/bge-reranker-base": NeuronRate(283),  # $0.00311 per M input tokens
    "@cf/google/gemma-4-26b-a4b-it": NeuronRate(9091, 27273),
    "@cf/zai-org/glm-4.7-flash": NeuronRate(5500, 36400),
    "@cf/meta/llama-3.1-8b-instruct-fp8-fast": NeuronRate(4119, 34868),
}


class UnpricedModel(Exception):
    """The model has no entry in ``NEURON_RATES``; no call is made without a price."""

    def __init__(self, model: str) -> None:
        super().__init__(f"no neuron rate for model {model!r}")
        self.model = model


def estimate_tokens(text: str) -> int:
    """Tokens in one text: ``max(1, ceil(UTF-8 bytes / 3))``."""
    return max(1, math.ceil(len(text.encode("utf-8")) / 3))


def estimate_input_tokens(texts: Iterable[str]) -> int:
    """``estimate_tokens`` summed over the input texts (Requirement 15.3)."""
    return sum(estimate_tokens(t) for t in texts)


def estimate_neurons(model: str, tokens_in: int, max_tokens_out: int = 0) -> int:
    """``ceil((tokens_in * input rate + max_tokens_out * output rate) / 1,000,000)``, at least 1.

    The arithmetic is exact (``Fraction``), so a total that is a whole number is never rounded
    up by float error. Raises ``UnpricedModel`` for a model with no rate, and ``ValueError`` for
    a negative count.
    """
    rate = NEURON_RATES.get(model)
    if rate is None:
        raise UnpricedModel(model)
    if tokens_in < 0 or max_tokens_out < 0:
        raise ValueError("token counts must not be negative")
    total = Fraction(rate.input_per_m) * tokens_in + Fraction(rate.output_per_m) * max_tokens_out
    return max(1, math.ceil(total / _NEURONS_PER_UNIT))


# --- stores ---------------------------------------------------------------------------------


def utc_today() -> date:
    """The current UTC date, the budget's day boundary (Workers AI resets at 00:00 UTC)."""
    return datetime.now(UTC).date()


def _check_arguments(provider: str, neurons: int, cap: int) -> None:
    if PROVIDER_PATTERN.fullmatch(provider) is None:
        raise ValueError("provider must match ^[a-z][a-z0-9_]{0,31}$")
    if neurons < 0:
        raise ValueError("neurons must not be negative")
    if cap < 0:
        raise ValueError("cap must not be negative")


class UsageStore(Protocol):
    """Where reserved neurons are counted, per UTC day and provider."""

    async def try_reserve(self, day: date, provider: str, neurons: int, cap: int) -> int | None:
        """Add ``neurons`` to the day's total if the new total stays at or below ``cap`` and
        return it; otherwise change nothing and return ``None``. Atomic."""
        ...

    async def used(self, day: date, provider: str) -> int:
        """The day's reserved total, 0 when nothing was reserved."""
        ...


class InMemoryUsageStore:
    """Process-local store for tests and local runs. Totals are lost on restart."""

    def __init__(self) -> None:
        self._totals: dict[tuple[date, str], int] = {}
        self._lock = asyncio.Lock()

    async def try_reserve(self, day: date, provider: str, neurons: int, cap: int) -> int | None:
        _check_arguments(provider, neurons, cap)
        async with self._lock:
            total = self._totals.get((day, provider), 0) + neurons
            if total > cap:
                return None
            self._totals[(day, provider)] = total
            return total

    async def used(self, day: date, provider: str) -> int:
        async with self._lock:
            return self._totals.get((day, provider), 0)


RESERVE_SQL: Final = """
INSERT INTO cairn_meta.provider_usage AS u (day, provider, neurons_reserved)
SELECT %(day)s::date, %(provider)s::text, %(n)s::bigint WHERE %(n)s::bigint <= %(cap)s::bigint
ON CONFLICT (day, provider) DO UPDATE
   SET neurons_reserved = u.neurons_reserved + EXCLUDED.neurons_reserved, updated_at = now()
 WHERE u.neurons_reserved + EXCLUDED.neurons_reserved <= %(cap)s::bigint
RETURNING neurons_reserved
"""
"""One atomic statement (Requirement 15.5). With no row for the day it inserts only when the
reservation alone fits the cap. With a row, the conflict branch updates only when the sum fits;
PostgreSQL re-checks that condition against the latest committed row after waiting for a
concurrent writer, so two sessions cannot both pass the cap. No row is returned when refused."""

USED_SQL: Final = (
    "SELECT neurons_reserved FROM cairn_meta.provider_usage "
    "WHERE day = %(day)s::date AND provider = %(provider)s::text"
)


class PostgresUsageStore:
    """Counts in ``cairn_meta.provider_usage`` through a short-lived connection as ``cairn_app``.

    One connection per call, closed straight away, so Neon can scale to zero between calls and
    nothing needs reconnecting after a restart. TLS settings come from ``libpq_tls`` like every
    other connection (Requirement 4.4). ``repr`` shows no credential.
    """

    def __init__(
        self, settings: CommonSettings, *, user: str | None = None, password: str | None = None
    ) -> None:
        # `user` and `password` let the importer and the embedding worker count as cairn_ingest,
        # which holds the same grants on the table (migration 0003).
        self._settings = settings
        self._user = user
        self._password = password

    def __repr__(self) -> str:
        s = self._settings
        return (
            f"PostgresUsageStore(host={s.db_host!r}, db={s.db_name!r}, "
            f"user={(self._user or getattr(s, 'app_db_user', '?'))!r})"
        )

    @staticmethod
    def _app_login(s: CommonSettings) -> tuple[str, str]:
        """The ``cairn_app`` login of API settings (the default when no login is passed)."""
        if not isinstance(s, ApiSettings):
            raise ValueError("user and password are required unless the settings are ApiSettings")
        return s.app_db_user, s.app_db_password.get_secret_value()

    async def _connect(self) -> psycopg.AsyncConnection[tuple[Any, ...]]:
        s = self._settings
        timeout_ms = max(1, round(s.db_timeout_s * 1000))
        return await psycopg.AsyncConnection.connect(
            host=s.db_host,
            port=s.db_port,
            dbname=s.db_name,
            user=self._user or self._app_login(s)[0],
            password=self._password or self._app_login(s)[1],
            connect_timeout=max(1, math.ceil(s.db_timeout_s)),
            application_name="cairn-neuron-budget",
            options=f"-c statement_timeout={timeout_ms}",
            autocommit=True,
            **libpq_tls(s),
        )

    async def try_reserve(self, day: date, provider: str, neurons: int, cap: int) -> int | None:
        _check_arguments(provider, neurons, cap)
        params = {"day": day, "provider": provider, "n": neurons, "cap": cap}
        async with await self._connect() as conn:
            cur = await conn.execute(RESERVE_SQL, params)
            row = await cur.fetchone()
        return None if row is None else int(row[0])

    async def used(self, day: date, provider: str) -> int:
        async with await self._connect() as conn:
            cur = await conn.execute(USED_SQL, {"day": day, "provider": provider})
            row = await cur.fetchone()
        return 0 if row is None else int(row[0])


# --- the guard ------------------------------------------------------------------------------


class BudgetExhausted(Exception):
    """A reservation would take the day's total above the cap. Nothing was reserved."""

    def __init__(self, requested: int, cap: int) -> None:
        super().__init__(f"neuron budget exhausted (requested {requested}, daily cap {cap})")
        self.requested = requested
        self.cap = cap


class NeuronBudget:
    """Reserve neurons against the current UTC day, or refuse. See the module docstring."""

    def __init__(
        self,
        store: UsageStore,
        cap: int,
        *,
        provider: str = "workers_ai",
        today: Callable[[], date] = utc_today,
    ) -> None:
        if cap < 0:
            raise ValueError("cap must not be negative")
        if PROVIDER_PATTERN.fullmatch(provider) is None:
            raise ValueError("provider must match ^[a-z][a-z0-9_]{0,31}$")
        self._store = store
        self._cap = cap
        self._provider = provider
        self._today = today

    @property
    def cap(self) -> int:
        return self._cap

    async def reserve(self, neurons: int) -> int:
        """Reserve ``neurons`` for today and return the new total; raise ``BudgetExhausted``
        (leaving the total unchanged) when it would pass the cap."""
        if neurons < 0:
            raise ValueError("neurons must not be negative")
        total = await self._store.try_reserve(self._today(), self._provider, neurons, self._cap)
        if total is None:
            log.warning(
                "workers_ai.budget_refused",
                extra={"requested": neurons, "cap": self._cap, "provider": self._provider},
            )
            raise BudgetExhausted(neurons, self._cap)
        return total

    async def used(self) -> int:
        """Today's reserved total."""
        return await self._store.used(self._today(), self._provider)
