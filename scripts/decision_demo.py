"""Phase 2 demos (plan 2.24): ADR-007 archaeology and ADR-012 time travel, through the HTTP API.

Needs the local stack with a seeded corpus (``make seed-local seed-decisions``). Logs in as the
scripted personas (dev login is only on in local and demo) and prints what each sees. Nothing is
written to the database. Exit 0 when every check holds, 1 when one fails.

    python scripts/decision_demo.py [--base http://127.0.0.1:8000] [--persona asha]
"""

from __future__ import annotations

import argparse
import sys

import httpx

CHECKS: list[tuple[str, bool]] = []


def check(label: str, ok: bool) -> None:
    CHECKS.append((label, ok))
    print(f"  [{'ok' if ok else 'FAIL'}] {label}")


def login(client: httpx.Client, persona: str) -> None:
    r = client.post("/auth/dev-login", json={"persona": persona})
    if r.status_code != 204:
        sys.exit(
            f"login as {persona} failed (HTTP {r.status_code}); is dev login on and the corpus seeded?"  # noqa: E501
        )
    value = next(
        (
            v.split(";")[0].split("=", 1)[1]
            for k, v in r.headers.multi_items()
            if k.lower() == "set-cookie"
        ),
        None,
    )
    if value is None:
        sys.exit("no session cookie set")
    client.cookies.clear()
    client.headers["cookie"] = f"cairn_session={value}"


def archaeology(client: httpx.Client) -> None:
    print("\nADR-007: from proposal on 3 March to the merged pull request on 12 March")
    page = client.get("/decisions/ADR-007").json()
    for e in page["timeline"]:
        print(f"  {str(e['valid_at'])[:10]}  {e['event']:<12} source={e['source']}")
    events = [e["event"] for e in page["timeline"]]
    check(
        "timeline runs proposed, accepted, implemented in order",
        [x for x in events if x != "discussed"][:3] == ["proposed", "accepted", "implemented"],
    )
    check(
        "proposal is dated 3 March and the implementation 12 March",
        page["timeline"][0]["valid_at"][:10] == "2026-03-03"
        and next(e for e in page["timeline"] if e["event"] == "implemented")["valid_at"][:10]
        == "2026-03-12",
    )
    for day, want in (
        ("2026-03-04", "proposed"),
        ("2026-03-08", "accepted"),
        ("2026-03-12", "implemented"),
    ):
        got = client.get(f"/decisions/ADR-007?valid={day}").json()["status"]
        print(f"  status on {day}: {got}")
        check(f"ADR-007 on {day} is {want}", got == want)
    cited = [c for c in page["claims"] if c["citations"]]
    check("claims carry the sources that support them", len(cited) >= 2)


def time_travel(client: httpx.Client) -> None:
    print("\nADR-012: Kafka on 10 March, superseded on 15 July")
    print(
        "  (the 'implemented' step of ADR-012 is recorded only in a ledger-team pull request; the"
    )
    print("   demo personas are not ledger members, so they see it as accepted until 15 July)")
    for day, want in (
        ("2026-03-10", "accepted"),
        ("2026-07-14", "accepted"),
        ("2026-07-20", "superseded"),
    ):
        r = client.get(f"/decisions/ADR-012?valid={day}")
        if r.status_code != 200:
            check("ADR-012 is visible to the demo persona", False)
            return
        got = r.json()["status"]
        print(f"  status on {day}: {got}")
        check(f"ADR-012 on {day} is {want}", got == want)
    early = client.get("/decisions/ADR-012?valid=2026-07-20&recorded=2026-07-10").json()["status"]
    print(f"  believed on 10 July about 20 July: {early}")
    check(
        "on 10 July the supersession was not yet known, so 20 July read as accepted",
        early == "accepted",
    )
    graph = client.get("/decisions/ADR-012?hops=2").json()
    check(
        "ADR-012 is linked to the decision that supersedes it",
        any(link["kind"] == "supersedes" for link in graph["links"]),
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--persona", default="asha")
    ap.add_argument(
        "--proxy-token", default=None, help="x-cairn-proxy-token if the API requires it"
    )
    a = ap.parse_args()
    headers = {"x-cairn-proxy-token": a.proxy_token} if a.proxy_token else {}
    with httpx.Client(base_url=a.base, headers=headers, timeout=60) as client:
        login(client, a.persona)
        archaeology(client)
        time_travel(client)
    failed = [label for label, ok in CHECKS if not ok]
    print(f"\n{len(CHECKS) - len(failed)} of {len(CHECKS)} checks hold")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
