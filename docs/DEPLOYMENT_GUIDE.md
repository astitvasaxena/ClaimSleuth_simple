# ClaimSleuth deployment guide

## Which option should I use?
| Option | Card needed? | Cost | Fits the SQLite blackboard? | Effort | Notes |
|---|---|---|---|---|---|
| Local Docker (any laptop) | No | 0 | Yes (one volume) | 5 min | Best for a live demo in the room: `docker compose` with `docker-compose.test.yml`. |
| Hugging Face Spaces (Docker SDK) | No | 0 (free CPU) | Yes: single container, ephemeral disk | 20 min | `Dockerfile.single`, see `deploy/huggingface/README.md`. Data lost on restart. |
| GCP Compute Engine VM | Yes (billing account) | about 30 USD/month if left on; a few USD for a week | Yes (one VM, shared volume) | 45 min | Full production shape: one image, Caddy, HTTPS, basic auth. `deploy/gcp/`. |
| AWS EC2 t4g.small (ARM, 2 GB) / Lightsail | Yes | about 12 to 15 USD/month plus disk and IPv4 (about 17 USD/month total if left on) | Yes (same as GCP) | 45 min | `deploy/aws/README.md`: complete walk-through, no Elastic IP needed. Recommended AWS shape. |
| GCP Cloud Run / AWS Fargate | Yes | pay per use | No (see below) | n/a | Not supported by this design. |

Honest note: a Hugging Face Space is a cloud platform and is a real public deployment, but it is not AWS or GCP. If the
assignment specifically requires AWS or GCP, only the two VM rows meet it, and both need a billing account with a card
(GCP also offers a free-trial credit that still asks for a card). The Space runs the identical application code and the
same four sample decisions (see `docs/submission/results/deploy_test.md`); it differs in packaging (one container instead
of two images plus Caddy) and in having no password prompt of its own (public or private Space, see the Space README).

## What gets deployed (VM option)
One VM runs two containers: `app` (image from `Dockerfile.single` at 911 MB: dashboard + Google ADK agents + MCP server + the CrewAI Forensic Agent service, which the dashboard starts itself on 127.0.0.1 inside the container) and `caddy` (HTTPS and password prompt). Only ports 80 and 443 are public. The two Python environments cannot be merged (conflicting opentelemetry-api and mcp versions), so the one image holds two separate virtual environments (`/app/.venv`, `/app/.venv-crew`). The earlier two-image layout (`Dockerfile`, `Dockerfile.crew`, `docker-compose.two-image.yml`) is kept only for comparison: it used the same memory but 1.95 GB of images instead of 0.91 GB and one more container. Numbers: `docs/submission/results/deploy_test.md`. The `runtime` volume holds the blackboard database and run history.

Default mode is OFFLINE: the agents are labelled rule-based test doubles, not an LLM; no key is needed. Set `MODE=live` plus `GOOGLE_API_KEY` for Gemini (free tier about 20 requests/day/model; a run costs 6 to 9 requests).

Why not Cloud Run: the blackboard is a SQLite (WAL) file that two services write to. Cloud Run instances do not share a local disk, and network file mounts do not give SQLite the locking it needs, so the two services would see different databases or corrupt it. One VM with one shared volume is the supported shape.

## Security notes
- The dashboard has no login of its own. Caddy asks for a username and password (basic auth). The reviewer name inside the dashboard is still typed in by the user and is not tied to the login.
- `GOOGLE_API_KEY` lives only in `deploy/.env` on the VM (chmod 600). It is not in the images or the repo.
- The dashboard binds 127.0.0.1 by default; the container sets `CLAIMSLEUTH_HOST=0.0.0.0` and is reachable only through Caddy (its port 7860 is not published).
- The VM's address is not fixed (see below). Basic auth over HTTPS protects it; do not put real claim data in a demo VM.

## Prerequisites (you do these)
1. Google Cloud account with billing, `gcloud` installed.
2. `gcloud auth login` then `gcloud config set project <YOUR_PROJECT_ID>` and enable Compute: `gcloud services enable compute.googleapis.com`.

## Steps
1. From the repo root: `bash deploy/gcp/create_vm.sh` (e2-medium, Ubuntu 24.04, firewall 80/443). It prints the external IP, e.g. 34.12.56.78.
2. Your hostname with `SITE_ADDRESS=auto`: `34-12-56-78.sslip.io` (free, resolves to that IP, gets a real certificate). Ephemeral GCP IPs also change after stop/start.
3. `bash deploy/gcp/push_and_run.sh`. First run copies the code, creates `/opt/claimsleuth/deploy/.env` and stops.
4. `gcloud compute ssh claimsleuth-vm --zone asia-south1-a`, then in `/opt/claimsleuth`:
   `sudo docker run --rm caddy:2 caddy hash-password --plaintext 'CHOOSE_A_PASSWORD'` and put the output (single-quoted) in `deploy/.env` as `BASIC_AUTH_HASH`; leave `SITE_ADDRESS=auto` (or set your own hostname). For live mode set `MODE=live` and `GOOGLE_API_KEY`. Exit.
5. Run `bash deploy/gcp/push_and_run.sh` again (it runs `deploy/boot.sh` on the VM). First image build takes several minutes.
6. Open `https://<hostname>/` and log in.

### Hostname when the VM has no fixed IP (both clouds)
Leave `SITE_ADDRESS=auto` in `deploy/.env` (the default in `.env.example`). At every boot `deploy/boot.sh` reads the VM's current public IPv4 (AWS IMDSv2, or GCP metadata) and sets `SITE_ADDRESS` to `<ip-with-dashes>.sslip.io`, then restarts the stack. After a stop/start the IP, and therefore the URL, changes: for `13.232.10.5` it is `https://13-232-10-5.sslip.io/`. A plain reboot keeps the IP. To get a fixed URL, reserve a static IP (GCP) / Elastic IP (AWS) or use your own domain and put it in `SITE_ADDRESS`.

AWS: see `deploy/aws/README.md` (t4g.small, user data, security group, swap, IMDSv2, step by step).

## Smoke test
- [ ] `https://<hostname>/` shows a login prompt, then the dashboard.
- [ ] On the VM, `sudo docker compose ps` shows app (healthy) and caddy running.
- [ ] Dashboard status panel: MCP server and CrewAI A2A service RUNNING; banner says OFFLINE (or LIVE if configured).
- [ ] Run one built-in sample claim; decision, audit trail and blackboard view appear.
- [ ] After a stop/start the new URL (step above) works without any manual edit.
- [ ] `http://<ip>/` redirects to HTTPS; ports other than 80/443/22 are closed.
- [ ] Restart test: `sudo docker compose restart` then repeat the sample run; earlier runs are still listed (volume kept).

## Cost estimate
e2-medium about 0.04 USD/hour (roughly 30 USD/month if left on) plus about 1.3 USD/month for the 30 GB disk and a small egress charge. Running for a week of assessment is a few USD. Check the current price in the GCP calculator; this is an estimate, not a measurement. Gemini free tier costs nothing but is rate limited.

## Operations
- Logs: `sudo docker compose logs -f app caddy` (json-file, rotated at 10 MB x 3).
- Restart policy is `unless-stopped`; the VM reboot brings the stack back once Docker starts.

## Rollback
Re-copy the previous code tarball and run `sudo bash deploy/boot.sh`. Data in the `runtime` volume survives. To return to offline, set `MODE=offline` and run `sudo bash deploy/boot.sh`.

## Teardown
`bash deploy/gcp/teardown.sh` deletes the VM and firewall rule (the volume goes with the VM disk).

## Local test (no cloud, no card)
```
BASIC_AUTH_HASH=x docker compose -f docker-compose.yml -f docker-compose.test.yml up -d --build   # dashboard on http://127.0.0.1:8110 (container port 7860), Caddy disabled
BASIC_AUTH_HASH=x docker compose -f docker-compose.yml -f docker-compose.test.yml down -v
```
`docker-compose.test.yml` is for testing only: it skips Caddy, so there is no password and no HTTPS. Never use it on a public machine.
`CLAIMSLEUTH_BLACKBOARD_DB=/app/runtime/blackboard.sqlite3` is set in `docker-compose.yml`. (In the old two-container layout the CrewAI container lacked it and the Forensic Agent reported "not run"; with one image the dashboard passes it on to the CrewAI service.)

## AWS result (2026-11-19)
Deployed on AWS EC2 t4g.small (Ubuntu 24.04 ARM, ap-south-1, 20 GB gp3 encrypted, IMDSv2, no Elastic IP) with `deploy/aws/user-data.sh` and `deploy/boot.sh`: Docker Compose, Caddy with Let's Encrypt on `<ip>.sslip.io`, the 911 MB single image, OFFLINE mode. Verified over HTTPS: wrong password rejected (401); four sample claims gave APPROVE, ESCALATE, ESCALATE, REQUEST_EVIDENCE; after a stop/start the public IP changed and the site came back by itself on the new hostname. Record: `docs/submission/DEPLOYED.txt`. Live mode was not enabled. Teardown: `deploy/aws/teardown.sh`.

### Lesson: SSH and a home connection with several public IPs
A home connection can leave through several public IPs. A security group that allows port 22 only from the one IP seen when the rule was created then refuses some connections at random. Use SSH `ControlMaster`/`ControlPersist` so one authenticated connection is reused for all later commands, or open port 22 temporarily (for example to your /24 or the current IPs) and close it again when finished. Ports 80 and 443 stay public.

## Status of this guide
Updated 2026-11-19. Deployed on AWS and verified (see "AWS result"). Local tests before that (Docker Desktop, ARM, OFFLINE): the slim single image (911 MB, was 1.74 GB), the four sample claims (4 of 4 match), peak memory 383 MiB for the app container plus about 14 MiB for Caddy, the production compose file with Caddy and basic auth on port 80. NOT executed: `deploy/gcp/*` and `deploy/huggingface/*`, live mode, the pruned image in live mode. Memory on the EC2 instance was not measured. Results: `docs/submission/results/deploy_test.md`.
