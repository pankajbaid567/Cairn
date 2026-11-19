# Task 11.1 notes: audit, secrets, licenses and actionlint targets

Date: 27 Oct 2026. No dependency, `uv.lock`, root `pyproject.toml`, `conftest.py` or other Make fragment touched (`uv lock --check` passes). `make audit secrets licenses` and `uv run pytest tests/scripts/test_licenses.py` (59 tests) pass; `make fmt-check lint types` pass.

## Files

| File | State |
|---|---|
| `mk/security.mk` | `audit` (`audit-python`, `audit-web`), `secrets`, `licenses`, `actionlint`; image pins for gitleaks and actionlint |
| `scripts/licenses.py` | classifier, policy constants, image table, report writer; stdlib only; exit 0 ok, 1 findings, 2 bad input |
| `tests/scripts/test_licenses.py` | 59 unit tests (expressions, record reading, package status, workspace and lock, images, report and exit codes) |
| `.gitleaks.toml` | default rules plus two path allowlists (see below) |
| `docs/licenses.md` | generated; deterministic (no dates), so a re-run with the same inputs gives no diff |
| `docs/evidence/phase-0/verify/V7.md`, `V8.md` | V7 resolved; the `pnpm audit` row of V8 finished |

## Pinned images (resolved 27 Oct 2026)

| Tool | Reference |
|---|---|
| gitleaks | `ghcr.io/gitleaks/gitleaks:v8.30.1@sha256:c00b6bd0aeb3071cbcb79009cb16a60dd9e0a7c60e2be9ab65d25e6bc8abbb7f` |
| actionlint | `rhysd/actionlint:1.7.12@sha256:b1934ee5f1c509618f2508e6eb47ee0d3520686341fec936f3b79331f9315667` |

Both digests are multi-arch index digests, and both tags are the latest release. The gitleaks reference is not written in `mk/security.mk`: `GITLEAKS_IMAGE` is read from the `entry:` line of the gitleaks hook in `.pre-commit-config.yaml`, so there is one source of truth. The target stops with exit 2 if that line no longer has a digest-pinned gitleaks image.

## Targets

| Target | What it does | Fails when |
|---|---|---|
| `audit-python` | `uv export --frozen --all-packages --no-emit-workspace` (hashed requirements, temp file), then `pip-audit --require-hashes --disable-pip --strict` | any advisory (any severity; pip-audit has no severity cut), or the advisory service is unreachable (`--strict`) |
| `audit-web` | `pnpm audit --audit-level high` on the web runner of `mk/web.mk` (Node 24 container, or `WEB_RUNNER=host`) | a high or critical advisory, or an endpoint error |
| `secrets` | pinned gitleaks `dir /repo`, `--config .gitleaks.toml --redact --no-banner --verbose`, repo mounted read-only | any finding (exit 1); values print as `REDACTED` |
| `licenses` | `pip-licenses --from=all --format=json`, `pnpm licenses list --json` (after a frozen install), then `scripts/licenses.py` | LIC-1, LIC-2 or LIC-3 (below) |
| `actionlint` | pinned actionlint on `.github/workflows/*.yml` and `*.yaml`, files passed by name | any finding, or no workflow file exists |

`make audit` ran clean on both sides: pip-audit and pnpm audit each printed "No known vulnerabilities found". Control runs: pip-audit on a hashed `urllib3==1.26.4` requirement exited 1 with 20 advisories; pnpm audit on a scratch project with `lodash@4.17.20` exited 1 with a high advisory.

## V7 and V8 results

- V7: `dir` and `--redact` exist in gitleaks v8.30.1. Details and the actionlint findings are in `verify/V7.md`.
- V8, audit part: the `pnpm audit` endpoint works with pnpm 10.8.1 in the pinned image. The osv-scanner fallback is documented in `verify/V8.md` but not wired in, since it was not needed.

## Licenses

Rules in `scripts/licenses.py`:

| Rule | Meaning | Fails the run |
|---|---|---|
| LIC-1 | a direct Python or web dependency has a license outside the allowlist, or an unknown one | yes |
| LIC-2 | `celery`, `kombu` or `redis` is in `uv.lock` | yes |
| LIC-3 | a container image has no entry in `IMAGE_LICENSES` | yes |
| LIC-R | a transitive package is outside the allowlist | no, listed for review |

- Allowlist: MIT, BSD-2-Clause, BSD-3-Clause, Apache-2.0, ISC, PSF-2.0, MPL-2.0. SPDX `AND` needs both sides allowed, `OR` needs one, `AND` binds tighter.
- License source order for Python: PEP 639 expression, then short known `License` metadata (full license text in that field is ignored), then trove classifiers (several classifiers count as AND, the strict reading).
- Direct dependencies are read from the member `pyproject.toml` files, the root dependency groups and `apps/web/package.json`. Workspace members (`cairn-*`) are the project's own code and are not judged; the repo license is still open (U6).
- Result today: 105 Python packages (30 direct), 236 web packages (21 direct), no failing finding. Six transitive web packages are listed for review: `@csstools/color-helpers` and `@csstools/css-syntax-patches-for-csstree` (MIT-0), `caniuse-lite` (CC-BY-4.0), `lru-cache` and `minimatch` (BlueOak-1.0.0), `mdn-data` (CC0-1.0). All are permissive or data licenses. I did not trace which direct dependency pulls each in. No action taken.
- `docs/licenses.md` carries the policy, the exceptions table, the AGPL-3.0 section (ParadeDB `pg_search` and Grafana: separate unmodified services, keep the repo open source, publish any change, native FTS fallback, not on Neon), `psycopg` LGPL-3.0, the stack components that no lock file lists, the image table, and the direct and transitive tables.
- Image table: built from `docker-compose.yml` and Dockerfiles when they exist, plus the `*_IMAGE` variables in `mk/*.mk` and the digest-pinned pre-commit hook. Today there is no compose file or Dockerfile, and the report says so in its "Container images" section. Three images are listed (gitleaks, node, actionlint).

### Allowlist entries and why

| Entry | Reason |
|---|---|
| `psycopg`, `psycopg-binary`: LGPL-3.0-only or later | Design allows LGPL-3.0 for psycopg only. Unmodified library import. `psycopg-binary` is the compiled wheel of the same project (the `binary` extra) |
| `numpy`: 0BSD, Zlib, CC0-1.0 | numpy's license expression is `BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0`. The extra three are permissive licenses of components bundled in its wheels. **Not in the design's allowlist; added as a per-package exception so the allowlist itself stays as designed.** Python packages only, and it stops applying if numpy adds any other license |
| `.gitleaks.toml`: `.env` | The local `.env` written by `make env` holds random generated passwords, which the default `generic-api-key` rule flags (5 findings, redacted). It is gitignored and in `.dockerignore`. Without this entry `make secrets` would fail on every machine after `make env` |
| `.gitleaks.toml`: generated directories | `.git`, `.venv`, `node_modules`, `.pnpm-store`, `__pycache__`, tool caches, `dist`, `var`. All gitignored |

`.env.example` is not allowlisted and is not flagged. The scan does not print `.env` content and does not need to read it for the gate.

## Deviations from the design

| Design | Done here | Why |
|---|---|---|
| Allowlist is MIT, BSD-2/3, Apache-2.0, ISC, PSF-2.0, MPL-2.0, LGPL-3.0 (psycopg only) | Same, plus the numpy exception above and the `psycopg-binary` exception | numpy's real expression needs it; `psycopg-binary` comes with the `binary` extra |
| Task text: "`.gitleaks.toml` may allowlist `.env.example` placeholders only" | `.env.example` needs no entry; `.env` and ignored directories are skipped by path | See the table above. `.env` would otherwise fail every local run |
| `licenses.py` reads pip-licenses, `pnpm licenses` and the image table | Inputs arrive as two JSON files from the Makefile; the script never runs pip-licenses, pnpm or docker itself | Keeps the judging pure and testable |
| `pip-audit` on a hashed export with `--no-deps` | `--no-deps` dropped, `--disable-pip` and `--require-hashes` kept | pip-audit warns that `--no-deps` is redundant with `--require-hashes` |
| `actionlint` target | Fails (exit 1, clear message) when `.github/workflows` has no workflow file | A missing CI workflow must not count as a pass. Task 11.2 adds `ci.yml` |
| actionlint "pinned image" | Files passed by name, not by directory discovery | The image exits 3 without a git project root when given no files (V7) |

## Notes for later tasks

- 11.2: the CI `security` job calls `make audit secrets`, `lint-config` calls `make actionlint`, `python` calls `make licenses`. `make actionlint` passes the workflow files by name, so it works before `git init`. In CI, set `WEB_RUNNER=host` for `audit` and `licenses` (the host mode runs `cd apps/web && pnpm audit ...` and `pnpm install --frozen-lockfile && pnpm licenses list --json`, so Node 24 and pnpm 10.8.1 must be set up first). `make secrets` needs Docker, which `ubuntu-24.04` has; with `fetch-depth: 0` the design also wants a history scan, which is a different command (`gitleaks git`), not what `make secrets` does. `make secrets` scans the checked-out tree only. If the job wants history, add a second target or a direct `docker run ... git` step with the same `GITLEAKS_IMAGE`.
- 11.2: I did not verify whether actionlint 1.7.12 checks that actions are pinned by commit SHA (Requirement 10.3). Do not rely on it; add a small test over `ci.yml` for the `uses:` lines.
- 11.2: `pip-audit` needs outbound network to PyPI and `pnpm audit` to the npm registry. A flaky advisory service fails the job; re-run it.
- 17.2: put `audit secrets licenses actionlint` into `gates`. `make actionlint` fails until `.github/workflows/ci.yml` exists, so `gates` cannot pass before 11.2. `make licenses` rewrites `docs/licenses.md`; run it before `docs-check` so the links group sees the file. The `licenses` target installs web dependencies itself (`pnpm install --frozen-lockfile`, output to stderr), so it does not need `make web-check` first; a cold web volume makes it slower.
- 17.2: `make secrets` takes about 3 seconds and needs Docker running. It scans the whole repo directory, including files other agents are writing; a finding in an in-progress file shows up here.
- 4.1, 7.2 and later image tasks: add any new image repository to `IMAGE_LICENSES` in `scripts/licenses.py` (with its license, read from the project, not guessed), or `make licenses` fails with LIC-3. Entries for ParadeDB, Grafana, the OTel Collector, Prometheus, nginx-unprivileged, node, python and the uv image already exist. Images named `cairn-*` count as project code. A compose `image:` value that is fully interpolated (`${X}`) is skipped. Pin every image by tag and digest (Requirement 21.3); the script does not check compose pins, because the design's compose file builds the reference from `${PARADEDB_TAG}@${PARADEDB_DIGEST}`.
- Any task that adds a dependency: `make licenses` must stay green. A new direct dependency with a license outside the allowlist needs a reasoned `PACKAGE_EXCEPTIONS` entry, which shows up in `docs/licenses.md`.
