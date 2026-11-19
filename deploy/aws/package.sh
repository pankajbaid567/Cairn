#!/bin/bash
# Pack the source tree for the host build: `deploy/aws/package.sh out.tar.gz`.
# Excludes every secret-bearing or generated path and then refuses to produce a tarball that
# contains an env file, a private key or a virtualenv.
set -euo pipefail
out="${1:?usage: package.sh OUT.tar.gz}"
cd "$(dirname "$0")/../.."
tar --exclude='./.env' --exclude='./.env.*' --exclude='./var' --exclude='./.venv' \
    --exclude='./.git' --exclude='./.hypothesis' --exclude='./.mypy_cache' \
    --exclude='./.ruff_cache' --exclude='./.pytest_cache' --exclude='*/__pycache__' \
    --exclude='*/node_modules' --exclude='./apps/web/dist' --exclude='./.DS_Store' \
    --exclude='./brainstorm.md' --exclude='./development_plan.md' --exclude='./handoff.md' \
    -czf "$out" \
    pyproject.toml uv.lock infra apps packages deploy
extra=$(tar -tzf "$out" | grep -E '(^|/)(\.env[^/]*|.*\.pem|.*\.key|id_rsa[^/]*|\.venv|node_modules)(/|$)' \
        | grep -v '\.env\.example$' || true)
if [ -n "$extra" ]; then
  rm -f "$out"
  echo "refusing: the tarball would contain secret-looking or generated paths:" >&2
  echo "$extra" | head -5 >&2
  exit 1
fi
echo "packed $(tar -tzf "$out" | wc -l | tr -d ' ') files into $out"
