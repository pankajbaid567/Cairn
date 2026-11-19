"""Check dependency licenses and write ``docs/licenses.md`` (``make licenses``).

Design §10 and "Dependencies"; Requirement 21.5. The Makefile target collects the
raw data and this script judges it, so the judging is plain functions over data:

* ``--python-json``: output of ``pip-licenses --from=all --format=json``
* ``--web-json``: output of ``pnpm licenses list --json``
* ``uv.lock``, the member ``pyproject.toml`` files and ``apps/web/package.json``
  under ``--root``
* container images, found in ``docker-compose.yml`` and ``Dockerfile`` files (when
  they exist), ``*_IMAGE`` variables in ``mk/*.mk`` and the digest-pinned entry in
  ``.pre-commit-config.yaml``

Rules (a finding with a LIC-1 to LIC-3 id fails the run):

========= ===========================================================================
LIC-1     a direct dependency (Python or web) has a license outside the allowlist
          or an unknown license. Per-package exceptions are listed in the policy
          and carry a reason
LIC-2     ``celery``, ``redis`` or ``kombu`` appears in ``uv.lock`` (decision D10)
LIC-3     a container image has no entry in the hand-kept image table
LIC-R     a transitive package has a license outside the allowlist: reported for
          review, never fails
========= ===========================================================================

License text is read from the best source a package offers: the PEP 639 license
expression, then the free-text ``License`` metadata (only when it is a short,
recognised name), then the trove classifiers. SPDX operators work as usual:
``A AND B`` needs both allowed, ``A OR B`` needs one, and AND binds tighter.

Exit status: 0 policy holds, 1 findings, 2 unreadable input or bad usage.
Standard library only (Python 3.11 or newer for ``tomllib``).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = Path("docs/licenses.md")

# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------

ALLOWED: Final[frozenset[str]] = frozenset(
    {"MIT", "BSD-2-Clause", "BSD-3-Clause", "Apache-2.0", "ISC", "PSF-2.0", "MPL-2.0"}
)

# Distributions that must never enter the lock file (decision D10: DBOS, not Celery).
DENIED_DISTRIBUTIONS: Final[tuple[str, ...]] = ("celery", "kombu", "redis")


@dataclass(frozen=True)
class PolicyException:
    """Extra license ids one named package may carry, with the reason on record."""

    ids: frozenset[str]
    reason: str


_LGPL = frozenset({"LGPL-3.0-only", "LGPL-3.0-or-later"})

PACKAGE_EXCEPTIONS: Final[Mapping[str, PolicyException]] = {
    "psycopg": PolicyException(
        _LGPL,
        "Design allows LGPL-3.0 for psycopg only. It is imported as a library and never modified.",
    ),
    "psycopg-binary": PolicyException(
        _LGPL,
        "The compiled wheel of psycopg (the `binary` extra): same project, same license, "
        "also unmodified.",
    ),
    "numpy": PolicyException(
        frozenset({"0BSD", "Zlib", "CC0-1.0"}),
        "numpy's license expression lists components bundled in its wheels besides BSD-3-Clause "
        "and MIT. All three added ids are permissive and impose no source-sharing duty.",
    ),
}

# Spelling variants seen in package metadata and trove classifiers, keyed by the
# casefolded text. Unlisted text stays as written and is judged unknown.
ALIASES: Final[Mapping[str, str]] = {
    "mit": "MIT",
    "mit license": "MIT",
    "bsd": "BSD-3-Clause",
    "bsd license": "BSD-3-Clause",
    "new bsd license": "BSD-3-Clause",
    "modified bsd license": "BSD-3-Clause",
    "3-clause bsd license": "BSD-3-Clause",
    "bsd 3-clause": "BSD-3-Clause",
    "bsd-3-clause": "BSD-3-Clause",
    "simplified bsd license": "BSD-2-Clause",
    "2-clause bsd license": "BSD-2-Clause",
    "bsd-2-clause": "BSD-2-Clause",
    "apache": "Apache-2.0",
    "apache 2.0": "Apache-2.0",
    "apache-2.0": "Apache-2.0",
    "apache license 2.0": "Apache-2.0",
    "apache license, version 2.0": "Apache-2.0",
    "apache software license": "Apache-2.0",
    "isc": "ISC",
    "isc license": "ISC",
    "isc license (iscl)": "ISC",
    "psf": "PSF-2.0",
    "psfl": "PSF-2.0",
    "psf-2.0": "PSF-2.0",
    "python software foundation license": "PSF-2.0",
    "mpl-2.0": "MPL-2.0",
    "mozilla public license 2.0": "MPL-2.0",
    "mozilla public license 2.0 (mpl 2.0)": "MPL-2.0",
    "lgpl-3.0": "LGPL-3.0-only",
    "gnu lesser general public license v3 (lgplv3)": "LGPL-3.0-only",
    "gnu lesser general public license v3 or later (lgplv3+)": "LGPL-3.0-or-later",
    "gnu affero general public license v3": "AGPL-3.0-only",
    "agpl-3.0": "AGPL-3.0-only",
    "gnu general public license v3 (gplv3)": "GPL-3.0-only",
    "gpl-3.0": "GPL-3.0-only",
    "public domain": "Unlicense",
}

# Ids that are real licenses even though they are not allowed, so that an
# unlisted license reads "denied" and a garbled one reads "unknown".
_EXTRA_KNOWN: Final[frozenset[str]] = frozenset(
    {
        "0BSD",
        "AGPL-3.0-only",
        "AGPL-3.0-or-later",
        "BlueOak-1.0.0",
        "CC-BY-3.0",
        "CC-BY-4.0",
        "CC0-1.0",
        "GPL-2.0-only",
        "GPL-2.0-or-later",
        "GPL-3.0-only",
        "GPL-3.0-or-later",
        "LGPL-2.1-only",
        "LGPL-2.1-or-later",
        "LGPL-3.0-only",
        "LGPL-3.0-or-later",
        "MIT-0",
        "Python-2.0",
        "Unlicense",
        "WTFPL",
        "Zlib",
    }
)

KNOWN: Final[frozenset[str]] = (
    ALLOWED
    | _EXTRA_KNOWN
    | frozenset(ALIASES.values())
    | frozenset(i for e in PACKAGE_EXCEPTIONS.values() for i in e.ids)
)
_KNOWN_BY_FOLD: Final[Mapping[str, str]] = {k.casefold(): k for k in KNOWN}


@dataclass(frozen=True)
class ImageLicense:
    license: str
    note: str = ""
    agpl: bool = False


# Hand-kept (design: "a hand-kept image table"). Keyed by repository, without
# registry host for Docker Hub, tag or digest. An image that is not listed here
# fails the run until someone reads its license and adds it.
IMAGE_LICENSES: Final[Mapping[str, ImageLicense]] = {
    # The `db` and `ft-db` image since the free-tier retarget: PostgreSQL 18 plus pgvector.
    "pgvector/pgvector": ImageLicense(
        "PostgreSQL License",
        "PostgreSQL 18 with pgvector (both PostgreSQL License); separate service, unmodified.",
    ),
    # `grafana/grafana` is the OSS edition (LICENSE in the image is the AGPL-3.0 text, checked
    # 27 Oct 2026). `grafana/grafana-oss` is the same edition under an older name and stopped
    # at 13.0.2, so the compose file uses `grafana/grafana`. Keep both rows.
    "grafana/grafana": ImageLicense(
        "AGPL-3.0", "Obs profile only, separate service, unmodified.", agpl=True
    ),
    "grafana/grafana-oss": ImageLicense(
        "AGPL-3.0", "Obs profile only, separate service, unmodified.", agpl=True
    ),
    "otel/opentelemetry-collector-contrib": ImageLicense(
        "Apache-2.0", "Obs profile only; run unmodified (infra/otel/Dockerfile adds busybox)."
    ),
    "prom/prometheus": ImageLicense("Apache-2.0", "Obs profile only; run unmodified."),
    "busybox": ImageLicense(
        "GPL-2.0-only",
        "One static binary copied next to the collector for its healthcheck "
        "(infra/otel/Dockerfile); unmodified, not linked to Cairn code.",
    ),
    "nginxinc/nginx-unprivileged": ImageLicense("BSD-2-Clause"),
    "nginx": ImageLicense("BSD-2-Clause"),
    "node": ImageLicense("MIT"),
    "python": ImageLicense("PSF-2.0"),
    "ghcr.io/astral-sh/uv": ImageLicense("MIT OR Apache-2.0"),
    "ghcr.io/gitleaks/gitleaks": ImageLicense("MIT"),
    "rhysd/actionlint": ImageLicense("MIT"),
}

# Components that no lock file lists (design "Dependencies"; brainstorm section 9).
STACK_COMPONENTS: Final[tuple[tuple[str, str, str], ...]] = (
    ("PostgreSQL 18", "PostgreSQL License", "In the pgvector image"),
    ("pgvector", "PostgreSQL License", "In the pgvector image"),
    ("OpenTelemetry Collector contrib", "Apache-2.0", "Obs profile image"),
    ("Prometheus", "Apache-2.0", "Obs profile image"),
    ("Grafana OSS", "AGPL-3.0", "Obs profile image; see the AGPL section"),
    ("Docker Compose", "Apache-2.0", "Task runner dependency, not shipped"),
    ("DuckDB", "MIT", "Phase 5 analytics; not a dependency in Phase 0"),
    ("Qdrant, Neo4j", "to review", "Experiments profile is empty in Phase 0; review when added"),
)

# Hosted services of the free-tier deployment (design §15, ADR-0014). They are accounts the
# user holds, not code Cairn ships, so the terms are the license. Links checked 3 Nov 2026
# (docs/evidence/free-tier/verify/FV10.md).
HOSTED_SERVICES: Final[tuple[tuple[str, str, str], ...]] = (
    (
        "Neon (free plan)",
        "[Neon terms of service](https://neon.com/terms-of-service)",
        "Production PostgreSQL 18 with pgvector",
    ),
    (
        "Render (free web service)",
        "[Render terms of service](https://render.com/terms)",
        "Production API and jobs (`cairn-combined`)",
    ),
    (
        "Cloudflare Pages and Pages Functions",
        "[Cloudflare Self-Serve Subscription Agreement](https://www.cloudflare.com/terms/)",
        "Production web build and the same-origin `/api/*` proxy",
    ),
    (
        "Cloudflare Workers",
        "[Cloudflare Self-Serve Subscription Agreement](https://www.cloudflare.com/terms/)",
        "Runtime of the Pages Function",
    ),
    (
        "Cloudflare Workers AI",
        "[Cloudflare Self-Serve Subscription Agreement](https://www.cloudflare.com/terms/); "
        "[Workers AI data usage](https://developers.cloudflare.com/workers-ai/privacy/)",
        "Embeddings, rerank and the default answer and judge models",
    ),
)

# Default models served by Workers AI (design FT-24). Cloudflare does not make the models;
# each license is the one on the publisher's model card (FV10).
DEFAULT_MODELS: Final[tuple[tuple[str, str, str, str], ...]] = (
    (
        "`@cf/baai/bge-small-en-v1.5`",
        "MIT",
        "[BAAI/bge-small-en-v1.5](https://huggingface.co/BAAI/bge-small-en-v1.5)",
        "Embedder (384 dimensions)",
    ),
    (
        "`@cf/baai/bge-reranker-base`",
        "MIT",
        "[BAAI/bge-reranker-base](https://huggingface.co/BAAI/bge-reranker-base)",
        "Reranker",
    ),
    (
        "`@cf/google/gemma-4-26b-a4b-it`",
        "Apache-2.0",
        "[google/gemma-4-26B-A4B-it](https://huggingface.co/google/gemma-4-26B-A4B-it)",
        "Default answer model",
    ),
    (
        "`@cf/zai-org/glm-4.7-flash`",
        "MIT",
        "[zai-org/GLM-4.7-Flash](https://huggingface.co/zai-org/GLM-4.7-Flash)",
        "Default judge model",
    ),
)

Rule = Literal["LIC-1", "LIC-2", "LIC-3", "LIC-R"]
FAILING_RULES: Final[frozenset[str]] = frozenset({"LIC-1", "LIC-2", "LIC-3"})


@dataclass(frozen=True, order=True)
class Finding:
    rule: str
    subject: str
    message: str

    def __str__(self) -> str:
        return f"{self.rule} {self.subject}: {self.message}"

    @property
    def fails(self) -> bool:
        return self.rule in FAILING_RULES


# ---------------------------------------------------------------------------
# License expressions
# ---------------------------------------------------------------------------


class ExpressionError(ValueError):
    """The text is not a license expression."""


@dataclass(frozen=True)
class Atom:
    id: str


@dataclass(frozen=True)
class Op:
    op: Literal["AND", "OR"]
    items: tuple[Node, ...]


Node = Atom | Op

_TOKEN = re.compile(r"\(|\)|[^\s()]+")
_KEYWORDS: Final[frozenset[str]] = frozenset({"AND", "OR", "WITH"})


def normalize_name(name: str) -> str:
    """PEP 503 normalisation, so ``pip_audit`` and ``pip-audit`` compare equal."""
    return re.sub(r"[-_.]+", "-", name).lower()


def canonical_id(token: str) -> str:
    """SPDX spelling for a known license name, or the token unchanged."""
    folded = token.strip().casefold()
    return ALIASES.get(folded) or _KNOWN_BY_FOLD.get(folded) or token.strip()


class _Parser:
    def __init__(self, text: str) -> None:
        self.tokens = _TOKEN.findall(text)
        self.pos = 0

    def peek(self) -> str | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def take(self) -> str:
        token = self.tokens[self.pos]
        self.pos += 1
        return token

    def parse(self) -> Node:
        if not self.tokens:
            raise ExpressionError("empty license text")
        node = self.expr()
        if self.peek() is not None:
            raise ExpressionError(f"unexpected {self.peek()!r}")
        return node

    def _is_keyword(self, token: str | None, word: str) -> bool:
        return token is not None and token.upper() == word

    def expr(self) -> Node:
        items = [self.term()]
        while self._is_keyword(self.peek(), "OR"):
            self.take()
            items.append(self.term())
        return items[0] if len(items) == 1 else Op("OR", tuple(items))

    def term(self) -> Node:
        items = [self.factor()]
        while self._is_keyword(self.peek(), "AND"):
            self.take()
            items.append(self.factor())
        return items[0] if len(items) == 1 else Op("AND", tuple(items))

    def factor(self) -> Node:
        word = self.peek()
        if word is None:
            raise ExpressionError("unexpected end")
        if word == "(":
            self.take()
            node = self.expr()
            if self.peek() != ")":
                raise ExpressionError("missing )")
            self.take()
            return node
        if word == ")" or word.upper() in _KEYWORDS:
            raise ExpressionError(f"unexpected {word!r}")
        self.take()
        if self._is_keyword(self.peek(), "WITH"):
            self.take()
            exception = self.peek()
            if exception is None or exception in ("(", ")") or exception.upper() in _KEYWORDS:
                raise ExpressionError("WITH needs an exception id")
            self.take()
            return Atom(f"{canonical_id(word)} WITH {exception}")
        return Atom(canonical_id(word))


def parse_license_text(text: str) -> Node:
    """Parse a license name or SPDX expression. Raises ExpressionError."""
    alias = ALIASES.get(text.strip().casefold())
    if alias is not None:
        return Atom(alias)
    return _Parser(text).parse()


def atoms(node: Node) -> list[str]:
    if isinstance(node, Atom):
        return [node.id]
    return [a for item in node.items for a in atoms(item)]


def format_node(node: Node, *, top: bool = True) -> str:
    if isinstance(node, Atom):
        return node.id
    inner = f" {node.op} ".join(format_node(i, top=False) for i in node.items)
    return inner if top else f"({inner})"


def blocking_ids(node: Node, allowed: frozenset[str]) -> frozenset[str]:
    """License ids that stop the expression from being allowed; empty means allowed.

    AND needs every operand allowed, so it collects all blockers. OR needs one
    operand allowed, so it is allowed as soon as any operand has no blockers.
    """
    if isinstance(node, Atom):
        return frozenset() if node.id in allowed else frozenset({node.id})
    blocked = [blocking_ids(item, allowed) for item in node.items]
    if node.op == "AND":
        return frozenset().union(*blocked)
    return frozenset() if any(not b for b in blocked) else frozenset().union(*blocked)


def _recognised(text: str) -> bool:
    try:
        node = parse_license_text(text)
    except ExpressionError:
        return False
    return all(a in KNOWN for a in atoms(node))


# ---------------------------------------------------------------------------
# Packages
# ---------------------------------------------------------------------------

Status = Literal["allowed", "exception", "denied", "unknown"]
UNKNOWN_ID: Final = "UNKNOWN"


@dataclass(frozen=True)
class Resolved:
    node: Node
    source: str


@dataclass(frozen=True)
class PackageLicense:
    ecosystem: Literal["python", "web"]
    name: str
    version: str
    license: str
    source: str
    direct: bool
    status: Status
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.status in ("allowed", "exception")


def _field(entry: Mapping[str, Any], key: str) -> str:
    value = str(entry.get(key) or "").strip()
    return "" if value.upper() == "UNKNOWN" else value


def resolve_python_license(entry: Mapping[str, Any]) -> Resolved:
    """Best license reading of one pip-licenses record (``--from=all``)."""
    expression = _field(entry, "License-Expression")
    if expression:
        try:
            return Resolved(parse_license_text(expression), "License-Expression")
        except ExpressionError:
            pass
    metadata = _field(entry, "License-Metadata")
    # Some packages put their whole license text here; only short, known names count.
    if metadata and len(metadata) <= 100 and "\n" not in metadata and _recognised(metadata):
        return Resolved(parse_license_text(metadata), "License-Metadata")
    classifier = _field(entry, "License-Classifier")
    if classifier:
        parts: list[Node] = []
        for part in (p.strip() for p in classifier.split(";")):
            if not part:
                continue
            try:
                parts.append(parse_license_text(part))
            except ExpressionError:
                parts.append(Atom(part))
        if parts:
            node = parts[0] if len(parts) == 1 else Op("AND", tuple(parts))
            return Resolved(node, "License-Classifier")
    return Resolved(Atom(UNKNOWN_ID), "none")


def web_license(text: str) -> Resolved:
    """Reading of a license string from ``pnpm licenses list``."""
    try:
        return Resolved(parse_license_text(text), "package.json")
    except ExpressionError:
        return Resolved(Atom(UNKNOWN_ID), "package.json")


def classify(
    ecosystem: Literal["python", "web"],
    name: str,
    version: str,
    resolved: Resolved,
    *,
    direct: bool,
    exceptions: Mapping[str, PolicyException] = PACKAGE_EXCEPTIONS,
) -> PackageLicense:
    """Judge one package against the allowlist and the per-package exceptions."""
    blockers = blocking_ids(resolved.node, ALLOWED)
    status: Status
    reason = ""
    if not blockers:
        status = "allowed"
    else:
        exception = exceptions.get(normalize_name(name)) if ecosystem == "python" else None
        if exception is not None and not blocking_ids(resolved.node, ALLOWED | exception.ids):
            status, reason = "exception", exception.reason
        elif UNKNOWN_ID in blockers or any(b not in KNOWN for b in blockers):
            status = "unknown"
        else:
            status = "denied"
    return PackageLicense(
        ecosystem,
        name,
        version,
        format_node(resolved.node),
        resolved.source,
        direct,
        status,
        reason,
    )


@dataclass(frozen=True)
class Workspace:
    members: frozenset[str]  # normalised names of the uv workspace members
    direct_python: frozenset[str]  # normalised names declared by members and dev groups
    direct_web: frozenset[str]  # names declared in apps/web/package.json


_REQ_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*")


def requirement_name(requirement: str) -> str | None:
    match = _REQ_NAME.match(requirement.strip())
    return normalize_name(match.group(0)) if match else None


def _read_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def load_workspace(root: Path) -> Workspace:
    """Read the declared (direct) dependencies of every workspace member and of the web app."""
    root_toml = _read_toml(root / "pyproject.toml")
    patterns: list[str] = (
        root_toml.get("tool", {}).get("uv", {}).get("workspace", {}).get("members", [])
    )
    member_files = sorted(
        {p / "pyproject.toml" for pattern in patterns for p in root.glob(pattern) if p.is_dir()}
    )
    members: set[str] = set()
    requirements: list[str] = []
    for path in [root / "pyproject.toml", *member_files]:
        if not path.is_file():
            continue
        data = _read_toml(path)
        project = data.get("project", {})
        if path != root / "pyproject.toml" and "name" in project:
            members.add(normalize_name(project["name"]))
        requirements += project.get("dependencies", [])
        for extra in project.get("optional-dependencies", {}).values():
            requirements += extra
        for group in data.get("dependency-groups", {}).values():
            requirements += [r for r in group if isinstance(r, str)]
    direct_python = {n for r in requirements if (n := requirement_name(r)) is not None} - members

    direct_web: set[str] = set()
    package_json = root / "apps" / "web" / "package.json"
    if package_json.is_file():
        data = json.loads(package_json.read_text(encoding="utf-8"))
        for key in ("dependencies", "devDependencies", "optionalDependencies"):
            direct_web |= set(data.get(key, {}))
    return Workspace(frozenset(members), frozenset(direct_python), frozenset(direct_web))


def python_packages(
    entries: Iterable[Mapping[str, Any]], workspace: Workspace
) -> list[PackageLicense]:
    """Classify pip-licenses records; workspace members are the project's own code."""
    result = []
    for entry in entries:
        name = str(entry.get("Name", ""))
        key = normalize_name(name)
        if not name or key in workspace.members:
            continue
        result.append(
            classify(
                "python",
                name,
                str(entry.get("Version", "")),
                resolve_python_license(entry),
                direct=key in workspace.direct_python,
            )
        )
    return sorted(result, key=lambda p: (normalize_name(p.name), p.version))


def web_packages(data: Mapping[str, Any], workspace: Workspace) -> list[PackageLicense]:
    """Classify ``pnpm licenses list --json``: ``{license: [{name, versions}, ...]}``."""
    result = []
    for license_text, packages in data.items():
        resolved = web_license(str(license_text))
        for package in packages:
            name = str(package.get("name", ""))
            for version in package.get("versions") or [""]:
                result.append(
                    classify(
                        "web", name, str(version), resolved, direct=name in workspace.direct_web
                    )
                )
    return sorted(result, key=lambda p: (p.name, p.version))


def package_findings(packages: Iterable[PackageLicense]) -> list[Finding]:
    findings = []
    for p in packages:
        if p.ok:
            continue
        subject = f"{p.ecosystem}:{p.name}=={p.version}" if p.version else f"{p.ecosystem}:{p.name}"
        if p.direct:
            findings.append(
                Finding(
                    "LIC-1",
                    subject,
                    f"direct dependency license {p.license} is not allowed ({p.status})",
                )
            )
        else:
            findings.append(
                Finding(
                    "LIC-R",
                    subject,
                    f"transitive license {p.license} is not on the allowlist ({p.status})",
                )
            )
    return sorted(findings)


def denylist_findings(lock_text: str) -> list[Finding]:
    """LIC-2: a denied distribution is present in uv.lock."""
    data = tomllib.loads(lock_text)
    names = {normalize_name(p.get("name", "")) for p in data.get("package", [])}
    return [
        Finding(
            "LIC-2", f"uv.lock:{name}", "denied distribution is in the lock file (decision D10)"
        )
        for name in sorted(names & set(DENIED_DISTRIBUTIONS))
    ]


# ---------------------------------------------------------------------------
# Container images
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ImageRef:
    reference: str
    source: str


@dataclass(frozen=True)
class ImageRow:
    repository: str
    references: tuple[str, ...]
    sources: tuple[str, ...]
    license: str
    note: str
    known: bool


_COMPOSE_IMAGE = re.compile(r"^\s*image:\s*[\"']?([^\s\"'#]+)")
# A top-level extension field that anchors an image reference, for example
# `x-pg-image: &pg-image pgvector/pgvector:<tag>@sha256:<digest>`. Services then say
# `image: *pg-image`; the alias itself is not an image and is skipped.
_COMPOSE_IMAGE_ANCHOR = re.compile(r"^x-[\w-]+:\s*&[\w-]+\s+[\"']?([^\s\"'#]+)")
_DOCKER_FROM = re.compile(r"^\s*FROM\s+(?:--\S+\s+)?(\S+)(?:\s+AS\s+(\S+))?\s*$", re.IGNORECASE)
_MK_IMAGE = re.compile(r"^[A-Z][A-Z0-9_]*_IMAGE\s*[?:]*=\s*(\S+)")
_PINNED_ENTRY = re.compile(r"^\s*entry:\s*(\S+@sha256:[0-9a-f]{64})")


def parse_image_ref(reference: str) -> str:
    """Repository part of an image reference: no tag, digest or Docker Hub host."""
    name = reference.split("@", 1)[0]
    last = name.rsplit("/", 1)[-1]
    if ":" in last:
        name = name[: len(name) - len(last)] + last.split(":", 1)[0]
    for prefix in ("docker.io/", "index.docker.io/", "registry-1.docker.io/"):
        if name.startswith(prefix):
            name = name[len(prefix) :]
    return name.removeprefix("library/")


def find_images(root: Path) -> tuple[list[ImageRef], bool, bool]:
    """Image references in the repo, and whether a compose file and Dockerfiles exist."""
    refs: list[ImageRef] = []

    def rel(path: Path) -> str:
        return path.relative_to(root).as_posix()

    compose = root / "docker-compose.yml"
    if compose.is_file():
        for line in compose.read_text(encoding="utf-8").splitlines():
            m = _COMPOSE_IMAGE.match(line) or _COMPOSE_IMAGE_ANCHOR.match(line)
            if m and not m.group(1).startswith("*"):
                refs.append(ImageRef(m.group(1), rel(compose)))
    dockerfiles = sorted(
        p
        for pattern in ("infra/**/*Dockerfile*", "apps/**/Dockerfile*")
        for p in root.glob(pattern)
        if p.is_file() and "node_modules" not in p.parts
    )
    for path in dockerfiles:
        stages: set[str] = set()
        for line in path.read_text(encoding="utf-8").splitlines():
            if m := _DOCKER_FROM.match(line):
                ref, alias = m.group(1), m.group(2)
                if alias:
                    stages.add(alias.casefold())
                if ref.casefold() not in stages and ref.casefold() != "scratch":
                    refs.append(ImageRef(ref, rel(path)))
    for path in sorted((root / "mk").glob("*.mk")) if (root / "mk").is_dir() else []:
        for line in path.read_text(encoding="utf-8").splitlines():
            if (m := _MK_IMAGE.match(line)) and not m.group(1).startswith("$"):
                refs.append(ImageRef(m.group(1), rel(path)))
    hooks = root / ".pre-commit-config.yaml"
    if hooks.is_file():
        for line in hooks.read_text(encoding="utf-8").splitlines():
            if m := _PINNED_ENTRY.match(line):
                refs.append(ImageRef(m.group(1), rel(hooks)))
    return refs, compose.is_file(), bool(dockerfiles)


def image_rows(refs: Iterable[ImageRef]) -> list[ImageRow]:
    by_repo: dict[str, list[ImageRef]] = defaultdict(list)
    for ref in refs:
        repository = parse_image_ref(ref.reference)
        if repository.startswith("$"):
            continue  # fully interpolated; nothing to read before it is set
        by_repo[repository].append(ref)
    rows = []
    for repository, group in sorted(by_repo.items()):
        references = tuple(sorted({r.reference for r in group}))
        sources = tuple(sorted({r.source for r in group}))
        if repository.startswith("cairn-"):
            row = ImageRow(repository, references, sources, "project code", "Built here.", True)
        elif repository in IMAGE_LICENSES:
            info = IMAGE_LICENSES[repository]
            row = ImageRow(repository, references, sources, info.license, info.note, True)
        else:
            row = ImageRow(repository, references, sources, "unknown", "Not in the table.", False)
        rows.append(row)
    return rows


def image_findings(rows: Iterable[ImageRow]) -> list[Finding]:
    return [
        Finding(
            "LIC-3", row.repository, "image has no entry in IMAGE_LICENSES (scripts/licenses.py)"
        )
        for row in rows
        if not row.known
    ]


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ").strip()


def _table(header: Sequence[str], rows: Iterable[Sequence[str]]) -> list[str]:
    lines = [
        "| " + " | ".join(header) + " |",
        "|" + "|".join("---" for _ in header) + "|",
    ]
    lines += ["| " + " | ".join(_cell(c) for c in row) + " |" for row in rows]
    return lines


def _direct_table(packages: Sequence[PackageLicense]) -> list[str]:
    rows = [
        (
            f"`{p.name}`",
            p.version,
            p.license,
            p.source,
            p.status + (f" ({p.reason})" if p.reason else ""),
        )
        for p in packages
        if p.direct
    ]
    return _table(("Package", "Version", "License", "Read from", "Status"), rows)


def _transitive_table(packages: Sequence[PackageLicense]) -> list[str]:
    groups: dict[str, list[PackageLicense]] = defaultdict(list)
    for p in packages:
        if not p.direct:
            groups[p.license].append(p)
    rows = [
        (
            license_text,
            str(len(group)),
            group[0].status,
            ", ".join(sorted({p.name for p in group}, key=str.casefold)),
        )
        for license_text, group in sorted(groups.items())
    ]
    return _table(("License", "Count", "Status", "Packages"), rows)


def render(
    *,
    python: Sequence[PackageLicense],
    web: Sequence[PackageLicense],
    images: Sequence[ImageRow],
    compose_present: bool,
    dockerfiles_present: bool,
    findings: Sequence[Finding],
) -> str:
    """The text of docs/licenses.md. Deterministic: no dates, sorted rows."""
    out: list[str] = [
        "# Licenses",
        "",
        "Generated by `make licenses` (`scripts/licenses.py`). Do not edit by hand: the next run "
        "overwrites this file. The policy below is the one the script enforces.",
        "",
        "The Cairn repository's own license is not chosen yet (user step U6).",
        "",
        "## Policy",
        "",
        "- Allowed for direct dependencies: " + ", ".join(sorted(ALLOWED)) + ".",
        "- `A AND B` needs both allowed; `A OR B` needs one of them allowed.",
        "- Denied in `uv.lock` (decision D10): "
        + ", ".join(f"`{n}`" for n in DENIED_DISTRIBUTIONS)
        + ".",
        "- Transitive packages outside the allowlist are listed for review and never fail the run.",
        "- Container images need an entry in the image table. AGPL-3.0 is accepted only for the "
        "Grafana images.",
        "",
        "### Per-package exceptions",
        "",
        *_table(
            ("Package", "Extra license ids", "Reason"),
            [
                (f"`{name}`", ", ".join(sorted(e.ids)), e.reason)
                for name, e in sorted(PACKAGE_EXCEPTIONS.items())
            ],
        ),
        "",
        "## Result",
        "",
    ]
    failing = [f for f in findings if f.fails]
    review = [f for f in findings if not f.fails]
    out.append(
        f"{'Failed' if failing else 'Passed'}: {len(failing)} finding(s) that fail the run, "
        f"{len(review)} transitive package(s) to review. "
        f"Python packages: {len(python)} ({sum(p.direct for p in python)} direct). "
        f"Web packages: {len(web)} ({sum(p.direct for p in web)} direct). "
        f"Container images: {len(images)}."
    )
    if findings:
        out += [
            "",
            *_table(
                ("Rule", "Subject", "Message"),
                [(f.rule, f"`{f.subject}`", f.message) for f in findings],
            ),
        ]
    out += [
        "",
        "## AGPL-3.0 components",
        "",
        "Grafana OSS is the only AGPL-3.0 image. It belongs to the optional `obs` profile and runs "
        "as a separate, unmodified container; Cairn code does not link to it. Lexical search "
        "uses native PostgreSQL full-text search, so no AGPL component is in the database "
        "or the request path. Obligations that follow:",
        "",
        "- Publish any change made to Grafana (brainstorm risk 11).",
        "- Leave Grafana out of any closed hosted demo; the free-tier deployment does not use it.",
        "",
        "`psycopg` is LGPL-3.0 and is imported as an unmodified library.",
        "",
        "## Stack components outside the lock files",
        "",
        *_table(("Component", "License", "Where"), STACK_COMPONENTS),
        "",
        "## Hosted services and models",
        "",
        "The free-tier deployment (ADR-0014) runs on accounts the user holds. Their terms govern "
        "use; nothing here is shipped in the repository or an image.",
        "",
        *_table(("Service", "Terms", "Used for"), HOSTED_SERVICES),
        "",
        "### Default Workers AI models",
        "",
        "Cloudflare hosts these models and does not train them. The license is the publisher's "
        "(read from the model card, 3 Nov 2026). Cloudflare states that it does not use Workers "
        "AI customer content to train models and does not retain inputs or outputs of standard "
        "requests; the corpus is synthetic either way (decision D20).",
        "",
        *_table(("Model", "License", "Source", "Role"), DEFAULT_MODELS),
        "",
        "## Container images",
        "",
    ]
    out.append(
        "`docker-compose.yml` is present; its images are listed below."
        if compose_present
        else "`docker-compose.yml` does not exist yet (task 4.1), so the compose services are not "
        "listed. Images below come from the Makefile fragments (`*_IMAGE` variables) and the "
        "digest-pinned pre-commit hook."
    )
    if not dockerfiles_present:
        out[-1] += " No Dockerfile exists yet (tasks 4.1 and 7.2)."
    out += [
        "",
        *_table(
            ("Repository", "Reference", "License", "Found in", "Note"),
            [
                (
                    f"`{r.repository}`",
                    "<br>".join(f"`{x}`" for x in r.references),
                    r.license,
                    ", ".join(f"`{s}`" for s in r.sources),
                    r.note,
                )
                for r in images
            ],
        ),
        "",
        "## Python dependencies",
        "",
        "Source: `pip-licenses --from=all` over the locked environment. Workspace members "
        "(`cairn-*`) are the project's own code and are not listed.",
        "",
        "### Direct",
        "",
        *_direct_table(python),
        "",
        "### Transitive, by license",
        "",
        *_transitive_table(python),
        "",
        "## Web dependencies",
        "",
        "Source: `pnpm licenses list --json` in `apps/web`.",
        "",
        "### Direct",
        "",
        *_direct_table(web),
        "",
        "### Transitive, by license",
        "",
        *_transitive_table(web),
        "",
    ]
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def analyse(
    root: Path, python_entries: Sequence[Mapping[str, Any]], web_data: Mapping[str, Any]
) -> tuple[str, list[Finding]]:
    """The report text and every finding for a repo root plus the collected license data."""
    workspace = load_workspace(root)
    python = python_packages(python_entries, workspace)
    web = web_packages(web_data, workspace)
    refs, compose_present, dockerfiles_present = find_images(root)
    images = image_rows(refs)
    lock = root / "uv.lock"
    findings = [
        *package_findings(python),
        *package_findings(web),
        *(denylist_findings(lock.read_text(encoding="utf-8")) if lock.is_file() else []),
        *image_findings(images),
    ]
    findings.sort()
    text = render(
        python=python,
        web=web,
        images=images,
        compose_present=compose_present,
        dockerfiles_present=dockerfiles_present,
        findings=findings,
    )
    return text, findings


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--python-json", type=Path, required=True, help="pip-licenses JSON file")
    parser.add_argument("--web-json", type=Path, required=True, help="pnpm licenses JSON file")
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help="repo root")
    parser.add_argument(
        "--out", type=Path, default=DEFAULT_OUT, help="report path, relative to --root"
    )
    args = parser.parse_args(argv)
    try:
        python_entries = json.loads(args.python_json.read_text(encoding="utf-8"))
        web_data = json.loads(args.web_json.read_text(encoding="utf-8"))
        if not isinstance(python_entries, list) or not isinstance(web_data, dict):
            raise ValueError("expected a JSON list of packages and a JSON object of licenses")
        text, findings = analyse(args.root, python_entries, web_data)
    except (OSError, ValueError, tomllib.TOMLDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    out = args.root / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    for finding in findings:
        print(("error: " if finding.fails else "review: ") + str(finding), file=sys.stderr)
    failing = [f for f in findings if f.fails]
    if failing:
        print(f"licenses: {len(failing)} finding(s) fail the policy; report: {args.out}")
        return 1
    review = len(findings) - len(failing)
    print(f"licenses: ok ({review} transitive package(s) to review); report: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
