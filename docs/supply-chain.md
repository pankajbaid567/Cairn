# Supply chain: pins, SBOM and audit findings

Checked 2026-11-04. Commands: `make audit`, `make licenses`, `make sbom`, `make secrets`.

## Pins

* **Python:** `uv.lock` records a SHA-256 hash for every artifact; CI and the images install with `uv sync --locked`, and `make audit-python` exports the lock as hashed requirements and runs `pip-audit --require-hashes`. Direct dependencies in each `pyproject.toml` are pinned with `==`.
* **Web and the Pages Functions:** `apps/web/pnpm-lock.yaml` records an integrity hash for every package, and installs use `--frozen-lockfile`. The Functions (`functions/`) use only standard Fetch and Web Crypto APIs and add no runtime package.
* **Images and tools:** base images, the gitleaks and actionlint tools, and Postgres carry digests (a Make variable ending in `_IMAGE` must have one; a test enforces it).

## SBOM

`make sbom` writes CycloneDX 1.5 files from the lock files alone (no install, no network):

| File | Contents |
|---|---|
| `docs/evidence/phase-6/sbom-python.cdx.json` | every locked Python package (116), workspace members excluded |
| `docs/evidence/phase-6/sbom-web.cdx.json` | every locked npm package (259) with its SHA-512 integrity hash |

## Audit findings

| Date | Tool | Result | Disposition |
|---|---|---|---|
| 2026-11-04 | `pip-audit` over the hashed export | no known vulnerabilities | none needed |
| 2026-11-04 | `pnpm audit` | 1 moderate: `minimatch` < 11.0.1, denial of service through crafted glob patterns (`eslint > minimatch`) | **Documented, not fixed.** It is in a build-time lint tool that never ships and never sees untrusted patterns. The gate fails on high and critical findings, so this does not block. Re-check when ESLint updates its dependency |
| 2026-11-04 | `make licenses` | ok; 8 transitive npm packages under permissive licences outside the allowlist (BlueOak-1.0.0, CC0-1.0) listed for review in [licenses.md](licenses.md) | Reviewed: both are permissive and compatible; nothing copyleft |
| 2026-11-04 | New dependencies this phase | `cryptography` 50.0.2 (GitHub App JWT signing), `duckdb` 2.0.0 (results tables), `httpx` (MCP server), `click` | Pinned in the lock with hashes; no advisories at the time of the audit |

## What is not covered

* The container base images are scanned only by the digest pin, not by an image scanner.
* `make secrets-history` needs a git checkout; this tree is not a repository, so the history scan runs in CI once the repository is pushed.
