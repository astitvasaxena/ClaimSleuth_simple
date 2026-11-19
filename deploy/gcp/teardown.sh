#!/bin/bash
# Deletes the VM and firewall rule (stops all charges except any reserved IP you created yourself).
set -euo pipefail
ZONE="${ZONE:-asia-south1-a}"; VM="${VM:-claimsleuth-vm}"
gcloud compute instances delete "$VM" --zone "$ZONE" --quiet
gcloud compute firewall-rules delete claimsleuth-allow-web --quiet
