# Task 8.1 notes: `make up`, `make down`, `make logs`, `make smoke` and the core profile smoke test

Date: 27 Oct 2026. No dependency, lockfile, compose or Python source change. The `smoke` marker was already registered (task 1.2).

## Files

| File | State |
|---|---|
| `mk/stack.mk` | New: `up`, `down`, `logs`, `smoke` |
| `tests/smoke/test_core_profile.py` | New: 6 tests, marker `smoke` |
| `CONTRIBUTING.md` | `test-integration`, `migrations-check`, `matrix`, `up`, `down`, `logs`, `smoke` no longer pending; new rows in the targets list, the quality-gate table and the target reference. `gates` and `phase0-exit` stay pending |

## Targets

| Target | Command | Notes |
|---|---|---|
| `up` | `docker compose --profile core up --wait --build` | `UP_BUILD=` skips the build. On failure prints `ps -a` and `logs --tail 50` to stderr and keeps compose's exit status (checked with `make up COMPOSE=false`: message, exit 1). The sequential fallback of design §4 is not needed: V3 showed `--wait` accepts the db-init one-shot, and it worked here |
| `down` | `docker compose --profile core down --remove-orphans` | Never `--volumes`; `cairn_pgdata` survives. Not run in this task, because it would stop the shared db |
| `logs` | `docker compose --profile core logs --no-color --tail $(LOG_TAIL) $(SERVICE)` | `LOG_TAIL` default 200, optional `SERVICE` |
| `smoke` | depends on `up`, then `pytest -m smoke tests/smoke` | Fails with compose's output if the stack cannot become healthy, so the tests never run against a broken start |

`test-unit` already excludes the marker (`-m "not integration and not smoke"`), so the smoke tests never run there.

## The smoke tests

They read the live containers of project `cairn` through `docker ps --filter label=com.docker.compose.project=cairn` and `docker inspect` (so other projects' containers are never looked at), and call the published ports on 127.0.0.1 with httpx. They change nothing. The module fixture skips when docker is missing or unreachable, or when none of api, worker and web is running; once one of them runs, anything missing or unhealthy is a failure.

| Test | Asserts |
|---|---|
| `test_readyz_is_200_and_lists_the_three_extensions` | `GET 127.0.0.1:8000/readyz` is 200, status `ready`, `pg_search`, `vector`, `btree_gist` present with a version |
| `test_web_page_is_served` | `GET 127.0.0.1:8080/` is 200 `text/html` and the HTML contains `<title>Cairn</title>` (no widget or heading text: rendered client-side) |
| `test_readyz_through_the_web_proxy_is_200` | `GET 127.0.0.1:8080/api/readyz` is 200 with status `ready` |
| `test_every_container_is_healthy` | db, api, worker, web are running and `healthy`; db-init is `exited` with code 0; any other service of the project that runs is healthy |
| `test_memory_limits_are_set_and_sum_under_four_gb` | every container has a memory limit; db + api + worker + web is at most 4 GiB (actual: 1856m) |
| `test_only_loopback_ports_are_published` | every host binding, in `HostConfig.PortBindings` and in the live `NetworkSettings.Ports`, has `HostIp` 127.0.0.1; api and web publish a port, worker none |

Skip behavior checked with only db up: 6 skipped, 0 failed.

## Verification (live)

Before `make smoke`: `docker compose --profile core config --hash db` equal to the `com.docker.compose.config-hash` label on `cairn-db-1` (`6e9b0dfb...`), so `up` could not recreate it.

```text
$ make smoke      # build (cached), db-init ran and exited 0, api, worker, web Healthy; 6 passed
$ docker inspect cairn-db-1   # StartedAt 2026-10-27T14:46:50Z before and after, healthy
```

The first `make smoke` took about 4.5 minutes, nearly all of it image builds on a loaded machine (load average about 10); later runs are cached.

The full sequence `make fmt-check lint types test-unit compose-check docs-check smoke` passed on the second run. On the first run `test-unit` had two timing failures that are unrelated to this task and pass alone: `test_invalid_configuration_exits_2_naming_keys_and_never_values` (Hypothesis `DeadlineExceeded`, 354 ms against 200 ms) and `test_oracle_agrees_with_the_naive_replay` (Hypothesis `FailedHealthCheck: Input generation is slow`), both during a Docker build with the machine under heavy load. Worth knowing for the CI job: the `ci` profile disables deadlines, `dev` does not.

## Cleanup

Afterwards only what this task started was stopped and removed: `docker compose --profile core stop web api worker`, then `rm -f web api worker db-init`. `cairn-db-1`, the `cairn_pgdata` volume and the other projects' containers (taskflow-*, openwa-api) were not touched. No `down`, no `--volumes`. The stack is left with db up.

## For later tasks

- 11.2 (`gates`): `up smoke` is redundant now, since `smoke` depends on `up`; `gates` can call `smoke` alone, then `down`.
- 17.1 (RAM report): the container names are `cairn-db-1`, `cairn-api-1`, `cairn-worker-1`, `cairn-web-1`; limits are 1024m, 384m, 384m, 64m.
