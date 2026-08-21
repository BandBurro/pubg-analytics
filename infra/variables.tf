variable "region" {
  description = "AWS region to deploy into."
  type        = string
  default     = "us-east-1"
}

variable "aws_profile" {
  description = <<-EOT
    Named AWS CLI profile to use. Deliberately has no default so a stray
    `default` profile — possibly a work account — can never be picked up by
    accident.
  EOT
  type        = string
}

variable "budget_alert_email" {
  description = "Email for cost alerts. Set this before anything else."
  type        = string
}

variable "monthly_budget_usd" {
  description = "Alert thresholds are a percentage of this."
  type        = number
  default     = 20
}

variable "collect_schedule" {
  description = "EventBridge schedule expression for the collector."
  type        = string
  default     = "rate(2 hours)"
}

variable "max_fetch_per_run" {
  description = <<-EOT
    Matches attempted per invocation. The observed rate is roughly 2/second at
    8-way concurrency, so 800 fits comfortably inside the 15-minute ceiling with
    room to spare.
  EOT
  type        = number
  default     = 800
}

variable "raw_retention_days" {
  description = <<-EOT
    Days before raw objects move to Glacier Instant Retrieval. Bronze is
    immutable and rarely re-read once shredded, but it must stay retrievable —
    PUBG deletes matches after 14 days, so this data cannot be re-collected.
  EOT
  type        = number
  default     = 90
}
