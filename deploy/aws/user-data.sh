#!/bin/bash
# EC2 user data for Ubuntu 24.04 LTS arm64 (t4g.small). Runs once, as root, on first boot.
# Installs Docker, adds a 2 GB swap file (the instance has only 2 GB RAM), and installs a systemd unit that runs
# /opt/claimsleuth/deploy/boot.sh on every boot (computes <ip-with-dashes>.sslip.io from the current public IP, then
# docker compose up). Contains no secrets.
set -euxo pipefail
exec > /var/log/claimsleuth-user-data.log 2>&1

# 2 GB swap so image builds and a run peak cannot get the instance OOM-killed
if [ ! -f /swapfile ]; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
  echo 'vm.swappiness=10' > /etc/sysctl.d/99-swappiness.conf && sysctl -p /etc/sysctl.d/99-swappiness.conf
fi

apt-get update -y
apt-get install -y ca-certificates curl
curl -fsSL https://get.docker.com | sh
systemctl enable --now docker
# Rotate container logs by default too (compose also sets per-service limits)
cat > /etc/docker/daemon.json <<'JSON'
{"log-driver": "json-file", "log-opts": {"max-size": "10m", "max-file": "3"}}
JSON
systemctl restart docker

mkdir -p /opt/claimsleuth && chown ubuntu:ubuntu /opt/claimsleuth

# The unit is installed now; it does nothing until the code and deploy/.env exist (boot.sh exits quietly if missing).
cat > /etc/systemd/system/claimsleuth.service <<'UNIT'
[Unit]
Description=ClaimSleuth stack (recompute public hostname, docker compose up)
After=docker.service network-online.target
Wants=network-online.target
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
TimeoutStartSec=1800
ExecStart=/bin/bash /opt/claimsleuth/deploy/boot.sh

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable claimsleuth.service
echo "user-data finished"
