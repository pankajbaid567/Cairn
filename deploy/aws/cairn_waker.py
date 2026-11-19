#!/usr/bin/env python3
"""cairn-waker: the front door of Cairn on a shared EC2 host (standard library only).

Why it exists. Neon's free plan gives 100 compute-hours a month, and a launched Cairn polls its
database about 9.5 times a second, so Neon is awake exactly while the app is. An always-on app
would use ~730 hours and exhaust the plan around day 17. This process keeps the app container
asleep until someone needs it, like the free Render service the design started from:

- serves the static web build (single-page app, with the same headers as the Pages project);
- forwards ``/api/<path>`` to the app as ``/<path>``, adds the proxy token and the forwarded
  headers, drops any token a client sent, and limits bodies to 1 MiB (the same rules as the
  Pages Function in ``apps/web/functions/_lib/proxy.ts``);
- starts the app container on the first ``/api`` request, holds that request until ``/readyz``
  answers (up to ``CAIRN_WAKER_START_TIMEOUT_S``), and stops the container after
  ``CAIRN_WAKER_IDLE_S`` seconds without an API request, which lets Neon scale to zero;
- answers ``/waker/health`` without waking anything.

Runs under systemd behind Caddy on 127.0.0.1. Python 3.9 compatible (Amazon Linux 2023).
"""

from __future__ import annotations

import http.client
import http.server
import json
import mimetypes
import os
import socketserver
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

MAX_BODY_BYTES = 1_048_576
UPSTREAM_TIMEOUT_S = 90.0
RETRY_AFTER_S = 30
TOKEN_HEADER = "x-cairn-proxy-token"  # noqa: S105 (a header name, not a secret)
HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "host",
        TOKEN_HEADER,
    }
)
FORWARDED = frozenset(
    {"x-forwarded-for", "x-forwarded-proto", "x-forwarded-host", "content-length"}
)
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}


def upstream_path(request_target: str) -> str | None:
    """``/api/readyz?x=1`` -> ``/readyz?x=1``. ``None`` for anything that is not under ``/api/``.

    The result always starts with exactly one slash, so a target such as ``/api//evil.example/x``
    cannot turn into an absolute or protocol-relative URL.
    """
    if not request_target.startswith("/api/"):
        return None
    rest = request_target[len("/api") :]
    path, sep, query = rest.partition("?")
    if "\\" in path or "\x00" in path or "/../" in path + "/" or "/./" in path + "/":
        return None
    path = "/" + path.lstrip("/")
    return path + (sep + query if sep else "")


def is_hop_by_hop(name: str) -> bool:
    return name in HOP_BY_HOP or name.startswith("proxy-")


def forward_headers(
    incoming: list[tuple[str, str]], token: str, client_ip: str | None, host: str | None
) -> dict[str, str]:
    """Client headers minus the hop-by-hop set and what the proxy sets itself, plus the token."""
    named = set()
    for name, value in incoming:
        if name.lower() == "connection":
            named.update(p.strip().lower() for p in value.split(",") if p.strip())
    out: dict[str, str] = {}
    for name, value in incoming:
        key = name.lower()
        if is_hop_by_hop(key) or key in FORWARDED or key in named:
            continue
        out[name] = value
    if token:
        out[TOKEN_HEADER] = token
    if client_ip:
        out["x-forwarded-for"] = client_ip
    out["x-forwarded-proto"] = "https"
    if host:
        out["x-forwarded-host"] = host
    return out


def static_file(root: Path, url_path: str) -> Path | None:
    """The file for ``url_path`` under ``root``; the SPA entry for a path with no extension.

    ``None`` when the path escapes ``root`` or names a missing file with an extension.
    """
    clean = url_path.split("?", 1)[0].split("#", 1)[0]
    rel = clean.lstrip("/")
    root = root.resolve()
    candidate = (root / rel).resolve() if rel else root / "index.html"
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    if candidate.is_file():
        return candidate
    if "." not in Path(rel).name:  # a client-side route such as /search
        index = root / "index.html"
        return index if index.is_file() else None
    return None


class Docker:
    """The three docker commands the waker needs (no shell, fixed argument lists)."""

    def __init__(
        self, name: str, run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run
    ):
        self.name = name
        self._run = run

    def running(self) -> bool:
        res = self._run(
            ["docker", "inspect", "-f", "{{.State.Running}}", self.name],
            capture_output=True,
            text=True,
            timeout=20,
        )
        return res.returncode == 0 and res.stdout.strip() == "true"

    def start(self) -> bool:
        res = self._run(["docker", "start", self.name], capture_output=True, text=True, timeout=60)
        return res.returncode == 0

    def stop(self) -> bool:
        res = self._run(
            ["docker", "stop", "-t", "25", self.name], capture_output=True, text=True, timeout=60
        )
        return res.returncode == 0


class AppState:
    """Wake on demand, stop when idle. One lock, so concurrent first requests start it once."""

    def __init__(
        self,
        docker: Docker,
        ready: Callable[[], bool],
        *,
        idle_s: float = 900.0,
        start_timeout_s: float = 90.0,
        poll_s: float = 2.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.docker, self.ready = docker, ready
        self.idle_s, self.start_timeout_s, self.poll_s = idle_s, start_timeout_s, poll_s
        self._clock, self._sleep = clock, sleep
        self._lock = threading.Lock()
        self._last_activity = clock()
        self._known_up = False

    def touch(self) -> None:
        self._last_activity = self._clock()

    def ensure_running(self) -> bool:
        """True once the app answers ``/readyz``; False if it cannot be started in time."""
        self.touch()
        if self._known_up:
            return True
        with self._lock:
            if self._known_up:
                return True
            if not self.docker.running() and not self.docker.start():
                return False
            deadline = self._clock() + self.start_timeout_s
            while self._clock() < deadline:
                if self.ready():
                    self._known_up = True
                    self.touch()
                    return True
                self._sleep(self.poll_s)
            return False

    def mark_down(self) -> None:
        """An upstream request failed: check again before the next one."""
        self._known_up = False

    def stop_if_idle(self) -> bool:
        """Stop the container when nothing used it for ``idle_s``. True when it was stopped."""
        with self._lock:
            if self._clock() - self._last_activity < self.idle_s:
                return False
            if not self.docker.running():
                self._known_up = False
                return False
            stopped = self.docker.stop()
            self._known_up = False
            return stopped


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_handler(
    state: AppState, app_port: int, token: str, web_root: Path
) -> type[http.server.BaseHTTPRequestHandler]:
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "cairn-waker"

        def log_message(self, fmt: str, *args: object) -> None:  # no request line, no cookie
            return

        def _json(self, code: int, obj: object, extra: dict[str, str] | None = None) -> None:
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _static(self) -> None:
            if self.command not in ("GET", "HEAD"):
                return self._json(405, {"error": "method_not_allowed"})
            path = static_file(web_root, self.path)
            if path is None:
                return self._json(404, {"error": "not_found"})
            data = path.read_bytes()
            ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            immutable = "/assets/" in self.path
            self.send_header(
                "Cache-Control", "public, max-age=31536000, immutable" if immutable else "no-cache"
            )
            for k, v in SECURITY_HEADERS.items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def _api(self) -> None:
            target = upstream_path(self.path)
            if target is None:
                return self._json(404, {"error": "not_found"})
            declared = self.headers.get("Content-Length")
            length = int(declared) if declared and declared.isdigit() else 0
            if length > MAX_BODY_BYTES:
                self.close_connection = True  # the body is not read, so this connection ends here
                return self._json(413, {"error": "payload_too_large"}, {"Connection": "close"})
            body = self.rfile.read(length) if length else None
            if not state.ensure_running():
                return self._json(
                    503, {"error": "api_unavailable"}, {"Retry-After": str(RETRY_AFTER_S)}
                )
            headers = forward_headers(
                list(self.headers.items()),
                token,
                self.headers.get("X-Forwarded-For") or self.client_address[0],
                self.headers.get("Host"),
            )
            conn = http.client.HTTPConnection("127.0.0.1", app_port, timeout=UPSTREAM_TIMEOUT_S)
            try:
                conn.request(self.command, target, body=body, headers=headers)
                resp = conn.getresponse()
                payload = resp.read()
                self.send_response(resp.status)
                for name, value in resp.getheaders():
                    if not is_hop_by_hop(name.lower()) and name.lower() != "content-length":
                        self.send_header(name, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(payload)
            except (OSError, http.client.HTTPException):
                state.mark_down()
                self._json(503, {"error": "api_unavailable"}, {"Retry-After": str(RETRY_AFTER_S)})
            finally:
                conn.close()
            state.touch()

        def _dispatch(self) -> None:
            if self.path == "/waker/health":
                return self._json(200, {"status": "ok", "app_running": state.docker.running()})
            if self.path.startswith("/api/") or self.path == "/api":
                return self._api()
            return self._static()

        do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = _dispatch

    return Handler


def ready_check(app_port: int, token: str) -> Callable[[], bool]:
    def check() -> bool:
        try:
            conn = http.client.HTTPConnection("127.0.0.1", app_port, timeout=5)
            try:
                conn.request("GET", "/readyz", headers={TOKEN_HEADER: token})
                return conn.getresponse().status == 200
            finally:
                conn.close()
        except (OSError, http.client.HTTPException):
            return False

    return check


def watchdog(state: AppState, every_s: float, stop: threading.Event) -> None:
    while not stop.wait(every_s):
        state.stop_if_idle()


def main() -> None:
    env = os.environ
    port = int(env.get("CAIRN_WAKER_PORT", "8003"))
    app_port = int(env.get("CAIRN_APP_PORT", "8004"))
    token = env.get("CAIRN_PROXY_TOKEN", "")
    if len(token) < 32:
        raise SystemExit("CAIRN_PROXY_TOKEN must be set (at least 32 characters)")
    state = AppState(
        Docker(env.get("CAIRN_WAKER_CONTAINER", "cairn")),
        ready_check(app_port, token),
        idle_s=float(env.get("CAIRN_WAKER_IDLE_S", "900")),
        start_timeout_s=float(env.get("CAIRN_WAKER_START_TIMEOUT_S", "90")),
    )
    stop = threading.Event()
    threading.Thread(target=watchdog, args=(state, 30.0, stop), daemon=True).start()
    web_root = Path(env.get("CAIRN_WAKER_WEB_ROOT", "/opt/cairn/web"))
    server = Server(("127.0.0.1", port), make_handler(state, app_port, token, web_root))
    try:
        server.serve_forever()
    finally:
        stop.set()


if __name__ == "__main__":
    main()
