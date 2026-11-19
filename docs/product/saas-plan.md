# Cairn as a SaaS product: plan

Status: accepted 2026-11-19. Decisions: Amazon Cognito for login; team productivity first, so the phase order is A, C, B, D, E; all phases in scope. Nothing here is built yet except where marked "exists".

## 1. The pitch (what a recruiter or buyer hears)

**Cairn is the system of record for how an engineering company decides and what it did, with evidence an auditor accepts.**

One platform for four things that today live in four tools:

| Job | Today people use | In Cairn |
|---|---|---|
| Why did we decide X, who approved it, does it still hold | Confluence, Slack archaeology | Decision register with cited evidence (exists as a read model) |
| What did everyone do today | Standup bots, Range, Geekbot | Daily work reports, team and company roll-ups |
| Who is allowed to do/see what, and prove it | Spreadsheets, Vanta, Drata | Governance: roles, policies with attestations, approvals with segregation of duties, access reviews |
| Show an auditor what happened | Screenshots and exports | Tamper-evident audit log, evidence packs, auditor role |

The wedge is the first and the last row: AI answers that only use what the asker may read, and an audit trail that can prove it was not edited. Reports and governance are modules around that wedge. Target customer: engineering organisations of 20 to 500 people in regulated or audit-heavy fields (fintech, health, B2B SaaS with SOC 2 pressure).

Honest limits to state in any pitch: this is a working product prototype, not a certified compliance product (no SOC 2 or ISO attestation); billing is a plan and usage page, not live payments; integrations are GitHub plus manual entry at launch.

## 2. What exists and what does not

Exists: PostgreSQL row-level security by principal and group (forced), bitemporal decisions, claims with cited sources, review queue (conflicts, candidates), append-only audit log with HMACs, agent tokens and MCP, GitHub connector design, usage metering with a dollar cap, a live deployment (AWS + Neon + Bedrock), a retrieval and answer benchmark.

Does not exist, and is the real work:

1. **Real login.** Today there is a demo "choose a persona" cookie. No accounts, passwords, invitations, MFA.
2. **Tenancy.** One organisation per database. `decisions.key` is globally unique. No `org` concept.
3. **Create and edit.** Decisions are imported or seeded, not authored or approved in the app.
4. **Reports, policies, approvals, controls, access reviews.** None.
5. **Tamper evidence.** The audit log is append-only and HMAC'd but not hash-chained, so a database administrator could still delete rows silently.
6. **A product UI.** The current page is a single scrolling demo.

## 3. Modules

### M1 Identity, organisations, people
- Sign up creates an organisation and its first Owner. Login with email and password or Google, email verification, password reset, optional TOTP MFA.
- Roles: Owner, Admin, Manager, Member, Auditor (read-only across audit data, cannot see content they are not granted), Guest.
- Teams (map to the existing groups with valid-time membership). Invite by email, accept, change role, deactivate (access ends immediately, recorded).
- Every admin action writes an audit event.

### M2 Decision register (authoring and approval)
- Create a decision from a template (question, options, outcome, owners, evidence links). States: proposed, in review, approved, implemented, superseded, rejected.
- Approval needs the configured approvers; the requester cannot approve their own decision (segregation of duties).
- Ask and Search keep working on top, now org-scoped.

### M3 Daily work reports
- Each member posts a daily report (done, next, blockers, links to decisions or PRs). Reminder if missing.
- Team and company roll-ups per day, week and month, with an AI digest ("what major work happened") that cites the underlying entries and only uses what the viewer may read.
- GitHub activity (merged PRs, reviews) can be attached automatically once the GitHub App is installed (a user step); until then, entries are manual.
- Export to PDF and CSV.

### M4 Governance
- Policy library with versions, owners, review dates and member attestations ("I have read and accept"), with an overdue list.
- Approval workflows (access requests, policy changes, exceptions) with segregation of duties and expiry on exceptions.
- Risk and exception register.
- Controls and evidence: about 20 SOC 2 style controls, each linked to evidence that the platform already produces (access review done, policy attested, approvals logged, audit log verified). It maps evidence; it does not certify anything.

### M5 Audit
- Org-wide audit log: sign-ins, role changes, content views, answers, approvals, exports. Content is never stored, only ids and hashes (as now).
- Hash chain per organisation plus a verification endpoint and a daily signed checkpoint stored outside the database (S3 with object lock), so deletion is detectable.
- Access reviews: a manager certifies who has which access each quarter; the record is evidence.
- Auditor role, filters, evidence packs (zip of CSV and a signed manifest).

### M6 Platform
- Dashboard (today's reports, pending approvals and attestations, recent decisions, audit health).
- Notifications in app and by email. Org settings, security settings, usage page (seats, Bedrock spend per organisation).
- Onboarding checklist and a seeded demo organisation (about 15 people, 30 days of reports, decisions, policies) so the pitch never starts on an empty screen.

## 4. Technical design (the parts that are hard)

**Auth.** Amazon Cognito user pool in ap-south-1 (free for this volume) for credentials, verification, reset, MFA and Google. Cognito only proves who you are; organisation, role and team live in PostgreSQL. Invitations use Cognito's admin invite email, so no separate email setup is needed to start. The API validates the Cognito token and maps it to a principal. The demo persona login stays, but only for the demo organisation.

**Tenancy, two layers.** (1) Every organisation gets its own groups and principals, and content is only ever readable by its own groups, so the existing row-level security already isolates organisations. (2) Add `org_id` to every table and a second row-level policy on it, and make unique keys per organisation (`decisions.key` becomes unique per org). The existing canary leak matrix is extended to cross-tenant canaries: a planted token in organisation A must never appear for any user of B through search, answers, exports, audit or reports. This is the riskiest change, so it goes first.

**New tables (migrations 0013 onward).** orgs, org_members (role, state), invites, teams, work_reports, report_comments, decision_approvals, policies, policy_versions, attestations, approval_requests, risks, controls, control_evidence, access_reviews, audit chain columns, notifications.

**Audit chain.** Each audit row stores `prev_hash` and `row_hash` (HMAC over the previous hash and the row). A verifier recomputes the chain. A scheduled job writes the head hash to an S3 bucket with object lock; a test deletes a row and expects verification to fail.

**Frontend.** A routed single page app (React Router), the design system already added, an app shell, login and signup, and one area per module. Server errors and empty states designed for each screen. Playwright end-to-end tests for the main flows.

**Hosting and cost.** Same EC2 host and Neon. Likely upgrade to a `t4g.medium` ($16 a month) if memory gets tight. Cognito and S3 object lock cost cents. Bedrock stays capped per organisation. Neon free storage (0.5 GB) is enough for a demo.

## 5. Phases (each one is deployable and demoable)

| Phase | Content | Done when |
|---|---|---|
| A. Foundation | M1, tenancy, audit chain, routed app shell, login, signup, invites, roles | Two organisations on one deployment, cross-tenant canary test passes, a new user can sign up, invite a teammate and see only their org |
| B. Decisions and governance core | M2, policies and attestations, approvals with segregation of duties | A decision goes proposed to approved by someone else; a policy is attested by all members; both appear in the audit log |
| C. Daily reports | M3 | Members post, managers roll up, AI digest cites entries, PDF export |
| D. Audit and controls | M5, controls and evidence, access reviews, evidence pack | Auditor logs in, verifies the chain, downloads an evidence pack; tampering is detected |
| E. Product polish | M6 dashboard, notifications, usage, onboarding, seeded demo org, docs, pitch script | A 10 minute demo runs end to end without touching a terminal |

Order is A, B, D, C, E if the pitch is governance-first, or A, C, B, D, E if it is team-productivity-first (see the questions).

## 6. How each phase is checked

- Unit and integration tests, `mypy --strict`, lint, the existing 2,100+ tests stay green.
- A cross-tenant canary matrix and a role-by-endpoint permission matrix (every endpoint tested for every role).
- Playwright tests for the main flows.
- A written threat model update per phase.
- Evidence written into `docs/evidence/` like the existing phases; no claim without a measured result.

## 7. Risks

- Multi-tenant mistakes are silent; the canary matrix is the control, and Phase A does not ship without it.
- Scope: this is months of normal team work. It will be built in the order above; if time runs out, A plus two of B, C, D is still a coherent product.
- Email deliverability and Google login need console steps only the account owner can do (Cognito app client, Google OAuth keys).
- Compliance wording: use "evidence" and "audit-ready", never "compliant" or "certified".

## 8. Decisions taken

1. Auth: Amazon Cognito (the user creates the app client and, later, the Google OAuth keys).
2. Pitch angle: team productivity first. Order A (foundation), C (daily reports), B (decisions and governance core), D (audit and controls), E (polish). The audit chain is still built in A, because every later module writes to it.
3. Scope: all phases.
