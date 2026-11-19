# Cairn: resume material

Repo: github.com/pankajbaid567/Cairn · Live: https://cairn.3-109-36-90.sslip.io (demo personas, synthetic data; or sign up with an email)
What it is: a multi-tenant SaaS for engineering organisations: cited answers from the company's own history, daily work reports with roll-ups, approvals with segregation of duties, policies and attestations, access reviews, and a tamper-evident audit trail with a signed evidence pack.
Stack: Python, FastAPI, DBOS, PostgreSQL 18 + pgvector with row-level security (Neon), Amazon Cognito, Amazon Bedrock (Titan v2, gpt-oss, Qwen3), AWS EC2, IAM, SSM, CloudFormation, S3 Object Lock, Caddy, Docker, React 19, TanStack Query, TypeScript, pytest, mypy --strict, Vitest.

Numbers come from the repo's tests and evidence files (`docs/evidence/`, `docs/product/saas-plan.md`). The "Do not claim" list matters as much as the bullets.

## CARL bullets (pick 4 or 5)

**1. Tenant isolation enforced in the database**
- **Context:** one database now holds many organisations; a missing filter in any query would leak one customer's data to another.
- **Actions:** **Added** an `org_id` and a RESTRICTIVE row-level-security policy to all 30 tables in one migration (combined by AND with the existing per-person policies, none rewritten), **wrote** a schema test that fails the build for any table without them, and **caught** a flaw in my own first draft (a `current_user` test would have let older definer functions escape the filter; changed to `session_user`).
- **Results:** cross-tenant tests across search, answers, reports, approvals, audit and exports; **227 integration tests, 2,116 unit tests, 128 web tests**; with no organisation set, nothing is visible.
- **Learning:** put the guarantee where it cannot be forgotten, then write the test that proves the guarantee has no hole.

**2. Governance rules the application cannot bypass**
- **Context:** approvals and access changes only mean something if the person asking cannot approve.
- **Actions:** **Built** approvals with quorum, where nobody votes on their own request (a function check plus a trigger), immutable votes, policy versions and attestations, and an access review whose reviewer is never the person reviewed.
- **Results:** a manager's attempt to approve their own proposal is refused with 403 and recorded; a policy is published only through an approval by someone else; the live demo shows each of these.
- **Learning:** enforce rules in the data layer and keep the API check as the friendly first line.

**3. A tamper-evident audit trail**
- **Context:** an append-only table still lets a database administrator rewrite history.
- **Actions:** **Chained** every audit event to the previous one with SHA-256 under a per-organisation lock, **anchored** the head of each chain to an S3 bucket with Object Lock (HMAC-signed), and **wrote** a test that rewrites the whole chain with valid hashes.
- **Results:** the chain check passes the rewritten chain; the anchor check catches it. A signed evidence pack (CSV files plus a manifest with a SHA-256 per file) is one click.
- **Learning:** state what a control does not stop, then add the layer that does.

**4. Daily reports with a verified AI digest**
- **Context:** managers ask "what did everyone do"; a summary is only useful if each line can be traced.
- **Actions:** **Built** daily reports with team-based visibility enforced by policy and a 14-day edit window, a roll-up showing who is missing, and a digest where every bullet must cite real report ids or is dropped.
- **Results:** tests show an invented bullet is removed, an unusable model reply returns an error instead of a made-up digest, and a digest is cached per viewer.
- **Learning:** a model's output is untrusted input; check it against what it was given.

**5. Measuring retrieval honestly**
- **Context:** the first evaluation used 33 chunks and scored about 1.0 for every variant, which proved nothing.
- **Actions:** **Built** a 150-PEP benchmark (2,007 chunks, 200 questions: AI-drafted with verbatim quotes, code-made, unanswerable and permission cases), seven retrieval variants including Titan v2 vs Cohere v3, bootstrap intervals and a test split.
- **Results:** hybrid beat keyword-only by **+0.13 nDCG@10 (interval +0.09 to +0.18)**; the LLM reranker did not help significantly (+0.015, interval crossing zero) and cost about 1.6 s; **no permission leak**; but only **96 of 143 answerable questions were answered**, a real weakness I documented.
- **Learning:** a benchmark that cannot fail is not evidence; report the null results too.

**6. Cheap, always-available AWS hosting** (earlier work, still true)
- **Actions:** **Wrote** a stdlib front door that starts the app container on the first request and stops it after 15 idle minutes so a free database tier lasts the month; **deployed** with CloudFormation, SSM parameters and an instance role (no keys on disk).
- **Results:** cold start 8 to 11 s; fixed cost about $1 to $3 a month plus Bedrock use (about half a cent for a verification session).

## Do not claim

- That it is SOC 2 or ISO certified, or "compliant": the controls map the platform's own records to themes and say "no evidence" where there is none.
- Real customers, real company data or production traffic: it is a demo with synthetic data.
- MFA, Google or SAML sign-in, email invitations, billing: not built (see `docs/product/saas-plan.md`).
- That the LLM reranker or Cohere improves retrieval, or that answer quality is good: on the benchmark the reranker was not significant and the answer rate was 67%.
- That the Neon database is on AWS: it is on Neon (Singapore); everything else is on AWS in Mumbai.
- Any comparison with named products ("better than Vanta", "replaces Jira").

## Interview one-liners

- "I made tenant isolation a database policy on every table, and wrote a test that fails the build for a table without it."
- "Nobody can approve their own request, and that is a trigger, not a UI rule."
- "The audit log is hash-chained and anchored in a locked S3 bucket; I wrote the test that rewrites the whole chain to show only the anchor catches it."
- "My own benchmark found my reranker did not help; I kept the finding."
