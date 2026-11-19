"""The real GitHub client: a GitHub App installation over REST (plan 3.3, 3.7).

Authentication is the App flow: sign a short-lived RS256 JWT with the App's private key, exchange it
for an installation token, cache the token until shortly before it expires. Reads use conditional
requests (``If-None-Match``) so an unchanged permission list costs no rate limit (a 304 is free),
and every list follows ``Link: rel="next"`` pagination. The private key and the tokens are never
logged or included in an exception message.

This module cannot be exercised against GitHub here (no org and no App yet): it is covered by
tests with a mocked transport, and by the user-run steps in ``docs/sync.md``.
"""

from __future__ import annotations

import base64
import json
import re
import time
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime
from typing import Any, Final

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from cairn_connectors.github.client import GitHubError, NotFound
from cairn_connectors.github.models import (
    ApiBudget,
    GhCollaborator,
    GhComment,
    GhDelivery,
    GhFile,
    GhIssue,
    GhPull,
    GhRepo,
    GhRepoTeam,
    GhTeam,
)

API: Final = "https://api.github.com"
TOKEN_SKEW_S: Final = 120
PER_PAGE: Final = 100
_NEXT: Final = re.compile(r'<([^>]+)>;\s*rel="next"')


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def app_jwt(app_id: str, private_key_pem: bytes, *, now: float | None = None) -> str:
    """A GitHub App JWT (RS256, valid 9 minutes; GitHub allows at most 10)."""
    issued = int(now if now is not None else time.time())
    header = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    claims = _b64(
        json.dumps(
            {"iat": issued - 30, "exp": issued + 540, "iss": app_id}, separators=(",", ":")
        ).encode()
    )
    key = serialization.load_pem_private_key(private_key_pem, password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise GitHubError("the GitHub App private key must be an RSA key")
    signature = key.sign(f"{header}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{header}.{claims}.{_b64(signature)}"


def _ts(value: str | None) -> datetime:
    return (
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        if value
        else datetime.fromtimestamp(0, UTC)
    )


class HttpGitHub:
    def __init__(
        self,
        app_id: str,
        installation_id: str,
        private_key_pem: bytes,
        *,
        client: httpx.Client | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._app_id, self._installation = app_id, installation_id
        self._key = private_key_pem
        self._http = client or httpx.Client(base_url=API, timeout=20)
        self._clock = clock
        self._token: tuple[str, float] | None = None
        self._etags: dict[str, tuple[str, Any, str | None]] = {}
        self.budget = ApiBudget()

    def __repr__(self) -> str:  # never shows the key or a token
        return f"HttpGitHub(app={self._app_id}, installation={self._installation})"

    # --- plumbing -----------------------------------------------------------------------

    def _installation_token(self) -> str:
        if self._token and self._token[1] - self._clock() > TOKEN_SKEW_S:
            return self._token[0]
        jwt = app_jwt(self._app_id, self._key, now=self._clock())
        r = self._http.post(
            f"/app/installations/{self._installation}/access_tokens",
            headers={"Authorization": f"Bearer {jwt}", "Accept": "application/vnd.github+json"},
        )
        if r.status_code != 201:
            raise GitHubError("could not get an installation token", r.status_code)
        data = r.json()
        expires = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00")).timestamp()
        self._token = (data["token"], expires)
        return str(data["token"])

    def _get_page(self, path: str, params: dict[str, Any] | None = None) -> tuple[Any, str | None]:
        """One GET: ``(body, next page url)``. Conditional: a 304 answers from the cache for free."""  # noqa: E501
        key = path + "?" + "&".join(f"{k}={v}" for k, v in sorted((params or {}).items()))
        headers = {
            "Authorization": f"Bearer {self._installation_token()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if key in self._etags:
            headers["If-None-Match"] = self._etags[key][0]
        try:
            r = self._http.get(path, params=params, headers=headers)
        except httpx.HTTPError as exc:
            raise GitHubError(f"network error on {path}: {type(exc).__name__}") from None
        self.budget.requests += 1
        name = path.split("?")[0].strip("/").split("/")[0] or path
        self.budget.by_path[name] = self.budget.by_path.get(name, 0) + 1
        if r.status_code == 304 and key in self._etags:
            self.budget.not_modified += 1
            _, body, nxt = self._etags[key]
            return body, nxt
        if r.status_code == 404:
            raise NotFound(path)
        if r.status_code >= 400:
            raise GitHubError(f"GET {path} failed", r.status_code)
        body = r.json()
        match = _NEXT.search(r.headers.get("link", ""))
        nxt = match.group(1) if match else None
        if (etag := r.headers.get("etag")) is not None:
            self._etags[key] = (etag, body, nxt)
        return body, nxt

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self._get_page(path, params)[0]

    def _pages(self, path: str, params: dict[str, Any] | None = None) -> Iterator[Any]:
        """Every item of a list endpoint, following ``Link: rel="next"`` until it ends."""
        url: str | None = path
        query: dict[str, Any] | None = {"per_page": PER_PAGE, **(params or {})}
        while url is not None:
            body, url = self._get_page(url, query)
            yield from body if isinstance(body, list) else body.get("items", [])
            query = None  # the next URL already carries its query string

    # --- GitHubClient --------------------------------------------------------------------

    def list_org_members(self, org: str) -> Sequence[str]:
        return [m["login"] for m in self._pages(f"/orgs/{org}/members")]

    def list_teams(self, org: str) -> Sequence[GhTeam]:
        return [GhTeam(t["slug"], t["name"]) for t in self._pages(f"/orgs/{org}/teams")]

    def list_team_members(self, org: str, slug: str) -> Sequence[str]:
        return [m["login"] for m in self._pages(f"/orgs/{org}/teams/{slug}/members")]

    def list_repos(self, org: str) -> Sequence[GhRepo]:
        return [
            GhRepo(int(r["id"]), r["name"], bool(r["private"]), bool(r.get("archived")))
            for r in self._pages(f"/orgs/{org}/repos", {"type": "all"})
        ]

    def list_repo_teams(self, org: str, repo: str) -> Sequence[GhRepoTeam]:
        return [
            GhRepoTeam(t["slug"], t.get("permission", "pull"))
            for t in self._pages(f"/repos/{org}/{repo}/teams")
        ]

    def list_collaborators(self, org: str, repo: str) -> Sequence[GhCollaborator]:
        out = []
        for c in self._pages(f"/repos/{org}/{repo}/collaborators", {"affiliation": "direct"}):
            perms = c.get("permissions", {})
            level = next(
                (p for p in ("admin", "maintain", "push", "triage", "pull") if perms.get(p)), "pull"
            )
            out.append(GhCollaborator(c["login"], level, True))
        return out

    def _comments(self, items: Sequence[dict[str, Any]]) -> tuple[GhComment, ...]:
        return tuple(
            GhComment(
                (c.get("user") or {}).get("login", "ghost"),
                c.get("body") or "",
                _ts(c.get("created_at")),
            )
            for c in items
        )

    def get_pull(self, org: str, repo: str, number: int) -> GhPull:
        p = self._get(f"/repos/{org}/{repo}/pulls/{number}")
        comments = self._comments(
            list(self._pages(f"/repos/{org}/{repo}/issues/{number}/comments"))
        )
        reviews = self._comments(list(self._pages(f"/repos/{org}/{repo}/pulls/{number}/reviews")))
        return GhPull(
            number,
            p["title"],
            p.get("body") or "",
            p["state"],
            bool(p.get("merged")),
            (p.get("user") or {}).get("login", "ghost"),
            _ts(p.get("updated_at")),
            tuple(label["name"] for label in p.get("labels", [])),
            comments,
            reviews,
        )

    def get_issue(self, org: str, repo: str, number: int) -> GhIssue:
        i = self._get(f"/repos/{org}/{repo}/issues/{number}")
        comments = self._comments(
            list(self._pages(f"/repos/{org}/{repo}/issues/{number}/comments"))
        )
        return GhIssue(
            number,
            i["title"],
            i.get("body") or "",
            i["state"],
            (i.get("user") or {}).get("login", "ghost"),
            _ts(i.get("updated_at")),
            tuple(label["name"] for label in i.get("labels", [])),
            comments,
        )

    def list_pulls(self, org: str, repo: str, since: datetime | None = None) -> Sequence[int]:
        return [
            p["number"]
            for p in self._pages(f"/repos/{org}/{repo}/pulls", {"state": "all"})
            if since is None or _ts(p.get("updated_at")) >= since
        ]

    def list_issues(self, org: str, repo: str, since: datetime | None = None) -> Sequence[int]:
        return [
            i["number"]
            for i in self._pages(f"/repos/{org}/{repo}/issues", {"state": "all"})
            if "pull_request" not in i and (since is None or _ts(i.get("updated_at")) >= since)
        ]

    def list_markdown(self, org: str, repo: str) -> Sequence[GhFile]:
        tree = self._get(f"/repos/{org}/{repo}/git/trees/HEAD", {"recursive": 1})
        files = []
        for node in tree.get("tree", []):
            if node.get("type") == "blob" and str(node["path"]).lower().endswith(".md"):
                blob = self._get(f"/repos/{org}/{repo}/contents/{node['path']}")
                text = base64.b64decode(blob.get("content", "")).decode("utf-8", "replace")
                files.append(GhFile(node["path"], text, datetime.now(UTC)))
        return files

    def list_deliveries(self, since: datetime | None = None) -> Sequence[GhDelivery]:
        # App webhook deliveries are listed with the App JWT, not the installation token.
        jwt = app_jwt(self._app_id, self._key, now=self._clock())
        r = self._http.get(
            "/app/hook/deliveries",
            params={"per_page": PER_PAGE},
            headers={"Authorization": f"Bearer {jwt}", "Accept": "application/vnd.github+json"},
        )
        if r.status_code >= 400:
            raise GitHubError("could not list deliveries", r.status_code)
        out = [
            GhDelivery(
                str(d["guid"]),
                d["event"],
                int(d["status_code"]),
                _ts(d["delivered_at"]),
                bool(d.get("redelivery")),
            )
            for d in r.json()
        ]
        return [d for d in out if since is None or d.delivered_at >= since]

    def redeliver(self, delivery_id: str) -> None:
        jwt = app_jwt(self._app_id, self._key, now=self._clock())
        listing = self._http.get(
            "/app/hook/deliveries",
            params={"per_page": PER_PAGE},
            headers={"Authorization": f"Bearer {jwt}", "Accept": "application/vnd.github+json"},
        )
        numeric = next((d["id"] for d in listing.json() if str(d["guid"]) == delivery_id), None)
        if numeric is None:
            raise NotFound(f"delivery {delivery_id}")
        r = self._http.post(
            f"/app/hook/deliveries/{numeric}/attempts",
            headers={"Authorization": f"Bearer {jwt}", "Accept": "application/vnd.github+json"},
        )
        if r.status_code >= 400:
            raise GitHubError("redelivery failed", r.status_code)
