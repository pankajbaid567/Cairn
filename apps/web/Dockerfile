# Web image: build the static files with Node, serve them with unprivileged nginx.
#
#   docker build -t cairn-web:local apps/web
#
# The build context is apps/web (see .dockerignore there). Both base images are
# pinned by tag and digest (Requirement 21.3), resolved 27 Oct 2026; both digests
# are multi-arch index digests.
#
# Stage 1 uses the same Node image as NODE_IMAGE in mk/web.mk (V8): the version
# that passes `make web-check` is the version that builds the shipped files.
# Stage 2 is nginx 1.31.7 on Alpine 3.24; how the digest was resolved is in
# docs/evidence/phase-0/verify/V8.md.

FROM node:24.22.1-alpine3.24@sha256:06d7adf3e0ef61a13f1d28682726366f97a15aa1888c934d3ecec86c33918b53 AS build

# corepack installs the pnpm version named in package.json's packageManager.
ENV COREPACK_ENABLE_DOWNLOAD_PROMPT=0 \
    CI=true

WORKDIR /app

# Layer 1: dependencies, from the manifests and the lock file only.
COPY package.json pnpm-lock.yaml .npmrc ./
RUN corepack enable \
    && pnpm install --frozen-lockfile

# Layer 2: sources and the production build into /app/dist.
COPY . .
RUN pnpm run build

FROM nginxinc/nginx-unprivileged:1.31.7-alpine3.24@sha256:b5047b9b76845eac124358373c6852377c52e4b05834a47647e955dd91cb8f1e AS runtime

# The image runs as uid 101 (nginx). Its default server block is replaced by the full
# configuration below, so the stock default.conf is removed rather than left unused.
RUN rm -f /etc/nginx/conf.d/default.conf
COPY nginx.conf /etc/nginx/nginx.conf
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 8080
USER 101:101
