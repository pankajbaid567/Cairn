# FV2: PostgreSQL 18 and the Singapore region on Neon free

Resolved from Neon's documentation on 2 Nov 2026 (task 1.1). Content was rephrased for compliance with licensing restrictions. The user confirms the parts marked **FU** when creating the project (FU1) and running `make remote-init` and `make remote-status` (FU2).

## PostgreSQL 18

| Question | Finding | Source |
|---|---|---|
| Is PostgreSQL 18 offered? | Yes. Neon supports major versions 14 to 18 and lets you pick the major version when creating a project | [Neon docs: Postgres version support](https://neon.com/docs/postgresql/postgres-version-policy) |
| GA or preview? | **GA**. PostgreSQL 18 became generally available on Neon on 1 May 2026 with the preview limits removed, and is the default for new projects since 5 Jun 2026 | [Neon changelog 1 May 2026](https://neon.tech/docs/changelog/2026-05-01); [Neon changelog 5 Jun 2026](https://neon.com/docs/changelog/2026-06-05) |
| Minor version | Neon runs only the latest minor and applies it on the next compute restart; since 18 Aug 2026 that is 18.6, the same minor as the pinned local image ([FV1](FV1.md)) | [Neon docs: Postgres version support](https://neon.com/docs/postgresql/postgres-version-policy); [Neon changelog 21 Aug 2026](https://neon.com/docs/changelog/2026-08-21) |
| Free plan restriction? | None found. The plan limits page lists no version restriction, and the version pages apply to every plan | [Neon FAQ: Free plan limits and quotas](https://neon.com/faqs/free-plan-limits-and-quotas) |
| Major upgrade later | A project keeps its major version; moving to 19 means a new project and a data migration | [Neon docs: Postgres version support](https://neon.com/docs/postgresql/postgres-version-policy) |

OQ-2 (no PostgreSQL 18 on Neon free) does not apply.

**FU1**: the user selects 18 in the project form. **FU2**: `make remote-status` shows `server_version` 18.x.

## Region `aws-ap-southeast-1`

| Question | Finding | Source |
|---|---|---|
| Does the region exist? | Yes, AWS Asia Pacific (Singapore), `aws-ap-southeast-1` | [Neon docs: Regions](https://neon.com/docs/introduction/regions) |
| Is Postgres available there? | Yes. Postgres runs in every listed region; only some add-on products (not used here) roll out region by region, and the AI Gateway needs a paid plan | [Neon docs: Regions](https://neon.com/docs/introduction/regions) |
| Free plan restriction? | None found: a project can be created in any listed region, and neither the regions page nor the plan limits page limits regions by plan | [Neon docs: Regions](https://neon.com/docs/introduction/regions); [Neon FAQ: Free plan limits and quotas](https://neon.com/faqs/free-plan-limits-and-quotas) |
| Can it change later? | No. The region is fixed at project creation; moving means a new project and a migration | [Neon docs: Regions](https://neon.com/docs/introduction/regions) |
| How to confirm | The region is the hostname segment before `.aws.neon.tech` in the connection string | [Neon docs: Regions](https://neon.com/docs/introduction/regions) |

**FU1**: the user picks `aws-ap-southeast-1` in the project form (a free-plan account offering only other regions would show here first). **FU2**: the `CAIRN_DB_HOST` in `.env.neon` contains `ap-southeast-1.aws.neon.tech`. The Render side of the pairing (`singapore` on the free plan) is FV6, task 9.1.
