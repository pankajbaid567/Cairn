"""Unit tests for ``python -m cairn_api`` argument handling and its uvicorn call."""

from __future__ import annotations

from typing import Any

import pytest

import cairn_api.__main__ as serve


def test_defaults_bind_loopback_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CAIRN_API_HOST", raising=False)
    monkeypatch.delenv("CAIRN_API_PORT", raising=False)
    args = serve.parse_args([])
    assert (args.host, args.port) == ("127.0.0.1", 8000)


def test_environment_and_flags_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAIRN_API_HOST", "0.0.0.0")  # noqa: S104
    monkeypatch.setenv("CAIRN_API_PORT", "9000")
    args = serve.parse_args([])
    assert (args.host, args.port) == ("0.0.0.0", 9000)  # noqa: S104
    args = serve.parse_args(["--host", "10.0.0.5", "--port", "8123"])
    assert (args.host, args.port) == ("10.0.0.5", 8123)


def test_main_runs_the_factory_with_uvicorn_logging_and_access_log_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    monkeypatch.setattr(serve.uvicorn, "run", lambda *a, **kw: calls.append((a, kw)))
    serve.main(["--port", "8001"])
    [(args, kwargs)] = calls
    assert args == ("cairn_api.main:create_app",)
    assert kwargs["factory"] is True
    assert kwargs["log_config"] is None
    assert kwargs["access_log"] is False
    assert kwargs["port"] == 8001
