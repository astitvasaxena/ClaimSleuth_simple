#!/bin/bash
# Removes everything the AWS deployment created (instance + its disk, security group, key pair).
# Reads ids from ~/.claimsleuth-aws/resources.env (written during the deployment). Stops all charges.
set -uo pipefail
export AWS_PAGER=""
D="${CLAIMSLEUTH_AWS_DIR:-$HOME/.claimsleuth-aws}"; source "$D/resources.env"
read -r -p "Terminate instance $IID and delete its security group and key pair in $R? type yes: " a; [ "$a" = "yes" ] || exit 1
aws ec2 terminate-instances --region "$R" --instance-ids "$IID" >/dev/null
aws ec2 wait instance-terminated --region "$R" --instance-ids "$IID"
[ -n "${EIP_ALLOC:-}" ] && aws ec2 release-address --region "$R" --allocation-id "$EIP_ALLOC"   # Elastic IPs are billed, release it
aws ec2 delete-security-group --region "$R" --group-id "$SG"
aws ec2 delete-key-pair --region "$R" --key-name claimsleuth-key
rm -f "$D/claimsleuth-key.pem"
echo "done; the site password file and notes stay in $D until you delete them"
