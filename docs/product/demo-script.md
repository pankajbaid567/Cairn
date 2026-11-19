# Ten-minute demo

URL: https://cairn.3-109-36-90.sslip.io (first request after a quiet period takes about 10 seconds while the app wakes). Keep two browser windows: one signed in as a persona, one private window for a real sign-up.

Personas (no password): **Dev** owner · **Priya** admin · **Carla** manager · **Asha**, **Ben** members · **Iris** auditor. All data is synthetic (a fictional payments company, Kestrel Pay).

## 1. Home as Dev, the owner (1 minute)
Say: "This is what the owner sees at the start of the day." Point at: approvals waiting for him, the team's reports today (5 of 14), the controls (12 passing), "Audit chain intact", and recent activity. Nothing here is hard-coded: each number is a query.

## 2. Ask (1.5 minutes)
Open Ask, click "Why did we move payment-service from MongoDB to PostgreSQL?". Show the numbered citations and quotes. Say: "If the sources do not support an answer it refuses." Then sign in as another workspace (step 7) and ask the same question: "Not found in sources you can access". That is tenant isolation, not a filter in the UI.

## 3. Daily reports (1.5 minutes)
As **Asha**: Daily reports, write today's report, add a link to a decision. Switch to **Carla**: Roll-up shows who has not reported, with coverage for the last 7 days; click Generate digest. Say: "Every bullet cites the reports it came from; a bullet the model cannot tie to a report is dropped."

## 4. Governance: nobody approves their own request (2 minutes)
As **Carla**: Governance, Decisions, propose "Use Redis for rate limiting". Open Approvals and try to approve it: it is refused ("segregation of duties") and the refusal is in the audit trail. Switch to **Priya** and approve. Show the decision is now Accepted, and that it also appears in the Decisions page that Ask reads. As **Ben**: his access request for manager is waiting for an administrator, not a manager.
Policies: **Asha** opens "Access Control Policy", ticks "I have read", Attest. Priya's view shows who has attested and who has not.

## 5. Compliance (2 minutes)
As **Iris** (auditor): Compliance, Controls. Say: "Fifteen checks computed from the records. Where there is no evidence it says so, it never guesses. This is evidence, not a certification." Access reviews: the completed Q4 review (reviewers were never the person reviewed). Evidence and integrity: Verify chain, then Download evidence pack; open `manifest.json`, which lists a SHA-256 for every file and a signature. Anchors: show the anchors that match.

## 6. Audit trail (30 seconds)
Audit trail as Dev: every role change, approval, refusal. "Each event is hashed with the one before it. If someone with database access rewrites the table and recomputes every hash, the anchors stored in an S3 bucket with Object Lock no longer match." (The test that rewrites the chain is `test_anchors_catch_a_chain_that_was_rewritten_end_to_end`.)

## 7. A real sign-up (1.5 minutes)
In the private window: Create an account with your email, name your workspace, People, Invitations, create a link for a teammate with a role, open it in another private window, sign up with that address and join. Show their role limits what they see. The new workspace sees none of Kestrel Pay's data.

## What to say about limits
Working prototype on AWS (EC2, Neon, Cognito, Bedrock, S3 Object Lock). Synthetic data. No MFA, Google or SAML yet; invitations are shared as links; no billing. Controls are evidence mapping, not a certification. The retrieval quality is measured on a separate 150-document benchmark (`docs/evidence/real-bench/`), including the weak spot: too many wrongful refusals.
