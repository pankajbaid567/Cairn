# FV1: pgvector image pin

Resolved 2 Nov 2026 (task 1.1). Applied in task 2.1 (`x-pg-image` in `docker-compose.yml`). Requirements 1.1 and 1.2; design FT-01 and FT-03.

## What Neon offers for PostgreSQL 18

The [Neon supported extensions table](https://neon.com/docs/extensions/pg-extensions), read 2 Nov 2026, lists for PG18:

| Extension | PG18 version on Neon |
|---|---|
| `vector` (pgvector) | **0.8.6** (PG14 to PG17: 0.8.0) |
| `btree_gist` | 1.8 |
| `pg_search` | unavailable on every version |

0.8.6 is above the 0.8.0 floor of FT-03 and Requirement 2.3.

## pgvector/pgvector tags for PostgreSQL 18

From the Docker Hub tag list (`hub.docker.com/v2/repositories/pgvector/pgvector/tags?name=pg18`), 2 Nov 2026:

| Tag | Pushed | Index digest |
|---|---|---|
| `0.8.7-pg18` (newest; same digest as `pg18`, `pg18-bookworm`, `0.8.7-pg18-bookworm`) | 31 Oct 2026 | `sha256:c29f30688a9ad961691bdf0ec1650717cc132a5814619d80f80463d6e4c42e47` |
| `0.8.6-pg18` (same digest as `0.8.6-pg18-bookworm`) | 13 Aug 2026 | `sha256:2ba9ca5f2e7daa0f0e7723cba1ee9167bab54efd3640516a44ac1a928dd67e7a` |

Older `-pg18` tags exist for 0.8.1 to 0.8.5. The `-trixie` variants have different digests and are not used.

`docker buildx imagetools inspect pgvector/pgvector:0.8.6-pg18`:

```
MediaType: application/vnd.oci.image.index.v1+json
Digest:    sha256:2ba9ca5f2e7daa0f0e7723cba1ee9167bab54efd3640516a44ac1a928dd67e7a
  linux/amd64  sha256:1d50c689b0a6511b9ea0a15615281c81a59fd04a08eb35057ec8646fb3a2118a
  linux/arm64  sha256:46481fb03d04f9160a2373d305f769eb34040fc866e7bd74b8f5b84a50cc2952
  (plus one attestation manifest per platform)
```

`docker buildx imagetools inspect pgvector/pgvector:0.8.7-pg18`:

```
MediaType: application/vnd.oci.image.index.v1+json
Digest:    sha256:c29f30688a9ad961691bdf0ec1650717cc132a5814619d80f80463d6e4c42e47
  linux/amd64  sha256:2547711f871f5d4c02b2dfc7ff86f3eddc438d00a23854cd449513f4ef1af79b
  linux/arm64  sha256:ed9dc4e391eb941f4bedf78b104b5bc6dd9630330d1ce3d818067dbe5f64a284
```

Image config of both tags, both platforms: `PG_MAJOR=18`, `PG_VERSION=18.6-1.pgdg12+2` (Debian bookworm), `PGDATA=/var/lib/postgresql/18/docker`. The PGDATA path sits under `/var/lib/postgresql`, which is where design §4 mounts `pgdata-pgvector` and where design §7 puts the `ft-db` tmpfs.

## Decision

Pin:

```
pgvector/pgvector:0.8.6-pg18@sha256:2ba9ca5f2e7daa0f0e7723cba1ee9167bab54efd3640516a44ac1a928dd67e7a
```

Why:

- Requirement 1.2 and FT-01 ask for Neon's pgvector version when an image for it exists. Neon lists 0.8.6 for PG18, and `0.8.6-pg18` exists, so the newer 0.8.7 is not used. Local then runs the same extension version as production.
- 0.8.6 meets the 0.8.0 floor (iterative index scans).
- The index digest (not a per-platform digest) is pinned, so the same reference works on the macOS arm64 host and the amd64 CI runners.
- Both tags carry PostgreSQL 18.6, so choosing 0.8.6 costs nothing on the server side.

When Neon moves PG18 to a newer pgvector, re-run this check and bump the tag and digest together.
