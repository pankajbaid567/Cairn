# Task 1.1: platform facts and the research Verify items

Date: 2 Nov 2026. Requirements 1.2 and 22.2; plan items 0.4, 6.3, D5, D18.

## Files written

- [README.md](README.md): index of this directory.
- [verify/FV0-platform-facts.md](verify/FV0-platform-facts.md): every fact of the design table, paraphrased, with its source URL. One drift since 30 Oct: the pricing page now lists `bge-small-en-v1.5` at 1,841 neurons per M input tokens, the design has 1,836 (for FV9, task 8.3).
- [verify/FV1.md](verify/FV1.md): pin `pgvector/pgvector:0.8.6-pg18@sha256:2ba9ca5f2e7daa0f0e7723cba1ee9167bab54efd3640516a44ac1a928dd67e7a`. Neon lists pgvector 0.8.6 for PG18; the newest tag is `0.8.7-pg18`, not used, so local matches Neon. Both images carry PostgreSQL 18.6.
- [verify/FV2.md](verify/FV2.md): PostgreSQL 18 is GA on Neon (1 May 2026) and `aws-ap-southeast-1` has Postgres with no plan restriction found. OQ-2 does not apply.
- [verify/FV3.md](verify/FV3.md): Neon's Manage roles page says hashed passwords are not supported (OQ-1 applies), and `vector` is not a trusted extension, so the `SET ROLE cairn_owner` path of Requirement 5.8 is doubtful on Neon and on `ft-db`.

Content from the sources was rephrased for compliance with licensing restrictions.

## Docker state before the retarget (names only, for task 14.1)

Captured 2 Nov 2026 08:5x IST with `docker ps` and `docker volume ls`. Nothing of another project was inspected, stopped or removed.

Running containers:

| Container | Compose project |
|---|---|
| `cairn-api-1` | cairn |
| `cairn-db-1` | cairn |
| `cairn-web-1` | cairn |
| `cairn-worker-1` | cairn |
| `openwa-api` | openwa |

Volumes of the `cairn` project: `cairn_grafana-data`, `cairn_pgdata`, `cairn_prometheus-data`. Two more volumes carry a `cairn-` prefix but no compose project label: `cairn-web-cache`, `cairn-web-node-modules`.

Volumes of other projects (names only):

- ecosphere: `ecosphere_odoo-web-data`, `ecosphere_pgdata`
- hireflow-dashboard-check: `hireflow-dashboard-check_application_data`, `hireflow-dashboard-check_postgres_data`
- hireflow-issue26-check: `hireflow-issue26-check_application_data`, `hireflow-issue26-check_postgres_data`
- inculcate_studio_v1-: `inculcate_studio_v1-_mongo_data`, `inculcate_studio_v1-_postgres_data`
- infra: `infra_minio_data`, `infra_postgres_data`
- og: `og_minio_data`, `og_mongodb_data`, `og_postgres_data`, `og_redis_data`
- razorpay_hackathon: `razorpay_hackathon_clearledger_pgdata`, `razorpay_hackathon_clearledger_uploads`
- reliable_ai_agent_multi-step_task: `reliable_ai_agent_multi-step_task_redis-data`
- resilient_taskflow: `resilient_taskflow_grafana_data`, `resilient_taskflow_postgres_data`, `resilient_taskflow_prometheus_data`, `resilient_taskflow_redis_data`
- scholarforge_ai: `scholarforge_ai_chroma_data`, `scholarforge_ai_postgres_data`, `scholarforge_ai_redis_data`
- technova: `swachhLens_hf_cache`, `swachhLens_minio_data`, `swachhLens_postgres_data`

Anonymous volumes (no project label), 9 in total: `050fc48c7eb8…`, `066cd50fd390…`, `24510fc05e49…`, `45a03dd7ef61…`, `46f867cb5009…`, `6904c6f0a8d7…`, `9372d4e3ea29…`, `a194c13250c5…`, `d52ef0399855…` (first 12 hex characters of each name).

`cairn_pgdata`: exists (local driver, created 27 Oct 2026 14:43 UTC). It is mounted by `cairn-db-1` (the ParadeDB-era `db`, up 14 hours), which is expected before task 2.1 switches `db` to the new volume. From task 2.3 on, `docker ps --filter volume=cairn_pgdata` must be empty and the volume must still exist (FU7 removes it).

Note: the Docker daemon socket did not answer at the start of the task; `open -a Docker` brought it back. The running containers above were already up.

## Verify

`test -f docs/evidence/free-tier/verify/FV0-platform-facts.md && test -f docs/evidence/free-tier/verify/FV1.md && grep -q 'https://' docs/evidence/free-tier/verify/FV0-platform-facts.md && docker volume inspect cairn_pgdata >/dev/null`

Result: see the end of this file.

## User decision on OQ-1 (2 Nov 2026)

FV3 item 2 (Neon accepts no hashed password) triggered OQ-1. The user chose **option 1**: in managed mode (Neon), bootstrap sends role passwords as plaintext over a `verify-full` TLS connection, only with the explicit `cairn-db init --allow-plaintext-password` flag. Superuser mode keeps SCRAM-SHA-256 verifiers; managed mode without the flag (`ft-db`) does too. The flag is refused in superuser mode and with any other sslmode. `make remote-init` passes it (task 9.3).

FV3 item 7 (pgvector is not a trusted extension) is resolved in task 4.3: on Neon, managed mode creates `vector` and `btree_gist` as the admin (`neondb_owner`, via `neon_superuser`) directly in `cairn` instead of under `SET ROLE`; on `ft-db`, the image superuser pre-creates both extensions in `template1` in `ft-admin-init` (`cairn-db emulate-managed-admin`), so managed-mode bootstrap finds them, emulating Neon's extension privilege.

Updated with this decision: [verify/FV3.md](verify/FV3.md), the retarget design (§2, OQ-1, the remote bootstrap diagram, §14), Requirements 5.8, 5.9 and 5.11, and the bullets of tasks 4.3, 6.1 and 9.3.
