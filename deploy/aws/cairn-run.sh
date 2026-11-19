#!/bin/bash
# Create (not start) the Cairn container with its production configuration.
#
#   cairn-run.sh create     read the secrets from SSM Parameter Store, write them to tmpfs files,
#                           and `docker create` the container. cairn-waker starts and stops it.
#
# Secrets never touch the repository or the disk: they are read with the instance role from
# /cairn/* in SSM (SecureString), written to /run (tmpfs, mode 600), and handed to docker as an
# env-file. Nothing is echoed. Run as root (systemd does, before cairn-waker starts).
set -euo pipefail
umask 077

REGION="${AWS_DEFAULT_REGION:-ap-south-1}"
IMAGE="${CAIRN_IMAGE:-cairn-combined:aws}"
NAME="${CAIRN_CONTAINER:-cairn}"
RUN_DIR=/run/cairn
APP_PORT="${CAIRN_APP_PORT:-8004}"

REQUIRED=(
  CAIRN_DB_HOST CAIRN_APP_DB_PASSWORD CAIRN_DBOS_DB_PASSWORD CAIRN_INGEST_DB_PASSWORD
  CAIRN_SESSION_SECRET CAIRN_AUDIT_HASH_KEY CAIRN_PROXY_TOKEN
)

[ "${1:-create}" = "create" ] || { echo "usage: cairn-run.sh create" >&2; exit 2; }

mkdir -p "$RUN_DIR"
chmod 700 "$RUN_DIR"
SECRETS="$RUN_DIR/secrets.env"
: > "$SECRETS"

# name<TAB>value lines; values are written as KEY=value (they are url-safe tokens or passwords
# that docker's env-file reads literally, one per line)
aws ssm get-parameters-by-path --region "$REGION" --path /cairn/ --with-decryption \
  --query 'Parameters[].[Name,Value]' --output text |
  while IFS=$'\t' read -r name value; do
    printf '%s=%s\n' "${name#/cairn/}" "$value" >> "$SECRETS"
  done

missing=()
for key in "${REQUIRED[@]}"; do
  grep -q "^${key}=." "$SECRETS" || missing+=("$key")
done
if [ "${#missing[@]}" -gt 0 ]; then
  echo "missing SSM parameters under /cairn/: ${missing[*]}" >&2
  exit 1
fi

# the waker needs only the proxy token
grep '^CAIRN_PROXY_TOKEN=' "$SECRETS" > "$RUN_DIR/waker.env"

CONFIG="$RUN_DIR/config.env"
cat > "$CONFIG" <<CFG
CAIRN_ENV=demo
CAIRN_DEMO_PERSONAS=on
CAIRN_COGNITO_POOL_ID=ap-south-1_6tj46t5dr
CAIRN_COGNITO_CLIENT_ID=2fveh85r5r0j36n7h5dnsd4fp9
CAIRN_COGNITO_REGION=${REGION}
# demo: skip the email code at sign-up so a visitor can create a workspace at once
CAIRN_AUTH_AUTO_CONFIRM=true
CAIRN_LOG_LEVEL=INFO
PORT=${APP_PORT}
AWS_DEFAULT_REGION=${REGION}
OTEL_TRACES_EXPORTER=none
CAIRN_DB_PORT=5432
CAIRN_DB_NAME=cairn
CAIRN_DB_TIMEOUT_S=10
CAIRN_DB_SSLMODE=verify-full
CAIRN_DB_SSLROOTCERT=/etc/ssl/certs/ca-certificates.crt
CAIRN_EMBEDDER=bedrock
CAIRN_EMBED_MODEL=amazon.titan-embed-text-v2:0
CAIRN_EMBED_DIM=1024
CAIRN_RERANKER=bedrock
CAIRN_RERANK_MODEL=openai.gpt-oss-20b-1:0
CAIRN_RERANK_MIN_SCORE=2.0
CAIRN_ANSWER_PROVIDER=bedrock
CAIRN_ANSWER_MODEL=openai.gpt-oss-120b-1:0
CAIRN_JUDGE_PROVIDER=bedrock
CAIRN_JUDGE_MODEL=qwen.qwen3-32b-v1:0
CAIRN_BEDROCK_REGION=${REGION}
CAIRN_BEDROCK_DAILY_USD=2.0
CAIRN_BEDROCK_BUDGET_STORE=postgres
CAIRN_BEDROCK_REASONING_EFFORT=low
CAIRN_LLM_TIMEOUT_S=60
CFG

docker rm -f "$NAME" >/dev/null 2>&1 || true
docker create --name "$NAME" \
  --network host \
  --memory 512m --memory-swap 768m --cpus 1 \
  --read-only --tmpfs /tmp:size=64m \
  --cap-drop ALL --security-opt no-new-privileges \
  --restart no \
  --log-driver awslogs --log-opt "awslogs-region=$REGION" \
  --log-opt awslogs-group=/cairn/app --log-opt awslogs-create-group=false \
  --env-file "$CONFIG" --env-file "$SECRETS" \
  "$IMAGE" cairn-combined >/dev/null
echo "container $NAME created from $IMAGE"
