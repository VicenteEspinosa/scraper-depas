#!/usr/bin/env bash
# Runs on the deploy host, piped in over SSH by the deploy workflow. Writes .env
# from the base64 blob the workflow built, fast-forwards the checkout to the
# deploy commit, and rebuilds + restarts the containers. The image is built
# natively (arm64) on the host — there is no registry.
#
# Secrets arrive as a single base64 blob (ENV_B64), NOT as individual inline
# `VAR=value` ssh args: the remote shell re-expands $ sequences in such args, so
# a token containing $ would be corrupted before .env was ever written.
#
# Inputs (env vars set on the SSH invocation line):
#   DEPLOY_PATH, GITHUB_SHA, ENV_B64

set -euo pipefail

: "${DEPLOY_PATH:?missing}"
: "${GITHUB_SHA:?missing}"
: "${ENV_B64:?missing}"

log() { printf '\n=== %s ===\n' "$*"; }

cd "$DEPLOY_PATH"
mkdir -p .rollback data

log "snapshot current .env to .rollback/"
[ -f .env ] && cp -f .env .rollback/.env

log "decode .env from ENV_B64"
printf '%s' "$ENV_B64" | base64 -d > .env

# Compose interpolates $VAR / ${VAR} in env_file values. Escape every literal $
# as $$ so Compose collapses it back to a single $ inside the container.
sed -i 's/\$/$$/g' .env

log "fast-forward checkout to ${GITHUB_SHA:0:7}"
git fetch origin
git reset --hard "$GITHUB_SHA"

log "docker compose build"
docker compose build

# The settings are parsed on the way into the database now, so a .env the current
# parsers refuse stops `connect` -- and with `restart: unless-stopped` that is a crash
# loop, not an error anybody reads. Checking here, after the build and before the
# restart, turns it into a failed deploy with the old containers still serving. It
# opens nothing and writes nothing.
#
# stdin is this script: the workflow pipes it into `bash -s`. `run` attaches the
# container's stdin, so without </dev/null it reads the rest of the file and the
# deploy ends here, green, having restarted nothing.
log "validate .env against the settings registry"
docker compose run --rm -T depas-bot depas config check < /dev/null

log "docker compose up -d"
docker compose up -d

echo "$GITHUB_SHA" > .last-deployed-sha
log "deploy of ${GITHUB_SHA:0:7} applied"
