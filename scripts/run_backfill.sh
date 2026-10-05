#!/usr/bin/env bash
# Launch the one-off backfill worker inside us-east-2 and walk away.
#
# The instance ships the *working tree* rather than cloning GitHub: the code
# that runs should be the code that was tested minutes earlier, not whatever
# happens to be pushed. A forgotten `git push` would otherwise run a stale
# shredder over a corpus that cannot be re-collected.
#
# Instances terminate themselves when the job finishes. On-demand rather than
# spot: spot interruption would mean noticing and relaunching, which is worth
# more than the small discount on a job this cheap.
#
# Why several small machines instead of one big one: the account is on the AWS
# Free Plan, which refuses any instance type that is not free-tier-eligible —
# every one of which has 2 vCPU. `RunInstances` fails outright on a c7g.8xlarge.
# So the job scales out. Each instance takes every Nth batch; batches are
# independent, so the shards never coordinate and never collide. Total compute
# cost is the same, only the wall clock divides.
set -euo pipefail

cd "$(dirname "$0")/.."

: "${AWS_PROFILE:=pubg-personal}"
: "${AWS_REGION:=us-east-2}"
export AWS_PROFILE AWS_REGION

# m7i-flex.large is the roomiest free-tier-eligible type: 2 vCPU but 8 GB, and
# memory is what actually binds here — one worker holds a batch of 200 matches'
# position rows (~1.6M dicts) before flushing.
INSTANCE_TYPE="${INSTANCE_TYPE:-m7i-flex.large}"
SHARDS="${SHARDS:-4}"
WORKERS="${WORKERS:-2}"
BATCH="${BATCH:-200}"

BUCKET=$(cd infra && tofu output -raw raw_bucket)
PROFILE_NAME=$(cd infra && tofu output -raw backfill_instance_profile)
AMI=$(aws ssm get-parameter \
  --name /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64 \
  --query 'Parameter.Value' --output text)
SUBNET=$(aws ec2 describe-subnets --filters Name=default-for-az,Values=true \
  --query 'Subnets[0].SubnetId' --output text)

echo "bucket=$BUCKET  type=$INSTANCE_TYPE  shards=$SHARDS  workers=$WORKERS  batch=$BATCH"

# Exactly the files the worker needs. Shipping data/ too would mean uploading
# 109 GB of raw telemetry to run a job whose entire purpose is reading it from
# S3 in the first place.
echo "packaging working tree..."
tar czf /tmp/backfill-code.tar.gz src scripts/cloud_shred.py pyproject.toml
aws s3 cp /tmp/backfill-code.tar.gz "s3://${BUCKET}/backfill/code.tar.gz" --quiet
echo "code uploaded"

# Re-running this script is expected: AWS refuses launches during first-time
# regional verification, so some shards land and others do not. Skipping shards
# that are already up makes the retry safe — otherwise a second run doubles the
# instance count and pays twice for work the running shard is already doing.
# (It would not corrupt anything: batches are deterministic and markers make
# the output idempotent. It would just cost twice as much.)
RUNNING=$(aws ec2 describe-instances \
  --filters Name=tag:Project,Values=pubg-analytics \
  Name=instance-state-name,Values=pending,running \
  --query 'Reservations[].Instances[].Tags[?Key==`Name`].Value' --output text)

IDS=()
for SHARD in $(seq 0 $((SHARDS - 1))); do
  if grep -qw "pubg-analytics-backfill-${SHARD}" <<<"$RUNNING"; then
    echo "  shard ${SHARD}/${SHARDS} -> already running, skipping"
    continue
  fi
  USER_DATA=$(cat <<EOF
#!/bin/bash
set -xeuo pipefail
exec > >(tee /var/log/backfill.log) 2>&1

dnf install -y -q python3.12 tar gzip

python3.12 -m venv /opt/venv
/opt/venv/bin/pip install -q --upgrade pip
/opt/venv/bin/pip install -q polars orjson boto3

mkdir -p /opt/backfill && cd /opt/backfill
aws s3 cp "s3://${BUCKET}/backfill/code.tar.gz" code.tar.gz
tar xzf code.tar.gz

export AWS_REGION="${AWS_REGION}"
set +e
/opt/venv/bin/python scripts/cloud_shred.py \\
  --bucket "${BUCKET}" --workers ${WORKERS} --batch ${BATCH} \\
  --shard ${SHARD} --shards ${SHARDS}
RC=\$?
set -e

# The log is the only record of a machine that is about to delete itself.
aws s3 cp /var/log/backfill.log \\
  "s3://${BUCKET}/backfill/logs/shard${SHARD}-\$(date +%Y%m%dT%H%M%S)-rc\${RC}.log"
shutdown -h now
EOF
)

  ID=$(aws ec2 run-instances \
    --image-id "$AMI" \
    --instance-type "$INSTANCE_TYPE" \
    --subnet-id "$SUBNET" \
    --iam-instance-profile "Name=${PROFILE_NAME}" \
    --instance-initiated-shutdown-behavior terminate \
    --block-device-mappings 'DeviceName=/dev/xvda,Ebs={VolumeSize=30,VolumeType=gp3,DeleteOnTermination=true}' \
    --metadata-options 'HttpTokens=required,HttpEndpoint=enabled' \
    --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=pubg-analytics-backfill-${SHARD}},{Key=Project,Value=pubg-analytics}]" \
    --user-data "$USER_DATA" \
    --query 'Instances[0].InstanceId' --output text)
  IDS+=("$ID")
  echo "  shard ${SHARD}/${SHARDS} -> ${ID}"
done

echo
echo "launched ${#IDS[@]} instances: ${IDS[*]}"
echo
echo "watch progress (2,573 batches total):"
echo "  aws s3 ls s3://${BUCKET}/backfill/done/ --profile ${AWS_PROFILE} --region ${AWS_REGION} | wc -l"
echo "kill them all:"
echo "  aws ec2 terminate-instances --instance-ids ${IDS[*]} --profile ${AWS_PROFILE} --region ${AWS_REGION}"
