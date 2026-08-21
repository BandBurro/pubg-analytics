# Storage, ledger state, and the cost guardrail.

locals {
  name   = "pubg-analytics"
  bucket = "${local.name}-raw-${data.aws_caller_identity.current.account_id}"
}

# ------------------------------------------------------------------ raw lake

resource "aws_s3_bucket" "raw" {
  bucket = local.bucket
}

# Bronze is append-only by design, so versioning would double the bill to
# protect against overwrites that never happen.
resource "aws_s3_bucket_public_access_block" "raw" {
  bucket                  = aws_s3_bucket.raw.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "raw" {
  bucket = aws_s3_bucket.raw.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "raw" {
  bucket = aws_s3_bucket.raw.id

  rule {
    id     = "archive-raw"
    status = "Enabled"
    filter {
      prefix = "raw/"
    }

    # Glacier Instant Retrieval keeps millisecond reads at roughly a quarter of
    # Standard's storage cost. Worth it here: this data is re-read rarely but
    # cannot be re-collected at all.
    transition {
      days          = var.raw_retention_days
      storage_class = "GLACIER_IR"
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

# ------------------------------------------------------------------ ledger

resource "aws_dynamodb_table" "ledger" {
  name         = "${local.name}-ledger"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "match_id"

  attribute {
    name = "match_id"
    type = "S"
  }
  attribute {
    name = "status"
    type = "S"
  }
  attribute {
    name = "discovered_at"
    type = "S"
  }

  # Lets the collector ask for "oldest pending" instead of scanning the table.
  #
  # Caveat worth knowing: `status` has only four values, so this index has four
  # partitions and writes concentrate on 'pending'. That is a genuine hot-partition
  # pattern and it is fine at a few tens of thousands of items; it would not be at
  # millions, where the key would need a shard suffix.
  global_secondary_index {
    name            = "status_index"
    hash_key        = "status"
    range_key       = "discovered_at"
    projection_type = "KEYS_ONLY"
  }

  point_in_time_recovery {
    enabled = true
  }
}

# ------------------------------------------------------------------ secret

# SecureString in Parameter Store rather than Secrets Manager: standard
# parameters are free, Secrets Manager is $0.40/secret/month for the same job.
resource "aws_ssm_parameter" "api_key" {
  name  = "/${local.name}/pubg_api_key"
  type  = "SecureString"
  value = "REPLACE_ME"

  # The real key is written out of band, never committed and never in state.
  lifecycle {
    ignore_changes = [value]
  }
}

# ------------------------------------------------------------------ cost guard

resource "aws_budgets_budget" "monthly" {
  name         = "${local.name}-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  # Alert on the way up, and again on a forecast overrun — the forecast is what
  # catches a resource left running before it has actually cost anything.
  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 25
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.budget_alert_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.budget_alert_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.budget_alert_email]
  }
}
