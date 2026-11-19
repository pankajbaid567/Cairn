"""The Cairn API app (design §7).

``create_app`` is the uvicorn factory (``uvicorn cairn_api.main:create_app --factory``). It
loads settings (exit status 2 on bad configuration, before any port is bound), installs JSON
logging and tracing, instruments psycopg, and builds the app. ``build_app`` does the rest with
everything injected, so tests need no environment and no database.

The app opens no database connection at startup and holds none between requests: ``/healthz``
never touches the database, and each ``/readyz`` call probes through one short-lived connection.
"""

from __future__ import annotations

import contextlib
import functools
import logging
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from starlette.types import Lifespan

from cairn_api import proxy_token
from cairn_api.accounts import install as install_accounts
from cairn_api.governance import install as install_governance
from cairn_api.people import install as install_people
from cairn_api.reports import install as install_reports
from cairn_api.search import SearchRuntime, build_runtime, runtime_lifespan
from cairn_api.search import install as install_search
from cairn_core.config import ApiSettings, SearchSettings, load_settings
from cairn_core.health import (
    DbUnavailable,
    JobsProbe,
    ProbeFn,
    ProbeResult,
    evaluate_readiness,
    probe_database,
)
from cairn_core.migrate import bundled_migrations, head
from cairn_core.telemetry import configure_logging, configure_tracing

SERVICE_NAME = "cairn-api"

access_log = logging.getLogger("cairn.api.access")
log = logging.getLogger(__name__)

QUIET_PATHS = frozenset({"/healthz", "/readyz"})
"""Probe endpoints are called every few seconds by the compose healthcheck; they log at DEBUG."""


def build_app(
    settings: ApiSettings,
    *,
    probe: ProbeFn | None = None,
    expected_head: int | None = None,
    tracer_provider: TracerProvider | None = None,
    jobs: JobsProbe | None = None,
    lifespan: Lifespan[FastAPI] | None = None,
    search: SearchRuntime | None = None,
) -> FastAPI:
    """Build the app.

    ``probe`` replaces the database probe, ``expected_head`` replaces the bundled migration
    head, and ``tracer_provider`` is used for the server spans instead of the global provider.
    ``search`` adds the Phase 1 routes (login, ``/me``, ``/search``, ``/answer``) and opens its
    database pool in the lifespan; without it the app behaves exactly as in Phase 0.
    ``jobs`` and ``lifespan`` are for the combined process (FT-10, FT-11): ``jobs`` adds the
    ``jobs`` check to ``/readyz`` and ``lifespan`` starts and stops the jobs runner. The split
    ``api`` service passes neither, which keeps the Phase 0 behavior.
    """
    run_probe: ProbeFn = probe or functools.partial(probe_database, settings)
    head_version = head(bundled_migrations()) if expected_head is None else expected_head

    # No interactive docs or schema endpoint in Phase 0: nothing here is authenticated, so
    # the only routes are the two probes.
    if search is not None:
        lifespan = _compose_lifespan(lifespan, search)
    app = FastAPI(
        title="Cairn API",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    # Installed first, so it sits inside the access log: refused requests are still logged.
    proxy_token.install(app, settings.proxy_token)

    @app.middleware("http")
    async def access_log_middleware(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        started = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            level = logging.DEBUG if request.url.path in QUIET_PATHS else logging.INFO
            # trace_id and span_id are added by the JSON formatter from the current span.
            # The path is logged without the query string.
            access_log.log(
                level,
                "http.request",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "status": status,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                },
            )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        result: ProbeResult
        try:
            result = await run_probe()
        except Exception as exc:  # a broken probe must still answer with a fixed code
            log.warning("readyz.probe_crashed", extra={"error_class": type(exc).__name__})
            result = DbUnavailable("probe_failed")
        status_code, report = evaluate_readiness(
            result, head_version, jobs() if jobs is not None else None
        )
        return JSONResponse(
            report.to_body(), status_code=status_code, headers={"Cache-Control": "no-store"}
        )

    if search is not None:
        app.state.runtime = search  # tests and tools reach the runtime here
        install_search(app, search)
        install_accounts(app, search)
        install_people(app, search)
        install_reports(app, search)
        install_governance(app, search)

    FastAPIInstrumentor.instrument_app(app, tracer_provider=tracer_provider)
    return app


def _compose_lifespan(base: Lifespan[FastAPI] | None, search: SearchRuntime) -> Lifespan[FastAPI]:
    """Run the search runtime's lifespan inside the caller's (the combined jobs runner)."""

    @contextlib.asynccontextmanager
    async def combined(app: FastAPI) -> AsyncIterator[None]:
        async with contextlib.AsyncExitStack() as stack:
            await stack.enter_async_context(runtime_lifespan(search))
            if base is not None:
                await stack.enter_async_context(base(app))
            yield

    return combined


def maybe_search_runtime(settings: ApiSettings) -> SearchRuntime | None:
    """The Phase 1 runtime when ``CAIRN_SESSION_SECRET`` is configured, otherwise ``None``.

    Phase 0 deployments (and their tests) have no session secret and keep serving only the
    probes. A configured but invalid Phase 1 setting exits with status 2.
    """
    if not os.environ.get("CAIRN_SESSION_SECRET") and not _dotenv_has("CAIRN_SESSION_SECRET"):
        return None
    from cairn_retrieval.cloudflare.budget import PostgresUsageStore
    from cairn_retrieval.runtime import build_providers, uses_bedrock, uses_workers_ai

    search = load_settings(SearchSettings)
    # One Postgres-backed usage store counts both the Workers AI neurons and the Bedrock
    # micro-dollars (provider names "workers_ai" and "bedrock"), so a restart keeps the day's total.
    durable = (uses_workers_ai(search) and search.budget_store == "postgres") or (
        uses_bedrock(search) and search.bedrock_budget_store == "postgres"
    )
    store = PostgresUsageStore(settings) if durable else None
    providers = build_providers(
        search, store if uses_workers_ai(search) or uses_bedrock(search) else None
    )
    return build_runtime(settings, search, providers)


def _dotenv_has(key: str) -> bool:
    try:
        with open(".env", encoding="utf-8") as fh:
            return any(line.startswith(f"{key}=") and line.strip() != f"{key}=" for line in fh)
    except OSError:
        return False


def create_app() -> FastAPI:
    """Uvicorn factory. Exits with status 2 on invalid configuration (Requirement 7.11)."""
    settings = load_settings(ApiSettings)
    configure_logging(SERVICE_NAME, settings.log_level)
    provider = configure_tracing(
        SERVICE_NAME,
        settings.otel_traces_exporter,
        str(settings.otel_endpoint) if settings.otel_endpoint else None,
        environment=settings.env,
    )
    # Client spans for every database query (Requirement 9.6). Global patch of psycopg's
    # connect functions, applied after the provider exists. capture_parameters stays off.
    PsycopgInstrumentor().instrument(tracer_provider=provider)
    return build_app(settings, tracer_provider=provider, search=maybe_search_runtime(settings))
