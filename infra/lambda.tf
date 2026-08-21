# The collector: a scheduled function, its permissions, and its logs.

# No dependencies means the deployment package is one source file. There is no
# build step, no layer and no container image to keep in sync.
data "archive_file" "collector" {
  type        = "zip"
  source_file = "${path.module}/../lambda/handler.py"
  output_path = "${path.module}/.build/collector.zip"
}

# ------------------------------------------------------------------ iam

data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "collector" {
  name               = "${local.name}-collector"
  assume_role_policy = data.aws_iam_policy_document.assume.json
}

# Scoped to exactly the resources this function touches. No wildcards on
# resources, no managed AdministratorAccess, no s3:* on every bucket.
data "aws_iam_policy_document" "collector" {
  statement {
    sid       = "WriteRawObjects"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.raw.arn}/raw/*"]
  }

  statement {
    sid = "LedgerReadWrite"
    actions = [
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
      "dynamodb:GetItem",
      "dynamodb:Query",
    ]
    resources = [
      aws_dynamodb_table.ledger.arn,
      "${aws_dynamodb_table.ledger.arn}/index/*",
    ]
  }

  statement {
    sid       = "ReadApiKey"
    actions   = ["ssm:GetParameter"]
    resources = [aws_ssm_parameter.api_key.arn]
  }

  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.collector.arn}:*"]
  }
}

resource "aws_iam_role_policy" "collector" {
  name   = "${local.name}-collector"
  role   = aws_iam_role.collector.id
  policy = data.aws_iam_policy_document.collector.json
}

# ------------------------------------------------------------------ function

# Declared explicitly rather than left to Lambda's auto-creation, so retention
# is set. Logs default to never expiring, which is a slow, silent cost.
resource "aws_cloudwatch_log_group" "collector" {
  name              = "/aws/lambda/${local.name}-collector"
  retention_in_days = 14
}

resource "aws_lambda_function" "collector" {
  function_name = "${local.name}-collector"
  role          = aws_iam_role.collector.arn
  handler       = "handler.handler"
  runtime       = "python3.13"

  # arm64 is roughly 20% cheaper per GB-second than x86_64, and this workload is
  # entirely IO-bound so the architecture is irrelevant to its speed.
  architectures = ["arm64"]

  filename         = data.archive_file.collector.output_path
  source_code_hash = data.archive_file.collector.output_base64sha256

  # Mostly waiting on the network, so memory is for throughput headroom rather
  # than compute. Timeout is the Lambda maximum; the handler stops itself early.
  memory_size = 1024
  timeout     = 900

  environment {
    variables = {
      BUCKET        = aws_s3_bucket.raw.id
      LEDGER_TABLE  = aws_dynamodb_table.ledger.name
      API_KEY_PARAM = aws_ssm_parameter.api_key.name
      SHARD         = "steam"
      MAX_FETCH     = tostring(var.max_fetch_per_run)
      CONCURRENCY   = "8"
    }
  }

  depends_on = [
    aws_iam_role_policy.collector,
    aws_cloudwatch_log_group.collector,
  ]
}

# ------------------------------------------------------------------ schedule

resource "aws_cloudwatch_event_rule" "collect" {
  name                = "${local.name}-collect"
  description         = "Run the PUBG collector on a schedule."
  schedule_expression = var.collect_schedule
}

resource "aws_cloudwatch_event_target" "collect" {
  rule = aws_cloudwatch_event_rule.collect.name
  arn  = aws_lambda_function.collector.arn
}

resource "aws_lambda_permission" "events" {
  statement_id  = "AllowEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.collector.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.collect.arn
}

# A function that stops running is indistinguishable from one with nothing to do,
# unless something is watching. This alarms on errors rather than on silence.
resource "aws_cloudwatch_metric_alarm" "collector_errors" {
  alarm_name          = "${local.name}-collector-errors"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "Errors"
  namespace           = "AWS/Lambda"
  period              = 3600
  statistic           = "Sum"
  threshold           = 0
  treat_missing_data  = "notBreaching"

  dimensions = {
    FunctionName = aws_lambda_function.collector.function_name
  }
}
