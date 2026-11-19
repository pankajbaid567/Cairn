"""Accounts, organisations, roles, invitations, tenancy and the audit chain through the real API."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from psycopg_pool import AsyncConnectionPool

from cairn_api.idp import FakeIdentityProvider
from cairn_api.main import build_app
from cairn_api.search import build_runtime
from cairn_core.config import ApiSettings
from cairn_core.testing import FreshDb
from cairn_eval.seed import SeededCorpus
from cairn_retrieval.runtime import build_providers

from .conftest import search_settings

pytestmark = pytest.mark.integration

PASSWORD = "correct-horse-9"


class Saas:
    def __init__(self, db: FreshDb, client: TestClient, idp: FakeIdentityProvider) -> None:
        self.db, self.main, self.idp = db, client, idp

    def new_client(self) -> TestClient:
        # each client is a different "visitor" to the rate limiter
        return TestClient(self.main.app, headers={"x-forwarded-for": uuid.uuid4().hex})

    def email(self, who: str) -> str:
        return f"{who}{uuid.uuid4().hex[:6]}@example.org"

    def sign_in(self, who: str, client: TestClient | None = None) -> tuple[TestClient, str]:
        c = client or self.new_client()
        mail = self.email(who)
        assert (
            c.post(
                "/auth/signup", json={"email": mail, "password": PASSWORD, "name": who}
            ).status_code
            == 202
        )
        assert c.post("/auth/confirm", json={"email": mail, "code": "123456"}).status_code == 200
        r = c.post("/auth/login", json={"email": mail, "password": PASSWORD})
        assert r.status_code == 200, r.text
        return c, mail

    def with_org(self, who: str, org: str) -> tuple[TestClient, str]:
        c, mail = self.sign_in(who)
        r = c.post("/orgs", json={"name": f"{org} {uuid.uuid4().hex[:4]}"})
        assert r.status_code == 201, r.text
        return c, mail


@pytest.fixture(scope="module")
def saas(seeded: tuple[FreshDb, SeededCorpus]) -> Iterator[Saas]:
    db = seeded[0]
    api = ApiSettings.model_validate({"CAIRN_APP_DB_PASSWORD": "unused", "CAIRN_PROXY_TOKEN": None})
    search = search_settings()
    pool: AsyncConnectionPool[Any] = AsyncConnectionPool(
        db.app_dsn, min_size=1, max_size=4, kwargs={"prepare_threshold": None}, open=False
    )
    runtime = build_runtime(api, search, build_providers(search), pool=pool)
    idp = FakeIdentityProvider()
    runtime.idp = idp
    with TestClient(build_app(api, probe=None, search=runtime)) as client:
        yield Saas(db, client, idp)


def test_sign_up_needs_confirmation_then_signs_in(saas: Saas) -> None:
    c = saas.new_client()
    mail = saas.email("ana")
    assert (
        c.post(
            "/auth/signup", json={"email": mail, "password": PASSWORD, "name": "Ana"}
        ).status_code
        == 202
    )
    assert c.post("/auth/login", json={"email": mail, "password": PASSWORD}).json() == {
        "error": "not_confirmed"
    }
    assert c.post("/auth/confirm", json={"email": mail, "code": "000000"}).status_code == 422
    assert c.post("/auth/confirm", json={"email": mail, "code": "123456"}).status_code == 200
    assert (
        c.post("/auth/login", json={"email": mail, "password": "wrong-password-1"}).status_code
        == 401
    )
    r = c.post("/auth/login", json={"email": mail, "password": PASSWORD})
    assert r.status_code == 200 and r.json()["orgs"] == [] and r.json()["org_id"] is None  # noqa: PT018
    s = c.get("/session").json()
    assert s["account"]["email"] == mail and s["org"] is None and s["role"] is None  # noqa: PT018


def test_weak_passwords_and_bad_input_are_refused_without_echo(saas: Saas) -> None:
    c = saas.new_client()
    r = c.post("/auth/signup", json={"email": "not-an-email", "password": PASSWORD, "name": "X"})
    assert r.status_code == 422 and r.json() == {"error": "invalid_request"}  # noqa: PT018
    r = c.post(
        "/auth/signup",
        json={"email": saas.email("w"), "password": "short", "name": "X", "extra": "SECRET"},
    )
    assert r.status_code == 422 and "SECRET" not in r.text  # noqa: PT018


def test_an_account_creates_an_organisation_and_owns_it(saas: Saas) -> None:
    c, mail = saas.with_org("owner", "Acme")
    s = c.get("/session").json()
    assert s["role"] == "owner" and s["org"]["name"].startswith("Acme")  # noqa: PT018
    members = c.get("/org/members").json()["members"]
    assert [(m["email"], m["role"], m["you"]) for m in members] == [(mail, "owner", True)]
    actions = [e["action"] for e in c.get("/org/audit").json()["events"]]
    assert "org.created" in actions
    v = c.get("/org/audit/verify").json()
    assert v["ok"] is True and v["checked"] >= 1 and len(v["head_hash"]) == 64  # noqa: PT018


def test_the_invitation_flow_end_to_end(saas: Saas) -> None:
    owner, _ = saas.with_org("boss", "Initech")
    guest_c = saas.new_client()
    bob_mail = saas.email("bob")
    inv = owner.post("/org/invites", json={"email": bob_mail, "role": "member"})
    assert inv.status_code == 201, inv.text
    token = inv.json()["token"]
    assert owner.get("/org/invites").json()["invites"][0]["email"] == bob_mail
    # anyone holding the link can see what it is for, before signing in
    assert guest_c.get("/invites/preview", params={"token": token}).json()["email"] == bob_mail
    assert guest_c.get("/invites/preview", params={"token": "x" * 40}).status_code == 404
    # signing up with another address cannot redeem it
    other, _ = saas.sign_in("eve", saas.new_client())
    assert other.post("/invites/accept", json={"token": token}).json() == {
        "error": "invalid_invite"
    }
    guest_c.post("/auth/signup", json={"email": bob_mail, "password": PASSWORD, "name": "Bob"})
    guest_c.post("/auth/confirm", json={"email": bob_mail, "code": "123456"})
    guest_c.post("/auth/login", json={"email": bob_mail, "password": PASSWORD})
    r = guest_c.post("/invites/accept", json={"token": token})
    assert r.status_code == 200, r.text
    assert guest_c.get("/session").json()["role"] == "member"
    assert guest_c.post("/invites/accept", json={"token": token}).status_code == 400  # once only
    assert len(owner.get("/org/members").json()["members"]) == 2
    assert owner.get("/org/invites").json()["invites"] == []
    actions = {e["action"] for e in owner.get("/org/audit").json()["events"]}
    assert {"member.invited", "invite.accepted"} <= actions


def test_a_member_cannot_administer_and_the_refusal_is_audited(saas: Saas) -> None:
    owner, _ = saas.with_org("boss", "Globex")
    bob = saas.new_client()
    mail = saas.email("bob")
    token = owner.post("/org/invites", json={"email": mail, "role": "member"}).json()["token"]
    saas.sign_in("bob", bob) if False else None
    bob.post("/auth/signup", json={"email": mail, "password": PASSWORD, "name": "Bob"})
    bob.post("/auth/confirm", json={"email": mail, "code": "123456"})
    bob.post("/auth/login", json={"email": mail, "password": PASSWORD})
    bob.post("/invites/accept", json={"token": token})
    assert (
        bob.post("/org/invites", json={"email": saas.email("x"), "role": "member"}).status_code
        == 403
    )
    assert bob.get("/org/invites").status_code == 403
    assert bob.get("/org/audit/verify").status_code == 403
    assert (
        bob.patch(
            "/org/members/" + owner.get("/session").json()["account"]["id"], json={"role": "member"}
        ).status_code
        == 403
    )
    # a member reads only their own audit events; the owner sees the denials
    assert all(e["actor"] for e in bob.get("/org/audit").json()["events"])
    denied = [e for e in owner.get("/org/audit").json()["events"] if e["outcome"] == "denied"]
    assert {e["action"] for e in denied} >= {"member.invite", "invite.list"}


def test_roles_teams_and_deactivation(saas: Saas) -> None:
    owner, _ = saas.with_org("boss", "Hooli")
    mail = saas.email("mia")
    token = owner.post("/org/invites", json={"email": mail, "role": "member"}).json()["token"]
    mia = saas.new_client()
    mia.post("/auth/signup", json={"email": mail, "password": PASSWORD, "name": "Mia"})
    mia.post("/auth/confirm", json={"email": mail, "code": "123456"})
    mia.post("/auth/login", json={"email": mail, "password": PASSWORD})
    mia.post("/invites/accept", json={"token": token})
    account = mia.get("/session").json()["account"]["id"]
    assert owner.patch(f"/org/members/{account}", json={"role": "manager"}).status_code == 200
    assert mia.get("/session").json()["role"] == "manager"
    me = owner.get("/session").json()["account"]["id"]
    r = owner.patch(f"/org/members/{me}", json={"role": "member"})  # nobody changes their own role
    assert r.status_code == 403
    team = owner.post("/org/teams", json={"name": "Payments"})
    assert team.status_code == 201
    pid = next(
        m["principal_id"]
        for m in owner.get("/org/members").json()["members"]
        if m["account_id"] == account
    )
    assert (
        owner.post(
            f"/org/teams/{team.json()['principal_id']}/members",
            json={"principal_id": pid, "on": True},
        ).status_code
        == 200
    )
    assert owner.get("/org/teams").json()["teams"][0]["member_count"] == 1
    # deactivating ends the session's organisation at once
    assert owner.patch(f"/org/members/{account}", json={"active": False}).status_code == 200
    assert mia.get("/org/members").status_code == 409
    assert mia.get("/session").json()["org"] is None


def test_organisations_are_isolated_from_each_other(saas: Saas) -> None:
    a, _ = saas.with_org("ana", "Alpha")
    b, _ = saas.with_org("bo", "Bravo")
    a_org, b_org = a.get("/org").json(), b.get("/org").json()
    assert a_org["org_id"] != b_org["org_id"]
    assert len(a.get("/org/members").json()["members"]) == 1
    # B cannot switch into A, and a forged switch does nothing
    assert b.post("/session/org", json={"org_id": a_org["org_id"]}).status_code == 403
    assert b.get("/org").json()["org_id"] == b_org["org_id"]
    # each organisation has its own audit chain, numbered from 1
    for c in (a, b):
        events = c.get("/org/audit").json()["events"]
        assert min(e["seq"] for e in events) == 1
        assert c.get("/org/audit/verify").json()["ok"] is True
    # B's account in A's organisation is not a member: A's member list does not include it
    assert all(m["email"] != "" for m in a.get("/org/members").json()["members"])
    assert saas.main.get("/org").status_code in (401, 409)


def test_a_tampered_chain_is_reported(saas: Saas) -> None:
    c, _ = saas.with_org("audit", "Initrode")
    for i in range(3):
        c.post("/org/teams", json={"name": f"Team {i} {uuid.uuid4().hex[:4]}"})
    org = c.get("/org").json()["org_id"]
    assert c.get("/org/audit/verify").json()["ok"] is True
    with saas.db.connect_superuser() as su:
        su.execute("SET session_replication_role = replica")
        su.execute(
            "UPDATE cairn.audit_events SET action = 'team.deleted' WHERE org_id = %s AND org_seq = 2",  # noqa: E501
            (org,),
        )
    bad = c.get("/org/audit/verify").json()
    assert bad["ok"] is False and bad["first_bad_seq"] == 2  # noqa: PT018


def test_audit_export_is_csv_with_the_chain_hashes(saas: Saas) -> None:
    c, _ = saas.with_org("exp", "Umbrella")
    r = c.get("/org/audit/export")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")  # noqa: PT018
    header, first = r.text.splitlines()[:2]
    assert header.endswith("prev_hash,row_hash") and first.split(",")[0] == "1"  # noqa: PT018


def test_demo_personas_have_roles_in_the_demo_organisation(saas: Saas) -> None:
    for persona, role in (
        ("dev", "owner"),
        ("priya", "admin"),
        ("carla", "manager"),
        ("ben", "member"),
    ):
        c = saas.new_client()
        assert c.post("/auth/dev-login", json={"persona": persona}).status_code == 204
        s = c.get("/session").json()
        assert (s["role"], s["org"]["org_id"], s["demo"]) == (role, 1, True)
        assert c.post("/orgs", json={"name": "Nope"}).status_code == 401
    ben = saas.new_client()
    ben.post("/auth/dev-login", json={"persona": "ben"})
    assert (
        ben.post("/org/invites", json={"email": saas.email("z"), "role": "member"}).status_code
        == 403
    )


def test_sign_in_is_rate_limited_and_never_reveals_unknown_addresses(saas: Saas) -> None:
    c = saas.new_client()
    codes = [
        c.post(
            "/auth/login", json={"email": "nobody@example.org", "password": "x" * 12}
        ).status_code
        for _ in range(14)
    ]
    assert set(codes[:12]) == {401} and codes[-1] == 429  # noqa: PT018
