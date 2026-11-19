"""Unit tests for cairn_api.healthcheck (stdlib only, against a local one-route server)."""

from __future__ import annotations

import socket
import threading
from collections.abc import Iterator
from contextlib import closing
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from cairn_api.healthcheck import DEFAULT_URL, HEADER_NAME, check, main

TOKEN = "t" * 16 + "0123456789abcdef"


def serve(status: int) -> tuple[HTTPServer, str]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}/readyz"


@pytest.fixture
def ok_server() -> Iterator[str]:
    server, url = serve(200)
    yield url
    server.shutdown()
    server.server_close()


@pytest.fixture
def unready_server() -> Iterator[str]:
    server, url = serve(503)
    yield url
    server.shutdown()
    server.server_close()


def test_default_url_targets_the_container_port() -> None:
    assert DEFAULT_URL == "http://127.0.0.1:8000/readyz"


def test_check_true_on_200(ok_server: str) -> None:
    assert check(ok_server) is True


def test_check_false_on_503(unready_server: str) -> None:
    assert check(unready_server) is False


def test_check_false_when_nothing_listens() -> None:
    with closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    assert check(f"http://127.0.0.1:{port}/readyz", timeout_s=1) is False


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://127.0.0.1/", "not a url", ""])
def test_check_refuses_non_http_urls(url: str) -> None:
    assert check(url) is False


def test_main_exit_codes(
    ok_server: str, unready_server: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main([ok_server]) == 0
    assert main([unready_server]) == 1
    assert "unhealthy" in capsys.readouterr().err


def test_main_reads_the_url_from_the_environment(
    ok_server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CAIRN_API_HEALTHCHECK_URL", ok_server)
    assert main([]) == 0


# --- proxy token header (Requirement 9.5) -----------------------------------------------


@pytest.fixture
def header_recorder() -> Iterator[tuple[str, list[str | None]]]:
    """A server that answers 200 and records the proxy token header of each request."""
    seen: list[str | None] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            seen.append(self.headers.get(HEADER_NAME))
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/readyz", seen
    server.shutdown()
    server.server_close()


def test_check_sends_the_proxy_token_header(
    header_recorder: tuple[str, list[str | None]],
) -> None:
    url, seen = header_recorder
    assert check(url, proxy_token=TOKEN) is True
    assert seen == [TOKEN]


def test_check_sends_no_header_without_a_token(
    header_recorder: tuple[str, list[str | None]],
) -> None:
    url, seen = header_recorder
    assert check(url) is True
    assert check(url, proxy_token="") is True
    assert seen == [None, None]


def test_main_sends_the_header_when_the_environment_has_a_token(
    header_recorder: tuple[str, list[str | None]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    url, seen = header_recorder
    monkeypatch.setenv("CAIRN_PROXY_TOKEN", TOKEN)
    assert main([url]) == 0
    assert seen == [TOKEN]
    captured = capsys.readouterr()
    assert TOKEN not in captured.out + captured.err


def test_main_sends_no_header_when_the_token_is_unset_or_empty(
    header_recorder: tuple[str, list[str | None]], monkeypatch: pytest.MonkeyPatch
) -> None:
    url, seen = header_recorder
    monkeypatch.delenv("CAIRN_PROXY_TOKEN", raising=False)
    assert main([url]) == 0
    monkeypatch.setenv("CAIRN_PROXY_TOKEN", "")
    assert main([url]) == 0
    assert seen == [None, None]


def test_main_does_not_print_the_token_when_unhealthy(
    unready_server: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CAIRN_PROXY_TOKEN", TOKEN)
    assert main([unready_server]) == 1
    captured = capsys.readouterr()
    assert TOKEN not in captured.out + captured.err
