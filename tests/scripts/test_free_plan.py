"""Free-plan cost check (plan 6.17), executable: nothing in the repo asks for a paid plan, an
always-on process, or a keep-alive that would defeat scale-to-zero."""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
BLUEPRINT = yaml.safe_load((ROOT / "render.yaml").read_text())


def test_every_render_service_is_one_free_web_service_with_no_extras() -> None:
    services = BLUEPRINT["services"]
    assert len(services) == 1
    (svc,) = services
    assert svc["type"] == "web"
    assert svc["plan"] == "free"
    assert "disk" not in svc
    assert "numInstances" not in svc
    assert "scaling" not in svc
    for forbidden in ("databases", "envVarGroups", "previews"):
        assert forbidden not in BLUEPRINT, f"render.yaml declares {forbidden}"
    assert not [
        s for s in services if s["type"] in ("cron", "worker", "pserv", "keyvalue", "redis")
    ]
    assert svc.get("autoDeployTrigger") == "off"  # no surprise builds either


def test_pages_uses_only_functions_and_has_no_paid_bindings() -> None:
    wrangler = (ROOT / "apps" / "web" / "wrangler.toml").read_text()
    for paid in (
        "[[queues",
        "[[r2_buckets",
        "[[d1_databases",
        "[[kv_namespaces",
        "[[hyperdrive",
        "[ai]",
        "[[durable",
    ):
        assert paid not in wrangler, f"wrangler.toml binds {paid}"
    routes = json.loads((ROOT / "apps" / "web" / "public" / "_routes.json").read_text())
    assert routes["include"] == ["/api/*", "/webhooks/*"]  # static traffic spends no Function quota


def test_no_keep_alive_pinger_exists_anywhere() -> None:
    """ADR-0014: a pinger keeps Render and Neon awake and spends Neon's compute allowance."""
    for wf in (ROOT / ".github" / "workflows").glob("*.y*ml"):
        doc = yaml.safe_load(wf.read_text())
        triggers = doc.get(True) or doc.get("on") or {}
        if isinstance(triggers, dict) and "schedule" in triggers:
            text = wf.read_text()
            assert not re.search(r"onrender\.com|/healthz|/readyz|curl .*https?://", text), (
                f"{wf.name} is scheduled and calls a URL"
            )
    for path in list((ROOT / "scripts").glob("*.py")) + list((ROOT / "mk").glob("*.mk")):
        assert "keepalive" not in path.read_text().lower().replace("-", "").replace("_", ""), (
            path.name
        )
