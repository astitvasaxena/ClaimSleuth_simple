# ClaimSleuth on AWS: architecture and deployment plan

Written 2026-11-19, before the deployment. Region `ap-south-1` (Mumbai), default AWS CLI profile, account on the AWS Free plan
($100 credits, plan ends 2027-01-30). Facts below come from read-only AWS CLI checks made the same day and from
`docs/submission/results/deploy_test.md`.

## 1. Target architecture

```
 Browser (HTTPS, basic auth)
        |
        v   443 / 80 (Security Group: 80+443 open to all, 22 only from the deployer's IP)
 +--------------------------- EC2 t4g.small (Ubuntu 24.04 arm64, 2 vCPU, 2 GB RAM + 2 GB swap, 20 GB gp3) ---------------------------+
 |  Docker                                                                                                                         |
 |   caddy:2-alpine  -- TLS certificate for <ip-with-dashes>.sslip.io, basic auth, gzip --> app:7860                                |
 |   claimsleuth-single (911 MB, non-root uid 1000)                                                                                |
 |     dashboard (Starlette) -> ADK agents (Evidence, Investigator, in .venv)                                                      |
 |                           -> MCP server :8102 (policy search, claim history)    all on 127.0.0.1 inside the container          |
 |                           -> CrewAI Forensic Agent as an A2A service :8103 (.venv-crew)                                         |
 |     SQLite blackboard + run records on the named volume `runtime`                                                               |
 |  systemd unit claimsleuth.service -> deploy/boot.sh on every boot (recomputes the sslip.io host from the current public IP)       |
 +-----------------------------------------------------------------------------------------------------------------------------------+
        |  outbound HTTPS only, and only in live mode: Gemini API
```

Why one VM and one container: the blackboard is a SQLite database in WAL mode shared by two processes, which needs one machine
and a local disk. That rules out Lambda, S3-hosted front end with API functions, and App Runner or Fargate with separate
services. Measured footprint: 339 MiB idle, 383 MiB peak during runs, Caddy 14 MiB, so 2 GB is a safe margin.

## 2. Resources to create (all tagged `Project=claimsleuth`)
| Resource | Setting | Reason |
|---|---|---|
| Key pair `claimsleuth-key` | private key saved to `~/.claimsleuth-aws/` (chmod 400), outside the repo | SSH for deployment |
| Security group `claimsleuth-sg` (default VPC) | 80, 443 from 0.0.0.0/0; 22 from the deployer's current IP/32 | minimal exposure |
| EC2 instance | t4g.small, Ubuntu 24.04 arm64 (latest from the public SSM parameter), 20 GB gp3 encrypted, IMDSv2 required, auto-assigned public IPv4 at launch, Elastic IP 13.204.3.14 attached on 2026-11-19, no IAM role | measured need, tested architecture |
| User data | `deploy/aws/user-data.sh`: swap, Docker, log rotation, `claimsleuth.service` | repeatable, no secrets |
Added later (2026-11-19): the Elastic IP (billed about 0.005 USD/h, also while stopped; released by `teardown.sh`), URL `https://13-204-3-14.sslip.io/`, live mode, and the systemd timer `claimsleuth-update.timer` that pulls `origin/main` (rollback on failed health check; `AUTO_UPDATE=off` pauses it). Not created: load balancer, NAT gateway, S3, ECR, IAM roles, RDS, budgets (none needed).

## 3. Cost (live prices from the Pricing API, ap-south-1)
t4g.small 0.0112 USD/h (about 8 USD/month) + public IPv4 0.005 USD/h (about 3.6) + 20 GB gp3 (about 2) = roughly 14 USD per month,
about 35 USD until the plan ends. A stopped instance costs only its disk.

## 4. Deployment sequence
1. Pre-flight: identity, region, instance type offered, AMI lookup, dry-run launch (done, all passed).
2. Generate the site password locally (stored in `~/.claimsleuth-aws/site_password.txt`, chmod 600, never in the repo or chat logs).
3. Create key pair and security group; launch the instance with user data; wait for `running` and for cloud-init.
4. Upload the source tarball (a few MB; the image is built ON the VM, because the home connection is slow and the VM has fast downloads).
5. On the VM: create `deploy/.env` (`SITE_ADDRESS=auto`, hashed password, `MODE=offline`), run `deploy/boot.sh` (builds, starts, prints the URL).
6. Verify over HTTPS: 401 without the password, 200 with it, then the four sample claims through the API (expect APPROVE, ESCALATE,
   ESCALATE, REQUEST_EVIDENCE) and a reboot test (stack comes back by itself; new hostname if the IP changed).
7. Record the result in `docs/submission/DEPLOYED.txt` (no password) so the report and slides pick it up.

## 5. Risks and fallbacks
| Risk | Fallback |
|---|---|
| Free plan refuses t4g.small at launch | launch t3.small instead (x86; build happens on the VM, so it still works) |
| Let's Encrypt rate limit or failure on sslip.io | Caddy retries and tries another CA; else serve HTTP on :80 with `SITE_ADDRESS=:80` for the demo |
| Image build runs out of memory on 2 GB | swap is already added; stop and retry, or resize to t4g.medium for the build only |
| Deployer's IP changes (SSH locked out) | re-authorise the new IP in the security group (one CLI call) |
| Public IP and URL change after stop/start | Fixed by the Elastic IP (stable URL). `boot.sh` still recomputes the hostname from the IP the metadata service returns |
| Anything goes wrong | `deploy/aws/teardown.sh` terminates the instance and removes the security group and key pair |

## 6. Security notes
Basic auth in front of a dashboard that has no login of its own (the reviewer name is still typed in). No secrets in the image or
repo; `GOOGLE_API_KEY` is optional and only ever placed in `deploy/.env` on the VM. SSH limited to one IP. IMDSv2 only.
Disk encrypted. The AWS access key used for deployment belongs to an administrator user: rotate or delete it after the assessment.
The GitHub repository is public, so nothing secret may be committed.
