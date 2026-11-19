"""The Cloudflare Pages configuration (free-tier retarget, task 9.2).

Requirements 17.4 and 17.11: `_routes.json` limits Functions to `/api/*`, `_headers` sets the
three security headers, `wrangler.toml` holds no secret, and the `/api` Function and its proxy
module exist. The proxy behavior itself is tested in `apps/web/functions/_lib/proxy.test.ts`.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import Any, Final

import pytest

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
WEB: Final = REPO_ROOT / "apps" / "web"
SECRET_LIKE: Final = re.compile(r"PASSWORD|SECRET|TOKEN|KEY|CREDENTIAL", re.IGNORECASE)
HEX_LITERAL: Final = re.compile(r"[0-9a-fA-F]{32,}")


def _parse_headers(text: str) -> dict[str, dict[str, str]]:
    """Parse a Pages `_headers` file into {pattern: {header: value}}."""
    rules: dict[str, dict[str, str]] = {}
    current: dict[str, str] | None = None
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if raw[0] not in " \t":
            current = rules.setdefault(raw.strip(), {})
        else:
            assert current is not None, "header line before any URL pattern"
            name, _, value = raw.strip().partition(":")
            current[name.strip()] = value.strip()
    return rules


def test_routes_json_limits_functions_to_api() -> None:
    routes = json.loads((WEB / "public" / "_routes.json").read_text(encoding="utf-8"))
    assert routes == {"version": 1, "include": ["/api/*", "/webhooks/*"], "exclude": []}


def test_headers_file_sets_security_headers_and_asset_caching() -> None:
    rules = _parse_headers((WEB / "public" / "_headers").read_text(encoding="utf-8"))
    assert rules["/*"] == {
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
    }
    assert rules["/assets/*"] == {"Cache-Control": "public, max-age=31536000, immutable"}
    assert len(rules) <= 100  # Pages allows 100 header rules


@pytest.fixture(scope="module")
def wrangler() -> dict[str, Any]:
    return tomllib.loads((WEB / "wrangler.toml").read_text(encoding="utf-8"))


def test_wrangler_toml_names_the_pages_project(wrangler: dict[str, Any]) -> None:
    assert wrangler["name"] == "cairn"
    assert wrangler["pages_build_output_dir"] == "./dist"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", wrangler["compatibility_date"])


def test_wrangler_toml_vars_hold_only_the_api_origin(wrangler: dict[str, Any]) -> None:
    assert set(wrangler["vars"]) == {"API_ORIGIN"}
    assert wrangler["vars"]["API_ORIGIN"].startswith("https://")


def test_no_secret_in_wrangler_toml() -> None:
    text = (WEB / "wrangler.toml").read_text(encoding="utf-8")
    parsed = tomllib.loads(text)

    def walk(node: object, path: str = "") -> list[tuple[str, object]]:
        if isinstance(node, dict):
            found: list[tuple[str, object]] = []
            for key, value in node.items():
                found += walk(value, f"{path}.{key}" if path else key)
            return found
        return [(path, node)]

    for path, value in walk(parsed):
        assert not SECRET_LIKE.search(path), f"secret-like key in wrangler.toml: {path}"
        assert not (isinstance(value, str) and HEX_LITERAL.search(value)), (
            f"long hex value at {path}"
        )
    # The token is a Pages secret, never a var (comments may name it).
    assert "CAIRN_PROXY_TOKEN" not in {path.rsplit(".", 1)[-1] for path, _ in walk(parsed)}
    assert not (WEB / ".dev.vars").exists(), "apps/web/.dev.vars would hold local secrets"


def test_api_function_delegates_to_the_proxy() -> None:
    function = (WEB / "functions" / "api" / "[[path]].ts").read_text(encoding="utf-8")
    assert "export const onRequest" in function
    assert "proxyToApi" in function
    proxy = (WEB / "functions" / "_lib" / "proxy.ts").read_text(encoding="utf-8")
    for name in (
        "MAX_BODY_BYTES = 1_048_576",
        "UPSTREAM_TIMEOUT_MS = 90_000",
        "export async function proxyToApi",
    ):
        assert name in proxy
