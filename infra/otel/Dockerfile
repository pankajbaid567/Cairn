# OpenTelemetry Collector (contrib) for the obs profile, plus one static tool for its healthcheck.
#
# The upstream collector image holds a single binary: no shell, no wget, no curl. Requirement
# 4.4 wants a healthcheck on every long-running service, so the only way to probe the
# health_check extension from inside the container is to bring a client along. This copies
# the busybox binary of the pinned `musl` variant (statically linked) next to the collector;
# compose runs `/busybox wget` against 127.0.0.1:13133. Nothing else of busybox is used, the
# collector binary, user (10001) and entrypoint are the upstream ones, and the config is
# mounted by compose, not baked in.
#
# Both images are pinned by tag and sha256 digest (multi-arch index digests, resolved
# 27 Oct 2026; see docs/evidence/phase-0/task-10.1-notes.md).

FROM busybox:1.37.0-musl@sha256:5cec3fc171c87218698e85a52af7087de727372aae264a787b8112901a5b0092 AS busybox

FROM otel/opentelemetry-collector-contrib:0.164.0@sha256:340c9ca6a36617084c6661bdc4f0fe92b7cda5a805d61673ec8db375fddc522b
COPY --from=busybox /bin/busybox /busybox
