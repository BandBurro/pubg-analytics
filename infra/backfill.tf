# Permissions for the one-off backfill worker that shreds the raw corpus into
# Parquet from inside the region.
#
# Only the *role* lives in Terraform. The instance itself is launched by
# scripts/run_backfill.sh and terminates itself when the job finishes, because
# a self-destroying resource and a desired-state tool disagree by construction:
# Terraform would see the terminated instance as drift and recreate it on the
# next apply, which is exactly the runaway this whole exercise is meant to stop.
#
# Egress is the reason the work happens here at all. S3 charges $0.09/GB to
# leave the region beyond 100 GB/month, so shredding ~1 TB on a laptop would
# cost ~$80 — several years of the storage bill it is meant to remove.

data "aws_iam_policy_document" "backfill_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "backfill" {
  name               = "${local.name}-backfill"
  assume_role_policy = data.aws_iam_policy_document.backfill_assume.json
}

data "aws_iam_policy_document" "backfill" {
  statement {
    sid       = "ReadRawCorpus"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.raw.arn}/raw/*"]
  }

  # Separate statement from the read above so the write surface is visible at a
  # glance: the worker may create Parquet and progress markers, and nothing else.
  statement {
    sid     = "WriteBronzeAndMarkers"
    actions = ["s3:PutObject", "s3:GetObject"]
    resources = [
      "${aws_s3_bucket.raw.arn}/bronze/*",
      "${aws_s3_bucket.raw.arn}/backfill/*",
    ]
  }

  statement {
    sid       = "ListForManifest"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.raw.arn]
  }
}

# Deliberately absent: s3:DeleteObject.
#
# Deleting the raw telemetry is the irreversible half of this plan and PUBG
# cannot re-serve a match older than 14 days. Withholding the permission means a
# bug in the worker — a bad path join, an over-broad prefix, a retry loop —
# cannot destroy the corpus it is in the middle of reading. Deletion is done
# later, deliberately, by a separate credential against a verified list.
resource "aws_iam_role_policy" "backfill" {
  name   = "${local.name}-backfill"
  role   = aws_iam_role.backfill.id
  policy = data.aws_iam_policy_document.backfill.json
}

# Session Manager, so the worker can be watched without opening port 22,
# managing a key pair, or giving the instance a public ingress path at all.
resource "aws_iam_role_policy_attachment" "backfill_ssm" {
  role       = aws_iam_role.backfill.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "backfill" {
  name = "${local.name}-backfill"
  role = aws_iam_role.backfill.name
}

output "backfill_instance_profile" {
  description = "Instance profile for scripts/run_backfill.sh."
  value       = aws_iam_instance_profile.backfill.name
}
