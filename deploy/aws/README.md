# AWS: one EC2 t4g.small (ARM), no Elastic IP

Same one-VM design as GCP: one instance, one Docker image (`Dockerfile.single`, 911 MB), Caddy in front (HTTPS + password),
a named volume for the SQLite blackboard. Nothing here was run against a real AWS account (see "What was tested").
You run every `aws` command yourself; no step needs a secret in this repo.

## Why these choices
- t4g.small: 2 vCPU (Graviton, ARM), 2 GB RAM. Measured on Docker Desktop (ARM): whole stack about 400 MiB at peak while running the
  sample claims, so 2 GB is enough; a 2 GB swap file is added as a safety margin and for the image build.
- ARM64 on purpose: the image builds natively on ARM (python:3.12-slim and caddy are multi-arch). If you build on an x86 laptop, build ON THE VM, do not copy the image.
- No Elastic IP: the public IPv4 changes every time the instance is stopped and started (a plain reboot keeps it).
  `deploy/boot.sh` runs at every boot, reads the current public IP from the metadata service (IMDSv2) and sets the site name to
  `<ip-with-dashes>.sslip.io` (e.g. `13-232-10-5.sslip.io`, a free public DNS name that resolves to that IP), then restarts the stack so Caddy gets a certificate for it.
  THE URL THEREFORE CHANGES AFTER EVERY STOP/START. Find the new one with the command in step 7. If you need a stable URL, attach an Elastic IP (it is free while attached to a running instance, billed when idle) or use your own domain and set `SITE_ADDRESS` in `deploy/.env`.
- IMDSv2 required (`HttpTokens=required`): the metadata service only answers token-authenticated requests.

## Prerequisites (you)
AWS account with a payment method, AWS CLI v2 configured (`aws configure` or SSO), a region (examples use `ap-south-1`), and an SSH key pair.

## Step by step
Set variables once (all later steps use them):
```
export AWS_REGION=ap-south-1
export KEY_NAME=claimsleuth-key
export MY_IP="$(curl -s https://checkip.amazonaws.com)/32"
```
1. Key pair (skip if you have one):
   `aws ec2 create-key-pair --region $AWS_REGION --key-name $KEY_NAME --query KeyMaterial --output text > claimsleuth-key.pem && chmod 400 claimsleuth-key.pem`
2. Security group (80 and 443 public, 22 only from your IP), default VPC:
```
SG=$(aws ec2 create-security-group --region $AWS_REGION --group-name claimsleuth-sg --description "ClaimSleuth web" --query GroupId --output text)
aws ec2 authorize-security-group-ingress --region $AWS_REGION --group-id $SG --ip-permissions \
  IpProtocol=tcp,FromPort=80,ToPort=80,IpRanges='[{CidrIp=0.0.0.0/0}]' \
  IpProtocol=tcp,FromPort=443,ToPort=443,IpRanges='[{CidrIp=0.0.0.0/0}]' \
  IpProtocol=tcp,FromPort=22,ToPort=22,IpRanges="[{CidrIp=$MY_IP}]"
```
3. Ubuntu 24.04 arm64 AMI (looked up from the public SSM parameter, so it is always current):
   `AMI=$(aws ssm get-parameter --region $AWS_REGION --name /aws/service/canonical/ubuntu/server/24.04/stable/current/arm64/hvm/ebs-gp3/ami-id --query Parameter.Value --output text)`
4. Launch (20 GB gp3, IMDSv2 required, public IP assigned, user data = `deploy/aws/user-data.sh`):
```
aws ec2 run-instances --region $AWS_REGION --image-id $AMI --instance-type t4g.small --key-name $KEY_NAME \
  --security-group-ids $SG --associate-public-ip-address \
  --metadata-options HttpTokens=required,HttpEndpoint=enabled,HttpPutResponseHopLimit=2 \
  --block-device-mappings 'DeviceName=/dev/sda1,Ebs={VolumeSize=20,VolumeType=gp3,DeleteOnTermination=true}' \
  --user-data file://deploy/aws/user-data.sh \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=claimsleuth}]' \
  --query 'Instances[0].InstanceId' --output text
```
   (`HttpPutResponseHopLimit=2` lets the metadata call work from inside a container too; the host script needs only 1.)
   User data installs Docker, creates the 2 GB swap file (`/swapfile`), sets Docker log rotation and installs `claimsleuth.service`. Allow about 2 minutes.
5. Get the public IP and copy the code (from the repo root; excludes venvs, docs, tests, secrets):
```
IID=<instance id printed above>
IP=$(aws ec2 describe-instances --region $AWS_REGION --instance-ids $IID --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)
tar --exclude=.git --exclude=.venv --exclude=.venv-crew --exclude=runtime --exclude=docs --exclude=presentation \
    --exclude=.claude --exclude=tests --exclude=deploy/.env --exclude=deploy/site.env --exclude='*.bak-*' --exclude='__pycache__' -czf /tmp/claimsleuth.tgz .
scp -i claimsleuth-key.pem /tmp/claimsleuth.tgz ubuntu@$IP:/tmp/
ssh -i claimsleuth-key.pem ubuntu@$IP 'cloud-init status --wait; cd /opt/claimsleuth && tar -xzf /tmp/claimsleuth.tgz && cp deploy/.env.example deploy/.env && chmod 600 deploy/.env'
```
6. On the VM: set the password and (optionally) live mode, then start.
```
ssh -i claimsleuth-key.pem ubuntu@$IP
cd /opt/claimsleuth
sudo docker run --rm caddy:2-alpine caddy hash-password --plaintext 'CHOOSE_A_PASSWORD'   # copy the output
nano deploy/.env      # BASIC_AUTH_HASH='<paste, single-quoted>'; leave SITE_ADDRESS=auto; MODE=offline (default) or live + GOOGLE_API_KEY
sudo bash deploy/boot.sh        # first start builds the image (about 5 to 10 minutes on a t4g.small), prints the https URL
```
7. After ANY stop/start, find the new URL (boot.sh already restarted the stack by itself when the instance came up):
   `aws ec2 describe-instances --region $AWS_REGION --instance-ids $IID --query 'Reservations[0].Instances[0].PublicIpAddress' --output text`
   then replace dots by dashes: `13.232.10.5` becomes `https://13-232-10-5.sslip.io/`. Or on the VM: `cat /opt/claimsleuth/deploy/site.env`.
   Allow 30 to 90 seconds after boot for the certificate. If a certificate is refused, wait a few minutes (sslip.io is shared by many users and Let's Encrypt rate limits it; Caddy retries and falls back to another CA).
8. Smoke test: see `docs/DEPLOYMENT_GUIDE.md` (login prompt, status panel, one sample claim, restart).

## Lightsail alternative
Lightsail 2 GB (about 10 to 12 USD/month, includes a static IP option and 3 TB transfer) works the same way: pick Ubuntu 24.04, paste `deploy/aws/user-data.sh` as the launch script, open ports 80/443 in the Lightsail firewall, then follow steps 5 to 8. Lightsail static IPs are free while attached, which avoids the changing URL.

## Secrets
`GOOGLE_API_KEY` only in `/opt/claimsleuth/deploy/.env` (chmod 600) on the VM, or AWS Secrets Manager / SSM Parameter Store (`SecureString`) fetched into that file by hand. Never in the image or repo. The instance has no IAM role (none needed).

## Cost (estimate, check the AWS pricing page for your region)
t4g.small about 0.017 to 0.02 USD/hour on demand (about 12 to 15 USD/month if left on), 20 GB gp3 about 1.6 to 2 USD/month (billed even when the instance is stopped), public IPv4 address about 0.005 USD/hour while the instance runs (since Feb 2024, about 3.6 USD/month). A stopped instance costs only its disk. Free-tier eligibility for t4g.small trials varies by account and date: check yours.

## Operations
- Logs: `sudo docker compose logs -f app caddy` (rotated 10 MB x 3 per container).
- Memory: `sudo docker stats --no-stream`; swap: `swapon --show`.
- Update code: repeat the tar/scp from step 5, then `sudo bash deploy/boot.sh` (rebuilds; the `runtime` volume and old runs are kept).
- Stop to save money: `aws ec2 stop-instances --region $AWS_REGION --instance-ids $IID`. Start: `aws ec2 start-instances ...`, then step 7 for the new URL. Volume data and `.env` survive.

## Rollback
Keep the previous tarball; extract it over `/opt/claimsleuth` and run `sudo bash deploy/boot.sh`. Or set `MODE=offline` in `deploy/.env` and run `sudo bash deploy/boot.sh`.

## Teardown (stops all charges)
```
aws ec2 terminate-instances --region $AWS_REGION --instance-ids $IID
aws ec2 wait instance-terminated --region $AWS_REGION --instance-ids $IID
aws ec2 delete-security-group --region $AWS_REGION --group-id $SG
aws ec2 delete-key-pair --region $AWS_REGION --key-name $KEY_NAME      # optional; also delete claimsleuth-key.pem locally
```
(The root volume is deleted with the instance. If you created an Elastic IP or a snapshot, release/delete it too.)

## What was tested
Locally (Docker Desktop, ARM): the slim image, the 4 sample claims (4 of 4 match), the production compose file with Caddy and basic auth on port 80 (401 without password, 200 with), and `deploy/boot.sh` with mocked IMDSv2 responses. NOT tested: any `aws` command, the user-data script on a real instance, Let's Encrypt/sslip.io certificate issuance, behaviour on 2 GB RAM, `docker compose build` on a t4g.small. Expect to fix small things on the first real run.
