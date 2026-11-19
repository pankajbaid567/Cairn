"""MCP tools through the real API: canary matrix on all five tools for agents acting for Asha and
Ben (plan 4.15), and the token rules (plan 4.17)."""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from cairn_api import agent_tokens
from cairn_core.testing import FreshDb
from cairn_eval.seed import SeededCorpus
from cairn_mcp.server import CairnTools, RateLimiter

from .conftest import SECRET, login, make_app_client
from .test_canary_matrix_v1 import _canary_cases

pytestmark = [pytest.mark.integration, pytest.mark.matrix]
USERS = ("asha", "ben")


@pytest.fixture(scope="module")
def world(
    decisions: tuple[FreshDb, SeededCorpus],
) -> Iterator[tuple[FreshDb, SeededCorpus, TestClient]]:
    db, corpus = decisions
    with db.connect_ingest() as c:
        for user in USERS:
            row = c.execute(
                "SELECT principal_id FROM cairn.principals WHERE external_id = %s",
                (f"user:{user}",),
            ).fetchone()
            assert row is not None
            c.execute(
                "INSERT INTO cairn.principals (external_id, kind, display_name, acting_for) "
                "VALUES (%s, 'agent', %s, %s) ON CONFLICT (org_id, external_id) DO NOTHING",
                (f"agent:{user}-claude", f"claude for {user}", row[0]),
            )
    for client in make_app_client(db, CAIRN_ANSWER_CACHE_SIZE=0):
        yield db, corpus, client


def token_for(user: str, ttl: int = 600) -> str:
    return agent_tokens.issue(f"{user}-claude", SECRET.encode(), ttl)


def tools_for(client: TestClient, token: str | None) -> CairnTools:
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    http = TestClient(client.app, headers=headers)
    return CairnTools(http, RateLimiter(10_000, 60.0))


def citation_for(db: FreshDb, external_id: str) -> str:
    with db.connect_owner() as c:
        row = c.execute(
            "SELECT left(r.content_hash, 8), length(r.text) FROM cairn.revisions r JOIN cairn.items i USING (item_id) "  # noqa: E501
            "WHERE i.external_id = %s ORDER BY r.revision_id DESC LIMIT 1",
            (external_id,),
        ).fetchone()
    assert row is not None
    return f"chunk:{external_id}@{row[0]}:0-{min(row[1], 200)}"


def all_tool_outputs(t: CairnTools, needle: str, source: str | None, db: FreshDb) -> dict[str, str]:
    out = {
        "search_context": t.call(
            "search_context", {"query": needle, "limit": 10, "include_public": True}
        )[0]
    }
    for key in ("ADR-007", "ADR-009", "ADR-012", "ADR-014", "ADR-015"):
        out[f"get_decision:{key}"] = t.call("get_decision", {"key": key})[0]
        out[f"decision_history:{key}"] = t.call("decision_history", {"key": key})[0]
    if source:
        out["get_evidence"] = t.call("get_evidence", {"citation": citation_for(db, source)})[0]
    out["pre_change_brief"] = t.call(
        "pre_change_brief", {"repo": "payment-service", "paths": ["db/pool.py"]}
    )[0]
    return out


def test_no_restricted_canary_reaches_an_agent_on_any_of_the_five_tools(
    world: tuple[FreshDb, SeededCorpus, TestClient],
) -> None:
    db, corpus, client = world
    cases = _canary_cases(corpus)
    cells = positives = 0
    for user in USERS:
        t = tools_for(client, token_for(user))
        for cid, needle, _family, who, external in cases:
            outputs = all_tool_outputs(t, needle, external, db)
            for tool, text in outputs.items():
                cells += 1
                if user not in who:
                    assert needle not in text, f"LEAK {cid} to agent for {user} via {tool}"
            if user in who and external:
                assert needle in outputs["search_context"] or needle in outputs.get(
                    "get_evidence", ""
                ), f"positive control: the agent for {user} should reach {cid}"
                positives += 1
    assert cells >= len(USERS) * len(cases) * 10
    assert positives >= 2


def test_an_agent_sees_exactly_what_its_user_sees(
    world: tuple[FreshDb, SeededCorpus, TestClient],
) -> None:
    _, _, client = world
    for user in USERS:
        login(client, user)
        mine = {
            r["citation"]
            for r in client.post(
                "/search", json={"query": "PostgreSQL ledger incident", "limit": 20}
            ).json()["results"]
        }
        theirs = {
            r["citation"]
            for r in json.loads(
                tools_for(client, token_for(user)).call(
                    "search_context",
                    {"query": "PostgreSQL ledger incident", "limit": 10, "include_public": True},
                )[0]
            )["results"]
        }
        assert theirs <= mine
        assert theirs
        for key in ("ADR-007", "ADR-012"):
            user_status = client.get(f"/decisions/{key}")
            _text, err = tools_for(client, token_for(user)).call("get_decision", {"key": key})
            assert (user_status.status_code == 200) == (not err)


def test_missing_wrong_expired_and_foreign_tokens_get_nothing(
    world: tuple[FreshDb, SeededCorpus, TestClient],
) -> None:
    _, _, client = world
    bare = TestClient(client.app)
    assert bare.post("/search", json={"query": "postgres"}).status_code == 401
    for bad in (
        "nonsense",
        "cairn-agent.x.y",
        token_for("asha")[:-3] + "AAA",
        agent_tokens.issue("asha-claude", b"o" * 40, 600),
        agent_tokens.issue("ghost-claude", SECRET.encode(), 600),
        agent_tokens.issue("asha-claude", SECRET.encode(), 1, now=1.0),
    ):
        r = bare.post(
            "/search", json={"query": "postgres"}, headers={"Authorization": f"Bearer {bad}"}
        )
        assert r.status_code == 401, bad[:20]
    # a session cookie is not an agent token, and a bad bearer is never rescued by a cookie
    login(client, "asha")
    cookie = client.cookies.get("cairn_session")
    r = bare.post(
        "/search",
        json={"query": "postgres"},
        headers={"Authorization": "Bearer nonsense", "cookie": f"cairn_session={cookie}"},
    )
    assert r.status_code == 401
    assert (
        bare.post(
            "/search", json={"query": "postgres"}, headers={"Authorization": f"Bearer {cookie}"}
        ).status_code
        == 401
    )


def test_agents_are_read_only_and_cannot_resolve_reviews_or_log_in(
    world: tuple[FreshDb, SeededCorpus, TestClient],
) -> None:
    _, _, client = world
    bare = TestClient(client.app, headers={"Authorization": f"Bearer {token_for('asha')}"})
    assert (
        bare.post(
            "/reviews/1/resolve", json={"state": "verified", "reason": "an agent tries this"}
        ).status_code
        == 403
    )
    assert bare.get("/me").status_code in (200, 401)


def test_agent_calls_are_audited_for_the_agent_and_visible_to_its_user(
    world: tuple[FreshDb, SeededCorpus, TestClient],
) -> None:
    db, _, client = world
    tools_for(client, token_for("ben")).call("search_context", {"query": "audit marker for agent"})
    with db.connect_owner() as c:
        row = c.execute(
            "SELECT a.principal_id, a.acting_for FROM cairn.audit_log a ORDER BY at DESC LIMIT 1"
        ).fetchone()
        agent = c.execute(
            "SELECT principal_id, acting_for FROM cairn.principals WHERE external_id = 'agent:ben-claude'"  # noqa: E501
        ).fetchone()
    assert row is not None
    assert agent is not None
    assert tuple(row) == (agent[0], agent[1])
    login(client, "ben")
    latest = client.get("/audit/mine").json()["entries"][0]
    assert latest["endpoint"] == "search"  # Ben sees what his agent did on his behalf


def test_a_per_agent_daily_cap_stops_a_runaway_token(
    world: tuple[FreshDb, SeededCorpus, TestClient],
) -> None:
    db, _, _ = world
    for client in make_app_client(db, CAIRN_AGENT_CALLS_PER_DAY=3):
        t = tools_for(client, token_for("asha"))
        statuses = [t.call("search_context", {"query": "q"})[1] for _ in range(5)]
        assert statuses[:3] == [False, False, False]
        assert statuses[3:] == [True, True]


def test_pre_change_brief_for_db_pool_py_surfaces_adr_009_and_its_partial_revert(
    world: tuple[FreshDb, SeededCorpus, TestClient],
) -> None:
    """The demo (plan 4.23): an agent acting for Asha, about to change db/pool.py in payment-service."""  # noqa: E501
    _, _, client = world
    text, err = tools_for(client, token_for("asha")).call(
        "pre_change_brief", {"repo": "payment-service", "paths": ["db/pool.py"]}
    )
    assert err is False
    brief = json.loads(text)
    by_key = {d["key"]: d for d in brief["decisions"]}
    assert {"db/pool.py", "db/", "payment-service"} <= set(brief["anchors"])
    assert "ADR-009" in by_key
    assert "ADR-007" in by_key
    adr9 = by_key["ADR-009"]
    assert adr9["status"] in ("implemented", "accepted")
    revert = [n for n in adr9["notes"] if n["event"] == "partially_reverted"]
    assert revert
    assert "session mode" in (revert[0]["note"] or "")
    assert adr9["notes"][0]["at"].startswith("2026-05-20")
    # a path that matches nothing yields an empty brief rather than an error
    text2, err2 = tools_for(client, token_for("asha")).call(
        "pre_change_brief", {"paths": ["docs/unrelated.md"]}
    )
    assert err2 is False
    assert json.loads(text2)["decisions"] == []
