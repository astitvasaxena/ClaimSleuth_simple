#!/bin/bash
# GCP startup script. GCP runs it as root on EVERY boot. First boot: installs Docker. Every boot: if the code and
# deploy/.env are already on the VM, recompute SITE_ADDRESS (when it is unset/auto) and run the stack (deploy/boot.sh).
set -uo pipefail
if ! command -v docker >/dev/null 2>&1; then
  apt-get update -y && apt-get install -y ca-certificates curl
  curl -fsSL https://get.docker.com | sh
  systemctl enable --now docker
  mkdir -p /opt/claimsleuth
fi
[ -f /opt/claimsleuth/deploy/boot.sh ] && bash /opt/claimsleuth/deploy/boot.sh
exit 0
