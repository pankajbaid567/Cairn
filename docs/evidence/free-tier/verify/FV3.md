# FV3: what `neondb_owner` can do

Resolved from Neon's documentation on 2 Nov 2026 (task 1.1); the PostgreSQL 18 rules behind each item are FV4 (task 4.3, proved on `ft-db` in task 6.2). Content was rephrased for compliance with licensing restrictions. Parts the documentation cannot settle are marked **FU2**: the user confirms them when running `make remote-init` and `make remote-status` (design §14).

## Background from the docs

- `neondb_owner` is a member of `neon_superuser` (NOLOGIN) and inherits CREATEDB, CREATEROLE, BYPASSRLS and REPLICATION, plus `pg_read_all_data`, `pg_write_all_data`, `pg_monitor`, `pg_signal_backend`, `pg_maintain` and (PG18) `pg_signal_autovacuum_worker`. It is not a superuser; no Neon user can connect as the Postgres superuser. Source: [Neon docs: Manage roles](https://neon.com/docs/manage/roles).
- Roles created with SQL are not members of `neon_superuser`; they get only the default `public` schema privileges. Sources: [Manage roles](https://neon.com/docs/manage/roles), [Manage database access](https://neon.com/docs/manage/database-access).
- `neon_superuser` is the role that carries the right to create databases, roles and extensions in Neon. Source: [Neon release notes: storage and compute, 2023](https://neon.com/docs/release-notes/storage-and-compute).
- Limits: 500 roles and 500 databases per branch; reserved role names (`pg_*`, `neon_superuser`, `cloud_admin`, `zenith_admin`, `public`, `none`) do not clash with `cairn_owner`, `cairn_app`, `cairn_dbos`.

## Items

| # | Item (design FV3) | Finding from the docs | Status |
|---|---|---|---|
| 1 | `CREATE ROLE … LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS` (plus `NOINHERIT` for `cairn_app`) as `neondb_owner` | Supported: Neon documents `CREATE ROLE` from SQL by the project role, and its RLS guide asks for exactly this kind of LOGIN role without BYPASSRLS. The docs do not list every attribute clause; under PG16+ rules a CREATEROLE non-superuser may only grant attributes it holds, and all clauses here are `NO…` except LOGIN and INHERIT (FV4) | Expected to work. **FU2** confirms |
| 2 | Passwords sent as SCRAM-SHA-256 verifiers (Requirement 5.9) | **Not supported.** The Manage roles page says passwords must be supplied in plain text (Neon encrypts them when stored) and hashed passwords are not supported | **Blocked Requirement 5.9 on Neon. OQ-1 applied; the user chose option 1 on 2 Nov 2026 (see below)**; Requirement 5.9 now carries the decision |
| 3 | Neon password rules | A password needs at least 60 bits of entropy. The guidance: at least 12 characters, mixed lower case, upper case, digits and symbols, no sequences, dictionary words or a character repeated more than twice in a row. The generated Cairn secrets must pass this check on Neon | Rule recorded; whether the generator's output passes is checked by **FU2** (Neon rejects a weak password with an error) |
| 4 | `GRANT cairn_owner, cairn_dbos TO neondb_owner WITH INHERIT FALSE, SET TRUE` | Not covered by Neon's docs. Under PG16+ a CREATEROLE role receives ADMIN OPTION on roles it creates, which allows this grant to itself (FV4). The "transfer table ownership" recipe on [Manage databases](https://neon.com/docs/manage/databases) uses role membership grants by `neondb_owner`, so membership grants are allowed in general | Expected to work. **FU2** confirms |
| 5 | `CREATE DATABASE cairn OWNER cairn_owner` and `cairn_dbos OWNER cairn_dbos` | Supported: CREATE DATABASE from SQL takes the standard parameters except TABLESPACE. With `OWNER` a non-superuser must be able to SET ROLE to the new owner (PG16+), which item 4 provides | Expected to work. **FU2** confirms |
| 6 | A second database `cairn_dbos` in the same project | Supported: up to 500 databases per branch; all databases on a branch share its compute, so no extra compute is spent | Resolved from docs |
| 7 | `vector` and `btree_gist` created by the database owner (`SET ROLE cairn_owner`, Requirement 5.8) | **Doubtful.** Neon ties extension creation to `neon_superuser`, and `SET ROLE cairn_owner` drops those privileges. Upstream, `btree_gist` is a trusted extension (any database owner with CREATE may install it), but pgvector 0.8.6 is not: its `vector.control` has no `trusted = true` line (checked at `github.com/pgvector/pgvector`, tag `v0.8.6`), so a non-superuser cannot create it on plain PostgreSQL. This also affects the local emulation: on `ft-db`, `cairn_admin` is a non-superuser, so it cannot create `vector` with or without `SET ROLE` | **Resolution decided 2 Nov 2026, implemented in task 4.3** (see "Item 7: how task 4.3 resolves it" below). **FU2** confirms the Neon side |
| 8 | Neon refuses ALTER of a role it manages | Not relevant: Cairn only alters roles it created. Neon forbids altering `neon_superuser` itself or making it a database owner | Resolved from docs |

## Question asked of the user (OQ-1)

Item 2 meets the condition in tasks.md ("If FV2 or FV3 shows that Neon free … refuses SCRAM verifiers, stop and ask the user"). The design's options (OQ-1):

1. Plaintext password over `verify-full` TLS, only behind an explicit `--allow-plaintext-password` flag (managed mode only; superuser mode keeps verifiers). Neon stores it encrypted; the password then sits in the server's statement text for that one statement.
2. The user sets the role passwords through the Neon API or Console; bootstrap creates the roles without a password. Caution from the docs: the roles must still be created with SQL, because roles created through the Console, CLI or API join `neon_superuser` (BYPASSRLS), which Requirement 6.4 forbids. Only the password reset may go through the Console or API, and that generates the value (Neon picks it).

The answer is recorded in [task-1.1-notes.md](../task-1.1-notes.md).

### Decision (2 Nov 2026)

The user chose **option 1**. In managed mode (Neon), bootstrap sends role passwords as plaintext over a `verify-full` TLS connection, and only when `cairn-db init` gets the explicit `--allow-plaintext-password` flag. The flag is refused (exit 2, before connecting) in superuser mode and with any sslmode other than `verify-full`. Superuser mode keeps SCRAM-SHA-256 verifiers, and so does managed mode without the flag (the `ft-db` emulation, whose plain PostgreSQL server accepts verifiers). `make remote-init` passes the flag (task 9.3). No statement, password or verifier is logged. Option 2 (passwords set through the Neon API or Console) was not chosen.

Recorded in: design §2 ("Role passwords in managed mode") and Open Questions OQ-1, Requirement 5.9, tasks 4.3 and 9.3, and [task-1.1-notes.md](../task-1.1-notes.md).

## Item 7: how task 4.3 resolves it

Decided 2 Nov 2026 with the OQ-1 answer; implemented in task 4.3, wired into the freetier profile in task 6.1.

- On Neon, managed mode creates `vector` and `btree_gist` as the admin (`neondb_owner`, which may create extensions through `neon_superuser`) directly in `cairn` and in each test or spike database, not under `SET ROLE`. Only the `REVOKE CREATE ON SCHEMA public FROM PUBLIC` runs under `SET ROLE <owner>`. If plain PostgreSQL shows that the admin also needs CONNECT or CREATE on the database or on `public`, bootstrap grants that under `SET ROLE <owner>` first and revokes the CREATE grants afterwards (FV4 records it).
- On `ft-db`, `cairn_admin` cannot create `vector` at all. `cairn-db emulate-managed-admin` (in `ft-admin-init`, as the image superuser) pre-creates `vector` and `btree_gist` in `template1`, so every database created later has them and managed-mode bootstrap's `CREATE EXTENSION IF NOT EXISTS` finds them. This emulates Neon's extension privilege. Because those statements are no-ops on `ft-db`, only **FU2** exercises the Neon extension path.

Recorded in: design §2 ("Extensions in managed mode"), Requirements 5.8 and 5.11, tasks 4.3 and 6.1.
