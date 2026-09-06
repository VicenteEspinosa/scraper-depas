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
#   DEPLOY_PATH, GITHUB_SHA, ENV_B64, MIN_FREE_MB (optional)

set -euo pipefail

: "${DEPLOY_PATH:?missing}"
: "${GITHUB_SHA:?missing}"
: "${ENV_B64:?missing}"

# Room a native `docker compose build` needs for a fresh layer set beside the running
# one. Checked before anything is written, because a box that fills up does not fail
# where the space ran out: it failed at `sed: couldn't flush` while rendering .env,
# which names a temp file nobody has heard of instead of the disk.
MIN_FREE_MB="${MIN_FREE_MB:-2048}"

log() { printf '\n=== %s ===\n' "$*"; }

# -P so a long device name cannot wrap onto its own line and shift the columns.
free_mb() { df -Pm "$1" | awk 'NR == 2 { print $4 }'; }

cd "$DEPLOY_PATH"
mkdir -p .rollback data

# The box hosts other stacks, and the Docker daemon is the one thing on it that is not
# scoped to this project. So neither of these may ever grow an `-a` on the images:
# `docker image prune -a` deletes every image no container references *daemon-wide*,
# which for a stack that is built on the box and pushed to no registry is unrecoverable
# without its source. Dangling images are untagged and referenced by nothing, and build
# cache rebuilds itself -- between them that is all `docker system df` calls reclaimable.
reclaim() {
  docker image prune -f || true
  docker builder prune -f --filter "until=${CACHE_KEEP_HOURS:-24}h" || true
}

log "check free space"
free=$(free_mb .)
if [ "$free" -lt "$MIN_FREE_MB" ]; then
  # Only when short: the build cache is what makes the next build quick, so it is
  # worth keeping right up until it is worth less than the deploy it is blocking.
  log "${free}MB free, under ${MIN_FREE_MB}MB -- reclaiming what Docker can spare"
  reclaim
  free=$(free_mb .)
fi
echo "${free}MB free, ${MIN_FREE_MB}MB wanted"
if [ "$free" -lt "$MIN_FREE_MB" ]; then
  # Said before the first write, so the old containers are still serving and the log
  # names the disk rather than whichever command happened to need a temp file first.
  echo "refusing to deploy: ${free}MB free on $DEPLOY_PATH, need ${MIN_FREE_MB}MB." >&2
  echo "Docker has nothing left to reclaim; look at data/ and \`docker system df\`." >&2
  exit 1
fi

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

# Taken before the build, which retags `depas:local` out from under them. `compose
# images` answers for this project's containers only, so what comes back is ours.
previous=$(docker compose images -q 2>/dev/null | sort -u || true)

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

# The rebuild orphans the layer set the old containers were running and nothing ever
# collected it, so every deploy leaked one until the box filled up.
#
# Removed by id, one at a time, rather than pruned: a prune is daemon-wide and this box
# runs other stacks, so the only safe reap is the one that can name what it is reaping.
# `docker image rm` without -f refuses while anything at all still references the image,
# which is the answer we want on every doubt. Never fatal -- by here the deploy is
# applied, and a failed cleanup is the next deploy's problem, not this one's.
log "reap the images this build replaced"
current=$(docker compose images -q 2>/dev/null | sort -u || true)
for image in $previous; do
  if ! printf '%s\n' "$current" | grep -qxF "$image"; then
    if docker image rm "$image" >/dev/null 2>&1; then
      echo "removed $image"
    else
      echo "kept $image, something still references it"
    fi
  fi
done

log "deploy of ${GITHUB_SHA:0:7} applied, $(free_mb .)MB free"
