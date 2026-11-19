#!/bin/bash
# One-time, on the VM:  sudo bash /opt/claimsleuth/deploy/install_autoupdate.sh
# Installs a systemd timer that runs deploy/auto_update.sh every minute (and 2 minutes after boot).
set -euo pipefail
command -v git >/dev/null && command -v rsync >/dev/null || { apt-get update -y && apt-get install -y git rsync; }
cat > /etc/systemd/system/claimsleuth-update.service <<'UNIT'
[Unit]
Description=ClaimSleuth auto-update from GitHub
After=docker.service network-online.target
[Service]
Type=oneshot
TimeoutStartSec=2400
ExecStart=/bin/bash /opt/claimsleuth/deploy/auto_update.sh
UNIT
cat > /etc/systemd/system/claimsleuth-update.timer <<'UNIT'
[Unit]
Description=Check GitHub for a new ClaimSleuth version every minute
[Timer]
OnBootSec=120
OnUnitInactiveSec=60
[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl enable --now claimsleuth-update.timer
echo "installed. Logs: journalctl -u claimsleuth-update -f   Pause: set AUTO_UPDATE=off in deploy/.env"
