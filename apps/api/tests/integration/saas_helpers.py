"""Shared helpers for the SaaS API tests: an organisation with named members of given roles."""

from __future__ import annotations

from fastapi.testclient import TestClient

from .test_saas_api import PASSWORD, Saas


class Workspace:
    """An owner plus members by name and role, each with a signed-in client."""

    def __init__(self, saas: Saas, members: dict[str, str]) -> None:
        self.saas = saas
        self.owner, _ = saas.with_org("owner", "Gov Inc")
        self.clients: dict[str, TestClient] = {"owner": self.owner}
        self.pids: dict[str, int] = {}
        for name, role in members.items():
            self.add(name, role)

    def add(self, name: str, role: str) -> TestClient:
        mail = self.saas.email(name)
        token = self.owner.post("/org/invites", json={"email": mail, "role": role}).json()["token"]
        c = self.saas.new_client()
        c.post("/auth/signup", json={"email": mail, "password": PASSWORD, "name": name.title()})
        c.post("/auth/confirm", json={"email": mail, "code": "123456"})
        c.post("/auth/login", json={"email": mail, "password": PASSWORD})
        assert c.post("/invites/accept", json={"token": token}).status_code == 200
        self.clients[name] = c
        self.pids[name] = next(
            m["principal_id"]
            for m in self.owner.get("/org/members").json()["members"]
            if m["email"] == mail
        )
        return c

    def __getitem__(self, name: str) -> TestClient:
        return self.clients[name]
