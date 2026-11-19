#!/bin/bash
# Runs as root on EVERY boot (installed as the systemd unit claimsleuth.service by deploy/aws/user-data.sh and
# deploy/gcp/startup.sh) and can be run by hand:  sudo bash /opt/claimsleuth/deploy/boot.sh
# 1. If SITE_ADDRESS in deploy/.env is unset, empty or "auto", compute it from this instance's CURRENT public IPv4
#    (AWS IMDSv2, GCP metadata fallback) as <ip-with-dashes>.sslip.io and write it to deploy/site.env.
#    sslip.io is a public DNS service: 13-232-10-5.sslip.io resolves to 13.232.10.5, so Caddy can get a real certificate
#    without a domain. Without an Elastic IP the public IP (and so the URL) changes after every stop/start.
# 2. (Re)start the compose stack so Caddy picks up the new name.
# Nothing here prints secrets. Safe to run repeatedly.
set -uo pipefail
APP_DIR="${APP_DIR:-/opt/claimsleuth}"
cd "$APP_DIR" || { echo "boot.sh: $APP_DIR missing (code not copied yet)"; exit 0; }
[ -f deploy/.env ] || { echo "boot.sh: deploy/.env missing; create it from deploy/.env.example first"; exit 0; }

configured="$(grep -E '^SITE_ADDRESS=' deploy/.env | tail -n1 | cut -d= -f2- | tr -d "\"' ")"
: > deploy/site.env
if [ -z "$configured" ] || [ "$configured" = "auto" ]; then
  ip=""
  for i in 1 2 3 4 5 6 7 8 9 10; do   # the network may not be up the instant the unit starts
    token="$(curl -fsS -m 3 -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 300' 2>/dev/null || true)"
    if [ -n "$token" ]; then
      ip="$(curl -fsS -m 3 -H "X-aws-ec2-metadata-token: $token" http://169.254.169.254/latest/meta-data/public-ipv4 2>/dev/null || true)"
    fi
    [ -z "$ip" ] && ip="$(curl -fsS -m 3 -H 'Metadata-Flavor: Google' \
      http://metadata.google.internal/computeMetadata/v1/instance/network-interfaces/0/access-configs/0/external-ip 2>/dev/null || true)"
    echo "$ip" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' && break
    ip=""; sleep 3
  done
  if [ -n "$ip" ]; then
    echo "SITE_ADDRESS=${ip//./-}.sslip.io" > deploy/site.env
    echo "boot.sh: public IP $ip -> https://${ip//./-}.sslip.io/"
  else
    echo "boot.sh: no public IPv4 found; serving plain HTTP on :80 (SITE_ADDRESS=:80)"
    echo "SITE_ADDRESS=:80" > deploy/site.env
  fi
fi
chmod 600 deploy/site.env
# later --env-file wins, so site.env overrides an empty/auto SITE_ADDRESS from .env
exec docker compose --env-file deploy/.env --env-file deploy/site.env up -d --build --remove-orphans
