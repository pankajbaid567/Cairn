---
status: accepted
date: 2026-11-19
decision-makers: Pankaj Baid
---

# ADR-0019: Multi-tenant SaaS on one database: restrictive row-level security, Cognito sign-in, roles in the database, and a hash-chained audit log with external anchors

Extends [ADR-0004](ADR-0004-layered-authorization.md) (layered authorisation, row-level security by principal) and [ADR-0018](ADR-0018-aws-deployment-neon-database-bedrock-models.md) (deployment). The product plan is `docs/product/saas-plan.md`.

## Context and Problem Statement

Cairn was a single-organisation demo with a "choose a persona" cookie. A product needs real accounts, many organisations in one deployment, roles and approvals that cannot be bypassed from the application, and an audit trail an auditor can trust. The free-tier budget (one small host, one Neon database) rules out a database per customer.

## Considered Options

* **Tenancy:** a database or schema per organisation; an `org_id` column filtered in application queries; an `org_id` column with a database-enforced policy on every table.
* **Identity:** build password handling in PostgreSQL; Amazon Cognito user pool; an external provider such as Auth0.
* **Authorisation:** roles checked only in the API; roles also enforced in database functions and policies.
* **Audit integrity:** an append-only table; a hash chain; a hash chain plus anchors outside the database.

## Decision Outcome

* **One database, an `org_id` on every table, and a RESTRICTIVE row-level-security policy on each** (migration 0013). A restrictive policy is combined with the existing per-principal policies by AND, so none of them was rewritten. The policy compares `org_id` with the connection's `cairn.org_id` and exempts only two internal logins, tested on `session_user` (the login) and not `current_user`: the older SECURITY DEFINER functions run as the owner, so a `current_user` test would have let an API session escape its organisation through them. A test caught that mistake in a first draft of the guard on the new identity functions. When no organisation is set, nothing is visible (fail closed). A schema test fails the build for any table in `cairn` without the column and the policy (`cairn.tenant_table()` adds both in one call).
* **Amazon Cognito proves who a person is; PostgreSQL holds everything else.** Accounts, organisation membership, role and teams live in `cairn_meta` and `cairn`. The API calls Cognito through a small protocol (`idp.py`) so tests use an in-memory fake. Sign-up can auto-confirm for a demo (`CAIRN_AUTH_AUTO_CONFIRM`); email verification and password reset use Cognito's own flows. MFA is enabled as optional in the pool but not wired in the API.
* **Roles are enforced in the database as well as the API.** Role changes, deactivation, invitations, approvals, attestations, access reviews and report visibility are SQL functions or policies that read the caller's role from a transaction setting the API writes from the stored membership. A person never votes on their own request (a function check and a trigger), a published policy version and an attestation are immutable, and a deactivated person's group memberships end in the same statement.
* **The audit log is a hash chain per organisation** (`cairn.audit_events`, migration 0014): each row hashes the previous hash and its own fields; the single writer function takes a per-organisation lock; updates, deletes and truncation are refused by triggers. `verify_chain()` recomputes it. A database administrator who disables the triggers can still rewrite the table, so the head of the chain is also **anchored** to an S3 bucket with Object Lock (governance mode, 365 days, versioning on) with an HMAC signature; verification compares each stored anchor with the database. A test rewrites the chain end to end with valid hashes, and only the anchor check notices.
* **Controls are computed from the records, not asserted** (`cairn.control_status()`, migration 0018): fifteen checks, each `pass`, `fail` or `no_evidence`, with the sentence saying what was counted. The screen and the evidence pack say these are evidence, not a certification.

## Consequences

* Good: isolation does not depend on every query remembering a filter; the canary tests, 227 integration tests and the new cross-tenant tests all run against the same policy.
* Good: a reviewer can read the rules in SQL and tests. The approvals, access reviews, reports and policies are demonstrable end to end in the live demo organisation.
* Bad: the owner and ingest logins bypass the filter by design, so they stay internal (never used for a request). Agent tokens (MCP) are bound to the demo organisation until they carry one.
* Bad: governance mode Object Lock can be bypassed by an account that may change retention; compliance mode would be stricter but cannot be undone, so it was not chosen for a demo.
* Not done: MFA in the API, Google sign-in, email delivery of invitations (the link is shown to the inviter), billing, SAML. See `docs/product/saas-plan.md`.
