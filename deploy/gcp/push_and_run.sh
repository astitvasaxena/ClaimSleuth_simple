#!/bin/bash
# Copies the code to the VM and starts the stack. Run from the repo root. Needs deploy/.env prepared on the VM first
# (the script creates it from .env.example if missing, then stops so you can fill it in).
set -euo pipefail
ZONE="${ZONE:-asia-south1-a}"; VM="${VM:-claimsleuth-vm}"
TAR="$(mktemp -t claimsleuth.XXXXXX).tgz"
tar --exclude=.git --exclude=.venv --exclude=.venv-crew --exclude=runtime --exclude=docs --exclude=presentation \
    --exclude=.claude --exclude=deploy/site.env --exclude=tests --exclude="*.bak-*" --exclude=deploy/.env --exclude='__pycache__' -czf "$TAR" .
gcloud compute scp "$TAR" "$VM":/tmp/claimsleuth.tgz --zone "$ZONE"
gcloud compute ssh "$VM" --zone "$ZONE" --command '
  set -e
  until command -v docker >/dev/null 2>&1; do echo "waiting for docker install..."; sleep 10; done
  sudo mkdir -p /opt/claimsleuth && sudo chown "$USER" /opt/claimsleuth
  cd /opt/claimsleuth && tar -xzf /tmp/claimsleuth.tgz
  if [ ! -f deploy/.env ]; then cp deploy/.env.example deploy/.env; chmod 600 deploy/.env;
    echo "Edit /opt/claimsleuth/deploy/.env (BASIC_AUTH_HASH, SITE_ADDRESS), then re-run this script."; exit 3; fi
  sudo bash deploy/boot.sh
  sudo docker compose ps'
rm -f "$TAR"
