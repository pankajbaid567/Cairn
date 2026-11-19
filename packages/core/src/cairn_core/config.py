"""Runtime configuration for every Cairn process (design §7, Requirements 7.11 and 7.12).

Each process loads exactly one settings class, so a container only needs the
secrets it is given (Requirement 5.9):

- ``ApiSettings``: the API service. Connects to the content database as ``cairn_app``.
- ``WorkerSettings``: the DBOS worker. Connects to the DBOS database as ``cairn_dbos``.
- ``CombinedSettings``: the combined process (API and DBOS jobs in one process, free tier).
- ``AdminSettings``: ``cairn-db`` only (bootstrap and migrate).
- ``EmulationSettings``: ``cairn-db emulate-managed-admin`` only (local Neon emulation).
- ``WorkersAiSettings``, ``ModelSettings``, ``LlmSettings``: provider selection for Phase 1
  services (embedder, reranker, answer and judge models, Workers AI access).

Values come from the process environment first, then from ``.env`` in the
working directory (host-side tools). Inside containers there is no ``.env`` and
compose supplies the environment. Secrets are ``SecretStr``, so ``repr`` and
logs show ``**********``.

``load_settings`` is the one entry point for services. On invalid or missing
configuration it prints the offending keys (never their values) to stderr and
exits with status 2.
"""

from __future__ import annotations

import sys
from typing import Any, Literal, Self

from pydantic import (
    AliasChoices,
    AnyHttpUrl,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic.fields import FieldInfo
from pydantic_core import InitErrorDetails, PydanticCustomError
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic_settings.exceptions import SettingsError

from cairn_core.model_family import FAKE_PROVIDER, UnknownModelFamily, model_family

CONFIG_EXIT_STATUS = 2
"""Exit status for invalid or missing configuration (Requirement 7.11)."""

SslMode = Literal["disable", "prefer", "require", "verify-ca", "verify-full"]
"""libpq ``sslmode`` values Cairn accepts (free-tier FT-14, Requirement 4.5)."""

AdminModeSetting = Literal["auto", "superuser", "managed"]
"""``CAIRN_DB_ADMIN_MODE``: how ``cairn-db`` bootstraps (free-tier design §2 and §6)."""

ADMIN_PASSWORD_KEYS = ("CAIRN_DB_ADMIN_PASSWORD", "POSTGRES_PASSWORD")
"""Where the admin password is read from, in order (Requirement 4.2)."""


class CommonSettings(BaseSettings):
    """Settings shared by every process."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_ignore_empty=True,  # an empty variable counts as unset, never as a value
        extra="ignore",  # .env also holds keys that belong to other processes
        frozen=True,
    )

    env: Literal["local", "ci", "demo"] = Field("local", alias="CAIRN_ENV")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field("INFO", alias="CAIRN_LOG_LEVEL")
    db_host: str = Field("127.0.0.1", alias="CAIRN_DB_HOST", min_length=1)
    db_port: int = Field(5432, alias="CAIRN_DB_PORT", ge=1, le=65535)
    db_name: str = Field("cairn", alias="CAIRN_DB_NAME", min_length=1)
    db_timeout_s: float = Field(2.0, alias="CAIRN_DB_TIMEOUT_S", gt=0, le=30)
    # TLS for every connection (FT-14). `prefer` suits the local db; Neon needs
    # `verify-full` with a root store (FV11: an explicit CA bundle path in the image).
    db_sslmode: SslMode = Field("prefer", alias="CAIRN_DB_SSLMODE")
    db_sslrootcert: str | None = Field(None, alias="CAIRN_DB_SSLROOTCERT", min_length=1)
    otel_traces_exporter: Literal["none", "otlp", "console"] = Field(
        "none", alias="OTEL_TRACES_EXPORTER"
    )
    otel_endpoint: AnyHttpUrl | None = Field(None, alias="OTEL_EXPORTER_OTLP_ENDPOINT")


def libpq_tls(settings: CommonSettings) -> dict[str, Any]:
    """libpq TLS keywords for ``psycopg.connect``: ``sslmode`` always, ``sslrootcert`` when set.

    Every connection Cairn opens passes these (Requirement 4.4), so one setting moves the
    probe, ``cairn-db``, the worker, the DBOS URL and the test fixtures together.
    """
    tls: dict[str, Any] = {"sslmode": settings.db_sslmode}
    if settings.db_sslrootcert is not None:
        tls["sslrootcert"] = settings.db_sslrootcert
    return tls


STRIP_ACL_ENV = "CAIRN_TEST_STRIP_ACL"
"""A test-only switch that removes the L2 predicate from registered queries (task 1.21). No
service may run with it set, so the stripped build cannot reach a deployment."""


def _refuse_strip_acl() -> None:
    import os

    if os.environ.get(STRIP_ACL_ENV):
        raise _config_error("Settings", [(STRIP_ACL_ENV, "forbidden_in_services")])


class ApiSettings(CommonSettings):
    """The API service. The password is required; there is no default."""

    app_db_user: str = Field("cairn_app", alias="CAIRN_APP_DB_USER", min_length=1)
    app_db_password: SecretStr = Field(alias="CAIRN_APP_DB_PASSWORD", min_length=1)
    # Shared secret with the Pages Function (FT-16). Unset or empty turns the check off;
    # a shorter value than 32 characters is refused at startup (Requirement 9.6).
    proxy_token: SecretStr | None = Field(None, alias="CAIRN_PROXY_TOKEN", min_length=32)

    @model_validator(mode="after")
    def _no_strip_acl(self) -> Self:
        _refuse_strip_acl()
        return self


class WorkerSettings(CommonSettings):
    """The DBOS worker. Its database is ``dbos_db_name``, not ``db_name``."""

    dbos_db_name: str = Field("cairn_dbos", alias="CAIRN_DBOS_DB_NAME", min_length=1)
    dbos_db_password: SecretStr = Field(alias="CAIRN_DBOS_DB_PASSWORD", min_length=1)
    heartbeat_cron: str = Field("*/30 * * * * *", alias="CAIRN_HEARTBEAT_CRON")

    @model_validator(mode="after")
    def _no_strip_acl(self) -> Self:
        _refuse_strip_acl()
        return self

    @field_validator("heartbeat_cron")
    @classmethod
    def _cron_has_five_or_six_fields(cls, value: str) -> str:
        # DBOS accepts a standard five-field cron with an optional leading seconds field.
        if len(value.split()) not in (5, 6):
            # Never echo the value; the error code and key are enough.
            raise ValueError("expected 5 or 6 whitespace-separated cron fields")
        return value


COMBINED_HEARTBEAT_CRON = "0 */5 * * * *"
"""Heartbeat of the combined process: every five minutes (FT-12). Six fields, seconds first."""


class CombinedSettings(ApiSettings, WorkerSettings):
    """The combined process: the API and the DBOS worker in one container (FT-10).

    It holds the ``cairn_app`` and ``cairn_dbos`` passwords and nothing else (FX2). The heartbeat
    default differs from the worker's: a tick every five minutes, because a faster schedule only
    spends Neon compute while Render has the service awake (FT-12).
    """

    port: int = Field(10000, alias="PORT", ge=1, le=65535)
    jobs_retry_max_s: float = Field(60.0, alias="CAIRN_JOBS_RETRY_MAX_S", gt=0, le=600)
    heartbeat_cron: str = Field(COMBINED_HEARTBEAT_CRON, alias="CAIRN_HEARTBEAT_CRON")


class AdminConnectionSettings(CommonSettings):
    """How ``cairn-db`` reaches the admin role: user, password and database.
    The admin password is optional here: commands that do not bootstrap (``status``,
    ``migrate``) connect as the owner role.
    The admin is the role bootstrap connects as: the image superuser ``postgres`` locally,
    ``neondb_owner`` on Neon, ``cairn_admin`` in the free-tier emulation (design §2).
    """

    admin_user: str = Field("postgres", alias="CAIRN_DB_ADMIN_USER", min_length=1)
    # CAIRN_DB_ADMIN_PASSWORD first, then the image's POSTGRES_PASSWORD (Requirement 4.2).
    # An empty value counts as unset, so the fallback also applies to an empty first key.
    # The mypy plugin cannot read AliasChoices as a static alias and flags it (a false
    # positive: the field has a default). Unit tests pin the two keys and their order.
    admin_password: SecretStr | None = Field(  # type: ignore[pydantic-alias]
        None, validation_alias=AliasChoices(*ADMIN_PASSWORD_KEYS)
    )
    admin_database: str = Field("postgres", alias="CAIRN_DB_ADMIN_DATABASE", min_length=1)


class AdminSettings(AdminConnectionSettings):
    """``cairn-db bootstrap``, ``migrate``, ``init`` and ``status``."""

    admin_mode: AdminModeSetting = Field("auto", alias="CAIRN_DB_ADMIN_MODE")
    owner_db_password: SecretStr = Field(alias="CAIRN_OWNER_DB_PASSWORD", min_length=1)
    app_db_password: SecretStr = Field(alias="CAIRN_APP_DB_PASSWORD", min_length=1)
    dbos_db_password: SecretStr = Field(alias="CAIRN_DBOS_DB_PASSWORD", min_length=1)
    # Phase 1 (PD1-04): the content writer. Optional so Phase 0 callers keep working; when set,
    # bootstrap creates `cairn_ingest` and grants it CONNECT on the content database.
    ingest_db_password: SecretStr | None = Field(
        None, alias="CAIRN_INGEST_DB_PASSWORD", min_length=1
    )
    # Phase 3 (plan 3.4): the edge receiver's insert-only login. Optional like the ingest one.
    edge_db_password: SecretStr | None = Field(None, alias="CAIRN_EDGE_DB_PASSWORD", min_length=1)


class EmulationSettings(AdminConnectionSettings):
    """``cairn-db emulate-managed-admin`` only (free-tier FT-08). Runs as the image superuser,
    so it holds the superuser password and the new ``cairn_admin`` password and none of the
    three Cairn role passwords."""

    managed_admin_password: SecretStr = Field(alias="CAIRN_MANAGED_ADMIN_PASSWORD", min_length=1)


# --- Provider settings (free-tier FT-20 to FT-24, Requirements 16.6 to 16.9) --------------
# Loaded by Phase 1 services, not by any Phase 0 process. Code defaults are the production
# defaults (Workers AI); `.env.example` selects the fakes so local runs and CI need no token.

WorkersAiProvider = Literal["workers_ai", "bedrock", "fake"]
LlmProviderName = Literal["workers_ai", "bedrock", "anthropic", "openai", "fake"]

_CLOUDFLARE_CREDENTIAL = "CLOUDFLARE_API_TOKEN"
_ANTHROPIC_CREDENTIAL = "ANTHROPIC_API_KEY"
_OPENAI_CREDENTIAL = "OPENAI_API_KEY"
_LLM_CREDENTIAL_KEYS: dict[str, str] = {
    "workers_ai": _CLOUDFLARE_CREDENTIAL,
    "anthropic": _ANTHROPIC_CREDENTIAL,
    "openai": _OPENAI_CREDENTIAL,
}
"""Credential key per non-fake LLM provider. ``bedrock`` has none: it signs requests with the AWS
credential chain (on EC2, the instance role), so no secret is stored or configured."""


class ProviderSettings(BaseSettings):
    """Base of the provider settings: no database fields, same source rules as every process."""

    # Literal, not shared with CommonSettings: the mypy plugin only reads a literal config.
    model_config = SettingsConfigDict(
        env_file=".env",
        env_ignore_empty=True,
        extra="ignore",
        frozen=True,
    )


def _config_error(title: str, problems: list[tuple[str, str]]) -> ValidationError:
    """A ``ValidationError`` whose lines are located at environment keys.

    ``load_settings`` reports ``KEY (code)`` for each line and drops the input and the message,
    so neither a value nor any secret can reach the output. ``problems`` is ``(key, code)``.
    """
    details = [
        InitErrorDetails(
            type=PydanticCustomError(code, "invalid provider configuration"),
            loc=(key,),
            input=None,
        )
        for key, code in problems
    ]
    return ValidationError.from_exception_data(title, details)


class WorkersAiSettings(ProviderSettings):
    """Workers AI REST access: credentials, endpoint, budget and retry bounds (FT-20 to FT-22)."""

    account_id: str | None = Field(None, alias="CLOUDFLARE_ACCOUNT_ID", pattern=r"^[0-9a-f]{32}$")
    api_token: SecretStr | None = Field(None, alias="CLOUDFLARE_API_TOKEN")
    base_url: AnyHttpUrl = Field(
        AnyHttpUrl("https://api.cloudflare.com/client/v4"), alias="CAIRN_WORKERS_AI_BASE_URL"
    )
    daily_neurons: int = Field(9000, alias="CAIRN_WORKERS_AI_DAILY_NEURONS", ge=0)
    budget_store: Literal["memory", "postgres"] = Field(
        "memory", alias="CAIRN_WORKERS_AI_BUDGET_STORE"
    )
    connect_timeout_s: float = Field(5.0, alias="CAIRN_WORKERS_AI_CONNECT_TIMEOUT_S", gt=0, le=30)
    read_timeout_s: float = Field(30.0, alias="CAIRN_WORKERS_AI_READ_TIMEOUT_S", gt=0, le=120)
    max_attempts: int = Field(3, alias="CAIRN_WORKERS_AI_MAX_ATTEMPTS", ge=1, le=5)


class BedrockSettings(ProviderSettings):
    """Amazon Bedrock access: region, daily dollar cap and call bounds.

    No credential field: boto3 uses the AWS credential chain (the instance role on EC2). The daily
    cap is counted in micro-dollars (1,000,000 = 1 USD) in the same ``cairn_meta.provider_usage``
    table as the Workers AI neurons, under the provider name ``bedrock``.
    """

    bedrock_region: str = Field(
        "ap-south-1", alias="CAIRN_BEDROCK_REGION", pattern=r"^[a-z]{2}-[a-z]+-\d$"
    )
    bedrock_daily_usd: float = Field(1.0, alias="CAIRN_BEDROCK_DAILY_USD", ge=0, le=1000)
    bedrock_budget_store: Literal["memory", "postgres"] = Field(
        "memory", alias="CAIRN_BEDROCK_BUDGET_STORE"
    )
    bedrock_connect_timeout_s: float = Field(
        5.0, alias="CAIRN_BEDROCK_CONNECT_TIMEOUT_S", gt=0, le=30
    )
    bedrock_read_timeout_s: float = Field(60.0, alias="CAIRN_BEDROCK_READ_TIMEOUT_S", gt=0, le=180)
    bedrock_max_attempts: int = Field(3, alias="CAIRN_BEDROCK_MAX_ATTEMPTS", ge=1, le=5)
    bedrock_reasoning_effort: Literal["", "low", "medium", "high"] = Field(
        "low", alias="CAIRN_BEDROCK_REASONING_EFFORT"
    )

    @property
    def bedrock_daily_micro_usd(self) -> int:
        return round(self.bedrock_daily_usd * 1_000_000)


class ModelSettings(ProviderSettings):
    """Embedder and reranker selection (FT-24). Workers AI needs ``CLOUDFLARE_API_TOKEN``; Bedrock
    needs AWS credentials from the environment (the instance role)."""

    embedder: WorkersAiProvider = Field("workers_ai", alias="CAIRN_EMBEDDER")
    embed_model: str = Field("@cf/baai/bge-small-en-v1.5", alias="CAIRN_EMBED_MODEL", min_length=1)
    embed_dim: int = Field(384, alias="CAIRN_EMBED_DIM", ge=8, le=4096)
    embed_query_prefix: str = Field(
        "Represent this sentence for searching relevant passages: ",
        alias="CAIRN_EMBED_QUERY_PREFIX",
    )
    reranker: Literal["workers_ai", "bedrock", "fake", "none"] = Field(
        "workers_ai", alias="CAIRN_RERANKER"
    )
    """``bedrock`` is an LLM reranker (``CAIRN_RERANK_MODEL`` is then a Bedrock model id): Bedrock's
    own rerank models are not offered in ap-south-1."""
    rerank_model: str = Field(
        "@cf/baai/bge-reranker-base", alias="CAIRN_RERANK_MODEL", min_length=1
    )
    # The credential the Workers AI selections above need; the client reads it from
    # WorkersAiSettings. Held here only so the missing-credential rule can run (Requirement 16.8).
    cloudflare_api_token: SecretStr | None = Field(None, alias="CLOUDFLARE_API_TOKEN")

    @model_validator(mode="after")
    def _workers_ai_needs_its_token(self) -> Self:
        if "workers_ai" in (self.embedder, self.reranker) and self.cloudflare_api_token is None:
            raise _config_error(
                type(self).__name__, [(_CLOUDFLARE_CREDENTIAL, "credential_missing")]
            )
        return self


class LlmSettings(ProviderSettings):
    """Answer and judge model selection (FT-24, plan D16).

    A non-fake provider needs its credential, and when neither side is fake the two models
    must come from different families. Errors name keys and codes, never values.
    """

    answer_provider: LlmProviderName = Field("workers_ai", alias="CAIRN_ANSWER_PROVIDER")
    answer_model: str = Field(
        "@cf/google/gemma-4-26b-a4b-it", alias="CAIRN_ANSWER_MODEL", min_length=1
    )
    judge_provider: LlmProviderName = Field("workers_ai", alias="CAIRN_JUDGE_PROVIDER")
    judge_model: str = Field("@cf/zai-org/glm-4.7-flash", alias="CAIRN_JUDGE_MODEL", min_length=1)
    cloudflare_api_token: SecretStr | None = Field(None, alias="CLOUDFLARE_API_TOKEN")
    anthropic_api_key: SecretStr | None = Field(None, alias="ANTHROPIC_API_KEY")
    openai_api_key: SecretStr | None = Field(None, alias="OPENAI_API_KEY")

    @model_validator(mode="after")
    def _check_credentials_and_families(self) -> Self:
        held = {
            _CLOUDFLARE_CREDENTIAL: self.cloudflare_api_token,
            _ANTHROPIC_CREDENTIAL: self.anthropic_api_key,
            _OPENAI_CREDENTIAL: self.openai_api_key,
        }
        problems: list[tuple[str, str]] = []
        needed = {
            _LLM_CREDENTIAL_KEYS[p]
            for p in (self.answer_provider, self.judge_provider)
            if p in _LLM_CREDENTIAL_KEYS
        }
        problems.extend((key, "credential_missing") for key in sorted(needed) if held[key] is None)
        if FAKE_PROVIDER not in (self.answer_provider, self.judge_provider):
            try:
                answer = model_family(self.answer_provider, self.answer_model)
            except UnknownModelFamily:
                problems.append(("CAIRN_ANSWER_MODEL", "unknown_model_family"))
                answer = None
            try:
                judge = model_family(self.judge_provider, self.judge_model)
            except UnknownModelFamily:
                problems.append(("CAIRN_JUDGE_MODEL", "unknown_model_family"))
                judge = None
            if answer is not None and answer == judge:
                problems.append(("CAIRN_ANSWER_MODEL", "same_model_family"))
                problems.append(("CAIRN_JUDGE_MODEL", "same_model_family"))
        if problems:
            raise _config_error(type(self).__name__, problems)
        return self


# --- Phase 1 settings (evidence search; design §2) -----------------------------------------
# Loaded by the search API, the importer and the embedding worker. Kept in separate classes, not
# folded into ApiSettings, so the Phase 0 processes and their tests do not need these keys.


class DemoSettings(ProviderSettings):
    """Demo persona mode and its per-client limits (PD1-26)."""

    demo_personas: Literal["off", "on"] = Field("off", alias="CAIRN_DEMO_PERSONAS")
    login_per_min: int = Field(10, alias="CAIRN_DEMO_LOGIN_PER_MIN", ge=1, le=100)
    search_per_10min: int = Field(30, alias="CAIRN_DEMO_SEARCH_PER_10MIN", ge=1, le=600)
    answer_per_10min: int = Field(10, alias="CAIRN_DEMO_ANSWER_PER_10MIN", ge=1, le=200)
    answers_per_day: int = Field(100, alias="CAIRN_DEMO_ANSWERS_PER_DAY", ge=1, le=1000)


class ModelCacheSettings(ProviderSettings):
    """Laptop tools only (importer, eval, S3): cache of embeddings and rerank scores (PD1-27)."""

    cache_mode: Literal["off", "read_write", "read_only"] = Field("off", alias="CAIRN_MODEL_CACHE")
    cache_path: str = Field("var/cache/models/cache.sqlite3", alias="CAIRN_MODEL_CACHE_PATH")


class SearchSettings(ModelSettings, LlmSettings, WorkersAiSettings, BedrockSettings, DemoSettings):  # type: ignore[no-redef]
    """Everything the search API needs besides its database login (PD1-02, PD1-11, PD1-15, PD1-16)."""  # noqa: E501

    env: Literal["local", "ci", "demo"] = Field("local", alias="CAIRN_ENV")
    session_secret: SecretStr = Field(alias="CAIRN_SESSION_SECRET", min_length=32)
    audit_hash_key: SecretStr = Field(alias="CAIRN_AUDIT_HASH_KEY", min_length=32)
    proxy_token: SecretStr | None = Field(None, alias="CAIRN_PROXY_TOKEN", min_length=32)
    access_mode: Literal["rls_first", "definer"] = Field("rls_first", alias="CAIRN_ACCESS_MODE")
    vector_strategy: str = Field(
        "iterative_strict_ef100", alias="CAIRN_VECTOR_STRATEGY", min_length=1
    )
    pool_max: int = Field(4, alias="CAIRN_DB_POOL_MAX", ge=1, le=16)
    rerank_min_score: float | None = Field(0.0, alias="CAIRN_RERANK_MIN_SCORE")
    model_concurrency: int = Field(2, alias="CAIRN_MODEL_CONCURRENCY", ge=1, le=8)
    max_output_tokens: int = Field(600, alias="CAIRN_LLM_OUTPUT_LIMIT", ge=64, le=2000)
    llm_timeout_s: float = Field(30.0, alias="CAIRN_LLM_TIMEOUT_S", gt=0, le=120)
    cost_budget_neurons: int | None = Field(None, alias="CAIRN_COST_BUDGET_NEURONS", gt=0)
    cost_budget_usd: float | None = Field(None, alias="CAIRN_COST_BUDGET_USD", gt=0)
    # Phase 3 live sync (plan 3.4, 3.9, 3.28)
    github_webhook_secret: SecretStr | None = Field(
        None, alias="CAIRN_GITHUB_WEBHOOK_SECRET", min_length=16
    )
    agent_calls_per_day: int = Field(500, alias="CAIRN_AGENT_CALLS_PER_DAY", ge=1, le=100000)
    sync_wait_s: float = Field(20.0, alias="CAIRN_SYNC_WAIT_S", ge=0, le=60)
    github_org: str | None = Field(None, alias="CAIRN_GITHUB_ORG", pattern=r"^[A-Za-z0-9-]{1,39}$")
    answer_cache_size: int = Field(128, alias="CAIRN_ANSWER_CACHE_SIZE", ge=0, le=1000)
    answer_cache_ttl_s: int = Field(600, alias="CAIRN_ANSWER_CACHE_TTL_S", ge=1, le=86400)

    @model_validator(mode="after")
    def _demo_personas_need_demo_env_and_proxy(self) -> Self:
        if self.demo_personas == "on":
            problems: list[tuple[str, str]] = []
            if self.env != "demo":
                problems.append(("CAIRN_ENV", "demo_personas_need_demo_env"))
            if self.proxy_token is None:
                problems.append(("CAIRN_PROXY_TOKEN", "demo_personas_need_proxy_token"))
            if problems:
                raise _config_error(type(self).__name__, problems)
        return self

    @property
    def dev_login_enabled(self) -> bool:
        """Persona login exists locally, and in the demo only with demo persona mode (PD1-15)."""
        return self.env == "local" or (self.env == "demo" and self.demo_personas == "on")


class IngestSettings(  # type: ignore[no-redef]
    CommonSettings, ModelSettings, WorkersAiSettings, BedrockSettings, ModelCacheSettings
):
    """``cairn-import`` and the embedding worker: connects as ``cairn_ingest`` (PD1-04)."""

    ingest_db_user: str = Field("cairn_ingest", alias="CAIRN_INGEST_DB_USER", min_length=1)
    ingest_db_password: SecretStr = Field(alias="CAIRN_INGEST_DB_PASSWORD", min_length=1)
    size_guard_mb: int = Field(800, alias="CAIRN_DB_SIZE_GUARD_MB", ge=16, le=1024)
    outbox_poll_s: float = Field(2.0, alias="CAIRN_OUTBOX_POLL_S", gt=0, le=60)
    # Phase 3 live sync (plan 3.3, 3.7, 3.10): off unless an organisation is configured.
    github_org: str | None = Field(None, alias="CAIRN_GITHUB_ORG", pattern=r"^[A-Za-z0-9-]{1,39}$")
    github_app_id: str | None = Field(None, alias="CAIRN_GITHUB_APP_ID", pattern=r"^\d{1,12}$")
    github_installation_id: str | None = Field(
        None, alias="CAIRN_GITHUB_INSTALLATION_ID", pattern=r"^\d{1,12}$"
    )
    github_private_key: SecretStr | None = Field(
        None, alias="CAIRN_GITHUB_PRIVATE_KEY", min_length=64
    )
    github_user_map: str | None = Field(
        None, alias="CAIRN_GITHUB_USER_MAP"
    )  # path to a login->person JSON
    sync_poll_s: float = Field(5.0, alias="CAIRN_SYNC_POLL_S", gt=0, le=300)
    reconcile_interval_s: float = Field(900.0, alias="CAIRN_RECONCILE_INTERVAL_S", ge=30, le=86400)


def field_env_keys(field: FieldInfo) -> tuple[str, ...]:
    """Environment keys one settings field reads: its alias, or each of its alias choices."""
    if field.alias is not None:
        return (field.alias,)
    if isinstance(field.validation_alias, AliasChoices):
        return tuple(c for c in field.validation_alias.choices if isinstance(c, str))
    if isinstance(field.validation_alias, str):
        return (field.validation_alias,)
    return ()


def env_keys(settings_cls: type[BaseSettings]) -> set[str]:
    """Every environment key ``settings_cls`` reads (tests use it to isolate the environment)."""
    return {key for field in settings_cls.model_fields.values() for key in field_env_keys(field)}


def invalid_keys(error: ValidationError, settings_cls: type[BaseSettings]) -> list[str]:
    """Names of the environment variables that failed validation, with the error
    code of each, e.g. ``CAIRN_DB_PORT (int_parsing)``.

    Only keys and pydantic error codes are reported. The input value and the
    human-readable message are dropped on purpose: they can echo the value.
    """
    alias_of: dict[str, str] = {}
    for name, field in settings_cls.model_fields.items():
        keys = field_env_keys(field)
        alias_of[name] = " or ".join(keys) if keys else name
        for key in keys:
            alias_of[key] = key
    found: set[str] = set()
    for err in error.errors(include_input=False, include_url=False, include_context=False):
        loc = err["loc"]
        # The location is the alias for aliased fields; map field names for safety.
        key = str(loc[0]) if loc else "<settings>"
        found.add(f"{alias_of.get(key, key)} ({err['type']})")
    return sorted(found)


def load_settings[S: BaseSettings](settings_cls: type[S]) -> S:
    """Load ``settings_cls`` from the environment and ``.env``.

    Exits the process with status 2 if any key is missing or invalid. The message
    on stderr names each key and its error code and never prints a value.
    """
    try:
        return settings_cls()
    except ValidationError as exc:
        keys = ", ".join(invalid_keys(exc, settings_cls))
        print(f"invalid configuration: {keys}", file=sys.stderr)
    except SettingsError:
        # Raised while reading a source (for example an unreadable .env). The
        # exception text can quote a value, so only the class is reported.
        print(
            f"invalid configuration: cannot read the settings sources for {settings_cls.__name__}",
            file=sys.stderr,
        )
    raise SystemExit(CONFIG_EXIT_STATUS)
