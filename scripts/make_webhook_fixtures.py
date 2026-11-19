"""Write the signed webhook fixtures shared by the Python and TypeScript receivers (plan 3.16).

Deterministic: running it again changes nothing. The secret is a published test value, never a
real one. Each fixture holds the exact body bytes, the headers GitHub would send, and the thin row
a receiver must produce (``null`` when the event is not used).

    python scripts/make_webhook_fixtures.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "packages" / "core" / "src"))

from cairn_core.webhook import sign, thin_row  # noqa: E402

OUT = REPO_ROOT / "tests" / "fixtures" / "webhooks"
WEB_COPY = REPO_ROOT / "apps" / "web" / "functions" / "_lib" / "fixtures" / "webhooks"
"""The web container mounts only apps/web, so the TypeScript tests read an identical copy; a Python
test asserts the two directories stay byte-for-byte equal."""
SECRET = "kestrel-test-webhook-secret"  # a published test value  # noqa: S105


def payloads() -> dict[str, tuple[str, dict[str, Any]]]:
    org = {"id": 7001, "login": "kestrel-pay-demo"}
    repo = {"id": 9002, "full_name": "kestrel-pay-demo/eng-docs"}
    inst = {"id": 5003}
    big = "x" * 120_000  # the largest fixture: the Function must stay inside its CPU budget
    return {
        "membership_removed": (
            "membership",
            {
                "action": "removed",
                "scope": "team",
                "member": {"login": "ben"},
                "team": {"name": "security", "id": 301},
                "organization": org,
                "installation": inst,
            },
        ),
        "organization_member_removed": (
            "organization",
            {
                "action": "member_removed",
                "membership": {"user": {"login": "ben"}},
                "organization": org,
                "installation": inst,
            },
        ),
        "repository_privatized": (
            "repository",
            {"action": "privatized", "repository": repo, "organization": org, "installation": inst},
        ),
        "member_removed": (
            "member",
            {
                "action": "removed",
                "member": {"login": "rook"},
                "repository": repo,
                "installation": inst,
            },
        ),
        "pull_request_edited": (
            "pull_request",
            {
                "action": "edited",
                "number": 123,
                "pull_request": {"number": 123, "title": "Use PostgreSQL"},
                "repository": {"id": 9003, "full_name": "kestrel-pay-demo/payment-service"},
                "organization": org,
                "installation": inst,
            },
        ),
        "issue_comment_created": (
            "issue_comment",
            {
                "action": "created",
                "issue": {"number": 44},
                "comment": {"body": "private text must not be stored"},
                "repository": repo,
                "installation": inst,
            },
        ),
        "push_large": (
            "push",
            {
                "ref": "refs/heads/main",
                "repository": repo,
                "installation": inst,
                "commits": [{"message": big}],
            },
        ),
        "star_ignored": ("star", {"action": "created", "repository": repo}),
        "ping_ignored": ("ping", {"zen": "Keep it logically awesome."}),
        "hostile_repo_name": (
            "pull_request",
            {
                "action": "opened",
                "number": 1,
                "repository": {"id": 1, "full_name": "evil/../name; drop table"},
            },
        ),
    }


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    manifest: list[dict[str, Any]] = []
    for n, (name, (event, payload)) in enumerate(payloads().items(), start=1):
        body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        headers = {
            "x-github-event": event,
            "x-github-delivery": f"fixture-{n:04d}-{name}",
            "x-hub-signature-256": sign(SECRET.encode(), body.encode()),
            "content-type": "application/json",
        }
        row = thin_row(headers, payload)
        record = {
            "name": name,
            "headers": headers,
            "body": body,
            "expected": row.as_dict() if row else None,
        }
        (OUT / f"{name}.json").write_text(
            json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        manifest.append({"name": name, "file": f"{name}.json"})
    (OUT / "index.json").write_text(
        json.dumps({"secret": SECRET, "fixtures": manifest}, indent=2) + "\n", encoding="utf-8"
    )
    WEB_COPY.mkdir(parents=True, exist_ok=True)
    for f in OUT.glob("*.json"):
        (WEB_COPY / f.name).write_bytes(f.read_bytes())
    print(
        f"wrote {len(manifest)} fixtures to {OUT.relative_to(REPO_ROOT)} and {WEB_COPY.relative_to(REPO_ROOT)}"
    )


if __name__ == "__main__":
    main()
