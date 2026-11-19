# Shared image for the Python services: db-init (cairn-core), api, worker and the combined
# process (cairn-combined, the default, which is what Render builds without build arguments).
#
#   docker build -f infra/docker/python-app.Dockerfile --build-arg PACKAGE=cairn-core .
#
# PACKAGE is the uv workspace member to install. Its locked, non-dev dependency
# closure goes into /app/.venv and nothing else does (no dev tools, no tests, no
# uv). The build context is the repo root; .dockerignore keeps .env, .venv, var
# and caches out of it.
#
# Every base image is pinned by tag and digest (Requirement 21.3), resolved
# 27 Oct 2026. Both are multi-arch index digests.

FROM ghcr.io/astral-sh/uv:0.11.25@sha256:1e3808aa9023d0980e7c15b1fa7c1ac16ff35925780cf5c459858b2d693f01a9 AS uv

FROM python:3.12.16-slim@sha256:962f801b08c870ac2a51b72bf02dae38c9a4162319e5e882ec244e198188d54e AS builder

COPY --from=uv /uv /usr/local/bin/uv

# Use the image's own CPython (the workspace config prefers uv-managed ones), never
# download one, keep the venv at a fixed path, and copy instead of hard-linking out
# of the cache mount.
ENV UV_PYTHON_PREFERENCE=only-system \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

ARG PACKAGE=cairn-combined

# Layer 1: third-party dependencies. Only the manifests are needed to resolve the
# workspace, so a source edit does not invalidate this layer.
COPY pyproject.toml uv.lock ./
COPY apps/api/pyproject.toml apps/api/pyproject.toml
COPY apps/combined/pyproject.toml apps/combined/pyproject.toml
COPY apps/mcp/pyproject.toml apps/mcp/pyproject.toml
COPY apps/worker/pyproject.toml apps/worker/pyproject.toml
COPY packages/connectors/pyproject.toml packages/connectors/pyproject.toml
COPY packages/core/pyproject.toml packages/core/pyproject.toml
COPY packages/eval/pyproject.toml packages/eval/pyproject.toml
COPY packages/retrieval/pyproject.toml packages/retrieval/pyproject.toml
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-workspace --package "${PACKAGE}"

# Layer 2: the workspace members' own code, installed non-editable into the venv.
COPY apps/api/src apps/api/src
COPY apps/combined/src apps/combined/src
COPY apps/mcp/src apps/mcp/src
COPY apps/worker/src apps/worker/src
COPY packages/connectors/src packages/connectors/src
COPY packages/core/src packages/core/src
COPY packages/eval/src packages/eval/src
COPY packages/retrieval/src packages/retrieval/src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable --package "${PACKAGE}"

FROM python:3.12.16-slim@sha256:962f801b08c870ac2a51b72bf02dae38c9a4162319e5e882ec244e198188d54e AS runtime

# Non-root user with a fixed id, so compose can set `user: 10001:10001` and mount
# read-only root filesystems.
RUN groupadd --system --gid 10001 cairn \
    && useradd --system --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent \
        --shell /usr/sbin/nologin cairn

COPY --from=builder /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
USER 10001:10001
