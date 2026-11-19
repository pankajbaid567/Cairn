"""Fill the demo organisation (Kestrel Pay) with a believable month of work, using the real flows.

Policies, decisions, approvals, risks, the access review and the anchor go through the API as the
demo personas, so the audit trail, the hash chain and every rule are exercised exactly as in use.
Daily reports and attestations are written straight to the database as the owner, because only five
people can sign in as a persona and the demo needs fourteen people's reports.

    set -a; . ./.env.neon; set +a
    uv run python scripts/seed_demo_org.py --base https://cairn.3-109-36-90.sslip.io

Safe to run twice: it skips what is already there. It never prints a secret.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import random
import sys
from typing import Any

import httpx
import psycopg

DEMO_ORG = 1

WORK: dict[str, list[str]] = {
    "payments": [
        "Fixed the refund event shape so the ledger consumer no longer drops partial refunds",
        "Reviewed the chargeback webhook PR and added idempotency keys",
        "Wrote the migration plan for moving payment-service tables to PostgreSQL 18",
        "Added contract tests for the partner settlement API",
        "Investigated a duplicate-capture alert; root cause was a retried client call",
        "Paired on the card-on-file token rotation and updated the runbook",
    ],
    "ledger": [
        "Reconciled the nightly ledger totals against the partner statements, no drift",
        "Implemented the transactional outbox for ledger events and replayed a week of data",
        "Reviewed the double-entry invariant checks and added two property tests",
        "Drafted ADR text for replacing Kafka replays with logical replication",
        "Tuned the slow balance query; p95 down from 410 ms to 90 ms",
    ],
    "mobile": [
        "Shipped the 4.12 release candidate to TestFlight and fixed the keyboard overlap bug",
        "Reduced cold-start time on Android by lazy-loading the rewards module",
        "Reviewed the biometric login PR and updated the threat notes",
        "Triaged crash reports; two are the same null-handling issue in the receipt view",
    ],
    "platform": [
        "Moved the CI runners to ARM and cut build minutes by a third",
        "Rotated the staging database credentials and updated the secret references",
        "Upgraded the cluster to the latest patch release with no downtime",
        "Added alerts for queue depth on the outbox drainer",
        "Wrote the disaster-recovery drill checklist and ran the first table-top",
    ],
    "security": [
        "Completed the quarterly access review prep and listed the privileged accounts",
        "Reviewed the IAM policy changes for the new analytics role",
        "Ran the dependency audit; two moderate findings queued for the next sprint",
        "Triaged a phishing report and confirmed no credentials were entered",
    ],
    "other": [
        "Prepared the partner limits update and shared it with the payments team",
        "Ran onboarding for two new joiners and walked them through the engineering handbook",
        "Closed out the vendor security questionnaire with evidence from the compliance page",
        "Reviewed open hiring requisitions with the engineering managers",
    ],
}
NEXT = [
    "Pair on the review comments and merge",
    "Start the next migration step behind a flag",
    "Write the follow-up ADR and ask for approval",
    "Watch the dashboards after the release",
    "Pick up the next item from the review queue",
]
BLOCKERS = [
    "Waiting for the ledger team to confirm the event schema",
    "The staging database is slow; blocked on the platform ticket",
    "Need a decision on the retry policy before I can continue",
    "Waiting on partner sandbox credentials",
]
TEAM_OF = {
    "asha": "payments",
    "suki": "payments",
    "dev": "payments",
    "mira": "ledger",
    "jonas": "ledger",
    "omar": "mobile",
    "tomas": "platform",
    "nadia": "platform",
    "priya": "security",
    "iris": "security",
    "lena": "other",
    "kofi": "other",
    "carla": "other",
    "ines": "other",
    "ben": "payments",
}


def weekdays_back(n: int, today: dt.date) -> list[dt.date]:
    out: list[dt.date] = []
    d = today
    while len(out) < n:
        d -= dt.timedelta(days=1)
        if d.weekday() < 5:
            out.append(d)
    return sorted(out)


def db_connect() -> psycopg.Connection[Any]:
    return psycopg.connect(
        host=os.environ["CAIRN_DB_HOST"],
        dbname=os.environ.get("CAIRN_DB_NAME", "cairn"),
        user="cairn_owner",
        password=os.environ["CAIRN_OWNER_DB_PASSWORD"],
        sslmode=os.environ.get("CAIRN_DB_SSLMODE", "verify-full"),
        sslrootcert=os.environ.get("CAIRN_DB_SSLROOTCERT", "/etc/ssl/cert.pem"),
        autocommit=True,
    )


def seed_reports() -> int:
    today = dt.datetime.now(dt.UTC).date()
    made = 0
    with db_connect() as conn:
        people = conn.execute(
            "SELECT m.principal_id, substr(p.external_id, 6), m.role FROM cairn_meta.org_members m "
            "JOIN cairn.principals p ON p.principal_id = m.principal_id "
            "WHERE m.org_id = %s AND m.state = 'active' AND m.role IN ('owner', 'admin', 'manager', 'member')",
            (DEMO_ORG,),
        ).fetchall()
        for pid, handle, _role in people:
            rng = random.Random(f"{handle}-seed")  # noqa: S311
            pool = WORK[TEAM_OF.get(handle, "other")]
            for day in [*weekdays_back(10, today), today]:
                skip = (
                    0.04 if day != today else 0.55
                )  # a few gaps, and most people have not reported yet today
                if rng.random() < skip or (handle in ("ines", "kofi") and rng.random() < 0.25):
                    continue
                done = ". ".join(rng.sample(pool, k=min(2, len(pool)))) + "."
                blockers = rng.choice(BLOCKERS) if rng.random() < 0.12 else ""
                cur = conn.execute(
                    "INSERT INTO cairn.work_reports (author_principal, report_date, done, next_up, blockers, links, org_id) "
                    "VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s) ON CONFLICT (org_id, author_principal, report_date) DO NOTHING",
                    (pid, day, done, rng.choice(NEXT), blockers, "[]", DEMO_ORG),
                )
                made += cur.rowcount
    return made


def seed_attestations(policy_slugs: list[str]) -> int:
    made = 0
    with db_connect() as conn:
        rng = random.Random("attest")  # noqa: S311
        people = [
            r[0]
            for r in conn.execute(
                "SELECT principal_id FROM cairn_meta.org_members WHERE org_id=%s AND state='active' AND role IN ('owner','admin','manager','member') ORDER BY principal_id",
                (DEMO_ORG,),
            )
        ]
        for slug in policy_slugs:
            row = conn.execute(
                "SELECT v.version_id FROM cairn.policy_versions v JOIN cairn.policies p ON p.policy_id = v.policy_id "
                "WHERE p.org_id = %s AND p.slug = %s AND v.state = 'published'",
                (DEMO_ORG, slug),
            ).fetchone()
            if row is None:
                continue
            for pid in people:
                if rng.random() < 0.8:
                    made += conn.execute(
                        "INSERT INTO cairn.attestations (version_id, principal_id, org_id, attested_at) "
                        "VALUES (%s, %s, %s, now() - make_interval(hours => %s)) ON CONFLICT DO NOTHING",
                        (row[0], pid, DEMO_ORG, rng.randint(1, 120)),
                    ).rowcount
    return made


class Persona:
    def __init__(self, base: str, name: str) -> None:
        self.name = name
        self.c = httpx.Client(
            base_url=base + "/api", timeout=120, headers={"content-type": "application/json"}
        )
        r = self.c.post("/auth/dev-login", json={"persona": name})
        r.raise_for_status()

    def get(self, path: str) -> Any:
        r = self.c.get(path)
        r.raise_for_status()
        return r.json()

    def post(self, path: str, body: Any = None, ok: tuple[int, ...] = (200, 201, 202)) -> Any:
        r = self.c.post(path, json=body)
        if r.status_code not in ok:
            raise RuntimeError(f"{self.name} POST {path}: {r.status_code} {r.text[:160]}")
        return r.json() if r.content else {}


POLICIES = {
    "access-control-policy": (
        "Access Control Policy",
        "Access to systems and data is granted on a need-to-know basis and reviewed every quarter.\n\n"
        "1. Every person has their own account; shared accounts are not allowed.\n"
        "2. Privileged roles (owner, admin) are limited and approved by an administrator.\n"
        "3. When someone leaves or changes team, access is removed or changed the same day.\n"
        "4. Managers review who has access to what at least once a quarter, and someone other than the person under review decides.",
    ),
    "acceptable-use-policy": (
        "Acceptable Use Policy",
        "Company systems are for company work and must be used responsibly.\n\n"
        "1. Keep credentials private and use multi-factor authentication where offered.\n"
        "2. Do not copy customer or company data to personal accounts or devices.\n"
        "3. Report lost devices and suspected phishing to security within one hour.\n"
        "4. Use only approved tools for source code, documents and chat.",
    ),
    "incident-response-policy": (
        "Incident Response Policy",
        "Security and availability incidents are reported, contained, fixed and learned from.\n\n"
        "1. Anyone can declare an incident in the incidents channel.\n"
        "2. The on-call engineer is the incident lead until they hand over.\n"
        "3. Customer-impacting incidents are communicated within one hour.\n"
        "4. Every incident gets a written review within five working days, with actions that have owners and dates.",
    ),
}


def seed_api(base: str) -> None:
    dev, priya, carla, asha, ben = (
        Persona(base, n) for n in ("dev", "priya", "carla", "asha", "ben")
    )
    existing = {p["slug"]: p for p in dev.get("/governance/policies")["policies"]}
    for slug, (title, body) in POLICIES.items():
        if slug in existing:
            continue
        pid = priya.post("/governance/policies", {"title": title, "body": body, "attest_days": 30})[
            "policy_id"
        ]
        approval = priya.post(f"/governance/policies/{pid}/submit")["approval_id"]
        dev.post(
            f"/governance/approvals/{approval}/vote",
            {"vote": "approve", "comment": "Reviewed and approved."},
        )
        print("policy", slug)
    print("attestations", seed_attestations(list(POLICIES)))

    titles = {d["title"] for d in dev.get("/governance/decisions")["decisions"]}
    if "Adopt Amazon Bedrock for AI features" not in titles:
        r = asha.post(
            "/governance/decisions",
            {
                "title": "Adopt Amazon Bedrock for AI features",
                "question": "Where should the answer and embedding models run?",
                "outcome": "Use Amazon Bedrock in ap-south-1 for embeddings and answers, so models, data and logs stay in one Region and cost is capped per day.",
                "required": 1,
            },
        )
        carla.post(
            f"/governance/approvals/{r['approval_id']}/vote",
            {"vote": "approve", "comment": "Cost cap and Region fit our constraints."},
        )
        carla.post(
            f"/governance/decisions/{r['decision_id']}/implemented", {"note": "Live since November"}
        )
        print("decision", r["key"])
    if "Move ledger events to the transactional outbox" not in titles:
        r = ben.post(
            "/governance/decisions",
            {
                "title": "Move ledger events to the transactional outbox",
                "question": "How do consumers replay ledger events in order?",
                "outcome": "Replace Kafka replay with a transactional outbox over logical replication, keeping the event shape unchanged.",
                "required": 2,
            },
        )
        print("decision", r["key"], "(waiting for two approvers)")
    pending = {a["title"] for a in dev.get("/governance/approvals?state=all")["approvals"]}
    if "Waive 90-day password rotation for the build bot" not in pending:
        exp = (dt.date.today() + dt.timedelta(days=60)).isoformat()
        e = carla.post(
            "/governance/approvals",
            {
                "kind": "exception",
                "title": "Waive 90-day password rotation for the build bot",
                "reason": "The bot uses a short-lived token, rotated on every run.",
                "expires_at": exp,
                "required": 1,
            },
        )
        priya.post(
            f"/governance/approvals/{e['approval_id']}/vote",
            {"vote": "approve", "comment": "Compensating control: token lifetime is 1 hour."},
        )
    if "Need manager access to lead the payments squad" not in pending:
        ben.post(
            "/governance/approvals",
            {
                "kind": "access",
                "title": "Need manager access to lead the payments squad",
                "role": "manager",
                "reason": "Taking over the squad from next sprint.",
            },
        )
    risks = {r["title"] for r in dev.get("/governance/risks")["risks"]}
    for title, cat, like, imp, mit in (
        (
            "Single cloud Region for all workloads",
            "operational",
            4,
            4,
            "Documented recovery steps; table-top drill done; evaluate a second Region next quarter.",
        ),
        (
            "Key partner depends on one sandbox environment",
            "vendor",
            3,
            3,
            "Contract tests against recorded fixtures keep the team unblocked.",
        ),
        (
            "Privileged access is concentrated in two people",
            "security",
            3,
            5,
            "Quarterly access review; add a second owner.",
        ),
        (
            "Reporting gaps hide blockers from managers",
            "people",
            2,
            3,
            "Daily reports with a manager roll-up; follow up on missing days.",
        ),
    ):
        if title not in risks:
            carla.post(
                "/governance/risks",
                {
                    "title": title,
                    "category": cat,
                    "likelihood": like,
                    "impact": imp,
                    "mitigation": mit,
                    "description": "",
                },
            )

    reviews = dev.get("/compliance/reviews")["reviews"]
    if not reviews:
        dev.post("/compliance/reviews", {"title": "Q4 2026 access review", "due_days": 14})
    reviews = dev.get("/compliance/reviews")["reviews"]
    open_review = next((r for r in reviews if r["state"] == "open"), None)
    if open_review:
        for who in (dev, priya):
            for item in who.get("/compliance/reviews")["reviews"][0]["items"]:
                if item["mine"] and item["decision"] == "pending":
                    who.post(
                        f"/compliance/reviews/items/{item['item_id']}/decide",
                        {"decision": "keep", "comment": "Access still needed."},
                    )
        dev.post(f"/compliance/reviews/{open_review['review_id']}/close")
        print("access review closed")
    dev.post("/org/audit/anchor")
    carla_digest = carla.post(
        "/reports/digest",
        {
            "start": (dt.date.today() - dt.timedelta(days=7)).isoformat(),
            "end": dt.date.today().isoformat(),
            "scope": "team",
        },
        ok=(200,),
    )
    print("digest items", len(carla_digest.get("highlights", [])))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--skip-db", action="store_true")
    args = ap.parse_args()
    if not args.skip_db:
        print("reports", seed_reports())
    seed_api(args.base.rstrip("/"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
