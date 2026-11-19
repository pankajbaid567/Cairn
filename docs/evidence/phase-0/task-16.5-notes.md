# Task 16.5 notes: CONTRIBUTING.md

Date: 27 Oct 2026. Only `CONTRIBUTING.md` and this file were created.

## Sections

Setup and quick start (prerequisites, first run, targets that exist today), Repo layout, Definition of done, Quality gates, AI-assisted workflow, ADR process, Data rules, User-owned steps (U1 to U7). Requirements 19.3 and 23.3.

## Make targets referenced

Existing in `mk/` at the time of writing: `env`, `fmt`, `fmt-check`, `lint`, `types`, `test-unit`, `web-check`, `web-fmt`, `web-lock`, `web-clean`, `scenario-validate`, `audit`, `secrets`, `licenses`, `actionlint`, `docs-check`, `help`.

Marked "(pending)" in the text because no fragment defines them yet: `test-integration`, `migrations-check`, `matrix`, `compose-check`, `gates`, `phase0-exit`. Once they land, remove the "(pending)" marks and the "arrive with later Phase 0 tasks" sentence.

Setup uses `uv sync --locked --all-packages` directly because there is no `bootstrap` target yet.

## Not verified

- The Quality gates table lists `scenario-validate` and `actionlint` as failing until task 12.6 and the CI workflow exist. That comes from the `mk` help text and the absence of `.github/workflows/`, not from a run.
- `docs/evidence/phase-0/github-org.md` (U3) is named from the design and does not exist yet.
- Layout rows for `infra` and `apps/*` describe the design; `infra/` is empty today.
- AGPL wording in U6 is a pointer to Verify, not legal advice.

## Verification

```text
$ make docs-check
docs-check: ok (prd, adr, threat-model, acceptance, contributing, links)
```
