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
    Matches attempted per invocation. Measured at ~2.3/second with 8-way
    concurrency — 800 took 354s of the 900s ceiling — so 1500 uses the budget
    without relying on the handler's deadline guard to cut it short.
  EOT
  type        = number
  default     = 1500
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

variable "cohort_batches_per_run" {
  description = <<-EOT
    /players calls per invocation, 10 accounts each. Five expands 50 players and
    leaves headroom under the 10 req/min cap for the /samples call alongside it.
  EOT
  type        = number
  default     = 5
}

variable "cohort_pause_above" {
  description = <<-EOT
    Pause player-history discovery once this many matches are already queued.
    One cohort batch finds ~8,600 matches while an invocation fetches ~1,500, and
    PUBG deletes matches after 14 days — so an unbounded queue converts discovery
    straight into expired rows rather than data.
  EOT
  type        = number
  default     = 20000
}
