"""cairn-waker: paths, header rules, static files, wake and idle logic, and a real HTTP run."""

from __future__ import annotations

import http.client
import http.server
import importlib.util
import json
import threading
from pathlib import Path
from typing import Any, ClassVar

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("cairn_waker", ROOT / "deploy/aws/cairn_waker.py")
assert spec is not None
assert spec.loader is not None
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)

TOKEN = "t" * 40


# --- pure helpers --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("/api/readyz", "/readyz"),
        ("/api/search?q=why+postgres&limit=5", "/search?q=why+postgres&limit=5"),
        ("/api//evil.example/x", "/evil.example/x"),  # never becomes protocol-relative
        ("/api///", "/"),
        ("/api", None),
        ("/apix/readyz", None),
        ("/readyz", None),
        ("/api/../admin", None),
        ("/api/a/../b", None),
        ("/api/a/./b", None),
        ("/api/a\\b", None),
    ],
)
def test_upstream_path(target: str, expected: str | None) -> None:
    assert w.upstream_path(target) == expected


def test_forward_headers_drop_hop_by_hop_and_client_supplied_token_and_set_the_trio() -> None:
    out = w.forward_headers(
        [
            ("Host", "cairn.example"),
            ("Connection", "keep-alive, X-Secret-Hop"),
            ("X-Secret-Hop", "1"),
            ("X-Cairn-Proxy-Token", "client-forged"),
            ("X-Forwarded-For", "6.6.6.6"),
            ("Proxy-Foo", "x"),
            ("Content-Length", "999"),
            ("Cookie", "session=abc"),
            ("Content-Type", "application/json"),
        ],
        TOKEN,
        "203.0.113.9",
        "cairn.example",
    )
    assert out["x-cairn-proxy-token"] == TOKEN
    assert out["x-forwarded-for"] == "203.0.113.9"
    assert out["x-forwarded-proto"] == "https"
    assert out["x-forwarded-host"] == "cairn.example"
    assert out["Cookie"] == "session=abc"
    assert out["Content-Type"] == "application/json"
    lowered = {k.lower() for k in out}
    assert not lowered & {"connection", "x-secret-hop", "proxy-foo", "content-length", "host"}
    assert "client-forged" not in out.values()
    assert "6.6.6.6" not in out.values()


def test_static_file_resolves_assets_spa_routes_and_refuses_traversal(tmp_path: Path) -> None:
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<html>app</html>")
    (tmp_path / "assets" / "a.js").write_text("x")
    secret = tmp_path.parent / "secret.txt"
    secret.write_text("no")
    assert w.static_file(tmp_path, "/") == tmp_path / "index.html"
    assert w.static_file(tmp_path, "/assets/a.js") == (tmp_path / "assets/a.js").resolve()
    assert w.static_file(tmp_path, "/search") == tmp_path / "index.html"  # client-side route
    assert w.static_file(tmp_path, "/missing.js") is None
    assert w.static_file(tmp_path, "/../secret.txt") is None
    assert w.static_file(tmp_path, "/%2e%2e/secret.txt") is None  # a literal, missing file name


# --- wake and idle -------------------------------------------------------------------------


class FakeDocker:
    def __init__(self, running: bool = False, can_start: bool = True) -> None:
        self._running, self.can_start = running, can_start
        self.starts = self.stops = 0

    def running(self) -> bool:
        return self._running

    def start(self) -> bool:
        self.starts += 1
        self._running = self.can_start
        return self.can_start

    def stop(self) -> bool:
        self.stops += 1
        self._running = False
        return True


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        self.now += s


def state(docker: FakeDocker, ready: Any, clock: Clock, **kw: Any) -> Any:
    return w.AppState(docker, ready, clock=clock, sleep=clock.sleep, poll_s=1.0, **kw)


def test_first_request_starts_the_container_and_waits_until_ready() -> None:
    clock, docker = Clock(), FakeDocker()
    polls = iter([False, False, True])
    s = state(docker, lambda: next(polls), clock, start_timeout_s=30)
    assert s.ensure_running() is True
    assert docker.starts == 1
    assert s.ensure_running() is True
    assert docker.starts == 1


def test_an_app_that_never_becomes_ready_times_out_and_is_retried_later() -> None:
    clock, docker = Clock(), FakeDocker()
    s = state(docker, lambda: False, clock, start_timeout_s=10)
    assert s.ensure_running() is False
    assert clock.now >= 10  # it waited out the timeout instead of giving up early
    ok = {"v": False}
    s.ready = lambda: ok["v"]
    ok["v"] = True
    assert s.ensure_running() is True


def test_a_container_that_cannot_start_fails_fast() -> None:
    clock = Clock()
    assert state(FakeDocker(can_start=False), lambda: True, clock).ensure_running() is False


def test_concurrent_first_requests_start_the_container_once() -> None:
    docker = FakeDocker()
    s = w.AppState(docker, lambda: True, start_timeout_s=5, poll_s=0.01)
    results: list[bool] = []
    threads = [
        threading.Thread(target=lambda: results.append(s.ensure_running())) for _ in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == [True] * 8
    assert docker.starts == 1


def test_idle_stop_only_after_the_idle_period_and_activity_resets_it() -> None:
    clock, docker = Clock(), FakeDocker()
    s = state(docker, lambda: True, clock, idle_s=900)
    assert s.ensure_running()
    clock.now += 899
    assert s.stop_if_idle() is False
    assert docker.stops == 0
    s.touch()
    clock.now += 899
    assert s.stop_if_idle() is False
    clock.now += 2
    assert s.stop_if_idle() is True
    assert docker.stops == 1
    assert s.ensure_running()
    assert docker.starts == 2  # the second start is the wake after the idle stop


def test_after_an_idle_stop_the_next_request_wakes_it_again() -> None:
    clock, docker = Clock(), FakeDocker()
    s = state(docker, lambda: True, clock, idle_s=10)
    s.ensure_running()
    clock.now += 20
    s.stop_if_idle()
    assert docker.running() is False
    assert s.ensure_running() is True
    assert docker.running() is True


# --- a real run: waker in front of a fake app ---------------------------------------------


class FakeApp(http.server.BaseHTTPRequestHandler):
    seen: ClassVar[list[dict[str, Any]]] = []

    def log_message(self, *a: Any) -> None:
        return

    def _reply(self) -> None:
        n = int(self.headers.get("Content-Length", "0") or 0)
        body = self.rfile.read(n) if n else b""
        FakeApp.seen.append(
            {"method": self.command, "path": self.path, "headers": dict(self.headers), "body": body}
        )
        payload = json.dumps({"ok": True}).encode()
        self.send_response(201 if self.command == "POST" else 200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Set-Cookie", "a=1; HttpOnly")
        self.send_header("Set-Cookie", "b=2; HttpOnly")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = _reply


@pytest.fixture
def stack(tmp_path: Path) -> Any:
    FakeApp.seen = []
    app = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeApp)
    threading.Thread(target=app.serve_forever, daemon=True).start()
    web = tmp_path / "web"
    (web / "assets").mkdir(parents=True)
    (web / "index.html").write_text("<html>cairn</html>")
    (web / "assets" / "app.js").write_text("console.log(1)")
    docker = FakeDocker()
    st = w.AppState(docker, lambda: True, start_timeout_s=5, poll_s=0.01)
    front = w.Server(("127.0.0.1", 0), w.make_handler(st, app.server_address[1], TOKEN, web))
    threading.Thread(target=front.serve_forever, daemon=True).start()
    yield front.server_address[1], docker, st
    front.shutdown()
    front.server_close()
    app.shutdown()
    app.server_close()


def call(
    port: int,
    method: str,
    path: str,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> Any:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        data = resp.read()
        return resp.status, resp.getheaders(), data
    finally:
        conn.close()


def test_static_pages_do_not_wake_the_app_and_carry_security_headers(stack: Any) -> None:
    port, docker, _ = stack
    status, headers, body = call(port, "GET", "/")
    assert status == 200
    assert b"cairn" in body
    assert docker.starts == 0
    h = {k.lower(): v for k, v in headers}
    assert h["x-frame-options"] == "DENY"
    assert h["x-content-type-options"] == "nosniff"
    assert h["cache-control"] == "no-cache"
    _, headers, _ = call(port, "GET", "/assets/app.js")
    assert "immutable" in dict((k.lower(), v) for k, v in headers)["cache-control"]
    assert call(port, "GET", "/search")[0] == 200  # SPA route
    assert call(port, "POST", "/")[0] == 405
    assert call(port, "GET", "/waker/health")[0] == 200
    assert docker.starts == 0


def test_an_api_request_wakes_the_app_strips_the_prefix_and_adds_the_token(stack: Any) -> None:
    port, docker, _ = stack
    status, headers, body = call(
        port, "GET", "/api/readyz?x=1", headers={"X-Cairn-Proxy-Token": "forged", "Cookie": "s=1"}
    )
    assert status == 200
    assert json.loads(body) == {"ok": True}
    assert docker.starts == 1
    seen = FakeApp.seen[-1]
    assert seen["path"] == "/readyz?x=1"
    lowered = {k.lower(): v for k, v in seen["headers"].items()}
    assert lowered["x-cairn-proxy-token"] == TOKEN
    assert lowered["cookie"] == "s=1"
    assert lowered["x-forwarded-proto"] == "https"
    # both Set-Cookie headers survive the proxy
    assert [v for k, v in headers if k.lower() == "set-cookie"] == [
        "a=1; HttpOnly",
        "b=2; HttpOnly",
    ]


def test_post_bodies_are_forwarded_and_large_ones_refused(stack: Any) -> None:
    port, _, _ = stack
    status, _, _ = call(
        port, "POST", "/api/ask", body=b'{"q":"why"}', headers={"Content-Type": "application/json"}
    )
    assert status == 201
    assert FakeApp.seen[-1]["body"] == b'{"q":"why"}'
    before = len(FakeApp.seen)
    # declare an over-limit body without sending it: the refusal must not depend on reading it
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.putrequest("POST", "/api/ask")
        conn.putheader("Content-Length", str(w.MAX_BODY_BYTES + 1))
        conn.endheaders()
        resp = conn.getresponse()
        assert resp.status == 413
        assert json.loads(resp.read()) == {"error": "payload_too_large"}
    finally:
        conn.close()
    assert len(FakeApp.seen) == before


def test_an_unreachable_app_is_a_503_with_retry_after(stack: Any) -> None:
    port, docker, _st = stack
    docker.can_start = False
    status, headers, body = call(port, "GET", "/api/readyz")
    assert status == 503
    assert json.loads(body) == {"error": "api_unavailable"}
    assert dict((k.lower(), v) for k, v in headers)["retry-after"] == "30"
    assert call(port, "GET", "/api")[0] == 404
