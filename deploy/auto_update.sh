#!/bin/bash
# Pull-based auto-deploy. Runs as root every minute from the systemd timer claimsleuth-update.timer (installed by
# deploy/install_autoupdate.sh). The repo is public, so no credentials are needed.
#   1. fetch REPO_URL / BRANCH (deploy/.env; defaults below) into /opt/claimsleuth-src
#   2. if the commit differs from deploy/.deployed_sha, copy the files into /opt/claimsleuth (deploy/.env, deploy/site.env,
#      the Docker volumes (runs, history) are never touched) and run deploy/boot.sh (docker compose up -d --build)
#   3. wait for the app container to be healthy; if not, restore the previous commit and rebuild (rollback)
# Safe to run by hand: sudo bash /opt/claimsleuth/deploy/auto_update.sh. Prints no secrets. Log: journalctl -u claimsleuth-update
set -uo pipefail
APP_DIR="${APP_DIR:-/opt/claimsleuth}"
SRC_DIR="${SRC_DIR:-/opt/claimsleuth-src}"
cd "$APP_DIR" || exit 0
exec 9>/var/lock/claimsleuth-update.lock
flock -n 9 || { echo "update already running"; exit 0; }

getv() { grep -E "^$1=" deploy/.env 2>/dev/null | tail -n1 | cut -d= -f2- | tr -d "\"' "; }
REPO_URL="$(getv REPO_URL)"; REPO_URL="${REPO_URL:-https://github.com/astitvasaxena/ClaimSleuth_simple.git}"
BRANCH="$(getv BRANCH)"; BRANCH="${BRANCH:-main}"
[ "$(getv AUTO_UPDATE)" = "off" ] && { echo "AUTO_UPDATE=off, skipping"; exit 0; }

if [ ! -d "$SRC_DIR/.git" ]; then
  git clone --quiet --branch "$BRANCH" "$REPO_URL" "$SRC_DIR" || { echo "clone failed"; exit 0; }
fi
git -C "$SRC_DIR" remote set-url origin "$REPO_URL"
git -C "$SRC_DIR" fetch --quiet origin "$BRANCH" || { echo "fetch failed (offline?), keeping current version"; exit 0; }
NEW="$(git -C "$SRC_DIR" rev-parse "origin/$BRANCH")"
OLD="$(cat deploy/.deployed_sha 2>/dev/null || echo none)"
[ "$NEW" = "$OLD" ] && exit 0

sync_to() {  # $1 = commit
  git -C "$SRC_DIR" checkout --quiet --force "$1" || return 1
  rsync -a --delete --exclude '.git' --exclude 'deploy/.env' --exclude 'deploy/site.env' --exclude 'deploy/.deployed_sha' \
        --exclude 'runtime' --exclude 'docs' --exclude 'tests' --exclude '.claude' --exclude '.github' --exclude '*.bak-*' \
        --exclude '__pycache__' "$SRC_DIR/" "$APP_DIR/"
}
healthy() {
  for i in $(seq 1 40); do   # up to ~10 minutes (first build on a t4g.small is slow)
    st="$(docker inspect -f '{{.State.Health.Status}}' "$(docker compose ps -q app 2>/dev/null)" 2>/dev/null || true)"
    [ "$st" = "healthy" ] && return 0
    sleep 15
  done
  return 1
}

echo "deploying $NEW (was $OLD) from $REPO_URL $BRANCH"
if sync_to "$NEW" && bash deploy/boot.sh && healthy; then
  echo "$NEW" > deploy/.deployed_sha
  echo "deployed $NEW"
  docker image prune -f >/dev/null 2>&1 || true
else
  echo "DEPLOY FAILED for $NEW"
  if [ "$OLD" != "none" ] && sync_to "$OLD" && bash deploy/boot.sh && healthy; then
    echo "rolled back to $OLD"
  else
    echo "rollback also failed; inspect: docker compose logs app"
  fi
  echo "$NEW" > deploy/.failed_sha   # not retried every minute; a newer commit on the branch is tried automatically
  echo "$NEW" > deploy/.deployed_sha
fi
