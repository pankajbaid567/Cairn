# FV0: free-tier platform facts

Facts as of **30 Oct 2026** (gathered by the user during design; the design table "Free-tier facts this design relies on" lists them). Each source was re-read on 2 Nov 2026 for task 1.1; where the page now says something different from the design, the row says so. Design: `.kiro/specs/cairn-free-tier-retarget/design.md`, Overview.

Content was rephrased for compliance with licensing restrictions. No row quotes more than a few words from its source; the URLs are the authority.

## Render (free web service)

| Fact used by the design | Source |
|---|---|
| A free web service gets 512 MB RAM and 0.1 CPU | [Render: From side project to production](https://render.com/articles/from-side-project-to-production-scaling-your-first-app) |
| It spins down after 15 minutes with no inbound HTTP request or WebSocket message, and takes about one minute to come back on the next request; local filesystem changes are lost on spin-down | [Render docs: Deploy for Free](https://render.com/docs/free) |
| Each workspace gets 750 free instance-hours per calendar month; a spun-down service uses none; when they run out, free web services are suspended until the next month | [Render docs: Deploy for Free](https://render.com/docs/free) |
| Only web services, Postgres and Key Value (plus static sites) have a free instance type; background workers and cron jobs do not | [Render docs: Deploy for Free](https://render.com/docs/free); [Render: cloud hosting cost for small businesses](https://render.com/articles/how-much-does-cloud-application-hosting-cost-for-small-businesses) |
| Free Render Postgres is 1 GB and expires 30 days after creation, with a 14-day grace period before deletion (not used by Cairn) | [Render docs: Deploy for Free](https://render.com/docs/free); [Render: Platforms with a real free tier for developers in 2026](https://render.com/articles/platforms-with-a-real-free-tier-for-developers-in-2026) |
| Also relevant: free services can be restarted at any time, have no SSH shell or one-off jobs, cannot bind ports 18012, 18013 or 19099, and may be suspended for unusually heavy outbound traffic | [Render docs: Deploy for Free](https://render.com/docs/free) |

## Neon (free plan)

| Fact used by the design | Source |
|---|---|
| The free plan has no time limit and needs no card | [Neon FAQ: free tier generous enough for a real app](https://neon.com/faqs/managed-postgres-databases-free-tier) |
| Per project and month: 100 CU-hours of compute and 5 GB of public network transfer; 1 GB Postgres storage per project (20 GB across all projects); 100 projects, 10 branches per project; autoscaling up to 2 CU | [Neon FAQ: Free plan limits and quotas](https://neon.com/faqs/free-plan-limits-and-quotas) |
| When CU-hours or transfer run out, the compute is suspended until the next billing period (connections drop); storage over 1 GB makes writes that grow storage fail; no data is deleted | [Neon FAQ: Free plan limits and quotas](https://neon.com/faqs/free-plan-limits-and-quotas) |
| Compute scales to zero after 5 minutes idle (cannot be turned off on Free) and resumes in a few hundred milliseconds | [Neon FAQ: Free plan limits and quotas](https://neon.com/faqs/free-plan-limits-and-quotas); [Neon FAQ: best free or low-cost managed Postgres](https://neon.com/faqs/best-free-low-cost-managed-postgres-services) |
| 100 CU-hours covers a 0.25 CU compute for about 400 hours a month (an always-on 0.25 CU compute for 744 h would need about 186) | [Neon FAQ: Free plan limits and quotas](https://neon.com/faqs/free-plan-limits-and-quotas) |
| pgvector is supported (version for PostgreSQL 18 in [FV1](FV1.md)) | [Neon docs: Supported Postgres extensions](https://neon.com/docs/extensions/pg-extensions) |
| `pg_search` (ParadeDB) is gone from Neon: blocked for new projects from 19 Mar 2026 and retired from existing projects on 21 Sep 2026; Neon points to `lakebase_text` instead | [Neon docs: The pg_search extension](https://neon.com/docs/extensions/pg_search); [Neon docs: Migrate from pg_search to lakebase_text](https://neon.com/docs/extensions/migrate-pg-search-to-lakebase-text); [Neon changelog (plans)](https://neon.tech/docs/release-notes/plans) |
| Each project has a role named after its database (`neondb_owner`), granted `neon_superuser`, which can create databases and roles and read and write all tables | [Neon docs: Manage database access](https://neon.com/docs/manage/database-access) |
| Roles created with SQL are not given `neon_superuser`; they start with the standard `public` schema privileges and need explicit grants | [Neon docs: Manage database access](https://neon.com/docs/manage/database-access) |
| `neondb_owner` has BYPASSRLS and ignores RLS policies; to enforce RLS the backend must log in with a custom LOGIN role without BYPASSRLS | [Neon docs: RLS query execution](https://neon.com/docs/guides/rls-query-execution) |

## Cloudflare

| Fact used by the design | Source |
|---|---|
| Pages: requests to static assets are free and unlimited on every plan; a request is static when it invokes no Function | [Cloudflare docs: Pages Functions pricing](https://developers.cloudflare.com/pages/functions/pricing/) |
| Pages Free: 500 builds per month, one at a time, 20-minute build timeout; up to 20,000 files per site, 25 MiB per file; `_headers` up to 100 rules | [Cloudflare docs: Pages limits](https://developers.cloudflare.com/pages/platform/limits/) |
| Pages Function requests count against the Workers Free daily quota of 100,000 requests, reset at midnight UTC | [Cloudflare docs: Pages Functions pricing](https://developers.cloudflare.com/pages/functions/pricing/); [Cloudflare docs: Workers limits](https://developers.cloudflare.com/workers/platform/limits/) |
| Workers Free: 100,000 requests per day, 10 ms CPU per request (network waits do not count), 128 MB memory, 50 subrequests per request; over the daily limit Cloudflare returns error 1027 | [Cloudflare docs: Workers limits](https://developers.cloudflare.com/workers/platform/limits/) |
| Containers have no free allowance; usage is included with, and billed on, the Workers Paid plan ($5 per month) | [Cloudflare docs: Containers pricing](https://developers.cloudflare.com/containers/pricing/) |
| Workers AI: every account gets 10,000 neurons per day free; above that needs Workers Paid at $0.011 per 1,000 neurons | [Cloudflare docs: Workers AI pricing](https://developers.cloudflare.com/workers-ai/platform/pricing/) |
| Since 28 Jul 2026 a few heavy models (`kimi-k2.6`, `kimi-k2.7-code`, `glm-5.2`) need Workers Paid and return HTTP 403 (internal error 5035) on Free; `glm-4.7-flash`, `gemma-4-26b-a4b-it` and `nemotron-3-120b-a12b` stay available on Free | [Cloudflare changelog: Select models now require the Workers Paid plan](https://developers.cloudflare.com/changelog/post/2026-07-28-models-require-workers-paid/) |
| Queues are on the Workers Free plan since 4 Feb 2026: 10,000 operations per day per account, retention capped at 24 hours (not used in this spec) | [Cloudflare changelog: Queues now available on Workers Free plan](https://developers.cloudflare.com/changelog/2026-02-04-queues-free-plan) |

### Workers AI model pages read during design

| Model | Fact | Source |
|---|---|---|
| `@cf/baai/bge-small-en-v1.5` | 384-dimensional embeddings, at most 512 input tokens, accepts a batch of texts; model page lists $0.0202 per M input tokens | [Workers AI model: bge-small-en-v1.5](https://developers.cloudflare.com/workers-ai/models/bge-small-en-v1.5/) |
| `@cf/baai/bge-reranker-base` | Takes a query and a list of contexts and returns a relevance score per context (a sigmoid maps it to 0..1); model page lists $0.00311 per M input tokens | [Workers AI model: bge-reranker-base](https://developers.cloudflare.com/workers-ai/models/bge-reranker-base/) |

Neuron rates on the pricing page on 2 Nov 2026 (for FV9, task 8.3; the design's `NEURON_RATES` table was written from the 30 Oct reading):

| Model | Pricing page, 2 Nov 2026 | Design `NEURON_RATES` |
|---|---|---|
| `@cf/baai/bge-small-en-v1.5` | 1,841 neurons per M input tokens ($0.020) | 1,836 ($0.0202). **Differs**: task 8.3 must re-read and pick the higher value |
| `@cf/baai/bge-reranker-base` | 283 neurons per M input tokens ($0.003) | 283 |
| `@cf/google/gemma-4-26b-a4b-it` | 9,091 input, 27,273 output per M tokens | see design §8 (FV9) |
| `@cf/zai-org/glm-4.7-flash` | 5,500 input, 36,400 output per M tokens | see design §8 (FV9) |

## Rejected options

| Fact | Source |
|---|---|
| Supabase pauses a Free project after about a week with too little database activity; paid projects never pause | [Supabase docs: Project pausing](https://supabase.com/docs/guides/platform/free-project-pausing); [Supabase pricing](https://supabase.com/pricing) |
| Oracle Always Free Ampere A1 is now 2 OCPU and 12 GB in total (1,500 OCPU-hours and 9,000 GB-hours a month), half the earlier 4 OCPU / 24 GB; the cut took effect 15 Jun 2026 without an announcement | [Oracle docs: OCI Free Tier](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier.htm); [InfoQ, Jul 2026](https://infoq.com/news/2026/07/oracle-cloud-free-tier-limits) |
| Oracle may reclaim idle Always Free compute instances, and instances over the new limit were stopped from about 18 Aug 2026 | [Oracle docs: OCI Free Tier](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier.htm); [Linuxiac](https://linuxiac.com/oracle-quietly-cuts-free-tier-ampere-a1-resources-in-half/) |
