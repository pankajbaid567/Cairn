"""License checker: expression classifier, package policy, images and the report (Requirement 21.5).

The classifier tests use plain examples. The repo-level tests read the real
pyproject files, uv.lock and Makefile fragments, so they also catch a pin or a
workspace change that the checker no longer understands.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from scripts import licenses
from scripts.licenses import (
    Atom,
    ExpressionError,
    Finding,
    ImageRef,
    Op,
    PackageLicense,
    Resolved,
    Workspace,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def parse(text: str) -> licenses.Node:
    return licenses.parse_license_text(text)


def blockers(text: str) -> frozenset[str]:
    return licenses.blocking_ids(parse(text), licenses.ALLOWED)


# ---------------------------------------------------------------------------
# Expressions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "MIT",
        "mit",
        "MIT License",
        "Apache Software License",
        "Mozilla Public License 2.0 (MPL 2.0)",
        "Apache-2.0 OR BSD-2-Clause",
        "(MIT OR CC0-1.0)",
        "GPL-3.0-only OR MIT",
        "BSD-3-Clause AND MIT AND (ISC OR GPL-3.0-only)",
        "MIT AND PSF-2.0",
    ],
)
def test_allowed_expressions_have_no_blockers(text: str) -> None:
    assert blockers(text) == frozenset()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("GPL-3.0-only", {"GPL-3.0-only"}),
        ("MIT AND GPL-3.0-only", {"GPL-3.0-only"}),
        ("GPL-3.0-only OR AGPL-3.0-only", {"GPL-3.0-only", "AGPL-3.0-only"}),
        ("BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0", {"0BSD", "Zlib", "CC0-1.0"}),
        ("LGPL-3.0", {"LGPL-3.0-only"}),
        ("SomethingOdd-1.0", {"SomethingOdd-1.0"}),
    ],
)
def test_blocking_ids_name_what_stops_an_expression(text: str, expected: set[str]) -> None:
    assert blockers(text) == frozenset(expected)


def test_and_binds_tighter_than_or() -> None:
    # MIT OR (GPL AND ISC): the AND group is blocked but the OR still has MIT.
    assert blockers("MIT OR GPL-3.0-only AND ISC") == frozenset()
    # (GPL AND MIT) OR AGPL: neither side is clear.
    assert blockers("GPL-3.0-only AND MIT OR AGPL-3.0-only") == {"GPL-3.0-only", "AGPL-3.0-only"}


def test_with_exception_is_one_atom_and_not_allowed() -> None:
    node = parse("GPL-2.0-only WITH Classpath-exception-2.0")
    assert node == Atom("GPL-2.0-only WITH Classpath-exception-2.0")
    assert licenses.blocking_ids(node, licenses.ALLOWED)


@pytest.mark.parametrize(
    "text", ["", "   ", "MIT AND", "(MIT", "MIT)", "AND MIT", "Apache License Version 2.0"]
)
def test_malformed_text_raises(text: str) -> None:
    with pytest.raises(ExpressionError):
        parse(text)


def test_format_node_keeps_grouping() -> None:
    node = parse("MIT AND (Apache-2.0 OR ISC)")
    assert licenses.format_node(node) == "MIT AND (Apache-2.0 OR ISC)"


# ---------------------------------------------------------------------------
# Reading pip-licenses records
# ---------------------------------------------------------------------------


def entry(
    expression: str = "UNKNOWN", metadata: str = "UNKNOWN", classifier: str = "UNKNOWN"
) -> dict[str, str]:
    return {
        "Name": "pkg",
        "Version": "1",
        "License-Expression": expression,
        "License-Metadata": metadata,
        "License-Classifier": classifier,
    }


def test_expression_beats_metadata_and_classifier() -> None:
    resolved = licenses.resolve_python_license(entry("Apache-2.0", "MIT", "ISC License (ISCL)"))
    assert resolved == Resolved(Atom("Apache-2.0"), "License-Expression")


def test_short_known_metadata_beats_classifier() -> None:
    resolved = licenses.resolve_python_license(entry(metadata="BSD", classifier="MIT License"))
    assert resolved == Resolved(Atom("BSD-3-Clause"), "License-Metadata")


def test_full_license_text_in_metadata_is_ignored() -> None:
    text = "Apache License\n   Version 2.0, January 2004\n" + "x" * 200
    resolved = licenses.resolve_python_license(
        entry(metadata=text, classifier="Apache Software License")
    )
    assert resolved == Resolved(Atom("Apache-2.0"), "License-Classifier")


def test_several_classifiers_must_all_be_allowed() -> None:
    resolved = licenses.resolve_python_license(
        entry(classifier="MIT License; GNU General Public License v3 (GPLv3)")
    )
    assert resolved.node == Op("AND", (Atom("MIT"), Atom("GPL-3.0-only")))
    assert licenses.blocking_ids(resolved.node, licenses.ALLOWED) == {"GPL-3.0-only"}


def test_nothing_known_reads_as_unknown() -> None:
    assert licenses.resolve_python_license(entry()) == Resolved(Atom("UNKNOWN"), "none")
    assert licenses.resolve_python_license(entry(metadata="Custom thing")) == Resolved(
        Atom("UNKNOWN"), "none"
    )


# ---------------------------------------------------------------------------
# Package status
# ---------------------------------------------------------------------------


def judge(name: str, expression: str, *, direct: bool = True) -> PackageLicense:
    return licenses.classify(
        "python", name, "1.0", Resolved(parse(expression), "test"), direct=direct
    )


def test_allowed_license_is_allowed() -> None:
    assert judge("fastapi", "MIT").status == "allowed"


def test_lgpl_is_an_exception_for_psycopg_only() -> None:
    assert judge("psycopg", "LGPL-3.0-only").status == "exception"
    assert judge("psycopg_binary", "LGPL-3.0-only").status == "exception"
    assert judge("some-other-lib", "LGPL-3.0-only").status == "denied"


def test_numpy_exception_covers_its_bundled_licenses_and_nothing_else() -> None:
    expression = "BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0"
    assert judge("numpy", expression).status == "exception"
    assert judge("numpy", expression + " AND GPL-3.0-only").status == "denied"
    assert judge("not-numpy", expression).status == "denied"


def test_exceptions_do_not_apply_to_web_packages() -> None:
    package = licenses.classify(
        "web", "numpy", "1", Resolved(parse("0BSD"), "package.json"), direct=True
    )
    assert package.status == "denied"


def test_agpl_and_unknown_licenses_are_not_allowed() -> None:
    assert judge("x", "AGPL-3.0-only").status == "denied"
    assert judge("x", "Custom-1.0").status == "unknown"
    assert judge("x", "UNKNOWN").status == "unknown"


def test_direct_failures_fail_and_transitive_ones_only_warn() -> None:
    direct = judge("a", "GPL-3.0-only", direct=True)
    transitive = judge("b", "GPL-3.0-only", direct=False)
    findings = licenses.package_findings([direct, transitive, judge("c", "MIT")])
    assert [f.rule for f in findings] == ["LIC-1", "LIC-R"]
    assert [f.fails for f in findings] == [True, False]


# ---------------------------------------------------------------------------
# Workspace, lock file
# ---------------------------------------------------------------------------


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def make_root(tmp_path: Path) -> Path:
    write(
        tmp_path / "pyproject.toml",
        '[tool.uv.workspace]\nmembers = ["packages/*"]\n'
        '[dependency-groups]\ndev = ["pytest==9.1.1", {include-group = "other"}]\n'
        'other = ["Hypothesis_Lib==1"]\n',
    )
    write(
        tmp_path / "packages/core/pyproject.toml",
        '[project]\nname = "cairn-core"\nversion = "0.1.0"\n'
        'dependencies = ["psycopg[binary]==3.3.6", "pydantic==2.14.1"]\n',
    )
    write(
        tmp_path / "packages/api/pyproject.toml",
        '[project]\nname = "cairn-api"\nversion = "0.1.0"\n'
        'dependencies = ["cairn-core", "fastapi==1"]\n',
    )
    write(
        tmp_path / "apps/web/package.json",
        json.dumps({"dependencies": {"react": "1"}, "devDependencies": {"@scope/tool": "1"}}),
    )
    return tmp_path


def test_load_workspace_reads_members_and_direct_dependencies(tmp_path: Path) -> None:
    workspace = licenses.load_workspace(make_root(tmp_path))
    assert workspace.members == {"cairn-core", "cairn-api"}
    assert workspace.direct_python == {"psycopg", "pydantic", "pytest", "fastapi", "hypothesis-lib"}
    assert workspace.direct_web == {"react", "@scope/tool"}


def test_python_packages_skip_members_and_mark_direct(tmp_path: Path) -> None:
    workspace = licenses.load_workspace(make_root(tmp_path))
    entries = [
        entry("MIT") | {"Name": "FastAPI"},
        entry("MIT") | {"Name": "cairn_core"},
        entry("Apache-2.0") | {"Name": "idna"},
    ]
    result = licenses.python_packages(entries, workspace)
    assert [(p.name, p.direct) for p in result] == [("FastAPI", True), ("idna", False)]


def test_web_packages_flatten_versions_and_mark_direct() -> None:
    workspace = Workspace(frozenset(), frozenset(), frozenset({"react"}))
    data: Mapping[str, Any] = {
        "MIT": [{"name": "react", "versions": ["19.0.0"]}, {"name": "ms", "versions": ["2", "3"]}],
        "(MIT OR CC0-1.0)": [{"name": "dual", "versions": ["1"]}],
        "Unknown": [{"name": "mystery", "versions": ["0.1"]}],
    }
    result = licenses.web_packages(data, workspace)
    by_name = {(p.name, p.version): p for p in result}
    assert by_name[("react", "19.0.0")].direct
    assert not by_name[("ms", "3")].direct
    assert by_name[("dual", "1")].status == "allowed"
    assert by_name[("mystery", "0.1")].status == "unknown"


def test_denylist_finds_celery_redis_and_kombu_only() -> None:
    lock = (
        '[[package]]\nname = "Celery"\nversion = "5"\n'
        '[[package]]\nname = "redis"\nversion = "5"\n'
        '[[package]]\nname = "kombu"\nversion = "5"\n'
        '[[package]]\nname = "redis-om"\nversion = "1"\n'
    )
    findings = licenses.denylist_findings(lock)
    assert [f.subject for f in findings] == ["uv.lock:celery", "uv.lock:kombu", "uv.lock:redis"]
    assert all(f.rule == "LIC-2" and f.fails for f in findings)


def test_real_lock_file_has_no_denied_distribution() -> None:
    assert licenses.denylist_findings((REPO_ROOT / "uv.lock").read_text(encoding="utf-8")) == []


def test_real_workspace_is_understood() -> None:
    workspace = licenses.load_workspace(REPO_ROOT)
    assert {"cairn-core", "cairn-api", "cairn-worker", "cairn-eval"} <= workspace.members
    assert {"fastapi", "psycopg", "numpy", "pytest", "pip-licenses"} <= workspace.direct_python
    assert not workspace.members & workspace.direct_python
    assert {"react", "vite"} <= workspace.direct_web


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------

DIGEST = "sha256:" + "a" * 64


@pytest.mark.parametrize(
    ("reference", "repository"),
    [
        ("node:24.22.1-alpine3.24@" + DIGEST, "node"),
        ("docker.io/library/python:3.12-slim", "python"),
        ("ghcr.io/gitleaks/gitleaks:v8.30.1@" + DIGEST, "ghcr.io/gitleaks/gitleaks"),
        ("pgvector/pgvector:0.8.6-pg18@" + DIGEST, "pgvector/pgvector"),
        ("pgvector/pgvector:${PG_TAG}@${PG_DIGEST}", "pgvector/pgvector"),
        ("localhost:5000/team/app:1", "localhost:5000/team/app"),
        ("cairn-api:local", "cairn-api"),
    ],
)
def test_parse_image_ref(reference: str, repository: str) -> None:
    assert licenses.parse_image_ref(reference) == repository


def test_only_the_grafana_images_may_be_agpl() -> None:
    agpl = {repo for repo, info in licenses.IMAGE_LICENSES.items() if info.agpl}
    assert agpl == {"grafana/grafana", "grafana/grafana-oss"}


def test_pgvector_image_is_listed_under_the_postgresql_license() -> None:
    info = licenses.IMAGE_LICENSES["pgvector/pgvector"]
    assert info.license == "PostgreSQL License"
    assert not info.agpl
    rows = licenses.image_rows([ImageRef("pgvector/pgvector:0.8.6-pg18@" + DIGEST, "x.yml")])
    assert [(r.repository, r.known) for r in rows] == [("pgvector/pgvector", True)]
    assert licenses.image_findings(rows) == []


def test_unknown_image_is_a_finding_and_known_ones_are_not() -> None:
    refs = [
        ImageRef("node:24@" + DIGEST, "mk/web.mk"),
        ImageRef("cairn-api:local", "docker-compose.yml"),
        ImageRef("mystery/thing:1", "docker-compose.yml"),
        ImageRef("${FULL_IMAGE}", "docker-compose.yml"),
    ]
    rows = licenses.image_rows(refs)
    assert [r.repository for r in rows] == ["cairn-api", "mystery/thing", "node"]
    findings = licenses.image_findings(rows)
    assert findings == [
        Finding(
            "LIC-3", "mystery/thing", "image has no entry in IMAGE_LICENSES (scripts/licenses.py)"
        )
    ]


def test_find_images_reads_compose_dockerfiles_makefiles_and_hooks(tmp_path: Path) -> None:
    write(
        tmp_path / "docker-compose.yml",
        "services:\n  db:\n    image: pgvector/pgvector:0.8.6-pg18@" + DIGEST + "\n"
        "  api:\n    image: cairn-api:local\n",
    )
    write(
        tmp_path / "infra/docker/python-app.Dockerfile",
        "FROM python:3.12-slim AS base\nFROM base AS final\nFROM --platform=linux/amd64 scratch\n",
    )
    write(
        tmp_path / "mk/web.mk", "NODE_IMAGE ?= node:24@" + DIGEST + "\nTOOL_IMAGE ?= $(shell x)\n"
    )
    write(
        tmp_path / ".pre-commit-config.yaml",
        "        entry: ghcr.io/gitleaks/gitleaks:v8@" + DIGEST + " git --staged\n",
    )
    refs, compose, dockerfiles = licenses.find_images(tmp_path)
    assert compose
    assert dockerfiles
    assert sorted((licenses.parse_image_ref(r.reference), r.source) for r in refs) == [
        ("cairn-api", "docker-compose.yml"),
        ("ghcr.io/gitleaks/gitleaks", ".pre-commit-config.yaml"),
        ("node", "mk/web.mk"),
        ("pgvector/pgvector", "docker-compose.yml"),
        ("python", "infra/docker/python-app.Dockerfile"),
    ]


def test_find_images_reads_a_compose_anchor_and_skips_the_alias(tmp_path: Path) -> None:
    write(
        tmp_path / "docker-compose.yml",
        "x-pg-image: &pg-image pgvector/pgvector:0.8.6-pg18@" + DIGEST + "\n"
        "x-python-app: &python-app\n  user: '10001:10001'\n"
        "services:\n  db:\n    image: *pg-image\n",
    )
    refs, _, _ = licenses.find_images(tmp_path)
    assert [(licenses.parse_image_ref(r.reference), r.source) for r in refs] == [
        ("pgvector/pgvector", "docker-compose.yml")
    ]


def test_find_images_reports_missing_compose_and_dockerfiles(tmp_path: Path) -> None:
    assert licenses.find_images(tmp_path) == ([], False, False)


def test_real_tool_images_are_pinned_by_digest() -> None:
    refs, _, _ = licenses.find_images(REPO_ROOT)
    tools = [r for r in refs if r.source.startswith("mk/") or r.source == ".pre-commit-config.yaml"]
    assert {licenses.parse_image_ref(r.reference) for r in tools} >= {
        "node",
        "ghcr.io/gitleaks/gitleaks",
        "rhysd/actionlint",
    }
    for ref in tools:
        assert "@sha256:" in ref.reference, ref


def test_compose_obs_images_are_pinned_by_tag_and_digest_and_have_license_rows() -> None:
    refs, _, _ = licenses.find_images(REPO_ROOT)
    external = [r for r in refs if not licenses.parse_image_ref(r.reference).startswith("cairn-")]
    obs = {
        "otel/opentelemetry-collector-contrib",
        "prom/prometheus",
        "grafana/grafana",
        "busybox",
    }
    assert obs <= {licenses.parse_image_ref(r.reference) for r in external}
    for ref in external:
        if licenses.parse_image_ref(ref.reference) in obs:
            assert re.search(r":[^:@/]+@sha256:[0-9a-f]{64}$", ref.reference), ref
    assert licenses.image_findings(licenses.image_rows(refs)) == []


# ---------------------------------------------------------------------------
# Report and command line
# ---------------------------------------------------------------------------


def run_main(root: Path, python: list[dict[str, str]], web: Mapping[str, Any]) -> int:
    write(root / "py.json", json.dumps(python))
    write(root / "web.json", json.dumps(web))
    return licenses.main(
        [
            "--root",
            str(root),
            "--python-json",
            str(root / "py.json"),
            "--web-json",
            str(root / "web.json"),
        ]
    )


GOOD_WEB: Mapping[str, Any] = {"MIT": [{"name": "react", "versions": ["19"]}]}


def test_main_passes_and_writes_a_deterministic_report(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    python = [entry("MIT") | {"Name": "fastapi"}, entry("MPL-2.0") | {"Name": "certifi"}]
    assert run_main(root, python, GOOD_WEB) == 0
    first = (root / "docs/licenses.md").read_text(encoding="utf-8")
    assert run_main(root, python, GOOD_WEB) == 0
    assert (root / "docs/licenses.md").read_text(encoding="utf-8") == first
    assert first.startswith("# Licenses\n")
    assert first.endswith("\n")
    assert "Passed: 0 finding(s) that fail the run" in first
    assert "AGPL-3.0" in first
    assert "Grafana OSS is the only AGPL-3.0 image" in first
    assert "## Hosted services and models" in first
    for name in ("Neon", "Render", "Cloudflare Pages", "Cloudflare Workers AI"):
        assert name in first
    for model in (
        "@cf/baai/bge-small-en-v1.5",
        "@cf/baai/bge-reranker-base",
        "@cf/google/gemma-4-26b-a4b-it",
        "@cf/zai-org/glm-4.7-flash",
    ):
        assert model in first
    assert "`docker-compose.yml` does not exist yet" in first
    assert all(line == line.rstrip() for line in first.splitlines())


def test_main_fails_on_a_direct_dependency_outside_the_allowlist(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_root(tmp_path)
    python = [entry("GPL-3.0-only") | {"Name": "fastapi"}]
    assert run_main(root, python, GOOD_WEB) == 1
    assert "LIC-1 python:fastapi==1" in capsys.readouterr().err
    assert "Failed: 1 finding(s) that fail the run" in (root / "docs/licenses.md").read_text(
        encoding="utf-8"
    )


def test_main_fails_when_celery_is_locked(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    write(root / "uv.lock", '[[package]]\nname = "celery"\nversion = "5"\n')
    assert run_main(root, [], GOOD_WEB) == 1


def test_main_fails_on_an_unlisted_image(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    write(root / "docker-compose.yml", "services:\n  x:\n    image: mystery/thing:1\n")
    assert run_main(root, [], GOOD_WEB) == 1
    assert "`docker-compose.yml` is present" in (root / "docs/licenses.md").read_text(
        encoding="utf-8"
    )


def test_main_exits_2_on_unreadable_input(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_root(tmp_path)
    write(root / "web.json", "{}")
    code = licenses.main(
        [
            "--root",
            str(root),
            "--python-json",
            str(root / "missing.json"),
            "--web-json",
            str(root / "web.json"),
        ]
    )
    assert code == 2
    assert "error:" in capsys.readouterr().err
    write(root / "py.json", "{}")
    assert (
        licenses.main(
            [
                "--root",
                str(root),
                "--python-json",
                str(root / "py.json"),
                "--web-json",
                str(root / "web.json"),
            ]
        )
        == 2
    )
