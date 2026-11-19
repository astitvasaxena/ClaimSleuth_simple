#!/bin/bash
# Creates the VM + firewall rule. YOU run this after `gcloud auth login` and `gcloud config set project <id>`.
set -euo pipefail
ZONE="${ZONE:-asia-south1-a}"; VM="${VM:-claimsleuth-vm}"
HERE="$(cd "$(dirname "$0")" && pwd)"
gcloud compute instances create "$VM" --zone "$ZONE" --machine-type e2-medium \
  --image-family ubuntu-2404-lts-amd64 --image-project ubuntu-os-cloud --boot-disk-size 30GB \
  --tags claimsleuth-web --metadata-from-file startup-script="$HERE/startup.sh"
gcloud compute firewall-rules create claimsleuth-allow-web --allow tcp:80,tcp:443 \
  --target-tags claimsleuth-web --source-ranges 0.0.0.0/0 2>/dev/null || echo "firewall rule already exists"
gcloud compute instances describe "$VM" --zone "$ZONE" --format 'get(networkInterfaces[0].accessConfigs[0].natIP)'
